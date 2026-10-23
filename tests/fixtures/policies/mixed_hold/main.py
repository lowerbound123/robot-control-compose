"""Mixed-format example: alternate measured joint and end-effector holds."""
import numpy as np


def initialize(config):
    return None


def policy(context, state, action_chunk, images, config):
    size = config["params"]["chunk_size"]
    if type(size) is not int or size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    modes = ["qpos" if index % 2 == 0 else "epos" for index in range(size)]
    return {"actions": np.asarray([state["qpos" if mode == "qpos" else "eef"] for mode in modes]), "modes": modes}


def reset_context(context):
    pass


def release(context):
    pass
