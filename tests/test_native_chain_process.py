import json
import sys
import threading
import time

import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool
from robot_control.config import PolicyRegistry
from host_helpers import LocalPolicyProcess as PolicyProcess
from test_chain_config import add_chain, add_policy


SOURCE = '''
import json, time
def event(config, value):
    with open(config['params']['events'], 'a') as f: f.write(json.dumps([config['id'], value]) + '\\n')
def initialize(config):
    event(config, 'initialize')
    if config['params'].get('fail_init'): raise ValueError('load failed')
    return {'config':config, 'calls':0}
def policy(context,state,action_chunk,images,config,*,runtime):
    context['calls'] += 1
    event(config, 'policy')
    time.sleep(config['params'].get('delay', 0))
    if config['params'].get('fail'): raise ValueError('model failed')
    assert runtime.consumed_actions == 7
    assert config is context['config']
    assert not images['head'].flags.writeable
    mode = config['execution']['control_mode']
    rows = action_chunk['actions'] if mode == 'mixed' else action_chunk
    row = rows[0] if rows else state['qpos']
    row = [v * 10 if config['id'] == 'b' else v + context['calls'] for v in row]
    return {'actions':[row], 'modes':['qpos']} if mode == 'mixed' else [row]
def reset_context(context):
    context['calls'] = 0
    event(context['config'], 'reset')
def release(context):
    if context is not None: event(context['config'], 'release')
def test(context): return [context['config']['id'], context['calls']]
def delay(context, seconds): time.sleep(seconds)
def observe(context, state, images, config, *, runtime): event(config, 'observe')
'''


def wait_for(function, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(.005)
    pytest.fail('timed out')


@pytest.fixture
def setup_chain(tmp_path):
    def build(*, mode='qpos', delays=(0, 0), fail=None, fail_init=None, timeout=3):
        events = tmp_path / 'events'
        for index, identity in enumerate(('a', 'b')):
            config = add_policy(tmp_path, identity, mode=mode, commands={
                'test': {'handler':'test', 'args':[], 'timeout_s':1},
                'delay': {'handler':'delay', 'args':['float'], 'timeout_s':.05},
            })
            config['params'].update(events=str(events), delay=delays[index], fail=identity == fail, fail_init=identity == fail_init)
            (tmp_path / identity / 'policy.json').write_text(json.dumps(config))
            (tmp_path / identity / 'main.py').write_text(SOURCE)
            (tmp_path / identity / 'main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
        add_chain(tmp_path, ['a', 'b', 'a'])
        registry = PolicyRegistry(tmp_path)
        registry.get('a').version = 3
        registry.get('b').version = 5
        record = registry.get('default_chain')
        from robot_control.chain_process import ChainProcess
        children = []
        def factory(*args, **kwargs):
            child = PolicyProcess(*args, python_command=[sys.executable], **kwargs)
            children.append(child)
            return child
        worker = ChainProcess(record, process_factory=factory, init_timeout_s=4,
                              inference_timeout_s=timeout, release_timeout_s=.3)
        return worker, children, events
    return build


@pytest.fixture
def frames(tmp_path):
    pool = ImagePool(tmp_path / 'frames', cameras=('head',))
    pool.publish('head', np.zeros((2, 2, 3), np.uint8), 1.)
    lease = pool.acquire()
    yield {'request_id':1, 'generation':2, 'config_version':1,
           'state':{'qpos':[0]*14}, 'images':lease.descriptors, 'action_chunk':[],
           'runtime':{'consumed_actions':7}}
    lease.release()
    pool.close()


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize('mode', ['qpos', 'epos', 'mixed'])
def test_real_chain_reuses_workers_and_maps_versions_and_commands(setup_chain, frames, mode):
    worker, children, events = setup_chain(mode=mode)
    if mode == 'mixed': frames['action_chunk'] = {'actions':[], 'modes':[]}
    try:
        worker.start()
        assert len(children) == 2
        assert [child.config['id'] for child in children] == ['a', 'b']
        worker.submit(frames)
        result = wait_for(worker.poll)
        assert result['type'] == 'result', result
        assert result['actions'] == [[12.]*14]
        assert result['config_version'] == 1 and result['generation'] == 2
        assert [item[0] for item in rows(events) if item[1] == 'policy'] == ['a','b','a']
        for name, expected in [('a-test',['a',2]), ('b-test',['b',1])]:
            worker.submit_command(name, name, [])
            message = wait_for(worker.poll_command)
            if message['type'] == 'command_started': message = wait_for(worker.poll_command)
            assert message['result'] == expected
        wait_for(lambda: worker.supports_observe)
        assert worker.observe(frames)
        assert wait_for(worker.poll_observation)['type'] == 'observed'
        worker.reset_context()
        worker.submit({**frames, 'request_id':2})
        assert wait_for(worker.poll)['actions'] == [[12.]*14]
    finally:
        worker.close()
    assert sorted(item[0] for item in rows(events) if item[1] == 'release') == ['a','b']
    assert sorted(item[0] for item in rows(events) if item[1] == 'observe') == ['a','b']
    assert not worker.alive
    worker.close()


def test_pause_finishes_current_reader_without_starting_next_stage(setup_chain, frames):
    worker, _, events = setup_chain(delays=(.15, 0))
    try:
        worker.start()
        worker.submit(frames)
        wait_for(lambda: ['a','policy'] in rows(events))
        worker.cancel_inference()
        assert wait_for(worker.poll)['type'] == 'cancelled'
        assert ['b','policy'] not in rows(events)
        worker.reset_context()
        worker.submit({**frames,'request_id':2})
        assert wait_for(worker.poll)['actions'] == [[12.]*14]
    finally:
        worker.close()


@pytest.mark.parametrize('failure', ['timeout', 'model'])
def test_whole_chain_deadline_and_stage_failure_short_circuit(setup_chain, frames, failure):
    worker, _, events = setup_chain(delays=(.10,.10) if failure == 'timeout' else (0,0),
                                   fail='b' if failure == 'model' else None,
                                   timeout=.16 if failure == 'timeout' else 3)
    try:
        worker.start()
        worker.submit(frames)
        result = wait_for(worker.poll)
        assert result['type'] == 'error', result
        assert 'b' in result['error'] and 'cell 2' in result['error']
        assert 'timeout' in result['error'] if failure == 'timeout' else 'model failed' in result['error']
    finally:
        worker.close()
    assert rows(events).count(['a','policy']) == 1


def test_partial_initialization_failure_closes_all_workers(setup_chain):
    worker, children, events = setup_chain(fail_init='b')
    with pytest.raises(RuntimeError, match='b.*load failed'):
        worker.start()
    worker.close()
    assert not any(child.alive for child in children)
    assert rows(events).count(['a','release']) == 1


def test_command_timeout_is_fatal_for_aggregate(setup_chain):
    worker, _, _ = setup_chain()
    try:
        worker.start()
        worker.submit_command('sleep', 'a-delay', [.3])
        message = wait_for(worker.poll_command)
        if message['type'] == 'command_started': message = wait_for(worker.poll_command)
        assert message['type'] == 'command_timeout'
        assert message['fatal'] is True
        with pytest.raises(RuntimeError): worker.submit_command('test', 'test', [])
    finally:
        worker.close()


def test_observation_error_waits_for_every_image_reader(setup_chain, frames, tmp_path):
    worker, _, events = setup_chain()
    gate = tmp_path / 'observation-finished'
    observer = '''
from pathlib import Path
def observe(context, state, images, config, *, runtime):
    event(config, 'observe-start')
    if config['id'] == 'b':
        while not Path(GATE).exists():
            assert int(images['head'][0,0,0]) == 0
            time.sleep(.005)
    event(config, 'observe-done')
    if config['id'] == 'a': raise ValueError('observer failed')
'''.replace('GATE', repr(str(gate)))
    for member in worker.record.members:
        (member.directory / 'main.py').write_text(SOURCE + observer)
        (member.directory / 'main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    try:
        worker.start()
        wait_for(lambda: worker.supports_observe)
        assert worker.observe(frames)
        wait_for(lambda: ['a','observe-done'] in rows(events) and ['b','observe-start'] in rows(events))
        assert worker.poll_observation() is None, 'failed A cannot acknowledge live B image reader'
        gate.touch()
        response = wait_for(worker.poll_observation)
        assert response['type'] == 'observation_error' and 'a' in response['error']
        assert ['b','observe-done'] in rows(events)
    finally:
        gate.touch()
        worker.close()


def test_external_resource_rpc_namespace_and_owner_are_aggregate(setup_chain, frames):
    worker, _, _ = setup_chain()
    messages = []
    class Resources:
        def handle(self, owner, message):
            assert owner is worker
            messages.append(dict(message))
            return {'retained':True}
    worker.resources = Resources()
    rpc_source = '''
def initialize(config, *, runtime):
    runtime.register_remote('same-id', 'http://localhost:1', config['id'], 'model')
    return config
def policy(context, state, action_chunk, images, config, *, runtime):
    runtime.retain_remote('same-id', 'inference', images.descriptors)
    runtime.release_images('same-id', 'inference')
    return [state['qpos']]
def reset_context(context): pass
def release(context): pass
'''
    for member in worker.record.members:
        (member.directory / 'main.py').write_text(SOURCE + rpc_source)
        (member.directory / 'main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    try:
        worker.start()
        worker.submit(frames)
        assert wait_for(worker.poll)['type'] == 'result'
        registered = [msg['remote_id'] for msg in messages if msg['method'] == 'register']
        assert len(set(registered)) == 2
        assert [msg['remote_id'] for msg in messages if msg['method'] == 'retain'] == [registered[0],registered[1],registered[0]]
        assert [msg['remote_id'] for msg in messages if msg['method'] == 'released'] == [registered[0],registered[1],registered[0]]
    finally:
        worker.close()


def test_cancel_during_second_initialize_cleans_first_and_current_worker(setup_chain, tmp_path):
    worker, children, _ = setup_chain()
    entered = tmp_path / 'initializing'
    second = worker.record.members[1].directory / 'main.py'
    second.write_text(SOURCE + f'''\nfrom pathlib import Path
def initialize(config):
    Path({str(entered)!r}).touch()
    time.sleep(20)
''')
    cancel = threading.Event()
    errors = []
    def start():
        try: worker.start(cancel_event=cancel)
        except Exception as exc: errors.append(exc)
    thread = threading.Thread(target=start)
    thread.start()
    try:
        wait_for(entered.exists)
        cancel.set()
        thread.join(8)
        assert not thread.is_alive() and errors
        assert not any(child.alive for child in children)
    finally:
        cancel.set()
        worker.close()
        thread.join(8)


def test_start_twice_is_rejected_without_replacing_live_workers(setup_chain):
    worker, children, _ = setup_chain()
    try:
        worker.start()
        pids = [child.pid for child in children]
        with pytest.raises(RuntimeError, match='start'):
            worker.start()
        assert [child.pid for child in children] == pids
    finally:
        worker.close()
        # Also clean up the original workers if this regression is reproduced.
        for child in children:
            child.close()


def test_legacy_qpos_worker_result_without_modes_remains_supported(tmp_path, frames):
    from robot_control.chain_process import ChainProcess
    root = tmp_path / 'legacy-policy'
    root.mkdir()
    add_policy(root, 'legacy')
    record = PolicyRegistry(root).propose_chain(['legacy'])
    class LegacyChild:
        supports_observe = False
        release_error = None
        def __init__(self, directory, config, version, **kwargs):
            self.alive = False
            self.config_version = version
            self.response = None
        def start(self, cancel_event=None): self.alive = True
        def submit(self, request):
            self.response = {**{key:request[key] for key in ('request_id','generation','config_version')},
                             'type':'result','images_released':True, 'actions':[[.5]*14]}
        def poll(self):
            result, self.response = self.response, None
            return result
        def close(self): self.alive = False; return False
    worker = ChainProcess(record, process_factory=LegacyChild)
    try:
        worker.start()
        worker.submit(frames)
        result = wait_for(worker.poll)
        assert result['type'] == 'result', result
        assert result['actions'] == [[.5]*14] and result['modes'] == ['qpos']
    finally:
        worker.close()
