"""Tick-driven manual motion. Called exclusively by the RC control thread."""
from collections import deque
import copy
import math
import time

import numpy as np
from scipy.spatial.transform import Rotation

from .direct_commands import MOTION_AXES, Step
from .safety import ARMS, interpolate_pose, rotation_gap

from .direct_config import DEFAULTS, control_settings

class DirectMotion:
    def __init__(self, config, frequency_hz, operations, send, robot):
        self.config = control_settings(config)
        self.hz, self.operations, self.send, self.robot = frequency_hz, operations, send, robot
        self.pending = deque()
        self.active = None
        self.batches = {}
        self.targets = {}

    def enqueue(self, identity, steps, clear_queue=False):
        if not steps or any(not isinstance(step, Step) or not step.arms for step in steps):
            raise ValueError("a nonempty validated motion sequence is required")
        count = (0 if clear_queue else len(self.pending)) + len(steps) + bool(self.active)
        if count > self.config["max_pending_steps"]:
            raise ValueError("direct motion queue is full")
        if clear_queue:
            self.cancel("pending queue cleared", active=False)
        self.batches[identity] = {"steps": len(steps), "completed_steps": 0, "cancelled_steps": 0, "failed_steps": 0}
        self.pending.extend((identity, step) for step in steps)

    def _finish_step(self, identity, kind, error=None):
        batch = self.batches[identity]
        batch[kind + "_steps"] += 1
        complete = sum(batch[key] for key in ("completed_steps", "cancelled_steps", "failed_steps")) == batch["steps"]
        status = ("failed" if batch["failed_steps"] else "cancelled" if batch["cancelled_steps"] else "succeeded") if complete else "running"
        self.operations.update(identity, status, result=batch, error=error)
        if complete:
            self.batches.pop(identity)

    def cancel(self, reason, *, active=True):
        while self.pending:
            identity, _ = self.pending.popleft()
            self._finish_step(identity, "cancelled", reason)
        if active and self.active:
            identity = self.active["identity"]
            self.active = None
            self._finish_step(identity, "cancelled", reason)
        if active:
            self.targets.clear()

    def progress(self):
        active = self.active
        return {"pending_steps": len(self.pending), "active_operation": active["identity"] if active else None,
                "phase": active["phase"] if active else "idle", "targets": copy.deepcopy(self.targets),
                "arrival": copy.deepcopy(active.get("arrival", {})) if active else {}}

    def _baseline(self, arm, mode, state):
        index = ARMS.index(arm) * 7
        measured = np.asarray(state["qpos" if mode == "qpos" else "eef"][index:index+7], float)
        prior = self.targets.get(arm)
        if prior:
            measured[6] = prior["values"][6]
            if prior["mode"] == mode:
                measured[:6] = prior["values"][:6]
        return measured

    def _resolve(self, step, state):
        starts, goals, speeds = {}, {}, {}
        for target in step.targets:
            mode = "qpos" if target.mode == 5 else "epos"
            start = self._baseline(target.arm, mode, state)
            gripper = start[6] if target.gripper is None else target.gripper
            starts[target.arm], speeds[target.arm] = start, target.speed
            goals[target.arm] = {"mode": mode, "values": [*target.values, gripper]}
        for clause in step.clauses:
            for arm in clause.arms:
                mode = ("qpos" if clause.motions == ("home",) else "epos" if any(name in MOTION_AXES for name in clause.motions)
                        else self.targets.get(arm, {}).get("mode", "qpos"))
                start = self._baseline(arm, mode, state)
                goal, delta = start.copy(), np.zeros(6)
                for name in clause.motions:
                    if name in MOTION_AXES:
                        axis, sign = MOTION_AXES[name]
                        value = clause.value if clause.value is not None else self.config["delta_pos" if axis < 3 else "delta_rot"]
                        delta[axis] += value * sign
                    elif name in ("open", "close"):
                        goal[6] = self.config["gripper_" + name] if clause.value is None else clause.value
                    elif name == "home":
                        goal[:] = 0
                if mode == "epos":
                    goal[:3] += delta[:3]
                    if np.any(delta[3:]):
                        goal[3:6] = (Rotation.from_euler("xyz", goal[3:6]) * Rotation.from_rotvec(delta[3:])).as_euler("xyz")
                starts[arm], speeds[arm] = start, clause.speed
                goals[arm] = {"mode": mode, "values": goal.tolist()}
        return starts, goals, speeds

    def _begin(self, identity, step, state):
        starts, goals, speeds = self._resolve(step, state)
        ticks = {}
        for arm, goal in goals.items():
            start, end = starts[arm], np.asarray(goal["values"])
            speed = self.config["speeds"][speeds[arm]]
            factor = self.config["reference_frequency_hz"] / self.hz
            ratios = [np.max(np.abs(end[:6] - start[:6])) / (speed["angle_rad"] * factor)] if goal["mode"] == "qpos" else [
                np.linalg.norm(end[:3] - start[:3]) / (speed["translation_m"] * factor),
                rotation_gap(start[3:6], end[3:6]) / (speed["angle_rad"] * factor)]
            ticks[arm] = max(1, math.ceil(max(ratios) - 1e-10))
        if max(ticks.values()) / self.hz > self.config["step_timeout_s"]:
            raise ValueError("trajectory exceeds step timeout")
        self.targets.update(copy.deepcopy(goals))
        self.active = {"identity": identity, "starts": starts, "goals": goals, "ticks": ticks, "tick": 0,
                       "deadline": time.monotonic() + self.config["step_timeout_s"], "phase": "interpolating",
                       "boundary": {}, "settled": {arm: 0 for arm in goals}}
        self.operations.update(identity, "running")

    def _error(self, target, sample):
        goal, measured = np.asarray(target["values"]), np.asarray(sample[target["mode"]])
        if target["mode"] == "qpos":
            value = float(np.max(np.abs(goal[:6] - measured[:6])))
            result, within = {"joint_max_rad": value}, value <= self.config["joint_tolerance_rad"]
        else:
            distance = float(np.linalg.norm(goal[:3] - measured[:3]))
            rotation = rotation_gap(measured[3:6], goal[3:6])
            result = {"position_m": distance, "orientation_rad": rotation}
            within = distance <= self.config["position_tolerance_m"] and rotation <= self.config["rotation_tolerance_rad"]
        gripper = float(abs(goal[6] - measured[6]))
        return {**result, "gripper_error": gripper, "within_tolerance": bool(within and gripper <= self.config["gripper_tolerance"])}

    def tick(self, state):
        identity = None
        try:
            if not self.active:
                if not self.pending:
                    return False
                identity, step = self.pending.popleft()
                self._begin(identity, step, state)
            active = self.active
            identity = active["identity"]
            if time.monotonic() >= active["deadline"]:
                raise RuntimeError("direct motion timeout waiting for fresh arrival feedback")
            if any(age > self.config["feedback_max_age_s"] for age in state.get("feedback_age_s", {}).values()):
                raise RuntimeError("stale arm feedback")
            tick = min(active["tick"] + 1, max(active["ticks"].values()))
            commands = {}
            for arm, goal in active["goals"].items():
                fraction = min(1., tick / active["ticks"][arm])
                start, end = active["starts"][arm], np.asarray(goal["values"])
                values = end if fraction >= 1 else interpolate_pose(start, end, fraction) if goal["mode"] == "epos" else start + fraction * (end - start)
                commands[arm] = {"mode": goal["mode"], "values": values.tolist()}
            _, sent = self.send(commands, state)
            if sent:
                active["tick"] = tick
            after = self.robot.read_state()
            arrival = {}
            for arm, goal in active["goals"].items():
                if sent and tick >= active["ticks"][arm] and arm not in active["boundary"]:
                    active["boundary"][arm] = after["sequences"][arm]
                if arm in active["boundary"]:
                    for sample in self.robot.feedback_since(arm, active["boundary"][arm]):
                        fresh = time.monotonic() - sample["received"] <= self.config["feedback_max_age_s"]
                        active["settled"][arm] = active["settled"][arm] + 1 if fresh and self._error(goal, sample)["within_tolerance"] else 0
                        active["boundary"][arm] = sample["seq"]
                index = ARMS.index(arm) * 7
                arrival[arm] = {**self._error(goal, {"qpos": after["qpos"][index:index+7], "epos": after["eef"][index:index+7]}),
                                "settled_observations": active["settled"][arm]}
                if not arrival[arm]["within_tolerance"]:
                    active["settled"][arm] = 0
            active["arrival"] = arrival
            if active["tick"] >= max(active["ticks"].values()):
                active["phase"] = "waiting_for_arrival"
                if all(count >= self.config["settle_observations"] for count in active["settled"].values()):
                    self.active = None
                    self._finish_step(identity, "completed")
            return True
        except Exception as exc:
            self.active = None
            if identity is not None:
                self._finish_step(identity, "failed", exc)
            self.cancel(f"cancelled after direct control failure: {exc}")
            raise
