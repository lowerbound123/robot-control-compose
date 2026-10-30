import queue
import threading
import time

import numpy as np
import pytest

from rc_policy_sdk.monitor import MonitorExecutor, normalize_snapshot
from rc_policy_sdk.outbox import PrioritySender


def test_snapshot_contract_and_detached_order():
    original = {"阶段": "移动\n下一步", "count": 3, "loss": 0.125}
    result = normalize_snapshot(original)
    original["count"] = 4
    assert list(result) == ["阶段", "count", "loss"]
    assert result["count"] == 3
    assert normalize_snapshot({}) == {}


@pytest.mark.parametrize("value", [True, np.int64(1), float("nan")])
def test_invalid_values(value):
    with pytest.raises((TypeError, ValueError)):
        normalize_snapshot({"v": value})


@pytest.mark.parametrize("value", [{1: "bad"}, [], {str(n): n for n in range(129)}, {"v": "中" * 22000}])
def test_shape_keys_and_limits(value):
    with pytest.raises((TypeError, ValueError)):
        normalize_snapshot(value)


class Channel:
    def __init__(self):
        self.results = queue.Queue()

    def send_monitor(self, message):
        self.results.put(message)


def request(number=1):
    return dict(type="monitor", monitor_id=number, monitor_epoch=0, config_version=1)


def test_monitor_runs_independently_and_lifecycle_waits():
    started, finish, drained = threading.Event(), threading.Event(), threading.Event()
    context = {"calls": 0}
    def callback(ctx):
        ctx["calls"] += 1
        started.set()
        assert finish.wait(2)
        return {"count": ctx["calls"]}
    channel = Channel()
    executor = MonitorExecutor(channel, context, callback)
    executor.submit(request(), 1)
    assert started.wait(1)
    executor.submit(request(2), 1)
    assert channel.results.get(timeout=1)["type"] == "monitor_error"
    waiter = threading.Thread(target=lambda: (executor.barrier(), drained.set()))
    waiter.start()
    assert not drained.wait(.05)
    finish.set()
    assert drained.wait(1)
    waiter.join()
    result = channel.results.get(timeout=1)
    assert result["result"] == {"count": 1}
    assert "images_released" not in result
    executor.submit(request(3), 1)
    assert channel.results.get(timeout=1)["type"] == "monitor_error"
    executor.resume()
    executor.submit(request(4), 1)
    assert channel.results.get(timeout=1)["result"] == {"count": 2}
    executor.close()


def test_monitor_errors_are_bounded_and_recoverable():
    channel = Channel()
    calls = []
    def callback(context):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("x" * 100000 + chr(0xd800))
        return {"ok": 1}
    executor = MonitorExecutor(channel, None, callback)
    try:
        executor.submit(request(), 1)
        error = channel.results.get(timeout=1)
        assert error["type"] == "monitor_error" and len(error["error"]) < 10000
        executor.submit(request(2), 1)
        assert channel.results.get(timeout=1)["result"] == {"ok": 1}
    finally:
        executor.close()


def test_sender_prioritizes_control_and_closes_without_losing_waiters():
    entered, unblock = threading.Event(), threading.Event()
    packets = []
    class Connection:
        def sendall(self, packet):
            packets.append(packet)
            if len(packets) == 1:
                entered.set()
                assert unblock.wait(2)
    sender = PrioritySender(Connection())
    sender.send_monitor({"type": "first"}, "a")
    assert entered.wait(1)
    sender.send_monitor({"type": "later"}, "a")
    done = threading.Event()
    thread = threading.Thread(target=lambda: (sender.send({"type": "control"}), done.set()))
    thread.start()
    deadline = time.monotonic() + 1
    while sender.outbox.qsize() < 2 and time.monotonic() < deadline:
        time.sleep(.001)
    unblock.set()
    assert done.wait(1)
    thread.join()
    deadline = time.monotonic() + 1
    while len(packets) < 3 and time.monotonic() < deadline:
        time.sleep(.001)
    assert b'control' in packets[1] and b'later' in packets[2]
    sender.close()
    assert not sender.thread.is_alive()


def test_full_control_outbox_waits_without_losing_a_normal_reply():
    entered, unblock = threading.Event(), threading.Event()
    class Connection:
        def sendall(self, packet):
            entered.set()
            assert unblock.wait(2)
    sender = PrioritySender(Connection(), maxsize=1)
    errors, done = [], []
    def send(number):
        try:
            sender.send({"normal":number})
            done.append(number)
        except BaseException as exc: errors.append(exc)
    threads = [threading.Thread(target=send, args=(n,)) for n in range(3)]
    try:
        threads[0].start()
        assert entered.wait(1)
        threads[1].start()
        threads[2].start()
        time.sleep(.03)
        assert not errors and not done
    finally:
        unblock.set()
        for thread in threads:
            if thread.ident is not None: thread.join(1)
        sender.close()
    assert not errors and sorted(done) == [0, 1, 2]
