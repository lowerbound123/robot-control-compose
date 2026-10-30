import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool
from robot_control.process import PolicyProcess


def wait_for(callback, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = callback()
        if result: return result
        time.sleep(.01)
    raise AssertionError('host did not complete operation')


class Host:
    def __init__(self, root, identity=None, *, source_policy_id=None):
        identity = identity or next(p.parent.name for p in Path(root).glob('*/main.py'))
        assert importlib.util.find_spec('rc_policy_sdk.host') is not None, 'policy host is missing'
        self.temp = tempfile.TemporaryDirectory(prefix='rch-', dir='/tmp')
        self.socket = str(Path(self.temp.name) / 'h.sock')
        self.log = open(Path(self.temp.name) / 'host.log', 'w+')
        source_args = ['--source-policy-id', source_policy_id] if source_policy_id is not None else []
        self.process = subprocess.Popen([sys.executable, '-m', 'rc_policy_sdk.host',
            '--socket', self.socket, '--policy-root', str(root), '--host-id', identity, *source_args],
            stdout=self.log, stderr=subprocess.STDOUT)
        def listening():
            if self.process.poll() is not None:
                return True
            try:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(.1)
                    probe.connect(self.socket)
                return True
            except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
                return False
        wait_for(listening)
        if self.process.poll() is not None:
            self.log.seek(0)
            raise AssertionError(self.log.read())

    def close(self):
        self.process.terminate()
        try: self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait()
        self.log.close()
        self.temp.cleanup()


SOURCE = '''
import os, time
from .helper import VALUE
def initialize(config): return {'calls':0, 'config':config}
def policy(context,state,action_chunk,images,config):
    assert not images['head'].flags.writeable
    context['calls'] += 1
    if config['params'].get('crash'): os._exit(23)
    if config['params'].get('retain'): context['image'] = images['head']
    time.sleep(config['params'].get('delay', 0))
    return [[VALUE,context['calls'],int(images['head'].sum())]+[0]*11]
def reset_context(context): context['calls']=0
def release(context):
    if context: time.sleep(context['config']['params'].get('release_delay', 0))
'''


def test_two_numbered_hosts_load_same_source_with_independent_contexts(tmp_path):
    source_id = 'a'
    folder = tmp_path / source_id
    folder.mkdir()
    (folder / 'main.py').write_text('''
import os
from pathlib import Path
def initialize(config): return {'id': config['id'], 'calls': 0}
def policy(context, state, action_chunk, images, config):
    context['calls'] += 1
    return [[context['calls']] * 14]
def reset_context(context): context['calls'] = 0
def release(context): pass
def info(context): return {**context, 'pid': os.getpid(), 'source': Path(__file__).parent.name}
''')
    hosts, workers = [], []
    pool = ImagePool(cameras=('head',))
    pool.publish('head', np.zeros((2, 2), np.uint8), 1.)
    lease = pool.acquire()
    try:
        for suffix in ('-0', '-1'):
            identity = source_id + suffix
            host = Host(tmp_path, identity, source_policy_id=source_id)
            hosts.append(host)
            config = dict(schema_version=3, id=identity, params={},
                          execution=dict(mode='sync', call_every_actions=1, control_mode='qpos'),
                          tui_commands={'info': {'handler': 'info', 'args': []}})
            worker = PolicyProcess(folder, config, host_socket=host.socket,
                                   init_timeout_s=3, inference_timeout_s=2, release_timeout_s=.2)
            workers.append(worker)
            worker.start()
        assert workers[0].host.instance_id != workers[1].host.instance_id
        assert workers[0].pid != workers[1].pid
        for index, expected in ((0, 1), (0, 2), (1, 1)):
            worker = workers[index]
            worker.submit(dict(request_id=expected, generation=1, config_version=1,
                               state={'qpos': [0] * 14}, images=lease.descriptors, action_chunk=[]))
            result = wait_for(worker.poll)
            assert result['type'] == 'result', result
            assert result['actions'] == [[expected] * 14]
        for index, worker in enumerate(workers):
            worker.submit_command('info', 'info', [])
            message = wait_for(worker.poll_command)
            if message['type'] == 'command_started': message = wait_for(worker.poll_command)
            assert message['result'] == {'id': source_id + f'-{index}', 'calls': 2 if index == 0 else 1,
                                         'pid': worker.pid, 'source': source_id}
    finally:
        for worker in workers: worker.close()
        for host in hosts: host.close()
        lease.release()
        pool.close()


@pytest.fixture
def host_case(tmp_path):
    root = tmp_path / 'policies'; root.mkdir()
    for identity, value in [('a', 11)]:
        folder = root / identity; folder.mkdir()
        (folder / 'main.py').write_text(SOURCE)
        (folder / 'main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
        (folder / 'helper.py').write_text(f'VALUE={value}\n')
    host = Host(root)
    pool = ImagePool(cameras=('head',)); pool.publish('head', np.ones((2, 2), np.uint8), 1.)
    lease = pool.acquire()
    workers = []
    def worker(identity, **params):
        config = dict(schema_version=3, id=identity,  params=params,
            execution=dict(mode='sync',call_every_actions=1,control_mode='qpos'), hot_reloadable=[])
        obj = PolicyProcess(root/identity, config, host_socket=host.socket,
            init_timeout_s=3, inference_timeout_s=.4, release_timeout_s=.2)
        workers.append(obj)
        obj.start()
        return obj
    def call(obj, serial=1):
        obj.submit(dict(request_id=serial,generation=1,config_version=1,
            state={'qpos':[0]*14},action_chunk=[],images=lease.descriptors))
        return wait_for(obj.poll)
    try: yield host, worker, call, workers
    finally:
        for obj in workers:
            try: obj.close()
            except Exception: pass
        host.close(); lease.release(); pool.close()


@pytest.mark.skipif(sys.version_info < (3, 14), reason='local helper needs Python 3.14; cross-container grammar checked by Docker integration')
def test_preflight_accepts_new_python_syntax_without_importing_module(tmp_path):
    root = tmp_path/'policies'; root.mkdir(); folder=root/'a'; folder.mkdir()
    (folder/'main.py').write_text('TEMPLATE = t"Python 3.14"\nraise RuntimeError("must not import")\n'+SOURCE)
    (folder/'main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    host = Host(root)
    try:
        from robot_control.host_client import HostClient
        client = HostClient.get(host.socket, 'a')
        assert client.preflight('a', timeout=2)['valid']
        client.disconnect()
    finally: host.close()


def test_lost_connection_keeps_readers_unconfirmed_until_explicit_cleanup(host_case):
    import socket
    _, worker, _, _ = host_case
    a=worker('a',delay=10)
    # A transport close is deliberately not an exit proof, even if the host
    # starts cleaning the old runtime as soon as it notices EOF.
    a.host.connection.shutdown(socket.SHUT_RDWR)
    wait_for(lambda:not a.runnable)
    assert a.has_readers and not a.cleanup_confirmed
    a.close()
    assert a.cleanup_confirmed and not a.has_readers


def test_supervisor_can_stop_runtime_while_large_dispatch_is_backpressured(host_case):
    import signal
    _, worker, _, _ = host_case
    a=worker('a')
    os.kill(a.pid,signal.SIGSTOP)
    try:
        a.host.enqueue({'type':'dispatch','runtime_id':a.runtime_id,'session_id':a.session_id,
            'event':{'type':'observe','request_id':1,'config_version':1,'state':{'padding':'x'*(3*1024*1024)},'images':{}}})
        time.sleep(.1)
        start=time.monotonic()
        a.close()
        assert time.monotonic()-start<7 and a.cleanup_confirmed
    finally:
        try:os.kill(a.pid,signal.SIGCONT)
        except ProcessLookupError:pass


@pytest.mark.parametrize('closed', [False, True])
def test_full_runtime_queue_cannot_bypass_tree_cleanup(tmp_path, monkeypatch, closed):
    import queue
    from unittest.mock import Mock
    from rc_policy_sdk import host as host_module
    monkeypatch.setattr(host_module.PolicyHost, '_watch', lambda self: None)
    host = host_module.PolicyHost(tmp_path, 'test')
    host.shutting_down = True
    host.runtime_id = 'runtime'
    host.process = Mock(pid=os.getpid())
    host.process.poll.return_value = None
    host.runtime = Mock()
    host.runtime_outbox = queue.Queue(1)
    host.runtime_outbox.put(b'blocked')
    host.sessions['a'] = dict(closed=closed, release_error=None)
    cleanup = Mock(return_value=True)
    monkeypatch.setattr(host_module, '_cleanup', cleanup)
    monkeypatch.setattr(host_module, 'descendant_snapshot', lambda _: {})
    monkeypatch.setattr(host, '_broadcast', lambda _: None)
    result = host.stop('runtime', timeout=.1)
    cleanup.assert_called_once()
    assert result['cleanup_confirmed'] and result['forced']


def test_term_during_hanging_release_exits_nonzero_and_cleans_runtime(host_case):
    host, worker, _, _ = host_case
    a = worker('a', release_delay=30)
    host.process.terminate()
    assert host.process.wait(10) != 0
    from rc_policy_sdk.lifecycle import process_birth
    wait_for(lambda: process_birth(a.pid) is None)
    wait_for(lambda: not a.runnable)
    # SIGTERM may close the supervisor before its final queued proof reaches
    # RC. OS cleanup must succeed, but RC must quarantine readers if ACK is lost.
    if not a.cleanup_confirmed:
        assert a.has_readers
        with pytest.raises((OSError, RuntimeError)):
            a.close()
        assert not a.cleanup_confirmed
    assert not Path(host.socket).exists()
