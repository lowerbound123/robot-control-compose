"""Bounded background sampling/encoding, polled by the TUI thread."""
from concurrent.futures import Future
from queue import Empty, Queue
from threading import Thread
from time import monotonic

from .telemetry import CAMERAS, CAMERA_PREVIEW_HZ


class _PreviewPool:
    """Display-only daemon workers, with bounded work and no atexit join.

    ThreadPoolExecutor registers even running jobs for interpreter-exit joins.
    A stuck image encoder must not hold up the controller's shutdown. Workers
    never write terminal output or robot commands; their results are disposable.
    """

    def __init__(self):
        self._queue = Queue(maxsize=5)
        self._threads = []
        self.closed = False

    def submit(self, function, *args):
        if self.closed:
            raise RuntimeError("preview pool is closed")
        if not self._threads:
            for index in range(5):
                thread = Thread(target=self._work, name=f"rc-preview-{index}", daemon=True)
                self._threads.append(thread)
                thread.start()
        future = Future()
        self._queue.put_nowait((future, function, args))
        return future

    def _work(self):
        while (job := self._queue.get()) is not None:
            future, function, args = job
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(function(*args))
                except BaseException as exc:
                    future.set_exception(exc)
            # Do not retain the last RGB array while waiting for another job.
            del job, future, function, args

    def close(self):
        if self.closed:
            return
        self.closed = True
        while True:
            try:
                self._queue.get_nowait()[0].cancel()
            except Empty:
                break
        for _ in self._threads:
            self._queue.put_nowait(None)


class LivePreview:
    def __init__(self, camera_source, monitor_source, prepare, protocol):
        self.camera_source = camera_source
        self.monitor_source = monitor_source
        self.prepare = prepare
        self.protocol = protocol
        # One sampler per panel and one encoder per camera, with no job backlog.
        self._pool = _PreviewPool()
        self.enabled = {"camera": False, "monitor": False}
        self._generation = {"camera": 0, "monitor": 0}
        self._camera_job = self._monitor_job = None
        self._encoding = {}
        self._last_keys = {}
        self._next_encode_at = {}
        self.samples = {}
        self.camera_error = None
        self.closed = False

    def set_enabled(self, panel, enabled):
        if self.closed or self.enabled[panel] == enabled:
            return
        self.enabled[panel] = enabled
        self._generation[panel] += 1
        if panel == "camera":
            self.samples = {}
            self.camera_error = None
            self._last_keys.clear()
            self._next_encode_at.clear()
            if not enabled:
                for future, _ in self._encoding.values():
                    future.cancel()
        # Keep running futures tracked until completion, even across toggles.
        job = self._camera_job if panel == "camera" else self._monitor_job
        if not enabled and job:
            job[0].cancel()

    def connected(self, camera, now):
        sample = self.samples.get(camera)
        return bool(self.enabled["camera"] and not self.camera_error and sample
                    and 0 <= now - sample["received"] < 1.)

    def poll(self, sizes):
        result = {"frames": {}, "monitor": None, "render_error": None}
        if self.closed:
            return result
        if self._camera_job and self._camera_job[0].done():
            future, generation = self._camera_job
            self._camera_job = None
            if self.enabled["camera"] and generation == self._generation["camera"]:
                try:
                    self.samples = future.result()
                    self.camera_error = None
                except Exception as exc:
                    self.camera_error = str(exc)
        if self._monitor_job and self._monitor_job[0].done():
            future, generation = self._monitor_job
            self._monitor_job = None
            if self.enabled["monitor"] and generation == self._generation["monitor"]:
                try:
                    result["monitor"] = future.result()
                except Exception as exc:
                    result["monitor"] = {"qpos": None, "epos": None, "targets": {}, "error": str(exc)}

        for camera, (future, key) in tuple(self._encoding.items()):
            if not future.done():
                continue
            del self._encoding[camera]
            if (self.enabled["camera"] and key[0] == self._generation["camera"]
                    and key[2] == sizes.get(camera)):
                try:
                    result["frames"][camera] = future.result()
                except Exception as exc:
                    result["render_error"] = f"{camera}: {exc}"

        if self.enabled["camera"]:
            if self._camera_job is None:
                self._camera_job = (self._pool.submit(self.camera_source), self._generation["camera"])
            now = monotonic()
            for camera in CAMERAS:
                sample, size = self.samples.get(camera), sizes.get(camera)
                if sample is None or size is None or min(size) <= 0 or camera in self._encoding:
                    continue
                key = (self._generation["camera"], sample["sequence"], size)
                # Limit image work independently of the 30 Hz monitor polling.
                if (self._last_keys.get(camera) != key
                        and now >= self._next_encode_at.get(camera, 0.)):
                    self._last_keys[camera] = key
                    self._encoding[camera] = (self._pool.submit(
                        self.prepare, sample["image"], size, self.protocol), key)
                    self._next_encode_at[camera] = now + 1 / CAMERA_PREVIEW_HZ
        if self.enabled["monitor"] and self._monitor_job is None:
            self._monitor_job = (self._pool.submit(self.monitor_source), self._generation["monitor"])
        return result

    def close(self):
        if self.closed:
            return
        self.set_enabled("camera", False)
        self.set_enabled("monitor", False)
        self.closed = True
        self._pool.close()
        self.samples.clear()
        self._encoding.clear()
        self._camera_job = self._monitor_job = None
