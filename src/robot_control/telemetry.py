"""Pure display calculations; all values come from readonly snapshots."""
import math

CAMERAS = ("left_wrist", "head", "right_wrist")
CAMERA_PREVIEW_HZ = 10
ARMS = ("left", "right")


def monitor_cells(snapshot, settings):
    rows = {}
    for mode in ("qpos", "epos"):
        values = snapshot.get(mode)
        text, red = ["--"] * 14, [False] * 14
        if values is not None and not snapshot.get("error"):
            text = [f"{value:.4f}" for value in values]
            for arm_index, arm in enumerate(ARMS):
                target = snapshot.get("targets", {}).get(arm)
                if not target or target["mode"] != mode:
                    continue
                for component in range(7):
                    index = arm_index * 7 + component
                    delta = values[index] - target["values"][component]
                    if mode == "epos" and component in (3, 4, 5):
                        delta = (delta + math.pi) % (2 * math.pi) - math.pi
                    red[index] = abs(delta) > settings[mode + "_thres"][index]
        rows[mode] = {"text": text, "red": red}
    return rows
