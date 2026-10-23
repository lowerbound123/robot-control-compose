import socket
import struct
import threading

import pytest

from rc_policy_sdk.wire import MAX_MESSAGE_BYTES, ProtocolError, receive_message, send_message


def test_framing_survives_fragmented_reads():
    reader, writer = socket.socketpair()
    payload = b'{"type":"ready","value":3}'
    framed = struct.pack("!I", len(payload)) + payload
    thread = threading.Thread(target=lambda: [writer.sendall(bytes([byte])) for byte in framed])
    thread.start()
    try:
        assert receive_message(reader) == {"type": "ready", "value": 3}
    finally:
        thread.join()
        reader.close()
        writer.close()


@pytest.mark.parametrize("payload", [b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'[]', b'{"x":1,"x":2}'])
def test_rejects_invalid_json_messages(payload):
    reader, writer = socket.socketpair()
    try:
        writer.sendall(struct.pack("!I", len(payload)) + payload)
        with pytest.raises(ProtocolError):
            receive_message(reader)
    finally:
        reader.close()
        writer.close()


def test_rejects_size_before_reading_payload():
    reader, writer = socket.socketpair()
    try:
        writer.sendall(struct.pack("!I", MAX_MESSAGE_BYTES + 1))
        with pytest.raises(ProtocolError):
            receive_message(reader)
        with pytest.raises(ProtocolError):
            send_message(writer, {"x": float("nan")})
    finally:
        reader.close()
        writer.close()


def test_truncated_frame_is_not_a_message():
    reader, writer = socket.socketpair()
    try:
        writer.sendall(struct.pack("!I", 8) + b"{}"); writer.close()
        with pytest.raises(EOFError):
            receive_message(reader)
    finally:
        reader.close()
