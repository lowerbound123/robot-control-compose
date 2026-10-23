"""Generic model-server policy. See policy.json and docs/model-server-policy.md."""
from bridge import Bridge
import math

_initializing = None


def initialize(config, *, runtime):
    global _initializing
    validate_config(config)
    context=Bridge.__new__(Bridge)
    _initializing=context
    Bridge.__init__(context,config,runtime)
    _initializing=None
    return context


def policy(context,state,action_chunk,images,config,*,runtime):
    return context.step(state,action_chunk,images.descriptors,config,runtime)


def observe(context,state,images,config,*,runtime):
    context.observe(state,images.descriptors,config,runtime)


def wants_observations(context):
    return context.session.observe_enabled


def validate_config(config):
    allowed_hot={'params.request_when_remaining','params.fusion.trunk_mode','params.fusion.trunk_smoothing',
                 'params.fusion.max_smoothing_steps','params.fusion.exponential_factor','params.fusion.exponential_smoothing',
                 'params.subtask.refresh_every_actions','params.subtask.minimum_lead_actions'}
    if set(config['hot_reloadable'])-allowed_hot:
        raise ValueError('bridge lifecycle/model configuration must remain cold')
    execution=config['execution']
    if execution != {'mode':'sync','call_every_actions':1,'control_mode':'qpos'}:
        raise ValueError('bridge requires sync policy callbacks, call_every_actions=1, qpos')
    params=config['params']
    if params['mode'] not in ('sync','async-add'):
        raise ValueError('model mode must be sync or async-add')
    if params['image_transport'] not in ('shm','http'):
        raise ValueError('image_transport must be shm or http')
    for name,value in [('request_when_remaining',params['request_when_remaining']),
                       ('refresh_every_actions',params['subtask']['refresh_every_actions']),
                       ('minimum_lead_actions',params['subtask']['minimum_lead_actions'])]:
        if type(value) is not int or value<=0:
            raise ValueError(f'{name} must be a positive integer')
    for name,value in params['timeouts'].items():
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
            raise ValueError(f'timeout {name} must be finite and positive')
    subtask=params['subtask']
    if type(subtask['enabled']) is not bool or type(subtask['prefetch']) is not bool:
        raise ValueError('subtask enabled/prefetch must be boolean')
    if subtask['workflow'] not in ('two_stage','two_stage_move'):
        raise ValueError('subtask workflow must be two_stage or two_stage_move')
    from rc_policy_sdk.transitions import TrajectoryAddTransition
    for name in ('exponential_factor','exponential_smoothing'):
        value=params['fusion'][name]
        if type(value) not in (int,float) or not math.isfinite(value):
            raise ValueError(f'{name} must be finite')
    TrajectoryAddTransition(**params['fusion'])


def reset_context(context):
    context.reset()


def release(context):
    context=context or _initializing
    if context is not None and hasattr(context,'_closed'):
        context.close()
