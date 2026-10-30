"""Remote policy session; liveness never substitutes for reader exit proof."""
from collections import OrderedDict, deque
import math
from pathlib import Path
import threading
import time
import uuid

from rc_policy_sdk.commands import command_specs, validate_arguments
from rc_policy_sdk.configuration import apply_patch, prepare_patch
from rc_policy_sdk.control import RUNTIME_TOOLS, control_requests, runtime_tools
from rc_policy_sdk.wire import encode_message
from .host_client import HostClient


from .monitor_client import MonitorClient
from rc_policy_sdk.outbox import PriorityOutbox


def _trace_payload(value):
    """Snapshot mutable containers, leaving immutable sensor/action values alone."""
    if isinstance(value, dict):
        return {key: _trace_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_trace_payload(item) for item in value]
    return value


def _inference_trace_context(request):
    runtime = request.get('runtime', {})
    runtime = runtime if isinstance(runtime, dict) else {}
    context = {key: request.get(key) for key in ('request_id', 'generation', 'config_version')}
    context['observation_id'] = runtime.get('observation_id', str(request.get('request_id')))
    context.update({key: runtime[key] for key in ('chain_id', 'chain_request_id', 'cell') if key in runtime})
    context['started_at'] = time.monotonic()
    return context


def _emit_inference_trace(callback, instance_id, source_id, kind, context, data, **extras):
    """The caller supplies a nonblocking sink; instrumentation never owns I/O."""
    if callback is None:
        return
    try:
        event = {'event': 'policy.' + kind, 'instance_id': instance_id, 'source_id': source_id,
                 **{key: value for key, value in context.items() if key != 'started_at'},
                 'timestamp_ns': time.time_ns(), 'monotonic_ns': time.monotonic_ns(),
                 'data': _trace_payload(data), **extras}
        if kind != 'start':
            event['elapsed_s'] = max(0., time.monotonic() - context['started_at'])
        callback(event)
    except Exception:
        pass


class PolicyProcess(MonitorClient):
    def __init__(self, policy_dir, config, config_version=1, *, host_socket=None, endpoint=None, exit_authority=None,
                 init_timeout_s=600, inference_timeout_s=60, release_timeout_s=30, on_log=None, global_state=None, on_trace=None):
        from rc_policy_sdk.catalog import instance_id
        instance_id(config.get('id'))
        self.endpoint = endpoint
        self.exit_authority = exit_authority
        self.expected_host = (dict(deployment_id=endpoint.deployment_id, run_id=endpoint.run_id,
                                   generation=endpoint.generation) if endpoint else None)
        for value in (init_timeout_s, inference_timeout_s, release_timeout_s):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError('policy timeout must be positive and finite')
        encode_message(config)
        self.policy_dir, self.config, self.config_version = Path(policy_dir), config, config_version
        self.init_timeout_s, self.inference_timeout_s, self.release_timeout_s = init_timeout_s, inference_timeout_s, release_timeout_s
        self.on_log = on_log
        self.on_trace = on_trace
        self._trace_requests = OrderedDict()
        self.global_state = global_state
        self.host_socket = host_socket or (endpoint.socket if endpoint else None)
        if not self.host_socket: raise ValueError('policy host socket is not configured')
        self.session_id = uuid.uuid4().hex
        self.runtime_id = None
        self.host = None
        self._pid = None
        self._condition = threading.Condition(threading.RLock())
        self._operation_lock = threading.Lock()
        self._launch_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._state = 'new'
        self._ready = False
        self._failure = None
        self._responses, self._command_responses, self._observation_responses = deque(), deque(), deque()
        self._acks, self._command_calls = {}, {}
        self._init_monitor()
        self.resources = None
        self.command_specs = command_specs(config)
        self.supports_commands = self.supports_observe = False
        self.runtime_tools = frozenset()
        self._commands_blocked = self._command_fatal = False
        self._inflight = self._observation = None
        self._pending_operation = False
        self._operation_number = 0
        self._forced = False
        self.release_error = None
        self.cleanup_confirmed = False
        self._submitted_load = False

    @property
    def pid(self): return self._pid

    @property
    def has_readers(self): return self._submitted_load and not self.cleanup_confirmed

    @property
    def runnable(self): return self._state == 'running' and not self._failure and not self.cleanup_confirmed

    @property
    def alive(self): return self.has_readers

    def _log(self, message):
        if self.on_log:
            try: self.on_log(message)
            except Exception: pass

    def _log_packet(self, packet):
        if self.on_trace:
            try:
                self.on_trace({**packet, 'event':'policy.stdout', 'instance_id':self.config['id'],
                    'source_id':self.policy_dir.name, 'config_version':self.config_version,
                    'timestamp_ns':time.time_ns(), 'monotonic_ns':time.monotonic_ns()})
            except Exception:
                pass
        self._log(packet['message'])

    def _trace_key(self, message, runtime_id=None):
        return (self.runtime_id if runtime_id is None else runtime_id,
                *(message.get(key) for key in ('request_id', 'generation', 'config_version')))

    def _trace(self, kind, message, *, runtime_id=None, **extras):
        if self.on_trace is not None:
            try:
                try:
                    context = self._trace_requests.get(self._trace_key(message, runtime_id))
                except TypeError:
                    context = None  # Malformed stale identity remains discardable.
                if context is None:
                    context = _inference_trace_context(message)
                _emit_inference_trace(self.on_trace, self.config['id'], self.policy_dir.name,
                                      kind, context, message, **extras)
            except Exception:
                pass

    def _trace_close(self):
        with self._condition:
            if self._inflight and not self._inflight.get('timed_out') and not self._inflight.get('trace_cancelled'):
                self._trace('cancel', {**{key: self._inflight[key] for key in ('request_id', 'generation', 'config_version')},
                                       'type': 'cancelled', 'reason': 'policy closing'})
                self._inflight['trace_cancelled'] = True

    def start(self, cancel_event=None):
        try:
            with self._launch_lock:
                if self._state != 'new': raise RuntimeError('policy session already started')
                if cancel_event and cancel_event.is_set(): raise RuntimeError('policy initialize cancelled')
                self._state = 'starting'
                self.host = HostClient.get(self.host_socket, self.config['id'], expected=self.expected_host)
                if self.endpoint and self.host.instance_id != self.endpoint.host_instance_id:
                    raise RuntimeError('host restarted after readiness; stale endpoint')
                self.host.register(self)
                self.host.preflight(self.config['id'], timeout=min(10, self.init_timeout_s))
                self._submitted_load = True
                global_descriptor = self.global_state.attach_session(self) if self.global_state is not None else None
                loaded = self.host.request('load', timeout=min(10, self.init_timeout_s),
                    session_id=self.session_id, policy_id=self.config['id'],
                    timeouts=dict(initialize=self.init_timeout_s,inference=self.inference_timeout_s,release=self.release_timeout_s),
                    event=dict(type='initialize',config=self.config,config_version=self.config_version,
                               tui_command_effects=['reload_policy'], runtime_tools=list(RUNTIME_TOOLS),
                               global_state=global_descriptor))
                self.runtime_id, self._pid = loaded['runtime_id'], loaded['pid']
            deadline = time.monotonic() + self.init_timeout_s
            with self._condition:
                while not self._ready:
                    self._check_wait(deadline, cancel_event, 'initialize')
                    self._condition.wait(.02)
                self._check_wait(deadline, cancel_event, 'initialize')
                self._state = 'running'
            self._log(f'policy ready host={self.config["id"]} runtime={self.runtime_id} pid={self.pid}')
        except BaseException:
            if self.endpoint is None:
                self.close()
            raise

    @property
    def has_command_results(self): return bool(self._command_responses)

    def _enqueue_monitor(self, event):
        return self.host.enqueue_monitor(dict(type="dispatch", runtime_id=self.runtime_id, session_id=self.session_id, event=event))

    def _enqueue(self, event):
        packet = dict(type='dispatch', runtime_id=self.runtime_id, session_id=self.session_id, event=event)
        if event['type'] in ('infer','observe','update_config','reset_context'):
            packet['operation_timeout_s'] = self.inference_timeout_s
        if event['type'] == 'policy_command': packet['command_timeout'] = self.command_specs[event['name']]['timeout_s']
        self.host.enqueue(packet)

    def _receive(self, packet):
        with self._condition:
            if self.runtime_id is not None and packet['runtime_id'] != self.runtime_id:
                if packet['event'].get('type') in ('result', 'error') and 'request_id' in packet['event']:
                    self._trace('stale', packet['event'], runtime_id=packet['runtime_id'], discarded=True, reason='runtime_id')
                return
            self.runtime_id = packet['runtime_id']
            message = packet['event']; kind = message['type']
            if kind == 'ready':
                if message.get('global_state_version') not in (None, 1):
                    self._record_failure('incompatible global state protocol'); return
                self.supports_monitor = message.get('monitor') is True
                self.supports_observe = message.get('observe') is True
                self.runtime_tools = runtime_tools(message.get('runtime_tools', []))
                mode = self.config.get('execution', {}).get('control_mode', 'qpos')
                if mode not in message.get('action_modes', ['qpos']):
                    self._record_failure('policy SDK does not support ' + mode); return
                self.supports_commands = message.get('tui_commands', False)
                if self.command_specs and not self.supports_commands:
                    self._record_failure('upgrade rc-policy-sdk: missing TUI commands'); return
                if any(spec.get('effect') == 'reload_policy' for spec in self.command_specs.values()) and 'reload_policy' not in message.get('tui_command_effects', []):
                    self._record_failure('upgrade rc-policy-sdk: missing reload_policy capability'); return
                self._ready = True
            elif kind == 'capabilities': self.supports_observe = message.get('observe', False)
            elif kind in ('observed','observation_error'):
                self._observation_responses.append(message); self._observation = None
            elif kind in ('monitor_result', 'monitor_error'):
                self._receive_monitor(message)
            elif kind.startswith('command_'): self._receive_command(message)
            elif kind == 'ack' or (kind == 'error' and message.get('operation_id') is not None):
                self._acks[message['operation_id']] = message
            elif kind in ('result','error') and 'request_id' in message:
                if self._inflight and all(message.get(k) == self._inflight[k] for k in ('request_id','generation','config_version')):
                    late = self._inflight.get('timed_out') or self._inflight.get('trace_cancelled')
                    self._trace('late' if late else kind, message, discarded=bool(late))
                    if not self._inflight.get('timed_out'):
                        if kind == 'result':
                            controls = control_requests(message.get('control_requests', []))
                            if controls and controls[0]['type'] not in self.runtime_tools:
                                raise ValueError('control_requests requires negotiated runtime_tools ' + controls[0]['type'])
                        self._responses.append(message)
                    self._inflight = None
                else:
                    self._trace('stale', message, discarded=True, reason='inference_identity')
            elif kind in ('fatal','error'): self._record_failure(message.get('error','policy failed'))
            elif kind == 'closed':
                self.release_error = message.get('release_error')
                self._forced |= bool(self.release_error)
                # Callback completion is not runtime-tree exit proof.
                self._ready = False
            elif kind == 'global_state_request':
                if self.global_state is not None:
                    self.global_state.receive(self, message, self._enqueue)
                else:
                    self._enqueue(dict(type='global_state_ack', rpc_id=message.get('rpc_id'), error='global state unavailable'))
            elif kind == 'resource':
                try:
                    if self.resources is None: raise RuntimeError('external resources unavailable')
                    result = self.resources.handle(self, message)
                    ack = dict(type='resource_ack',rpc_id=message['rpc_id'],result=result)
                except Exception as exc: ack = dict(type='resource_ack',rpc_id=message['rpc_id'],error=str(exc))
                self._enqueue(ack)
            self._condition.notify_all()

    def _confirm_closed(self, proof):
        with self._condition:
            if proof.get('runtime_id') != self.runtime_id and self.runtime_id is not None: return
            if proof.get('cleanup_confirmed') is not True: return
            self.cleanup_confirmed = True
            self._forced |= proof.get('forced', False)
            self.release_error = self.release_error or proof.get('error')
            self._record_failure(self.release_error or 'policy runtime stopped')
            self._condition.notify_all()

    def close_group(self, members, *, deadline=None):
        with self._close_lock, self._launch_lock:
            for member in members:
                member._trace_close()
            if self.cleanup_confirmed:
                self._state = 'closed'
                if self.host and all(p.cleanup_confirmed or not p._submitted_load for p in self.host.members.values()):
                    self.host.disconnect()
                return self._forced
            if not self._submitted_load:
                self.cleanup_confirmed = True; self._state = 'closed'; return self._forced
            self._state = 'closing'
            try:
                self._forced |= self.host.stop_group(self.runtime_id,
                    [p.session_id for p in members], self.release_timeout_s, deadline=deadline)
                if not self.cleanup_confirmed: raise RuntimeError('runtime cleanup unconfirmed')
                self._state = 'closed'
                return self._forced
            except Exception:
                self._state = 'cleanup_failed'
                raise

    def close(self, *, deadline=None): return self.close_group([self], deadline=deadline)

    def accept_exit_proof(self, proof):
        endpoint = self.endpoint
        if (endpoint is None or proof.authority is not self.exit_authority or not proof.removed or
                any(getattr(proof, key) != getattr(endpoint, key) for key in
                    ('deployment_id', 'daemon_id', 'policy_id', 'run_id', 'generation', 'container_id', 'host_instance_id'))):
            raise ValueError('untrusted or stale container exit proof')
        if self.host:
            self.host.disconnect()
        self._confirm_closed(dict(runtime_id=self.runtime_id, cleanup_confirmed=True, forced=proof.forced))
        self._state = 'closed'


    def _record_failure(self, error):
        if self.global_state is not None:
            self.global_state.revoke(self)
        with self._condition:
            if self._state not in ("closing", "closed"):
                self._failure = error
                if self._inflight:
                    if not self._inflight.get("timed_out"):
                        response = {"type": "error", **{key: self._inflight[key] for key in ("request_id", "generation", "config_version")}, "error": error}
                        if not self._inflight.get('trace_cancelled'):
                            self._trace('timeout' if error in ('policy infer timeout', 'policy inference timeout') else 'error', response)
                        self._responses.append(response)
                    self._inflight = None
            self._condition.notify_all()

    def _check_wait(self, deadline, cancel_event, operation):
        with self._condition:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError(f"policy {operation} cancelled")
            if self._state in ("closing", "closed", "cleanup_failed"):
                raise RuntimeError(f"policy {operation} cancelled by close")
            if self._failure:
                raise RuntimeError(self._failure)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"policy {operation} timeout")

    def submit(self, request):
        from rc_policy_sdk.actions import normalize_actions
        if not isinstance(request, dict) or any(key not in request for key in ("request_id", "generation", "config_version", "state", "action_chunk", "images")):
            raise ValueError("inference request is missing required fields")
        if any(type(request[key]) is not int or request[key] < 0 for key in ("request_id", "generation", "config_version")):
            raise ValueError("inference identity must contain nonnegative integers")
        if not isinstance(request["state"], dict) or not isinstance(request["images"], dict):
            raise ValueError("inference state/chunk/images have invalid types")
        mode = self.config.get("execution", {}).get("control_mode", "qpos")
        chunk = normalize_actions(request["action_chunk"], mode, allow_empty=True)
        request = {**request, "action_chunk": chunk.payload(mode)}
        with self._condition:
            if self._state != "running" or self._failure:
                raise RuntimeError(self._failure or "policy process is not ready")
            if self._inflight is not None or self._pending_operation:
                raise RuntimeError("policy already has an operation in flight")
            if request["config_version"] != self.config_version:
                raise ValueError("inference config version differs from supervisor")
            self._enqueue({**request, "type": "infer"})
            self._inflight = {key: request[key] for key in ("request_id", "generation", "config_version")}
            self._inflight["deadline"] = time.monotonic() + self.inference_timeout_s
            if self.on_trace is not None:
                self._trace_requests[self._trace_key(request)] = _inference_trace_context(request)
                while len(self._trace_requests) > 256:
                    self._trace_requests.popitem(last=False)
                self._trace('start', {**request, 'type': 'infer'})

    def observe(self, request):
        with self._condition:
            if self._state != 'running' or self._failure or self._pending_operation or self._observation is not None:
                return False
            self._enqueue({**request, 'type':'observe'})
            self._observation = request['request_id']
            return True

    def poll_observation(self):
        with self._condition:
            if self._observation_responses:
                return self._observation_responses.popleft()
            return None

    def poll(self):
        with self._condition:
            if self._responses:
                return self._responses.popleft()
            if self._inflight and not self._inflight.get("timed_out") and time.monotonic() >= self._inflight["deadline"]:
                self._inflight["timed_out"] = True
                response = {"type": "error", **{key: self._inflight[key] for key in ("request_id", "generation", "config_version")}, "error": "policy inference timeout"}
                self._trace('timeout', response)
                return response
            return None

    def _operation(self, kind, extras, cancel_event):
        with self._operation_lock:
            with self._condition:
                if self._state != "running" or self._failure:
                    raise RuntimeError(self._failure or "policy process is not ready")
                if self._inflight is not None:
                    raise RuntimeError("cannot update/reset while inference is in flight")
                if kind == "update_config":
                    prepare_patch(self.config, extras["patch"])
                    if type(extras["config_version"]) is not int or extras["config_version"] <= self.config_version:
                        raise ValueError("config version must increase")
                self.stop_commands()
                self._operation_number += 1
                operation_id = self._operation_number
                self._enqueue({"type": kind, "operation_id": operation_id, **extras})
                self._pending_operation = True
            try:
                deadline = time.monotonic() + self.inference_timeout_s
                while True:
                    with self._condition:
                        self._check_wait(deadline, cancel_event, kind)
                        if operation_id in self._acks:
                            response = self._acks.pop(operation_id)
                            if response["type"] == "error":
                                raise RuntimeError(response["error"])
                            if kind == "update_config":
                                apply_patch(self.config, extras["patch"])
                                self.config_version = extras["config_version"]
                            self._commands_blocked = False
                            return
                        self._condition.wait(.05)
            except (TimeoutError, RuntimeError):
                # Unknown acknowledgment state cannot be followed by another call.
                self.close()
                raise
            finally:
                with self._condition:
                    self._pending_operation = False

    def submit_command(self, identity, name, args):
        with self._condition:
            if (self._state != "running" or self._failure or self._pending_operation
                    or self._commands_blocked or self._command_fatal):
                raise RuntimeError("busy: policy is not accepting commands")
            if not self.supports_commands or name not in self.command_specs:
                raise ValueError("policy SDK/manifest does not support this command")
            validate_arguments(args, self.command_specs[name])
            if identity in self._command_calls or len(self._command_calls) >= 256:
                raise RuntimeError("duplicate command or full command queue")
            self._enqueue({"type": "policy_command", "command_id": identity, "name": name,
                           "args": args, "config_version": self.config_version})
            self._command_calls[identity] = {"timeout": self.command_specs[name]["timeout_s"],
                                             "version": self.config_version, "deadline": None}

    def stop_commands(self):
        with self._condition:
            self._commands_blocked = True
            if self._state == "running" and self.supports_commands and not self._failure and not self.cleanup_confirmed:
                try:
                    self._enqueue({"type": "command_gate"})
                except RuntimeError as exc:
                    self._record_failure(str(exc))

    def resume_commands(self):
        """Undo a gate only when no context-changing operation was launched."""
        with self._condition:
            if (self._state != "running" or self._failure or self._pending_operation
                    or self._command_fatal or not self.runnable):
                return False
            if self.supports_commands:
                self._enqueue({"type": "command_resume"})
            self._commands_blocked = False
            return True

    def _receive_command(self, message):
        identity = message.get("command_id")
        call = self._command_calls.get(identity)
        if call is None or message.get("config_version") != call["version"]:
            return
        kind = message["type"]
        if kind == "command_started":
            if call["timeout"] != -1:
                call["deadline"] = message["started_at"] + call["timeout"]
        elif kind in ("command_result", "command_error", "command_cancelled"):
            deadline = call["deadline"]
            if deadline is not None and message.get("finished_at", time.monotonic()) > deadline:
                message = {**message, "type": "command_timeout", "error": "policy command timeout"}
                self._command_fatal = True
            self._command_calls.pop(identity, None)
        else:
            raise ValueError(f"unexpected command response: {kind}")
        self._command_responses.append(message)

    def poll_command(self):
        with self._condition:
            for identity, call in list(self._command_calls.items()):
                if call["deadline"] is not None and time.monotonic() >= call["deadline"]:
                    self._command_calls.pop(identity)
                    self._command_fatal = True
                    self._command_responses.append({"type": "command_timeout", "command_id": identity,
                                                    "error": "policy command timeout"})
            if self._command_calls and self._state == "running" and (self._failure or not self.runnable):
                identity = next(iter(self._command_calls))
                self._command_calls.clear()
                self._command_fatal = True
                self._command_responses.append({"type": "command_error", "command_id": identity,
                                                "fatal": True, "error": self._failure or "policy worker exited"})
            if self._command_responses:
                return self._command_responses.popleft()
            return None

    def update_config(self, patch, version, cancel_event=None):
        self._operation("update_config", {"patch": patch, "config_version": version}, cancel_event)

    def reset_context(self, cancel_event=None):
        self._operation("reset_context", {}, cancel_event)
