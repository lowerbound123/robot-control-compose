"""Bounded lossless image streams and their durable, independently sealed index.

The file/reader helpers use only the standard library so the offline reducer can
also consume recordings without importing RC, NumPy or a policy runtime.
"""
from collections import OrderedDict
from concurrent.futures import Future
import fcntl
import functools
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time

STORAGE = 'mkv_ffv1_v1'
FPS, SEGMENT_FRAMES = 30, 150
CAMERAS = ('head', 'left_wrist', 'right_wrist')
INDEX = 'video_segments.jsonl'


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try: os.fsync(fd)
    finally: os.close(fd)


class SessionLock:
    def __init__(self, root, *, create=True):
        self.fd = None
        flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | (os.O_CREAT if create else 0)
        try: fd = os.open(Path(root) / 'recording.lock', flags, 0o600)
        except FileNotFoundError:
            if create: raise
            return
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


@functools.lru_cache(maxsize=4)
def _check_encoder(executable):
    result = subprocess.run([executable, '-hide_banner', '-h', 'encoder=ffv1'],
                            capture_output=True, text=True, timeout=10)
    if result.returncode or 'Encoder ffv1' not in result.stdout or 'bgr0' not in result.stdout:
        raise RuntimeError('FFmpeg requires the FFV1 encoder with lossless bgr0 support')
    return executable


def encoder_path():
    executable = shutil.which('ffmpeg')
    if not executable:
        raise RuntimeError('recording requires ffmpeg with FFV1; rebuild the RC image')
    return _check_encoder(executable)


def pixel_hash(data, width, height):
    digest = hashlib.sha256(str(((height, width, 3), 'uint8')).encode())
    digest.update(data)
    return digest.hexdigest()


def image_info(pixels):
    if (str(getattr(pixels, 'dtype', '')) != 'uint8' or len(pixels.shape) != 3
            or pixels.shape[2] != 3 or min(pixels.shape[:2]) <= 0):
        raise ValueError('recording video requires nonempty uint8 RGB images')
    view = memoryview(pixels).cast('B')
    height, width = pixels.shape[:2]
    return width, height, pixel_hash(view, width, height)


class VideoStream:
    """One pipe owner. Nothing in the text writer waits for its writes."""
    def __init__(self, store, camera, kind, *, queue_limit=128):
        if not isinstance(camera, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', camera):
            raise ValueError('invalid recording camera name')
        self.store, self.camera, self.kind = store, camera, kind
        self.queue = queue.Queue(maxsize=queue_limit)
        self.closing = threading.Event()
        self.cancelled = threading.Event()
        self.finished = Future()
        self.width = self.height = None
        self.error = None
        self.next_index = self.written = 0
        self.last_rgb = None
        self._process = self._errors = None
        self._process_lock = threading.Lock()
        self._segment = None
        self._hashes = []
        self._temporary = self._destination = None
        self.thread = threading.Thread(target=self._run, name=f'record-video-{kind}-{camera}', daemon=True)
        self.thread.start()

    def filename(self, index):
        return f'video_raw/{self.kind}/{self.camera}/{index // SEGMENT_FRAMES:06d}.mkv'

    def append(self, index, pixels):
        if self.closing.is_set() or self.error:
            raise RuntimeError(str(self.error or 'video stream is closing'))
        if index != self.next_index:
            raise ValueError('video slots must be contiguous')
        future = Future()
        self.store._admit(pixels, future)
        try: self.queue.put_nowait((index, pixels, future))
        except queue.Full:
            future.set_exception(RuntimeError('recording video queue is full'))
            raise RuntimeError('recording video queue is full') from None
        self.next_index += 1
        return future

    def _open_encoder(self, index):
        self._segment = index // SEGMENT_FRAMES
        self._hashes = []
        self._destination = self.store.root / self.filename(index)
        folder = self._destination.parent
        folder.mkdir(parents=True, exist_ok=True)
        self._temporary = self._destination.with_suffix('.mkv.part')
        command = [self.store.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror',
            '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{self.width}x{self.height}',
            '-framerate', str(FPS), '-i', 'pipe:0', '-an', '-c:v', 'ffv1', '-level', '3',
            '-coder', '0', '-context', '0', '-g', '1', '-slicecrc', '1', '-threads', '2',
            '-pix_fmt', 'bgr0', '-f', 'matroska', '-n', str(self._temporary)]
        with self._process_lock:
            if self.cancelled.is_set(): raise RuntimeError('video encoding cancelled')
            self._errors = tempfile.TemporaryFile()
            try:
                self._process = subprocess.Popen(command, stdin=subprocess.PIPE, bufsize=0,
                    stdout=subprocess.DEVNULL, stderr=self._errors)
            except BaseException:
                self._errors.close(); self._errors = None
                raise

    def _write_pixels(self, pixels):
        view = memoryview(pixels).cast('B')
        while view:
            if self.cancelled.is_set(): raise RuntimeError('video encoding cancelled')
            count = self._process.stdin.write(view)
            if not count: raise RuntimeError('FFmpeg video pipe made no progress')
            view = view[count:]

    def _finish_segment(self):
        process = self._process
        if process is None: return
        try:
            process.stdin.close()
            result = process.wait(timeout=max(.001, min(30., self.store.deadline-time.monotonic())))
            if result:
                self._errors.seek(0, os.SEEK_END)
                self._errors.seek(max(0, self._errors.tell()-3000))
                raise RuntimeError('FFmpeg failed: ' + self._errors.read().decode('utf-8', 'replace'))
            if self.cancelled.is_set(): raise RuntimeError('video encoding cancelled')
            with self._temporary.open('rb') as stream: os.fsync(stream.fileno())
            # Link refuses an unexpected replacement; only a complete encoder
            # output receives the final name. The index is the durable proof.
            os.link(self._temporary, self._destination)
            self._temporary.unlink()
            sync_directory(self._destination.parent)
            sync_directory(self._destination.parent.parent)
            sync_directory(self._destination.parent.parent.parent)
            sync_directory(self.store.root)
            self.store._seal({'event': 'video.segment', 'storage': STORAGE, 'stream': self.kind,
                'camera': self.camera, 'segment': self._segment,
                'file': str(self._destination.relative_to(self.store.root)), 'frames': len(self._hashes),
                'width': self.width, 'height': self.height, 'fps': FPS, 'codec': 'ffv1',
                'first_frame': self._segment * SEGMENT_FRAMES, 'sha256': self._hashes})
        finally:
            if process.poll() is None:
                process.kill(); process.wait()
            if process.stdin and not process.stdin.closed: process.stdin.close()
            with self._process_lock:
                self._process = None
                if self._errors is not None: self._errors.close(); self._errors = None

    def _write_frame(self, index, rgb):
        if self._process is None: self._open_encoder(index)
        self._write_pixels(rgb)
        self._hashes.append(pixel_hash(rgb, self.width, self.height))
        self.written += 1
        if len(self._hashes) == SEGMENT_FRAMES: self._finish_segment()

    def _run(self):
        job = pixels = None
        try:
            while not self.cancelled.is_set():
                try: job = self.queue.get(timeout=.02)
                except queue.Empty:
                    if self.closing.is_set(): break
                    continue
                index, pixels, future = job
                if pixels is not None:
                    width, height = pixels.shape[1], pixels.shape[0]
                    if self.width is None:
                        self.width, self.height = width, height
                        black = bytes(width * height * 3)
                        # Missing slots before the first size was known retain
                        # no images. Backfill them only on this video thread.
                        for previous in range(self.written, index): self._write_frame(previous, black)
                    elif (width, height) != (self.width, self.height):
                        raise ValueError('camera resolution changed during recording')
                    rgb = pixels.tobytes()
                    self._write_frame(index, rgb)
                    self.last_rgb = rgb
                elif self.width is not None:
                    self._write_frame(index, self.last_rgb or bytes(self.width*self.height*3))
                # Retain only a bounded bytes copy for future hold placeholders,
                # never the original acknowledged NumPy/IPC object.
                pixels = job = None
                future.set_result(None)
                future = None
            if self.cancelled.is_set(): raise RuntimeError('video encoding cancelled')
            self._finish_segment()
            if self.width is None and self.next_index:
                self.store._seal({'event':'video.unavailable', 'stream':self.kind,
                                  'camera':self.camera, 'frames':self.next_index})
            self.finished.set_result(None)
        except BaseException as exc:
            self.error = exc
            if job is not None and not job[2].done(): job[2].set_exception(exc)
            pixels = job = None
            self._kill_encoder()
            while True:
                try: _, _, future = self.queue.get_nowait()
                except queue.Empty: break
                future.set_exception(RuntimeError(str(exc)))
            self.finished.set_exception(exc)
        finally:
            self.last_rgb = None

    def _kill_encoder(self):
        with self._process_lock:
            process = self._process
            if process is not None:
                if process.poll() is None: process.kill()
                process.wait()
                if process.stdin and not process.stdin.closed: process.stdin.close()
                self._process = None
            if self._errors is not None: self._errors.close(); self._errors = None

    def cancel(self):
        self.cancelled.set(); self.closing.set()
        # Kill first: this releases a thread blocked on a full rawvideo pipe.
        with self._process_lock:
            if self._process is not None and self._process.poll() is None: self._process.kill()


class VideoStore:
    def __init__(self, root, *, cache_limit=4096):
        self.root = Path(root)
        self.ffmpeg = encoder_path()
        self.streams, self.cache, self.previous = {}, OrderedDict(), {}
        self.cache_limit = cache_limit
        self.deadline = float('inf')
        self._lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._index = None
        self._closing = False
        self._pending_frames = self._pending_bytes = self._segments = 0

    def _stream(self, camera, kind):
        key = kind, camera
        if key not in self.streams: self.streams[key] = VideoStream(self, camera, kind)
        return self.streams[key]

    def _admit(self, pixels, future):
        size = 0 if pixels is None else pixels.nbytes
        with self._lock:
            self._pending_frames += 1; self._pending_bytes += size
        def release(_):
            with self._lock:
                self._pending_frames -= 1; self._pending_bytes -= size
        future.add_done_callback(release)

    def _seal(self, entry):
        # Disk synchronization must never own the admission/statistics lock:
        # those operations run on the text writer's main thread.
        with self._index_lock:
            if self._index is None: self._index = (self.root / INDEX).open('ab', buffering=0)
            fd = self._index.fileno()
            offset = os.lseek(fd,0,os.SEEK_END)
            pending = memoryview((json.dumps(entry,separators=(',',':'))+'\n').encode())
            try:
                while pending:
                    count = os.write(fd,pending)
                    if count <= 0: raise OSError('video index write made no progress')
                    pending = pending[count:]
                os.fsync(fd)
                sync_directory(self.root)
            except BaseException:
                try:
                    os.ftruncate(fd,offset); os.fsync(fd)
                except OSError: pass
                raise
        with self._lock:
            self._segments += int(entry['event'] == 'video.segment')

    def _remember(self, camera, digest, reference, future):
        self.cache[(camera, digest)] = (dict(reference), future)
        self.cache.move_to_end((camera, digest))
        while len(self.cache) > self.cache_limit: self.cache.popitem(last=False)

    def _add(self, camera, frame, index, kind):
        stream = self._stream(camera, kind)
        pixels = frame.get('image') if frame.get('valid', True) else None
        reference = {k:v for k,v in frame.items() if k != 'image'}
        if pixels is not None:
            width, height, digest = image_info(pixels)
            details = dict(width=width, height=height, sha256=digest)
            self.previous[(kind,camera)] = details
            reference.update(details, placeholder=None)
        else:
            details = self.previous.get((kind,camera), {})
            reference.update(details, missing=True, valid=False,
                             placeholder='hold' if details else 'black')
        reference.update(file=stream.filename(index), frame_index=index % SEGMENT_FRAMES,
                         storage=STORAGE)
        future = stream.append(index, pixels)
        if pixels is not None: self._remember(camera, digest, reference, future)
        return reference, future

    def sample(self, frames, sample_id):
        if self._closing: raise RuntimeError('video store is closing')
        images, pending = {}, []
        for camera in sorted(set(CAMERAS) | set(frames)):
            reference, future = self._add(camera, frames.get(camera, {}), sample_id, 'samples')
            images[camera] = reference; pending.append(future)
        return images, pending

    def observation(self, frames):
        if self._closing: raise RuntimeError('video store is closing')
        images, pending = {}, []
        for camera, frame in frames.items():
            if not frame.get('valid', True) or frame.get('image') is None: continue
            _, _, digest = image_info(frame['image'])
            cached = self.cache.get((camera, digest))
            if cached is None:
                stream = self._stream(camera, 'observations')
                reference, future = self._add(camera, frame, stream.next_index, 'observations')
            else:
                reference, future = cached
                # Reuse pixels/location, never the sampled frame's identity.
                # Equal pixels can belong to a different policy lease/time.
                reference = {key:reference[key] for key in ('file','frame_index','storage',
                             'width','height','sha256','placeholder')}
                reference.update({k:v for k,v in frame.items() if k != 'image'})
                self.cache.move_to_end((camera, digest))
            images[camera] = reference; pending.append(future)
        return images, pending

    def stats(self):
        with self._lock:
            return {'video_pending_frames':self._pending_frames,'video_pending_bytes':self._pending_bytes,
                    'video_segments_written':self._segments}

    def check(self):
        for stream in self.streams.values():
            if stream.error is not None: raise RuntimeError(str(stream.error)) from stream.error

    def begin_close(self, deadline):
        self._closing = True; self.deadline = min(self.deadline, deadline)
        for stream in self.streams.values(): stream.closing.set()

    def closed(self): return all(stream.finished.done() for stream in self.streams.values())

    def close(self, deadline):
        self.begin_close(deadline)
        try:
            for stream in self.streams.values(): stream.finished.result(max(0., deadline-time.monotonic()))
            self.check()
        except BaseException:
            self.cancel()
            raise
        finally:
            for stream in self.streams.values(): stream.thread.join(max(0., deadline-time.monotonic()))
            with self._index_lock:
                if self._index is not None: self._index.close(); self._index = None
            self.cache.clear()

    def cancel(self):
        self._closing = True
        deadline = time.monotonic() + 2
        for stream in self.streams.values(): stream.cancel()
        for stream in self.streams.values(): stream.thread.join(max(0, deadline-time.monotonic()))
        with self._index_lock:
            if self._index is not None: self._index.close(); self._index = None
        self.cache.clear()


def safe_video_path(root, relative):
    root = Path(root)
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'video_raw':
        raise ValueError(f'invalid video path: {relative}')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink(): raise ValueError(f'symlink video path: {relative}')
    return current


def segments(root):
    """Only a complete index record proves that a segment was sealed."""
    records, seen = [], set()
    with (Path(root) / INDEX).open(encoding='utf-8') as source:
        for line in source:
            try: entry = json.loads(line)
            except ValueError:
                # A crash can leave a partial last index line, never a segment.
                if not line.endswith('\n'): break
                raise ValueError('corrupt video segment index') from None
            if entry.get('event') == 'video.unavailable': continue
            if entry.get('event') != 'video.segment' or entry.get('storage') != STORAGE:
                raise ValueError('unsupported video segment index')
            name = entry['file']
            safe_video_path(root, name)
            if (entry['stream'] not in ('samples','observations')
                    or not re.fullmatch(r'[A-Za-z0-9_-]+',entry['camera'])
                    or type(entry['segment']) is not int or entry['segment'] < 0
                    or name != f"video_raw/{entry['stream']}/{entry['camera']}/{entry['segment']:06d}.mkv"):
                raise ValueError('invalid video stream identity')
            if (name in seen or not 1 <= entry['frames'] <= SEGMENT_FRAMES
                    or len(entry['sha256']) != entry['frames'] or min(entry['width'],entry['height']) <= 0
                    or entry['fps'] != FPS or entry['first_frame'] != entry['segment']*SEGMENT_FRAMES):
                raise ValueError(f'invalid video segment index: {name}')
            seen.add(name); records.append(entry)
    return sorted(records, key=lambda row:(row['stream'], row['camera'], row['segment']))


def decode_segment(root, entry, *, ffmpeg=None):
    """Yield verified rgb24 frames; full consumption also checks exact count."""
    path = safe_video_path(root, entry['file'])
    if not path.is_file(): raise ValueError(f'missing sealed video segment: {path}')
    command = [ffmpeg or encoder_path(), '-hide_banner', '-loglevel', 'error', '-nostdin',
               '-xerror', '-err_detect', 'explode', '-i', str(path), '-map', '0:v:0',
               '-threads', '2', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1']
    size = entry['width'] * entry['height'] * 3
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            for expected in entry['sha256']:
                frame = process.stdout.read(size)
                if len(frame) != size or pixel_hash(frame, entry['width'], entry['height']) != expected:
                    raise ValueError(f'video pixel hash mismatch: {path}')
                yield frame
            if process.stdout.read(1): raise ValueError(f'extra video frames: {path}')
            if process.wait():
                errors.seek(0)
                raise ValueError(f'video decode failed: {path}: {errors.read(3000)!r}')
        finally:
            if process.poll() is None: process.kill()
            process.wait(); process.stdout.close()


def read_rgb(root, reference):
    """Read one committed video reference, refusing unsealed material."""
    entry = next((row for row in segments(root) if row['file'] == reference['file']), None)
    if entry is None: raise ValueError('video reference has not been sealed')
    index = reference['frame_index']
    if not 0 <= index < entry['frames']: raise ValueError('invalid video frame index')
    selected = None
    for position, frame in enumerate(decode_segment(root, entry)):
        if position == index: selected = frame
    if reference.get('sha256') and pixel_hash(selected,entry['width'],entry['height']) != reference['sha256']:
        raise ValueError('video reference pixel hash mismatch')
    return selected, (entry['width'], entry['height'])
