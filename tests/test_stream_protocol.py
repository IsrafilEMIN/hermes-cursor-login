from __future__ import annotations

import json

import pytest
from google.protobuf import struct_pb2

from hermes_cursor_login.native.connect_framing import (
    ConnectProtocolError,
    frame_connect_message,
    parse_connect_frames,
)
from hermes_cursor_login.native.proto import agent_pb2
from hermes_cursor_login.native.stream_client import _new_state, _server_payload


def value_bytes(value: object) -> bytes:
    message = struct_pb2.Value()
    if isinstance(value, str):
        message.string_value = value
    elif isinstance(value, bool):
        message.bool_value = value
    elif isinstance(value, int | float):
        message.number_value = value
    return message.SerializeToString()


def server_payload(message: agent_pb2.AgentServerMessage) -> bytes:
    return message.SerializeToString()


def test_text_and_reasoning_deltas_remain_separate() -> None:
    state = _new_state()
    text: list[str] = []
    reasoning: list[str] = []
    sent: list[object] = []

    for update in (
        agent_pb2.InteractionUpdate(
            thinking_delta=agent_pb2.ThinkingDeltaUpdate(text="think")
        ),
        agent_pb2.InteractionUpdate(
            text_delta=agent_pb2.TextDeltaUpdate(text="answer")
        ),
        agent_pb2.InteractionUpdate(turn_ended=agent_pb2.TurnEndedUpdate()),
    ):
        _server_payload(
            server_payload(agent_pb2.AgentServerMessage(interaction_update=update)),
            state=state,
            blob_store={},
            tools=[],
            send_message=sent.append,
            on_text_delta=text.append,
            on_reasoning_delta=reasoning.append,
        )

    assert state["content"] == ["answer"]
    assert state["reasoning"] == ["think"]
    assert text == ["answer"]
    assert reasoning == ["think"]
    assert state["completed"] is True


def test_mcp_exec_request_becomes_openai_tool_boundary_without_execution() -> None:
    state = _new_state()
    sent: list[object] = []
    execution = agent_pb2.ExecServerMessage(
        id=1,
        exec_id="2",
        mcp_args=agent_pb2.McpArgs(
            name="read_file",
            tool_name="read_file",
            tool_call_id="call-1",
            args={"path": value_bytes("secret.txt")},
        ),
    )

    _server_payload(
        server_payload(agent_pb2.AgentServerMessage(exec_server_message=execution)),
        state=state,
        blob_store={},
        tools=[],
        send_message=sent.append,
        on_text_delta=None,
        on_reasoning_delta=None,
    )

    call = state["tool_calls"][0]
    assert call.id == "call-1"
    assert call.function.name == "read_file"
    assert json.loads(call.function.arguments) == {"path": "secret.txt"}
    assert state["finish_reason"] == "tool_calls"
    assert state["completed"] is True
    assert sent == []


def test_request_context_exposes_only_offered_mcp_tools() -> None:
    state = _new_state()
    sent: list[agent_pb2.AgentClientMessage] = []
    offered = [
        agent_pb2.McpToolDefinition(
            name="read_file",
            tool_name="read_file",
            provider_identifier="hermes-agent",
        )
    ]
    execution = agent_pb2.ExecServerMessage(
        id=3,
        exec_id="4",
        request_context_args=agent_pb2.RequestContextArgs(),
    )

    _server_payload(
        server_payload(agent_pb2.AgentServerMessage(exec_server_message=execution)),
        state=state,
        blob_store={},
        tools=offered,
        send_message=sent.append,
        on_text_delta=None,
        on_reasoning_delta=None,
    )

    context = sent[0].exec_client_message.request_context_result.success.request_context
    assert [tool.name for tool in context.tools] == ["read_file"]
    assert not context.file_contents


def test_grep_exec_request_returns_grep_error_when_native_fs_is_disabled() -> None:
    state = _new_state()
    sent: list[agent_pb2.AgentClientMessage] = []
    execution = agent_pb2.ExecServerMessage(
        id=5,
        exec_id="6",
        grep_args=agent_pb2.GrepArgs(
            pattern="secret",
            path="/tmp/project",
            tool_call_id="grep-1",
        ),
    )

    _server_payload(
        server_payload(agent_pb2.AgentServerMessage(exec_server_message=execution)),
        state=state,
        blob_store={},
        tools=[],
        send_message=sent.append,
        on_text_delta=None,
        on_reasoning_delta=None,
    )

    result = sent[0].exec_client_message.grep_result
    assert result.WhichOneof("result") == "error"
    assert result.error.error == "Native Cursor filesystem tools are disabled"


def test_connect_parser_preserves_partial_frame_and_rejects_oversize() -> None:
    frame = frame_connect_message(b"payload")
    buffer = bytearray(frame[:-2])

    assert list(parse_connect_frames(buffer)) == []
    assert bytes(buffer) == frame[:-2]
    buffer.extend(frame[-2:])
    assert list(parse_connect_frames(buffer)) == [(0, b"payload")]
    assert buffer == bytearray()

    oversized = bytes((0,)) + (100).to_bytes(4, "big")
    with pytest.raises(ConnectProtocolError, match="size limit"):
        list(parse_connect_frames(oversized, max_frame_bytes=10))
