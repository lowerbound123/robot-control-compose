import json
import threading
from types import SimpleNamespace

from rc_policy_sdk.host import Peer, PolicyHost
from rc_policy_sdk.outbox import PriorityOutbox
from robot_control.host_client import HostClient


def packet():
    return dict(type="dispatch", session_id="s", runtime_id="r",
                event=dict(type="monitor", monitor_id=1, monitor_epoch=0, config_version=1))


def test_rc_host_runtime_and_peer_queues_keep_monitor_out_of_control_capacity():
    client = HostClient.__new__(HostClient)
    client.condition = threading.Condition()
    client.closed = False
    client.failure = None
    client.members = {"s": SimpleNamespace()}
    client.outbox = PriorityOutbox(1)
    client.outbox.put_nowait(b"control")
    assert client.enqueue_monitor(packet())
    assert not client.enqueue_monitor(packet())
    assert client.outbox.get() == b"control"
    assert json.loads(client.outbox.get()[4:])["event"]["type"] == "monitor"

    host = PolicyHost.__new__(PolicyHost)
    host.runtime_outbox = PriorityOutbox(1)
    host.runtime_outbox.put_nowait(b"control")
    host._send_runtime(packet())
    assert host.runtime_outbox.get() == b"control"
    assert json.loads(host.runtime_outbox.get()[4:])["event"]["type"] == "monitor"

    peer = Peer.__new__(Peer)
    peer.outbox = PriorityOutbox(1)
    peer.outbox.put_nowait(b"control")
    reply = dict(type="policy_event", session_id="s", runtime_id="r",
                 event=dict(type="monitor_result", monitor_id=1, monitor_epoch=0, config_version=1, result={"x":1}))
    assert peer.send(reply)
    assert peer.outbox.get() == b"control"
    assert json.loads(peer.outbox.get()[4:])["event"]["result"] == {"x":1}
