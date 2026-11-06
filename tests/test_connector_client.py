from collections import deque
import threading
from unittest.mock import Mock

import pytest

from rc_connector_sdk.client import ConnectorClient


def test_duplicate_receipt_after_poll_does_not_fault_or_consume_twice(monkeypatch):
    client = ConnectorClient.__new__(ConnectorClient)
    client.closed, client.failure, client.serial = False, None, 1
    client._lock = threading.RLock()
    client._pending = {1: {}}
    client._results, client._outbox = deque(), deque()
    client._command = object()
    client.wake = Mock()
    calls = []

    def receive(connection):
        if len(calls) == 1:
            assert len(client.poll_results()) == 1
        if len(calls) == 2:
            client.closed = True
        calls.append(True)
        return {'id': 1, 'success': True}

    monkeypatch.setattr('rc_connector_sdk.client.receive_message', receive)
    client._read()
    assert client.failure is None
    assert client.poll_results() == []
    client.wake.assert_called_once()


def test_shutdown_delivers_final_receipts_even_if_close_fails():
    client = ConnectorClient.__new__(ConnectorClient)
    client.closed = False
    client._query = Mock()
    client._pending = {1: {}}
    receipt = {'id': 1, 'success': False, 'arms': {'left': 'published', 'right': 'unknown'}}

    def poll():
        client._pending.clear()
        return [receipt]

    client.poll_results = poll
    client._request = Mock(return_value={'clean': False})
    client.disconnect = Mock()
    recorded = []
    with pytest.raises(RuntimeError, match='unreleased'):
        client.close(on_results=recorded.extend)
    assert recorded == [receipt]
    client.disconnect.assert_called_once()
