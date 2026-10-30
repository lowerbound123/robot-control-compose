"""Configuration and typed arguments for local policy commands (no UI imports)."""
import inspect
import math
import re
import shlex

BUILTIN_COMMANDS = (
    "set", "unset", "show", "save-config", "reset", "r", "save", "load", "pause",
    "resume", "policies", "switch", "status", "help", "exit", "quit", "take", "return", "mcp",
    "camera", "monitor", "manage-policy", "snapshot", "policy", "record",
)
_TYPES = {"str": "", "int": 0, "float": 0., "bool": False}


def command_specs(config):
    workers = config.get("execution", {}).get("tui_command_workers", 4)
    if type(workers) is not int or workers <= 0:
        raise ValueError("execution.tui_command_workers must be a positive integer")
    raw = config.get("tui_commands", {})
    if not isinstance(raw, dict):
        raise ValueError("tui_commands must be an object")
    result = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) or name in BUILTIN_COMMANDS or name.startswith("monitor-"):
            raise ValueError(f"invalid or reserved TUI command name: {name}")
        if not isinstance(entry, dict) or set(entry) - {"handler", "args", "help", "timeout_s", "effect"}:
            raise ValueError(f"invalid command declaration: {name}")
        handler, args = entry.get("handler"), entry.get("args")
        if not isinstance(handler, str) or not handler.isidentifier():
            raise ValueError(f"{name}: handler must name a callable in main.py")
        if not isinstance(args, list) or any(not isinstance(arg, str) or arg not in _TYPES for arg in args):
            raise ValueError(f"{name}: args must list str/int/float/bool")
        timeout = entry.get("timeout_s", -1)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or (timeout != -1 and timeout <= 0):
            raise ValueError(f"{name}: timeout_s must be -1 or a positive finite number")
        help_text = entry.get("help", "")
        if not isinstance(help_text, str):
            raise ValueError(f"{name}: help must be a string")
        if "effect" in entry and entry["effect"] != "reload_policy":
            raise ValueError(f"{name}: effect must be reload_policy when specified")
        result[name] = {"handler": handler, "args": list(args), "help": help_text, "timeout_s": timeout}
        if "effect" in entry:
            result[name]["effect"] = entry["effect"]
    return result


def command_usage(name, spec):
    return " ".join([name, *(f"<{arg}>" for arg in spec["args"])])


def validate_arguments(args, spec):
    if not isinstance(args, list) or len(args) != len(spec["args"]):
        raise ValueError("command argument count differs from declaration")
    for value, kind in zip(args, spec["args"]):
        valid = (type(value) is str if kind == "str" else
                 type(value) is int if kind == "int" else
                 type(value) is bool if kind == "bool" else
                 type(value) in (int, float) and math.isfinite(value))
        if not valid:
            raise ValueError(f"invalid {kind} argument")
    return args


def parse_command(line, specs):
    words = shlex.split(line)
    if not words or words[0] not in specs:
        raise ValueError("unknown policy command")
    name, raw = words[0], words[1:]
    spec = specs[name]
    try:
        if len(raw) != len(spec["args"]):
            raise ValueError("wrong argument count")
        args = []
        for value, kind in zip(raw, spec["args"]):
            if kind == "str":
                converted = value
            elif kind == "int":
                if not re.fullmatch(r"[+-]?[0-9]+", value):
                    raise ValueError("expected decimal integer")
                converted = int(value, 10)
            elif kind == "float":
                converted = float(value)
            else:
                if value.lower() not in ("true", "false"):
                    raise ValueError("expected true/false")
                converted = value.lower() == "true"
            args.append(converted)
        return name, validate_arguments(args, spec)
    except ValueError as exc:
        raise ValueError(f"{exc}; usage: {command_usage(name, spec)}") from exc


def resolve_handlers(module, specs):
    result = {}
    for name, spec in specs.items():
        function = getattr(module, spec["handler"], None)
        if (not callable(function) or inspect.iscoroutinefunction(function)
                or inspect.isasyncgenfunction(function) or inspect.isgeneratorfunction(function)):
            raise ValueError(f"{name}: missing synchronous handler {spec['handler']}")
        try:
            inspect.signature(function).bind(None, *(_TYPES[kind] for kind in spec["args"]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}: handler must accept context and declared arguments: {exc}") from exc
        result[name] = function
    return result
