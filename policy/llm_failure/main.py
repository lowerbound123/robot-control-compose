"""Passthrough chain member; network checks run only in its isolated worker."""
from pathlib import Path

from rc_llm_failure.configuration import load_provider
from rc_llm_failure.runtime import FailurePolicy


POLICY_ROOT = Path(__file__).resolve().parent


def initialize(config):
    settings = load_provider(config['params'].get('provider_config_path'), POLICY_ROOT)
    return FailurePolicy(config, settings)


def switch_provider(context, config_path):
    load_provider(config_path, POLICY_ROOT)
    return {'params.provider_config_path': config_path}


def policy(context, state, action_chunk, images, config, *, runtime):
    return context.policy(action_chunk, images, runtime)


def reset_context(context):
    context.reset_context()


def release(context):
    if context is not None:
        context.close()
