"""Display metadata follows the actual binding across real example transactions."""
import json
import threading

from robot_control.connector import ConnectorRobot
from test_example_controller import example_rc, command, ROOT
from test_rc_logging import rc


def binding(rc):
    snapshot = rc._statusbar_snapshot()
    return snapshot['connector_source'], snapshot['connector_config']


def next_profile(rc):
    # A second source makes a mixed old/new metadata pair observable. Only the
    # shipped mock is copied; no hardware driver, model or Docker is started.
    directory = rc.root / 'connectors' / 'next_mock'
    (directory / 'devices').mkdir(parents=True)
    (directory / 'main.py').write_bytes((ROOT / 'connector/mock/main.py').read_bytes())
    (directory / 'devices/next.yaml').write_text(json.dumps({
        'initial_qpos': [.3] * 14, 'initial_epos': [.4] * 14, 'image_size': [12, 16],
    }))
    rc._initial_robot_config['connector_directory'] = str(directory.parent)
    folder = rc.root / 'robots'
    folder.mkdir(exist_ok=True)
    profile = folder / 'next.yaml'
    profile.write_text(json.dumps({'connector': 'body', 'source': 'next_mock', 'config': 'devices/next.yaml'}))
    return profile


def test_metadata_requires_controller_connection_available_binding_and_spec(rc):
    assert binding(rc) == (None, None)
    rc._connected = True
    assert binding(rc) == (None, None)  # In-process mock has no ConnectorRobot spec.
    proxy = ConnectorRobot(rc.config, rc.root)
    rc.robot.bind(proxy)
    assert binding(rc) == ('mock', 'devices/default.yaml.example')
    rc.robot.suspend()
    assert rc._connected and binding(rc) == (None, None)
    rc.robot.bind(proxy)
    rc._connected = False
    assert binding(rc) == (None, None)
    rc._connected = True
    with rc.robot._lock:
        rc.robot.raw = None
    assert binding(rc) == (None, None)
    proxy.close()


def test_binding_metadata_changes_together_only_at_commit(example_rc, monkeypatch):
    rc = example_rc
    old_pair = ('mock', str(rc.root / 'mock-device.yaml'))
    assert binding(rc) == old_pair
    next_profile(rc)
    stages = {name: (threading.Event(), threading.Event()) for name in ('prepared', 'disconnected', 'connected')}
    observed, finished = [], threading.Event()

    def stop_at(name):
        entered, proceed = stages[name]
        entered.set()
        assert proceed.wait(8), name

    prepare = ConnectorRobot.prepare
    def prepared(self, *args, **kwargs):
        prepare(self, *args, **kwargs)
        stop_at('prepared')
    monkeypatch.setattr(ConnectorRobot, 'prepare', prepared)
    close = rc.robot.raw.close
    def disconnected(**kwargs):
        stop_at('disconnected')
        close(**kwargs)
    monkeypatch.setattr(rc.robot.raw, 'close', disconnected)
    initialize = rc._worker.initialize_context
    def connected(*args, **kwargs):
        stop_at('connected')
        return initialize(*args, **kwargs)
    monkeypatch.setattr(rc._worker, 'initialize_context', connected)

    def observe():
        while not finished.wait(.001):
            observed.append(binding(rc))
    reader = threading.Thread(target=observe)
    reader.start()
    operation = rc.submit('connector switch next')
    try:
        for name, expected in (
            ('prepared', old_pair), ('disconnected', (None, None)),
            ('connected', ('next_mock', 'devices/next.yaml')),
        ):
            entered, proceed = stages[name]
            assert entered.wait(8), name
            assert binding(rc) == expected
            proceed.set()
        assert rc.wait_operation(operation, 12)['status'] == 'succeeded'
        assert binding(rc) == ('next_mock', 'devices/next.yaml')
    finally:
        for _, proceed in stages.values():
            proceed.set()
        finished.set()
        reader.join(2)
    assert observed and set(observed) <= {old_pair, (None, None), ('next_mock', 'devices/next.yaml')}


def test_preflight_failure_and_disk_edits_do_not_replace_active_metadata(example_rc):
    rc = example_rc
    original = binding(rc)
    path = next_profile(rc)
    path.write_text(json.dumps({'connector': 'body', 'source': 'next_mock', 'config': 'missing.yaml'}))
    command(rc, 'connector switch next', status='failed')
    assert binding(rc) == original
    path.write_text(json.dumps({'connector': 'body', 'source': 'next_mock', 'config': 'devices/next.yaml'}))
    command(rc, 'connector switch next')
    assert binding(rc) == ('next_mock', 'devices/next.yaml')
    path.write_text(json.dumps({'connector': 'other', 'source': 'edited_source', 'config': 'edited.yaml'}))
    (rc.root / 'connectors/next_mock/devices/next.yaml').write_text('invalid after commit')
    assert binding(rc) == ('next_mock', 'devices/next.yaml')


def test_failed_new_connection_clears_old_metadata_until_retry(example_rc, monkeypatch):
    rc = example_rc
    next_profile(rc)
    connect = ConnectorRobot.connect
    def failed(*args, **kwargs):
        raise RuntimeError('new feedback unavailable')
    monkeypatch.setattr(ConnectorRobot, 'connect', failed)
    command(rc, 'connector switch next', status='failed')
    assert rc.connector_status()['phase'] == 'connection_failed'
    assert binding(rc) == (None, None)
    monkeypatch.setattr(ConnectorRobot, 'connect', connect)
    command(rc, 'connector retry')
    assert binding(rc) == ('next_mock', 'devices/next.yaml')


def test_policy_adaptation_failure_keeps_committed_connection_metadata(example_rc, monkeypatch):
    rc = example_rc
    next_profile(rc)
    def failed(*args, **kwargs):
        raise RuntimeError('new policy context failed')
    monkeypatch.setattr(rc._worker, 'initialize_context', failed)
    command(rc, 'connector switch next', status='failed')
    assert rc.connector_status()['connected']
    assert rc.connector_status()['instances']['example']['status'] == 'failed'
    assert binding(rc) == ('next_mock', 'devices/next.yaml')
