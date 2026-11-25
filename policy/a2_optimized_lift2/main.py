from pathlib import Path

from rc_qwen35_history.a2.entry import (
    configure_a2, export_config, initialize_a2, monitor, observe,
    resume_context, switch_model, switch_task, validate_config,
)
from .runtime import OptimizedPolicy

POLICY_ROOT = Path(__file__).resolve().parent
ROBOT_TYPE = OptimizedPolicy.robot_type


def initialize(config, *, runtime):
    return initialize_a2(config, POLICY_ROOT, OptimizedPolicy, ROBOT_TYPE)


def policy(context, state, action_chunk, images, config, *, runtime):
    return context.step(state, action_chunk, images, runtime=runtime)


def reset_context(context):
    context.reset()


def release(context):
    if context is not None:
        context.close()


def configure(context, config, key, value, callback):
    return configure_a2(context, config, key, value, callback, POLICY_ROOT, ROBOT_TYPE)
