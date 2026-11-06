"""Bounded, finite JSON messages framed with a network-order uint32 length."""

import json
import socket
import struct

MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class ProtocolError(ValueError):
    pass


def encode_message(message: dict) -> bytes:
    if not isinstance(message, dict):
        raise ProtocolError("message must be an object")
    try:
        payload = json.dumps(message, allow_nan=False, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolError(f"message must contain finite JSON values: {exc}") from exc
    if not payload or len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message exceeds {MAX_MESSAGE_BYTES} bytes")
    return struct.pack("!I", len(payload)) + payload


def send_message(connection: socket.socket, message: dict) -> None:
    connection.sendall(encode_message(message))


def _read_exact(connection, length):
    parts = bytearray()
    while len(parts) < length:
        data = connection.recv(length - len(parts))
        if not data:
            raise EOFError("policy connection closed during frame")
        parts.extend(data)
    return bytes(parts)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ProtocolError(f"nonfinite JSON value: {value}")


def receive_message(connection: socket.socket) -> dict:
    size = struct.unpack("!I", _read_exact(connection, 4))[0]
    if not size or size > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"invalid message length {size}")
    try:
        result = json.loads(_read_exact(connection, size), parse_constant=_constant, object_pairs_hook=_object)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise ProtocolError("message must be an object")
    # JSON numbers such as 1e999 also overflow, without triggering parse_constant.
    encode_message(result)
    return result
