"""Bounded, display-only JPEG encoding over immutable robot camera caches.

No policy leases, commands, or controller locks are acquired here. One encoder
per subscribed camera owns one latest frame; subscribers never own frame queues.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import io
import threading
import time

from PIL import Image, ImageOps

CAMERAS = ('head', 'left_wrist', 'right_wrist')
SIZE = (640, 480)
FPS = 30


@dataclass(frozen=True)
class PreviewFrame:
    identity: int
    sequence: int
    received: float
    encoded_at: float
    jpeg: bytes


def encode_jpeg(pixels):
    with Image.fromarray(pixels) as original:
        with ImageOps.pad(original, SIZE, method=Image.Resampling.BILINEAR, color=(15, 18, 22)) as image:
            output = io.BytesIO()
            image.convert('RGB').save(output, format='JPEG', quality=80)
            return output.getvalue()


def _rate(samples, now):
    while samples and samples[0] < now - 2:
        samples.popleft()
    if len(samples) < 2 or now - samples[-1] > .25:
        return 0.
    return round((len(samples) - 1) / max(.001, samples[-1] - samples[0]), 2)


class PreviewSubscription:
    def __init__(self, owner, camera):
        self.owner, self.camera = owner, camera
        self.loop = asyncio.get_running_loop()
        self.event = asyncio.Event()
        self.notified = False
        self.closed = False
        self.last_identity = 0

    async def next_frame(self):
        while not self.closed:
            if self.owner.stopping():
                self.close()
                return None
            with self.owner.condition:
                self.event.clear()
                self.notified = False
                if self.owner.closed:
                    self.close()
                    return None
                channel = self.owner.channels[self.camera]
                frame = channel['frame']
                if (frame is not None and frame.identity != self.last_identity
                        and time.monotonic() - frame.received <= self.owner.stale_after):
                    self.last_identity = frame.identity
                    if channel['last_output'] != frame.identity:
                        channel['last_output'] = frame.identity
                        channel['output_total'] += 1
                        channel['output_times'].append(time.monotonic())
                    return frame
            try:
                await asyncio.wait_for(self.event.wait(), .25)
            except asyncio.TimeoutError:
                pass
        return None

    def close(self):
        with self.owner.condition:
            if self.closed:
                return
            self.closed = True
            channel = self.owner.channels[self.camera]
            channel['readers'].discard(self)
            if not channel['readers']:
                channel['generation'] += 1
                channel['frame'] = None
            self.owner.condition.notify_all()


class WebPreview:
    def __init__(self, source, *, stopping=lambda: False, stale_after=2., encoder=encode_jpeg):
        self.source, self.stopping, self.encoder = source, stopping, encoder
        self.stale_after = stale_after
        self.condition = threading.Condition(threading.RLock())
        self.closed = False
        self.threads = []
        self.channels = {camera: dict(readers=set(), generation=0, frame=None, thread=None,
            sequence=None, received=None, source_size=None, error=None, encoded_total=0,
            output_total=0, last_output=0, encode_ms=None,
            source_samples=deque(maxlen=128), encode_times=deque(maxlen=128),
            output_times=deque(maxlen=128)) for camera in CAMERAS}

    def subscribe(self, camera):
        if camera not in self.channels:
            raise ValueError('unknown camera')
        reader = PreviewSubscription(self, camera)
        with self.condition:
            if self.closed or self.stopping():
                raise RuntimeError('preview is stopping')
            channel = self.channels[camera]
            channel['readers'].add(reader)
            if channel['thread'] is None:
                thread = threading.Thread(target=self._encode, args=(camera,),
                                          name='rc-web-preview-' + camera, daemon=True)
                channel['thread'] = thread
                self.threads.append(thread)
                thread.start()
            self.condition.notify_all()
        return reader

    def _wake_readers(self, channel):
        for reader in tuple(channel['readers']):
            if reader.notified:
                continue
            reader.notified = True
            try:
                reader.loop.call_soon_threadsafe(reader.event.set)
            except RuntimeError:
                reader.close()

    def _encode(self, camera):
        channel = self.channels[camera]
        last_key = None
        next_due = 0.
        while True:
            with self.condition:
                while not channel['readers'] and not self.closed:
                    last_key, next_due = None, 0.
                    self.condition.wait()
                if self.closed or self.stopping():
                    return
                generation = channel['generation']
                delay = next_due - time.monotonic()
                if delay > 0:
                    self.condition.wait(min(delay, .1))
                    continue
            try:
                sample = self.source().get(camera)
                now = time.monotonic()
                with self.condition:
                    if sample is not None:
                        sequence, received = sample['sequence'], sample['received']
                        if sequence != channel['sequence'] or received != channel['received']:
                            if channel['sequence'] is not None and sequence <= channel['sequence']:
                                channel['source_samples'].clear()
                            channel['source_samples'].append((received, sequence))
                        channel.update(sequence=sequence, received=received,
                                       source_size=[sample['image'].shape[1], sample['image'].shape[0]], error=None)
                    else:
                        channel['error'] = None
                key = (generation, sample['sequence'], sample['received']) if sample is not None else None
                if sample is None or key == last_key or now - sample['received'] > self.stale_after:
                    with self.condition:
                        self.condition.wait(.002 if sample is not None else .05)
                    continue
                last_key = key
                start = time.monotonic()
                jpeg = self.encoder(sample['image'])
                finished = time.monotonic()
                with self.condition:
                    if self.closed or channel['generation'] != generation or not channel['readers']:
                        continue
                    channel['encoded_total'] += 1
                    channel['encode_times'].append(finished)
                    channel['encode_ms'] = round((finished - start) * 1000, 3)
                    channel['frame'] = PreviewFrame(channel['encoded_total'], sample['sequence'],
                                                    sample['received'], finished, jpeg)
                    self._wake_readers(channel)
                # Keep a stable pacing clock; encoding duration does not add to
                # every 33.3ms interval. A late source is awaited, never repeated.
                next_due = next_due + 1 / FPS if next_due else start + 1 / FPS
                if next_due < finished - 1 / FPS:
                    next_due = finished
            except Exception as exc:
                with self.condition:
                    channel['error'] = str(exc)[:512]
                    self.condition.wait(.05)

    def status(self):
        now = time.monotonic()
        cameras = {}
        with self.condition:
            for camera, channel in self.channels.items():
                age = None if channel['received'] is None else max(0., now - channel['received'])
                subscribed = bool(channel['readers'])
                state = ('closed' if self.closed else 'idle' if not subscribed else
                         'error' if channel['error'] else 'waiting' if age is None else
                         'stale' if age > self.stale_after else 'live')
                samples = channel['source_samples']
                while samples and samples[0][0] < now - 2:
                    samples.popleft()
                source_fps = ((samples[-1][1] - samples[0][1]) / max(.001, samples[-1][0] - samples[0][0])
                              if len(samples) > 1 and age is not None and age < .25 else 0.)
                cameras[camera] = dict(state=state, subscribers=len(channel['readers']),
                    source_sequence=channel['sequence'], frame_age_s=None if age is None else round(age, 3),
                    source_size=channel['source_size'], width=SIZE[0], height=SIZE[1],
                    source_fps=round(source_fps, 2), encode_fps=_rate(channel['encode_times'], now),
                    output_fps=_rate(channel['output_times'], now), encode_ms=channel['encode_ms'],
                    encoded_total=channel['encoded_total'], output_total=channel['output_total'], error=channel['error'])
        return {'target_fps': FPS, 'jpeg_quality': 80, 'cameras': cameras}

    def stop(self):
        with self.condition:
            self.closed = True
            for channel in self.channels.values():
                channel['frame'] = None
                self._wake_readers(channel)
            self.condition.notify_all()

    def close(self, deadline):
        self.stop()
        for thread in self.threads:
            thread.join(max(0., deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self.threads)


async def multipart_frames(reader):
    try:
        while (frame := await reader.next_frame()) is not None:
            yield (f'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame.jpeg)}\r\n'
                   f'X-Sequence: {frame.sequence}\r\nX-Received-Monotonic: {frame.received:.9f}\r\n'
                   f'X-Encoded-Monotonic: {frame.encoded_at:.9f}\r\n\r\n').encode() + frame.jpeg + b'\r\n'
    finally:
        reader.close()
