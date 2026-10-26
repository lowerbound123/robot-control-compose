"""Controller-owned, best-effort 10 Hz scalar polling; UI reads cached snapshots."""
from collections import OrderedDict
import copy
import threading
import time


class PolicyMonitors:
    PERIOD = .1
    SOFT_TIMEOUT = 1.

    def __init__(self, source, *, clock=time.monotonic, threaded=True):
        self.source, self.clock, self.threaded = source, clock, threaded
        self.lock = threading.RLock()
        self.entries = OrderedDict()
        self._epoch = 0
        self._revision = 0
        self._closed = False
        self._wake = threading.Event()
        self.thread = None

    def available(self):
        members, _ = self.source()
        return [name for name, worker in members.items() if worker.supports_monitor]

    def _reset(self, entry, worker=None):
        self._epoch += 1
        entry.update(epoch=self._epoch, values=None, updated_at=None, error=None,
                     status="waiting", next_due=0.)
        if worker is not None:
            entry.update(worker=worker, version=worker.config_version)

    def set_enabled(self, identity, desired):
        members, _ = self.source()
        with self.lock:
            enabled = identity in self.entries
            desired = not enabled if desired is None else desired
            if not desired:
                self.entries.pop(identity, None)
                self._revision += 1
                return False
            if enabled:
                return True
            if self._closed:
                raise RuntimeError("policy 监控正在关闭")
            worker = members.get(identity)
            if worker is None:
                raise ValueError(f"policy {identity} 未加载")
            if not worker.supports_monitor:
                raise ValueError(f"policy {identity} 不支持 monitor(context)；请检查回调和 SDK 版本")
            entry = {}
            self._reset(entry, worker)
            self.entries[identity] = entry
            self._revision += 1
            if self.threaded and self.thread is None:
                self.thread = threading.Thread(target=self._run, name="rc-policy-monitor", daemon=True)
                self.thread.start()
            self._wake.set()
            return True

    def invalidate(self):
        # Called synchronously before lifecycle work, even if it completes
        # entirely between two polling ticks.
        with self.lock:
            self._revision += 1
            for entry in self.entries.values():
                self._reset(entry)

    def tick(self):
        with self.lock:
            if self._closed or not self.entries:
                return
            revision = self._revision
        # Never acquire the controller lock while holding the monitor lock.
        members, blocked = self.source()
        now = self.clock()
        with self.lock:
            if self._closed or revision != self._revision:
                return
            for identity, entry in list(self.entries.items()):
                if blocked:
                    entry["status"] = "waiting"
                    continue
                worker = members.get(identity)
                if worker is None:
                    del self.entries[identity]
                    continue
                if worker is not entry["worker"] or worker.config_version != entry["version"]:
                    self._reset(entry, worker)
                if not worker.supports_monitor:
                    entry.update(status="unavailable", error="当前 policy 不支持 monitor(context)")
                    continue
                try:
                    result = worker.poll_monitor()
                    if (result is not None and result.get("monitor_epoch") == entry["epoch"]
                            and result.get("config_version") == entry["version"]):
                        if result["type"] == "monitor_result":
                            entry.update(values=result["result"], updated_at=now, status="ready", error=None)
                        else:
                            entry.update(status="error", error=result.get("error", "监控读取失败"))
                    started = worker.monitor_started_at
                    if started is not None and now - started >= self.SOFT_TIMEOUT:
                        entry.update(status="timeout", error="监控读取超过 1 秒，等待原调用结束")
                    if now + 1e-9 >= entry["next_due"]:
                        entry["next_due"] = now + self.PERIOD
                        worker.submit_monitor(entry["epoch"])
                except Exception as exc:
                    entry.update(status="error", error=str(exc)[:1024])

    def snapshot(self):
        with self.lock:
            return {identity: copy.deepcopy({key: entry[key] for key in
                    ("values", "updated_at", "status", "error")})
                    for identity, entry in self.entries.items()}

    def _run(self):
        while not self._closed:
            self._wake.clear()
            start = self.clock()
            self.tick()
            self._wake.wait(max(0., self.PERIOD - (self.clock() - start)))

    def close(self, *, wait=True):
        with self.lock:
            self._closed = True
            self.entries.clear()
            self._revision += 1
            self._wake.set()
        if wait and self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(1)
