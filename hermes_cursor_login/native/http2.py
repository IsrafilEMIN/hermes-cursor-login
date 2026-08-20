from __future__ import annotations

import socket
import ssl
import uuid

import h2.config
import h2.connection

from .constants import CURSOR_CLIENT_VERSION


def cursor_connect_headers(
    *, path: str, host: str, token: str, content_type: str
) -> list[tuple[str, str]]:
    return [
        (":method", "POST"),
        (":path", path),
        (":scheme", "https"),
        (":authority", host),
        ("authorization", f"Bearer {token}"),
        ("content-type", content_type),
        ("connect-protocol-version", "1"),
        ("te", "trailers"),
        ("x-ghost-mode", "true"),
        ("x-cursor-client-version", CURSOR_CLIENT_VERSION),
        ("x-cursor-client-type", "cli"),
        ("x-request-id", str(uuid.uuid4())),
    ]


def send_h2(tls_socket: ssl.SSLSocket, connection: h2.connection.H2Connection) -> None:
    output = connection.data_to_send()
    if output:
        tls_socket.sendall(output)


def open_cursor_h2(
    host: str,
    port: int,
    *,
    connect_timeout: float,
    io_timeout: float,
) -> tuple[socket.socket, ssl.SSLSocket, h2.connection.H2Connection]:
    network_socket = socket.create_connection((host, port), timeout=connect_timeout)
    try:
        network_socket.settimeout(io_timeout)
        context = ssl.create_default_context()
        context.set_alpn_protocols(["h2"])
        tls_socket = context.wrap_socket(network_socket, server_hostname=host)
    except BaseException:
        network_socket.close()
        raise
    try:
        if tls_socket.selected_alpn_protocol() != "h2":
            raise RuntimeError("Cursor did not negotiate HTTP/2")
        connection = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
        )
        connection.initiate_connection()
        send_h2(tls_socket, connection)
        return network_socket, tls_socket, connection
    except BaseException:
        tls_socket.close()
        raise


def close_h2(
    tls_socket: ssl.SSLSocket | None, network_socket: socket.socket | None
) -> None:
    if tls_socket is not None:
        tls_socket.close()
    elif network_socket is not None:
        network_socket.close()
