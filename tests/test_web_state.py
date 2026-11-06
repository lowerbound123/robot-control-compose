"""Read-only state streaming shares sampling and preserves control semantics."""
import asyncio
import json
import threading
import time

import httpx
import pytest

from test_controller import setup_controller, ready, until


class StateSource:
    def __init__(self):
        self.calls = 0
        self.error = None

    def __call__(self):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return dict(qpos=list(range(14)), eef=list(range(14, 28)),
                    sequences={'left': self.calls, 'right': self.calls},
                    timestamps={'left': 10., 'right': 11.},
                    feedback_age_s={'left': 0., 'right': .001})


@pytest.mark.asyncio
async def test_shared_sampler_is_idle_without_readers_and_slow_reader_gets_latest():
    from robot_control.web_state import WebState
    source = StateSource()
    feed = WebState(source, run_id='run')
    assert source.calls == 0
    first, second = feed.subscribe(), feed.subscribe()
    try:
        packet = await asyncio.wait_for(first.next_frame(), 1)
        assert await second.next_frame() is packet
        value = json.loads(packet.data.removeprefix(b'data: '))
        assert value['state']['qpos'] == list(range(14))
        assert value['state']['eef'] == list(range(14, 28))
        assert value['run_id'] == 'run'
        await asyncio.sleep(.12)
        latest = await second.next_frame()
        assert latest.sequence > packet.sequence + 2
        assert source.calls < 12  # one source read per tick, not per subscriber
        first.close()
        second.close()
        count = source.calls
        await asyncio.sleep(.08)
        assert source.calls == count
    finally:
        first.close()
        second.close()
        assert feed.close(time.monotonic() + 1)


@pytest.mark.asyncio
async def test_feedback_errors_are_explicit_and_recover_without_closing_stream():
    from robot_control.web_state import WebState
    source = StateSource()
    source.error = 'stale left arm feedback'
    feed = WebState(source, run_id='run')
    reader = feed.subscribe()
    try:
        frame = await reader.next_frame()
        value = json.loads(frame.data.removeprefix(b'data: '))
        assert value['state'] is None
        assert value['error'] == 'stale left arm feedback'
        source.error = None
        frame = await reader.next_frame()
        assert json.loads(frame.data.removeprefix(b'data: '))['state']['qpos'][13] == 13
    finally:
        reader.close()
        assert feed.close(time.monotonic() + 1)


@pytest.mark.asyncio
async def test_sampler_produces_at_least_30_hz_and_closes_waiters():
    from robot_control.web_state import WebState
    feed = WebState(StateSource(), run_id='run')
    reader = feed.subscribe()
    try:
        start = await reader.next_frame()
        end = start
        count = 0
        while end.sampled_monotonic - start.sampled_monotonic < 1:
            end = await reader.next_frame()
            count += 1
        assert count / (end.sampled_monotonic - start.sampled_monotonic) >= 30
        assert feed.close(time.monotonic() + 1)
        assert await reader.next_frame() is None
    finally:
        reader.close()
        feed.close(time.monotonic() + 1)


def test_sampler_close_respects_deadline_even_if_source_is_stuck():
    from robot_control.web_state import WebState
    started, release = threading.Event(), threading.Event()
    def source():
        started.set()
        release.wait(2)
        return StateSource()()
    async def run():
        feed = WebState(source, run_id='run')
        reader = feed.subscribe()
        try:
            assert await asyncio.to_thread(started.wait, 1)
            start = time.monotonic()
            assert not feed.close(start + .03)
            assert time.monotonic() - start < .2
        finally:
            release.set()
            reader.close()
            assert feed.close(time.monotonic() + 1)
    asyncio.run(run())


def test_http_state_is_authenticated_readonly_and_closes_with_service(setup_controller, monkeypatch):
    from robot_control.mcp_server import MCPService
    controller, _, _ = setup_controller
    ready(controller)
    before = controller.status()
    monkeypatch.setenv('RC_STATE_TEST_TOKEN', 'state-test')
    service = MCPService(controller, {'port': 0, 'token_env': 'RC_STATE_TEST_TOKEN'}, allow_ephemeral=True)
    service.start()
    base = service.url.removesuffix('/mcp')
    try:
        with httpx.Client(trust_env=False, timeout=3) as http:
            assert http.get(base + '/state/stream').status_code == 401
            with http.stream('GET', base + '/state/stream', headers={'Authorization': 'Bearer state-test'}) as response:
                assert response.status_code == 200
                assert response.headers['content-type'].startswith('text/event-stream')
                first = next(line for line in response.iter_lines() if line.startswith('data: '))
                data = json.loads(first[6:])
                assert len(data['state']['qpos']) == len(data['state']['eef']) == 14
                assert data['run_id'] == before['run_id']
            until(lambda: service.state_feed.subscriber_count == 0)
            after = controller.status()
            for field in ('state', 'generation', 'control_owner', 'control_epoch'):
                assert after[field] == before[field]
            with http.stream('GET', base + '/state/stream', headers={'Authorization': 'Bearer state-test'}) as response:
                assert next(response.iter_lines()).startswith('data: ')
                service.close()
                assert not service.thread.is_alive()
    finally:
        service.close()


@pytest.mark.asyncio
async def test_state_cleanup_failure_sets_nonzero_exit_under_shared_absolute_deadline(setup_controller, monkeypatch):
    from robot_control.mcp_server import create_app, mcp_settings
    controller, _, _ = setup_controller
    ready(controller)
    deadline = time.monotonic() - .01
    calls = []
    class Display:
        def __init__(self, name, succeeds): self.name, self.succeeds = name, succeeds
        def stop(self): calls.append((self.name, 'stop'))
        def close(self, at):
            calls.append((self.name, at))
            return self.succeeds
    app = create_app(controller, mcp_settings({'port': 0}, allow_ephemeral=True),
                     preview=Display('camera', True), state_feed=Display('state', False))
    with monkeypatch.context() as patch:
        patch.setattr(controller, '_shutdown_deadline', deadline)
        async with app.router.lifespan_context(app):
            pass
    assert calls == [('state', 'stop'), ('camera', deadline), ('state', deadline)]
    assert controller.exit_code == 1


@pytest.mark.asyncio
async def test_unexpected_sampler_exit_notifies_owner_and_releases_waiter():
    from robot_control.web_state import WebState
    failures = []
    def source(): raise SystemExit('sampler failed')
    feed = WebState(source, run_id='run', on_failure=failures.append)
    reader = feed.subscribe()
    try:
        assert await asyncio.wait_for(reader.next_frame(), 1) is None
        assert feed.close(time.monotonic() + 1)
        assert len(failures) == 1
        assert str(failures[0]) == 'sampler failed'
    finally:
        reader.close()
        feed.close(time.monotonic() + 1)
