"""Return an absolute qpos chunk that holds the measured pose."""

import numpy as np


def initialize(config):
    return {"config": config}


def policy(context, state, action_chunk, images, config):
    # The worker updates this same config object in place on accepted hot patches.
    chunk_size = context["config"]["params"]["chunk_size"]
    qpos = np.asarray(state["qpos"], dtype=np.float64).reshape(14)
    return np.repeat(qpos[None, :], chunk_size, axis=0)


def reset_context(context):
    pass


def release(context):
    pass
