from __future__ import annotations

import json
from collections.abc import Iterator

MAX_CONNECT_FRAME_BYTES = 16 * 1024 * 1024


class ConnectProtocolError(RuntimeError):
    pass


def frame_connect_message(payload: bytes, flags: int = 0) -> bytes:
    if len(payload) > MAX_CONNECT_FRAME_BYTES:
        raise ConnectProtocolError("Connect message exceeded the size limit")
    if not 0 <= flags <= 255:
        raise ConnectProtocolError("Connect flags must fit in one byte")
    return bytes((flags,)) + len(payload).to_bytes(4, "big") + payload


def parse_connect_frames(
    buffer: bytes | bytearray,
    *,
    max_frame_bytes: int = MAX_CONNECT_FRAME_BYTES,
) -> Iterator[tuple[int, bytes]]:
    offset = 0
    total = len(buffer)
    while total - offset >= 5:
        flags = buffer[offset]
        payload_size = int.from_bytes(buffer[offset + 1 : offset + 5], "big")
        if payload_size > max_frame_bytes:
            raise ConnectProtocolError("Connect frame exceeded the size limit")
        frame_end = offset + 5 + payload_size
        if frame_end > total:
            break
        yield flags, bytes(buffer[offset + 5 : frame_end])
        offset = frame_end
    if isinstance(buffer, bytearray) and offset:
        del buffer[:offset]


def parse_connect_end_stream(payload: bytes) -> ConnectProtocolError | None:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return ConnectProtocolError(
            "Cursor returned a malformed Connect end-stream message"
        )
    if not isinstance(decoded, dict) or not decoded.get("error"):
        return None
    error = decoded["error"]
    if not isinstance(error, dict):
        return ConnectProtocolError("Cursor returned an unknown Connect error")
    code = str(error.get("code") or "unknown")
    message = str(error.get("message") or "Unknown error")
    return ConnectProtocolError(f"Cursor Connect error {code}: {message}")
