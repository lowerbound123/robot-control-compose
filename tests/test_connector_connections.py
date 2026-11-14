"""Connection ownership must end without cancelling commands or image leases."""
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rc_connector_sdk.server import ConnectorServer
from rc_connector_sdk.wire import receive_message, send_message


def accept_connections(server, handler, connections):
    class Listener:
        pending = iter(connections)

        def accept(self):
            try:
                return next(self.pending), None
            except StopIteration:
                raise OSError('test listener exhausted') from None

    server._accept(Listener(), handler)


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.005)


@pytest.mark.parametrize('malformed', [False, True])
def test_query_disconnect_releases_transport_but_preserves_image_leases(malformed):
    server = ConnectorServer(object(), '/unused')
    lease = SimpleNamespace(release=Mock())
    server.leases['borrowed'] = lease
    pairs = [socket.socketpair() for _ in range(8)]
    try:
        for peer, _ in pairs:
            if malformed:
                peer.sendall(b'\x00\x00\x00\x01!')
            peer.close()
        accepted = [connection for _, connection in pairs]
        accept_connections(server, server._query_connection, accepted)
        for thread in tuple(server.threads):
            thread.join(2)
            assert not thread.is_alive()
        assert all(connection.fileno() == -1 for connection in accepted)
        assert not server.connections and not server.threads
        assert server.leases == {'borrowed': lease}
        lease.release.assert_not_called()
        server._query('release_images', {'lease_ids': ['borrowed']})
        lease.release.assert_called_once_with()
    finally:
        server.stop.set()
        for peer, connection in pairs:
            peer.close()
            connection.close()


def test_command_half_close_keeps_fifo_receipts_then_releases_connection():
    published = []
    driver = SimpleNamespace(execute_arms=published.append, read_state=lambda: {})
    server = ConnectorServer(driver, '/unused')
    peer, connection = socket.socketpair()
    peer.settimeout(2)
    publisher = threading.Thread(target=server._publish, daemon=True)
    try:
        for identity in (1, 2):
            send_message(peer, {'id': identity, 'method': 'send_targets', 'params': {
                'left': {'mode': 'qpos', 'values': [identity] * 7}}})
        peer.shutdown(socket.SHUT_WR)
        accept_connections(server, server._command_connection, [connection])
        wait_for(lambda: server.commands.qsize() == 2)
        for thread in tuple(server.threads):
            thread.join(2)
            assert not thread.is_alive()
        publisher.start()
        receipts = [receive_message(peer), receive_message(peer)]
        assert [row['id'] for row in receipts] == [1, 2]
        assert all(row['success'] for row in receipts)
        assert [row['left']['values'][0] for row in published] == [1, 2]
        assert peer.recv(1) == b''
        wait_for(lambda: not server.connections and not server.threads)
        assert connection.fileno() == -1
    finally:
        server.stop.set()
        peer.close()
        connection.close()
        if publisher.ident is not None:
            server.commands.put(None)
            publisher.join(2)


def test_rejected_extra_command_connections_leave_no_registry_entries():
    server = ConnectorServer(object(), '/unused')
    pairs = [socket.socketpair() for _ in range(4)]
    server._command_lock.acquire()
    try:
        accept_connections(server, server._command_connection, [connection for _, connection in pairs])
        for thread in tuple(server.threads):
            thread.join(2)
        assert all(connection.fileno() == -1 for _, connection in pairs)
        assert not server.connections and not server.threads
    finally:
        server._command_lock.release()
        server.stop.set()
        for peer, connection in pairs:
            peer.close()
            connection.close()
