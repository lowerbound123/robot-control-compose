"""Continuous recording contract, using real writer subprocesses."""
import csv
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image


def wait(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'recording condition timed out'
        time.sleep(.01)


def snapshot():
    pixels = np.arange(12 * 16 * 3, dtype=np.uint8).reshape(12,16,3)
    pixels.flags.writeable = False
    return {'state': {'qpos': list(range(14)), 'eef': [x+.5 for x in range(14)], 'raw': {'marker': 7}},
            'state_error': None, 'frames': {name: {'image': pixels, 'sequence': 1,
            'timestamp': 123., 'received': time.monotonic(), 'valid': True, 'error': None}
            for name in ('head','left_wrist','right_wrist')}}


def test_recording_png_csv_append_and_no_stale_send(tmp_path):
    from robot_control.recording import RecordingService
    service = RecordingService(tmp_path, 'test', snapshot)
    service.start()
    try:
        service.command('on', {'rc': {'api_key': 'hidden'}}).result(5)
        service.sent({'arm':'left','mode':'epos','values':[.5]*7,
                      'timestamp_ns':time.time_ns(),'monotonic_ns':time.monotonic_ns()})
        wait(lambda: service.status()['samples_written'] >= 5)
        status = service.command('off').result(5)
        folder = Path(status['path'])
        tables = {}
        for name in ('epos','qpos','control_epos','control_qpos'):
            with (folder / (name+'.csv')).open() as stream: tables[name] = list(csv.reader(stream))
        assert len({len(rows) for rows in tables.values()}) == 1
        assert len(tables['qpos']) >= 6
        assert all(row[3:] == [str(x) for x in range(14)] for row in tables['qpos'][1:])
        sent = [row for row in tables['control_epos'][1:] if any(row[3:])]
        assert len(sent) == 1 and sent[0][3:10] == ['0.5']*7 and sent[0][10:] == ['']*7
        assert all(row[3:] == ['']*14 for row in tables['control_qpos'][1:])
        commits = [json.loads(line) for line in (folder/'samples.jsonl').read_text().splitlines()]
        assert len(commits) == len(tables['qpos'])-1
        assert commits[-1]['images']['head']['repeated']
        for camera in ('head','left_wrist','right_wrist'):
            with Image.open(folder/commits[0]['images'][camera]['file']) as image:
                np.testing.assert_array_equal(np.asarray(image), snapshot()['frames'][camera]['image'])
        original = (folder/'qpos.csv').read_bytes()
        service.command('on').result(5)
        wait(lambda: service.status()['samples_written'] >= 2)
        second = service.command('off').result(5)
        assert second['path'] != str(folder) and (folder/'qpos.csv').read_bytes() == original
        assert 'hidden' not in (folder/'configs/initial.json').read_text()
    finally:
        assert not service.shutdown(time.monotonic()+5)


def test_async_base_logs_and_optional_overflow_do_not_break_samples(tmp_path):
    from robot_control.recording import RecordingService
    service = RecordingService(tmp_path, 'base', snapshot, extra_budget=4096)
    service.start()
    try:
        service.log({'time':'now','message':'ordinary'})
        service.command('on').result(5)
        assert not service.event('policy', {'payload': 'x'*10000})
        wait(lambda: service.status()['samples_written'] >= 3)
        status = service.command('off').result(5)
        assert status['extra_dropped'] >= 1 and status['error'] is None
    finally:
        assert not service.shutdown(time.monotonic()+5)
    assert 'ordinary' in (tmp_path/'rc_base.jsonl').read_text()


def test_required_overflow_stops_recording_with_error_and_keeps_existing_data(tmp_path):
    from robot_control.recording import RecordingService
    service = RecordingService(tmp_path, 'overflow', snapshot, sample_budget=100)
    service.start()
    try:
        service.command('on').result(5)
        wait(lambda: service.status()['state'] == 'error')
        assert 'budget' in service.status()['error']
        assert Path(service.status()['path']).is_dir()
    finally:
        service.shutdown(time.monotonic()+5)


def test_normal_log_count_saturation_reserves_space_for_receipts_and_llm(tmp_path):
    from robot_control.recording import RecordingService
    service = RecordingService(tmp_path, 'priority', snapshot)
    service._state, service._session = 'recording', 'session'
    accepted = 0
    while service.log({'message':'ordinary'}):
        accepted += 1
    assert accepted > 0
    assert service.event('events', {'event':'command.receipt'}, priority=True)
    assert service.log({'message':'{"event":"llm_failure.request.completed"}'})
    assert service.status()['pending_bytes'] < service.extra_budget


def test_diagnostic_gaps_stay_in_their_original_session(tmp_path):
    from robot_control.recording import RecordingService
    service = RecordingService(tmp_path, 'gaps', snapshot, extra_budget=4096)
    service.start()
    try:
        assert not service.log({'message':'x'*5000})
        first = service.command('on').result(3)
        assert not service.event('policy', {'payload':'x'*5000})
        service.command('off').result(3)
        second = service.command('on').result(3)
        service.command('off').result(3)
        def gaps(path):
            return [json.loads(line) for line in path.read_text().splitlines()
                    if json.loads(line).get('event') == 'diagnostic_gap']
        assert sum(row['count'] for row in gaps(Path(first['path'])/'session.jsonl')) == 1
        assert not gaps(Path(second['path'])/'session.jsonl')
        outside = gaps(tmp_path/'rc_gaps.jsonl')
        assert len(outside) == 1 and outside[0]['record_session_id'] is None
    finally:
        assert not service.shutdown(time.monotonic()+3)
