from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cursor_login import native_runner
from hermes_cursor_login.native_runner import NativeCursorRunner, NativeRunnerConfig


def response(*, tool_name: str | None = None):
    tool_calls = None
    finish_reason = "stop"
    if tool_name is not None:
        tool_calls = [
            SimpleNamespace(
                id="call-1",
                type="function",
                function=SimpleNamespace(name=tool_name, arguments='{"path":"a.txt"}'),
            )
        ]
        finish_reason = "tool_calls"
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None if tool_calls else "answer",
                    reasoning_content="reasoning",
                    tool_calls=tool_calls,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(completion_tokens=3),
    )


def test_completion_resolves_fresh_token_and_flattens_openai_tools(monkeypatch) -> None:
    captured: dict[str, object] = {}
    tokens = iter(["token-one", "token-two"])

    def run(**kwargs):
        captured.update(kwargs)
        return response(tool_name="read_file")

    monkeypatch.setattr(native_runner, "run_cursor_agent_turn", run)
    runner = NativeCursorRunner(NativeRunnerConfig(token_resolver=lambda: next(tokens)))
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }
    ]

    first = runner.complete(
        model="auto",
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "read"},
        ],
        tools=tools,
        tool_choice="auto",
    )
    second = runner.complete(
        model="auto",
        messages=[{"role": "user", "content": "read again"}],
        tools=tools,
        tool_choice="auto",
    )

    assert captured["api_key"] == "token-two"
    assert captured["system_prompt"] == []
    assert captured["tools"] == [
        {
            "name": "read_file",
            "description": "Read",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        }
    ]
    assert json.loads(
        first["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    ) == {"path": "a.txt"}
    assert second["usage"]["completion_tokens"] == 3


def test_unoffered_tool_call_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        native_runner, "run_cursor_agent_turn", lambda **_: response(tool_name="shell")
    )
    runner = NativeCursorRunner(NativeRunnerConfig(token_resolver=lambda: "token"))

    with pytest.raises(RuntimeError, match="unoffered"):
        runner.complete(
            model="auto",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            tool_choice="auto",
        )


def test_specific_tool_choice_filters_and_enforces_selected_tool(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def run(**kwargs):
        captured.update(kwargs)
        return response(tool_name="read_file")

    monkeypatch.setattr(native_runner, "run_cursor_agent_turn", run)
    runner = NativeCursorRunner(NativeRunnerConfig(token_resolver=lambda: "token"))
    tools = [
        {
            "type": "function",
            "function": {"name": "read_file", "parameters": {"type": "object"}},
        },
        {
            "type": "function",
            "function": {"name": "write_file", "parameters": {"type": "object"}},
        },
    ]

    completion = runner.complete(
        model="auto",
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice={"type": "function", "function": {"name": "read_file"}},
    )

    assert [tool["name"] for tool in captured["tools"]] == ["read_file"]
    assert completion["choices"][0]["finish_reason"] == "tool_calls"


def test_live_model_catalog_falls_back_only_when_discovery_is_unavailable(
    monkeypatch,
) -> None:
    runner = NativeCursorRunner(NativeRunnerConfig(token_resolver=lambda: "token"))
    monkeypatch.setattr(
        native_runner, "fetch_cursor_usable_models", lambda **_: ["live-b", "live-a"]
    )
    assert runner.list_models() == ["live-b", "live-a"]

    monkeypatch.setattr(native_runner, "fetch_cursor_usable_models", lambda **_: None)
    assert runner.list_models()[0] == "default"


def test_duplicate_tool_names_are_rejected_before_network_call(monkeypatch) -> None:
    monkeypatch.setattr(
        native_runner,
        "run_cursor_agent_turn",
        lambda **_: (_ for _ in ()).throw(AssertionError("network should not run")),
    )
    runner = NativeCursorRunner(NativeRunnerConfig(token_resolver=lambda: "token"))
    tools = [
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "read_file"}},
    ]

    with pytest.raises(ValueError, match="unique"):
        runner.complete(
            model="default",
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            tool_choice="auto",
        )
