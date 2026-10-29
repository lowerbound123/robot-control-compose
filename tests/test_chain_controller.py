from config_helpers import read_config
from project_templates import template_path, copy_templates
import copy
import json
from pathlib import Path
import sys

import pytest

from robot_control.controller import RobotController
from host_helpers import LocalPolicyProcess as PolicyProcess
from robot_control.robots import MockRobot
from test_chain_config import add_chain, add_policy
from test_controller import until


SOURCE = '''
import time
def initialize(config): return {'config':config, 'calls':0}
def policy(context,state,action_chunk,images,config):
    time.sleep(config['params'].get('delay', 0))
    context['calls'] += 1
    return [state['qpos']] * 10
def reset_context(context): context['calls'] = 0
def release(context): pass
def test(context): return context['config']['id']
def change(context, value): return {'params.value':value}
'''


def operation(controller, identity, expected='succeeded'):
    until(lambda: controller.get_operation(identity)['status'] in ('succeeded','failed','cancelled'), 8)
    value = controller.get_operation(identity)
    assert value['status'] == expected, value
    return value


@pytest.fixture
def native_controller(tmp_path):
    root = tmp_path / 'policy'
    root.mkdir()
    for identity in ('a','b'):
        config = add_policy(root, identity, commands={
            'test': {'handler':'test', 'args':[]},
            'change': {'handler':'change', 'args':['int'], 'effect':'reload_policy'},
        })
        config['hot_reloadable'] = []
        (root / identity / 'policy.yaml').write_text(json.dumps(config))
        (root / identity / 'main.py').write_text(SOURCE)
        (root / identity / 'main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    add_chain(root, ['a','b','a'])
    config = read_config(template_path(Path(__file__).resolve().parents[1] / 'rc.yaml'))
    config.update(policy_directory='policy', startup_policy='default_chain', outputs_directory='outputs', frequency_hz=100)
    config['timeouts'] = {'initialize_s':5, 'inference_s':3, 'release_s':.5}
    path = tmp_path / 'rc.yaml'
    path.write_text(json.dumps(config))
    children = []
    def factory(*args, **kwargs):
        child = PolicyProcess(*args, python_command=[sys.executable], **kwargs)
        children.append(child)
        return child
    controller = RobotController(path, robot=MockRobot(), process_factory=factory)
    controller.start()
    try:
        until(lambda: controller.status()['state'] in ('paused','fault'), 8)
        assert controller.status()['state'] == 'paused', list(controller.events.queue)
        yield controller, children, tmp_path
    finally:
        controller.submit('exit')
        assert controller.done.wait(10)
        assert not any(child.alive for child in children)


def test_native_chain_start_edit_save_apply_and_invalid_draft(native_controller):
    controller, children, root = native_controller
    assert len(children) == 2
    operation(controller, controller.submit('resume'))
    until(lambda: controller.status()['consumed'] >= 2)
    opened = operation(controller, controller.manage_policy('open'))['result']
    assert controller.status()['state'] == 'paused'
    assert opened['policies'] == ['a','b','a']
    token = opened['token']
    worker = controller._worker
    operation(controller, controller.manage_policy('save', ['b','a'], token=token))
    assert controller._worker is worker
    assert controller.registry.get('default_chain').config['policies'] == ['a','b','a']
    assert read_config(root / 'policy/default_chain/policy.yaml')['policies'] == ['b','a']
    operation(controller, controller.manage_policy('apply', [], token=token), 'failed')
    assert controller._worker is worker and worker.alive
    assert controller.status()['state'] == 'paused'
    operation(controller, controller.manage_policy('apply', ['b','a'], token=token))
    assert controller.status()['state'] == 'paused'
    assert controller._worker is not worker and not worker.alive
    assert len(children) == 4
    assert controller.registry.get('default_chain').config['policies'] == ['b','a']


def test_disconnect_can_still_shutdown_controller_and_confirm_reader_exit(native_controller):
    import socket
    controller,children,_=native_controller
    operation(controller,controller.submit('resume'))
    until(lambda:controller.status()['in_flight'])
    children[0].host.connection.shutdown(socket.SHUT_RDWR)
    controller.submit('exit')
    assert controller.done.wait(12)
    assert all(child.cleanup_confirmed for child in children)
    assert controller._pool._bytes==0


def test_chain_commands_and_member_reload_and_explicit_save(native_controller):
    controller, children, root = native_controller
    for name, expected in [('a-test','a'), ('b-test','b')]:
        assert operation(controller, controller.submit(name))['result'] == expected
    old = controller._worker
    operation(controller, controller.submit('a-change 9'))
    assert controller.status()['state'] == 'paused'
    assert not old.alive and len(children) == 4
    assert controller.registry.get('a').config['params']['value'] == 9
    assert controller.registry.get('b').config['params']['value'] == 1
    operation(controller, controller.submit('save-config policy a'))
    assert read_config(root / 'policy/a/policy.yaml')['params']['value'] == 9
    operation(controller, controller.submit('set policy.params.value 10'), 'failed')


def test_edit_credentials_invalidated_by_switch_and_mcp_rejected(native_controller):
    controller, _, _ = native_controller
    token = operation(controller, controller.manage_policy('open'))['result']['token']
    operation(controller, controller.submit('switch a'))
    with pytest.raises(RuntimeError, match='stale'):
        controller.manage_policy('apply', ['b'], token=token)
    with pytest.raises(PermissionError):
        controller.submit('manage-policy', origin='mcp')


def test_chain_uses_head_execution_and_saved_draft_survives_restart(native_controller):
    controller, _, root = native_controller
    original = controller.registry.get('a')
    changed = copy.deepcopy(original.config)
    changed['execution'].update(mode='sync', call_every_actions=4)
    controller.registry.commit('a', changed)
    token = operation(controller, controller.manage_policy('open'))['result']['token']
    operation(controller, controller.manage_policy('apply', ['a','b'], token=token))
    assert controller.registry.get('default_chain').execution['mode'] == 'sync'
    operation(controller, controller.submit('resume'))
    until(lambda: controller.status()['consumed'] >= 4)
    operation(controller, controller.manage_policy('save', ['b','a','b'], token=token))
    from robot_control.config import PolicyRegistry
    restarted = PolicyRegistry(root / 'policy').get('default_chain')
    assert restarted.config['policies'] == ['b','a','b']
    assert restarted.execution['mode'] == 'async'


@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_controller_schedules_and_clips_once_per_whole_chain(native_controller, mode, monkeypatch):
    controller, _, _ = native_controller
    for identity in ('a', 'b'):
        config = copy.deepcopy(controller.registry.get(identity).config)
        config['params']['delay'] = .035
        config['execution'].update(mode=mode, call_every_actions=3)
        controller.registry.commit(identity, config)
    token = operation(controller, controller.manage_policy('open'))['result']['token']
    operation(controller, controller.manage_policy('apply', ['a','b','a'], token=token))
    accepted = []
    real_accept = controller.buffer.accept
    def accept(request, actions):
        before = controller.buffer.consumed
        result = real_accept(request, actions)
        accepted.append((request.consumed, before, len(actions.actions), controller.buffer.last_trim))
        return result
    monkeypatch.setattr(controller.buffer, 'accept', accept)
    operation(controller, controller.submit('resume'))
    until(lambda: len(accepted) >= 2, 8)
    operation(controller, controller.submit('pause'))
    assert all(length == 10 and trim == end - start for start, end, length, trim in accepted)
    if mode == 'sync':
        assert all(trim == 0 for _, _, _, trim in accepted)
        assert accepted[1][0] == 3
    else:
        assert accepted[1][0] == 3
        assert accepted[1][3] > 0


def test_editor_waiting_application_is_invalidated_by_later_switch(native_controller, monkeypatch):
    import threading
    controller, _, _ = native_controller
    token = operation(controller, controller.manage_policy('open'))['result']['token']
    entered, finish = threading.Event(), threading.Event()
    original = controller.registry.propose_chain
    def blocked(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(controller.registry, 'propose_chain', blocked)
    pending = controller.manage_policy('apply', ['b','a'], token=token)
    assert entered.wait(5)
    switch = controller.submit('switch b')
    finish.set()
    operation(controller, pending, 'cancelled')
    operation(controller, switch)
    assert controller.status()['policy'] == 'b'


def test_cold_set_of_single_policy_invalidates_open_editor(native_controller):
    controller, _, _ = native_controller
    operation(controller, controller.submit('switch a'))
    token = operation(controller, controller.manage_policy('open'))['result']['token']
    operation(controller, controller.submit('set policy.params.value 2'))
    with pytest.raises(RuntimeError, match='stale'):
        controller.manage_policy('apply', ['b'], token=token)
