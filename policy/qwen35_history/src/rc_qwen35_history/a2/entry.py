"""Policy callbacks shared by the two A2 source entries."""
from functools import partial

from ..configuration import load_model_config, validate_manifest
from ..settings import configure_policy, export_settings, initialize_policy

HOT_MODEL_KEYS = ('model_config.execution.pipelined', 'model_config.transition.trunk_mode')


def load_a2_config(path, **kwargs):
    return load_model_config(path, trunk_modes=('close', 'tail'), **kwargs)


def initialize_a2(config, root, policy_class, robot_type):
    return initialize_policy(config, root, policy_class, robot_type=robot_type, load_config=load_a2_config)


def configure_a2(context, config, key, value, callback, root, robot_type):
    return configure_policy(context, config, key, value, callback, root,
                            load_config=partial(load_a2_config, robot_type=robot_type), hot_model_keys=HOT_MODEL_KEYS)


def export_config(context, config):
    return export_settings(context, config)


def observe(context, state, images, config, *, runtime):
    context.observe(state, images, runtime=runtime)


def resume_context(context):
    context.resume()


def monitor(context):
    return context.monitor()


def switch_task(context, task):
    return context.switch_task(task)


def switch_model(context, path):
    return context.switch_model(path, load_config=load_a2_config)


def validate_config(config):
    validate_manifest(config)
