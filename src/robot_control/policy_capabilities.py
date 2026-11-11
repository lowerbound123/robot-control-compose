"""Read policy command declarations without importing model implementations."""
from rc_policy_sdk.commands import command_specs, command_usage
from rc_policy_sdk.argument_hints import normalize_config_hints
from rc_policy_sdk.help_metadata import normalize_help_details

from .policy_catalog import read_descriptor
from .secret_config import public_value
from .help_document import document, help_payload, help_text


EFFECT_HELP = {
    'update_config': '只提交 manifest 白名单中的热配置叶字段，保留 context 与执行进程。',
    'reload_policy': '先证明目标实例旧进程树、容器和外部读者退出，再冷重建该实例；保留其他实例。',
    'reload_all_policy': '收齐整个旧执行组的退出证明后重建全组。',
    'reinitialize_policy': '保留执行进程和已加载模块；暂停并收齐读者确认后，在旧绑定 release、新绑定 initialize。失败只能手动冷重试该实例。',
}


def policy_help_document(capabilities, *, detailed=True, command=None):
    value = document('策略帮助', '静态声明来自 policy.yaml；查询不会导入模型、启动容器或调用策略回调。')

    def add_details(details, label, instance=None):
        def bind(text):
            return text.replace('<实例>', instance) if instance else text
        if details.get('description'):
            value['sections'].append({'title': label, 'body': bind(details['description'])})
        value['sections'].extend({'title': label + ' · ' + row['title'], 'body': bind(row['body'])}
                                 for row in details.get('sections', []))
        value['examples'].extend({'title': label + ' · ' + row['title'], 'command': bind(row['command'])}
                                 for row in details.get('examples', []))

    for chain in capabilities.get('chains', []):
        label = '原生链 ' + chain['source_id']
        if chain.get('help_details', {}).get('group'):
            label += ' · ' + chain['help_details']['group']
        value['sections'].append({'title': label, 'body': chain['help'] + '\n成员顺序：' +
            ' → '.join(chain['policies']) + '\n原生链自身没有执行容器，每个环节独立实例化。'})
        add_details(chain.get('help_details', {}), label)
    for policy in capabilities['policies']:
        loaded = policy['loaded'] and policy['instance_id'] is not None
        target = policy['instance_id'] if loaded else '<实例>'
        identity = policy['instance_id'] or policy.get('planned_instance_id') or policy['source_id']
        label = identity + (' [running]' if loaded else ' [not_running]')
        details = policy.get('help_details', {})
        if details.get('group'):
            label += ' · ' + details['group']
        state = f"来源：{policy['source_id']}；" + (f"运行实例：{target}" if loaded else '未加载；示例中的 <实例> 保持占位，加载后请使用确切实例 ID。')
        if policy.get('planned_instance_id'):
            state += ' 计划实例：' + policy['planned_instance_id'] + '。'
        value['sections'].append({'title': label, 'body': '\n'.join(filter(None, [policy.get('help', ''), state]))})
        add_details(details, label, target if loaded else None)
        for spec in policy['commands']:
            custom = spec.get('help_details', {})
            usage = f"policy {target} {command_usage(spec['name'], spec)}"
            group = f"{identity} · {custom.get('group') or details.get('group') or '策略命令'}"
            value['commands'].append({'group': group, 'command': usage, 'summary': spec['help']})
            if not detailed:
                continue
            title = identity + ' / ' + spec['name']
            if command is not None:
                value['usage'].append(usage)
            add_details(custom, title, target if loaded else None)
            timeout = '不设 handler 超时（timeout_s=-1）' if spec['timeout_s'] == -1 else f"handler 超时：{spec['timeout_s']} 秒"
            effect = EFFECT_HELP.get(spec.get('effect'), '未声明生命周期 effect；具体业务行为由策略 handler 实现。')
            value['sections'].append({'title': title + ' · 执行影响', 'body': f'{timeout}。MCP 调用需先 take。\n{effect}' +
                ('\n生命周期完成后保留 MCP 控制权并暂停策略；等待超时不自动重试。' if spec.get('effect') else '')})
            for parameter in spec['parameters']:
                row = {'name': parameter['name'] if command is not None and len(capabilities['policies']) == 1 else title + ' / ' + parameter['name'],
                       'type': parameter['type'], 'required': True, 'description': parameter.get('help', '')}
                if 'choices' in parameter:
                    row['choices'] = parameter['choices']
                    row['description'] += ' 候选仅供提示，策略仍可解析其他输入。'
                if parameter['type'] == 'path':
                    row['description'] += f" 路径提示根目录：{parameter['root']}；kind={parameter['kind']}。"
                value['parameters'].append(row)
        if detailed and command in (None, 'config'):
            hints = policy['config_hints']
            title = identity + ' / 配置'
            if hints.get('help_details', {}).get('group'):
                title += ' · ' + hints['help_details']['group']
            value['usage'].append(f'policy {target} config set <设置项> <原始值>')
            value['sections'].append({'title': title, 'body': hints['help'] + '\n设置项和原始文本全部交给 configure 解析，提示不会改变业务校验；普通提交保持配置引用不变。'})
            add_details(hints.get('help_details', {}), title, target if loaded else None)
            for key, parameter in hints['settings'].items():
                row = {'name': identity + ' / ' + key, 'type': parameter['type'], 'required': False,
                       'description': parameter.get('help', '')}
                if 'choices' in parameter:
                    row['choices'] = parameter['choices']
                value['parameters'].append(row)
    if capabilities.get('errors'):
        value['sections'].append({'title': '无法读取的来源', 'body': '\n'.join(row['policy_id'] + '：' + str(row['error']) for row in capabilities['errors'])})
    if not capabilities['policies'] and not capabilities.get('chains'):
        value['sections'].append({'title': '策略目录', 'body': '没有可用策略声明。policy list 查看来源配置状态。'})
    return value


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
            errors, chains = [], []
            if active is not None and active.is_chain:
                chains.append({'source_id': active.source_id, 'policies': active.config['policies'],
                    'help': active.config.get('help', ''), 'help_details': normalize_help_details(active.config.get('help_details', {}))})
            for row in self.registry.list():
                if row['id'] in sources or active is not None and active.is_chain and row['id'] == active.source_id:
                    continue
                if not row['valid']:
                    errors.append({'policy_id': row['id'], 'error': row.get('error', row.get('state'))})
                    continue
                descriptor = read_descriptor(row['directory'])
                config = descriptor['config']
                if config.get('kind') == 'chain':
                    chains.append({'source_id': row['id'], 'policies': config['policies'],
                        'help': config.get('help', ''), 'help_details': normalize_help_details(config.get('help_details', {}))})
                else:
                    rows.append(self._describe_commands(row['id'], None, config))
            if policy_id is not None:
                selected_chain = next((item for item in chains if item['source_id'] == policy_id), None)
                exact = [row for row in rows if row['instance_id'] == policy_id]
                rows = exact or [row for row in rows if row['source_id'] == policy_id]
                if active is not None and active.is_chain and policy_id == active.config['id']:
                    rows = [self._describe_commands(m.source_id, m.instance_id, m.config,
                        loaded=m.instance_id in workers and workers[m.instance_id].alive,
                        runnable=m.instance_id in workers and workers[m.instance_id].runnable, chain=True) for m in members]
                elif selected_chain is not None:
                    from .chain_config import chain_instance_ids
                    rows = [{**self._describe_commands(source, None, read_descriptor(self.registry.directory / source)['config']),
                             'planned_instance_id': instance}
                            for source, instance in zip(selected_chain['policies'], chain_instance_ids(selected_chain['policies']))]
                chains = [selected_chain] if selected_chain is not None else []
                if not rows:
                    error = next((row['error'] for row in errors if row['policy_id'] == policy_id), None)
                    raise ValueError(error or f'unknown policy or instance: {policy_id}')
            if command == 'config':
                rows = [{**row, 'commands': []} for row in rows]
            elif command is not None:
                selected = []
                for row in rows:
                    commands = [spec for spec in row['commands'] if command in (spec['name'], spec['alias'])]
                    if commands:
                        selected.append({**row, 'commands': commands})
                if not selected:
                    raise ValueError(f'unknown policy command: {command}')
                rows = selected
            return public_value({'policies': rows, 'chains': chains, 'errors': errors if policy_id is None else []})

    @staticmethod
    def _describe_commands(source, instance, config, *, loaded=False, runnable=False, chain=False):
        commands = []
        for name, spec in command_specs(config).items():
            commands.append({'name': name, 'alias': None,
                             'args': spec['args'], 'parameters': spec['parameters'], 'help': spec['help'], 'timeout_s': spec['timeout_s'],
                             'help_details': spec.get('help_details', {}),
                             'effect': spec.get('effect'), 'requires_take': True})
        return {'source_id': source, 'instance_id': instance, 'loaded': loaded,
                'runnable': runnable, 'help': config.get('help', ''),
                'help_details': normalize_help_details(config.get('help_details', {})),
                'commands': commands, 'config_hints': normalize_config_hints(config.get('config_hints'))}

    @staticmethod
    def _format_capabilities(capabilities):
        return help_text(policy_help_document(capabilities))

    def policy_help(self, policy_id=None, command=None):
        capabilities = self.policy_capabilities(policy_id, command)
        value = policy_help_document(capabilities, detailed=policy_id is not None or command is not None, command=command)
        return help_payload(value, **capabilities)
