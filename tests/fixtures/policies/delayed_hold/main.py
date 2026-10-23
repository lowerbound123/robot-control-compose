"""A delayed measured-pose hold, useful for observing asynchronous exhaustion."""

import time

import numpy as np


def initialize(config):
    return {"config": config, "calls": 0}


def policy(context, state, action_chunk, images, config):
    params = context["config"]["params"]
    time.sleep(params["delay_s"])
    qpos = np.asarray(state["qpos"], dtype=np.float64).reshape(14)
    context["calls"] += 1
    return np.repeat(qpos[None, :], params["chunk_size"], axis=0)


def reset_context(context):
    context["calls"] = 0


def release(context):
    if context is not None:
        context["released"] = True
