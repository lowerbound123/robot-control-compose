import socket
import threading

from rc_policy_sdk.worker import run_session
from rc_policy_sdk.wire import send_message, receive_message


def opted(config):
    return {**config, 'tui_commands': {'reload': {'handler':'reset_context', 'args':[], 'effect':'reinitialize_policy'}}}


def test_reinitialize_same_session_and_release_once(tmp_path):
    (tmp_path / 'main.py').write_text('''
from pathlib import Path
def initialize(config, *, runtime):
    Path(config["path"]).open("a").write("init:" + runtime.context_id + "\\n")
    return None
def validate_config(config):
    if config.get("invalid"): raise ValueError("invalid")
def policy(context, state, action_chunk, images, config): return [[0]*14]
def reset_context(context): pass
def release(context):
    with open(PATH, "a") as f: f.write("release\\n")
'''.replace('PATH', repr(str(tmp_path / 'log'))))
    left, right = socket.socketpair()
    left.settimeout(2)
    thread = threading.Thread(target=run_session, args=(right, tmp_path, 'reinit_test'))
    thread.start()
    config = {'path': str(tmp_path / 'log')}
    def send(event): send_message(left, event)
    def recv(): return receive_message(left)
    try:
        send(dict(type='initialize', config=opted(config), config_version=1, context_id='old', tui_command_effects_v2=['reinitialize_policy']))
        assert recv()['type'] == 'ready'
        assert recv()['type'] == 'capabilities'
        send(dict(type='reinit_validate', operation_id='v', config_version=1, context_id='old', config={**config, 'invalid':True}, new_config_version=2))
        rejected = recv()
        assert rejected['type'] == 'error' and rejected['error_type'] == 'candidate_rejected'
        send(dict(type='reinit_validate', operation_id='v2', config_version=1, context_id='old', config=config, new_config_version=2))
        assert recv()['type'] == 'ack'
        send(dict(type='reinit_release', operation_id='r', config_version=1, context_id='old'))
        assert recv()['type'] == 'ack'
        send(dict(type='reinit_initialize', operation_id='i', config_version=2, context_id='new', config=config, global_state=None))
        result = recv()
        assert result['type'] == 'ack' and result['context_id'] == 'new'
        assert result['observe'] is False and result['monitor'] is False
        send({'type':'shutdown'})
        assert recv()['type'] == 'closed'
        thread.join(2)
        assert (tmp_path / 'log').read_text().splitlines() == ['init:old', 'release', 'init:new', 'release']
    finally:
        left.close()
        thread.join(2)


def test_connector_switch_can_reinitialize_without_declared_commands(tmp_path):
    (tmp_path / 'main.py').write_text('''
def initialize(config): return object()
def policy(context, state, action_chunk, images, config): return [[0]*14]
def reset_context(context): pass
def release(context): pass
''')
    left, right = socket.socketpair()
    left.settimeout(2)
    thread = threading.Thread(target=run_session, args=(right, tmp_path, 'connector_reinit'))
    thread.start()
    try:
        send_message(left, dict(type='initialize', config={}, config_version=1,
                               context_id='old', connector_switch_v1=True))
        ready = receive_message(left)
        assert ready.get('connector_switch_v1') is True
        receive_message(left)
        send_message(left, dict(type='reinit_release', operation_id='r', config_version=1, context_id='old'))
        assert receive_message(left)['type'] == 'ack'
        send_message(left, dict(type='reinit_initialize', operation_id='i', config_version=2,
                               context_id='new', config={}))
        assert receive_message(left)['type'] == 'ack'
    finally:
        send_message(left, {'type': 'shutdown'})
        left.close()
        thread.join(2)


def test_runtime_old_context_cannot_access_new_resources():
    import pytest
    from rc_policy_sdk.runtime import PolicyRuntime
    class Channel:
        context_id = 'old'
        global_proxy = object()
        def check_globals(self): pass
        def rpc(self, method, **payload): return payload
    channel = Channel()
    old = PolicyRuntime(channel, {'context_id':'old'})
    assert old.release_images('remote', 'request')['context_id'] == 'old'
    channel.context_id = 'new'
    with pytest.raises(RuntimeError, match='context'):
        old.release_images('remote', 'request')
    with pytest.raises(RuntimeError, match='context'):
        old.globals


def test_host_advertises_reinitialize_and_sets_deadline(tmp_path):
    import time
    from rc_policy_sdk.host import PolicyHost
    class Peer: owner = None
    host = PolicyHost(tmp_path, 'a')
    peer = Peer()
    hello = host.dispatch(peer, dict(type='hello', owner_id='owner', protocol_version=1, image_protocol_version=1))
    assert hello['reinitialize_policy'] is True
    host.owner = 'owner'
    host.runtime_id = 'runtime'
    host.sessions['session'] = dict(closed=False, timeouts={'inference':10}, deadlines={}, commands={})
    host._send_runtime = lambda event: None
    before = time.monotonic()
    host.dispatch(peer, dict(type='dispatch', runtime_id='runtime', session_id='session', operation_timeout_s=.5,
                             event=dict(type='reinit_release', operation_id='op')))
    assert before + .5 <= host.sessions['session']['deadlines']['operation'] <= time.monotonic() + .5
    host.shutting_down = True


def test_failed_reinitialization_releases_none_once(tmp_path):
    import pytest
    (tmp_path / 'main.py').write_text('''
def initialize(config):
    if config.get("fail"): raise ValueError("failed")
    return None
def policy(context, state, action_chunk, images, config): return [[0]*14]
def reset_context(context): pass
def release(context):
    with open(PATH, "a") as f: f.write(repr(context) + "\\n")
'''.replace('PATH', repr(str(tmp_path / 'log'))))
    left, right = socket.socketpair()
    left.settimeout(2)
    thread = threading.Thread(target=run_session, args=(right, tmp_path, 'reinit_fail'))
    thread.start()
    try:
        send_message(left, dict(type='initialize', config=opted({}), config_version=1, context_id='old', tui_command_effects_v2=['reinitialize_policy']))
        assert receive_message(left)['type'] == 'ready'
        receive_message(left)
        send_message(left, dict(type='reinit_release', operation_id='r', config_version=1, context_id='old'))
        assert receive_message(left)['type'] == 'ack'
        send_message(left, dict(type='reinit_initialize', operation_id='i', config_version=2, context_id='new', config={'fail':True}))
        assert receive_message(left)['type'] == 'error'
        send_message(left, {'type':'shutdown'})
        assert receive_message(left)['type'] == 'closed'
        thread.join(2)
        assert (tmp_path / 'log').read_text().splitlines() == ['None', 'None']
    finally:
        left.close()
        thread.join(2)


def test_release_waits_for_active_monitor(tmp_path):
    import time
    (tmp_path / 'main.py').write_text('''
import time
from pathlib import Path
root = Path(ROOT)
def initialize(config): return None
def policy(context, state, action_chunk, images, config): return [[0]*14]
def reset_context(context): pass
def monitor(context):
    (root / "started").touch()
    while not (root / "go").exists(): time.sleep(.005)
    return {"ok": 1}
def release(context): (root / "released").touch()
'''.replace('ROOT', repr(str(tmp_path))))
    left, right = socket.socketpair()
    left.settimeout(2)
    thread = threading.Thread(target=run_session, args=(right, tmp_path, 'reinit_barrier'))
    thread.start()
    try:
        send_message(left, dict(type='initialize', config=opted({}), config_version=1, context_id='old', tui_command_effects_v2=['reinitialize_policy']))
        receive_message(left); receive_message(left)
        send_message(left, dict(type='monitor', monitor_id=1, monitor_epoch=1, config_version=1))
        deadline = time.monotonic() + 2
        while not (tmp_path / 'started').exists() and time.monotonic() < deadline: time.sleep(.005)
        assert (tmp_path / 'started').exists()
        send_message(left, dict(type='reinit_release', operation_id='r', config_version=1, context_id='old'))
        time.sleep(.05)
        assert not (tmp_path / 'released').exists()
        (tmp_path / 'go').touch()
        assert {receive_message(left)['type'], receive_message(left)['type']} == {'monitor_result', 'ack'}
        assert (tmp_path / 'released').exists()
        send_message(left, {'type':'shutdown'})
        assert receive_message(left)['type'] == 'closed'
    finally:
        (tmp_path / 'go').touch()
        left.close()
        thread.join(2)


def test_old_global_proxy_does_not_rebind(monkeypatch):
    import pytest
    from rc_policy_sdk import global_state
    from rc_policy_sdk.runtime import WorkerChannel
    class Memory:
        closed = False
        def snapshot(self): return 1, {'value': 1}
        def close(self): self.closed = True
    monkeypatch.setattr(global_state.SharedState, 'attach', lambda descriptor: Memory())
    left, right = socket.socketpair()
    channel = WorkerChannel(right)
    try:
        channel.attach_globals({'binding': {'context_id': 'old'}})
        old = channel.global_proxy
        assert old['value'] == 1
        channel.detach_globals()
        channel.attach_globals({'binding': {'context_id': 'new'}})
        with pytest.raises(RuntimeError, match='context'):
            old['value']
        with pytest.raises(RuntimeError, match='context'):
            old['value'] = 2
    finally:
        channel.detach_globals()
        left.close()
        right.close()
        channel.sender.close()


def test_release_exception_does_not_release_twice(tmp_path):
    (tmp_path / 'main.py').write_text('''
def initialize(config): return None
def policy(context, state, action_chunk, images, config): return [[0]*14]
def reset_context(context): pass
def release(context):
    with open(PATH, "a") as f: f.write("release\\n")
    raise ValueError("release failed")
'''.replace('PATH', repr(str(tmp_path / 'log'))))
    left, right = socket.socketpair()
    left.settimeout(2)
    thread = threading.Thread(target=run_session, args=(right, tmp_path, 'reinit_release_fail'))
    thread.start()
    try:
        send_message(left, dict(type='initialize', config=opted({}), config_version=1, context_id='old', tui_command_effects_v2=['reinitialize_policy']))
        receive_message(left); receive_message(left)
        send_message(left, dict(type='reinit_release', operation_id='r', config_version=1, context_id='old'))
        result = receive_message(left)
        assert result['type'] == 'error' and result['operation_id'] == 'r'
        send_message(left, {'type':'shutdown'})
        assert receive_message(left)['type'] == 'closed'
        thread.join(2)
        assert (tmp_path / 'log').read_text().splitlines() == ['release']
    finally:
        left.close()
        thread.join(2)


def test_non_opted_session_cannot_reinitialize(tmp_path):
    (tmp_path / 'main.py').write_text('''
def initialize(config): return None
def policy(context, state, action_chunk, images, config): return [[0]*14]
def reset_context(context): pass
def release(context): pass
''')
    left, right = socket.socketpair()
    left.settimeout(2)
    thread = threading.Thread(target=run_session, args=(right, tmp_path, 'legacy_no_reinit'))
    thread.start()
    try:
        send_message(left, dict(type='initialize', config={}, config_version=1))
        receive_message(left); receive_message(left)
        send_message(left, dict(type='reinit_initialize', operation_id='i', context_id='new', config_version=2, config={}))
        assert receive_message(left)['type'] == 'error'
        send_message(left, {'type':'shutdown'})
        assert receive_message(left)['type'] == 'closed'
    finally:
        left.close()
        thread.join(2)


def test_opted_session_requires_initial_context_id(tmp_path):
    (tmp_path / 'main.py').write_text('''
def initialize(config): return None
def policy(context, state, action_chunk, images, config): return [[0]*14]
def reset_context(context): pass
def release(context): pass
''')
    left, right = socket.socketpair()
    left.settimeout(2)
    thread = threading.Thread(target=run_session, args=(right, tmp_path, 'missing_context'))
    thread.start()
    try:
        cfg = {'tui_commands':{'reload':{'handler':'reset_context','args':[], 'effect':'reinitialize_policy'}}}
        send_message(left, dict(type='initialize', config=cfg, config_version=1, tui_command_effects_v2=['reinitialize_policy']))
        assert receive_message(left)['type'] == 'error'
    finally:
        left.close()
        thread.join(2)


def test_old_context_collected_before_new_initialize(tmp_path):
    (tmp_path / 'main.py').write_text('''
import gc
import weakref
class Model: pass
old = None
def initialize(config):
    global old
    if old is not None:
        gc.collect()
        if old() is not None: raise RuntimeError("old model retained")
    value = Model()
    old = weakref.ref(value)
    return value
def policy(context, state, action_chunk, images, config): return [[0]*14]
def reset_context(context): pass
def release(context): pass
''')
    left, right = socket.socketpair()
    left.settimeout(2)
    thread = threading.Thread(target=run_session, args=(right, tmp_path, 'weak_context'))
    thread.start()
    try:
        send_message(left, dict(type='initialize', config=opted({}), config_version=1, context_id='old', tui_command_effects_v2=['reinitialize_policy']))
        assert receive_message(left)['type'] == 'ready'
        receive_message(left)
        send_message(left, dict(type='reinit_release', operation_id='r', config_version=1, context_id='old'))
        assert receive_message(left)['type'] == 'ack'
        send_message(left, dict(type='reinit_initialize', operation_id='i', config_version=2, context_id='new', config=opted({})))
        assert receive_message(left)['type'] == 'ack'
        send_message(left, {'type':'shutdown'})
        assert receive_message(left)['type'] == 'closed'
    finally:
        left.close()
        thread.join(2)
