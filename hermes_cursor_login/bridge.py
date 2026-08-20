from __future__ import annotations

import hmac
import json
import logging
import select
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .installer import validate_bridge_token

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BridgeResponse:
    status: int
    body: bytes
    content_type: str = "application/json"


def _encode_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_response(payload: dict[str, Any], status: int = 200) -> BridgeResponse:
    return BridgeResponse(status=status, body=_encode_json(payload).encode())


def _error_payload(message: str, error_type: str) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": None,
            "code": None,
        }
    }


def _error(message: str, status: int, error_type: str) -> BridgeResponse:
    return _json_response(_error_payload(message, error_type), status)


def _bad_request(message: str) -> BridgeResponse:
    return _error(message, 400, "invalid_request_error")


def _sse(payload: dict[str, Any] | str) -> bytes:
    encoded = payload if isinstance(payload, str) else _encode_json(payload)
    return f"data: {encoded}\n\n".encode()


def _sse_chunk(
    common: dict[str, Any],
    delta: dict[str, Any],
    finish_reason: str | None = None,
    usage: Any = None,
) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        **common,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _completion_sse(completion: dict[str, Any]) -> bytes:
    choice = completion["choices"][0]
    message = choice["message"]
    common = {
        "id": completion["id"],
        "object": "chat.completion.chunk",
        "created": completion["created"],
        "model": completion["model"],
    }
    chunks = [_sse_chunk(common, {"role": "assistant"})]
    delta = {
        key: message[key]
        for key in ("content", "reasoning_content", "tool_calls")
        if message.get(key) is not None
    }
    if delta:
        chunks.append(_sse_chunk(common, delta))
    chunks.append(
        _sse_chunk(
            common,
            {},
            choice.get("finish_reason") or "stop",
            completion.get("usage"),
        )
    )
    return b"".join(_sse(chunk) for chunk in chunks) + _sse("[DONE]")


class BridgeApplication:
    def __init__(
        self,
        *,
        runner: Any,
        token: str,
        max_body_bytes: int = 8 * 1024 * 1024,
        max_concurrency: int = 2,
    ) -> None:
        self.runner = runner
        self.token = validate_bridge_token(token)
        if max_body_bytes <= 0 or max_concurrency <= 0:
            raise ValueError("Bridge limits must be positive")
        self.max_body_bytes = max_body_bytes
        self.max_concurrency = max_concurrency
        self._slots = threading.BoundedSemaphore(max_concurrency)

    def authorized(self, headers: dict[str, str]) -> bool:
        value = ""
        for name, header in headers.items():
            if str(name).lower() == "authorization":
                value = str(header)
        return hmac.compare_digest(value, f"Bearer {self.token}")

    def acquire(self) -> bool:
        return self._slots.acquire(blocking=False)

    def release(self) -> None:
        self._slots.release()

    def parse_completion(self, body: bytes) -> dict[str, Any] | BridgeResponse:
        if len(body) > self.max_body_bytes:
            return _error("Request body is too large", 413, "invalid_request_error")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return _bad_request("Request body must be valid UTF-8 JSON")
        if not isinstance(payload, dict):
            return _bad_request("Request body must be a JSON object")
        model = payload.get("model")
        messages = payload.get("messages")
        tools = payload.get("tools")
        if not isinstance(model, str) or not model.strip():
            return _bad_request("model must be a non-empty string")
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) for message in messages
        ):
            return _bad_request("messages must be an array of objects")
        if tools is not None and (
            not isinstance(tools, list)
            or not all(isinstance(tool, dict) for tool in tools)
        ):
            return _bad_request("tools must be an array of objects")
        return payload

    def complete(
        self,
        payload: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
        on_text_delta: Any = None,
        on_reasoning_delta: Any = None,
    ) -> dict[str, Any]:
        return self.runner.complete(
            model=payload["model"],
            messages=payload["messages"],
            tools=payload.get("tools"),
            tool_choice=payload.get("tool_choice"),
            cancel_event=cancel_event,
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
        )

    def handle(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        *,
        cancel_event: threading.Event | None = None,
    ) -> BridgeResponse:
        if not self.authorized(headers):
            return _error(
                "Invalid or missing bearer token", 401, "authentication_error"
            )
        method = method.upper()
        route = urlsplit(path).path.rstrip("/") or "/"
        if method == "GET" and route == "/health":
            return _json_response({"status": "ok"})
        if method == "GET" and route == "/v1/models":
            return self._limited(
                lambda: _json_response(
                    {
                        "object": "list",
                        "data": [
                            {"id": model, "object": "model", "owned_by": "cursor"}
                            for model in self.runner.list_models()
                        ],
                    }
                ),
                "Cursor model discovery failed",
            )
        if method != "POST" or route != "/v1/chat/completions":
            return _error("Route not found", 404, "invalid_request_error")
        parsed = self.parse_completion(body)
        if isinstance(parsed, BridgeResponse):
            return parsed
        result = self._limited(
            lambda: self.complete(parsed, cancel_event=cancel_event),
            "Cursor completion failed",
        )
        if isinstance(result, BridgeResponse):
            return result
        if parsed.get("stream") is True:
            return BridgeResponse(200, _completion_sse(result), "text/event-stream")
        return _json_response(result)

    def _invoke(self, work: Any, fail_message: str) -> Any:
        try:
            return work()
        except Exception as exc:
            logger.warning("%s (%s)", fail_message, type(exc).__name__)
            return _error(fail_message, 502, "cursor_error")

    def _limited(self, work: Any, fail_message: str) -> Any:
        if not self.acquire():
            return _error("Cursor bridge is busy", 429, "rate_limit_error")
        try:
            return self._invoke(work, fail_message)
        finally:
            self.release()


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HermesCursorLogin/0.1"

    @property
    def app(self) -> BridgeApplication:
        return self.server.bridge_app

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15.0)

    def do_GET(self) -> None:
        self._dispatch()

    do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

    def _dispatch(self) -> None:
        headers = dict(self.headers.items())
        if not self.app.authorized(headers):
            self._send(
                _error("Invalid or missing bearer token", 401, "authentication_error")
            )
            return
        route = urlsplit(self.path).path.rstrip("/") or "/"
        if self.command == "POST" and route == "/v1/chat/completions":
            self._completion(headers)
            return
        self._send(self.app.handle(self.command, self.path, headers, b""))

    def _fail(
        self, message: str, status: int, error_type: str = "invalid_request_error"
    ) -> None:
        self._send(_error(message, status, error_type))

    def _completion(self, headers: dict[str, str]) -> None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._fail("Content-Length is required", 411)
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._fail("Invalid Content-Length", 400)
            return
        if length < 0 or length > self.app.max_body_bytes:
            status = 413 if length > self.app.max_body_bytes else 400
            self._fail("Invalid request body length", status)
            return
        if not self.app.acquire():
            self._fail("Cursor bridge is busy", 429, "rate_limit_error")
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                self._fail("Request body ended early", 400)
                return
            parsed = self.app.parse_completion(body)
            if isinstance(parsed, BridgeResponse):
                self._send(parsed)
                return
            if parsed.get("stream") is True:
                self._stream(parsed)
                return
            completion = self.app._invoke(
                lambda: self.app.complete(parsed), "Cursor completion failed"
            )
            self._send(
                completion
                if isinstance(completion, BridgeResponse)
                else _json_response(completion)
            )
        except (OSError, TimeoutError):
            self.close_connection = True
        finally:
            self.app.release()

    def _stream(self, payload: dict[str, Any]) -> None:
        stream_id = f"chatcmpl-cursor-{uuid.uuid4().hex}"
        created = int(time.time())
        common = {
            "id": stream_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": payload["model"],
        }
        cancel_event = threading.Event()
        monitor_stop = threading.Event()

        def monitor_disconnect() -> None:
            while not monitor_stop.wait(0.2):
                try:
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if readable and not self.connection.recv(1, socket.MSG_PEEK):
                        cancel_event.set()
                        return
                except BlockingIOError:
                    continue
                except OSError:
                    cancel_event.set()
                    return

        monitor = threading.Thread(target=monitor_disconnect, daemon=True)
        monitor.start()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(
            delta: dict[str, Any], finish_reason: str | None = None, usage: Any = None
        ) -> None:
            try:
                self.wfile.write(_sse(_sse_chunk(common, delta, finish_reason, usage)))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                cancel_event.set()
                raise ConnectionAbortedError("Client disconnected") from exc

        try:
            emit({"role": "assistant"})
            completion = self.app.complete(
                payload,
                cancel_event=cancel_event,
                on_text_delta=lambda text: emit({"content": text}),
                on_reasoning_delta=lambda text: emit({"reasoning_content": text}),
            )
            choice = completion["choices"][0]
            message = choice["message"]
            if message.get("tool_calls"):
                emit(
                    {
                        "tool_calls": [
                            {"index": index, **tool_call}
                            for index, tool_call in enumerate(message["tool_calls"])
                        ]
                    }
                )
            emit({}, choice.get("finish_reason") or "stop", completion.get("usage"))
            self.wfile.write(_sse("[DONE]"))
            self.wfile.flush()
        except ConnectionAbortedError:
            cancel_event.set()
        except Exception as exc:
            logger.warning(
                "Cursor streaming completion failed (%s)", type(exc).__name__
            )
            try:
                self.wfile.write(
                    _sse(_error_payload("Cursor completion failed", "cursor_error"))
                )
                self.wfile.write(_sse("[DONE]"))
                self.wfile.flush()
            except OSError:
                cancel_event.set()
        finally:
            monitor_stop.set()
            monitor.join(timeout=1.0)

    def _send(self, response: BridgeResponse) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if response.body:
            self.wfile.write(response.body)

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("bridge http: " + format, *args)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_http_server(
    *,
    host: str,
    port: int,
    app: BridgeApplication,
) -> BridgeServer:
    if host != "127.0.0.1":
        raise ValueError("Cursor bridge must bind exactly to 127.0.0.1")
    if not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535")
    server = BridgeServer((host, port), BridgeHandler)
    server.bridge_app = app
    return server
