"""Public command model and RC configuration drafts, independent of terminal UI."""
import copy
import json
import shlex

from rc_policy_sdk.command_syntax import Command, parse_command
from .config import dotted_set, _validate_rc
from .secret_config import public_value, reject_secret_edit
from .yaml_config import save_yaml_config


def config_changes(active, draft, prefix=''):
    changes = {}
    for key in active.keys() | draft.keys():
        path = f'{prefix}.{key}' if prefix else key
        old, new = active.get(key), draft.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            changes.update(config_changes(old, new, path))
        elif old != new:
            changes[path] = {'active': copy.deepcopy(old), 'draft': copy.deepcopy(new)}
    return changes


def execution_line(command):
    """Render existing private controller operations; never accepts user text."""
    kind, target, args = command.kind, command.target, command.args
    if kind in ('help', 'status', 'pause', 'resume', 'reset', 'exit'):
        return kind
    if kind == 'connector':
        return shlex.join(['connector', *args])
    if kind == 'policy_list':
        return 'policies'
    if kind == 'policy_switch':
        return shlex.join(['switch', target, *args])
    if kind == 'policy_retry':
        return shlex.join(['retry-reload', target])
    if kind == 'policy_command':
        return shlex.join(['policy', target, *args])
    if kind == 'policy_config_save':
        return shlex.join(['save-config', 'policy', target])
    if kind == 'global_show':
        return shlex.join(['show', 'global', *([target] if target else [])])
    if kind == 'global_set':
        return f'set global.{target} {command.value}'
    if kind == 'global_remove':
        return 'unset global.' + target
    if kind in ('pose_save', 'pose_restore'):
        return shlex.join(['save' if kind == 'pose_save' else 'load', target])
    if kind == 'record':
        return 'record' + ((' ' + {'start':'on', 'stop':'off', 'status':'status'}[args[0]]) if args else '')
    return kind


class HumanCommandSupport:
    def completion_snapshot(self):
        """Copy cached command metadata only; safe for the background completer.

        Do not call registry.get/list here: an uncached get can read manifests.
        Source discovery is handled separately by CompletionService.
        """
        from rc_policy_sdk.argument_hints import normalize_config_hints
        from rc_policy_sdk.commands import command_specs
        from .completion_service import worker_binding

        with self._lock:
            records = self.registry._records
            active = records.get(self._identity)
            members = (active.members if active.is_chain else (active,)) if active else ()
            worker = self._worker
            workers = (worker.member_workers() if hasattr(worker, 'member_workers')
                       else {self._identity: worker} if worker is not None else {})
            instances = {}
            for member in members:
                target = workers.get(member.instance_id)
                instances[member.instance_id] = {
                    'commands': command_specs(member.config),
                    'config_hints': normalize_config_hints(member.config.get('config_hints')),
                    'worker': target, 'binding': worker_binding(target),
                    'monitor': bool(getattr(target, 'supports_monitor', False)),
                }
            service = getattr(self, '_global_service', None)
            try:
                global_keys = list(json.loads(service.raw)) if service is not None else []
            except (TypeError, ValueError):
                global_keys = []
            return {'instances': instances, 'sources': list(records),
                    'rc_config': public_value(getattr(self, '_rc_config_draft', self.config)),
                    'global_keys': global_keys, 'pose_root': str(self.outputs / 'state_saves')}

    def submit(self, line, *, origin='tui', policy_arguments=None, mcp_tool=None,
               expected_control=None, batch_id=None):
        if not isinstance(line, str):
            raise ValueError('invalid command line')
        if not line.strip():
            return None
        # Actual local input interrupts pending batches, even when invalid.
        if origin == 'tui' and batch_id is None:
            self.interrupt_tui_batches()
        reject_secret_edit(line)
        parsed = parse_command(line)
        if parsed.local_only:
            raise PermissionError('此命令只支持本地终端入口')
        return self._submit(execution_line(parsed), origin=origin, policy_arguments=policy_arguments,
                            mcp_tool=mcp_tool, expected_control=expected_control, batch_id=batch_id,
                            parsed=parsed, display_line=line.strip())

    def submit_control(self, action, *, expected_control=None):
        if action not in ('take', 'return'):
            raise ValueError('control action must be take or return')
        return self._submit(action, origin='mcp', mcp_tool=action, expected_control=expected_control,
                            display_line='mcp ' + action)

    @staticmethod
    def is_read_command(line):
        try:
            return parse_command(line).readonly
        except (ValueError, TypeError):
            return False

    def query(self, line):
        parsed = parse_command(line)
        if not parsed.readonly:
            raise ValueError('not a read command')
        if parsed.kind == 'connector' and parsed.args == ('list',):
            return self.connector_profiles.list()
        with self._lock:
            return self._query_parsed(parsed)

    def show_active_policy_config(self):
        with self._lock:
            return self._query('show policy')

    def _active_member(self, identity):
        if self._identity is None or self._worker is None:
            raise ValueError('没有已运行的策略实例')
        record = self.registry.get(self._identity)
        if record.is_chain:
            return record, self.registry.resolve_member(record, identity)
        if identity != record.instance_id:
            raise ValueError(f'{identity}: not_running')
        return record, record

    def _query_parsed(self, parsed):
        kind = parsed.kind
        if kind == 'help':
            from .help_content import builtin_help
            from .help_document import help_payload
            value = builtin_help(parsed.args)
            if not parsed.args:
                capabilities = self.policy_capabilities()
                for policy in capabilities['policies']:
                    if policy['loaded']:
                        identity = policy['instance_id']
                        value['commands'].append({'group': '已加载策略实例', 'command': 'policy help ' + identity,
                            'summary': f"来源 {policy['source_id']}；查看命令、参数与示例。"})
            return help_payload(value)
        if kind == 'rc_config_show':
            draft = getattr(self, '_rc_config_draft', self.config)
            return public_value({'active': self.config, 'draft': draft, 'changes': config_changes(self.config, draft)})
        if kind == 'policy_config_show':
            _, member = self._active_member(parsed.target)
            exported = self._member_config_export(member)
            if exported is not None:
                return public_value({**exported['policy'], **exported['settings']})
            return public_value(member.runtime_config)
        if kind == 'policy_help':
            return self.policy_help(parsed.target, parsed.args[0] if parsed.args else None)
        return self._query(execution_line(parsed))

    def _dispatch_human(self, request):
        parsed = request.parsed
        if parsed is None:
            return False
        if parsed.kind == 'rc_config_set':
            draft = copy.deepcopy(getattr(self, '_rc_config_draft', self.config))
            def invalid(value):
                raise ValueError(f'JSON 不支持 {value}')
            dotted_set(draft, parsed.target, json.loads(parsed.value, parse_constant=invalid))
            self._rc_config_draft = _validate_rc(draft)
            result = self._query_parsed(Command('rc_config_show'))
            self.operations.update(request.operation_id, 'succeeded', result=result)
            self._emit_command('RC 配置草稿已更新；config rc save 保存，重启后生效')
            return True
        if parsed.kind == 'rc_config_save':
            draft = _validate_rc(copy.deepcopy(getattr(self, '_rc_config_draft', self.config)))
            save_yaml_config(self.config_path, draft)
            self._rc_config_draft = draft
            self.operations.update(request.operation_id, 'succeeded', result={'path': str(self.config_path), 'restart_required': bool(config_changes(self.config, draft))})
            self._emit_command(f'RC 配置已保存：{self.config_path}；重启后生效')
            return True
        return False

    def submit_policy_config(self, instance_id, action='show', *, key=None, value_text=None,
                             expected_control=None):
        if not isinstance(instance_id, str) or action not in ('show', 'set', 'save'):
            raise ValueError('invalid policy config target or action')
        line = shlex.join(['policy', instance_id, 'config', action])
        if action == 'set':
            if not isinstance(key, str) or not key or any(c.isspace() for c in key) or not isinstance(value_text, str) or not value_text.strip():
                raise ValueError('config set requires key and value_text strings')
            line += ' ' + key + ' ' + value_text
        elif key is not None or value_text is not None:
            raise ValueError('key/value_text only apply to set')
        return self.submit(line, origin='mcp', expected_control=expected_control, mcp_tool='policy_config')
