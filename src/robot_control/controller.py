"""Single-writer robot loop with a separate, cancellable lifecycle manager."""
from __future__ import annotations

from .secret_config import public_value, public_message, reject_secret_edit

import copy
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
from pathlib import Path

from rc_policy_sdk.images import ImagePool

from .external import ExternalResources
from .log_format import format_log_time
from .buffer import ActionBuffer
from .config import PolicyRegistry, atomic_json, changed_config, dotted_get, load_rc, validate_entry
from .process import PolicyProcess
from .robots import make_robot
from .safety import ModeLimiter, finite_qpos, arm_targets
from .policy_commands import PolicyCommandSupport
from .chain_management import ChainManagement
from rc_policy_sdk.commands import BUILTIN_COMMANDS
from .operations import ControlRequest, Operations
from .direct import DirectMotion
from rc_policy_sdk.actions import normalize_actions
from rc_policy_sdk.control import control_requests


HELP = "\n".join([
    "set rc.path <JSON> / set policy.path <JSON>；show rc|policy；save-config rc / save-config policy [policy-id]",
    "reset/r；save <名称>；load <名称或路径>；pause；resume",
    "policies；switch <id> [--reset]；status；help；exit/quit",
    "take：MCP 接管并暂停推理；return：交还并恢复 policy（接管时 resume 同义）",
    '示例：set policy.params.delay_s 0.2；set policy.execution.mode "async"',
])


from .policy_monitors import PolicyMonitors


class RobotController(PolicyCommandSupport, ChainManagement):
    def __init__(self, config_path, *, robot=None, process_factory=PolicyProcess, runtime_manager=None):
        self.config_path = Path(config_path).resolve()
        self.config = load_rc(self.config_path)
        self.root = self.config_path.parent
        self.registry = PolicyRegistry(self.root / self.config["policy_directory"])
        self.robot = robot or make_robot(self.config)
        self.process_factory = process_factory
        self.events = queue.Queue(maxsize=10000)
        self.done = threading.Event()
        self.exit_code = 0
        self.buffer = ActionBuffer()
        self.limiter = ModeLimiter(self.config["safety"], self.config["frequency_hz"])
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
        self._exit_operations = []
        self._publish_fault = False
        self._published_targets = {}
        self.operations = Operations()
        self.direct = DirectMotion(self.config.get("direct_control", {}), self.config["frequency_hz"],
                                   self.operations, self._send_direct, self.robot)
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
        self._recovery = (self.runtime.state_dir.parent if self.runtime else self.outputs) / 'recovery'
        self._external = ExternalResources(self._pool, self._recovery / f'{self.run_id}.json',
                                           deployment_id=self.runtime.context['deployment_id'] if self.runtime else None)
        self._observation_serial = 0
        self._observe_inflight = None
        self._init_policy_commands()
        self._init_chain_editor()
        self._log_stream = None
        self._log_lock = threading.Lock()

    def _emit(self, message, level="info", *, record_history=True):
        entry = {"time": format_log_time(time.time()), "level": level, "message": public_message(message)}
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
        with self._log_lock:
            if self._log_stream is not None:
                try:
                    self._log_stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    self._log_stream.flush()
                except OSError as exc:
                    self.exit_code = 1
                    try:
                        self._log_stream.close()
                    except OSError:
                        pass
                    self._log_stream = None
                    try:
                        self.events.put_nowait({"time": format_log_time(time.time()), "level": "error", "message": f"日志文件写入失败：{exc}"})
                    except queue.Full:
                        pass

    def start(self):
        with self._lock:
            if self._thread is not None:
                return
            try:
                directory = self.outputs / "logs"
                directory.mkdir(parents=True, exist_ok=True)
                self._log_stream = (directory / f"rc_{self.run_id}.jsonl").open("a", encoding="utf-8")
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
                    if self._log_stream is not None:
                        self._log_stream.close()
                        self._log_stream = None
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
    def is_read_command(line):
        try:
            words = shlex.split(line)
        except ValueError:
            return False
        return words in (["status"], ["policies"], ["help"], ["show", "rc"], ["show", "policy"])

    def query(self, line):
        with self._lock:
            words = shlex.split(line)
            if words == ["status"]:
                return self.status()
            if words == ["policies"]:
                return self.registry.list()
            if words == ["help"]:
                return HELP
            if words == ["show", "rc"]:
                return public_value(self.config)
            if words == ["show", "policy"] and self._identity is not None:
                return public_value(self.registry.get(self._identity).config)
            raise ValueError("unknown read command or no active policy")

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

    def submit(self, line: str, *, origin="tui"):
        reject_secret_edit(line)
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
            try:
                words = shlex.split(command)
            except ValueError:
                words = []
            if origin == "mcp" and words and words[0] in ("mcp", "camera", "monitor", "manage-policy", "snapshot", "policy"):
                raise PermissionError(f"{words[0]} is a local-only terminal command")
            if origin == "mcp" and words and words[0] not in BUILTIN_COMMANDS:
                raise PermissionError("policy extension commands are local-only")
            local_help = origin == "tui" and words and words[0] == "help" and len(words) in (1, 2)
            readonly = self.is_read_command(command) or local_help
            if command == "return" and self._return_operation is not None:
                return self._return_operation
            if origin == "mcp" and not readonly and command not in ("take", "return"):
                self._require_mcp()
            if origin == "mcp" and self._operation is not None and not readonly and command not in ("pause", "exit", "quit"):
                raise RuntimeError(f"busy: {self._operation['kind']}")
            identity = self.operations.create(command)
            if readonly:
                try:
                    result = self.local_help(words[1] if len(words) == 2 else None) if local_help else self.query(command)
                    self.operations.update(identity, "succeeded", result=result)
                    self._emit(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
                except Exception as exc:
                    self.operations.update(identity, "failed", error=exc)
                    self._emit(f"命令失败：{exc}", "error")
                return identity
            if origin == "tui" and words:
                try:
                    custom = self._policy_request(identity, command, words[0])
                except Exception as exc:
                    self.operations.update(identity, "failed", error=exc)
                    self._emit(f"policy command [{identity}] failed: {exc}", "error")
                    return identity
                if custom is not None:
                    self._commands.put(custom)
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
            if command == "return" or (command == "resume" and self.control_owner == "mcp"):
                self._return_requested = True
                self._return_operation = identity
            request = ControlRequest(identity, command, origin, self._control_epoch)
            urgent = command in {"exit", "quit", "pause", "take", "return"} or (command == "resume" and self.control_owner == "mcp")
            target = self._urgent if urgent else self._commands
            target.put(request)
            self._wake.set()
            return identity

    def submit_direct(self, steps, *, clear_queue=False):
        with self._lock:
            self._require_mcp()
            if not self._connected or self._operation is not None or self._motion is not None or self._stop.is_set():
                raise RuntimeError("busy: robot connection or lifecycle operation")
            if self._publish_fault:
                raise RuntimeError("command publication fault; repair the connection and reset or return before moving")
            if type(clear_queue) is not bool:
                raise ValueError("clear_queue must be a boolean")
            identity = self.operations.create("direct motion")
            self._commands.put(ControlRequest(identity, "direct", "mcp", self._control_epoch, tuple(steps), clear_queue))
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
        try:
            if request.origin == "mcp" and request.line not in ("take", "return"):
                if request.epoch != self._control_epoch:
                    self.operations.update(identity, "cancelled", error="control ownership changed")
                    return
                if request.line != "resume":
                    self._require_mcp()
            if request.policy_name is not None:
                self._dispatch_policy_command(request)
                return
            self.operations.update(identity, "running")
            self._dispatch_id = identity
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
            result = self._command(request.line)
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
            self.operations.update(identity, "failed", error=exc)
            self._emit(f"命令失败：{exc}", "error")
            if identity == self._return_operation:
                self._return_operation = None
                self._return_requested = False
        finally:
            self._dispatch_id = None

    def status(self):
        with self._lock:
            record = self.registry.get(self._identity) if self._identity is not None else None
            return {
                "state": self._state, "policy": self._identity,
                "runtime": self.runtime.snapshot() if self.runtime else None,
                "queue_remaining": len(self.buffer.remaining()),
                "in_flight": self._inflight is not None,
                "generation": self.buffer.generation, "consumed": self.buffer.consumed,
                "consumed_since_request": self.buffer.consumed_since_request,
                "config_version": record.version if record else 0,
                "pending_config": public_value(self._pending_config),
                "hold_name": self._hold_name, "exit_code": self.exit_code,
                "control_owner": self.control_owner, "control_epoch": self._control_epoch,
                "returning": self._return_requested, "direct": self.direct.progress(),
            }

    def capture_snapshot(self):
        """Capture cached feedback and retain immutable frames for background I/O."""
        from .telemetry import CAMERAS

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
            return members, self._operation is not None or self._stop.is_set()

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

    def _pause(self, state="paused"):
        if self._worker is not None and hasattr(self._worker, 'cancel_inference'):
            self._worker.cancel_inference()
        self.buffer.invalidate()
        self._needs_request = True
        if self._motion is not None:
            self.operations.update(self._motion.get("public_id"), "cancelled", error=f"motion interrupted: {state}")
        self._motion = None
        if self.limiter.previous is not None:
            self._hold = self.limiter.previous.copy()
        self._state = state

    def _operation_start(self, kind, *, wait_inflight=False, **payload):
        if self._operation is not None:
            raise RuntimeError("生命周期操作正在执行")
        if kind in ("switch", "reset_context", "hot_patch", "unload", "shutdown"):
            self._gate_policy_commands(payload.get("worker", payload.get("old_worker", self._worker)))
        self._job_serial += 1
        self.policy_monitors.invalidate()
        job = {"id": self._job_serial, "kind": kind, "cancel": threading.Event(),
               "public_id": self._dispatch_id, **payload}
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

    def _close_worker(self, worker, deadline=None):
        if worker is None:
            return
        from .runtime_manager import remaining, BlockedCleanup
        deadline = deadline if deadline is not None else self._cleanup_deadline()
        self._external.begin_close(worker, deadline=deadline)
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
                for handle in self._runtime_groups.get(worker, ()):
                    handle.cleanup_forced = True
                    self.runtime._save(handle)
        if self.runtime:
            self.runtime.phase = 'STOPPING'
            failures = []
            for handle in self._runtime_groups.get(worker, ()):
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
        try:
            remote_forced = self._external.close_worker(worker, deadline=deadline)
        except Exception as exc:
            raise BlockedCleanup(str(exc)) from exc
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
                if self.runtime:
                    from .runtime_manager import BlockedCleanup, BlockedInfrastructure
                    if isinstance(exc, BlockedInfrastructure):
                        self.runtime.phase = 'BLOCKED_INFRA'
                    elif isinstance(exc, BlockedCleanup):
                        self.runtime.phase = 'BLOCKED_CLEANUP'
                    elif job.get('prepared') or job['kind'] == 'switch' and self._worker is None:
                        self.runtime.phase = 'FAULT_EMPTY'
                    self.runtime.error = str(exc)
            self._results.put(result)
            self._wake.set()

    def _prepare_switch(self, job):
        cancel = job['cancel']
        prepared = threading.Event()
        self._results.put({'type':'switch_prepare', 'id':job['id'], 'complete':prepared})
        self._wake.set()
        while not prepared.wait(.05):
            if cancel.is_set():
                raise RuntimeError('policy switch cancelled')
        if cancel.is_set():
            raise RuntimeError('policy switch cancelled')

    def _run_job(self, job):
        kind, cancel = job["kind"], job["cancel"]
        if kind in ('chain_open', 'chain_save'):
            return self._run_chain_editor_job(job)
        if kind == 'save_member':
            return {'saved_path': str(self.registry.save(job['identity']))}
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
            self.robot.connect(cancel_event=cancel)
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
                    self.runtime.cleanup_owned(deadline)
                except Exception as exc:
                    failures.append(str(exc))
            if failures:
                from .runtime_manager import BlockedCleanup
                raise BlockedCleanup("；".join(failures))
            return {}
        if kind == "unload":
            self._close_worker(job["worker"])
            return {}
        if kind == "close_robot":
            self.robot.close()
            return {}
        if kind == "reset_context":
            job["worker"].reset_context(cancel_event=cancel)
            return {}
        if kind == "hot_patch":
            job["worker"].update_config(job["patch"], job["version"], cancel_event=cancel)
            return {"config": job["config"], "identity": job["identity"]}
        if kind != "switch":
            raise ValueError(f"未知生命周期操作：{kind}")
        if not self._connected:
            raise RuntimeError('robot/recovery is not ready; restart after successful cleanup')
        record = job['record']
        if record is None:
            record = (self.registry.snapshot(job['target_id']) if job.get('target_id')
                      else self.registry.propose_chain(job['chain_ids']))
        specs = []
        if self.runtime:
            if self.runtime.phase in ('BLOCKED_CLEANUP', 'BLOCKED_INFRA'):
                raise RuntimeError('runtime is blocked; cleanup/restart is required before switching')
            self.runtime.target = record.config['id']
            self.runtime.phase = 'PREFLIGHT'
            self.runtime.error = None
        from .runtime_manager import BlockedInfrastructure
        try:
            for member in record.members if record.is_chain else (record,):
                if cancel.is_set():
                    raise RuntimeError('policy preflight cancelled')
                validate_entry(member.directory)
                if self.runtime:
                    specs.append(self.runtime.prepare_spec(member, deadline=time.monotonic() + 20, cancel=cancel))
        except BlockedInfrastructure:
            self._prepare_switch(job)
            self._close_worker(job.get('old_worker'))
            self._results.put({'type':'switch_empty', 'id':job['id']})
            self._wake.set()
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
        worker_options = dict(
            init_timeout_s=timeouts.get("initialize_s", 600),
            inference_timeout_s=timeouts.get("inference_s", 60),
            release_timeout_s=timeouts.get("release_s", 30),
            on_log=lambda message: self._emit(f"[{record.config['id']}] {message}"),
        )
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
                worker_options['init_timeout_s'] = max(.001, deadline-time.monotonic())
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
                    job.get('version', record.version), endpoint=endpoints.get(record.config['id']),
                    exit_authority=self.runtime.authority if self.runtime else None, **worker_options)
            worker.resources = self._external
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
                    self.runtime.cleanup_owned(cleanup_deadline)
                except Exception as exc:
                    cleanup_error = exc
            if cleanup_error:
                from .runtime_manager import BlockedCleanup
                raise BlockedCleanup(str(cleanup_error)) from cleanup_error
            raise
        return {"worker": worker, "identity": record.config["id"], "config": record.config,
                "record": record, "commit": job.get("commit", False), 'member_commit': job.get('member_commit')}

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
            if result['type'] == 'switch_empty':
                self._worker = self._identity = None
                self._clear_finished_lease()
                continue
            if result["type"] == "switch_initializing":
                if not self._stop.is_set() and not job["cancel"].is_set():
                    self._state = "initializing"
                continue
            if result["type"] == "switch_prepare":
                if self._stop.is_set() or job["cancel"].is_set():
                    continue
                self._pause("reloading" if job.get("commit") else "switching")
                job['prepared'] = True
                result["complete"].set()
                continue
            if result["type"] == "switch_reset":
                if self._stop.is_set() or job["cancel"].is_set():
                    continue
                self._motion = {"target": finite_qpos(self.config["reset_qpos"]), "complete": result["complete"], "source": "reset"}
                self._state = "moving"
                continue
            self._operation = None
            self._clear_finished_lease()
            if job['kind'] in ('chain_open', 'chain_save', 'save_member'):
                if result.get('error') or job['cancel'].is_set():
                    error = result.get('error', 'editor operation cancelled')
                    self.operations.update(job.get('public_id'), 'failed', error=error)
                    self._emit(error, 'error')
                else:
                    self.operations.update(job.get('public_id'), 'succeeded', result=result.get('editor', result.get('saved_path')))
                continue
            if result.get("connected"):
                self._connected = True
                self.limiter.reset(self.robot.read_state()["qpos"])
                self._hold = self.limiter.previous.copy()
            if job["kind"] == "shutdown":
                if result.get("error"):
                    self.exit_code = 1
                    self._shutdown_failed = True
                    self._emit(f"退出清理未完成：{result['error']}；保留恢复账本和未确认图像，宿主 down 可继续收尾", 'error')
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
                    self._emit(f"机器人关闭错误：{result['error']}", "error")
                self._finish_shutdown()
                continue
            if self._stop.is_set():
                self.operations.update(job.get("public_id"), "cancelled", error="controller is shutting down")
                continue
            if result.get("error"):
                self._pending_config = None
                editor_preflight = job.get('edit_token') is not None and not job.get('prepared')
                static_rejection = job['kind'] == 'switch' and not job.get('prepared') and self._worker is not None
                if not static_rejection:
                    self._pause("paused" if job["cancel"].is_set() or job.get("returning") or editor_preflight else "fault")
                self._emit(result["error"], "warning" if job["cancel"].is_set() else "error")
                self.operations.update(job.get("public_id"), "cancelled" if job["cancel"].is_set() else "failed", error=result["error"])
                self._return_requested = False
                self._return_operation = None
                if job["kind"] == "switch":
                    self._restore_policy_commands(job.get("old_worker"))
                # A failed hot transaction cannot leave divergent configurations.
                if job["kind"] in {"hot_patch", "reset_context"} and self._worker is not None:
                    self._operation_start("unload", worker=self._worker, preserve_state=bool(job.get("returning")) or self._worker in self._command_faults)
                continue
            if job["kind"] == "connect":
                self._state = "paused"
                identity = self.config.get("startup_policy")
                if identity:
                    self._state = "initializing"
                    self._operation_start("switch", record=None, target_id=identity, reset=False, old_worker=None)
            elif job["kind"] == "switch":
                self._worker = result["worker"]
                self._identity = result["identity"]
                self.registry.install(result['record'])
                self._pending_config = None
                self._state = "paused"
                if self.runtime:
                    self.runtime.phase = 'PAUSED'
                self._emit(f"policy {self._identity} ready；等待 resume")
            elif job["kind"] == "hot_patch":
                self.registry.commit(result["identity"], result["config"])
                self._pending_config = None
                self._emit("policy 热配置已生效")
            elif job["kind"] == "reset_context":
                if job["cancel"].is_set():
                    self._pause()
                    self._emit("恢复操作已取消；保持暂停")
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
                    self._pause()
                    self._publish_fault = True
                    self._return_requested = False
                    self._return_operation = None
                    self.operations.update(job.get("public_id"), "failed", error=f"cannot resume without fresh feedback: {exc}")
                    self._emit(f"恢复失败，保持暂停：{exc}", "error")
                    continue
                self._publish_fault = False
                self._hold = self.limiter.previous.copy()
                self._hold_name = None
                self._state = "running"
                self._return_requested = False
                self._return_operation = None
                self._needs_request = True
                self._emit("从最新机器人状态继续")
            elif job["kind"] == "unload":
                self._command_faults.discard(job["worker"])
                self._worker = self._identity = None
                if self.runtime:
                    self.runtime.phase = "EMPTY"
                if not job.get("preserve_state"):
                    self._state = "fault"
            self.operations.update(job.get("public_id"), "succeeded", result=self.status())

    def _collect_prediction(self):
        request = self._inflight
        if request is None:
            return
        response = request["worker"].poll()
        if response is None:
            return
        if response.get("request_id", request["id"]) != request["id"]:
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
                    self.operations.update(self._operation.get("public_id"), "failed", error=response.get("error", "policy failed"))
                    self._operation = None
                    self._return_requested = False
                    self._return_operation = None
                    if self.control_owner == "policy":
                        self._state = "paused"
                if self._operation is None:
                    self._operation_start("unload", worker=request["worker"], preserve_state=True)
                return
            if self._operation is not None and self._operation["kind"] == "switch" and self._operation.get("old_worker") is request["worker"]:
                self._pause("switching")
                self._emit(f"切换中的旧 policy 请求失败：{response.get('error', response)}", "warning")
                return
            self._pause("fault")
            self._emit(f"policy 错误：{response.get('error', response)}", "error")
            if self._operation is not None:
                self._operation["cancel"].set()
                if not self._operation["launched"]:
                    self._operation = None
            if self._operation is None:
                self._operation_start("unload", worker=request["worker"])
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
            pause = controls[0]
            source = self._identity
            if record.is_chain:
                member, cell = pause['source']['policy_id'], pause['source']['cell']
                if cell > len(record.config['policies']) or record.config['policies'][cell - 1] != member:
                    raise ValueError('control_requests source does not match the active chain')
                source = f'{self._identity} cell {cell} ({member})'
            if (self._state != 'running' or self.control_owner != 'policy'
                    or self._stop.is_set() or self._return_requested):
                return
            # A valid pause result acknowledges all readers, but none of its
            # actions may reach the buffer (including async consumption trim).
            self._pause()
            self._emit(f"policy {source} 请求 {request['id']} 暂停：{pause['reason']}；保持当前目标，等待 resume")
            return
        accepted = self.buffer.accept(request["token"], chunk)
        self._needs_request = not accepted
        self._emit(f"请求 {request['id']}：裁剪 {self.buffer.last_trim}，缓存 {len(self.buffer.remaining())}")

    def _snapshot(self):
        observation = self.robot.read_state()
        for name, (image,timestamp) in self.robot.read_images().items():
            self._pool.publish(name,image,timestamp)
        lease = self._pool.acquire()
        self._observation_serial += 1
        runtime = {'observation_id': f'{self.run_id}:{self.buffer.generation}:{self._observation_serial}',
                   'control_epoch':self.buffer.generation,'consumed_actions':self.buffer.consumed}
        return observation,lease,runtime

    def _observe_tick(self):
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
        if (self._state!='running' or self._operation is not None or self._worker is None
                or not getattr(self._worker,'supports_observe',False) or self._observe_inflight):
            return None
        snapshot = self._snapshot()
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
            observation,lease,runtime = self._snapshot()
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
        if (self._worker is not None and self._operation is None and not self._stop.is_set()
                and not getattr(self._worker, 'runnable', self._worker.alive)):
            if self.control_owner == 'policy':
                self._pause('fault')
            else:
                self.buffer.invalidate()
            self._emit('policy host 不可运行；清理运行组，等待手动重载', 'error')
            self._operation_start('unload', worker=self._worker, preserve_state=self.control_owner == 'mcp')
        snapshot = self._observe_tick()
        if not self._connected or self._publish_fault:
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
            action, consumed = self._send_limited(motion["target"], "qpos")
            self._hold = action
            if consumed:
                self._motion = None
                self._hold_name = motion["source"]
                self._state = "paused"
                if motion.get("complete") is not None:
                    motion["complete"].set()
                self._emit(f"到达 {motion['source']} 指令目标；等待 resume")
                self.operations.update(motion.get("public_id"), "succeeded", result=self.status())
            return
        if self._state == "running" and self.control_owner == "policy" and not self._stop.is_set():
            self.robot.read_state()
            record = self.registry.get(self._identity)
            if self._inflight is not None and record.execution["mode"] == "sync":
                return
            due = self._needs_request or self.buffer.current() is None or self.buffer.consumed_since_request >= record.execution["call_every_actions"]
            if due and record.execution["mode"] == "sync":
                # A hot patch can delay the callback at a completed target.
                # Complete that decision before starting the next slew target.
                if self._inflight is None and self._operation is None and self._worker is not None:
                    self._submit_prediction(snapshot)
                return
            action = self.buffer.current()
            if action is not None:
                command, consumed = self._send_limited(action, self.buffer.current_mode())
                self._hold = command
                if consumed:
                    self.buffer.consume()
            elif self._hold is not None:
                self._send_hold()
            due = self._needs_request or self.buffer.current() is None or self.buffer.consumed_since_request >= record.execution["call_every_actions"]
            if due and self._inflight is None and self._operation is None and self._worker is not None:
                self._submit_prediction(snapshot)
        elif self._hold is not None:
            self._send_hold()

    def _send_limited(self, target, mode="qpos"):
        previous = self.limiter.checkpoint()
        command, consumed = self.limiter.apply(target, mode, self.robot.read_state())
        try:
            if mode == "qpos":
                self.robot.execute(command)
            else:
                self.robot.execute(command, mode=mode)
        except Exception:
            self.limiter.restore(previous)
            self._published_targets.clear()
            self._publish_fault = True
            raise
        self._published_targets.update(arm_targets(command, mode))
        return command, consumed

    def _send_direct(self, targets, state):
        previous = self.limiter.checkpoint()
        commands, complete = self.limiter.apply_arms(targets, state)
        try:
            self.robot.execute_arms(commands)
        except Exception:
            self.limiter.restore(previous)
            for arm in commands:
                self._published_targets.pop(arm, None)
            self._publish_fault = True
            raise
        self._published_targets.update(copy.deepcopy(commands))
        self._hold = self.limiter.previous.copy()
        return commands, complete

    def _send_hold(self):
        if self.limiter.modes == ("qpos", "qpos"):
            self._send_limited(self._hold)
        else:
            self._send_direct(self.limiter.commands(), self.robot.read_state())

    def _take(self):
        if not self._connected or self._operation is not None or self._stop.is_set():
            raise RuntimeError("busy: robot connection or lifecycle operation")
        if self.control_owner == "mcp":
            return self.status()
        state = self.robot.read_state()
        if self._publish_fault:
            self.limiter.reset(state["qpos"])
            self._publish_fault = False
        self._pause()
        self.control_owner = "mcp"
        self._control_epoch += 1
        # A return accepted after this take was queued already revoked writes.
        # Processing an older take must not undo that revocation.
        self._return_requested = self._return_operation is not None
        self.direct.targets = self.limiter.commands()
        self._emit("MCP 已接管；暂停新推理，保留模型")
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
        self._pause("resuming")
        if self._worker is None or not getattr(self._worker, "runnable", self._worker.alive):
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
        self._emit(f"保存并保持：{path}")

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
        if line == "return" or (line == "resume" and self.control_owner == "mcp"):
            return self._return_control()
        if line in {"exit", "quit"}:
            self._cancel_direct("controller shutting down")
            self._stop.set()
            if self._operation is not None:
                self._operation["cancel"].set()
                if not self._operation["launched"]:
                    self._operation = None
            self._pause("stopping")
            self._emit("退出：停止新操作，等待所有 policy 释放")
            return
        if line == "pause":
            self._cancel_direct("cancelled by pause")
            if self._operation is not None:
                self._operation["cancel"].set()
                if not self._operation["launched"]:
                    self.operations.update(self._operation.get("public_id"), "cancelled", error="cancelled by pause")
                    self._restore_policy_commands(self._operation.get("worker"))
                    self._operation = None
                    self._pending_config = None
                    self._return_requested = False
                    self._return_operation = None
            self._pause()
            self._emit("已暂停，保持当前目标")
            return
        if self._stop.is_set() or self._shutdown_failed:
            self._emit("退出清理期间只接受 exit", "warning")
            return
        parts = line.split(maxsplit=2)
        if parts[0] == "set":
            if len(parts) != 3:
                raise ValueError("用法：set policy.xxx.xxx <JSON值>")
            scope, separator, path = parts[1].partition(".")
            if not separator:
                raise ValueError("配置路径必须以 rc. 或 policy. 开头")
            if scope == "rc":
                dotted_get(self.config, path)
                raise ValueError(f"rc.{path} 是冷参数，请编辑 rc.json 后重启")
            if scope != "policy" or self._identity is None or self._worker is None or not getattr(self._worker, "runnable", self._worker.alive):
                raise ValueError("需要已激活的 policy")
            value = json.loads(parts[2])
            record = self.registry.get(self._identity)
            if record.is_chain:
                raise ValueError('链模式不能 set policy.*；请编辑成员原配置或使用其命令')
            config, hot = changed_config(record.config, path, value)
            self._pending_config = {"path": path, "value": value, "hot": hot}
            if hot:
                self._operation_start("hot_patch", wait_inflight=True, worker=self._worker, patch={path: value}, version=record.version + 1, config=config, identity=self._identity)
            else:
                from .config import PolicyRecord
                proposed = PolicyRecord(record.directory, config, record.version + 1)
                self._invalidate_policy_editor()
                self._operation_start("switch", record=proposed, old_worker=self._worker, reset=False, commit=True, version=proposed.version)
            return
        words = shlex.split(line)
        command, args = words[0], words[1:]
        if command == "help" and not args:
            self._emit(HELP)
        elif command == "status" and not args:
            self._emit(json.dumps(self.status(), ensure_ascii=False))
        elif command == "policies" and not args:
            self._emit(json.dumps(self.registry.list(), ensure_ascii=False))
        elif command == "show" and args in [["rc"], ["policy"]]:
            config = self.config if args[0] == "rc" else self.registry.get(self._identity).config
            self._emit(json.dumps(public_value(config), ensure_ascii=False, indent=2))
        elif command == 'save-config' and len(args) == 2 and args[0] == 'policy':
            self._operation_start('save_member', identity=args[1])
        elif command == "save-config" and args in [["rc"], ["policy"]]:
            if args[0] == "rc":
                atomic_json(self.config_path, self.config)
                path = self.config_path
            else:
                path = self.registry.save(self._identity)
            self._emit(f"有效配置已保存：{path}")
        elif command == "resume" and not args:
            if self._worker is None or not getattr(self._worker, "runnable", self._worker.alive):
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
        for identity in self._exit_operations:
            self.operations.update(identity, "succeeded" if self.exit_code == 0 else "failed", result={"exit_code": self.exit_code})
        self.operations.cancel_pending("controller stopped")
        self._emit("安全退出完成，退出码 0" if self.exit_code == 0 else f"退出完成，存在强制终止或清理错误，退出码 {self.exit_code}")
        with self._log_lock:
            if self._log_stream is not None:
                try:
                    self._log_stream.close()
                except OSError:
                    self.exit_code = 1
            self._log_stream = None
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
                    try:
                        urgent = self._urgent.get_nowait()
                    except queue.Empty:
                        urgent = None
                    if urgent:
                        self._dispatch(urgent)
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
                        deadline = time.monotonic() + period
                except Exception as exc:
                    self._cancel_direct(f"control failure: {exc}")
                    self._pause("fault")
                    self._emit(f"控制错误：{exc}", "error")
                    if self._stop.is_set():
                        self.exit_code = 1
                    if self._worker is not None and self._operation is None and self.control_owner != "mcp":
                        self._operation_start("unload", worker=self._worker)
                    deadline = time.monotonic() + period
            self._wake.wait(max(0, deadline - time.monotonic()))
            self._wake.clear()
