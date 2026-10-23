import importlib.util
import json
import os
from pathlib import Path
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
    def __init__(self, root, identity='test'):
        assert importlib.util.find_spec('rc_policy_sdk.host') is not None, 'policy host is missing'
        self.temp = tempfile.TemporaryDirectory(prefix='rch-', dir='/tmp')
        self.socket = str(Path(self.temp.name) / 'h.sock')
        self.log = open(Path(self.temp.name) / 'host.log', 'w+')
        self.process = subprocess.Popen([sys.executable, '-m', 'rc_policy_sdk.host',
            '--socket', self.socket, '--policy-root', str(root), '--host-id', identity],
            stdout=self.log, stderr=subprocess.STDOUT)
        wait_for(lambda: Path(self.socket).exists() or self.process.poll() is not None)
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


@pytest.fixture
def host_case(tmp_path):
    root = tmp_path / 'policies'; root.mkdir()
    for identity, value in [('A', 11), ('B', 22)]:
        folder = root / identity; folder.mkdir()
        (folder / 'main.py').write_text(SOURCE)
        (folder / 'helper.py').write_text(f'VALUE={value}\n')
    host = Host(root)
    pool = ImagePool(cameras=('head',)); pool.publish('head', np.ones((2, 2), np.uint8), 1.)
    lease = pool.acquire()
    workers = []
    def worker(identity, **params):
        config = dict(schema_version=2, id=identity, policy_host='test', params=params,
            execution=dict(mode='sync',call_every_actions=1,control_mode='qpos'), hot_reloadable=['params.delay'])
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


def test_two_policies_share_runtime_but_not_module_globals(host_case):
    _, worker, call, _ = host_case
    a, b = worker('A'), worker('B')
    assert a.pid == b.pid and a.pid != os.getpid()
    assert call(a)['actions'][0][:3] == [11,1,4]
    assert call(b)['actions'][0][:3] == [22,1,4]
    assert call(a, 2)['actions'][0][:3] == [11,2,4]
    assert a.runnable and b.runnable
    assert not a.close_group([b, a])
    assert a.cleanup_confirmed and b.cleanup_confirmed
    assert not a.has_readers and not b.has_readers


@pytest.mark.parametrize('failure', [{'delay':10}, {'crash':True}, {'retain':True}])
def test_fatal_runtime_failure_invalidates_all_sessions_with_exit_proof(host_case, failure):
    _, worker, call, _ = host_case
    a, b = worker('A', **failure), worker('B')
    response = call(a)
    assert response['type'] == 'error'
    wait_for(lambda: not b.runnable)
    wait_for(lambda: a.cleanup_confirmed and b.cleanup_confirmed)
    with pytest.raises(RuntimeError):
        b.reset_context()


def test_hanging_release_is_bounded_and_manual_reload_uses_new_runtime(host_case):
    _, worker, _, _ = host_case
    a = worker('A', release_delay=10)
    old_pid = a.pid
    start = time.monotonic()
    assert a.close()
    assert time.monotonic() - start < 7
    assert a.cleanup_confirmed
    replacement = worker('A')
    assert replacement.pid != old_pid


@pytest.mark.skipif(sys.version_info < (3, 14), reason='local helper needs Python 3.14; cross-container grammar checked by Docker integration')
def test_preflight_accepts_new_python_syntax_without_importing_module(tmp_path):
    root = tmp_path/'policies'; root.mkdir(); folder=root/'A'; folder.mkdir()
    (folder/'main.py').write_text('TEMPLATE = t"Python 3.14"\nraise RuntimeError("must not import")\n'+SOURCE)
    host = Host(root)
    try:
        from robot_control.host_client import HostClient
        client = HostClient.get(host.socket, 'test')
        assert client.preflight('A', timeout=2)['valid']
        client.disconnect()
    finally: host.close()


def test_lost_connection_keeps_readers_unconfirmed_until_explicit_cleanup(host_case):
    import socket
    _, worker, _, _ = host_case
    a=worker('A',delay=10)
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
    a=worker('A')
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
    host.sessions['A'] = dict(closed=closed, release_error=None)
    cleanup = Mock(return_value=True)
    monkeypatch.setattr(host_module, '_cleanup', cleanup)
    monkeypatch.setattr(host_module, 'descendant_snapshot', lambda _: {})
    monkeypatch.setattr(host, '_broadcast', lambda _: None)
    result = host.stop('runtime', timeout=.1)
    cleanup.assert_called_once()
    assert result['cleanup_confirmed'] and result['forced']


def test_term_during_hanging_release_exits_nonzero_and_cleans_runtime(host_case):
    host, worker, _, _ = host_case
    a = worker('A', release_delay=30)
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


def test_host_resolves_manifest_identity_independent_of_folder(tmp_path, monkeypatch):
    from rc_policy_sdk.host import PolicyHost
    monkeypatch.setattr(PolicyHost, '_watch', lambda self: None)
    folder = tmp_path / 'implementation'; folder.mkdir()
    (folder / 'policy.json').write_text(json.dumps({'id':'A'}))
    (folder / 'main.py').write_text(SOURCE)
    host = PolicyHost(tmp_path, 'test')
    assert host.preflight('A')['directory'] == str(folder)
    duplicate = tmp_path / 'duplicate'; duplicate.mkdir()
    (duplicate / 'policy.json').write_text(json.dumps({'id':'A'}))
    with pytest.raises(ValueError, match='ambiguous'):
        host.preflight('A')
