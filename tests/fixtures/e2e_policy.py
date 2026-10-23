"""A real worker policy recording observable cross-process lifecycle events."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np


_config = None


def record(kind, **fields):
    event = {"kind": kind, "id": _config["id"], "pid": os.getpid(),
             "time_ns": time.monotonic_ns(), **fields}
    with Path(_config["params"]["events_path"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


def initialize(config):
    global _config
    _config = config
    record("initialize_start")
    if config["params"]["spawn_child"]:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=config["params"]["detached_child"],
        )
        record("child", child_pid=child.pid)
    time.sleep(config["params"]["initialize_delay_s"])
    record("initialize_done")
    return {"config": config, "resets": 0}


def policy(context, state, action_chunk, images, config):
    record("inference_start", value=context["config"]["params"]["value"],
           same_config=context["config"] is config,
           readonly=all(not image.flags.writeable for image in images.values()),
           cameras=sorted(images), remaining=len(action_chunk), qpos=state["qpos"])
    time.sleep(config["params"]["delay_s"])
    if config["params"].get("raise_inference"):
        raise RuntimeError("old policy inference failed during switch")
    actions = np.full((config["params"]["chunk_size"], 14),
                      context["config"]["params"]["value"], dtype=np.float64)
    record("inference_done")
    return actions


def reset_context(context):
    context["resets"] += 1
    record("reset_context", count=context["resets"])


def release(context):
    record("release_start")
    time.sleep(_config["params"]["release_delay_s"])
    record("release_done")
