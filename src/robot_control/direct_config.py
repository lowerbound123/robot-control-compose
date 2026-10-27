"""Direct-control settings, importable before numerical dependencies are installed."""
import copy
import math

DEFAULTS = {
    "default_speed": "slow", "reference_frequency_hz": 30,
    "speeds": {"slow": {"translation_m": .001, "angle_rad": .005},
               "medium": {"translation_m": .002, "angle_rad": .010},
               "fast": {"translation_m": .005, "angle_rad": .025}},
    "delta_pos": .05, "delta_rot": .1, "gripper_open": -3.4, "gripper_close": 0.,
    "position_tolerance_m": .005, "rotation_tolerance_rad": .05,
    "joint_tolerance_rad": .02, "gripper_tolerance": .12,
    "settle_observations": 2, "step_timeout_s": 60., "feedback_max_age_s": .5,
    "max_pending_steps": 256,
}


def control_settings(raw=None):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("unknown direct_control configuration")
    config = copy.deepcopy(DEFAULTS)
    config.update(raw)
    if config["default_speed"] not in ("slow", "medium", "fast"):
        raise ValueError("invalid direct_control.default_speed")
    for key in DEFAULTS:
        if key in ("speeds", "default_speed"):
            continue
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"direct_control.{key} must be finite")
        if key not in ("gripper_open", "gripper_close") and value <= 0:
            raise ValueError(f"direct_control.{key} must be positive")
    for key in ("settle_observations", "max_pending_steps"):
        if type(config[key]) is not int:
            raise ValueError(f"direct_control.{key} must be an integer")
    if not isinstance(config["speeds"], dict) or set(config["speeds"]) != {"slow", "medium", "fast"}:
        raise ValueError("speeds must define slow, medium and fast")
    for values in config["speeds"].values():
        if not isinstance(values, dict) or set(values) != {"translation_m", "angle_rad"}:
            raise ValueError("speed must define translation_m and angle_rad")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in values.values()):
            raise ValueError("speed limits must be finite and positive")
    return config
