"""Generate absolute targets at the measured pose."""

import numpy as np


def validate_config(config):
    size = config["params"]["chunk_size"]
    if type(size) is not int or size <= 0:
        raise ValueError("chunk_size must be a positive integer")


def initialize(config):
    return config


def policy(context, state, action_chunk, images, config):
    size = context["params"]["chunk_size"]
    mode = config["execution"]["control_mode"]
    if mode == "mixed":
        modes = ["qpos" if index % 2 == 0 else "epos" for index in range(size)]
        return {"actions": [state["qpos" if item == "qpos" else "eef"] for item in modes], "modes": modes}
    pose = np.asarray(state["qpos" if mode == "qpos" else "eef"], dtype=np.float64).reshape(14)
    return np.repeat(pose[None, :], size, axis=0)


def reset_context(context):
    pass  # This stage has no temporal state.


def release(context):
    pass  # This stage owns no external resources.
