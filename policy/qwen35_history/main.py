"""Independent Qwen visual-history instance; no robot I/O in this process."""
from pathlib import Path

from rc_qwen35_history.configuration import load_model_config, validate_manifest
from rc_qwen35_history.runtime import HistoryPolicy

POLICY_ROOT = Path(__file__).resolve().parent


def initialize(config, *, runtime):
    validate_config(config)
    model = load_model_config(config['params']['model_config_path'], project_root=POLICY_ROOT)
    return HistoryPolicy(model, policy_config=config, project_root=POLICY_ROOT)


def policy(context, state, action_chunk, images, config, *, runtime):
    return context.step(state, action_chunk, images, runtime=runtime)


def reset_context(context):
    context.reset()


def release(context):
    if context is not None:
        context.close()


def monitor(context):
    return context.monitor()


def switch_task(context, task):
    return context.switch_task(task)


def switch_model(context, path):
    return context.switch_model(path)


def validate_config(config):
    validate_manifest(config)
