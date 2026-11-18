import json
import threading
import time
import pytest

from test_runtime_manager import manager, spec


@pytest.mark.parametrize('source,level,expected', [('acone', 'DEBUG', True), ('lift2', 'TRACE', True),
                                                 ('lift2', 'INFO', False), ('mock', 'DEBUG', False)])
def test_prepared_connector_command_follows_rc_log_level(tmp_path, source, level, expected):
    from robot_control.connector import ConnectorRobot
    from robot_control.performance import PerformanceLogger

    runtime, _ = manager(tmp_path)
    runtime.context['connector_rpc_volume'] = 'connector-rpc'
    directory = tmp_path / 'connector' / source
    directory.mkdir(parents=True)
    (directory / 'main.py').touch()
    (directory / 'device.yaml').write_text('{}')
    (directory / 'compose.yaml').write_text(json.dumps({'services': {'connector': {'image': 'mock:local'}}}))
    proxy = ConnectorRobot({'connector_directory': 'connector', 'robot': {'connector': 'body'},
                            'connectors': {'body': {'source': source, 'config': 'device.yaml'}}}, tmp_path)
    proxy.runtime = runtime
    proxy.performance = PerformanceLogger(level, 'run', ingress=object())
    proxy.prepare()
    command = json.loads(proxy._prepared_spec.model_json)['services']['connector']['command']
    assert ('--debug-timing' in command) is expected


def test_connector_has_own_container_and_survives_policy_cleanup(tmp_path):
    runtime, engine = manager(tmp_path)
    runtime.context['connector_rpc_volume'] = 'connector-rpc'
    directory = tmp_path / 'connector/mock'
    directory.mkdir(parents=True)
    (directory / 'main.py').write_text('')
    (directory / 'compose.yaml').write_text(json.dumps({'services': {'connector': {'image': 'mock:local'}}}))
    handle = runtime.start_connector('body', directory, {}, threading.Event(), time.monotonic() + 10)
    policy = runtime.start(spec(runtime, tmp_path, 'body'), 1, threading.Event(), time.monotonic() + 10)
    assert handle.project != policy.project
    assert handle.kind == 'connector'
    model = json.loads((runtime.state_dir / (handle.project + '.compose.json')).read_text())
    mounts = model['services']['connector']['volumes']
    assert any(m.get('target') == '/run/rc-connectors' for m in mounts)
    assert all(m.get('target') != '/run/robot-control' for m in mounts)
    runtime.cleanup_owned(time.monotonic() + 10, include_connectors=False)
    assert handle.container_id in engine.containers
    assert policy.container_id not in engine.containers
    runtime.cleanup_owned(time.monotonic() + 10)
    assert not engine.containers


def test_connector_pool_recovery_requires_reader_container_exit(tmp_path):
    from robot_control.runtime_manager import BlockedCleanup
    runtime, engine = manager(tmp_path)
    engine.containers['rc-reader'] = {'Id': 'rc-reader', 'Config': {'Labels': {}},
                                    'State': {'Running': True, 'Dead': False}}
    runtime.record_pool('0123456789ab', reader_container_id='rc-reader')
    with pytest.raises(BlockedCleanup, match='reader'):
        runtime.recover_pools(time.monotonic() + 5)
