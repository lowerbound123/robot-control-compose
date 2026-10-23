"""One policy session, running on a thread in the shared host runtime."""

import argparse
import copy
import importlib.util
from pathlib import Path
import signal
import socket
import sys
import traceback
import types

from .commands import command_specs, resolve_handlers
from .command_runtime import CommandExecutor
from .control import RUNTIME_TOOLS, runtime_tools

from .actions import CONTROL_MODES, normalize_actions

from .configuration import apply_patch
from .images import map_images
from .lifecycle import become_subreaper, cleanup_worker_children
from .runtime import PolicyRuntime, WorkerChannel, invoke


class Shutdown(BaseException):
    pass


_shutdown_requested = False


def _shutdown(_signum, _frame):
    global _shutdown_requested
    if not _shutdown_requested:
        _shutdown_requested = True
        raise Shutdown()


def _identity(message):
    return {key: message[key] for key in ("request_id", "generation", "config_version") if key in message}


def _load_policy(directory, namespace):
    path = Path(directory).resolve() / "main.py"
    package = types.ModuleType(namespace)
    package.__path__ = [str(path.parent)]
    package.__package__ = namespace
    sys.modules[namespace] = package
    spec = importlib.util.spec_from_file_location(namespace + ".main", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load policy: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for name in ("initialize", "policy", "reset_context", "release"):
        if not callable(getattr(module, name, None)):
            raise ValueError(f"main.py must define {name}()")
    return module


def run_session(connection, policy_dir, namespace):
    channel = None
    module = None
    context = None
    commands = None
    config = None
    message = {}
    try:
        channel = WorkerChannel(connection)
        message = channel.commands.get()
        if message.get("type") != "initialize" or not isinstance(message.get("config"), dict):
            raise ValueError("first message must initialize with a config object")
        config = message["config"]
        channel.runtime_tools = runtime_tools(message.get("runtime_tools", []))
        version = message["config_version"]
        module = _load_policy(policy_dir, namespace)
        specs = command_specs(config)
        if (any(spec.get("effect") == "reload_policy" for spec in specs.values())
                and "reload_policy" not in message.get("tui_command_effects", [])):
            raise ValueError("host does not support reload_policy; upgrade robot-control")
        handlers = resolve_handlers(module, specs)
        context = invoke(module.initialize, (config,), PolicyRuntime(channel, message))
        commands = CommandExecutor(channel, context, specs, handlers, config.get("execution", {}).get("tui_command_workers", 4))
        channel.on_command = lambda request: commands.submit(request, version)
        channel.on_command_gate = commands.pause
        channel.on_command_resume = commands.resume
        channel.send({"type": "ready", "config_version": version, "action_modes": list(CONTROL_MODES), "tui_commands": True,
                      "tui_command_effects": ["reload_policy"], "runtime_tools": list(RUNTIME_TOOLS)})
        def observe(message):
            try:
                if message.get('config_version') != version:
                    raise ValueError('observation config version differs from worker')
                with map_images(message['images']) as images:
                    if callable(getattr(module, 'observe', None)):
                        invoke(module.observe, (context, message['state'], images, config), PolicyRuntime(channel, message))
                channel.send({'type':'observed', **_identity(message), 'images_released':True})
            except BufferError as exc:
                channel.send({'type':'fatal', **_identity(message), 'images_released':False, 'error':str(exc)})
            except Exception as exc:
                channel.send({'type':'observation_error', **_identity(message), 'images_released':True, 'error':str(exc)})
        channel.on_observe = observe
        supported=callable(getattr(module,'observe',None))
        if callable(getattr(module,'wants_observations',None)):
            supported=supported and module.wants_observations(context)
        channel.send({'type':'capabilities','observe':supported})
        while True:
            message = channel.commands.get()
            kind = message.get("type")
            if kind == "shutdown":
                break
            try:
                if kind == "infer":
                    if message.get("config_version") != version:
                        raise ValueError("inference config version differs from worker")
                    runtime = PolicyRuntime(channel, message)
                    with map_images(message["images"]) as images:
                        with runtime._inference_scope():
                            output = invoke(module.policy, (context, message["state"], message["action_chunk"], images, config), runtime)
                        actions = normalize_actions(
                            output,
                            config.get("execution", {}).get("control_mode", "qpos"),
                        )
                        result = {"type": "result", **_identity(message), **actions.payload()}
                        if runtime._control_requests:
                            result["control_requests"] = runtime._control_requests
                        del actions, output
                    # Result is the lease-release acknowledgment: all image
                    # mappings must be closed before the supervisor receives it.
                    channel.send({**result, "images_released": True})
                elif kind == "update_config":
                    if type(message.get("config_version")) is not int or message["config_version"] <= version:
                        raise ValueError("config version must increase")
                    commands.barrier()
                    channel.barrier()
                    if callable(getattr(module,'validate_config',None)):
                        proposed=copy.deepcopy(config)
                        apply_patch(proposed,message['patch'])
                        module.validate_config(proposed)
                    apply_patch(config, message["patch"])
                    version = message["config_version"]
                    commands.resume()
                    channel.send({"type": "ack", "operation_id": message["operation_id"], "config_version": version})
                elif kind == "reset_context":
                    commands.barrier()
                    channel.barrier()
                    module.reset_context(context)
                    commands.resume()
                    channel.send({"type": "ack", "operation_id": message["operation_id"]})
                else:
                    raise ValueError(f"unknown worker message type: {kind}")
            except BufferError as exc:
                channel.send({"type": "fatal", **_identity(message), "images_released": False, "error": str(exc)})
                break
            except Exception as exc:
                channel.send({"type": "error", "images_released": True, **_identity(message), "operation_id": message.get("operation_id"), "error": f"{type(exc).__name__}: {exc}"})
    except Shutdown:
        pass
    except (EOFError, BrokenPipeError, ConnectionResetError):
        pass
    except Exception as exc:
        try:
            channel.send({"type": "error", **_identity(message), "error": f"{type(exc).__name__}: {exc}"})
        except OSError:
            pass
        traceback.print_exc()
    finally:
        release_error = None
        # Supervisor escalates if release hangs. Signals cannot call release twice.
        try:
            if commands is not None:
                commands.close()
            if channel is not None:
                channel.observations.shutdown(wait=True)
            if module is not None:
                module.release(context)
        except BaseException as exc:
            release_error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        try:
            channel.send({"type": "closed", "release_error":release_error})
        except (OSError, AttributeError):
            pass
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()
        for name in list(sys.modules):
            if name == namespace or name.startswith(namespace + "."):
                sys.modules.pop(name, None)
