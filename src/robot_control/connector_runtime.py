"""Connector deployments share ownership/exit proofs, not policy lifecycle."""
import json
import os
from pathlib import Path
import uuid

from .config import atomic_json
from .runtime_spec import DeploymentSpec, map_host_path


class ConnectorRuntime:
    def start_connector(self, identity, directory, config, cancel, deadline):
        anchor = self.check_infrastructure(deadline, cancel=cancel)
        directory = Path(directory)
        env = {key: value for key, value in os.environ.items() if key in
               ('PATH', 'HOME', 'LANG', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CERT_PATH', 'DOCKER_TLS_VERIFY')}
        env.update(RC_HOST_PROJECT_ROOT=self.context['host_root'], RC_UID=str(self.context['uid']), RC_GID=str(self.context['gid']))
        model = json.loads(self._run(['compose', '--project-directory', str(directory), '--env-file', '/dev/null',
            '-f', str(directory / 'compose.yaml'), 'config', '--format', 'json', '--no-path-resolution'],
            deadline, cwd=directory, env=env, cancel=cancel))
        if set(model.get('services', {})) != {'connector'}:
            raise ValueError('connector compose must contain one service named connector')
        service = model['services']['connector']
        if service.get('depends_on') or service.get('privileged') or service.get('pid'):
            raise ValueError('connector cannot use sidecars, privileged mode or shared PID namespaces')
        image_id = json.loads(self._run(['image', 'inspect', service['image']], deadline, cancel=cancel))[0]['Id']
        launch = uuid.uuid4().hex
        working = self.state_dir / ('connector-' + launch)
        working.mkdir(parents=True, mode=0o700)
        atomic_json(working / 'config.json', config)
        atomic_json(working / 'reader.json', {'container_id': getattr(self, 'rc_container_id', None)})
        endpoint = '/run/rc-connectors/c-' + launch[:20]
        source = '/workspace/connector/' + directory.name
        sdk = '/workspace/packages/connector-sdk/src'
        for key in ('build', 'profiles', 'container_name', 'pull_policy', 'healthcheck'): service.pop(key, None)
        service.update(image=image_id, ipc='container:' + anchor, restart='no', user=f"{self.context['uid']}:{self.context['gid']}")
        service['volumes'] = [
            {'type': 'volume', 'source': 'connector-rpc', 'target': '/run/rc-connectors'},
            {'type': 'bind', 'source': map_host_path(directory, directory, self.context), 'target': source, 'read_only': True},
            {'type': 'bind', 'source': str(Path(self.context['host_root']) / 'packages/connector-sdk/src'), 'target': sdk, 'read_only': True},
            {'type': 'bind', 'source': map_host_path(working, directory, self.context), 'target': '/runtime'},
        ]
        for mount in service['volumes']:
            if mount['type'] == 'bind': mount['bind'] = {'create_host_path': False}
        service['environment'] = dict(service.get('environment', {}), PYTHONPATH=sdk,
                                      HOME='/tmp', PYTHONPYCACHEPREFIX='/tmp/connector-pycache')
        service['command'] = ['python3', '-m', 'rc_connector_sdk', '--source', source + '/main.py',
            '--socket', endpoint, '--instance-id', identity, '--config-file', '/runtime/config.json', '--journal', '/runtime/pool.json']
        model['volumes'] = {'connector-rpc': {'external': True, 'name': self.context['connector_rpc_volume']}}
        frozen = DeploymentSpec(identity, str(directory), 1, json.dumps(config), json.dumps(model), image_id,
            anchor, endpoint, self.anchor_epoch or '', 'connector', str(working / 'pool.json'))
        return self.start(frozen, 1, cancel, deadline)

    def connector_ready(self, handle, info):
        if handle.kind != 'connector' or info['instance_id'] != handle.policy_id:
            raise RuntimeError('connector readiness identity mismatch')
        handle.host_instance_id = info['runtime_id']
        self._save(handle)
        reader = json.loads(Path(handle.connector_journal).with_name('reader.json').read_text())
        self.record_pool(info['pool_id'], reader_container_id=reader['container_id'])

    def record_connector_orphans(self):
        for handle in self.handles.values():
            if handle.kind != 'connector' or not handle.connector_journal: continue
            path = Path(handle.connector_journal)
            if not path.exists(): continue
            info = json.loads(path.read_text())
            if info['instance_id'] != handle.policy_id or (handle.host_instance_id and info['runtime_id'] != handle.host_instance_id):
                raise RuntimeError('connector pool journal identity mismatch')
            reader = json.loads(path.with_name('reader.json').read_text())
            self.record_pool(info['pool_id'], reader_container_id=reader['container_id'])
