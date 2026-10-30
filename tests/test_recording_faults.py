"""Adversarial recorder tests using real pipes, writers and OS failures."""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import signal
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from robot_control.recording import RecordingService


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("recording fault test condition timed out")
        time.sleep(.005)


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def sample(number=0):
    return dict(sample_id=number, timestamp_ns=number + 1, monotonic_ns=number + 1,
                scheduled_ns=number + 1, state={"padding": "x" * 512}, state_error=None,
                frames={}, controls={}, missed=False)


class AckGate:
    """Delay a real writer ACK after the sample has been handled."""
    def __init__(self, connection, directory):
        self.connection, self.root = connection, Path(directory)
        self.blocked = False

    def send(self, message):
        if (not self.blocked and message["kind"] == "ack"
                and (message.get("stats") or {}).get("samples_written", 0)):
            self.blocked = True
            (self.root / "sample-ack-blocked").touch()
            while not (self.root / "release-sample-ack").exists():
                time.sleep(.005)
        self.connection.send(message)

    def close(self):
        self.connection.close()


def ack_gated_writer(receiver, responder, directory, run_id):
    from robot_control.recording_writer import writer_main
    writer_main(receiver, AckGate(responder, directory), directory, run_id)


def file_size_limited_writer(receiver, responder, directory, run_id):
    """The kernel returns EFBIG on actual writes in this child only."""
    import resource
    from robot_control.recording_writer import writer_main
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
    writer_main(receiver, responder, directory, run_id)


@contextmanager
def stopped(process):
    os.kill(process.pid, signal.SIGSTOP)
    _, status = os.waitpid(process.pid, os.WUNTRACED)
    assert os.WIFSTOPPED(status)
    try:
        yield
    finally:
        if process.is_alive():
            os.kill(process.pid, signal.SIGCONT)


@pytest.fixture
def recorder(tmp_path):
    """Hold the external snapshot source so admission tests are deterministic."""
    created = []

    def create(**options):
        release, entered = threading.Event(), threading.Event()
        root = tmp_path / str(len(created))

        def snapshot():
            entered.set()
            release.wait(15)
            return dict(state={}, state_error=None, frames={})

        service = RecordingService(root, "faults", snapshot, **options)
        service.start()
        result = SimpleNamespace(service=service, root=root, process=service._process,
                                 release=release, entered=entered)
        created.append(result)
        return result

    yield create
    for item in created:
        item.release.set()
        if item.process.is_alive():
            os.kill(item.process.pid, signal.SIGCONT)
        item.service.shutdown(time.monotonic() + 2)
        if item.process.is_alive():
            item.process.kill()
            item.process.join(1)
        for connection in (item.service._sender, item.service._receiver):
            if connection and not connection.closed:
                connection.close()
        for thread in (item.service._sampler, item.service._feeder, item.service._manager):
            thread.join(.5)


@pytest.mark.parametrize("limits", [
    dict(max_samples=1, sample_budget=1024 * 1024),
    dict(max_samples=100, sample_budget=10000),
])
def test_mandatory_count_and_byte_credits_remain_held_until_writer_ack(monkeypatch, recorder, limits):
    import robot_control.recording as recording
    monkeypatch.setattr(recording, "writer_main", ack_gated_writer)
    item = recorder(stop_timeout=3, **limits)
    service = item.service
    status = service.command("on").result(3)
    assert item.entered.wait(1)
    assert service._enqueue("sample", sample(0))
    wait_for(lambda: (item.root / "sample-ack-blocked").exists())
    held = service.status()
    assert held["pending_samples"] == 1
    assert 0 < held["pending_bytes"] <= limits["sample_budget"]
    assert not service._enqueue("sample", sample(1))
    assert service.status()["state"] == "error"
    assert "budget" in service.status()["error"]
    # The first sample was processed, but its credit cannot disappear early.
    assert service.status()["pending_samples"] == 1
    (item.root / "release-sample-ack").touch()
    wait_for(lambda: service.status()["pending_samples"] == 0)
    wait_for(lambda: service._stop_deadline is None)
    item.release.set()
    assert not service.shutdown(time.monotonic() + 2)
    commits = read_jsonl(Path(status["path"]) / "samples.jsonl")
    assert [entry["sample_id"] for entry in commits] == [0]
    assert not item.process.is_alive()


def test_sigstopped_writer_keeps_baseline_log_producers_async_and_bounded(recorder):
    item = recorder(extra_budget=32768)
    service = item.service
    accepted = []
    with stopped(item.process):
        start = time.monotonic()
        for number in range(2000):
            if service.log({"message": f"line-{number}:" + "x" * 1024}):
                accepted.append(number)
        elapsed = time.monotonic() - start
        assert elapsed < .5
        status = service.status()
        assert 0 < len(accepted) < 10
        assert 0 < status["pending_bytes"] <= 32768
        assert status["extra_dropped"] == 2000 - len(accepted)
        assert status["state"] == "idle"
    wait_for(lambda: service.status()["pending_bytes"] == 0)
    item.release.set()
    assert not service.shutdown(time.monotonic() + 2)
    messages = read_jsonl(item.root / "rc_faults.jsonl")
    assert [int(entry["message"].split(":")[0][5:]) for entry in messages
            if entry['message'].startswith('line-')] == accepted
    gaps = [entry for entry in messages if entry.get('event') == 'diagnostic_gap']
    assert sum(entry['count'] for entry in gaps) == 2000 - len(accepted)
    assert all(entry['record_session_id'] is None for entry in gaps)
    assert not item.process.is_alive()


def test_record_off_deadline_kills_sigstopped_writer_without_process_leak(recorder):
    item = recorder(stop_timeout=.15, extra_budget=1024 * 1024)
    service = item.service
    service.command("on").result(3)
    assert item.entered.wait(1)
    with stopped(item.process):
        assert service.event("policy", {"event": "queued", "padding": "x" * 100000})
        started = time.monotonic()
        future = service.command("off")
        with pytest.raises(RuntimeError, match="deadline"):
            future.result(3)
        assert time.monotonic() - started < 2
        assert not item.process.is_alive()
        assert item.process.exitcode != 0
        assert service.status()["state"] == "error"
        assert service.status()["pending_bytes"] == 0
    item.release.set()
    assert service.shutdown(time.monotonic() + .5)
    assert not item.process.is_alive()


def test_optional_overflow_gap_does_not_lose_counts_while_its_lane_is_full(recorder):
    item = recorder(extra_budget=8192, stop_timeout=3)
    service = item.service
    status = service.command("on").result(3)
    assert item.entered.wait(1)
    with stopped(item.process):
        assert service.event("policy", {"event": "retained", "padding": "x" * 800}, priority=True)
        for _ in range(37):
            assert not service.event("policy", {"event": "too-large", "padding": "x" * 2000})
        # Each completed no-op proves a manager turn ran while no ACK could
        # return the credit needed to store the aggregated gap itself.
        service.command("on").result(1)
        service.command("on").result(1)
    journal = Path(status["path"]) / "session.jsonl"
    wait_for(lambda: "diagnostic_gap" in journal.read_text())
    stopped_status = service.command("off").result(3)
    gaps = [entry for entry in read_jsonl(journal) if entry["event"] == "diagnostic_gap"]
    assert (stopped_status["extra_dropped"], sum(entry["count"] for entry in gaps)) == (37, 37)
    item.release.set()
    assert not service.shutdown(time.monotonic() + 2)
    assert not item.process.is_alive()


@pytest.mark.parametrize("action,transition", [("on", "starting"), ("off", "stopping")])
def test_repeated_on_and_off_share_the_inflight_transition(recorder, action, transition):
    item = recorder(stop_timeout=3)
    service = item.service
    if action == "off":
        service.command("on").result(3)
        assert item.entered.wait(1)
    with stopped(item.process):
        first = service.command(action)
        wait_for(lambda: service.status()["state"] == transition)
        second = service.command(action)
        # Both commands must reach the manager before the child is resumed.
        wait_for(lambda: not service._commands)
    first_status = first.result(3)
    second_status = second.result(3)
    assert first_status["session_id"] == second_status["session_id"]
    assert first_status["state"] == second_status["state"]
    again = service.command(action).result(3)
    assert again["session_id"] == first_status["session_id"]
    assert len(list(item.root.glob("record_*"))) == 1


def test_real_write_failure_preserves_committed_samples_and_baseline_logging(monkeypatch, recorder):
    import robot_control.recording as recording
    monkeypatch.setattr(recording, "writer_main", file_size_limited_writer)
    item = recorder(stop_timeout=2)
    service = item.service
    assert service.log({"message": "before-write-failure"})
    status = service.command("on").result(3)
    assert item.entered.wait(1)
    payload = sample()
    payload["frames"] = {"head": dict(image=np.array([[[255, 0, 0]]], dtype=np.uint8),
                                     valid=True, sequence=1, timestamp=1.)}
    assert service._enqueue("sample", payload)
    folder = Path(status["path"])
    commits_path = folder / "samples.jsonl"
    wait_for(lambda: commits_path.stat().st_size > 0)
    committed = commits_path.read_bytes()
    commits = read_jsonl(commits_path)
    image_path = folder / commits[0]["images"]["head"]["file"]
    image_bytes = image_path.read_bytes()
    assert service.event("policy", {"event": "write-over-limit", "padding": "x" * 262144})
    wait_for(lambda: service.status()["state"] == "error")
    assert "File too large" in service.status()["error"] or str(errno.EFBIG) in service.status()["error"]
    assert item.process.is_alive()
    assert service.log({"message": "after-write-failure"})
    item.release.set()
    assert not service.shutdown(time.monotonic() + 3)
    assert commits_path.read_bytes() == committed
    assert image_path.read_bytes() == image_bytes
    assert folder.is_dir()
    messages = read_jsonl(item.root / "rc_faults.jsonl")
    assert [entry["message"] for entry in messages] == ["before-write-failure", "after-write-failure"]
    assert not item.process.is_alive()


def test_exact_session_boundaries_exclude_idle_logs_and_old_request_tokens(recorder):
    item = recorder(stop_timeout=3)
    service = item.service
    assert service.log({"message": "before"})
    first = service.command("on").result(3)
    assert item.entered.wait(1)
    start_event = dict(event="policy.start", instance_id="same-source-0", request_id=1,
                       generation=2, config_version=3, observation_id="observation")
    assert service.policy_event(start_event)
    assert service.log({"message": "first-only"})
    with stopped(item.process):
        stopping = service.command("off")
        wait_for(lambda: service.status()["state"] == "stopping")
        assert not service.policy_event({**start_event, "event": "outside"})
        assert service.log({"message": "while-stopping"})
    stopping.result(3)
    assert service.log({"message": "between"})
    second = service.command("on").result(3)
    assert second["session_id"] != first["session_id"]
    assert service.policy_event({**start_event, "event": "policy.result", "actions": [[0] * 14]})
    assert service.log({"message": "second-only"})
    service.command("off").result(3)
    assert service.log({"message": "after"})
    item.release.set()
    assert not service.shutdown(time.monotonic() + 3)
    first_path, second_path = Path(first["path"]), Path(second["path"])
    first_events = read_jsonl(first_path / "events.jsonl")
    second_events = read_jsonl(second_path / "events.jsonl")
    assert [entry["message"] for entry in first_events] == ["first-only"]
    assert [entry["message"] for entry in second_events] == ["second-only"]
    assert {entry["record_session_id"] for entry in first_events} == {first["session_id"]}
    assert {entry["record_session_id"] for entry in second_events} == {second["session_id"]}
    first_policy = read_jsonl(first_path / "policy.jsonl")
    second_policy = read_jsonl(second_path / "policy.jsonl")
    assert [entry["event"] for entry in first_policy] == ["policy.start"]
    assert [entry["event"] for entry in second_policy] == ["policy.result"]
    assert second_policy[0]["start_outside_recording"] is True
    base = read_jsonl(item.root / "rc_faults.jsonl")
    assert [entry["message"] for entry in base] == [
        "before", "first-only", "while-stopping", "between", "second-only", "after",
    ]
    assert service.status()["pending_bytes"] == 0
    assert not item.process.is_alive()


@pytest.mark.parametrize("action,transition", [("on", "starting"), ("off", "stopping")])
def test_writer_death_settles_inflight_commands_and_shutdown_closes_transport(recorder, action, transition):
    item = recorder(stop_timeout=3)
    service = item.service
    if action == "off":
        service.command("on").result(3)
        assert item.entered.wait(1)
    with stopped(item.process):
        first = service.command(action)
        wait_for(lambda: service.status()["state"] == transition)
        second = service.command(action)
        wait_for(lambda: not service._commands)
        item.process.kill()
        item.process.join(1)
        for future in (first, second):
            with pytest.raises(RuntimeError):
                future.result(1)
    assert service.status()["state"] == "error"
    for after_death in ("on", "off"):
        future = service.command(after_death)
        wait_for(future.done, timeout=.3)
        try:
            result = future.result()
        except RuntimeError:
            pass
        else:
            assert result["state"] == "error"
    item.release.set()
    started = time.monotonic()
    service.shutdown(started + .5)
    assert time.monotonic() - started < .7
    wait_for(lambda: not any(thread.is_alive() for thread in
                            (service._manager, service._feeder, service._sampler)), timeout=.3)
    assert service._sender.closed and service._receiver.closed
    assert not item.process.is_alive()


def test_shutdown_uses_one_absolute_deadline_and_reaps_all_writer_threads(recorder):
    item = recorder(stop_timeout=30, extra_budget=16 * 1024 * 1024)
    service = item.service
    with stopped(item.process):
        for number in range(3):
            assert service.log({"message": str(number) + "x" * 500000})
        item.release.set()
        started = time.monotonic()
        deadline = started + .25
        assert service.shutdown(deadline)
        elapsed = time.monotonic() - started
        assert elapsed < .4
        # Returning from cleanup is also the exit proof; no extra deadline may
        # be started to finish a blocked feeder, manager, or the writer itself.
        remaining = {"writer": item.process.is_alive(), **{
            thread.name: thread.is_alive() for thread in
            (service._manager, service._feeder, service._sampler)}}
        assert not any(remaining.values()), remaining
        assert service._sender.closed and service._receiver.closed
        again = time.monotonic()
        assert service.shutdown(deadline)
        assert time.monotonic() - again < .1


def test_repeated_record_commands_cannot_grow_transition_waiters_without_bound(recorder):
    item = recorder(stop_timeout=5)
    service = item.service
    with stopped(item.process):
        first = service.command("on")
        wait_for(lambda: service.status()["state"] == "starting")
        pending = [first]
        for index in range(255):
            pending.append(service.command("on"))
            # Drain each command into its transition waiter, so this cannot
            # pass merely because the not-yet-processed command queue is full.
            wait_for(lambda: len(service._transition_waiters["starting"]) == index + 1)
        rejected = service.command("on")
        assert rejected.done()
        with pytest.raises(RuntimeError, match="full"):
            rejected.result()
    statuses = [future.result(3) for future in pending]
    assert len({status["session_id"] for status in statuses}) == 1
    assert {status["state"] for status in statuses} == {"recording"}
    service.command("off").result(3)
    item.release.set()
    assert not service.shutdown(time.monotonic() + 2)
    assert not item.process.is_alive()


def test_optional_gap_keeps_exact_loss_count_when_message_count_limit_is_full(recorder):
    item = recorder(stop_timeout=5)
    service = item.service
    status = service.command("on").result(3)
    assert item.entered.wait(1)
    wait_for(lambda: service.status()["pending_bytes"] == 0)
    with stopped(item.process):
        for number in range(4096):
            assert service.event("policy", {"event": "tiny", "number": number}, priority=True)
        for number in range(37):
            assert not service.event("policy", {"event": "lost", "number": number}, priority=True)
        service.command("on").result(1)
        service.command("on").result(1)
        assert service.status()["pending_bytes"] < 16 * 1024 * 1024
    journal = Path(status["path"]) / "session.jsonl"
    wait_for(lambda: "diagnostic_gap" in journal.read_text())
    status = service.command("off").result(3)
    gaps = [entry for entry in read_jsonl(journal) if entry["event"] == "diagnostic_gap"]
    assert (status["extra_dropped"], sum(entry["count"] for entry in gaps)) == (37, 37)
    item.release.set()
    assert not service.shutdown(time.monotonic() + 2)
    assert not item.process.is_alive()


def test_shutdown_settles_the_first_off_future_while_its_data_is_still_queued(recorder):
    item = recorder(stop_timeout=5)
    service = item.service
    status = service.command("on").result(3)
    assert item.entered.wait(1)
    results, errors = [], []

    def shutdown():
        try:
            results.append(service.shutdown(time.monotonic() + 3))
        except BaseException as exc:
            errors.append(exc)

    with stopped(item.process):
        assert service.event("policy", {"event": "first", "padding": "x" * 500000})
        wait_for(lambda: not service._lanes["extra"])
        assert service.event("policy", {"event": "second"})
        off = service.command("off")
        wait_for(lambda: service.status()["state"] == "stopping")
        item.release.set()
        shutdown_thread = threading.Thread(target=shutdown)
        shutdown_thread.start()
        wait_for(lambda: service._shutdown_future is not None)
    shutdown_thread.join(4)
    assert not shutdown_thread.is_alive()
    assert not errors
    assert results == [False]
    assert off.done(), "shutdown overwrote the original off receipt"
    try:
        stopped_status = off.result()
    except RuntimeError:
        # Explicitly cancelling a superseded off request also settles it.
        pass
    else:
        assert stopped_status["state"] == "idle"
    policy = read_jsonl(Path(status["path"]) / "policy.jsonl")
    assert [entry["event"] for entry in policy] == ["first", "second"]
    assert not item.process.is_alive()
