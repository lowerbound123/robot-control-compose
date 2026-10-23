import copy
import types

import pytest

from robot_control.config import validate_policy


def config():
    return {"schema_version": 2, "id": "commands", "policy_host": "test",
            "execution": {"mode": "sync", "call_every_actions": 1, "control_mode": "qpos"},
            "params": {}, "hot_reloadable": [],
            "tui_commands": {"test": {"handler": "handle_test", "args": ["str", "int", "float", "bool"]}}}


def test_command_codec_quotes_and_types():
    from rc_policy_sdk.commands import command_specs, parse_command
    specs = command_specs(config())
    assert specs["test"]["timeout_s"] == -1
    assert parse_command('test "hello world" -12 .25 TRUE', specs) == ("test", ["hello world", -12, .25, True])
    assert parse_command('test "" 0 1 false', specs)[1] == ["", 0, 1., False]
    for line in ('test x 1', 'test x 1 2 yes', 'test x 1 nan true', 'test x 1.5 2 true', 'test "'):
        with pytest.raises(ValueError):
            parse_command(line, specs)


@pytest.mark.parametrize("change", [
    {"tui_commands": {"pause": {"handler": "f", "args": []}}},
    {"tui_commands": {"x": {"handler": "missing.dot", "args": []}}},
    {"tui_commands": {"x": {"handler": "f", "args": ["json"]}}},
    {"tui_commands": {"x": {"handler": "f", "args": [], "timeout_s": 0}}},
    {"tui_commands": {"x": {"handler": "f", "args": [], "timeout_s": True}}},
    {"tui_commands": {"x": {"handler": "f", "args": [], "timeout_s": float("inf")}}},
    {"execution": {"mode": "sync", "call_every_actions": 1, "control_mode": "qpos", "tui_command_workers": 0}},
    {"hot_reloadable": ["tui_commands.test.args"]},
])
def test_invalid_declarations_rejected(change):
    value = config()
    value.update(change)
    with pytest.raises(ValueError):
        validate_policy(value)


def test_command_settings_cannot_be_hot_patched():
    from rc_policy_sdk.configuration import prepare_patch
    value = config()
    value["execution"]["tui_command_workers"] = 4
    for path, replacement in (("tui_commands.test.args", []), ("execution.tui_command_workers", 2)):
        value["hot_reloadable"] = [path]
        with pytest.raises(ValueError):
            prepare_patch(value, {path: replacement})
        with pytest.raises(ValueError):
            validate_policy(value)


def test_handler_validation_and_zero_arguments():
    from rc_policy_sdk.commands import command_specs, resolve_handlers, parse_command
    value = config()
    value["tui_commands"] = {"ping": {"handler": "ping", "args": []}}
    specs = command_specs(value)
    def ping(context): return "pong"
    assert resolve_handlers(types.SimpleNamespace(ping=ping), specs)["ping"] is ping
    assert parse_command("ping", specs) == ("ping", [])
    for module in (types.SimpleNamespace(), types.SimpleNamespace(ping=lambda: None)):
        with pytest.raises(ValueError):
            resolve_handlers(module, specs)
    async def async_ping(context): pass
    with pytest.raises(ValueError):
        resolve_handlers(types.SimpleNamespace(ping=async_ping), specs)
    original = copy.deepcopy(value)
    command_specs(value)
    assert original == value
