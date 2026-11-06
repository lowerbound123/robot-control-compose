"""One policy session, running on a thread in the shared host runtime."""

import copy
import importlib.util
from pathlib import Path
import socket
import sys
import traceback
import types

from .commands import COMMAND_EFFECTS, check_command_effects, command_specs, resolve_handlers
from .command_runtime import CommandExecutor
from .monitor import MonitorExecutor, monitor_callback
from .control import RUNTIME_TOOLS, runtime_tools

from .actions import CONTROL_MODES, normalize_actions

from .configuration import ConfigureCallback, apply_candidate, apply_patch, same_config, validate_candidate
from .images import map_images
from .runtime import PolicyRuntime, WorkerChannel, invoke


class CandidateRejected(ValueError):
    pass


def _identity(message):
    return {key: message[key] for key in ("request_id", "generation", "config_version", "context_id") if key in message}


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
    monitor = None
    config = None
    message = {}
    release_due = False
    active = False
    prepared_config = None
    configure_applied = False
    configure_failed = False
    configure_committed = False
    try:
        channel = WorkerChannel(connection)
        message = channel.commands.get()
        if message.get("type") != "initialize" or not isinstance(message.get("config"), dict):
            raise ValueError("first message must initialize with a config object")
        peer_capabilities = message
        config = message["config"]
        channel.context_id = message.get("context_id")
        if message.get('global_state') is not None:
            if channel.context_id is not None and message['global_state'].get('binding', {}).get('context_id') != channel.context_id:
                raise ValueError('global state context binding mismatch')
            channel.attach_globals(message['global_state'])
        channel.runtime_tools = runtime_tools(message.get("runtime_tools", []))
        version = message["config_version"]
        module = _load_policy(policy_dir, namespace)
        release_due = True
        specs = command_specs(config)
        check_command_effects(specs, message)
        supports_configure = callable(getattr(module, "configure", None))
        configure_enabled = supports_configure and message.get("configure") is True
        reinitialize_enabled = (configure_enabled or message.get("backend_switch_v1") is True or
                                any(spec.get("effect") == "reinitialize_policy" for spec in specs.values()))
        channel.reinitialize_enabled = reinitialize_enabled
        if reinitialize_enabled and (type(channel.context_id) is not str or not channel.context_id):
            raise ValueError("reinitialize_policy requires a nonempty initial context_id")
        handlers = resolve_handlers(module, specs)
        context = invoke(module.initialize, (config,), PolicyRuntime(channel, message))
        active = True
        commands = CommandExecutor(channel, context, specs, handlers, config.get("execution", {}).get("tui_command_workers", 4))
        monitor = MonitorExecutor(channel, context, monitor_callback(module))
        channel.on_monitor = lambda request: monitor.submit(request, version)
        channel.on_monitor_gate = monitor.pause
        channel.on_command = lambda request: commands.submit(request, version)
        channel.on_command_gate = commands.pause
        def resume():
            nonlocal configure_committed
            if active and (not channel.configure_fenced or configure_committed):
                channel.configure_fenced = False
                configure_committed = False
                commands.resume()
                monitor.resume()
                channel.observations_gated = False
        channel.on_command_resume = resume
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
        channel.send({"type": "ready", "config_version": version, "action_modes": list(CONTROL_MODES), "tui_commands": True, "monitor": monitor.callback is not None, "observe": supported,
                      "configure": configure_enabled, "context_id": channel.context_id,
                      "backend_switch_v1": message.get("backend_switch_v1") is True,
                      "tui_command_effects_v2": list(COMMAND_EFFECTS), "runtime_tools": list(RUNTIME_TOOLS),
                      "global_state_version": 1 if channel.global_memory is not None else None})
        channel.send({'type':'capabilities','observe':supported})
        while True:
            message = channel.commands.get()
            kind = message.get("type")
            if kind == "shutdown":
                break
            try:
                if channel.configure_fenced and kind in ('update_config', 'reset_context'):
                    raise RuntimeError('configure transaction blocks legacy lifecycle operations')
                if configure_failed and kind.startswith('reinit_'):
                    raise RuntimeError('configure failed after apply; cold retry required')
                if kind.startswith("configure_"):
                    if configure_failed:
                        raise RuntimeError("configure failed after apply; cold retry required")
                    expected_version = message.get("new_config_version") if kind == "configure_commit" else message.get("config_version")
                    if not active or message.get("context_id") != channel.context_id or type(expected_version) is not int or expected_version != version:
                        raise ValueError("stale configure context")
                    if kind == "configure_prepare":
                        monitor.barrier()
                        commands.barrier()
                        channel.barrier()
                        if prepared_config is not None:
                            raise ValueError("configure transaction already prepared")
                        try:
                            if not configure_enabled:
                                raise ValueError("policy does not support configure")
                            if type(message.get("key")) is not str or not message["key"] or type(message.get("value")) is not str:
                                raise ValueError("configure requires key and raw text value")
                            candidate = copy.deepcopy(config)
                            callback = ConfigureCallback()
                            try:
                                returned = module.configure(context, candidate, message["key"], message["value"], callback)
                            finally:
                                callback.expire()
                            if returned is not None:
                                raise ValueError("configure must return None")
                            validate_candidate(config, candidate, callback.action)
                            if config.get('schema_version') == 3:
                                from .catalog import validate_runtime_policy
                                validate_runtime_policy(candidate, Path(policy_dir).name, config['id'])
                            if callable(getattr(module, "validate_config", None)):
                                module.validate_config(copy.deepcopy(candidate))
                            # User code may retain its candidate; freeze a detached snapshot.
                            prepared_config = (copy.deepcopy(candidate), callback.action)
                        except Exception as exc:
                            raise CandidateRejected(str(exc)) from exc
                        channel.send({"type": "ack", "operation_id": message["operation_id"], **_identity(message),
                                      "config": prepared_config[0], "action": prepared_config[1]})
                    elif kind == "configure_apply":
                        if (prepared_config is None or configure_applied or not same_config(message.get("config"), prepared_config[0])
                                or message.get("action") != prepared_config[1] or prepared_config[1] == "reinitialize"
                                or type(message.get("new_config_version")) is not int or message["new_config_version"] <= version):
                            raise ValueError("invalid prepared configure application")
                        # From here even a throwing reset has changed active state.
                        configure_failed = True
                        apply_candidate(config, prepared_config[0])
                        if prepared_config[1] == "reset":
                            module.reset_context(context)
                        version = message["new_config_version"]
                        configure_applied = True
                        configure_failed = False
                        channel.send({"type": "ack", "operation_id": message["operation_id"], "config_version": version, "context_id": channel.context_id})
                    elif kind == "configure_commit":
                        if not configure_applied:
                            raise ValueError("configure has not been applied")
                        prepared_config = None
                        configure_applied = False
                        configure_committed = True
                        channel.send({"type": "ack", "operation_id": message["operation_id"], "config_version": version, "context_id": channel.context_id})
                    elif kind == "configure_abort":
                        if configure_applied:
                            raise ValueError("applied configure cannot be aborted")
                        prepared_config = None
                        channel.configure_fenced = False
                        channel.send({"type": "ack", "operation_id": message["operation_id"], **_identity(message)})
                    else:
                        raise ValueError("unknown configure operation")
                elif kind.startswith("reinit_"):
                    if not reinitialize_enabled:
                        raise ValueError("policy did not opt in to reinitialization")
                    if channel.context_id is None and kind != "reinit_initialize":
                        raise ValueError("reinitialization requires context identity")
                    if kind != "reinit_initialize" and (not active or message.get("context_id") != channel.context_id or message.get("config_version") != version):
                        raise ValueError("stale policy context")
                    if kind == "reinit_validate":
                        if type(message.get("new_config_version")) is not int or message["new_config_version"] <= version or not isinstance(message.get("config"), dict):
                            raise ValueError("invalid candidate configuration")
                        if callable(getattr(module, "validate_config", None)):
                            try:
                                module.validate_config(copy.deepcopy(message["config"]))
                            except Exception as exc:
                                raise CandidateRejected(str(exc)) from exc
                    elif kind == "reinit_release":
                        monitor.barrier()
                        commands.barrier()
                        channel.barrier()
                        # Mark before invoking user code: even a throwing release must not run twice.
                        release_due = False
                        active = False
                        try:
                            module.release(context)
                        finally:
                            channel.detach_globals()
                            channel.context_id = None
                            context = None
                            commands.context = None
                            monitor.context = None
                    elif kind == "reinit_initialize":
                        if active or release_due or type(message.get("context_id")) is not str or not message["context_id"] or type(message.get("config_version")) is not int or message["config_version"] <= version or not isinstance(message.get("config"), dict):
                            raise ValueError("invalid new policy context")
                        config = message["config"]
                        version = message["config_version"]
                        channel.context_id = message["context_id"]
                        if message.get("global_state") is not None:
                            if message["global_state"].get("binding", {}).get("context_id") != channel.context_id:
                                raise ValueError("global state context binding mismatch")
                            channel.attach_globals(message["global_state"])
                        specs = command_specs(config)
                        check_command_effects(specs, peer_capabilities)
                        handlers = resolve_handlers(module, specs)
                        context = None
                        release_due = True
                        context = invoke(module.initialize, (config,), PolicyRuntime(channel, message))
                        commands.close()
                        monitor.close()
                        commands = CommandExecutor(channel, context, specs, handlers, config.get("execution", {}).get("tui_command_workers", 4))
                        monitor = MonitorExecutor(channel, context, monitor_callback(module))
                        commands.pause()
                        monitor.pause()
                        channel.on_command_gate = commands.pause
                        channel.on_monitor_gate = monitor.pause
                        active = True
                        prepared_config = None
                        configure_applied = False
                        channel.configure_fenced = False
                        supported = callable(getattr(module, 'observe', None))
                        if callable(getattr(module, 'wants_observations', None)):
                            supported = supported and module.wants_observations(context)
                    else:
                        raise ValueError("unknown reinitialization operation")
                    ack = {"type": "ack", "operation_id": message["operation_id"], "config_version": message["config_version"], "context_id": message["context_id"]}
                    if kind == "reinit_initialize":
                        ack.update(observe=bool(supported), monitor=monitor.callback is not None)
                    channel.send(ack)
                elif kind == "infer":
                    if not active or channel.configure_fenced:
                        raise ValueError("policy context unavailable")
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
                    monitor.barrier()
                    commands.barrier()
                    channel.barrier()
                    if callable(getattr(module,'validate_config',None)):
                        proposed=copy.deepcopy(config)
                        apply_patch(proposed,message['patch'])
                        module.validate_config(proposed)
                    apply_patch(config, message["patch"])
                    version = message["config_version"]
                    commands.resume()
                    monitor.resume()
                    channel.send({"type": "ack", "operation_id": message["operation_id"], "config_version": version})
                elif kind == "reset_context":
                    monitor.barrier()
                    commands.barrier()
                    channel.barrier()
                    module.reset_context(context)
                    commands.resume()
                    monitor.resume()
                    channel.send({"type": "ack", "operation_id": message["operation_id"]})
                else:
                    raise ValueError(f"unknown worker message type: {kind}")
            except BufferError as exc:
                channel.send({"type": "fatal", **_identity(message), "images_released": False, "error": str(exc)})
                break
            except Exception as exc:
                if kind in ("update_config", "reset_context") and not channel.configure_fenced:
                    monitor.resume()
                channel.send({"type": "error", "images_released": True, **_identity(message), "operation_id": message.get("operation_id"), "error": f"{type(exc).__name__}: {exc}", **({"error_type": "candidate_rejected"} if isinstance(exc, CandidateRejected) else {})})
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
            if monitor is not None:
                monitor.close()
            if commands is not None:
                commands.close()
            if channel is not None:
                channel.observations.shutdown(wait=True)
            if module is not None and release_due:
                module.release(context)
        except BaseException as exc:
            release_error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        if channel is not None and channel.global_memory is not None:
            channel.global_memory.close()
        try:
            channel.send({"type": "closed", "release_error":release_error})
        except (OSError, AttributeError):
            pass
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()
        if channel is not None:
            channel.sender.close()
        for name in list(sys.modules):
            if name == namespace or name.startswith(namespace + "."):
                sys.modules.pop(name, None)
