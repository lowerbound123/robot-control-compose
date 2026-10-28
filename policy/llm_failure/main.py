"""Passthrough chain member; network checks run only in its isolated worker."""
from rc_llm_failure.runtime import FailurePolicy


def initialize(config):
    return FailurePolicy(config)


def policy(context, state, action_chunk, images, config, *, runtime):
    return context.policy(action_chunk, images, runtime)


def reset_context(context):
    context.reset_context()


def release(context):
    if context is not None:
        context.close()
