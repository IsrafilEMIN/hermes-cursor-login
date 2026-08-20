from __future__ import annotations

import base64
import json

import pytest
from google.protobuf import json_format, struct_pb2

from hermes_cursor_login.native.proto import agent_pb2
from hermes_cursor_login.native.request_builder import (
    build_mcp_tool_definitions,
    build_run_request,
)


def decode_blob(store: dict[str, bytes], identifier: bytes) -> bytes:
    return store[identifier.hex()]


def test_active_user_message_is_action_and_prior_history_is_replayed() -> None:
    payload, store = build_run_request(
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ],
        system_prompt=["system one", "system two"],
        model_id="cursor-composer-2.5",
        conversation_id="conversation-id",
    )
    envelope = agent_pb2.AgentClientMessage()
    envelope.ParseFromString(payload)
    request = envelope.run_request

    assert request.action.user_message_action.user_message.text == "second"
    assert request.model_details.model_id == "composer-2.5"
    assert request.requested_model.model_id == "composer-2.5"
    root = [
        json.loads(decode_blob(store, identifier))
        for identifier in request.conversation_state.root_prompt_messages_json
    ]
    assert root == [
        {"role": "system", "content": "system one"},
        {"role": "system", "content": "system two"},
        {"role": "user", "content": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
    ]


def test_tool_call_and_result_are_paired_in_conversation_turn() -> None:
    payload, store = build_run_request(
        messages=[
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"a.txt"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "read_file",
                "content": "contents",
            },
        ],
        system_prompt=None,
        model_id="auto",
        conversation_id="conversation-id",
    )
    envelope = agent_pb2.AgentClientMessage()
    envelope.ParseFromString(payload)
    turn = agent_pb2.ConversationTurnStructure()
    turn.ParseFromString(
        decode_blob(store, envelope.run_request.conversation_state.turns[0])
    )
    step = agent_pb2.ConversationStep()
    step.ParseFromString(decode_blob(store, turn.agent_conversation_turn.steps[0]))
    call = step.tool_call.mcp_tool_call
    value = struct_pb2.Value()
    value.ParseFromString(call.args.args["path"])

    assert call.args.tool_call_id == "call-1"
    assert call.args.tool_name == "read_file"
    assert json_format.MessageToDict(value) == "a.txt"
    assert call.result.success.content[0].text.text == "contents"
    assert envelope.run_request.action.WhichOneof("action") == "resume_action"


def test_active_inline_image_is_embedded_without_remote_fetch() -> None:
    image = base64.b64encode(b"png-bytes").decode()
    payload, _ = build_run_request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image}"},
                    },
                ],
            }
        ],
        system_prompt=None,
        model_id="auto",
        conversation_id="conversation-id",
    )
    envelope = agent_pb2.AgentClientMessage()
    envelope.ParseFromString(payload)
    selected = envelope.run_request.action.user_message_action.user_message.selected_context.selected_images

    assert selected[0].mime_type == "image/png"
    assert selected[0].data == b"png-bytes"


def test_remote_image_url_is_rejected_instead_of_fetched() -> None:
    with pytest.raises(ValueError, match="inline base64"):
        build_run_request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/private.png"},
                        }
                    ],
                }
            ],
            system_prompt=None,
            model_id="auto",
            conversation_id="conversation-id",
        )


def test_mcp_tool_schema_round_trips_through_protobuf_value() -> None:
    definitions = build_mcp_tool_definitions(
        [
            {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ]
    )
    value = struct_pb2.Value()
    value.ParseFromString(definitions[0].input_schema)

    assert definitions[0].provider_identifier == "hermes-agent"
    assert json_format.MessageToDict(value)["required"] == ["path"]


def test_tool_result_images_and_errors_preserve_mcp_semantics() -> None:
    image = base64.b64encode(b"tool-image").decode()
    payload, store = build_run_request(
        messages=[
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-image",
                        "type": "function",
                        "function": {"name": "inspect_image", "arguments": "{}"},
                    },
                    {
                        "id": "call-error",
                        "type": "function",
                        "function": {"name": "failing_tool", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-image",
                "content": [
                    {"type": "text", "text": "image"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-error",
                "content": "failed",
                "is_error": True,
            },
        ],
        system_prompt=None,
        model_id="default",
        conversation_id="conversation-id",
    )
    envelope = agent_pb2.AgentClientMessage()
    envelope.ParseFromString(payload)
    turn = agent_pb2.ConversationTurnStructure()
    turn.ParseFromString(
        decode_blob(store, envelope.run_request.conversation_state.turns[0])
    )
    steps = []
    for identifier in turn.agent_conversation_turn.steps:
        step = agent_pb2.ConversationStep()
        step.ParseFromString(decode_blob(store, identifier))
        steps.append(step)

    image_result = steps[0].tool_call.mcp_tool_call.result.success
    error_result = steps[1].tool_call.mcp_tool_call.result.error
    assert image_result.content[0].text.text == "image"
    assert image_result.content[1].image.data == b"tool-image"
    assert image_result.content[1].image.mime_type == "image/png"
    assert error_result.error == "failed"
    root = [
        json.loads(decode_blob(store, identifier))
        for identifier in envelope.run_request.conversation_state.root_prompt_messages_json
    ]
    tool_entries = [entry for entry in root if entry["role"] == "tool"]
    assert [entry["content"][0]["toolName"] for entry in tool_entries] == [
        "inspect_image",
        "failing_tool",
    ]
