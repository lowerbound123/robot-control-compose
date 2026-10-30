import json
import queue
import socket

import pytest

from rc_policy_sdk.commands import command_specs
from rc_policy_sdk.host import Peer, PolicyHost
from rc_policy_sdk.outbox import PriorityOutbox
from rc_policy_sdk.wire import receive_message, send_message


def gap(count, saturated):
    return ("gap", count, saturated)


def test_record_is_reserved_for_controller_commands():
    with pytest.raises(ValueError, match="reserved"):
        command_specs({"tui_commands": {"record": {"handler": "record", "args": []}}})


def test_logs_preserve_control_capacity_monitor_retention_and_fifo():
    box = PriorityOutbox(2, log_maxsize=2, log_maxbytes=8)
    box.put_nowait("control")
    box.put_nowait("release")
    assert box.put_log("first", size=4, gap_factory=gap)
    assert box.put_log("second", size=4, gap_factory=gap)
    assert not box.put_log("dropped", size=4, gap_factory=gap)
    assert box.put_monitor("session", "monitor")
    assert not box.put_monitor("session", "must-not-replace")
    with pytest.raises(queue.Full):
        box.put_nowait("overflow")
    assert [box.get_nowait() for _ in range(6)] == [
        "control", "release", "monitor", "first", "second", ("gap", 1, False),
    ]
    with pytest.raises(queue.Empty):
        box.get(timeout=.01)


@pytest.mark.parametrize("count_limit,byte_limit,sizes", [
    (2, 100, [1, 1, 1]),
    (10, 6, [3, 3, 1]),
    (10, 6, [7]),
])
def test_log_lane_limits_both_message_count_and_encoded_bytes(count_limit, byte_limit, sizes):
    box = PriorityOutbox(1, log_maxsize=count_limit, log_maxbytes=byte_limit)
    accepted = []
    for index, size in enumerate(sizes):
        if box.put_log(index, size=size, gap_factory=gap):
            accepted.append(index)
    assert accepted == list(range(len(sizes) - 1))
    box.put_nowait("control-after-overflow")
    assert box.get_nowait() == "control-after-overflow"
    assert [box.get_nowait() for _ in accepted] == accepted
    assert box.get_nowait() == ("gap", 1, False)
    with pytest.raises(queue.Empty):
        box.get_nowait()


@pytest.mark.parametrize("limits", [
    {"log_maxsize": 0}, {"log_maxsize": -1}, {"log_maxsize": True},
    {"log_maxbytes": 0}, {"log_maxbytes": -1}, {"log_maxbytes": 1.5},
])
def test_log_limits_cannot_disable_bounded_admission(limits):
    with pytest.raises(ValueError, match="positive integers"):
        PriorityOutbox(1, **limits)


@pytest.mark.parametrize("size,factory", [(-1, gap), (True, gap), (1, None)])
def test_invalid_log_accounting_is_rejected_without_queuing(size, factory):
    box = PriorityOutbox(1)
    with pytest.raises(ValueError):
        box.put_log("invalid", size=size, gap_factory=factory)
    assert box.qsize() == 0


def test_one_gap_aggregates_a_flood_without_retaining_each_dropped_identity():
    box = PriorityOutbox(1, log_maxsize=2, log_maxbytes=4)
    assert box.put_log("before-a", size=2, gap_factory=gap)
    assert box.put_log("before-b", size=2, gap_factory=gap)
    for index in range(10000):
        assert not box.put_log(index, size=2, gap_factory=gap)
    assert box.qsize() == 3
    assert box.get_nowait() == "before-a"
    # A later line cannot jump ahead of the gap even after capacity is freed.
    assert not box.put_log("later", size=1, gap_factory=gap)
    assert box.get_nowait() == "before-b"
    assert box.get_nowait() == ("gap", 10001, False)
    assert box.put_log("after-gap", size=4, gap_factory=gap)
    assert box.get_nowait() == "after-gap"
    with pytest.raises(queue.Empty):
        box.get_nowait()


def test_log_gap_counter_saturates_without_claiming_an_exact_count(monkeypatch):
    from rc_policy_sdk import outbox
    monkeypatch.setattr(outbox, "MAX_LOG_GAP_COUNT", 3)
    box = PriorityOutbox(1, log_maxsize=1, log_maxbytes=1)
    for _ in range(10):
        assert not box.put_log("oversized", size=2, gap_factory=gap)
    assert box.qsize() == 1
    assert box.get_nowait() == ("gap", 3, True)
    assert not box.put_log("oversized", size=2, gap_factory=gap)
    assert box.get_nowait() == ("gap", 1, False)


@pytest.mark.parametrize("drops,count_limit,event", [
    (2, 100, {"event": "sdk.log_gap", "count": 2}),
    (10, 3, {"event": "sdk.log_gap", "count": 3, "count_saturated": True}),
])
def test_host_log_and_gap_use_authoritative_identity(monkeypatch, drops, count_limit, event):
    from rc_policy_sdk import outbox
    monkeypatch.setattr(outbox, "MAX_LOG_GAP_COUNT", count_limit)
    host = PolicyHost.__new__(PolicyHost)
    host.instance_id = "host-instance"
    host.host_id = "demo-1"
    host.source_policy_id = "demo"
    host.deployment_id = "deployment"
    host.run_id = "run"
    host.generation = 7
    peer = Peer.__new__(Peer)
    peer.outbox = PriorityOutbox(1, log_maxsize=1, log_maxbytes=8192)
    expected = dict(host_instance_id="host-instance", host_id="demo-1",
                    policy_instance_id="demo-1", source_policy_id="demo",
                    deployment_id="deployment", run_id="run", generation=7,
                    runtime_id="runtime")
    spoofed = {key: "forged" for key in expected if key != "runtime_id"}
    for line in ["first", *(f"lost-{index}" for index in range(drops))]:
        host._emit(peer, dict(type="log", message=line, runtime_id="runtime", **spoofed))
    original = json.loads(peer.outbox.get_nowait()[4:])
    loss = json.loads(peer.outbox.get_nowait()[4:])
    assert original["message"] == "first"
    assert json.loads(loss["message"]) == event
    for packet in (original, loss):
        assert packet["type"] == "log"
        assert {key: packet[key] for key in expected} == expected


def test_real_socket_log_flood_keeps_control_and_monitor_connected():
    upstream, downstream = socket.socketpair()
    upstream.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    downstream.settimeout(3)
    peer = Peer(upstream)
    count = 3000
    try:
        for number in range(count):
            peer.send(dict(type="log", host_instance_id="host", runtime_id="runtime",
                           message=f"{number}:" + "x" * 4096))
        controls = ["rpc_result", "ready", "closed"]
        for kind in controls:
            peer.send(dict(type=kind))
        monitor = dict(type="policy_event", session_id="session",
                       event=dict(type="monitor_result", result={"ok": 1}))
        assert peer.send(monitor)
        assert not peer.send(monitor)
        messages = []
        while True:
            message = receive_message(downstream)
            messages.append(message)
            if message["type"] == "log" and message["message"].startswith("{"):
                loss = json.loads(message["message"])
                if loss.get("event") == "sdk.log_gap":
                    break
        types = [message["type"] for message in messages]
        first_control = types.index("rpc_result")
        assert types[first_control:first_control + 4] == [*controls, "policy_event"]
        assert messages[first_control + 3]["event"]["result"] == {"ok": 1}
        retained = sum(message["type"] == "log" for message in messages) - 1
        assert loss["count"] == count - retained > 0
        peer.send(dict(type="still-connected"))
        assert receive_message(downstream) == {"type": "still-connected"}
    finally:
        peer.close()
        upstream.close()
        downstream.close()


def test_real_host_stdout_flood_keeps_ready_monitor_commands_and_stop(tmp_path):
    from test_host_transport import Host, wait_for

    folder = tmp_path / "flood"
    folder.mkdir()
    marker = tmp_path / "finished"
    (folder / "main.py").write_text('''
from pathlib import Path
def initialize(config): return config['params']
def policy(context, state, action_chunk, images, config): return [[0] * 14]
def reset_context(context): pass
def release(context): pass
def monitor(context): return {'alive': 1}
def flood(context):
    for number in range(5000):
        print(str(number) + ':' + 'x' * 1024, flush=True)
    Path(context['marker']).write_text('finished')
    return 'finished'
''')
    host = Host(tmp_path, "flood-0", source_policy_id="flood")
    connection = socket.socket(socket.AF_UNIX)
    connection.settimeout(5)
    try:
        connection.connect(host.socket)
        send_message(connection, dict(type="hello", rpc_id=1, owner_id="log-test",
                                     protocol_version=1, image_protocol_version=1))
        hello = receive_message(connection)["result"]
        config = dict(schema_version=3, id="flood-0", params={"marker": str(marker)},
                      execution=dict(mode="sync", call_every_actions=1, control_mode="qpos"),
                      tui_commands={"flood": {"handler": "flood", "args": []}})
        send_message(connection, dict(type="load", rpc_id=2, session_id="session", policy_id="flood-0",
            timeouts=dict(initialize=5, inference=5, release=1),
            event=dict(type="initialize", config=config, config_version=1)))
        ready = loaded = None
        while ready is None or loaded is None:
            message = receive_message(connection)
            if message.get("rpc_id") == 2:
                assert "error" not in message, message
                loaded = message["result"]
            if message.get("event", {}).get("type") == "ready":
                ready = message
        runtime = loaded["runtime_id"]
        send_message(connection, dict(type="dispatch", runtime_id=runtime, session_id="session",
            command_timeout=-1,
            event=dict(type="policy_command", command_id="flood", name="flood", args=[], config_version=1)))
        # The receiver deliberately does not drain stdout while the callback runs.
        wait_for(marker.exists, timeout=5)
        assert marker.read_text() == "finished"
        send_message(connection, dict(type="ping", rpc_id=3))
        send_message(connection, dict(type="dispatch", runtime_id=runtime, session_id="session",
            event=dict(type="monitor", monitor_id=1, monitor_epoch=0, config_version=1)))
        received = set()
        lost_count = 0
        log_count = 0
        while received != {"ping", "command_result", "monitor_result"} or log_count + lost_count < 5000:
            message = receive_message(connection)
            if message.get("rpc_id") == 3:
                assert message["result"]["healthy"]
                received.add("ping")
            kind = message.get("event", {}).get("type")
            if kind in ("command_result", "monitor_result"):
                assert message["event"]["result"] == ("finished" if kind == "command_result" else {"alive": 1})
                received.add(kind)
            if message["type"] == "log":
                assert message["host_instance_id"] == hello["host_instance_id"]
                assert message["host_id"] == message["policy_instance_id"] == "flood-0"
                assert message["source_policy_id"] == "flood"
                assert message["runtime_id"] == runtime
                if message["message"].startswith("{"):
                    event = json.loads(message["message"])
                    if event.get("event") == "sdk.log_gap":
                        lost_count += event["count"]
                else:
                    log_count += 1
        assert lost_count > 0
        assert log_count + lost_count == 5000
        send_message(connection, dict(type="stop", rpc_id=4, runtime_id=runtime,
                                     sessions=["session"], release_timeout=1))
        while True:
            message = receive_message(connection)
            if message.get("rpc_id") == 4:
                assert message["result"]["cleanup_confirmed"]
                assert not message["result"]["forced"]
                break
        assert host.process.poll() is None
    finally:
        connection.close()
        host.close()
