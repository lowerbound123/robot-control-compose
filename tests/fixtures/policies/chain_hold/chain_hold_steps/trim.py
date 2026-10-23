"""Keep at most max_steps targets from the previous policy's output."""


def validate_config(config):
    limit = config["params"]["max_steps"]
    if type(limit) is not int or limit <= 0:
        raise ValueError("max_steps must be a positive integer")


def initialize(config):
    return config


def policy(context, state, action_chunk, images, config):
    limit = context["params"]["max_steps"]
    if config["execution"]["control_mode"] == "mixed":
        return {"actions": action_chunk["actions"][:limit], "modes": action_chunk["modes"][:limit]}
    return action_chunk[:limit]


def reset_context(context):
    pass  # This stage has no temporal state.


def release(context):
    pass  # This stage owns no external resources.
