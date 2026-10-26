"""Deliberately stuck monitor for process-tree/Compose shutdown acceptance."""
import time


def initialize(config):
    return config


def policy(context, state, action_chunk, images, config):
    return [state["qpos"]]


def monitor(context):
    print("monitor-stop-entered", flush=True)
    time.sleep(60)
    return {"finished": 1}


def reset_context(context):
    pass


def release(context):
    pass
