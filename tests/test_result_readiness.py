"""Readiness signals transfer results, never execute control work."""
import threading
import time

import pytest

from test_recording_hooks import remote_worker, inference_request, receive_result, build_chain, recording_frames


@pytest.mark.parametrize('kind', ['result', 'error', 'observed', 'observation_error', 'failure'])
def test_policy_publishes_ready_event_after_terminal_data_is_available(tmp_path, kind):
    worker = remote_worker(tmp_path, None)
    worker.ready_event = threading.Event()
    request = inference_request()
    if kind in ('observed', 'observation_error'):
        assert worker.observe(request)
        message = {key: request[key] for key in ('request_id','generation','config_version')}
        worker._receive({'runtime_id':'runtime', 'event':{**message,'type':kind,'images_released':True}})
        result = worker.poll_observation()
    else:
        worker.submit(request)
        if kind == 'failure': worker._record_failure('test worker failed')
        else: receive_result(worker, request, type=kind)
        result = worker.poll()
    assert result is not None
    assert worker.ready_event.is_set(), 'queued terminal data did not wake its parent'


def test_policy_deadline_is_exposed_and_retires_after_timeout(tmp_path):
    worker = remote_worker(tmp_path, None)
    worker.submit(inference_request())
    assert worker.next_result_deadline > time.monotonic()
    worker._inflight['deadline'] = 0
    assert worker.poll()['type'] == 'error'
    assert worker.next_result_deadline is None


def test_native_chain_notifies_parent_without_waiting_for_health_poll(tmp_path, recording_frames):
    chain = build_chain(tmp_path, [], delay=.01)
    # This test measures result notification, not cold interpreter startup or
    # forced cleanup; leave their independent deadlines enough headroom.
    chain.init_timeout_s = 10
    chain.release_timeout_s = 3
    chain.ready_event = threading.Event()
    chain.HEALTH_CHECK_INTERVAL = 1.
    request = inference_request()
    request.update(config_version=1, images=recording_frames)
    try:
        chain.start()
        chain.ready_event.clear()
        chain.submit(request)
        assert chain.ready_event.wait(.5), 'completed child/chain waited for the health poll'
        response = chain.poll()
        assert response and response['type'] == 'result', response
        assert response['actions'] == [[3.] * 14]
    finally:
        chain.close()


def test_child_deadline_wakes_chain_before_health_check_and_preserves_reader_proof(tmp_path, recording_frames):
    from robot_control.process import PolicyProcess
    class SilentHost:
        def enqueue(self, packet): pass
    class SilentPolicy(PolicyProcess):
        """Keep the real RC deadline/poll path; transport never sends a reply."""
        def __init__(self, directory, config, version, **options):
            options.pop('endpoint', None)
            super().__init__(directory, config, version, host_socket='/tmp/unused-deadline-host', **options)
        def start(self, cancel_event=None):
            self._state = 'running'
            self._ready = True
            self.host = SilentHost()
    chain = build_chain(tmp_path, [], timeout=3)
    chain._factory = SilentPolicy
    chain.record.members[0].config.setdefault('timeouts', {})['inference_s'] = .03
    chain.ready_event = threading.Event()
    chain.HEALTH_CHECK_INTERVAL = 1.
    request = inference_request()
    request.update(config_version=1, images=recording_frames)
    try:
        chain.start()
        chain.ready_event.clear()
        chain.submit(request)
        assert chain.ready_event.wait(.3)
        response = chain.poll()
        assert response['type'] == 'error' and 'policy inference timeout' in response['error']
        assert response.get('images_released') is not True
        assert chain.next_result_deadline is None
    finally:
        chain.close()


def test_stale_policy_reply_neither_notifies_nor_replaces_current_request(tmp_path):
    worker = remote_worker(tmp_path, None)
    worker.ready_event = threading.Event()
    request = inference_request()
    worker.submit(request)
    receive_result(worker, request, request_id=request['request_id']+1)
    assert not worker.ready_event.is_set() and worker.poll() is None
    assert worker.next_result_deadline is not None
    receive_result(worker, request)
    assert worker.ready_event.is_set() and worker.poll()['request_id'] == request['request_id']
