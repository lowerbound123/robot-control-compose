from pathlib import Path

from rc_qwen35_history.configuration import validate_manifest
from rc_qwen35_history.settings import configure_policy, export_settings, initialize_policy
from rc_qwen35_history.runtime import HistoryPolicy

POLICY_ROOT = Path(__file__).resolve().parent


def initialize(config, *, runtime):
    return initialize_policy(config, POLICY_ROOT, HistoryPolicy)


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
    return context.switch_model(path)


def validate_config(config):
    validate_manifest(config)


def configure(context, config, key, value, callback):
    return configure_policy(context, config, key, value, callback, POLICY_ROOT)
