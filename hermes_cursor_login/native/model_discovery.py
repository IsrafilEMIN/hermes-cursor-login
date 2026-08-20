from __future__ import annotations

import time

import h2.events
from google.protobuf.message import DecodeError

from .constants import (
    CURSOR_API_URL,
    CURSOR_GET_USABLE_MODELS_PATH,
    CursorTransportError,
    normalize_cursor_model_id,
    validate_cursor_api_url,
)
from .http2 import close_h2, cursor_connect_headers, open_cursor_h2, send_h2
from .proto import agent_pb2

MAX_MODEL_RESPONSE_BYTES = 4 * 1024 * 1024


def parse_usable_models(payload: bytes) -> list[str]:
    response = agent_pb2.GetUsableModelsResponse()
    try:
        response.ParseFromString(payload)
    except DecodeError as exc:
        raise RuntimeError("Cursor returned a malformed model catalog") from exc
    model_ids = {
        normalize_cursor_model_id(details.model_id)
        for details in response.models
        if details.model_id.strip()
    }
    return sorted(model_ids)


def fetch_cursor_usable_models(
    *,
    api_key: str,
    base_url: str = CURSOR_API_URL,
    timeout: float = 12.0,
) -> list[str] | None:
    token = api_key.strip()
    if not token:
        raise ValueError("Cursor access token is required")
    if timeout <= 0:
        raise ValueError("Cursor model timeout must be positive")
    host, port = validate_cursor_api_url(base_url)
    network_socket = None
    tls_socket = None
    deadline = time.monotonic() + timeout
    status: str | None = None
    grpc_status: str | None = None
    response = bytearray()
    stream_ended = False

    try:
        network_socket, tls_socket, connection = open_cursor_h2(
            host,
            port,
            connect_timeout=min(timeout, 30.0),
            io_timeout=min(timeout, 5.0),
        )
        stream_id = connection.get_next_available_stream_id()
        connection.send_headers(
            stream_id,
            cursor_connect_headers(
                path=CURSOR_GET_USABLE_MODELS_PATH,
                host=host,
                token=token,
                content_type="application/proto",
            ),
            end_stream=False,
        )
        connection.send_data(
            stream_id,
            agent_pb2.GetUsableModelsRequest().SerializeToString(),
            end_stream=True,
        )
        send_h2(tls_socket, connection)

        while not stream_ended:
            if time.monotonic() >= deadline:
                raise TimeoutError("Cursor model discovery timed out")
            try:
                data = tls_socket.recv(65535)
            except TimeoutError:
                continue
            if not data:
                break
            events = connection.receive_data(data)
            for event in events:
                if isinstance(event, h2.events.ResponseReceived):
                    status = str(dict(event.headers).get(":status", ""))
                elif isinstance(event, h2.events.TrailersReceived):
                    grpc_status = str(dict(event.headers).get("grpc-status", "") or "")
                elif isinstance(event, h2.events.DataReceived):
                    response.extend(event.data)
                    if len(response) > MAX_MODEL_RESPONSE_BYTES:
                        raise RuntimeError(
                            "Cursor model catalog exceeded the size limit"
                        )
                    connection.acknowledge_received_data(
                        event.flow_controlled_length, event.stream_id
                    )
                elif isinstance(event, h2.events.StreamReset):
                    raise CursorTransportError(
                        f"Cursor reset model discovery: {event.error_code}"
                    )
                elif isinstance(event, h2.events.ConnectionTerminated):
                    raise CursorTransportError(
                        f"Cursor terminated model discovery: {event.error_code}"
                    )
                elif isinstance(event, h2.events.StreamEnded):
                    stream_ended = True
            send_h2(tls_socket, connection)

        if status and status != "200":
            raise RuntimeError(f"Cursor model discovery failed with HTTP {status}")
        if grpc_status and grpc_status != "0":
            raise RuntimeError(f"Cursor model discovery failed with gRPC {grpc_status}")
        if not response:
            return None
        models = parse_usable_models(bytes(response))
        return models or None
    except (OSError, TimeoutError):
        return None
    finally:
        close_h2(tls_socket, network_socket)
