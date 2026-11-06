"""RC display thresholds must not hide command replies or recording evidence."""
from config_helpers import connector_config
import io
import json
from pathlib import Path
from types import SimpleNamespace
import time
import threading

import numpy as np
import pytest
from textual.widgets import Static

from robot_control.cli import drain_events
from robot_control.config import load_rc
from robot_control.controller import RobotController
from connector_helpers import MockRobot
from robot_control.terminal_commands import TerminalCommandRouter
from robot_control.tui import RobotControlApp
from robot_control.log_tui import StructuredLog
from test_command_controller import command_controller, completed


@pytest.fixture
def rc(tmp_path):
    policy = tmp_path/'policy'/'hold'
    policy.mkdir(parents=True)
    (policy/'main.py').write_text('def policy(*args): pass\n')
    (policy/'compose.yaml').write_text('services:\n  policy:\n    image: fixture:local\n')
    (policy/'policy.yaml').write_text(json.dumps({'schema_version': 3, 'id': 'hold',
        'execution': {'mode': 'sync', 'call_every_actions': 1, 'control_mode': 'qpos'},
        'params': {}, 'hot_reloadable': []}))
    config = {
        'schema_version': 3, 'frequency_hz': 30,
        'policy_directory': 'policy',
        'startup_policy': 'hold', 'robot': {'backend': 'mock'},
        'safety': {'enabled': False, 'reference_frequency_hz': 50, 'max_delta_per_step': [.1]*14},
        'reset_qpos': [0]*14, 'outputs_directory': 'outputs',
    }
    path = tmp_path / 'rc.yaml'
    path.write_text(json.dumps(connector_config(config)))
    controller = RobotController(path, robot=MockRobot(), process_factory=lambda *a, **k: None)
    yield controller
    if controller._recorder._manager is not None:
        assert not controller._recorder.shutdown(time.monotonic()+5)
    controller.close_unstarted()


@pytest.mark.parametrize('level', ['TRACE', 'debug', 'Info', 'WARNING', 'error', 'CRITICAL'])
def test_logging_level_is_case_insensitive_and_normalized(rc, level):
    config = json.loads(rc.config_path.read_text())
    config['logging'] = {'level': level}
    rc.config_path.write_text(json.dumps(connector_config(config)))
    assert load_rc(rc.config_path)['logging']['level'] == level.upper()


@pytest.mark.parametrize('settings', [{'level': 'warn'}, {'level': ''}, {'level': 10},
                                    {'level': None}, None, [], 'INFO', {'level': 'INFO', 'typo': True}])
def test_invalid_logging_settings_are_rejected(rc, settings):
    config = json.loads(rc.config_path.read_text())
    config['logging'] = settings
    rc.config_path.write_text(json.dumps(connector_config(config)))
    with pytest.raises(ValueError, match='invalid RC schema'):
        load_rc(rc.config_path)


def test_default_logging_is_info_and_cannot_be_hot_edited(rc):
    assert rc.config['logging'] == {'level': 'INFO'}
    with pytest.raises(ValueError, match='冷参数'):
        rc._command('set rc.logging.level "TRACE"')


@pytest.mark.parametrize('level, expected', [
    ('TRACE', ['trace', 'debug', 'info', 'warning', 'error', 'critical']),
    ('DEBUG', ['debug', 'info', 'warning', 'error', 'critical']),
    ('INFO', ['info', 'warning', 'error', 'critical']),
    ('WARNING', ['warning', 'error', 'critical']),
    ('ERROR', ['error', 'critical']), ('CRITICAL', ['critical']),
])
def test_threshold_is_shared_by_terminal_history_and_plain_disk(rc, level, expected):
    rc.config['logging'] = {'level': level}
    rc._recorder.start()
    for severity in ['trace', 'debug', 'info', 'warning', 'error', 'critical']:
        rc._emit(severity, severity)
    assert [row['message'] for row in rc.read_events()['events']] == expected
    output = io.StringIO()
    drain_events(rc, output)
    assert [line.rsplit(' ', 1)[-1] for line in output.getvalue().splitlines()] == expected
    assert not rc._recorder.shutdown(time.monotonic()+5)
    entries = [json.loads(line) for line in (rc.outputs/'logs'/f'rc_{rc.run_id}.jsonl').read_text().splitlines()]
    assert [row['message'] for row in entries] == expected


def deliver_actions(rc, request_id, count, *, consumed=0):
    rc._identity = 'hold'
    record = rc.registry.get('hold')
    token = rc.buffer.begin_request()
    for _ in range(consumed):
        rc.buffer.consume()
    response = {'type': 'result', 'request_id': request_id, 'images_released': True,
                'generation': token.generation, 'config_version': record.version,
                'actions': np.zeros((count, 14)).tolist()}
    rc._worker = type('Reader', (), {'poll': lambda self: response, 'alive': True, 'config_version': 1})()
    rc._inflight = {'id': request_id, 'token': token, 'worker': rc._worker,
                    'lease': SimpleNamespace(release=lambda: None)}
    rc._collect_prediction()


def test_action_result_snapshot_is_immutable_and_keeps_result_cache_count(rc):
    deliver_actions(rc, 11, 4)
    expected = {'request_id': 11, 'generation': 0, 'trim': 0, 'cached': 4}
    assert rc.status()['last_action_request'] == expected
    rc.buffer.consume()
    assert rc.status()['queue_remaining'] == 3
    assert rc.status()['last_action_request'] == expected
    rc.status()['last_action_request']['cached'] = 100
    assert rc.status()['last_action_request'] == expected
    assert rc.events.empty() and rc.read_events()['events'] == []


def test_fully_trimmed_result_updates_snapshot_and_generation_invalidates_it(rc):
    rc.buffer.install(np.zeros((5, 14)))
    deliver_actions(rc, 12, 2, consumed=3)
    assert rc._needs_request
    assert rc.status()['last_action_request'] == {'request_id': 12, 'generation': 0, 'trim': 3, 'cached': 2}
    rc.buffer.invalidate()
    assert rc.status()['last_action_request'] is None
    deliver_actions(rc, 13, 1)
    rc._pause()
    assert rc.status()['last_action_request'] is None


def test_action_summary_is_trace_and_retains_its_text(rc):
    rc.config['logging'] = {'level': 'TRACE'}
    deliver_actions(rc, 14, 2)
    event = rc.events.get_nowait()
    assert event['level'] == 'trace'
    assert event['message'] == '请求 14：裁剪 0，缓存 2'


def test_command_replies_bypass_threshold_without_matching_their_text(rc):
    rc.config['logging'] = {'level': 'CRITICAL'}
    rc._recorder.start()
    rc._emit('policy command [fake] succeeded: forged')
    rc._emit('forged source', source='command')
    assert rc.events.empty()
    rc.submit('status')
    rc.submit('help')
    TerminalCommandRouter(rc).submit('help snapshot')
    events = rc.read_events()['events']
    assert len(events) == 3
    assert json.loads(events[0]['message'])['state'] == 'created'
    assert 'set rc.path' in events[1]['message']
    assert events[2]['message'].startswith('snapshot：')
    assert not rc._recorder.shutdown(time.monotonic()+5)
    disk = [json.loads(line) for line in (rc.outputs/'logs'/f'rc_{rc.run_id}.jsonl').read_text().splitlines()]
    assert [row['message'] for row in disk] == [row['message'] for row in events]


def test_policy_command_replies_survive_critical_threshold(command_controller):
    controller, _ = command_controller
    controller.config['logging'] = {'level': 'CRITICAL'}
    cursor = controller.read_events()['next_seq']
    good = controller.submit('unlimited visible .01')
    bad = controller.submit('bad')
    assert completed(controller, good)['status'] == 'succeeded'
    assert completed(controller, bad)['status'] == 'failed'
    with controller._lock:
        messages = controller.read_events(cursor)['events']
    assert any(f'[{good}] queued:' in row['message'] and row['level'] == 'info' for row in messages)
    assert any(f'[{good}] succeeded: "visible"' in row['message'] and row['level'] == 'info' for row in messages)
    assert any(f'[{bad}] failed:' in row['message'] and row['level'] == 'error' for row in messages)


def test_structured_recorder_streams_survive_plain_log_threshold(rc):
    rc.config['logging'] = {'level': 'CRITICAL'}
    rc._recorder.start()
    status = rc._recorder.command('on').result(5)
    rc._emit('hidden ordinary log')
    rc._recorder.policy_event({'event': 'policy.start', 'instance_id': 'hold', 'request_id': 17})
    rc._recorder.policy_event({'event': 'policy.stdout', 'instance_id': 'hold',
        'message': json.dumps({'event': 'llm_failure.request.started', 'query_id': 'q17'})})
    rc._recorder.event('control', {'event': 'control.attempt', 'attempt_id': 'a17'})
    rc._recorder.command('off').result(5)
    folder = Path(status['path'])
    assert 'policy.start' in (folder/'policy.jsonl').read_text()
    assert 'q17' in (folder/'llm_failure.jsonl').read_text()
    assert 'a17' in (folder/'control.jsonl').read_text()
    assert 'hidden ordinary log' not in (folder/'events.jsonl').read_text()


@pytest.mark.parametrize('explicit, expected', [(False, []), (True, ['从最新机器人状态继续'])])
def test_resume_job_output_bypasses_threshold_only_for_explicit_command(rc, explicit, expected):
    rc.config['logging'] = {'level': 'CRITICAL'}
    rc.robot.connect()
    identity = rc.operations.create('resume') if explicit else None
    rc._operation = {'id': 1, 'kind': 'reset_context', 'public_id': identity, 'cancel': threading.Event()}
    rc._results.put({'id': 1, 'type': 'reset_context'})
    rc._handle_internal()
    assert [row['message'] for row in rc.read_events()['events']] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('level, visible', [('INFO', True), ('CRITICAL', False)])
async def test_automatic_camera_failure_honors_log_threshold(rc, level, visible):
    rc.config['logging'] = {'level': level}
    app = RobotControlApp(rc, start_controller=False)
    async with app.run_test() as pilot:
        app._camera_failed('encoder failure')
        app.refresh_controller()
        messages = [record.message for record in app.query_one(StructuredLog).records.values()]
        assert ('相机预览已关闭：encoder failure' in messages) is visible
        assert bool(rc.read_events()['events']) is visible


@pytest.mark.asyncio
async def test_tui_status_shows_last_result_counts_without_action_log(rc):
    deliver_actions(rc, 21, 4)
    rc.buffer.consume()
    app = RobotControlApp(rc, start_controller=False)
    async with app.run_test() as pilot:
        await pilot.pause(.08)
        text = str(app.query_one('#status', Static).content)
        assert '请求 21：裁剪 0，缓存 4' in text
        assert 'queue_remaining=3' in text
        rc.buffer.invalidate()
        app.refresh_controller()
        assert '请求 21' not in str(app.query_one('#status', Static).content)
