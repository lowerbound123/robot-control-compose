"""Display-only HTTP images must not borrow policy leases or control ownership."""
import asyncio
import io
import threading
import time

import httpx
import numpy as np
import pytest
from PIL import Image

from test_controller import setup_controller, ready, until


class CameraSource:
    def __init__(self):
        self.sequence = 1
        self.received = time.monotonic()
        self.calls = 0
        self.error = None
        self.image = np.full((480, 640, 3), 90, dtype=np.uint8)
        self.image.flags.writeable = False

    def __call__(self):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return {'head': {'image': self.image, 'sequence': self.sequence, 'received': self.received}}

    def advance(self):
        self.sequence += 1
        self.received = time.monotonic()


@pytest.mark.asyncio
async def test_preview_shares_one_encoding_and_does_no_work_without_subscribers():
    from robot_control.web_preview import WebPreview
    source = CameraSource()
    preview = WebPreview(source)
    assert preview.status()['cameras']['head']['subscribers'] == 0
    assert source.calls == 0
    first, second = preview.subscribe('head'), preview.subscribe('head')
    try:
        frame = await asyncio.wait_for(first.next_frame(), 2)
        same = await asyncio.wait_for(second.next_frame(), 2)
        assert frame is same
        assert Image.open(io.BytesIO(frame.jpeg)).size == (640, 480)
        assert not source.image.flags.writeable
        await asyncio.sleep(.09)
        assert preview.status()['cameras']['head']['encoded_total'] == 1
        first.close()
        source.advance()
        new = await asyncio.wait_for(second.next_frame(), 2)
        assert new.sequence == 2
        assert preview.status()['cameras']['head']['subscribers'] == 1
    finally:
        first.close()
        second.close()
        assert preview.close(time.monotonic() + 1)
    assert not any(thread.is_alive() for thread in preview.threads)


@pytest.mark.asyncio
async def test_slow_reader_receives_latest_frame_and_last_unsubscribe_stops_encoding():
    from robot_control.web_preview import WebPreview
    source = CameraSource()
    preview = WebPreview(source)
    reader = preview.subscribe('head')
    try:
        await asyncio.wait_for(reader.next_frame(), 2)
        for _ in range(4):
            source.advance()
            await asyncio.sleep(.04)
        latest = await asyncio.wait_for(reader.next_frame(), 2)
        assert latest.sequence == source.sequence
        reader.close()
        count = preview.status()['cameras']['head']['encoded_total']
        source.advance()
        await asyncio.sleep(.07)
        assert preview.status()['cameras']['head']['encoded_total'] == count
        assert preview.status()['cameras']['head']['subscribers'] == 0
    finally:
        reader.close()
        assert preview.close(time.monotonic() + 1)


@pytest.mark.asyncio
async def test_stale_frames_are_not_repeated_and_camera_recovers():
    from robot_control.web_preview import WebPreview
    source = CameraSource()
    preview = WebPreview(source, stale_after=.06)
    reader = preview.subscribe('head')
    try:
        await asyncio.wait_for(reader.next_frame(), 2)
        await asyncio.sleep(.09)
        assert preview.status()['cameras']['head']['state'] == 'stale'
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.next_frame(), .04)
        assert preview.status()['cameras']['head']['encoded_total'] == 1
        source.advance()
        assert (await asyncio.wait_for(reader.next_frame(), 2)).sequence == 2
        assert preview.status()['cameras']['head']['state'] == 'live'
    finally:
        reader.close()
        assert preview.close(time.monotonic() + 1)


@pytest.mark.asyncio
async def test_missing_camera_and_source_error_recover_without_new_threads():
    from robot_control.web_preview import WebPreview
    source = CameraSource()
    source.error = 'camera unavailable'
    preview = WebPreview(source)
    reader = preview.subscribe('head')
    missing = preview.subscribe('left_wrist')
    try:
        await asyncio.to_thread(until, lambda: preview.status()['cameras']['head']['error'] == 'camera unavailable')
        source.error = None
        assert (await asyncio.wait_for(reader.next_frame(), 2)).sequence == 1
        await asyncio.to_thread(until, lambda: preview.status()['cameras']['left_wrist']['state'] == 'waiting')
        assert len(preview.threads) == 2
        with pytest.raises(ValueError):
            preview.subscribe('invalid')
    finally:
        reader.close()
        missing.close()
        assert preview.close(time.monotonic() + 1)


def test_hung_encoder_has_bounded_cleanup_and_late_result_is_discarded():
    from robot_control.web_preview import WebPreview
    source = CameraSource()
    started, release = threading.Event(), threading.Event()

    def encode(image):
        started.set()
        release.wait(3)
        return b'late'

    async def scenario():
        preview = WebPreview(source, encoder=encode)
        reader = preview.subscribe('head')
        try:
            assert await asyncio.to_thread(started.wait, 1)
            start = time.monotonic()
            assert not preview.close(start + .05)
            assert time.monotonic() - start < .3
            assert await reader.next_frame() is None
            assert preview.status()['cameras']['head']['encoded_total'] == 0
        finally:
            release.set()
            reader.close()
            assert preview.close(time.monotonic() + 1)
    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_sampling_cost_does_not_accumulate_into_frame_period():
    from robot_control.web_preview import WebPreview
    source = CameraSource()
    epoch = time.monotonic()

    def sampled():
        time.sleep(.004)  # Cache sampling is real work, not part of the 33ms wait.
        now = time.monotonic()
        source.sequence = int((now - epoch) * 30) + 1
        source.received = epoch + (source.sequence - 1) / 30
        return source()

    preview = WebPreview(sampled)
    reader = preview.subscribe('head')
    try:
        await asyncio.wait_for(reader.next_frame(), 2)
        before = preview.status()['cameras']['head']['encoded_total']
        await asyncio.sleep(2)
        assert preview.status()['cameras']['head']['encoded_total'] - before >= 57
    finally:
        reader.close()
        assert preview.close(time.monotonic() + 1)


def test_real_http_preview_works_while_paused_and_closes_streams(setup_controller, monkeypatch):
    from robot_control.mcp_server import MCPService
    controller, robot, _ = setup_controller
    ready(controller)
    monkeypatch.setenv('RC_PREVIEW_TEST_TOKEN', 'test-preview-token')
    service = MCPService(controller, {'port': 0, 'token_env': 'RC_PREVIEW_TEST_TOKEN'}, allow_ephemeral=True)
    service.start()
    base = service.url.removesuffix('/mcp')
    headers = {'Authorization': 'Bearer test-preview-token'}
    try:
        with httpx.Client(trust_env=False, timeout=3) as http:
            assert http.get(base + '/preview/status').status_code == 401
            assert http.get(base + '/preview/invalid.mjpg', headers=headers).status_code == 404
            with http.stream('GET', base + '/preview/head.mjpg', headers=headers) as response:
                assert response.status_code == 200
                assert response.headers['content-type'].startswith('multipart/x-mixed-replace')
                payload = b''
                iterator = response.iter_bytes()
                for chunk in iterator:
                    payload += chunk
                    if b'\xff\xd9' in payload:
                        break
                jpeg = payload[payload.index(b'\xff\xd8'):payload.index(b'\xff\xd9') + 2]
                assert Image.open(io.BytesIO(jpeg)).size == (640, 480)
                assert b'X-Sequence:' in payload
                status = http.get(base + '/preview/status', headers=headers).json()
                assert status['cameras']['head']['subscribers'] == 1
                assert controller.status()['state'] == 'paused'
                assert controller.status()['control_owner'] == 'policy'
            until(lambda: service.preview.status()['cameras']['head']['subscribers'] == 0)
            with http.stream('GET', base + '/preview/head.mjpg', headers=headers) as response:
                iterator = response.iter_bytes()
                assert next(iterator)
                service.close()
                assert not service.thread.is_alive()
    finally:
        service.close()


@pytest.mark.asyncio
async def test_http_lifespan_honors_existing_absolute_cleanup_deadline(setup_controller, monkeypatch):
    from robot_control.mcp_server import create_app, mcp_settings
    controller, _, _ = setup_controller
    ready(controller)
    deadline = time.monotonic() - .01
    received = []

    class ClosingPreview:
        def close(self, value):
            received.append(value)
            return False

    app = create_app(controller, mcp_settings({'port': 0}, allow_ephemeral=True), preview=ClosingPreview())
    with monkeypatch.context() as patch:
        patch.setattr(controller, '_shutdown_deadline', deadline)
        async with app.router.lifespan_context(app):
            pass
    assert received == [deadline]
    assert controller.exit_code == 1
