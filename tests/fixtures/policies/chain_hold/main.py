"""Generate measured hold targets, then trim the chunk in a separate policy."""

from rc_policy_sdk import PolicyChain

from .chain_hold_steps import hold, trim


chain = PolicyChain([hold, trim])


def initialize(config, *, runtime=None):
    return chain.initialize(config, runtime=runtime)


def policy(context, state, action_chunk, images, config, *, runtime=None):
    return chain.policy(context, state, action_chunk, images, config, runtime=runtime)


def validate_config(config):
    chain.validate_config(config)


def reset_context(context):
    chain.reset_context(context)


def release(context):
    chain.release(context)
