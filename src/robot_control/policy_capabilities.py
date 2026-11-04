"""Read policy command declarations without importing model implementations."""
from rc_policy_sdk.commands import command_specs, command_usage

from .policy_catalog import read_descriptor
from .secret_config import public_value


class PolicyCapabilities:
    def policy_capabilities(self, policy_id=None, command=None):
        with self._lock:
            active = self.registry.get(self._identity) if self._identity is not None else None
            members = (active.members if active.is_chain else (active,)) if active else ()
            workers = (self._worker.member_workers() if hasattr(self._worker, 'member_workers')
                       else {self._identity: self._worker} if self._worker is not None else {})
            rows = []
            for member in members:
                worker = workers.get(member.instance_id)
                rows.append(self._describe_commands(member.source_id, member.instance_id, member.config,
                    loaded=worker is not None and worker.alive,
                    runnable=worker is not None and getattr(worker, 'runnable', worker.alive), chain=active.is_chain))
            sources = {member.source_id for member in members}
            errors = []
            for row in self.registry.list():
                if row['id'] in sources or row.get('kind') == 'chain':
                    continue
                if not row['valid']:
                    errors.append({'policy_id': row['id'], 'error': row.get('error', row.get('state'))})
                    continue
                descriptor = read_descriptor(row['directory'])
                rows.append(self._describe_commands(row['id'], None, descriptor['config']))
            if policy_id is not None:
                exact = [row for row in rows if row['instance_id'] == policy_id]
                rows = exact or [row for row in rows if row['source_id'] == policy_id]
                if active is not None and active.is_chain and policy_id == active.config['id']:
                    rows = [self._describe_commands(m.source_id, m.instance_id, m.config,
                        loaded=m.instance_id in workers and workers[m.instance_id].alive,
                        runnable=m.instance_id in workers and workers[m.instance_id].runnable, chain=True) for m in members]
                if not rows:
                    error = next((row['error'] for row in errors if row['policy_id'] == policy_id), None)
                    raise ValueError(error or f'unknown policy or instance: {policy_id}')
            if command is not None:
                selected = []
                for row in rows:
                    commands = [spec for spec in row['commands'] if command in (spec['name'], spec['alias'])]
                    if commands:
                        selected.append({**row, 'commands': commands})
                if not selected:
                    raise ValueError(f'unknown policy command: {command}')
                rows = selected
            return public_value({'policies': rows, 'errors': errors if policy_id is None else []})

    @staticmethod
    def _describe_commands(source, instance, config, *, loaded=False, runnable=False, chain=False):
        commands = []
        for name, spec in command_specs(config).items():
            commands.append({'name': name, 'alias': f'{instance}-{name}' if instance and chain else None,
                             'args': spec['args'], 'help': spec['help'], 'timeout_s': spec['timeout_s'],
                             'effect': spec.get('effect'), 'requires_take': True})
        return {'source_id': source, 'instance_id': instance, 'loaded': loaded,
                'runnable': runnable, 'commands': commands}

    @staticmethod
    def _format_capabilities(capabilities):
        rows = []
        for policy in capabilities['policies']:
            target = policy['instance_id'] or policy['source_id']
            state = 'running' if policy['loaded'] else 'not_running'
            for spec in policy['commands']:
                effect = f"; effect={spec['effect']}" if spec['effect'] else ''
                rows.append(f"policy {target} {command_usage(spec['name'], spec)} — {spec['help']} "
                            f"[{state}] (timeout_s={spec['timeout_s']}{effect}; MCP requires take)")
        return '\n'.join(rows)

    def policy_help(self, policy_id=None, command=None):
        capabilities = self.policy_capabilities(policy_id, command)
        return {**capabilities, 'help': self._format_capabilities(capabilities)}
