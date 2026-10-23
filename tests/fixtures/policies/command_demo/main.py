"""Local command examples; explicit locking protects state shared with inference."""
import threading
import time

import numpy as np


def initialize(config):
    return {"lock": threading.Lock(), "calls": 0, "last_command": None}


def policy(context, state, action_chunk, images, config):
    with context["lock"]:
        context["calls"] += 1
    return np.repeat(np.asarray(state["qpos"])[None, :], config["params"]["chunk_size"], axis=0)


def test(context, text, count):
    with context["lock"]:
        context["last_command"] = {"text": text, "count": count}
        return {**context["last_command"], "policy_calls": context["calls"]}


def delay(context, seconds):
    if seconds < 0:
        raise ValueError("seconds must be nonnegative")
    time.sleep(seconds)
    return {"waited_s": seconds}


def reset_context(context):
    with context["lock"]:
        context["calls"] = 0


def release(context):
    pass
