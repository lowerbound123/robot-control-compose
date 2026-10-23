"""Local Qwen3.5 single workflow; model weights live only in this worker."""
from pathlib import Path

from rc_qwen35_policy.configuration import load_model_config, validate_manifest
from rc_qwen35_policy.runtime import AsyncQwenPolicy

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def initialize(config, *, runtime):
    validate_config(config)
    model_config = load_model_config(config['params']['model_config_path'],
                                     workflow='single', project_root=PROJECT_ROOT)
    return AsyncQwenPolicy(model_config, project_root=PROJECT_ROOT)


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
