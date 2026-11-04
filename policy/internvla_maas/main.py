"""Remote InternVLA action chunks with Qwen single scheduling and processing."""
from pathlib import Path

from rc_internvla_maas.configuration import load_model_config, validate_manifest
from rc_internvla_maas.runtime import AsyncMaasPolicy

POLICY_ROOT = Path(__file__).resolve().parent


def initialize(config, *, runtime):
    validate_config(config)
    settings = load_model_config(config['params']['model_config_path'], project_root=POLICY_ROOT)
    return AsyncMaasPolicy(settings, project_root=POLICY_ROOT)


def policy(context, state, action_chunk, images, config, *, runtime):
    return context.step(state, action_chunk, images, runtime=runtime)


def switch_model(context, config_path):
    return context.switch_model(config_path)


def validate_config(config):
    validate_manifest(config)


def reset_context(context):
    context.reset()


def release(context):
    if context is not None:
        context.close()
