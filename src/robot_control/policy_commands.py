"""Local policy command routing; called under the controller lock."""
import json

from rc_policy_sdk.commands import command_usage, parse_command
from .operations import ControlRequest
from .config import PolicyRecord, changed_config, dotted_get


class PolicyCommandSupport:
    def _init_policy_commands(self):
        self._policy_calls = {}
        self._policy_reloads = {}
        self._command_faults = set()

    def _local_specs(self):
        worker = self._worker
        if (worker is None or not getattr(worker, "runnable", worker.alive) or worker in self._command_faults
                or self._operation is not None or self._stop.is_set()):
            return {}
        return getattr(worker, "command_specs", {})

    def local_commands(self):
        with self._lock:
            return list(self._local_specs())

    def declared_commands(self):
        from rc_policy_sdk.catalog import read_descriptor
        from rc_policy_sdk.commands import command_specs
        result = {}
        for row in self.registry.list():
            if not row['valid'] or row.get('kind') == 'chain':
                continue
            config = read_descriptor(row['directory'])['config']
            result[row['id']] = command_specs(config)
        return result

    def local_help(self, name=None):
        from .controller import HELP
        with self._lock:
            specs = self._local_specs()
            if name in specs:
                value = specs[name]
                return f"{command_usage(name, value)} — {value['help']} (timeout_s={value['timeout_s']})"
            catalog = self.declared_commands()
            if name is not None and name not in catalog:
                raise ValueError(f'unknown policy or active command: {name}')
            worker = self._worker
            active = (set(worker.monitor_members()) if hasattr(worker, 'monitor_members') else
                      {self._identity} if worker is not None else set())
            rows = []
            for identity, commands in catalog.items():
                if name is not None and name != identity:
                    continue
                state = 'running' if identity in active else 'not_running'
                for key,value in commands.items():
                    rows.append(f"policy {identity} {command_usage(key, value)} — {value['help']} [{state}] (timeout_s={value['timeout_s']})")
            return '\n'.join(([HELP, 'Policy TUI commands:'] if name is None else []) + rows)

    def _policy_request(self, identity, line, word):
        specs = getattr(self._worker, "command_specs", {})
        if word == 'policy':
            import shlex
            words = shlex.split(line)
            if len(words) < 3:
                raise ValueError('用法：policy <id> <command> [args...]')
            target, command = words[1:3]
            worker = self._worker
            if worker is None:
                raise RuntimeError(f'{target}: not_running; switch first')
            if getattr(worker, 'record', None) is not None and worker.record.is_chain:
                key = f'{target}-{command}'
                if worker.command_targets.get(key) != (target, command):
                    raise RuntimeError(f'{target}: not_running or unknown command')
            else:
                if target != self._identity:
                    raise RuntimeError(f'{target}: not_running; switch first')
                key = command
            if key not in specs:
                raise ValueError(f'{target}: unknown command {command}')
            word, line = key, shlex.join([key, *words[3:]])
        if word not in specs:
            targets = getattr(self._worker, 'command_targets', {})
            if sum(1 for _,command in targets.values() if command == word) > 1:
                raise ValueError(f'ambiguous command {word}; use policy <id> {word}')
            return None
        if not self._local_specs():
            raise RuntimeError("busy: no active policy accepting commands")
        name, args = parse_command(line, specs)
        reload_effect = specs[name].get("effect") == "reload_policy"
        if reload_effect and (self.control_owner == "mcp" or self._return_requested):
            raise PermissionError("reload_policy commands are unavailable during MCP takeover")
        self._emit(f"policy command [{identity}] queued: {line}")
        request = ControlRequest(identity, line, "tui", self._control_epoch,
                              policy_worker=self._worker, policy_version=self._worker.config_version,
                              policy_name=name, policy_args=tuple(args))
        if reload_effect:
            self._policy_reloads[identity] = (request, self.buffer.generation)
        return request

    def _reload_is_current(self, request, generation):
        worker = request.policy_worker
        return (worker is self._worker and getattr(worker, "runnable", worker.alive) and worker not in self._command_faults
                and worker.config_version == request.policy_version
                and request.epoch == self._control_epoch and generation == self.buffer.generation
                and self.control_owner == "policy" and not self._return_requested
                and self._operation is None and not self._stop.is_set())

    def _reload_policy_result(self, identity, snapshot, patch):
        request, generation = snapshot
        if not self._reload_is_current(request, generation):
            self.operations.update(identity, "cancelled", error="policy/control state changed during reload command")
            self._emit(f"policy command [{identity}] cancelled: policy/control state changed")
            return
        try:
            if not isinstance(patch, dict) or not patch:
                raise ValueError("reload_policy result must be a nonempty params leaf patch")
            record = self.registry.get(self._identity)
            member_id = None
            target = record
            if record.is_chain:
                member_id = request.policy_worker.command_targets[request.policy_name][0]
                target = self.registry.get(member_id)
            config = target.config
            for path, value in patch.items():
                if not isinstance(path, str) or not path.startswith("params."):
                    raise ValueError("reload_policy may change only existing params.* leaves")
                if isinstance(dotted_get(target.config, path), dict) or isinstance(value, dict):
                    raise ValueError("reload_policy must address existing params.* leaves")
                config, hot = changed_config(config, path, value)
                if hot:
                    raise ValueError(f"reload_policy requires cold params: {path}")
            proposed = PolicyRecord(target.directory, config, target.version + 1)
            extra = {}
            if member_id is not None:
                extra['member_commit'] = (member_id, config)
                proposed = self.registry.resolve_chain(
                    PolicyRecord(record.directory, record.config, record.version + 1), {member_id: proposed})
            self._invalidate_policy_editor()
            # The command callback has finished. Transfer its receipt to the
            # existing cold lifecycle, whose prepare handshake invalidates all
            # cached actions before closing the old worker. Even equal patches
            # must run initialize again (the file at that path may have changed).
            self._operation_start("switch", record=proposed, old_worker=request.policy_worker,
                                  reset=False, commit=True, version=proposed.version, public_id=identity, **extra)
            self.operations.update(identity, "running")
        except Exception as exc:
            self.operations.update(identity, "failed", error=exc)
            self._emit(f"policy command [{identity}] failed: {exc}", "error")

    def _dispatch_policy_command(self, request):
        worker = request.policy_worker
        snapshot = self._policy_reloads.get(request.operation_id)
        if snapshot is not None and not self._reload_is_current(*snapshot):
            self._policy_reloads.pop(request.operation_id, None)
            self.operations.update(request.operation_id, "cancelled", error="policy/control state changed before reload command")
            return
        if (worker is not self._worker or worker.config_version != request.policy_version
                or worker in self._command_faults or not getattr(worker, "runnable", worker.alive)):
            self.operations.update(request.operation_id, "cancelled", error="policy instance/configuration changed")
            self._emit(f"policy command [{request.operation_id}] cancelled: policy changed")
            return
        if request.origin != "tui":
            raise PermissionError("policy commands are local-only")
        try:
            worker.submit_command(request.operation_id, request.policy_name, list(request.policy_args))
        except Exception:
            self._policy_reloads.pop(request.operation_id, None)
            raise
        self._policy_calls[request.operation_id] = worker

    def _restore_policy_commands(self, worker):
        if (worker is not None and worker is self._worker and worker not in self._command_faults
                and not self._stop.is_set() and getattr(worker, "supports_commands", False)):
            worker.resume_commands()

    def _gate_policy_commands(self, worker):
        if worker is None:
            return
        if getattr(worker, "supports_commands", False):
            worker.stop_commands()
        # Requests accepted by the UI but not yet sent must obey the same barrier.
        import queue
        retained = []
        while True:
            try:
                request = self._commands.get_nowait()
            except queue.Empty:
                break
            if request.policy_worker is worker:
                self._policy_reloads.pop(request.operation_id, None)
                self.operations.update(request.operation_id, "cancelled", error="policy lifecycle operation")
                self._emit(f"policy command [{request.operation_id}] cancelled: lifecycle operation")
            else:
                retained.append(request)
        for request in retained:
            self._commands.put(request)

    def _poll_policy_commands(self):
        for worker in set(self._policy_calls.values()):
            for _ in range(512):
                message = worker.poll_command()
                if message is None:
                    break
                identity = message["command_id"]
                if self._policy_calls.get(identity) is not worker:
                    continue
                kind = message["type"]
                if kind == "command_started":
                    self.operations.update(identity, "running")
                    self._emit(f"policy command [{identity}] running")
                    continue
                self._policy_calls.pop(identity)
                snapshot = self._policy_reloads.pop(identity, None)
                if snapshot is not None and kind == "command_result":
                    self._reload_policy_result(identity, snapshot, message.get("result"))
                    continue
                status = {"command_result": "succeeded", "command_error": "failed",
                          "command_cancelled": "cancelled", "command_timeout": "failed"}[kind]
                self.operations.update(identity, status, result=message.get("result"), error=message.get("error"))
                value = json.dumps(message.get("result"), ensure_ascii=False) if kind == "command_result" else message.get("error", "cancelled")
                self._emit(f"policy command [{identity}] {status}: {value}", "error" if status == "failed" else "info")
                if kind == "command_timeout" or message.get("fatal"):
                    self._command_faults.add(worker)
                    for other, owner in list(self._policy_calls.items()):
                        if owner is worker:
                            self._policy_calls.pop(other)
                            self._policy_reloads.pop(other, None)
                            self.operations.update(other, "cancelled", error=f"policy worker failed: {message.get('error')}")
                            self._emit(f"policy command [{other}] cancelled: worker failure")
                    self._gate_policy_commands(worker)
                    if worker is self._worker:
                        self._pause()
                        if self._operation is not None and self._operation["kind"] not in ("shutdown", "close_robot", "unload"):
                            self._operation["cancel"].set()
                            if not self._operation["launched"]:
                                self.operations.update(self._operation.get("public_id"), "cancelled", error="policy command timeout")
                                if self._operation.get("public_id") == self._return_operation:
                                    self._return_requested = False
                                    self._return_operation = None
                                self._operation = None
                    break
            if not worker.alive and not getattr(worker, "has_command_results", False):
                for identity, owner in list(self._policy_calls.items()):
                    if owner is worker:
                        self._policy_calls.pop(identity)
                        self._policy_reloads.pop(identity, None)
                        self.operations.update(identity, "cancelled", error="policy worker exited")
                        self._emit(f"policy command [{identity}] cancelled: worker exited")

    def _unload_command_fault(self):
        if self._worker in self._command_faults and self._operation is None and not self._stop.is_set():
            self._operation_start("unload", worker=self._worker, preserve_state=True)
