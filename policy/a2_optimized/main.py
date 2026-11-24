from pathlib import Path

from rc_qwen35_history.configuration import load_model_config, validate_manifest
from rc_qwen35_history.settings import configure_policy, export_settings, initialize_policy
from .runtime import OptimizedPolicy

POLICY_ROOT = Path(__file__).resolve().parent
HOT_MODEL_KEYS = ('model_config.execution.pipelined', 'model_config.transition.trunk_mode')


def _load_model_config(path, **kwargs):
    return load_model_config(path, trunk_modes=('close', 'tail'), **kwargs)


def initialize(config, *, runtime):
    return initialize_policy(config, POLICY_ROOT, OptimizedPolicy, load_config=_load_model_config)


def export_config(context, config):
    return export_settings(context, config)


def policy(context, state, action_chunk, images, config, *, runtime):
    return context.step(state, action_chunk, images, runtime=runtime)


def observe(context, state, images, config, *, runtime):
    context.observe(state, images, runtime=runtime)


def reset_context(context):
    context.reset()


def resume_context(context):
    context.resume()


def release(context):
    if context is not None:
        context.close()


def monitor(context):
    return context.monitor()


def switch_task(context, task):
    return context.switch_task(task)


def switch_model(context, path):
    return context.switch_model(path, load_config=_load_model_config)


def validate_config(config):
    validate_manifest(config)


def configure(context, config, key, value, callback):
    return configure_policy(context, config, key, value, callback, POLICY_ROOT,
                            load_config=_load_model_config, hot_model_keys=HOT_MODEL_KEYS)
