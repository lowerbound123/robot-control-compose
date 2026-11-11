"""Configuration validation shared by supervisor and worker."""

import copy
import json
import threading

from .wire import encode_message
from .catalog import validate_connector_switch_settings

IMMUTABLE_FIELDS = {"schema_version", "id", "hot_reloadable", "tui_commands", "kind", "timeouts", "help", "help_details", "config_hints"}


class ConfigureCallback:
    """A call-scoped recorder; lifecycle work runs only after configure returns."""

    def __init__(self):
        self.action = None
        self._thread = threading.get_ident()
        self._active = True

    def _request(self, action):
        if not self._active or threading.get_ident() != self._thread:
            raise RuntimeError("configure callback is only valid in its active calling thread")
        if self.action is not None and self.action != action:
            raise ValueError("conflicting configure lifecycle requests")
        self.action = action

    def __call__(self, action):
        if action not in ('reset', 'reinitialize'):
            raise ValueError('configure callback action must be reset or reinitialize')
        self._request(action)

    def reset(self):
        self._request("reset")

    def reinitialize(self):
        self._request("reinitialize")

    def expire(self):
        self._active = False


def validate_candidate(config, candidate, action):
    if type(candidate) is not dict or action not in (None, "reset", "reinitialize"):
        raise ValueError("invalid configure candidate or action")
    validate_connector_switch_settings(candidate)
    if not same_config(candidate, json.loads(encode_message(candidate)[4:])):
        raise ValueError('configure candidate must contain plain JSON values and string keys')
    for key in set(config) | set(candidate):
        if key not in ("params", "execution") and (key not in config or key not in candidate or
                encode_message({key: config[key]}) != encode_message({key: candidate[key]})):
            raise ValueError("configure cannot change metadata: " + str(key))
    for key in ("params", "execution"):
        if type(candidate.get(key)) is not dict:
            raise ValueError("configure requires a mapping: " + key)
    for key in ("control_mode", "tui_command_workers"):
        if config.get("execution", {}).get(key) != candidate["execution"].get(key) and action != "reinitialize":
            raise ValueError("execution." + key + " requires reinitialize")
    execution = candidate["execution"]
    if execution.get("control_mode", "qpos") not in ("qpos", "epos", "mixed"):
        raise ValueError("invalid execution.control_mode")
    if "mode" in execution and execution["mode"] not in ("sync", "async"):
        raise ValueError("invalid execution.mode")
    if "call_every_actions" in execution and (type(execution["call_every_actions"]) is not int or execution["call_every_actions"] <= 0):
        raise ValueError("call_every_actions must be a positive integer")
    from .commands import command_specs
    command_specs(candidate)


def apply_candidate(config, candidate):
    """Keep existing dict/list references held by policy contexts valid."""
    for key in list(config):
        if key not in candidate:
            del config[key]
    for key, value in candidate.items():
        old = config.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            apply_candidate(old, value)
        elif isinstance(old, list) and isinstance(value, list):
            old[:] = copy.deepcopy(value)
        else:
            config[key] = copy.deepcopy(value)


def same_config(left, right):
    """Python equality alone conflates JSON booleans, integers and floats."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_config(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(same_config(a, b) for a, b in zip(left, right))
    return left == right


def prepare_patch(config, patch):
    validate_connector_switch_settings(config)
    if not isinstance(patch, dict) or not patch:
        raise ValueError("config patch must be a nonempty mapping")
    encode_message(patch)
    allowed = config.get("hot_reloadable", [])
    assignments = []
    for path, value in patch.items():
        if (not isinstance(path, str) or path not in allowed or path.split(".")[0] in IMMUTABLE_FIELDS
                or path in {"execution.control_mode", "execution.tui_command_workers"}):
            raise ValueError(f"config path is not hot reloadable: {path}")
        parts = path.split(".")
        current = config
        for part in parts[:-1]:
            if not isinstance(current, dict) or part not in current:
                raise ValueError(f"config path does not exist: {path}")
            current = current[part]
        if not isinstance(current, dict) or parts[-1] not in current or isinstance(current[parts[-1]], dict):
            raise ValueError(f"config path is not an existing leaf: {path}")
        if isinstance(value, dict):
            raise ValueError(f"hot config value must be a leaf: {path}")
        assignments.append((current, parts[-1], value))
    return assignments


def apply_patch(config, patch):
    assignments = prepare_patch(config, patch)
    for parent, name, value in assignments:
        parent[name] = value
