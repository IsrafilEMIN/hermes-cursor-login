from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from hermes_cursor_login.bridge import BridgeApplication, create_http_server

TOKEN = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop"


class StubRunner:
    def __init__(self) -> None:
        self.calls = 0

    def list_models(self) -> list[str]:
        return ["auto", "composer-2.5"]

    def complete(
        self,
        *,
        model,
        messages,
        tools,
        tool_choice,
        cancel_event=None,
        on_text_delta=None,
        on_reasoning_delta=None,
    ):
        self.calls += 1
        if on_reasoning_delta is not None:
            on_reasoning_delta("thinking")
        if on_text_delta is not None:
            on_text_delta("hello")
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "hello",
                        "reasoning_content": "thinking",
                        "tool_calls": None,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }


def headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_every_route_requires_bridge_bearer_before_runner_access() -> None:
    runner = StubRunner()
    app = BridgeApplication(runner=runner, token=TOKEN)

    health = app.handle("GET", "/health", {}, b"")
    models = app.handle("GET", "/v1/models", {}, b"")
    completion = app.handle(
        "POST",
        "/v1/chat/completions",
        {},
        json.dumps({"model": "auto", "messages": []}).encode(),
    )

    assert [health.status, models.status, completion.status] == [401, 401, 401]
    assert runner.calls == 0


def test_models_and_completion_follow_openai_shapes() -> None:
    app = BridgeApplication(runner=StubRunner(), token=TOKEN)

    models = app.handle("GET", "/v1/models", headers(), b"")
    completion = app.handle(
        "POST",
        "/v1/chat/completions",
        headers(),
        json.dumps(
            {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
        ).encode(),
    )

    assert json.loads(models.body) == {
        "object": "list",
        "data": [
            {"id": "auto", "object": "model", "owned_by": "cursor"},
            {"id": "composer-2.5", "object": "model", "owned_by": "cursor"},
        ],
    }
    assert json.loads(completion.body)["choices"][0]["message"]["content"] == "hello"


def test_stream_response_is_valid_sse_with_usage() -> None:
    app = BridgeApplication(runner=StubRunner(), token=TOKEN)

    response = app.handle(
        "POST",
        "/v1/chat/completions",
        headers(),
        json.dumps(
            {
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            }
        ).encode(),
    )

    assert response.content_type == "text/event-stream"
    assert b'"reasoning_content":"thinking"' in response.body
    assert b'"total_tokens":3' in response.body
    assert response.body.endswith(b"data: [DONE]\n\n")


def test_http_adapter_rejects_unauthorized_request_before_reading_body() -> None:
    app = BridgeApplication(runner=StubRunner(), token=TOKEN)
    server = create_http_server(host="127.0.0.1", port=0, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
            data=b"{",
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("unauthorized request succeeded")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_bridge_refuses_non_loopback_binding() -> None:
    app = BridgeApplication(runner=StubRunner(), token=TOKEN)

    try:
        create_http_server(host="0.0.0.0", port=0, app=app)
    except ValueError as exc:
        assert "127.0.0.1" in str(exc)
    else:
        raise AssertionError("non-loopback bridge was allowed")


def test_stream_disconnect_cancels_upstream_and_releases_admission() -> None:
    cancelled = threading.Event()

    class DisconnectRunner(StubRunner):
        def complete(self, *, on_text_delta=None, cancel_event=None, **kwargs):
            on_text_delta("partial")
            assert cancel_event.wait(3)
            cancelled.set()
            raise InterruptedError("cancelled")

    app = BridgeApplication(runner=DisconnectRunner(), token=TOKEN, max_concurrency=1)
    server = create_http_server(host="127.0.0.1", port=0, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps(
            {
                "model": "default",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            }
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
            data=body,
            headers={**headers(), "Content-Type": "application/json"},
            method="POST",
        )
        response = urllib.request.urlopen(request, timeout=5)
        assert response.read(1)
        response.close()
        assert cancelled.wait(5)
        models = urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/v1/models",
                headers=headers(),
            ),
            timeout=5,
        )
        assert models.status == 200
        models.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
