"""Proportional slew wall preserving the direction of a 14D target."""
from __future__ import annotations

import numpy as np

ARMS = ("left", "right")


def arm_targets(values, mode="qpos"):
    if mode not in ("qpos", "epos"):
        raise ValueError("mode must be qpos or epos")
    array = finite_qpos(values)
    return {arm: {"mode": mode, "values": array[index * 7:index * 7 + 7].tolist()}
            for index, arm in enumerate(ARMS)}


def validate_arm_targets(targets):
    if not isinstance(targets, dict) or not targets or set(targets) - set(ARMS):
        raise ValueError("targets must select left and/or right")
    result = {}
    for arm, target in targets.items():
        if not isinstance(target, dict) or target.get("mode") not in ("qpos", "epos"):
            raise ValueError("target mode must be qpos or epos")
        values = np.asarray(target.get("values"), dtype=float)
        if values.shape != (7,) or not np.isfinite(values).all():
            raise ValueError("arm target requires 7 finite values")
        result[arm] = {"mode": target["mode"], "values": values.tolist()}
    return result


def rotation_gap(start, goal):
    from scipy.spatial.transform import Rotation
    return float((Rotation.from_euler("xyz", start).inv() * Rotation.from_euler("xyz", goal)).magnitude())


def interpolate_pose(start, goal, fraction):
    import warnings
    from scipy.spatial.transform import Rotation, Slerp
    values = np.asarray(start) + fraction * (np.asarray(goal) - start)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Gimbal lock detected.*", category=UserWarning)
        values[3:6] = Slerp([0, 1], Rotation.from_euler("xyz", [start[3:6], goal[3:6]]))(fraction).as_euler("xyz")
    return values


def finite_qpos(value) -> np.ndarray:
    result = np.asarray(value, dtype=np.float32)
    if result.shape != (14,) or not np.isfinite(result).all():
        raise ValueError("qpos 必须为 14 维有限数值")
    return result


class SlewLimiter:
    def __init__(self, config: dict, frequency_hz: float):
        self.enabled = bool(config.get("enabled", True))
        reference = float(config.get("reference_frequency_hz", 50))
        if not np.isfinite(reference) or reference <= 0 or frequency_hz <= 0:
            raise ValueError("控制和限速参考频率必须为正数")
        limits = finite_qpos(config.get("max_delta_per_step", [-1] * 14))
        if np.any((limits <= 0) & (limits != -1)):
            raise ValueError("限速值必须为正数，或 -1 表示不限速")
        self.limits = np.where(limits < 0, np.inf, limits * reference / frequency_hz)
        self.previous: np.ndarray | None = None

    def reset(self, anchor) -> None:
        self.previous = finite_qpos(anchor).copy()

    def apply(self, target) -> tuple[np.ndarray, bool]:
        target = finite_qpos(target)
        if not self.enabled or self.previous is None:
            self.previous = target.copy()
            return self.previous.copy(), True
        delta = target - self.previous
        ratios = np.full(14, np.inf, dtype=np.float32)
        np.divide(self.limits, np.abs(delta), out=ratios, where=np.abs(delta) > 0)
        scale = min(1.0, float(np.min(ratios)))
        self.previous = target.copy() if scale >= 1 else (self.previous + delta * scale).astype(np.float32)
        return self.previous.copy(), scale >= 1


class ModeLimiter(SlewLimiter):
    """Limit physical units separately; never interpolate between representations."""
    def __init__(self, config, frequency_hz):
        super().__init__(config, frequency_hz)
        defaults = {"translation_m_s": .15, "rotation_rad_s": .75, "gripper_units_s": 2.5}
        epos = {**defaults, **config.get("epos", {})}
        if set(epos) != set(defaults) or any(isinstance(v, bool) or not np.isfinite(v) or v <= 0 for v in epos.values()):
            raise ValueError("safety.epos limits must be finite positive physical rates")
        self.epos_limits = {key: float(value) / frequency_hz for key, value in epos.items()}
        self.modes = ("qpos", "qpos")

    def reset(self, anchor, mode="qpos"):
        super().reset(anchor)
        self.modes = (mode, mode)

    def checkpoint(self):
        return None if self.previous is None else self.previous.copy(), self.modes

    def restore(self, checkpoint):
        previous, self.modes = checkpoint
        self.previous = None if previous is None else previous.copy()

    def commands(self):
        return {arm: {"mode": self.modes[i], "values": self.previous[i * 7:i * 7 + 7].tolist()}
                for i, arm in enumerate(ARMS)}

    def apply(self, target, mode="qpos", state=None):
        commands, complete = self.apply_arms(arm_targets(target, mode), state)
        return np.asarray(commands["left"]["values"] + commands["right"]["values"], np.float32), complete

    def apply_arms(self, targets, state):
        targets = validate_arm_targets(targets)
        starts, scales = {}, [1.0]
        if self.previous is None:
            if state is None:
                raise ValueError("feedback is required to anchor targets")
            self.reset(state["qpos"])
        for arm, target in targets.items():
            index = ARMS.index(arm)
            sl = slice(index * 7, index * 7 + 7)
            if self.modes[index] != target["mode"]:
                if state is None:
                    raise ValueError("fresh feedback required when changing mode")
                start = finite_qpos(state["qpos" if target["mode"] == "qpos" else "eef"])[sl].copy()
            else:
                start = self.previous[sl].copy()
            starts[arm] = start
            goal = np.asarray(target["values"])
            if not self.enabled:
                continue
            if target["mode"] == "qpos":
                delta = np.abs(goal - start)
                scales.extend((self.limits[sl][delta > 0] / delta[delta > 0]).tolist())
            else:
                gaps = (np.linalg.norm(goal[:3] - start[:3]), rotation_gap(start[3:6], goal[3:6]), abs(goal[6] - start[6]))
                limits = (self.epos_limits["translation_m_s"], self.epos_limits["rotation_rad_s"], self.epos_limits["gripper_units_s"])
                scales.extend(limit / gap for gap, limit in zip(gaps, limits) if gap > 0)
        scale = min(scales)
        commands, modes = {}, list(self.modes)
        for arm, target in targets.items():
            start, goal = starts[arm], np.asarray(target["values"])
            if scale >= 1:
                values = goal.copy()
            elif target["mode"] == "epos":
                values = interpolate_pose(start, goal, scale)
            else:
                values = start + scale * (goal - start)
            index = ARMS.index(arm)
            self.previous[index * 7:index * 7 + 7] = values
            modes[index] = target["mode"]
            commands[arm] = {"mode": target["mode"], "values": values.tolist()}
        self.modes = tuple(modes)
        return commands, scale >= 1
