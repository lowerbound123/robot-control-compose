"""Dynamic single-policy Compose projects. No robot or UI work occurs here."""
from __future__ import annotations

from dataclasses import asdict
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import signal
import threading
import time
import uuid

from rc_policy_sdk.catalog import socket_path
from .config import atomic_json
from .runtime_spec import LABEL, DeploymentSpec, DeploymentHandle, ContainerExitProof, map_host_path, project_name


class BlockedCleanup(RuntimeError):
    pass


class BlockedInfrastructure(RuntimeError):
    pass


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError('runtime deadline exceeded')
    return value


def docker_command(args, *, timeout, env=None, cwd=None, cancel=None):
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(['docker', *args], cwd=cwd, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise RuntimeError('Docker command cancelled; creation outcome requires reconciliation')
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('Docker command deadline exceeded')
            try:
                stdout, _ = process.communicate(timeout=min(.05, left))
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        # Stop the CLI/plugin group, not daemon-managed containers. The durable
        # creation intent survives and must be reconciled even after cancellation.
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=.2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.communicate(timeout=.5)
        raise
    if process.returncode:
        raise RuntimeError(f'Docker {args[0]} failed (exit {process.returncode}); inspect daemon/service diagnostics')
    return stdout


class ComposeRuntimeManager:
    def __init__(self, context, state_dir, *, run_id=None, runner=docker_command):
        self.context = dict(context)
        self.state_dir = Path(state_dir)
        self.run_id = run_id or uuid.uuid4().hex
        self.runner = runner
        self.handles = {}
        self.authority = object()
        self._lock_file = None
        self.anchor_id = None
        self.anchor_epoch = None
        self.daemon_id = context.get('daemon_id')
        self.cleanup_forced = False
        self.phase = 'EMPTY'
        self.target = None
        self.error = None

    @classmethod
    def from_file(cls, path, state_dir=None, **kwargs):
        context = json.loads(Path(path).read_text())
        return cls(context, state_dir or Path(context['client_root'])/'outputs/runtime', **kwargs)

    def _run(self, args, deadline, *, cancel=None, **kwargs):
        if cancel is not None:
            if cancel.is_set():
                raise RuntimeError('Docker command cancelled')
            kwargs['cancel'] = cancel
        return self.runner(args, timeout=remaining(deadline), **kwargs)

    def _save(self, handle):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(self.state_dir / f'{handle.project}.json', asdict(handle))
        self.handles[handle.project] = handle

    def acquire(self, deadline):
        if self._lock_file is not None:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (self.state_dir / 'deployment.lock').open('a+')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise BlockedCleanup('deployment already has an active lifecycle owner') from None
        self._lock_file = lock
        actual = self._run(['info', '--format', '{{.ID}}'], deadline).strip()
        if self.daemon_id and actual != self.daemon_id:
            raise BlockedInfrastructure('Docker daemon identity changed')
        self.daemon_id = actual

    def release(self):
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    def _ids(self, labels, deadline, *, cancel=None):
        args = ['ps', '-aq', '--no-trunc']
        for key, value in labels.items():
            args.extend(['--filter', f'label={key}={value}'])
        return self._run(args, deadline, cancel=cancel).split()

    def _inspect(self, ids, deadline, *, cancel=None):
        return json.loads(self._run(['inspect', *ids], deadline, cancel=cancel)) if ids else []

    def check_daemon(self, deadline):
        actual = self._run(['info', '--format', '{{.ID}}'], deadline).strip()
        if actual != self.daemon_id:
            raise BlockedInfrastructure('Docker daemon identity changed; old resources cannot be proved absent')

    def check_infrastructure(self, deadline, *, cancel=None):
        actual = self._run(['info', '--format', '{{.ID}}'], deadline, cancel=cancel).strip()
        if actual != self.daemon_id:
            raise BlockedInfrastructure('Docker daemon identity changed')
        project = self.context['base_project']
        def service(name):
            ids = self._ids({'com.docker.compose.project':project,'com.docker.compose.service':name}, deadline, cancel=cancel)
            if len(ids) != 1:
                raise BlockedInfrastructure(f'{name}: expected one base container')
            item = self._inspect(ids, deadline, cancel=cancel)[0]
            if not item['State'].get('Running'):
                raise BlockedInfrastructure(f'{name}: base container is not running')
            return item
        anchor, rc = service('ipc-anchor'), service('robot-control')
        identity = anchor['Id']
        epoch = anchor['State'].get('StartedAt')
        from datetime import datetime
        import re
        def started(value):
            if not isinstance(value,str):
                raise BlockedInfrastructure('base IPC generation is unavailable')
            value = re.sub(r'\.(\d+)',lambda m:'.'+m[1][:6].ljust(6,'0'),value)
            return datetime.fromisoformat(value.replace('Z','+00:00'))
        if started(epoch) > started(rc['State'].get('StartedAt')):
            raise BlockedInfrastructure('anchor restarted after RC joined IPC; recreate both')
        if self.anchor_epoch is not None and epoch != self.anchor_epoch:
            raise BlockedInfrastructure('anchor IPC generation changed; recreate RC plus anchor')
        if anchor['HostConfig'].get('IpcMode') != 'shareable' or rc['HostConfig'].get('IpcMode') != 'container:' + identity:
            raise BlockedInfrastructure('RC/anchor IPC mismatch; recreate RC and anchor together')
        if self.anchor_id is not None and identity != self.anchor_id:
            raise BlockedInfrastructure('anchor changed; recreate RC and anchor together')
        self.anchor_id = identity
        self.anchor_epoch = epoch
        return identity

    def prepare_spec(self, record, *, deadline, cancel=None):
        anchor = self.check_infrastructure(deadline, cancel=cancel)
        identity = record.config['id']
        directory = record.directory
        env = dict({k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CERT_PATH','DOCKER_TLS_VERIFY')}, RC_HOST_PROJECT_ROOT=self.context['host_root'],
                   RC_HOST_POLICY_DIR=map_host_path(directory, directory, self.context),
                   RC_IPC_ANCHOR_ID=anchor, RC_RPC_VOLUME=self.context['rpc_volume'],
                   RC_UID=str(self.context['uid']), RC_GID=str(self.context['gid']))
        command = ['compose', '--project-directory', str(directory)]
        env_file = directory / 'deployment.env'
        command += ['--env-file', str(env_file) if env_file.is_file() else '/dev/null']
        command += ['-f', str(directory / 'compose.yaml'), 'config', '--format', 'json', '--no-path-resolution']
        model = json.loads(self._run(command, deadline, env=env, cwd=directory, cancel=cancel))
        services = model.get('services', {})
        if set(services) != {'policy'}:
            raise ValueError('policy compose.yaml must contain exactly one service named policy')
        service = services['policy']
        if service.get('depends_on') or service.get('pid') == 'host' or service.get('privileged'):
            raise ValueError('sidecars, host PID and privileged policies are not supported')
        image = service.get('image')
        if not isinstance(image, str) or not image:
            raise ValueError('policy must name a prepared image')
        image_id = json.loads(self._run(['image', 'inspect', image], deadline, cancel=cancel))[0]['Id']
        service.pop('build', None)
        service.pop('profiles', None)
        service.pop('container_name', None)
        service.pop('pull_policy', None)
        service['image'] = image_id
        service['restart'] = 'no'
        service['ipc'] = 'container:' + anchor
        service['user'] = f"{self.context['uid']}:{self.context['gid']}"
        target = f'/workspace/policy/{identity}'
        mounts = []
        for mount in service.get('volumes', []):
            if not isinstance(mount, dict):
                raise ValueError('Compose must normalize volume entries')
            if mount['target'] in ('/run/robot-control', target):
                continue
            if mount['type'] != 'bind':
                raise ValueError('only policy binds and the external RPC volume are supported')
            mount['source'] = map_host_path(mount['source'], directory, self.context)
            if (mount['target'].startswith('/run/') or mount['target'].startswith('/var/run/') or
                    mount['source'].endswith('.sock') or mount['source'] in ('/', '/run', '/var/run')):
                raise ValueError('policy may not mount the Docker API or host runtime sockets')
            mount['bind'] = {'create_host_path':False}
            mounts.append(mount)
        mounts += [dict(type='volume', source='rpc', target='/run/robot-control'),
                   dict(type='bind', source=map_host_path(directory, directory, self.context), target=target,
                        read_only=True, bind={'create_host_path':False})]
        service['volumes'] = mounts
        service['entrypoint'] = ['python', '-m', 'rc_policy_sdk.host']
        service['command'] = ['--host-id', identity, '--socket', socket_path(identity), '--policy-root', '/workspace/policy']
        service['environment'] = dict(service.get('environment', {}), HOME='/tmp', RC_POLICY_ID=identity)
        service['healthcheck'] = {'disable':True}  # Manager performs the identity-bearing handshake.
        model['volumes'] = {'rpc':{'external':True,'name':self.context['rpc_volume']}}
        for key in ('configs', 'secrets', 'include'):
            if model.get(key):
                raise ValueError(f'{key}: use policy-owned bind files')
        return DeploymentSpec(identity, str(directory), record.version, json.dumps(record.config),
                              json.dumps(model), image_id, anchor, socket_path(identity), self.anchor_epoch or "")

    def start(self, spec, generation, cancel, deadline):
        if cancel.is_set():
            raise RuntimeError('policy start cancelled')
        if self.check_infrastructure(deadline, cancel=cancel) != spec.anchor_id:
            raise BlockedInfrastructure('anchor changed after preflight')
        if (self.anchor_epoch or '') != spec.anchor_epoch:
            raise BlockedInfrastructure('anchor IPC generation changed after preflight')
        handle = DeploymentHandle(self.context['deployment_id'], self.daemon_id, spec.policy_id,
            self.run_id, generation, project_name(self.context['deployment_id'], spec.policy_id, self.run_id, generation),
            spec.anchor_id, spec.socket, anchor_epoch=spec.anchor_epoch)
        self._save(handle)  # Persist intent before the first mutating daemon request.
        model = json.loads(spec.model_json)
        model['name'] = handle.project
        service = model['services']['policy']
        service['labels'] = {**service.get('labels', {}), **handle.labels()}
        service['environment'].update(RC_DEPLOYMENT_ID=handle.deployment_id, RC_RUN_ID=handle.run_id,
                                      RC_GENERATION=str(generation))
        snapshot = self.state_dir / (handle.project + '.compose.json')
        atomic_json(snapshot, model)
        os.chmod(snapshot, 0o600)
        handle.phase = 'creating'
        handle.uncertain = True
        self._save(handle)
        try:
            self._run(['compose', '-p', handle.project, '-f', str(snapshot), 'create', '--no-build', '--pull', 'never'], deadline, cancel=cancel)
            ids = self._ids(handle.labels(), deadline, cancel=cancel)
            if len(ids) != 1:
                raise BlockedCleanup('create did not yield exactly one owned policy container')
            handle.container_id = ids[0]
            handle.resources = ids
            handle.uncertain = False
            handle.phase = 'created'
            self._save(handle)
            if cancel.is_set():
                raise RuntimeError('policy start cancelled')
            self._run(['start', handle.container_id], deadline, cancel=cancel)
            handle.phase = 'running'
            self._save(handle)
            return handle
        except BaseException:
            self._save(handle)
            raise

    def wait_ready(self, handle, cancel, deadline):
        from .host_client import HostClient
        last = None
        while remaining(deadline):
            if cancel.is_set():
                raise RuntimeError('policy readiness cancelled')
            try:
                expected = dict(deployment_id=handle.deployment_id, run_id=handle.run_id, generation=handle.generation)
                client = HostClient.get(handle.socket, handle.policy_id, expected=expected, connect_timeout=min(3, remaining(deadline)))
                handle.host_instance_id = client.instance_id
                self._save(handle)
                return client
            except (OSError, RuntimeError, ValueError, EOFError) as exc:
                last = exc
            ids = self._ids(handle.labels(), deadline, cancel=cancel)
            if ids != [handle.container_id]:
                raise RuntimeError('policy container disappeared before handshake')
            if not self._inspect(ids, deadline, cancel=cancel)[0]['State']['Running']:
                raise RuntimeError('policy container exited before handshake')
            cancel.wait(min(.05, remaining(deadline)))
        raise TimeoutError(f'policy readiness failed: {last}')

    def reconcile_owned(self, deadline):
        self.check_daemon(deadline)
        for path in self.state_dir.glob('rcp-*.json'):
            if path.name.endswith('.compose.json'):
                continue
            handle = DeploymentHandle(**json.loads(path.read_text()))
            if handle.deployment_id != self.context['deployment_id'] or handle.daemon_id != self.daemon_id:
                raise BlockedCleanup('runtime journal ownership mismatch')
            self.handles[handle.project] = handle
        ids = self._ids({LABEL+'deployment':self.context['deployment_id']}, deadline)
        for item in self._inspect(ids, deadline):
            labels = item['Config']['Labels']
            project = labels.get(LABEL+'project')
            handle = self.handles.get(project)
            if handle is None:
                raise BlockedCleanup('owned container has no durable creation intent')
            self._verify(item, handle)
            if handle.container_id and handle.container_id != item['Id']:
                raise BlockedCleanup('multiple containers in a single-policy generation')
            handle.container_id = item['Id']
            handle.resources = [item['Id']]
            handle.uncertain = False
            self._save(handle)
        return [h for h in self.handles.values() if h.phase != 'removed']

    def _verify(self, item, handle):
        labels = item['Config'].get('Labels', {})
        if any(labels.get(key) != value for key,value in handle.labels().items()):
            raise BlockedCleanup('container ownership mismatch')
        if item['HostConfig'].get('IpcMode') != 'container:' + handle.anchor_id:
            raise BlockedCleanup('container IPC ownership mismatch')
        if handle.daemon_id != self.daemon_id:
            raise BlockedCleanup('daemon ownership mismatch')

    def stop_and_remove(self, handle, deadline):
        self.check_daemon(deadline)
        forced = handle.cleanup_forced
        # A successful unfiltered list distinguishes absence from a daemon failure.
        all_ids = self._ids({}, deadline)
        owned = self._ids(handle.labels(), deadline)
        if handle.container_id and handle.container_id in all_ids and handle.container_id not in owned:
            raise BlockedCleanup('container ownership mismatch')
        if not handle.container_id and owned:
            if len(owned) != 1:
                raise BlockedCleanup('unexpected policy container count')
            handle.container_id = owned[0]
            handle.uncertain = False
            self._save(handle)
        if handle.uncertain:
            raise BlockedCleanup('create outcome unknown; retain intent and retry reconciliation before any new start')
        for cid in dict.fromkeys([*owned, *([handle.container_id] if handle.container_id in all_ids else [])]):
            item = self._inspect([cid], deadline)[0]
            self._verify(item, handle)
            if item['State'].get('Running'):
                grace = max(0, min(5, int(remaining(deadline) / 2)))
                try:
                    self._run(['stop', '--time', str(grace), cid], deadline)
                except Exception:
                    forced = True
                    self._run(['kill', cid], deadline)
                item = self._inspect([cid], deadline)[0]
                forced |= item['State'].get('ExitCode') in (137, 143)
            if item['State'].get('Running') or item['State'].get('Dead'):
                raise BlockedCleanup('container exit not confirmed')
            self._run(['rm', cid], deadline)
        if self._ids(handle.labels(), deadline) or handle.container_id in self._ids({}, deadline):
            raise BlockedCleanup('container removal not confirmed')
        networks = self._run(['network','ls','-q','--filter',f'label=com.docker.compose.project={handle.project}'], deadline).split()
        if networks:
            self._run(['network','rm',*networks], deadline)
        self.check_daemon(deadline)
        handle.cleanup_forced = forced
        handle.phase = 'removed'
        self._save(handle)
        (self.state_dir / (handle.project + '.compose.json')).unlink(missing_ok=True)
        self.cleanup_forced |= forced
        return ContainerExitProof(handle.deployment_id, handle.daemon_id, handle.policy_id, handle.run_id,
            handle.generation, handle.container_id, handle.host_instance_id, True, forced, self.authority)

    def cleanup_owned(self, deadline):
        errors = []
        for handle in self.reconcile_owned(deadline):
            try:
                self.stop_and_remove(handle, deadline)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            self.phase = 'BLOCKED_CLEANUP'
            raise BlockedCleanup('; '.join(errors))

    def record_pool(self, pool_id):
        import re
        if not re.fullmatch(r'[0-9a-f]{12}',pool_id):
            raise ValueError('invalid shared image pool identity')
        atomic_json(self.state_dir / f'pool-{pool_id}.json', dict(version=3, pool_id=pool_id,
            deployment_id=self.context['deployment_id'], daemon_id=self.daemon_id,
            run_id=self.run_id, anchor_id=self.anchor_id, anchor_epoch=self.anchor_epoch))

    def forget_pool(self, pool_id):
        (self.state_dir / f'pool-{pool_id}.json').unlink(missing_ok=True)

    def pool_records(self):
        import re
        records=[]
        for path in self.state_dir.glob('pool-*.json'):
            data=json.loads(path.read_text())
            if (data.get('version')!=3 or data.get('deployment_id')!=self.context['deployment_id'] or
                    data.get('daemon_id')!=self.daemon_id or not isinstance(data.get('pool_id'),str) or
                    not re.fullmatch(r'[0-9a-f]{12}',data['pool_id']) or
                    path.name!=f"pool-{data['pool_id']}.json"):
                raise BlockedCleanup('image pool journal ownership mismatch')
            records.append((path,data))
        return records

    def recover_pools(self, deadline):
        # Invoked only after container and external-owner cleanup. Old RC pools
        # otherwise survive SIGKILL when the shareable anchor is kept alive.
        import mmap
        import re
        import posix_ipc
        from rc_policy_sdk.images import _HEADER
        from .external import ExternalResources
        for path,data in self.pool_records():
            remaining(deadline)
            if data['anchor_id']!=self.anchor_id or data.get('anchor_epoch')!=self.anchor_epoch:
                if data['anchor_id'] in self._ids({},deadline):
                    raise BlockedInfrastructure('old image pool anchor remains; recreate RC plus anchor')
                path.unlink()
                continue
            if not Path('/dev/shm').is_dir():
                raise BlockedCleanup('pool recovery requires the original Linux IPC namespace')
            descriptors=[]
            for entry in Path('/dev/shm').iterdir():
                remaining(deadline)
                if not re.fullmatch('rc'+data['pool_id']+r'[0-9a-f]{10}',entry.name):
                    continue
                segment=posix_ipc.SharedMemory('/'+entry.name,read_only=True)
                try:
                    if segment.size<_HEADER.size:
                        raise BlockedCleanup('invalid orphan image segment')
                    with mmap.mmap(segment.fd,_HEADER.size,access=mmap.ACCESS_READ) as mapping:
                        size,frame=_HEADER.unpack(mapping[:])
                    descriptors.append(dict(shm_name='/'+entry.name,pool_id=data['pool_id'],nbytes=size,frame_id=frame))
                finally:
                    segment.close_fd()
            ExternalResources._reclaim_segments(descriptors,deadline)
            path.unlink()

    def finish_pool_namespace_cleanup(self, removed_anchors):
        for path,data in self.pool_records():
            if data['anchor_id'] not in removed_anchors:
                raise BlockedCleanup('image pool anchor removal unconfirmed')
            path.unlink()

    def snapshot(self):
        return dict(phase=self.phase, target=self.target, error=self.error,
                    cleanup_forced=self.cleanup_forced, run_id=self.run_id)
