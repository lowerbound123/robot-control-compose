"""Cold display settings, independent of Textual and terminal protocols."""
import math


def tui_settings(raw=None):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - {"camera", "monitor"}:
        raise ValueError("tui must contain only camera and monitor settings")
    camera, monitor = raw.get("camera", {}), raw.get("monitor", {})
    if not isinstance(camera, dict) or set(camera) - {"protocol"}:
        raise ValueError("tui.camera only accepts protocol")
    protocol = camera.get("protocol", "kitty")
    if protocol not in ("kitty", "sixel"):
        raise ValueError("tui.camera.protocol must be kitty or sixel")
    if not isinstance(monitor, dict) or set(monitor) - {"qpos_thres", "epos_thres"}:
        raise ValueError("tui.monitor only accepts qpos_thres and epos_thres")
    defaults = {
        "qpos_thres": ([.02] * 6 + [.12]) * 2,
        "epos_thres": ([.005] * 3 + [.05] * 3 + [.12]) * 2,
    }
    values = {}
    for name, default in defaults.items():
        value = monitor.get(name, default)
        if (not isinstance(value, list) or len(value) != 14
                or any(type(item) not in (int, float) or not math.isfinite(item) or item < 0 for item in value)):
            raise ValueError(f"tui.monitor.{name} must contain 14 finite nonnegative numbers")
        values[name] = list(value)
    return {"camera": {"protocol": protocol}, "monitor": values}
