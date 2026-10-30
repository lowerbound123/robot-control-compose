import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import time

import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool
from host_helpers import LocalPolicyProcess as PolicyProcess


@pytest.fixture
def policy(tmp_path):
    directory = tmp_path / "policy"
    directory.mkdir()
    shutil.copy(Path(__file__).parent / "fixtures/sdk_policy.py", directory / "main.py")
    return directory


def config(tmp_path, **params):
    return {"schema_version": 3, "id": "sdk-test",  "hot_reloadable": ["params.value"], "params": {"value": 1, "release_file": str(tmp_path / "released"), **params}}


def process(policy, settings, **kwargs):
    return PolicyProcess(policy, settings, python_command=[sys.executable], init_timeout_s=2, inference_timeout_s=2, release_timeout_s=.2, **kwargs)


def request(pool, request_id=1):
    lease = pool.acquire()
    return lease, {"request_id": request_id, "generation": 3, "config_version": 1, "state": {"qpos": [0] * 14}, "action_chunk": [], "images": lease.descriptors}


def wait_response(worker):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = worker.poll()
        if result:
            return result
        time.sleep(.01)
    pytest.fail("worker produced no response")


def test_worker_readonly_images_hot_config_identity_and_reset(policy, tmp_path):
    settings = config(tmp_path, value=1)
    worker = process(policy, settings)
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.zeros((3, 4, 3), dtype=np.uint8), 1.0)
    try:
        worker.start()
        lease, call = request(pool)
        worker.submit(call)
        result = wait_response(worker)
        lease.release()
        assert result["type"] == "result"
        assert [result[key] for key in ("request_id", "generation", "config_version")] == [1, 3, 1]
        assert result["actions"][0][:4] == [1, 1, 0, 1]
        worker.update_config({"params.value": 5}, 2)
        assert settings["params"]["value"] == 5
        worker.reset_context()
        lease, call = request(pool, 2)
        call["config_version"] = 2
        worker.submit(call)
        result = wait_response(worker)
        lease.release()
        assert result["actions"][0][:4] == [5, 1, 1, 1]
        with pytest.raises(ValueError):
            worker.update_config({"params.unknown": 2}, 3)
    finally:
        assert not worker.close()
        assert all(not thread.is_alive() for thread in worker.host.threads)
        assert not worker.close()
        pool.close()
    assert (tmp_path / "released").read_text() == "release\n"


@pytest.mark.parametrize("mode", ["invalid", "crash", "retain_image"])
def test_invalid_actions_or_crash_produces_identified_error(policy, tmp_path, mode):
    worker = process(policy, config(tmp_path, **{mode: True}))
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.zeros((2, 2, 3), dtype=np.uint8), 1.0)
    lease = None
    try:
        worker.start()
        lease, call = request(pool)
        worker.submit(call)
        result = wait_response(worker)
        assert result["type"] == "error"
        assert result["request_id"] == 1
        assert result["generation"] == 3
    finally:
        worker.close()
        if lease:
            lease.release()
        pool.close()


def test_close_during_initialize_and_cancel_cannot_orphan_worker(policy, tmp_path):
    worker = process(policy, config(tmp_path, init_delay=60, child_file=str(tmp_path / "child"), detached_child=True))
    errors = []
    cancel = threading.Event()
    thread = threading.Thread(target=lambda: _start_capture(worker, cancel, errors))
    thread.start()
    deadline = time.monotonic() + 2
    while not (tmp_path / "child").exists() and time.monotonic() < deadline:
        time.sleep(.01)
    child_pid = int((tmp_path / "child").read_text())
    cancel.set()
    closers = [threading.Thread(target=worker.close) for _ in range(2)]
    for closer in closers:
        closer.start()
    for closer in closers:
        closer.join(7)
        assert not closer.is_alive()
    thread.join(7)
    assert not thread.is_alive()
    assert errors
    assert not worker.alive and worker.cleanup_confirmed
    assert not (tmp_path / "released").exists(), "release must not race blocked initialize"
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def _start_capture(worker, cancel, errors):
    try:
        worker.start(cancel)
    except Exception as exc:
        errors.append(exc)


def test_inference_timeout_emitted_once(policy, tmp_path):
    worker = PolicyProcess(policy, config(tmp_path, delay=3), python_command=[sys.executable], init_timeout_s=2, inference_timeout_s=.05, release_timeout_s=.2)
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.zeros((2, 2, 3), dtype=np.uint8), 1.0)
    try:
        worker.start()
        lease, call = request(pool)
        worker.submit(call)
        result = wait_response(worker)
        assert result["type"] == "error" and "timeout" in result["error"].lower()
        assert worker.poll() is None
        worker.close()
        lease.release()
    finally:
        worker.close()
        pool.close()




def test_graceful_close_reaps_detached_child(policy, tmp_path):
    worker = process(policy, config(tmp_path, child_file=str(tmp_path / "child"), detached_child=True))
    worker.start()
    child_pid = int((tmp_path / "child").read_text())
    assert not worker.close()
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_hanging_release_has_bounded_kill_fallback_and_runs_once(policy, tmp_path):
    worker = process(policy, config(tmp_path, release_delay=60))
    worker.start()
    old_pid = worker.pid
    started = time.monotonic()
    assert worker.close()
    assert time.monotonic() - started < 7
    assert worker.cleanup_confirmed
    assert worker.close()
    assert not worker.alive
    assert (tmp_path / "released").read_text() == "release\n"
    # A forced cleanup still proves runtime exit, so a manual reload gets a new runtime.
    replacement = process(policy, config(tmp_path))
    try:
        replacement.start()
        assert replacement.pid != old_pid
    finally:
        assert not replacement.close()


def test_cancel_before_launch_creates_no_process(policy, tmp_path):
    worker = process(policy, config(tmp_path))
    cancel = threading.Event(); cancel.set()
    with pytest.raises(RuntimeError, match="cancel"):
        worker.start(cancel)
    assert worker.pid is None
    assert not worker.close()




def test_multi_leaf_patch_failure_is_atomic(policy, tmp_path):
    worker = process(policy, config(tmp_path, value=1))
    try:
        worker.start()
        with pytest.raises(ValueError):
            worker.update_config({"params.value": 9, "params.unknown": 2}, 2)
        assert worker.config_version == 1
        assert worker.config["params"]["value"] == 1
        assert worker.alive
    finally:
        worker.close()


@pytest.mark.parametrize("detached", [False, True])
def test_abnormal_worker_exit_still_closes_established_child(policy, tmp_path, detached):
    worker = process(policy, config(tmp_path, crash=True, child_file=str(tmp_path / "child"), detached_child=detached))
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.zeros((2, 2, 3), dtype=np.uint8), 1.0)
    lease = None
    try:
        worker.start()
        child_pid = int((tmp_path / "child").read_text())
        lease, call = request(pool)
        worker.submit(call)
        assert wait_response(worker)["type"] == "error"
        worker.close()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        worker.close()
        if lease:
            lease.release()
        pool.close()


def test_failed_initialize_calls_release_once(policy, tmp_path):
    worker = process(policy, config(tmp_path, fail_init=True))
    with pytest.raises(RuntimeError, match="initialize failure"):
        worker.start()
    assert not worker.close()
    assert not worker.alive
    assert (tmp_path / "released").read_text() == "release\n"


def test_submit_does_not_wait_for_stopped_worker_socket(policy, tmp_path):
    worker = process(policy, config(tmp_path))
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.zeros((2, 2, 3), dtype=np.uint8), 1)
    lease = None
    thread = None
    errors = []
    try:
        worker.start()
        lease, call = request(pool)
        call["state"]["padding"] = "x" * (3 * 1024 * 1024)
        os.kill(worker.pid, signal.SIGSTOP)
        thread = threading.Thread(target=lambda: _submit_capture(worker, call, errors))
        thread.start(); thread.join(.5)
        assert not thread.is_alive(), "submit blocked on worker I/O"
        assert not errors
    finally:
        if worker.alive:
            os.kill(worker.pid, signal.SIGCONT)
        if thread:
            thread.join(2)
        worker.close()
        if lease:
            lease.release()
        pool.close()


def _submit_capture(worker, call, errors):
    try:
        worker.submit(call)
    except Exception as exc:
        errors.append(exc)


def test_explicit_hot_array_leaf_preserves_config_identity(policy, tmp_path):
    settings = config(tmp_path, values=[1, 2])
    settings["hot_reloadable"].append("params.values")
    worker = process(policy, settings)
    try:
        worker.start()
        worker.update_config({"params.values": [3, 4]}, 2)
        assert worker.config is settings
        assert settings["params"]["values"] == [3, 4]
        assert worker.config_version == 2
    finally:
        worker.close()






def test_recycled_descendant_pid_is_never_signaled(monkeypatch):
    import rc_policy_sdk.lifecycle as lifecycle
    signals = []
    closed = []
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.setattr(lifecycle.os, "pidfd_open", lambda pid, flags: 55, raising=False)
    monkeypatch.setattr(lifecycle, "process_birth", lambda pid: "new-birth")
    monkeypatch.setattr(lifecycle.signal, "pidfd_send_signal", lambda *args: signals.append(args), raising=False)
    original_close = os.close
    monkeypatch.setattr(lifecycle.os, "close", lambda fd: closed.append(fd) if fd == 55 else original_close(fd))
    lifecycle.signal_processes({1234: "original-birth"}, signal.SIGTERM)
    assert not signals
    assert closed == [55]


def test_linux_snapshot_reads_ancestry_and_birth_together(monkeypatch):
    import rc_policy_sdk.lifecycle as lifecycle
    class Entry:
        name = "1001"
        reads = 0
        def __truediv__(self, _name):
            return self
        def read_text(self):
            self.reads += 1
            # A second read simulates the PID being reused outside this tree.
            fields = ["S", "42" if self.reads == 1 else "99"] + ["0"] * 17 + ["77" if self.reads == 1 else "88"]
            return "1001 (child with spaces) " + " ".join(fields)
    entry = Entry()
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: iter([entry]) if path == Path("/proc") else original_iterdir(path))
    assert lifecycle.descendant_snapshot(42) == {1001: 77}
    assert entry.reads == 1


def test_linux_signal_uses_opened_pidfd_after_birth_validation(monkeypatch):
    import rc_policy_sdk.lifecycle as lifecycle
    calls = []
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.setattr(lifecycle.os, "pidfd_open", lambda pid, flags: calls.append(("open", pid)) or 55, raising=False)
    monkeypatch.setattr(lifecycle, "process_birth", lambda pid: calls.append(("birth", pid)) or "original-birth")
    monkeypatch.setattr(lifecycle.signal, "pidfd_send_signal", lambda *args: calls.append(("signal", args)), raising=False)
    original_close = os.close
    monkeypatch.setattr(lifecycle.os, "close", lambda fd: calls.append(("close", fd)) if fd == 55 else original_close(fd))
    lifecycle.signal_processes({1234: "original-birth"}, signal.SIGTERM)
    assert calls == [("open", 1234), ("birth", 1234), ("signal", (55, signal.SIGTERM, None, 0)), ("close", 55)]


def test_linux_missing_python_pidfd_wrappers_use_native_syscalls(monkeypatch):
    import rc_policy_sdk.lifecycle as lifecycle
    from types import SimpleNamespace
    calls = []
    class Syscall:
        def __call__(self, number, *args):
            calls.append((number.value, tuple(arg.value for arg in args)))
            return 55 if number.value == 434 else 0
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.setattr(lifecycle.platform, "machine", lambda: "aarch64")
    monkeypatch.delattr(lifecycle.os, "pidfd_open", raising=False)
    monkeypatch.delattr(lifecycle.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(lifecycle.ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace(syscall=Syscall()))
    assert lifecycle._open_pidfd(1234) == 55
    lifecycle._send_pidfd_signal(55, signal.SIGTERM)
    assert [call[0] for call in calls] == [434, 424]
    _, open_args = calls[0]
    _, signal_args = calls[1]
    assert open_args[0] == 1234
    assert signal_args[0] == 55 and signal_args[1] == signal.SIGTERM


def test_native_pidfd_error_preserves_process_lookup_semantics(monkeypatch):
    import ctypes
    import errno
    import rc_policy_sdk.lifecycle as lifecycle
    from types import SimpleNamespace
    class Syscall:
        def __call__(self, *_args):
            ctypes.set_errno(errno.ESRCH)
            return -1
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.setattr(lifecycle.platform, "machine", lambda: "aarch64")
    monkeypatch.delattr(lifecycle.os, "pidfd_open", raising=False)
    monkeypatch.setattr(lifecycle.ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace(syscall=Syscall()))
    with pytest.raises(ProcessLookupError):
        lifecycle._open_pidfd(1234)






@pytest.mark.parametrize("alive", [False, True])
def test_macos_denied_birth_metadata_requires_verified_exit(monkeypatch, alive):
    import ctypes
    import errno
    from types import SimpleNamespace
    import rc_policy_sdk.lifecycle as lifecycle
    class DeniedInfo:
        def __call__(self, *_args):
            ctypes.set_errno(errno.EPERM)
            return 0
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    monkeypatch.setattr(lifecycle.ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace(proc_pidinfo=DeniedInfo()))
    def probe(_pid, _sig):
        if not alive:
            raise ProcessLookupError()
    monkeypatch.setattr(lifecycle.os, "kill", probe)
    if alive:
        with pytest.raises(OSError) as error:
            lifecycle.process_birth(1234)
        assert error.value.errno == errno.EPERM
    else:
        assert lifecycle.process_birth(1234) is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux subreaper contract")
@pytest.mark.parametrize("phase", ["initialize", "infer"])
def test_linux_subreaper_cleans_immediate_detach_before_tracker(policy, tmp_path, phase, monkeypatch):
    worker = PolicyProcess(policy, config(tmp_path, rapid_detach=phase, child_file=str(tmp_path / "child")), python_command=[sys.executable], init_timeout_s=5, inference_timeout_s=2, release_timeout_s=2)
    # The fixture detaches a child then immediately os._exit()s. RC no longer
    # has a local tracker: the host subreaper must adopt and reap that child.
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.zeros((2, 2, 3), dtype=np.uint8), 1)
    lease = None
    try:
        if phase == "initialize":
            with pytest.raises(RuntimeError):
                worker.start()
        else:
            worker.start()
            lease, call = request(pool)
            worker.submit(call)
            result = wait_response(worker)
            assert result["type"] == "error"
            assert worker.close()  # A crashed runtime is a forced group exit.
        child_pid = int((tmp_path / "child").read_text())
        assert worker.close()
        assert worker.cleanup_confirmed
        assert not worker.alive
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        worker.close()
        if lease:
            lease.release()
        pool.close()
