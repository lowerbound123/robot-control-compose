"""LLM decisions -> ordinary epos action chunks; network runs only in this worker."""
from rc_llm_direction.runtime import DirectionPolicy


def initialize(config):
    return DirectionPolicy(config)


def policy(context, state, action_chunk, images, config, *, runtime):
    return context.policy(state, action_chunk, images, runtime)


def reset_context(context):
    context.reset_context()


def release(context):
    if context is not None:
        context.close()
