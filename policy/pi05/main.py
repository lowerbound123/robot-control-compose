"""Local LeRobot Pi05; model dependencies live only in this policy's image."""
from pathlib import Path

from rc_pi05_policy.configuration import load_model_config, validate_manifest
from rc_pi05_policy.runtime import AsyncPi05Policy

POLICY_ROOT = Path(__file__).resolve().parent


def initialize(config, *, runtime):
    validate_config(config)
    model_config = load_model_config(config['params']['model_config_path'], project_root=POLICY_ROOT)
    return AsyncPi05Policy(model_config, project_root=POLICY_ROOT)


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
