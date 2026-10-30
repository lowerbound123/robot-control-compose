"""Writer persistence, bounded barriers and request-boundary evidence."""
from collections import deque
import gc
import json
from pathlib import Path
import weakref

import numpy as np
import pytest

from robot_control import recording_writer as module


def rows(path):
    return [json.loads(text) for text in path.read_text().splitlines()]


def sample(number=0):
    return {'sample_id': number, 'timestamp_ns': 100 + number, 'monotonic_ns': 200 + number,
            'scheduled_ns': 200 + number, 'frames': {}, 'controls': {}, 'missed': False,
            'state': {'qpos': list(range(14)), 'eef': [n + .5 for n in range(14)]}}


class Receiver:
    def __init__(self, packets, before_poll=None):
        self.packets = deque(packets)
        self.before_poll = before_poll
        self.closed = False

    def poll(self, timeout):
        if self.before_poll:
            self.before_poll()
        if not self.packets:
            raise EOFError
        return True

    def recv(self):
        return self.packets.popleft()

    def close(self):
        self.closed = True


class Responder:
    def __init__(self):
        self.messages = []
        self.closed = False

    def send(self, message):
        self.messages.append(message)

    def close(self):
        self.closed = True


def writer(tmp_path, monkeypatch, packets, before_poll=None):
    monkeypatch.setattr(module.os, 'nice', lambda value: None)
    receiver, responder = Receiver(packets, before_poll), Responder()
    module.writer_main(receiver, responder, str(tmp_path), 'rc-run-fixture')
    assert receiver.closed and responder.closed
    return responder.messages


def test_sample_commit_is_durable_only_after_all_csv_and_state_records(tmp_path, monkeypatch):
    session = module.SessionFiles(tmp_path / 'session', 'session', {'run_id': 'run'})
    synced = []
    original = module.os.fsync
    def fsync(fd):
        synced.append(fd)
        original(fd)
    monkeypatch.setattr(module.os, 'fsync', fsync)
    try:
        session.sample(sample())
        committed = rows(session.root / 'samples.jsonl')
        assert [value['sample_id'] for value in committed] == [0]
        target = session.files['samples'].fileno()
        assert target in synced
        for name in ('epos', 'qpos', 'control_epos', 'control_qpos', 'state'):
            assert session.files[name].fileno() in synced[:synced.index(target)]
            assert (session.root / (name + ('.jsonl' if name == 'state' else '.csv'))).stat().st_size
    finally:
        session.close()


def test_sample_data_fsync_failure_never_publishes_commit(tmp_path, monkeypatch):
    session = module.SessionFiles(tmp_path / 'session', 'session', {})
    original = module.os.fsync
    def fsync(fd):
        if fd == session.files['qpos'].fileno():
            raise OSError('state disk failure')
        original(fd)
    monkeypatch.setattr(module.os, 'fsync', fsync)
    try:
        with pytest.raises(OSError, match='state disk failure'):
            session.sample(sample())
        assert (session.root / 'samples.jsonl').read_bytes() == b''
        assert session.stats['samples_written'] == 0
    finally:
        monkeypatch.setattr(module.os, 'fsync', original)
        session.close('state disk failure')


def test_sample_commit_keeps_exact_control_event_and_late_observer_metadata(tmp_path):
    session = module.SessionFiles(tmp_path / 'session', 's', {})
    payload = sample()
    payload['controls'] = {'left': {'mode': 'epos', 'values': [.25] * 7,
                                    'monotonic_ns': 150, 'late_observer': True, 'attempt_id': 'attempt'}}
    try:
        session.sample(payload)
        assert rows(session.root / 'samples.jsonl')[0]['control_events'] == payload['controls']
    finally:
        session.close()


def test_barrier_remembers_processed_tokens_across_session_start_and_stop(tmp_path, monkeypatch):
    packets = [
        {'kind': 'log', 'token': 1, 'data': {'message': 'idle'}},
        {'kind': 'start', 'token': 2, 'request': 'on', 'session': 's', 'path': str(tmp_path / 's')},
        {'kind': 'stop', 'token': 3, 'request': 'off', 'session': 's', 'pending': []},
        # ACK 1 can still be in the parent's receive pipe after both transitions.
        {'kind': 'shutdown', 'token': 4, 'request': 'exit', 'session': 's', 'pending': [1]},
    ]
    messages = writer(tmp_path, monkeypatch, packets)
    assert [item['request'] for item in messages if item['kind'] == 'stopped'] == ['off', 'exit']


def test_stop_barrier_waits_for_exact_out_of_order_tokens(tmp_path, monkeypatch):
    packets = [
        {'kind': 'start', 'token': 1, 'request': 'on', 'session': 's', 'path': str(tmp_path / 's')},
        {'kind': 'stop', 'token': 4, 'request': 'off', 'session': 's', 'pending': [2, 3]},
        {'kind': 'log', 'token': 3, 'session': 's', 'data': {'message': 'three'}},
        {'kind': 'log', 'token': 2, 'session': 's', 'data': {'message': 'two'}},
        {'kind': 'shutdown', 'token': 5, 'request': 'exit', 'pending': [4]},
    ]
    messages = writer(tmp_path, monkeypatch, packets)
    stop = next(index for index, item in enumerate(messages) if item.get('request') == 'off' and item['kind'] == 'stopped')
    assert {item['token'] for item in messages[:stop] if item['kind'] == 'ack'} >= {2, 3, 4}
    assert [entry['message'] for entry in rows(tmp_path / 's/events.jsonl')] == ['three', 'two']
    assert any(item.get('request') == 'exit' and item['kind'] == 'stopped' for item in messages)


def test_shutdown_does_not_overwrite_an_outstanding_stop_receipt(tmp_path, monkeypatch):
    packets = [
        {'kind': 'start', 'token': 1, 'request': 'on', 'session': 's', 'path': str(tmp_path / 's')},
        {'kind': 'stop', 'token': 3, 'request': 'off', 'session': 's', 'pending': [2]},
        {'kind': 'shutdown', 'token': 4, 'request': 'exit', 'session': 's', 'pending': [2, 3]},
        {'kind': 'log', 'token': 2, 'session': 's', 'data': {'message': 'drained'}},
    ]
    messages = writer(tmp_path, monkeypatch, packets)
    assert [item['request'] for item in messages if item['kind'] == 'stopped'] == ['off', 'exit']


def test_too_many_token_holes_fail_explicitly_instead_of_losing_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'MAX_TOKEN_INTERVALS', 2, raising=False)
    packets = [{'kind': 'log', 'token': token, 'data': {'message': str(token)}} for token in (2, 4, 6)]
    packets.append({'kind': 'shutdown', 'token': 7, 'request': 'exit', 'pending': []})
    messages = writer(tmp_path, monkeypatch, packets)
    assert any(item['kind'] == 'fatal' and 'token' in item['error'] for item in messages)


def stdout(event, query_id, *, structured=True, instance='llm_failure-1'):
    message = json.dumps({'event': event, 'query_id': query_id, 'instance_id': instance, 'generation': 1})
    if not structured:
        return {'message': '[' + instance + '] ' + message, 'run_id': 'rc-run-fixture'}
    return {'event': 'policy.stdout', 'message': message, 'instance_id': instance,
            'runtime_id': 'runtime-42', 'session_id': 'host-session', 'host_instance_id': 'host-9',
            'deployment_id': 'deployment', 'run_id': 'rc-run-fixture', 'config_version': 3, 'generation': 5}


def test_llm_stream_deduplicates_plain_and_structured_stdout_with_authoritative_identity(tmp_path):
    session = module.SessionFiles(tmp_path / 'session', 's', {'run_id': 'rc-run-fixture'})
    try:
        session.event('events', stdout('llm_failure.request.started', 'q'))
        session.event('events', stdout('llm_failure.request.started', 'q', structured=False))
        session.event('events', stdout('llm_failure.request.completed', 'q'))
        session.event('events', stdout('llm_failure.collection.accepted', 'q'))
    finally:
        session.close()
    values = rows(session.root / 'llm_failure.jsonl')
    assert [value['event'] for value in values] == [
        'llm_failure.request.started', 'llm_failure.request.completed', 'llm_failure.collection.accepted']
    assert all(value['policy_instance_id'] == 'llm_failure-1' and value['rc_run_id'] == 'rc-run-fixture' for value in values)
    assert values[0]['policy_runtime_id'] == 'runtime-42'
    assert values[0]['policy_session_id'] == 'host-session'
    assert not any(value.get('start_outside_recording') for value in values)


def test_llm_boundaries_distinguish_missing_start_request_end_and_collection(tmp_path):
    session = module.SessionFiles(tmp_path / 'session', 's', {'run_id': 'rc-run-fixture'})
    try:
        for event, query in (
            ('llm_failure.request.completed', 'old'), ('llm_failure.collection.accepted', 'old'),
            ('llm_failure.request.started', 'http-pending'), ('llm_failure.request.started', 'collection-pending'),
            ('llm_failure.request.error', 'collection-pending'),
        ):
            session.event('events', stdout(event, query))
    finally:
        session.close()
    values = rows(session.root / 'llm_failure.jsonl')
    old = [value for value in values if value['query_id'] == 'old']
    assert all(value['start_outside_recording'] for value in old)
    unfinished = {value['query_id']: value for value in values if value['event'] == 'llm_failure.recording_incomplete'}
    assert set(unfinished) == {'http-pending', 'collection-pending'}
    assert unfinished['http-pending']['request_terminal_seen'] is False
    assert unfinished['collection-pending']['request_terminal_seen'] is True
    assert all(value['end_outside_recording'] for value in unfinished.values())


def test_llm_authoritative_instance_cannot_be_overridden_by_printed_prefix(tmp_path):
    session = module.SessionFiles(tmp_path / 'session', 's', {'run_id': 'run'})
    data = stdout('llm_failure.request.started', 'q')
    data['message'] = '[spoofed-instance] ' + data['message']
    try:
        session.event('events', data)
    finally:
        session.close()
    assert rows(session.root / 'llm_failure.jsonl')[0]['policy_instance_id'] == 'llm_failure-1'


def test_redaction_preserves_json_encoded_provider_response(tmp_path):
    session = module.SessionFiles(tmp_path / 'session', 's', {'run_id': 'run'})
    data = stdout('llm_failure.request.error', 'q')
    event = json.loads(data['message'])
    event['raw_response'] = json.dumps({'error': {'message': 'Authorization: Bearer private-header-value'}})
    data['message'] = json.dumps(event)
    try:
        session.event('events', data)
    finally:
        session.close()
    saved = rows(session.root / 'llm_failure.jsonl')[0]
    assert 'private-header-value' not in saved['raw_response']
    assert isinstance(json.loads(saved['raw_response']), dict)


def test_request_tracking_is_bounded_and_eviction_leaves_an_explicit_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'MAX_OPEN_REQUESTS', 3, raising=False)
    session = module.SessionFiles(tmp_path / 'session', 's', {'run_id': 'run'})
    try:
        for number in range(8):
            session.event('policy', {'event': 'policy.start', 'instance_id': 'worker', 'request_id': number})
        assert len(session.open_requests) <= 3
    finally:
        session.close()
    assert sum(value.get('count', 0) for value in rows(session.root / 'session.jsonl')
               if value['event'] == 'record.request_tracking_gap') == 5


def test_config_and_plain_logs_redact_known_credentials_and_header_forms(tmp_path, monkeypatch):
    secret = 'private-provider-key-12345'
    password = 'private-password-12345'
    packets = [
        {'kind': 'start', 'token': 1, 'request': 'on', 'session': 's', 'path': str(tmp_path / 's'),
         'data': {'provider': {'api_key': secret}, 'password': password, 'echo': secret}},
        {'kind': 'log', 'token': 2, 'session': 's', 'data': {'message': 'reflected: ' + secret + ' ' + password}},
        {'kind': 'log', 'token': 3, 'session': 's', 'data': {'message': 'Authorization: Bearer header-secret-123'}},
        {'kind': 'log', 'token': 4, 'session': 's', 'data': {'message': 'api_key=label-secret-123'}},
        {'kind': 'stop', 'token': 5, 'request': 'off', 'session': 's', 'pending': [2, 3, 4]},
        {'kind': 'log', 'token': 6, 'data': {'message': 'after stop: ' + secret}},
        {'kind': 'shutdown', 'token': 7, 'request': 'exit', 'pending': []},
    ]
    writer(tmp_path, monkeypatch, packets)
    text = '\n'.join(path.read_text() for path in tmp_path.rglob('*') if path.is_file())
    assert all(value not in text for value in (secret, password, 'header-secret-123', 'label-secret-123'))
    assert rows(tmp_path / 'rc_rc-run-fixture.jsonl')[-1]['message'] == 'after stop: ***'


def test_partial_initialization_closes_every_open_file_and_removes_its_own_directory(tmp_path, monkeypatch):
    opened = []
    original = Path.open
    def open_file(path, *args, **kwargs):
        if path.name == 'policy.jsonl':
            raise OSError('initialize disk failure')
        value = original(path, *args, **kwargs)
        opened.append(value)
        return value
    monkeypatch.setattr(Path, 'open', open_file)
    with pytest.raises(OSError, match='initialize disk failure'):
        module.SessionFiles(tmp_path / 'new-session', 's', {})
    assert opened and all(stream.closed for stream in opened)
    assert not (tmp_path / 'new-session').exists()
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'keep').write_text('owned by user')
    with pytest.raises(FileExistsError):
        module.SessionFiles(existing, 's', {})
    assert (existing / 'keep').read_text() == 'owned by user'


def test_writer_releases_sample_rgb_after_ack_without_waiting_for_next_packet(tmp_path, monkeypatch):
    pixels = np.zeros((2, 3, 3), dtype=np.uint8)
    reference = weakref.ref(pixels)
    payload = sample()
    payload['frames'] = {'head': {'image': pixels, 'valid': True, 'sequence': 1, 'timestamp': 1.}}
    packets = [
        {'kind': 'start', 'token': 1, 'request': 'on', 'session': 's', 'path': str(tmp_path / 's')},
        {'kind': 'sample', 'token': 2, 'session': 's', 'data': payload},
        {'kind': 'shutdown', 'token': 3, 'request': 'exit', 'pending': [2]},
    ]
    receiver, responder = Receiver(packets), Responder()
    del packets, payload, pixels
    def before_poll():
        if responder.messages and responder.messages[-1].get('token') == 2:
            gc.collect()
            assert reference() is None, 'writer retained the acknowledged RGB packet while idle'
    receiver.before_poll = before_poll
    monkeypatch.setattr(module.os, 'nice', lambda value: None)
    module.writer_main(receiver, responder, str(tmp_path), 'rc-run-fixture')
    assert not any(value['kind'] == 'fatal' for value in responder.messages), responder.messages
