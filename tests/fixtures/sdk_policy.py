import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np

_config = None


def _rapid_detach(params):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"], start_new_session=True)
    Path(params["child_file"]).write_text(str(child.pid))
    os._exit(42)


def initialize(config):
    global _config
    _config = config
    params = config["params"]
    if params.get("rapid_detach") == "initialize":
        _rapid_detach(params)
    if params.get("fail_init"):
        raise ValueError("intentional initialize failure")
    if params.get("child_file"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"], start_new_session=params.get("detached_child", False))
        Path(params["child_file"]).write_text(str(child.pid))
    time.sleep(params.get("init_delay", 0))
    return {"config": config, "resets": 0}


def policy(context, state, action_chunk, images, config):
    if config["params"].get("rapid_detach") == "infer":
        _rapid_detach(config["params"])
    time.sleep(config["params"].get("delay", 0))
    if config["params"].get("crash"):
        os._exit(42)
    if config["params"].get("retain_image"):
        context["retained_image"] = images["head"]
    actions = np.zeros((2, 14), dtype=float)
    actions[:, 0] = config["params"].get("value", 0)
    actions[:, 1] = context["config"] is config
    actions[:, 2] = context["resets"]
    actions[:, 3] = not images["head"].flags.writeable
    if config["params"].get("invalid"):
        actions[0, 0] = float("nan")
    return actions


def reset_context(context):
    context["resets"] += 1


def release(context):
    params = _config["params"]
    if params.get("release_file"):
        with open(params["release_file"], "a") as file:
            file.write("release\n")
    time.sleep(params.get("release_delay", 0))
