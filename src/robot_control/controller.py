"""Single-writer robot loop with a separate, cancellable lifecycle manager."""
from __future__ import annotations

from .secret_config import public_value, public_message, reject_secret_edit

import copy
from dataclasses import replace
import json
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from rc_policy_sdk.images import ImagePool

from .external import ExternalResources
from .log_format import LEVELS, LogSource, format_log_time
from .buffer import ActionBuffer
from .yaml_config import save_yaml_config
from .config import PolicyRegistry, atomic_json, load_rc, validate_entry
from .process import PolicyProcess
from .robots import make_robot
from .safety import ModeLimiter, finite_qpos, arm_targets
from .policy_commands import PolicyCommandSupport
from .member_reload import MemberReloadSupport
from .chain_management import ChainManagement
from .policy_results import PolicyResultSupport
from .global_state_control import GlobalStateControl
from .recording_control import RecordingControl, RECORD_HELP
from .connector_lifecycle import ConnectorLifecycle
from .connector_control import ConnectorControl
from .connector import ImagesPending
from .scheduling import next_deadline, prepare_control_threads
from rc_policy_sdk.commands import BUILTIN_COMMANDS
from .help_content import builtin_help
from .help_document import help_text
from .human_commands import HumanCommandSupport
from .policy_configuration import PolicyConfigurationSupport
from .operations import ControlRequest, Operations
from .terminal_batches import TerminalBatches
from .direct import DirectMotion
from rc_policy_sdk.actions import normalize_actions
from rc_policy_sdk.control import control_requests


HELP = help_text(builtin_help())


from .policy_monitors import PolicyMonitors


@dataclass(frozen=True)
class ActionRequestSnapshot:
    request_id: int
    generation: int
    trim: int
    cached: int


class RobotController(HumanCommandSupport, PolicyConfigurationSupport, ConnectorLifecycle, ConnectorControl, MemberReloadSupport, PolicyCommandSupport, ChainManagement, PolicyResultSupport, GlobalStateControl, RecordingControl):
    def __init__(self, config_path, *, robot=None, process_factory=PolicyProcess, runtime_manager=None):
        self.config_path = Path(config_path).resolve()
        self.config = load_rc(self.config_path)
        self.root = self.config_path.parent
        self.registry = PolicyRegistry(self.root / self.config["policy_directory"])
        self._init_connector(robot)
        self.process_factory = process_factory
        self.events = queue.Queue(maxsize=10000)
        self.done = threading.Event()
        self.exit_code = 0
        self.buffer = ActionBuffer()
        self._last_action_request = None
        self.limiter = ModeLimiter(self.config["safety"], self.config["frequency_hz"])
        self._effective_safety = copy.deepcopy(self.config['safety'])
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._commands = queue.Queue()
        self._urgent = queue.Queue()
        self._jobs = queue.Queue()
        self._results = queue.Queue()
        self._owned = set()
        self._confirmed_closed = set()
        self._owned_lock = threading.RLock()
        self._thread = None
        self._manager = None
        self._operation = None
        self._job_serial = 0
        self._request_serial = 0
        self._inflight = None
        self._worker = None
        self.policy_monitors = PolicyMonitors(self._policy_monitor_source)
        self._identity = None
        self._connected = False
        self._motion = None
        self._hold = None
        self._hold_name = None
        self._needs_request = True
        self._pending_config = None
        self._shutdown_failed = False
        self._shutdown_deadline = None
        self._state = "created"
        self.control_owner = "policy"
        self._control_epoch = 0
        self._return_requested = False
        self._return_operation = None
        self._dispatch_id = None
        self._dispatch_origin = None
        self._exit_operations = []
        self._publish_fault = False
        self._published_targets = {}
        self._publications = {}
        self._last_submission = None
        self._last_send_at = 0.
        self.operations = Operations(control_lock=self._lock, control_snapshot=lambda: {
            'run_id': self.run_id, 'control_epoch': self._control_epoch, 'generation': self.buffer.generation})
        self._init_policy_results()
        self.direct = DirectMotion(self.config.get("direct_control", {}), self.config["frequency_hz"],
                                   self.operations, self._send_direct, self.robot, emit=self._emit)
        self._event_lock = threading.Lock()
        self._event_seq = 0
        self._event_history = deque(maxlen=10000)
        self._pool = ImagePool()
        self.run_id = uuid.uuid4().hex[:12]
        self.outputs = self.root / self.config["outputs_directory"]
        self.runtime = runtime_manager
        if self.runtime is None and process_factory is PolicyProcess:
            from .runtime_manager import ComposeRuntimeManager
            context_path = os.environ.get('RC_RUNTIME_CONTEXT', str(self.registry.directory.parent / '.setup/runtime.json'))
            self.runtime = ComposeRuntimeManager.from_file(context_path, run_id=self.run_id)
        self._runtime_groups = {}
        self._bind_connector_runtime(self.robot.raw)
        self._recovery = (self.runtime.state_dir.parent if self.runtime else self.outputs) / 'recovery'
        self._external = ExternalResources(self._pool, self._recovery / f'{self.run_id}.json',
                                           deployment_id=self.runtime.context['deployment_id'] if self.runtime else None)
        self._observation_serial = 0
        self._observe_inflight = None
        self._init_policy_commands()
        self._init_chain_editor()
        self._init_globals()
        self._init_recording()
        self._tui_batches = TerminalBatches(self)

    def _emit_command(self, message, level="info", *, record_history=True, help_document=None,
                      table_document=None, copy_json=None, log_context=None):
        self._emit(message, level, source=LogSource.COMMAND, record_history=record_history,
                   help_document=help_document, table_document=table_document,
                   copy_json=copy_json, log_context=log_context)

    def _emit_policy_log(self, event):
        self._emit(event['message'], event.get('level', 'info'),
                   table_document=event.get('table_document'), copy_json=event.get('copy_json'),
                   log_context=event)

    def _mcp_operation_logger(self, tool, details=None):
        """Follow the controller receipt, independently of an HTTP/local waiter."""
        def log(event):
            status = event['status']
            if status not in ('queued', 'succeeded', 'failed', 'cancelled'):
                return
            payload = {'operation_id': event['operation_id']}
            if status == 'queued':
                payload.update(details or {})
            else:
                payload.update({key: event[key] for key in ('error', 'reason') if key in event})
                if tool in ('send_command', 'move_eef', 'move_joints', 'return_and_wait',
                            'policy_command', 'policy_chain', 'record', 'tui_command') and 'result' in event:
                    payload['result'] = event['result']
            level = 'error' if status == 'failed' else 'warning' if status == 'cancelled' else 'info'
            self._emit_command(f'MCP {tool} {status}: ' + json.dumps(payload, ensure_ascii=False), level)
        return log

    def _emit(self, message, level="info", *, record_history=True, source=LogSource.DIAGNOSTIC, help_document=None,
              table_document=None, copy_json=None, log_context=None):
        if (source is not LogSource.COMMAND
                and LEVELS.get(str(level).upper(), LEVELS['INFO']) < LEVELS[self.config['logging']['level']]):
            return
        entry = {"time": format_log_time(time.time()), "level": level,
                 "message": public_message(message) if table_document is None else ''}
        if log_context is not None:
            entry.update(public_value({key: log_context[key] for key in (
                'event', 'instance_id', 'source_id', 'runtime_id', 'session_id', 'context_id', 'host_instance_id',
                'config_version', 'request_id', 'generation', 'command_id', 'command_status',
                'log_event', 'stage', 'count', 'count_saturated') if key in log_context}))
        if table_document is not None:
            from .table_output import public_table_document
            from rc_policy_sdk.tables import table_document_text
            try:
                document = public_table_document(table_document)
                entry['message'] = table_document_text(document)
                entry['table_document'] = document
            except (TypeError, ValueError, RecursionError):
                entry['message'] = public_message(copy_json if copy_json is not None else message)
        if copy_json is not None:
            entry['copy_json'] = public_message(copy_json)
        if help_document is not None:
            from .help_document import help_text
            entry['help_document'] = public_value(help_document)
            entry['message'] = help_text(entry['help_document'])
        if record_history:
            with self._event_lock:
                self._event_seq += 1
                entry["seq"] = self._event_seq
                self._event_history.append(entry.copy())
        try:
            self.events.put_nowait(entry)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(entry)
            except queue.Full:
                pass
        self._recorder.log({**entry, 'run_id':self.run_id, 'timestamp_ns':time.time_ns(),
                            'monotonic_ns':time.monotonic_ns()})

    def start(self):
        with self._lock:
            if self._thread is not None:
                return
            try:
                prepare_control_threads()
                self._recorder.start()
                self._manager = threading.Thread(target=self._manager_loop, name="rc-lifecycle", daemon=False)
                self._thread = threading.Thread(target=self._control_loop, name="rc-control", daemon=False)
                self._external.start()
                self._manager.start()
                self._thread.start()
            except Exception:
                self.exit_code = 1
                if self._manager is not None and self._manager.is_alive():
                    self._jobs.put(None)
                    self._manager.join(5)
                try:
                    self.robot.close()
                    self._external.close()
                    self._pool.close()
                finally:
                    self._recorder.shutdown(self._cleanup_deadline())
                    self._state = "stopped"
                    self.done.set()
                raise

    def close_unstarted(self):
        """Release allocations if CLI setup failed before any controller thread started."""
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("running controller must be closed through exit")
            try:
                self.robot.close()
                self._external.close()
                self._pool.close()
            finally:
                self._state = "stopped"
                self.done.set()

    @staticmethod
    def _is_read_command(line):
        try:
            words = shlex.split(line)
        except ValueError:
            return False
        return (RobotController._is_global_read(words) or words in (["status"], ["policies"], ["connector", "list"], ["connector", "status"], ["record", "status"], ["show", "rc"], ["show", "policy"])
                or bool(words and words[0] == 'help' and len(words) in (1, 2)))

    def _query(self, line):
        if line.strip() == 'connector list':
            return self.connector_profiles.list()
        with self._lock:
            words = shlex.split(line)
            if self._is_global_read(words):
                return self._query_global(words)
            if words == ["status"]:
                return self.status()
            if words == ['connector', 'status']:
                return self.connector_status()
            if words == ["policies"]:
                return self.registry.list()
            if words and words[0] == 'help' and len(words) in (1, 2):
                return HELP if len(words) == 1 else self.local_help(words[1])
            if words == ['record', 'status']:
                return self._recorder.status()
            if words == ["show", "rc"]:
                return public_value(self.config)
            if words == ["show", "policy"] and self._identity is not None:
                record = self.registry.get(self._identity)
                exported = None if record.is_chain else self._member_config_export(record)
                return public_value({**exported['policy'], **exported['settings']}
                                    if exported is not None else record.config)
            raise ValueError("unknown read command or no active policy")

    def _emit_query_result(self, command, result):
        from .query_tables import query_table_document
        from .table_output import public_table_fields
        try:
            document = query_table_document(command, result)
            fields = public_table_fields(result, document) if document is not None else None
        except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
            fields = None  # Optional presentation cannot change the query result.
        if fields is not None:
            self._emit_command(**fields)
        else:
            self._emit_command(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))

    def get_operation(self, identity):
        return self.operations.get(identity)

    def wait_operation(self, identity, timeout_s=30):
        return self.operations.wait(identity, timeout_s)

    def read_events(self, after_seq=0, limit=100):
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid event cursor or limit")
        with self._event_lock:
            items = [item.copy() for item in self._event_history if item["seq"] > after_seq][:limit]
            return {"events": items, "next_seq": items[-1]["seq"] if items else after_seq,
                    "first_seq": self._event_history[0]["seq"] if self._event_history else self._event_seq + 1}

    def _require_mcp(self):
        if self.control_owner != "mcp" or self._return_requested:
            raise PermissionError("MCP is read-only; call take before changing state")

    def _check_expected_control(self, expected):
        if expected is None:
            return
        if (not isinstance(expected, dict) or set(expected) != {'run_id', 'control_epoch', 'generation'}
                or not isinstance(expected['run_id'], str)
                or type(expected['control_epoch']) is not int or type(expected['generation']) is not int):
            raise ValueError('invalid expected_control')
        if (expected['run_id'] != self.run_id or expected['control_epoch'] != self._control_epoch
                or expected['generation'] != self.buffer.generation):
            raise PermissionError('control state changed; discard the stale client plan')

    def submit_tui_batch(self, text, *, expected_control=None):
        with self._lock:
            return self._tui_batches.submit(text, expected_control)

    def cancel_tui_batch(self, identity):
        with self._lock:
            return self._tui_batches.cancel(identity)

    def interrupt_tui_batches(self):
        with self._lock:
            self._tui_batches.interrupt()

    def _submit(self, line: str, *, origin="tui", policy_arguments=None, mcp_tool=None, expected_control=None, batch_id=None, parsed=None, display_line=None):
        if self.done.is_set():
            if origin == "tui" and line.strip() in ("exit", "quit"):
                return
            raise RuntimeError("controller is stopped")
        if origin not in ("tui", "mcp") or not isinstance(line, str):
            raise ValueError("invalid command origin or line")
        command = line.strip()
        if not command:
            return
        with self._lock:
            if batch_id is not None:
                if origin != 'tui' or self._tui_batches.active != batch_id:
                    raise ValueError('invalid text batch source')
            elif origin == 'tui':
                self._tui_batches.interrupt()
            reject_secret_edit(line)
            self._check_expected_control(expected_control)
            try:
                words = shlex.split(command)
            except ValueError:
                words = []
            if origin == "mcp" and words and (words[0] in ("mcp", "camera", "monitor", "manage-policy", "snapshot") or words[0].startswith('monitor-')):
                raise PermissionError(f"{words[0]} is a local-only terminal command")
            if parsed is None and origin == "mcp" and words and words[0] not in ("take", "return") and words[0] not in BUILTIN_COMMANDS and words[0] not in self._local_specs():
                raise ValueError('unknown or ambiguous policy command; use policy <instance_id> <command>')
            local_help = parsed is None and origin == "tui" and words and words[0] == "help" and len(words) in (1, 2)
            readonly = parsed.readonly if parsed is not None else self._is_read_command(command) or local_help
            recording = bool(words and words[0] == 'record')
            if command == "return" and self._return_operation is not None:
                return self._return_operation
            if origin == "mcp" and not readonly and not recording and command not in ("take", "return"):
                self._require_mcp()
            if origin == "mcp" and self._operation is not None and not readonly and not recording and command not in ("pause", "exit", "quit"):
                if command != 'return' or self._operation['kind'] not in ('connector_switch', 'connector_member_retry'):
                    raise RuntimeError(f"busy: {self._operation['kind']}")
            if (self._operation is not None and self._operation['kind'] in ('connector_switch', 'connector_member_retry')
                    and not readonly and not recording and command not in ('pause', 'return', 'take', 'exit', 'quit')):
                raise RuntimeError(f"busy: {self._operation['kind']}")
            observer = None
            if origin == 'mcp' and not readonly:
                tool = mcp_tool or (command if command in ('take', 'return') else 'record' if recording else 'command')
                observer = self._mcp_operation_logger(tool, {'line': command})
            identity = self.operations.create(display_line or command, observer=observer)
            if words and words[0] == 'retry-reload':
                self._queue_reload_retry(identity, command, words, origin=origin, expected_control=expected_control)
                return identity
            if recording and not readonly:
                self._submit_recording(identity, words)
                return identity
            if readonly:
                if words == ['connector', 'list']:
                    self._commands.put(ControlRequest(identity, command, origin, self._control_epoch))
                    self._wake.set()
                    return identity
                try:
                    result = self._query_parsed(parsed) if parsed is not None else self.local_help(words[1] if len(words) == 2 else None) if local_help else self._query(command)
                    self.operations.update(identity, "succeeded", result=result)
                    help_document = (result.get('help_document') if parsed is not None
                        and parsed.kind in ('help', 'policy_help') and isinstance(result, dict) else None)
                    if help_document is not None:
                        self._emit_command(result['help'], help_document=help_document)
                    else:
                        self._emit_query_result(command, result)
                except Exception as exc:
                    self.operations.update(identity, "failed", error=exc)
                    self._emit_command(f"命令失败：{exc}", "error")
                return identity
            if words and parsed is not None and parsed.kind == "policy_command":
                try:
                    custom = self._policy_request(identity, command, words[0], origin=origin, arguments=policy_arguments)
                except Exception as exc:
                    self.operations.update(identity, "failed", error=exc)
                    self._emit_command(f"policy command [{identity}] failed: {exc}", "error")
                    return identity
                if custom is not None:
                    custom = replace(custom, expected_control=copy.deepcopy(expected_control))
                    self._commands.put(custom)
                    self._wake.set()
                    return identity
            try:
                config_request = self._configure_request(identity, parsed, origin, expected_control) if parsed is not None and parsed.kind == "policy_config_set" else None
                if config_request is not None:
                    self._commands.put(config_request)
                    self._wake.set()
                    return identity
                if parsed is not None and parsed.kind == 'policy_config_save':
                    self._active_member(parsed.target)
                save_request = self._policy_save_request(identity, command, words, origin)
            except Exception as exc:
                self.operations.update(identity, 'failed', error=exc)
                self._emit_command(f'配置操作失败：{exc}', 'error')
                return identity
            if save_request is not None:
                save_request = replace(save_request, expected_control=copy.deepcopy(expected_control))
                self._commands.put(save_request)
                self._wake.set()
                return identity
            if command in {"exit", "quit"}:
                if self._shutdown_deadline is None:
                    self._shutdown_deadline = time.monotonic() + self.config.get("timeouts", {}).get("shutdown_s", 115)
                self._stop.set()
                if self._operation is not None:
                    self._operation["cancel"].set()
            if words and words[0] in ('switch', 'take', 'exit', 'quit'):
                self._invalidate_policy_editor()
            if command == "return":
                if self._operation is not None and self._operation['kind'] in ('connector_switch', 'connector_member_retry'):
                    self._operation['cancel'].set()
                self._return_requested = True
                self._return_operation = identity
            request = ControlRequest(identity, command, origin, self._control_epoch,
                                     expected_control=copy.deepcopy(expected_control), parsed=parsed)
            urgent = command in {"exit", "quit", "pause", "take", "return"}
            target = self._urgent if urgent else self._commands
            target.put(request)
            self._wake.set()
            return identity

    def submit_direct(self, steps, *, clear_queue=False, tool_name='send_command', expected_control=None):
        with self._lock:
            self._check_expected_control(expected_control)
            self._require_mcp()
            if self._connector_failures or not self._connected or self._operation is not None or self._motion is not None or self._stop.is_set():
                raise RuntimeError("busy: robot connection or lifecycle operation")
            if self._publish_fault:
                raise RuntimeError("command publication fault; repair the connection and reset or return before moving")
            if type(clear_queue) is not bool:
                raise ValueError("clear_queue must be a boolean")
            details = {'steps': len(steps), 'arms': sorted({arm for step in steps for arm in step.arms}),
                       'clear_queue': clear_queue, 'first_step': steps[0].text if steps else None}
            identity = self.operations.create("direct motion", observer=self._mcp_operation_logger(tool_name, details))
            self._commands.put(ControlRequest(identity, "direct", "mcp", self._control_epoch, tuple(steps), clear_queue,
                                              expected_control=copy.deepcopy(expected_control)))
            self._wake.set()
            return identity

    def _cancel_direct(self, reason):
        self.direct.cancel(reason)
        # A priority interrupt also cancels requests still awaiting dispatch,
        # not just steps already installed in the direct motion queue.
        retained = []
        while True:
            try:
                request = self._commands.get_nowait()
            except queue.Empty:
                break
            if request.line == "direct" and request.origin == "mcp" and request.policy_name is None:
                self.operations.update(request.operation_id, "cancelled", error=reason)
            else:
                retained.append(request)
        for request in retained:
            self._commands.put(request)

    def _dispatch(self, request):
        identity = request.operation_id
        if not self._tui_batches.claim_child(identity):
            return
        try:
            if self.operations.get(identity)['status'] in ('succeeded', 'failed', 'cancelled'):
                return
        except ValueError:
            return  # Only terminal receipts can have expired from the bounded history.
        policy_wait = request.origin == 'mcp' and request.policy_name is None and request.line == 'return_and_wait'
        try:
            self._check_expected_control(request.expected_control)
            if policy_wait and (
                    self._policy_wait is None or self._policy_wait['handoff_operation_id'] != identity):
                return  # An earlier urgent intervention cancelled the reservation.
            if request.origin == "mcp" and not self.is_read_command(request.line) and (request.policy_name is not None or request.line not in ("take", "return", "return_and_wait")):
                if (request.epoch != self._control_epoch
                        or request.generation is not None and request.generation != self.buffer.generation):
                    self.operations.update(identity, "cancelled", error="control ownership or generation changed")
                    return
                if request.line != "resume":
                    self._require_mcp()
            if request.policy_name is not None:
                self._dispatch_policy_command(request)
                return
            self.operations.update(identity, "running")
            self._dispatch_id = identity
            self._dispatch_origin = request.origin
            if self._dispatch_human(request):
                return
            if request.connector_retry is not None:
                self._dispatch_connector_member_retry(request)
                return
            if request.config_edit is not None:
                self._dispatch_configure(request)
                return
            if request.retry_candidate is not None:
                self._dispatch_reload_retry(request)
                return
            if request.save_target is not None:
                self._dispatch_policy_save(request)
                return
            if self._dispatch_global(request):
                return
            if request.chain_action is not None:
                self._dispatch_chain_editor(request)
                return
            if request.line == "direct":
                if self._operation is not None or self._motion is not None:
                    raise RuntimeError("busy: lifecycle or reset motion")
                self.robot.read_state()
                self.direct.enqueue(identity, request.steps, request.clear_queue)
                return
            word = request.line.split(maxsplit=1)[0]
            previous_motion = self._motion
            result = self._begin_policy_wait() if policy_wait else self._command(request.line)
            if self._operation is not None and self._operation.get("public_id") == identity:
                return
            if self._motion is not None and self._motion is not previous_motion:
                self._motion["public_id"] = identity
                return
            if word in ("exit", "quit"):
                self._exit_operations.append(identity)
                return
            self.operations.update(identity, "succeeded", result=result if result is not None else self.status())
            if identity == self._return_operation:
                self._return_operation = None
        except Exception as exc:
            self._pending_config = None
            if self._policy_wait is not None and identity == self._policy_wait['handoff_operation_id']:
                self._fail_policy_wait(exc)
            self.operations.update(identity, "failed", error=exc)
            self._emit_command(f"命令失败：{exc}", "error")
            if identity == self._return_operation:
                self._return_operation = None
                self._return_requested = False
        finally:
            self._dispatch_id = None
            self._dispatch_origin = None

    def status(self):
        with self._lock:
            record = self.registry.get(self._identity) if self._identity is not None else None
            retry = self._retry_candidate.member.instance_id if self._retry_candidate and self._operation is None else None
            retries = list(self._connector_failures)
            retry = retry or (retries[0] if retries and self._operation is None else None)
            last_request = self._last_action_request
            return {
                "run_id": self.run_id,
                "connector": self.connector_status(), "retry_reload_instances": retries or ([retry] if retry else []),
                "state": self._state, "policy": self._identity,
                "runtime": self.runtime.snapshot() if self.runtime else None,
                "queue_remaining": len(self.buffer.remaining()),
                "last_action_request": (asdict(last_request) if last_request is not None
                                        and last_request.generation == self.buffer.generation else None),
                "in_flight": self._inflight is not None,
                "generation": self.buffer.generation, "consumed": self.buffer.consumed,
                "consumed_since_request": self.buffer.consumed_since_request,
                "config_version": record.version if record else 0,
                "pending_config": public_value(self._pending_config),
                "retry_reload_instance": retry, "retry_reload_command": f'policy {retry} retry' if retry else None,
                "hold_name": self._hold_name, "exit_code": self.exit_code,
                "control_owner": self.control_owner, "control_epoch": self._control_epoch,
                "returning": self._return_requested, "direct": self.direct.progress(),
                "policy_wait_operation_id": self._policy_wait['operation_id'] if self._policy_wait else None,
                "recording": self._recorder.status(),
            }

    def _statusbar_snapshot(self):
        """Copy display-only memory under RC -> connector/recorder lock order."""
        with self._lock:
            source, config = self.robot._binding_metadata() if self._connected else (None, None)
            runtime = self.runtime.snapshot() if self.runtime else {}
            recording = self._recorder.status()
            request = self._last_action_request
            return {
                'state': self._state, 'policy': self._identity, 'control_owner': self.control_owner,
                'in_flight': self._inflight is not None, 'consumed': self.buffer.consumed,
                'last_action_request': (asdict(request) if request is not None
                                        and request.generation == self.buffer.generation else None),
                'runtime': runtime.get('phase'), 'target': runtime.get('target'),
                'runtime_error': runtime.get('error'),
                'recording_state': recording.get('state'), 'recording_error': recording.get('error'),
                'connector_source': source, 'connector_config': config,
            }

    def capture_snapshot(self):
        """Capture cached feedback and retain immutable frames for background I/O."""
        from .telemetry import CAMERAS
        if hasattr(self.robot, 'wait_images'): self.robot.wait_images()

        with self._lock:
            if not self._connected or self._stop.is_set():
                raise RuntimeError("robot is not connected or is shutting down")
            captured_at = time.time()
            state = self.robot.read_state()
            images = self.robot.read_images()
            missing = [camera for camera in CAMERAS if camera not in images]
            if missing:
                raise RuntimeError(f"missing camera frames: {', '.join(missing)}")
            payload = {
                "captured_at": captured_at, "run_id": self.run_id,
                "state": copy.deepcopy(state), "control": self.status(),
                "targets": copy.deepcopy(self._published_targets),
            }
            return payload, {camera: images[camera] for camera in CAMERAS}

    def _policy_monitor_source(self):
        with self._lock:
            worker = self._worker
            members = worker.monitor_members() if worker is not None and hasattr(worker, 'monitor_members') else {}
            if self._operation is not None or self._stop.is_set():
                return members, True
            return members, {self._retry_candidate.member.instance_id} if self._retry_candidate else False

    def monitor_snapshot(self):
        """Read feedback and confirmed output under the controller lock."""
        with self._lock:
            targets = copy.deepcopy(self._published_targets)
            try:
                if not self._connected:
                    raise RuntimeError("robot is not connected")
                state = self.robot.read_state()
                return {"qpos": state["qpos"], "epos": state["eef"], "targets": targets, "error": None}
            except (RuntimeError, ValueError) as exc:
                return {"qpos": None, "epos": None, "targets": targets, "error": str(exc)}

    def _pause(self, state="paused", *, cancel_wait=True):
        if self._worker is not None and hasattr(self._worker, 'cancel_inference'):
            self._worker.cancel_inference()
        self.buffer.invalidate()
        self._last_action_request = None
        self._needs_request = True
        if self._motion is not None:
            self.operations.update(self._motion.get("public_id"), "cancelled", error=f"motion interrupted: {state}")
        self._motion = None
        if self.limiter.previous is not None:
            self._hold = self.limiter.previous.copy()
        self._state = state
        if cancel_wait:
            self._settle_policy_wait('cancelled', reason='controller stopped' if self._stop.is_set() else '人工接管')

    def _operation_start(self, kind, *, wait_inflight=False, **payload):
        if self._operation is not None:
            raise RuntimeError("生命周期操作正在执行")
        if kind in ("switch", "reload_member", "reset_context", "hot_patch", "configure", "unload", "shutdown", "connector_switch", "connector_member_retry"):
            self._gate_policy_commands(payload.get("worker", payload.get("old_worker", self._worker)))
        self._job_serial += 1
        self.policy_monitors.invalidate(payload['member'].instance_id if kind == 'reload_member' else None)
        job = {"id": self._job_serial, "kind": kind, "cancel": threading.Event(),
               "public_id": self._dispatch_id, 'origin': self._dispatch_origin or 'tui',
               'epoch': self._control_epoch, 'source_generation': self.buffer.generation, **payload}
        if self._stop.is_set() and kind != "shutdown":
            job["cancel"].set()
        job["launched"] = not (wait_inflight and self._inflight is not None)
        self._operation = job
        if job["launched"]:
            self._jobs.put(job)

    def _launch_deferred(self):
        if self._operation and not self._operation["launched"] and self._inflight is None:
            self._operation["launched"] = True
            self._jobs.put(self._operation)

    def _cleanup_deadline(self):
        return self._shutdown_deadline if self._shutdown_deadline is not None else time.monotonic() + self.config.get('timeouts', {}).get('shutdown_s', 115)

    def _close_worker(self, worker, deadline=None, *, handles=None):
        if worker is None:
            return
        from .runtime_manager import remaining, BlockedCleanup
        deadline = deadline if deadline is not None else self._cleanup_deadline()
        owners = tuple(worker.member_workers().values()) if hasattr(worker, 'member_workers') else (worker,)
        handles = self._runtime_groups.get(worker, ()) if handles is None else handles
        for owner in owners:
            self._external.begin_close(owner, deadline=deadline)
        forced = False
        sdk_error = None
        try:
            if self.runtime:
                # Reserve container/external fallback time inside the same total budget.
                sdk_deadline = min(deadline - min(15, remaining(deadline)/2),
                                   time.monotonic() + self.config.get('timeouts', {}).get('release_s', 30) + 6)
                forced = worker.close(deadline=sdk_deadline)
            else:
                forced = worker.close()
        except Exception as exc:
            sdk_error = exc
            forced = True
        if forced:
            self.exit_code = 1
            if self.runtime:
                self.runtime.cleanup_forced = True
                for handle in handles:
                    handle.cleanup_forced = True
                    self.runtime._save(handle)
        if self.runtime:
            self.runtime.phase = 'STOPPING'
            failures = []
            for handle in handles:
                try:
                    proof = self.runtime.stop_and_remove(handle, deadline)
                    worker.accept_exit_proof(proof)
                    forced |= proof.forced
                except Exception as exc:
                    failures.append(str(exc))
            if failures:
                self.runtime.phase = 'BLOCKED_CLEANUP'
                raise BlockedCleanup('; '.join(failures))
        elif sdk_error:
            raise sdk_error
        if getattr(worker, 'has_readers', worker.alive):
            raise BlockedCleanup('policy readers remain; cannot reclaim images')
        remote_forced = False
        errors = []
        for owner in owners:
            try:
                remote_forced |= self._external.close_worker(owner, deadline=deadline)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            if forced or remote_forced:
                self.exit_code = 1
            raise BlockedCleanup('; '.join(errors))
        with self._owned_lock:
            self._owned.discard(worker)
            self._confirmed_closed.add(worker)
        self._runtime_groups.pop(worker, None)
        if forced or remote_forced:
            self.exit_code = 1
            if self.runtime:
                self.runtime.cleanup_forced = True
            self._emit(f'policy 强制清理已取得退出证明：{sdk_error or getattr(worker, "release_error", None) or "forced"}', 'warning')

    def _manager_loop(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            result = {"type": "job_done", "id": job["id"], "kind": job["kind"]}
            try:
                result.update(self._run_job(job))
            except Exception as exc:
                result["error"] = str(exc)
                if self.runtime and not job.get('kept_context'):
                    from .runtime_manager import BlockedCleanup, BlockedInfrastructure
                    if isinstance(exc, BlockedInfrastructure):
                        self.runtime.phase = 'BLOCKED_INFRA'
                    elif isinstance(exc, BlockedCleanup):
                        self.runtime.phase = 'BLOCKED_CLEANUP'
                    elif job['kind'] in ('reload_member', 'configure') and self._retry_candidate is not None:
                        self.runtime.phase = 'FAULT_MEMBER'
                    elif job.get('prepared') or job['kind'] == 'switch' and self._worker is None:
                        self.runtime.phase = 'FAULT_EMPTY'
                    self.runtime.error = str(exc)
            self._results.put(result)
            self._wake.set()

    def _prepare_switch(self, job, *, invalid_infrastructure=False):
        cancel = job['cancel']
        prepared = threading.Event()
        self._results.put({'type':'switch_prepare', 'id':job['id'], 'complete':prepared,
                           'invalid_infrastructure': invalid_infrastructure})
        self._wake.set()
        while not prepared.wait(.05):
            if cancel.is_set() and not invalid_infrastructure:
                raise RuntimeError('policy switch cancelled')
        if job.get('prepare_error'):
            raise RuntimeError(job['prepare_error'])
        if cancel.is_set() and not invalid_infrastructure:
            raise RuntimeError('policy switch cancelled')

    def _retire_invalid_group(self, job):
        self._prepare_switch(job, invalid_infrastructure=True)
        self._close_worker(job.get('old_worker'))
        self._results.put({'type':'switch_empty', 'id':job['id']})
        self._wake.set()

    def _run_job(self, job):
        kind, cancel = job["kind"], job["cancel"]
        if kind == 'configure':
            return self._run_configure(job)
        if kind == 'connector_list':
            return {'profiles': self.connector_profiles.list()}
        if kind == 'connector_switch':
            return self._run_connector_switch(job)
        if kind == 'connector_member_retry':
            return self._run_connector_member_retry(job)
        if kind in ('chain_open', 'chain_save'):
            return self._run_chain_editor_job(job)
        if kind == 'save_member':
            target = job['save_target']
            with self._lock:
                if cancel.is_set() or not self._save_target_current(target):
                    raise RuntimeError('policy instance/configuration changed before save')
            if target.config_export is not None:
                from contextlib import contextmanager
                from .config_export import save_export
                from .config import policy_config_path, validate_policy
                from .yaml_config import load_yaml_config
                if target.record.directory != self.registry.directory / target.record.source_id:
                    raise ValueError('policy source directory differs')
                exported = copy.deepcopy(target.config_export)
                exported['policy']['id'] = target.record.source_id
                path = policy_config_path(target.record.directory)
                validate_policy(exported['policy'], target.record.source_id)
                validate_policy(load_yaml_config(path), target.record.source_id)
                @contextmanager
                def current():
                    with self._lock:
                        if cancel.is_set() or not self._save_target_current(target):
                            raise RuntimeError('policy instance/configuration changed before save')
                        yield
                paths = save_export(target.record.directory, path, exported, guard=current)
                return {'saved_path': paths}
            path = (self.registry.save_record(target.record) if target.record is not None
                    else self.registry.save(target.identity))
            return {'saved_path': str(path)}
        if kind == "connect":
            if self.runtime:
                from .runtime_manager import BlockedCleanup, BlockedInfrastructure
                try:
                    deadline = self._cleanup_deadline()
                    self.runtime.acquire(deadline)
                    self.runtime.check_infrastructure(deadline, cancel=cancel)
                    self._external.anchor_id = self.runtime.anchor_id
                    self._external.anchor_epoch = self.runtime.anchor_epoch
                    self.runtime.cleanup_owned(deadline)
                    if self._external.recover(self._recovery, deadline, self.runtime.context['deployment_id'], anchor_id=self.runtime.anchor_id, anchor_epoch=self.runtime.anchor_epoch):
                        self.runtime.cleanup_forced = True
                        self.exit_code = 1
                    self.runtime.recover_pools(deadline)
                    self.runtime.record_pool(self._pool.pool_id)
                    if self.runtime.cleanup_forced:
                        self.exit_code = 1
                except BlockedInfrastructure:
                    raise
                except Exception as exc:
                    raise BlockedCleanup(f'startup recovery unconfirmed: {exc}') from exc
            self._start_globals(cancel)
            self._connect_initial_connector(job)
            return {"connected": True}
        if kind == "shutdown":
            deadline = self._cleanup_deadline()
            failures = []
            with self._owned_lock:
                workers = list(self._owned)
            for worker in workers:
                try:
                    self._close_worker(worker, deadline)
                except Exception as exc:
                    failures.append(str(exc))
            if self.runtime:
                try:
                    self.runtime.cleanup_owned(deadline, include_connectors=False)
                except Exception as exc:
                    failures.append(str(exc))
            try:
                self._save_globals(deadline)
            except Exception as exc:
                failures.append(f'global state 保存失败：{exc}')
            try:
                self._close_globals(deadline, reclaim=not failures)
            except Exception as exc:
                failures.append(f'global state 清理失败：{exc}')
            if failures:
                from .runtime_manager import BlockedCleanup
                raise BlockedCleanup("；".join(failures))
            return {}
        if kind == "unload":
            self._close_worker(job["worker"])
            return {}
        if kind == "close_robot":
            self._close_connectors(deadline=self._cleanup_deadline())
            return {}
        if kind == "reset_context":
            job["worker"].reset_context(cancel_event=cancel)
            return {}
        if kind == "hot_patch":
            if job.get('command_request') is not None:
                return self._run_command_hot_patch(job)
            job["worker"].update_config(job["patch"], job["version"], cancel_event=cancel)
            return {"config": job["config"], "identity": job["identity"]}
        from .runtime_manager import BlockedInfrastructure
        if kind == 'reload_member':
            try:
                return self._run_member_reload(job)
            except BlockedInfrastructure:
                self._retire_invalid_group(job)
                raise
        if kind != "switch":
            raise ValueError(f"未知生命周期操作：{kind}")
        with self._lock:
            self._check_mcp_job(job)
        self._finish_reload_cleanup()
        if not self._connected:
            raise RuntimeError('robot/recovery is not ready; restart after successful cleanup')
        record = job['record']
        if record is None:
            record = (self.registry.snapshot(job['target_id']) if job.get('target_id')
                      else self.registry.propose_chain(job['chain_ids']))
        specs = []
        if self.runtime:
            if self.runtime.phase == 'BLOCKED_INFRA' or (
                    self.runtime.phase == 'BLOCKED_CLEANUP' and self._retry_candidate is None):
                raise RuntimeError('runtime is blocked; cleanup/restart is required before switching')
            self.runtime.target = record.config['id']
            self.runtime.phase = 'PREFLIGHT'
            self.runtime.error = None
        try:
            for member in record.members if record.is_chain else (record,):
                if cancel.is_set():
                    raise RuntimeError('policy preflight cancelled')
                validate_entry(member.directory)
                if self.runtime:
                    specs.append(self.runtime.prepare_spec(member, deadline=time.monotonic() + 20, cancel=cancel))
        except BlockedInfrastructure:
            self._retire_invalid_group(job)
            raise
        if cancel.is_set():
            raise RuntimeError("policy 切换已取消")
        self._prepare_switch(job)
        self._close_worker(job.get("old_worker"))
        self._results.put({'type':'switch_empty', 'id':job['id']})
        self._wake.set()
        if job.get("reset", False):
            complete = threading.Event()
            self._results.put({"type": "switch_reset", "id": job["id"], "complete": complete})
            self._wake.set()
            while not complete.wait(0.05):
                if cancel.is_set():
                    raise RuntimeError("切换 reset 已取消")
        if cancel.is_set():
            raise RuntimeError("policy 初始化已取消")
        timeouts = self.config.get("timeouts", {})
        init_timeout_s = timeouts.get('initialize_s', 600)
        handles = []
        worker = None
        try:
            if self.runtime:
                self.runtime.phase = 'CREATING'
                deadline = time.monotonic() + timeouts.get('initialize_s', 600)
                for spec in specs:
                    handle = self.runtime.start(spec, job['id'], cancel, deadline)
                    handles.append(handle)
                    self.runtime.phase = 'WAITING_HOST'
                    self.runtime.wait_ready(handle, cancel, deadline)
                init_timeout_s = max(.001, deadline-time.monotonic())
            worker = self._make_worker(record, handles, init_timeout_s)
            with self._owned_lock:
                self._owned.add(worker)
            self._runtime_groups[worker] = handles
            self._results.put({'type':'switch_initializing', 'id':job['id']})
            self._wake.set()
            if self.runtime:
                self.runtime.phase = 'INITIALIZING'
            worker.start(cancel_event=cancel)
            if cancel.is_set():
                raise RuntimeError('policy initialization cancelled')
            if job.get('origin') == 'mcp':
                # Commit under the same lock as ownership/generation changes.
                # The control-thread acknowledgement must not install a late worker.
                with self._lock:
                    self._check_mcp_job(job, after_retire=True)
                    self._worker, self._identity = worker, record.config['id']
                    self.registry.install(record)
                    job['committed'] = True
        except Exception:
            cleanup_deadline = self._cleanup_deadline()
            cleanup_error = None
            if worker is not None:
                try:
                    self._close_worker(worker, cleanup_deadline)
                except Exception as exc:
                    cleanup_error = exc
            if self.runtime:
                try:
                    self.runtime.cleanup_owned(cleanup_deadline, include_connectors=False)
                except Exception as exc:
                    cleanup_error = exc
            if cleanup_error:
                from .runtime_manager import BlockedCleanup
                raise BlockedCleanup(str(cleanup_error)) from cleanup_error
            raise
        return {"worker": worker, "identity": record.config["id"], "config": record.config,
                "record": record, "commit": job.get("commit", False)}

    def _make_worker(self, record, handles, init_timeout_s):
        timeouts = self.config.get('timeouts', {})
        worker_options = dict(
            global_state=self._global_service,
            init_timeout_s=init_timeout_s,
            inference_timeout_s=timeouts.get("inference_s", 60),
            release_timeout_s=timeouts.get("release_s", 30),
            on_log=self._emit if record.is_chain else lambda message: self._emit(f"[{record.config['id']}] {message}"),
            on_trace=self._recorder.policy_event,
            on_log_event=self._emit_policy_log,
        )
        endpoints = {h.policy_id:h for h in handles}
        if record.is_chain:
            from .chain_process import ChainProcess
            worker = ChainProcess(record, process_factory=self.process_factory, endpoints=endpoints,
                                  exit_authority=self.runtime.authority if self.runtime else None, **worker_options)
        else:
            for key, value in record.config.get('timeouts', {}).items():
                option = {'initialize_s':'init_timeout_s', 'inference_s':'inference_timeout_s', 'release_s':'release_timeout_s'}[key]
                worker_options[option] = min(worker_options[option], value)
            worker = self.process_factory(record.directory, copy.deepcopy(record.config),
                record.version, endpoint=endpoints.get(record.config['id']),
                exit_authority=self.runtime.authority if self.runtime else None, **worker_options)
        worker.resources = self._external
        return worker

    def _clear_finished_lease(self):
        with self._owned_lock:
            confirmed = self._inflight is not None and self._inflight["worker"] in self._confirmed_closed
        if confirmed:
            self._inflight["lease"].release()
            self._inflight = None

    def _handle_internal(self):
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                return
            job = self._operation
            if job is None or result["id"] != job["id"]:
                continue
            emit = self._emit_command if job.get('public_id') else self._emit
            if result['type'] == 'connector_step':
                self._handle_connector_step(result, job)
                continue
            if result['type'] == 'switch_empty':
                self._connector_failures.clear()
                self._connector_members.clear()
                if not job.get('committed'):
                    self._worker = self._identity = None
                self._clear_finished_lease()
                continue
            if result["type"] == "switch_initializing":
                if not self._stop.is_set() and not job["cancel"].is_set():
                    self._state = "initializing"
                continue
            if result["type"] == "switch_prepare":
                invalid = result.get('invalid_infrastructure', False)
                if (self._stop.is_set() or job["cancel"].is_set()) and not invalid:
                    continue
                if job['kind'] == 'switch' and not invalid:
                    try:
                        self._check_mcp_job(job)
                    except RuntimeError as exc:
                        job['prepare_error'] = str(exc)
                        result['complete'].set()
                        continue
                if job['kind'] in ('reload_member', 'configure') and not invalid:
                    try:
                        self._prepare_member_reload(job)
                    except RuntimeError as exc:
                        job['prepare_error'] = str(exc)
                        result["complete"].set()
                        continue
                else:
                    self._retry_candidate = None
                    self._reload_owner = None
                if job.get('origin') == 'mcp':
                    self._cancel_direct('cancelled by policy lifecycle change')
                self._pause("reloading" if job.get("commit") else "switching",
                            cancel_wait=not (job["kind"] == "configure" and job.get("action") is None))
                job['prepared_generation'] = self.buffer.generation
                job['prepared'] = True
                result["complete"].set()
                continue
            if result["type"] == "switch_reset":
                if self._stop.is_set() or job["cancel"].is_set():
                    continue
                self._motion = {"target": finite_qpos(self.config["reset_qpos"]), "complete": result["complete"], "source": "reset"}
                self._state = "moving"
                continue
            # The manager retains its last job while waiting for more work.
            # Transfer display ownership on every terminal result, including
            # failure/cancel/stop paths that return before success publication.
            command_table_output = job.pop('command_table_output', None)
            self._operation = None
            self._clear_finished_lease()
            if job['kind'] in ('connector_switch', 'connector_list', 'connector_member_retry'):
                self._finish_connector_job(job, result)
                continue
            if job['kind'] in ('chain_open', 'chain_save', 'save_member'):
                if result.get('error') or job['cancel'].is_set():
                    error = result.get('error', 'editor operation cancelled')
                    self.operations.update(job.get('public_id'), 'failed', error=error)
                    emit(error, 'error')
                else:
                    self.operations.update(job.get('public_id'), 'succeeded', result=result.get('editor', result.get('saved_path')))
                continue
            if result.get("connected"):
                limiter, safety = self._connection_limiter(self.robot.raw, self.robot.read_state())
                self.limiter, self._effective_safety = limiter, safety
                self._connected = True
                self._hold = self.limiter.previous.copy()
            if job["kind"] == "shutdown":
                if result.get("error"):
                    self.exit_code = 1
                    self._shutdown_failed = True
                    emit(f"退出清理未完成：{result['error']}；保留恢复账本和未确认图像，宿主 down 可继续收尾", 'error')
                    self._connected = False
                    self._state = 'closing_robot'
                    self._operation_start('close_robot')
                else:
                    self._connected = False
                    self._state = "closing_robot"
                    self._operation_start("close_robot")
                continue
            if job["kind"] == "close_robot":
                if result.get("error"):
                    self.exit_code = 1
                    emit(f"机器人关闭错误：{result['error']}", "error")
                self._finish_shutdown()
                continue
            if self._stop.is_set():
                self._settle_policy_wait('cancelled', reason='controller stopped')
                self.operations.update(job.get("public_id"), "cancelled", error="controller is shutting down")
                continue
            if result.get("error"):
                self._pending_config = None
                if job['kind'] == 'reload_member' and self._reload_owner and not self._reload_owner.reload_recoverable:
                    self._retry_candidate = self._reload_owner = None
                editor_preflight = job.get('edit_token') is not None and not job.get('prepared')
                static_rejection = (job['kind'] in ('switch', 'reload_member') and not job.get('prepared')
                                    and self._worker is not None) or job.get('kept_context')
                if job.get('kept_context') and job.get('prepared') and self._state == 'reloading':
                    self._state = 'paused'
                if not static_rejection:
                    self._pause("paused" if job["cancel"].is_set() or job.get("returning") or editor_preflight else "fault",
                                cancel_wait=job['cancel'].is_set())
                emit(result["error"], "warning" if job["cancel"].is_set() else "error")
                self._return_requested = False
                self._return_operation = None
                if job["kind"] in ("switch", "reload_member", "configure"):
                    self._restore_policy_commands(job.get("old_worker"))
                if job['kind'] in ('reload_member', 'configure') and self._retry_candidate is not None:
                    emit(f"局部重载未完成，可修正后执行 policy {job['member'].instance_id} retry", 'error')
                # A failed hot transaction cannot leave divergent configurations.
                if job["kind"] in {"hot_patch", "reset_context"} and self._worker is not None:
                    self._operation_start("unload", worker=self._worker, preserve_state=bool(job.get("returning")) or self._worker in self._command_faults)
                if not static_rejection and not job['cancel'].is_set():
                    self._fail_policy_wait(result['error'])
                self.operations.update(job.get("public_id"), "cancelled" if job["cancel"].is_set() else "failed", error=result["error"])
                continue
            if job["kind"] == "connect":
                self._state = "paused"
                identity = self.config.get("startup_policy")
                if identity:
                    self._state = "initializing"
                    self._operation_start("switch", record=None, target_id=identity, reset=False, old_worker=None)
            elif job["kind"] in ("switch", "reload_member"):
                if job['kind'] == 'switch' and not job.get('committed'):
                    self._worker = result["worker"]
                    self._identity = result["identity"]
                    self.registry.install(result['record'])
                self._pending_config = None
                self._state = "paused"
                self._retry_candidate = None
                if job['kind'] == 'reload_member':
                    self._restore_policy_commands(self._worker)
                if self.runtime:
                    self.runtime.phase = 'PAUSED'
                emit(f"policy {self._identity} ready；等待 resume")
                self._record_configuration()
            elif job["kind"] == "configure":
                # Publication can precede an urgent pause/take in this loop.
                # Never restore execution across a later control intervention.
                current = (not job['cancel'].is_set()
                           and self.buffer.generation == job.get('prepared_generation')
                           and self._policy_control_current(job['origin'], job['epoch']))
                if current:
                    self._state = job['prior_state'] if job.get('action') is None else 'paused'
                elif self._state == 'reloading':
                    self._state = 'paused'
                self._needs_request = True
                if self._policy_wait is not None and self._state == 'running':
                    self._policy_wait.update(generation=self.buffer.generation, epoch=self._control_epoch)
                self._restore_policy_commands(self._worker)
                self._pending_config = None
                emit('policy 配置已生效')
                self._record_configuration()
            elif job["kind"] == "hot_patch":
                if not job.get('committed'):
                    self.registry.commit(result["identity"], result["config"])
                self._restore_policy_commands(self._worker)
                self._pending_config = None
                emit("policy 热配置已生效")
                self._record_configuration()
            elif job["kind"] == "reset_context":
                if job["cancel"].is_set():
                    self._pause()
                    emit("恢复操作已取消；保持暂停")
                    self._return_requested = False
                    self._return_operation = None
                    self.operations.update(job.get("public_id"), "cancelled", error="resume cancelled")
                    continue
                try:
                    self.limiter.reset(self.robot.read_state()["qpos"])
                except Exception as exc:
                    # Context reset succeeded; a missing observation does not
                    # invalidate the loaded model. Stop hold publication until
                    # an explicit take/resume establishes a fresh anchor.
                    self._pause(cancel_wait=False)
                    self._publish_fault = True
                    self._return_requested = False
                    self._return_operation = None
                    self._fail_policy_wait(f"cannot resume without fresh feedback: {exc}")
                    self.operations.update(job.get("public_id"), "failed", error=f"cannot resume without fresh feedback: {exc}")
                    emit(f"恢复失败，保持暂停：{exc}", "error")
                    continue
                self._publish_fault = False
                self._hold = self.limiter.previous.copy()
                self._hold_name = None
                self._state = "running"
                self._return_requested = False
                self._return_operation = None
                self._needs_request = True
                emit("从最新机器人状态继续")
                self._policy_wait_started(job.get('public_id'))
            elif job["kind"] == "unload":
                self._command_faults.discard(job["worker"])
                self._worker = self._identity = None
                self._retry_candidate = None
                if self.runtime:
                    self.runtime.phase = "EMPTY"
                if not job.get("preserve_state"):
                    self._state = "fault"
            receipt = self.status()
            request = job.get('request')
            if request is not None and request.config_edit is not None:
                effect = job.get('action') or 'ordinary'
                receipt['configuration_effect'] = effect
                description = {'ordinary': '普通更新，保留执行上下文',
                               'reset': '已重置执行上下文，保留加载资源；等待 resume',
                               'reinitialize': '已重新初始化策略资源；等待 resume'}[effect]
                emit('配置生效方式：' + description)
            self.operations.update(job.get("public_id"), "succeeded", result=receipt)
            if command_table_output is not None:
                self._emit_command_table(job['public_id'], *command_table_output)

    def _collect_prediction(self):
        request = self._inflight
        if request is None:
            return
        worker = request["worker"]
        draining_context = (self._operation is not None and self._operation.get('in_place')
                            and self._operation.get('prepared') and self._operation['old_worker'] is worker)
        response = (worker.poll_reader_result() if draining_context and not hasattr(worker, 'member_workers')
                    else worker.poll())
        if response is None:
            return
        if response.get("request_id", request["id"]) != request["id"]:
            return
        if draining_context:
            if (response.get('images_released') is True and response.get('request_id') == request['id']
                    and response.get('generation') == request['token'].generation
                    and response.get('config_version') == worker.config_version):
                request['lease'].release()
                self._inflight = None
            return
        if response.get('type') in ('result', 'cancelled') and response.get('images_released') is not True:
            response = {**response, 'type':'error', 'error':'policy response has no image release proof'}
        if response.get('type') == 'cancelled':
            request['lease'].release()
            self._inflight = None
            return
        if response.get("type") == "error":
            if request["worker"] in self._command_faults:
                request["failed"] = True
                return
            if self._stop.is_set():
                request["failed"] = True
                return
            if request.get("failed"):
                return
            request["failed"] = True
            if request["token"].generation != self.buffer.generation and (self.control_owner == "mcp" or self._return_requested):
                self._emit(f"已失效 policy 请求失败：{response.get('error')}", "warning")
                if self._operation and not self._operation["launched"]:
                    public_id = self._operation.get('public_id')
                    self._operation.pop('command_table_output', None)
                    self._operation = None
                    self._return_requested = False
                    self._return_operation = None
                    if self.control_owner == "policy":
                        self._state = "paused"
                    self._fail_policy_wait(response.get('error', 'policy failed'))
                    self.operations.update(public_id, 'failed', error=response.get('error', 'policy failed'))
                if self._operation is None:
                    self._operation_start("unload", worker=request["worker"], preserve_state=True)
                return
            if self._operation is not None and self._operation["kind"] in ("switch", "reload_member", "connector_switch") and self._operation.get("old_worker") is request["worker"]:
                if not self._operation.get('prepared'):
                    self._pause("switching")
                self._emit(f"切换中的旧 policy 请求失败：{response.get('error', response)}", "warning")
                return
            self._pause("fault", cancel_wait=False)
            self._emit(f"policy 错误：{response.get('error', response)}", "error")
            if self._operation is not None:
                self._operation["cancel"].set()
                if not self._operation["launched"]:
                    self._operation.pop('command_table_output', None)
                    self._operation = None
            if self._operation is None:
                self._operation_start("unload", worker=request["worker"])
            self._fail_policy_wait(response.get('error', 'policy failed'))
            return
        if response.get("type") != "result":
            return
        request["lease"].release()
        self._inflight = None
        if request["token"].generation != self.buffer.generation or request["worker"] is not self._worker:
            self._emit(f"丢弃过期请求 {request['id']}")
            return
        record = self.registry.get(self._identity)
        if response.get("generation") != request["token"].generation or response.get("config_version") != record.version:
            raise ValueError("policy 结果代次或配置版本不匹配")
        payload = {"actions": response["actions"], "modes": response["modes"]} if "modes" in response else response["actions"]
        chunk = normalize_actions(payload, record.execution["control_mode"])
        controls = control_requests(response.get("control_requests", []), chain=record.is_chain)
        if controls:
            control = controls[0]
            source = self._identity
            source_info = {'policy_id': self._identity}
            if record.is_chain:
                member, cell = control['source']['policy_id'], control['source']['cell']
                if cell > len(record.members) or record.members[cell - 1].instance_id != member:
                    raise ValueError('control_requests source does not match the active chain')
                source = f'{self._identity} cell {cell} ({member})'
                source_info = dict(control['source'])
            if (self._state != 'running' or self.control_owner != 'policy'
                    or self._stop.is_set() or self._return_requested):
                return
            # A valid pause result acknowledges all readers, but none of its
            # actions may reach the buffer (including async consumption trim).
            matches = self._matches_policy_wait(request)
            self._pause(cancel_wait=control['type'] == 'pause')
            if control['type'] == 'finish':
                report = {'outcome': control['outcome'], 'message': control['message'], 'source': source_info}
                if matches:
                    self._settle_policy_wait('succeeded', result=report, reclaim=True)
                self._emit(f"policy {source} 请求 {request['id']} 结束：{control['outcome']}；{control['message']}")
            else:
                self._emit(f"policy {source} 请求 {request['id']} 暂停：{control['reason']}；保持当前目标，等待 resume")
            return
        accepted = self.buffer.accept(request["token"], chunk)
        self._needs_request = not accepted and self.buffer.submission_state() == 'empty'
        self._last_action_request = ActionRequestSnapshot(
            request['id'], request['token'].generation, self.buffer.last_trim, len(self.buffer.remaining()))
        self._emit(f"请求 {request['id']}：裁剪 {self.buffer.last_trim}，缓存 {self._last_action_request.cached}", 'trace')

    def _snapshot(self):
        observation = self.robot.read_state()
        images = self.robot.read_images()
        published = {}
        for name, (image,timestamp) in images.items():
            if self._pool.publish(name,image,timestamp):
                published[name] = (image,timestamp)
        lease = self._pool.acquire()
        self._observation_serial += 1
        runtime = {'observation_id': f'{self.run_id}:{self.buffer.generation}:{self._observation_serial}',
                   'control_epoch':self.buffer.generation,'consumed_actions':self.buffer.consumed}
        self._recorder.observation(runtime['observation_id'], observation, published, descriptors=lease.descriptors)
        return observation,lease,runtime

    def _collect_observation(self):
        if self._observe_inflight:
            worker,lease,epoch = self._observe_inflight
            response = worker.poll_observation()
            with self._owned_lock:
                closed = worker in self._confirmed_closed
            if closed or (response and response.get('images_released') is True):
                lease.release()
                self._observe_inflight = None
                if (response and response['type']=='observation_error' and self._state=='running'
                        and not self._stop.is_set() and epoch==self.buffer.generation):
                    raise RuntimeError(response['error'])
    def _observe_tick(self):
        self._collect_observation()
        if (self._state!='running' or self._operation is not None or self._worker is None
                or not getattr(self._worker,'supports_observe',False) or self._observe_inflight):
            return None
        try:
            snapshot = self._snapshot()
        except ImagesPending:
            return None
        observation,lease,runtime = snapshot
        record = self.registry.get(self._identity)
        request = {'request_id':self._observation_serial,'generation':self.buffer.generation,
                   'config_version':record.version,'state':observation,'images':lease.descriptors,'runtime':runtime}
        try:
            accepted = self._worker.observe(request)
        except Exception:
            # observe only raises synchronously when responsibility was not
            # transferred into the host client's local send queue.
            lease.release()
            raise
        if accepted:
            self._observe_inflight = (self._worker,lease,self.buffer.generation)
            return snapshot
        lease.release()
        return None

    def _submit_prediction(self, snapshot=None):
        if snapshot and snapshot[2]["consumed_actions"] != self.buffer.consumed:
            snapshot = None
        if snapshot is None:
            try:
                observation,lease,runtime = self._snapshot()
            except ImagesPending:
                return
        else:
            observation,original,runtime = snapshot
            lease = self._pool.retain(original.descriptors)
        token = self.buffer.begin_request()
        self._request_serial += 1
        record = self.registry.get(self._identity)
        request = {"request_id": self._request_serial, "generation": token.generation,
                   "config_version": record.version, "state": observation, "runtime":runtime,
                   "action_chunk": self.buffer.remaining_payload(record.execution["control_mode"]), "images": lease.descriptors}
        try:
            self._worker.submit(request)
        except Exception:
            lease.release()
            raise
        self._inflight = {"id": self._request_serial, "worker": self._worker, "token": token, "lease": lease}
        self._needs_request = False

    def _tick(self):
        self._collect_observation()
        if (self._operation is not None and self._operation['kind'] == 'connector_switch'
                and self._operation.get('prepared') and not self._operation.get('connector_committed')):
            return
        if (self._worker is not None and self._operation is None and not self._stop.is_set()
                and not self._awaiting_member_retry()
                and not getattr(self._worker, 'runnable', self._worker.alive)):
            if self.control_owner == 'policy':
                self._pause('fault', cancel_wait=False)
            else:
                self.buffer.invalidate()
                self._last_action_request = None
            self._emit('policy host 不可运行；清理运行组，等待手动重载', 'error')
            self._operation_start('unload', worker=self._worker, preserve_state=self.control_owner == 'mcp')
            self._fail_policy_wait('policy host 不可运行')
        snapshot = None
        if not self._connected or self._publish_fault or self._stop.is_set():
            return
        if self.control_owner == "mcp" and self._return_requested:
            return
        if self.control_owner == "mcp" and (self.direct.active or self.direct.pending) and not self._stop.is_set():
            self.direct.tick(self.robot.read_state())
            self._hold = self.limiter.previous.copy()
            return
        if self._motion is not None and not self._stop.is_set():
            self.robot.read_state()
            motion = self._motion
            if motion.get('awaiting_publication'):
                self._send_hold()
                return
            action, consumed = self._send_limited(motion["target"], "qpos")
            self._hold = action
            if consumed:
                if getattr(self.robot, 'asynchronous', False):
                    motion['awaiting_publication'] = self._last_submission
                    self._publications[self._last_submission]['motion'] = motion
                else:
                    self._complete_motion(motion)
            return
        if self._state == "running" and self.control_owner == "policy" and not self._stop.is_set():
            self.robot.read_state()
            record = self.registry.get(self._identity)
            synchronous = record.execution['mode'] == 'sync'
            progress = self.buffer.submission_state()
            if synchronous and (progress == 'awaiting_publication'
                                or (self._inflight is not None and progress != 'partial')):
                self._observe_tick()
                return
            due = self._needs_request or self.buffer.current() is None or self.buffer.consumed_since_request >= record.execution["call_every_actions"]
            if due and synchronous and progress != 'partial':
                # A hot patch can delay the callback at a completed target.
                # Complete that decision before starting the next slew target.
                if self._inflight is None and self._operation is None and self._worker is not None:
                    snapshot = self._observe_tick()
                    self._submit_prediction(snapshot)
                return
            action = self.buffer.current()
            if action is not None:
                command, consumed = self._send_limited(action, self.buffer.current_mode())
                self._hold = command
                if getattr(self.robot, 'asynchronous', False):
                    self.buffer.pin_current()
                    if consumed:
                        self._publications[self._last_submission]['buffer_token'] = self.buffer.reserve()
                elif consumed:
                    self.buffer.consume()
            elif self._hold is not None:
                self._send_hold()
            snapshot = self._observe_tick()
            if synchronous and self.buffer.submission_state() in ('partial', 'awaiting_publication'):
                return
            due = self._needs_request or self.buffer.current() is None or self.buffer.consumed_since_request >= record.execution["call_every_actions"]
            if due and self._inflight is None and self._operation is None and self._worker is not None:
                self._submit_prediction(snapshot)
        elif self._hold is not None:
            self._send_hold()

    def _send_limited(self, target, mode="qpos"):
        previous = self.limiter.checkpoint()
        command, consumed = self.limiter.apply(target, mode, self.robot.read_state())
        attempt = self._recorder.control_attempt({'requested':target, 'limited':command,
            'mode':mode, 'owner':self.control_owner, 'generation':self.buffer.generation})
        try:
            if mode == "qpos":
                identity = self.robot.execute(command)
            else:
                identity = self.robot.execute(command, mode=mode)
            self._last_send_at = time.monotonic()
        except Exception as exc:
            self._recorder.event('control', {'event':'control.error', 'attempt_id':attempt, 'error':str(exc)}, priority=True)
            self.limiter.restore(previous)
            self._published_targets.clear()
            self._publish_fault = True
            raise
        if getattr(self.robot, 'asynchronous', False):
            self._register_publication(identity, arm_targets(command, mode), attempt)
        else:
            self._published_targets.update(arm_targets(command, mode))
        return command, consumed

    def _send_direct(self, targets, state):
        previous = self.limiter.checkpoint()
        commands, complete = self.limiter.apply_arms(targets, state)
        attempt = self._recorder.control_attempt({'requested':targets, 'limited':commands,
            'owner':self.control_owner, 'generation':self.buffer.generation})
        try:
            identity = self.robot.execute_arms(commands)
            self._last_send_at = time.monotonic()
        except Exception as exc:
            self._recorder.event('control', {'event':'control.error', 'attempt_id':attempt, 'error':str(exc)}, priority=True)
            self.limiter.restore(previous)
            for arm in commands:
                self._published_targets.pop(arm, None)
            self._publish_fault = True
            raise
        if getattr(self.robot, 'asynchronous', False):
            self._register_publication(identity, commands, attempt)
        else:
            self._published_targets.update(copy.deepcopy(commands))
        self._hold = self.limiter.previous.copy()
        return commands, complete

    def _send_hold(self):
        if self.limiter.modes == ("qpos", "qpos"):
            self._send_limited(self._hold)
        else:
            self._send_direct(self.limiter.commands(), self.robot.read_state())

    def _take(self):
        if self._operation is not None or self._stop.is_set():
            raise RuntimeError("busy: robot connection or lifecycle operation")
        if self.control_owner == "mcp":
            return self.status()
        state = self.robot.read_state() if self._connected else None
        if self._publish_fault and state is not None:
            self.limiter.reset(state["qpos"])
            self._publish_fault = False
        self._pause()
        self.control_owner = "mcp"
        self._control_epoch += 1
        # A return accepted after this take was queued already revoked writes.
        # Processing an older take must not undo that revocation.
        self._return_requested = self._return_operation is not None
        self.direct.targets = self.limiter.commands() if state is not None else {}
        self._emit_command("MCP 已接管；暂停新推理，保留模型")
        return self.status()

    def _return_control(self):
        if self._worker in self._command_faults:
            self._cancel_direct("control returned after policy command failure")
            self.control_owner = "policy"
            self._control_epoch += 1
            self._pause()
            self._return_requested = False
            raise ValueError("control returned, but policy command failed; switch a policy first")
        if self._operation is not None:
            self._return_requested = False
            raise RuntimeError(f"busy: {self._operation['kind']}")
        if self.control_owner != "mcp":
            self._return_requested = False
            return self.status()
        self._cancel_direct("control returned to policy")
        self.control_owner = "policy"
        self._control_epoch += 1
        self._pause("resuming", cancel_wait=False)
        if self._connector_failures or not self._connected or self._retry_candidate is not None or self._worker is None or not getattr(self._worker, "runnable", self._worker.alive):
            self._return_requested = False
            self._state = "paused"
            raise ValueError("control returned, but no runnable policy; switch a policy first")
        self._operation_start("reset_context", wait_inflight=True, worker=self._worker, returning=True)

    def _save_state(self, name):
        qpos = finite_qpos(self.robot.read_state()["qpos"])
        safe = re.sub(r"[^0-9A-Za-z._-]+", "_", name).strip("._-") or f"saved_{self.run_id}"
        path = self.outputs / "state_saves" / f"{safe}.json"
        atomic_json(path, {"name": safe, "saved_at": time.time(), "qpos": qpos.tolist(), "source": "robot-control", "run_id": self.run_id})
        self._cancel_direct("cancelled by save")
        self._pause()
        self.limiter.reset(qpos)
        self._hold, self._hold_name = qpos.copy(), safe
        self._emit_command(f"保存并保持：{path}")

    def _load_state(self, name):
        candidate = Path(name).expanduser()
        path = candidate if candidate.is_absolute() else self.outputs / "state_saves" / f"{name}.json"
        if candidate.suffix == ".json" and not candidate.is_absolute():
            path = self.root / candidate
        with path.open(encoding="utf-8") as stream:
            payload = json.load(stream)
        target = finite_qpos(payload["qpos"])
        self._cancel_direct("cancelled by load")
        self._pause()
        self._motion = {"target": target, "source": payload.get("name") or path.stem}
        self._state = "moving"

    def _command(self, line):
        if line == "take":
            return self._take()
        if line == "return":
            return self._return_control()
        if line in {"exit", "quit"}:
            self._cancel_direct("controller shutting down")
            self._stop.set()
            if self._operation is not None:
                self._operation["cancel"].set()
                if not self._operation["launched"]:
                    self._operation.pop('command_table_output', None)
                    self._operation = None
            self._pause("stopping")
            self._emit_command("退出：停止新操作，等待所有 policy 释放")
            return
        if line == "pause":
            self._cancel_direct("cancelled by pause")
            if self._operation is not None:
                self._operation["cancel"].set()
                if not self._operation["launched"]:
                    self.operations.update(self._operation.get("public_id"), "cancelled", error="cancelled by pause")
                    self._restore_policy_commands(self._operation.get("worker"))
                    self._operation.pop('command_table_output', None)
                    self._operation = None
                    self._pending_config = None
                    self._return_requested = False
                    self._return_operation = None
            self._pause()
            self._emit_command("已暂停，保持当前目标")
            return
        if self._stop.is_set() or self._shutdown_failed:
            self._emit_command("退出清理期间只接受 exit", "warning")
            return
        words = shlex.split(line)
        if words and words[0] == 'connector':
            return self._connector_command(words[1:])
        if self._connector_failures and words and words[0] in ('resume', 'reset', 'r', 'load', 'save'):
            raise RuntimeError('请先冷重试失败的 policy 实例：' + ', '.join(self._connector_failures))
        words = shlex.split(line)
        command, args = words[0], words[1:]
        if command == "help" and not args:
            value = builtin_help()
            self._emit_command(help_text(value), help_document=value)
        elif command == "status" and not args:
            self._emit_query_result('status', self.status())
        elif command == "policies" and not args:
            self._emit_query_result('policies', self.registry.list())
        elif command == "show" and args in [["rc"], ["policy"]]:
            config = self.config if args[0] == "rc" else self.registry.get(self._identity).config
            self._emit_command(json.dumps(public_value(config), ensure_ascii=False, indent=2))
        elif command == "save-config" and args in [["rc"], ["policy"]]:
            if args[0] == "rc":
                save_yaml_config(self.config_path, self.config)
                path = self.config_path
            else:
                path = self.registry.save(self._identity)
            self._emit_command(f"有效配置已保存：{path}")
        elif command == "resume" and not args:
            if self.control_owner == "mcp":
                raise PermissionError("MCP 持有控制权；请先使用 mcp return")
            if not self._connected or self._retry_candidate is not None or self._worker is None or not getattr(self._worker, "runnable", self._worker.alive):
                if self._retry_candidate:
                    raise ValueError(f"请先执行 policy {self._retry_candidate.member.instance_id} retry")
                raise ValueError("没有可运行的 policy，请 switch")
            self._cancel_direct("cancelled by resume")
            self._pause("resuming")
            self._operation_start("reset_context", wait_inflight=True, worker=self._worker)
        elif command in {"reset", "r"} and not args:
            self._cancel_direct("cancelled by reset")
            self._pause()
            self._publish_fault = False
            self._motion = {"target": finite_qpos(self.config["reset_qpos"]), "source": "reset"}
            self._state = "moving"
        elif command == "save" and len(args) == 1:
            self._save_state(args[0])
        elif command == "load" and len(args) == 1:
            self._load_state(args[0])
        elif command == "switch" and len(args) in {1, 2}:
            if len(args) == 2 and args[1] != "--reset":
                raise ValueError("用法：switch <id> [--reset]")
            self._operation_start("switch", record=None, target_id=args[0], old_worker=self._worker, reset=len(args) == 2)
        else:
            raise ValueError("未知命令或参数，请输入 help")

    def _finish_shutdown(self):
        self.policy_monitors.close(wait=False)
        self._clear_finished_lease()
        if self._observe_inflight and not self._shutdown_failed:
            self._observe_inflight[1].release()
            self._observe_inflight = None
        self._external.close()
        self._emit("policy 清理已结束；保留未确认读者的图像和恢复记录" if self._shutdown_failed else "policy 已退出；回收共享内存和通信")
        self._connected = False
        try:
            self._pool.close()
            if self.runtime and self._pool._bytes == 0:
                self.runtime.forget_pool(self._pool.pool_id)
        except OSError as exc:
            self.exit_code = 1
            self._emit(f"图像资源回收错误：{exc}", "error")
        self._jobs.put(None)
        self._manager.join(timeout=5)
        if self._manager.is_alive():
            raise RuntimeError("生命周期线程没有退出")
        if self.runtime:
            self.runtime.release()
        self._state = "stopped"
        self.operations.cancel_pending("controller stopped", exclude=(*self._exit_operations, *self._tui_batches.batches))
        self._emit('控制与 policy 已退出；等待日志写入收尾')
        final_entry = {'event':'rc.exit', 'time':format_log_time(time.time()), 'level':'info',
            'exit_code':self.exit_code, 'exit_operations':list(self._exit_operations),
            'message':("安全退出完成，退出码 0" if self.exit_code == 0 else
                       f"退出完成，存在强制终止或清理错误，退出码 {self.exit_code}")}
        if self._recorder.shutdown(self._cleanup_deadline(), final_entry=final_entry):
            self.exit_code = 1
        # Finish priority shutdown receipts after the cancellation batch, so
        # a full queue cannot immediately prune the final exit acknowledgement.
        for identity in self._exit_operations:
            self.operations.update(identity, "succeeded" if self.exit_code == 0 else "failed", result={"exit_code": self.exit_code})
        self._tui_batches.finish_shutdown()
        self._emit("安全退出完成，退出码 0" if self.exit_code == 0 else f"退出完成，存在强制终止或清理错误，退出码 {self.exit_code}")
        self.done.set()

    def _control_loop(self):
        with self._lock:
            self._state = "connecting"
            self._operation_start("connect")
        period = 1 / self.config["frequency_hz"]
        deadline = time.monotonic()
        while not self.done.is_set():
            with self._lock:
                try:
                    self._tui_batches.poll()
                    try:
                        urgent = self._urgent.get_nowait()
                    except queue.Empty:
                        urgent = None
                    if urgent:
                        self._dispatch(urgent)
                    self._collect_publications()
                    self._poll_policy_commands()
                    self._handle_internal()
                    self._unload_command_fault()
                    if self.done.is_set():
                        break
                    self._clear_finished_lease()
                    self._collect_prediction()
                    self._launch_deferred()
                    if self._stop.is_set():
                        if self._state != "stopping":
                            self._pause("stopping")
                        if self._operation is None:
                            self._operation_start("shutdown")
                    elif self._operation is None and not self._return_requested:
                        try:
                            command = self._commands.get_nowait()
                        except queue.Empty:
                            command = None
                        if command:
                            self._dispatch(command)
                    now = time.monotonic()
                    if now >= deadline:
                        self._tick()
                        deadline = next_deadline(deadline, now, self._last_send_at, period)
                        if deadline < time.monotonic():
                            deadline = time.monotonic() + period
                except Exception as exc:
                    self._cancel_direct(f"control failure: {exc}")
                    self._pause("fault", cancel_wait=False)
                    self._emit(f"控制错误：{exc}", "error")
                    if self._stop.is_set():
                        self.exit_code = 1
                    if self._worker is not None and self._operation is None and self.control_owner != "mcp":
                        self._operation_start("unload", worker=self._worker)
                    self._fail_policy_wait(exc)
                    deadline = time.monotonic() + period
            self._wake.wait(max(0, deadline - time.monotonic()))
            self._wake.clear()
