"""Real example workers, native self-chain and connector exercise periodic holds."""
import json
from pathlib import Path
import time

import numpy as np
import pytest

from config_helpers import read_config, connector_config
from project_templates import copy_templates
from host_helpers import LocalPolicyProcess
from robot_control.controller import RobotController
from test_controller import until
from test_example_controller import ROOT, command


@pytest.mark.parametrize('mode,chained', [('sync',False),('sync',True),('async',False),('async',True)])
def test_slow_example_keeps_sending_and_only_async_advances_while_waiting(tmp_path, monkeypatch, mode, chained):
    folder = copy_templates(ROOT/'policy/example', tmp_path/'policy/example')
    manifest = read_config(folder/'policy.yaml')
    manifest['execution'].update(mode=mode, call_every_actions=1)
    manifest['params'].update(chunk_size=8, inference_delay_s=.12)
    (folder/'policy.yaml').write_text(json.dumps(manifest))
    if chained: copy_templates(ROOT/'policy/example_chain', tmp_path/'policy/example_chain')
    config = read_config(ROOT/'configs/rc.mock.yaml.example')
    config.update(policy_directory='policy', startup_policy='example_chain' if chained else 'example',
        outputs_directory='outputs', frequency_hz=30, logging={'level':'DEBUG'},
        timeouts={'initialize_s':10,'inference_s':5,'release_s':3})
    device = tmp_path/'device.yaml'
    device.write_text(json.dumps({'initial_qpos':[.01]*14,'initial_epos':[.02]*14,'image_size':[12,16]}))
    connector_config(config, device=device)
    path = tmp_path/'rc.yaml'; path.write_text(json.dumps(config))
    monkeypatch.chdir(tmp_path)
    rc = RobotController(path, process_factory=LocalPolicyProcess)
    sent = []
    record_attempt = rc._recorder.control_attempt
    def capture(data):
        if rc._state == 'running':
            sent.append({'at':time.monotonic(), 'hold':data['is_hold'], 'reason':data['hold_reason'],
                         'in_flight':rc._inflight is not None, 'consumed':rc.buffer.consumed})
        return record_attempt(data)
    rc._recorder.control_attempt = capture
    rc.start()
    try:
        until(lambda: rc.status()['state'] == 'paused' and rc._worker is not None, 10)
        command(rc, 'record start')
        command(rc, 'resume')
        until(lambda: rc.buffer.consumed >= 4, 6)
        command(rc, 'pause')
        status = command(rc, 'record stop')
    finally:
        rc.submit('exit'); assert rc.done.wait(15), rc.status()
    assert rc.exit_code == 0 and rc._pool._bytes == 0
    waiting = [row for row in sent if row['in_flight']]
    assert waiting and any(row['hold'] for row in waiting)
    if mode == 'sync':
        assert all(row['hold'] and row['reason'] == 'policy_wait' for row in waiting)
    else:
        assert any(not row['hold'] for row in waiting)
    # A prior real-action ACK may arrive between two holds. Only actual action
    # submissions bound consumption; equal held positions alone are not progress.
    assert 4 <= rc.buffer.consumed <= sum(not row['hold'] for row in sent)
    # Notifications can cause extra control-loop passes, never extra send ticks.
    intervals = np.diff([row['at'] for row in sent])
    assert len(intervals) >= 6 and intervals.min() >= .8/30-.002
    assert np.percentile(intervals,95) < .065
    folder = Path(status['path'])
    controls = [json.loads(line) for line in (folder/'control.jsonl').read_text().splitlines()]
    attempts = {row['attempt_id']:row for row in controls if row['event'] == 'control.attempt'}
    published = [row for row in controls if row['event'] == 'control.published']
    assert any(attempts[row['attempt_id']]['is_hold'] for row in published)
    events = [json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]
    points = {point['stage'] for row in events for point in row.get('performance',{}).get('points',[])}
    assert 'rc.policy_ready_notified' in points
    if chained: assert 'chain.ready_notified' in points
    (tmp_path/'ready-hold-evidence.json').write_text(json.dumps({'sent':sent,
        'interval_p95_ms':float(np.percentile(intervals,95)*1000),
        'holds':sum(row['hold'] for row in sent)}, indent=2))
