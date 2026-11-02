"""LLM decisions -> ordinary epos action chunks; network runs only in this worker."""
from pathlib import Path

from rc_llm_direction.configuration import load_provider
from rc_llm_direction.runtime import DirectionPolicy


POLICY_ROOT = Path(__file__).resolve().parent


def initialize(config):
    settings = load_provider(config['params'].get('provider_config_path'), POLICY_ROOT)
    return DirectionPolicy(config, settings)


def switch_provider(context, config_path):
    load_provider(config_path, POLICY_ROOT)
    return {'params.provider_config_path': config_path}


def policy(context, state, action_chunk, images, config, *, runtime):
    return context.policy(state, action_chunk, images, runtime)


def reset_context(context):
    context.reset_context()


def release(context):
    if context is not None:
        context.close()
