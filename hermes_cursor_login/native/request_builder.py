from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from typing import Any

from google.protobuf import json_format, struct_pb2

from .constants import normalize_cursor_model_id
from .proto import agent_pb2

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _store_blob(store: dict[str, bytes], payload: bytes) -> bytes:
    identifier = hashlib.sha256(payload).digest()
    store[identifier.hex()] = payload
    return identifier


def _stable_uuid(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return (
        f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    )


def _text_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content] if content else []
    if not isinstance(content, list):
        return []
    return [
        str(part["text"])
        for part in content
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
    ]


def _text(content: Any) -> str:
    return "\n".join(_text_parts(content)).strip()


def _decode_image_bytes(encoded: str, label: str) -> bytes:
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"Cursor {label} contains invalid base64 data") from exc
    if len(payload) > MAX_IMAGE_BYTES:
        raise ValueError(f"Cursor {label} exceeded the size limit")
    return payload


def _image_url(part: dict[str, Any]) -> Any:
    image = part.get("image_url")
    return image.get("url") if isinstance(image, dict) else image


def _data_image(part: dict[str, Any]) -> tuple[str, bytes] | None:
    url = _image_url(part)
    if not isinstance(url, str) or not url.startswith("data:") or "," not in url:
        return None
    metadata, encoded = url[5:].split(",", 1)
    fields = metadata.split(";")
    if not fields or fields[-1].lower() != "base64":
        return None
    mime_type = fields[0] or "application/octet-stream"
    return mime_type, _decode_image_bytes(encoded, "image")


def _selected_images(content: Any) -> list[agent_pb2.SelectedImage]:
    if not isinstance(content, list):
        return []
    selected: list[agent_pb2.SelectedImage] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        decoded = _data_image(part)
        if decoded is None:
            raise ValueError("Cursor supports inline base64 image URLs only")
        mime_type, payload = decoded
        selected.append(
            agent_pb2.SelectedImage(
                uuid=str(uuid.uuid4()),
                mime_type=mime_type,
                data=payload,
            )
        )
    return selected


def _root_user_content(content: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = [
        {"type": "text", "text": text} for text in _text_parts(content)
    ]
    if not isinstance(content, list):
        return result
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        url = _image_url(part)
        if isinstance(url, str) and url:
            mime_type = (
                url[5:].split(";", 1)[0]
                if url.startswith("data:")
                else "application/octet-stream"
            )
            result.append({"type": "image", "image": url, "mediaType": mime_type})
    return result


def _assistant_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = [
        {"type": "text", "text": text} for text in _text_parts(message.get("content"))
    ]
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            continue
        function = call["function"]
        arguments = _parse_tool_arguments(function.get("arguments"))
        result.append(
            {
                "type": "tool-call",
                "toolCallId": str(call.get("id") or ""),
                "toolName": str(function.get("name") or ""),
                "args": arguments,
            }
        )
    return result


def _parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Assistant tool-call arguments are malformed") from exc
    elif not arguments:
        arguments = {}
    if not isinstance(arguments, dict):
        raise TypeError("Assistant tool-call arguments must be an object")
    return arguments


def _system_blob_ids(
    system_prompts: list[str] | str | None, store: dict[str, bytes]
) -> list[bytes]:
    prompts = (
        [system_prompts] if isinstance(system_prompts, str) else system_prompts or ()
    )
    normalized = [
        prompt.strip()
        for prompt in prompts
        if isinstance(prompt, str) and prompt.strip()
    ]
    if not normalized:
        normalized = [DEFAULT_SYSTEM_PROMPT]
    return [
        _store_blob(
            store,
            json.dumps(
                {"role": "system", "content": prompt}, separators=(",", ":")
            ).encode(),
        )
        for prompt in normalized
    ]


def _root_prompt_blobs(
    messages: list[dict[str, Any]],
    system_ids: list[bytes],
    store: dict[str, bytes],
    history_end: int,
) -> list[bytes]:
    entries = list(system_ids)
    tool_names: dict[str, str] = {}
    for prior in messages[:history_end]:
        if prior.get("role") != "assistant":
            continue
        for call in prior.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and call.get("id") and function.get("name"):
                tool_names[str(call["id"])] = str(function["name"])
    for message in messages[:history_end]:
        role = message.get("role")
        payload: dict[str, Any] | None = None
        if role in {"user", "developer"}:
            content = _root_user_content(message.get("content"))
            if content:
                payload = {"role": "user", "content": content}
        elif role == "assistant":
            content = _assistant_content(message)
            if content:
                payload = {"role": "assistant", "content": content}
        elif role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "")
            payload = {
                "role": "tool",
                "id": tool_call_id,
                "content": [
                    {
                        "type": "tool-result",
                        "toolName": str(
                            message.get("name") or tool_names.get(tool_call_id, "")
                        ),
                        "toolCallId": tool_call_id,
                        "result": _text(message.get("content")),
                    }
                ],
            }
        if payload is not None:
            entries.append(
                _store_blob(
                    store,
                    json.dumps(
                        payload, separators=(",", ":"), ensure_ascii=False
                    ).encode(),
                )
            )
    return entries


def _value_bytes(value: Any) -> bytes:
    protobuf_value = struct_pb2.Value()
    json_format.ParseDict(value, protobuf_value)
    return protobuf_value.SerializeToString()


def _mcp_result(message: dict[str, Any]) -> agent_pb2.McpToolResult:
    text = _text(message.get("content"))
    if message.get("is_error"):
        return agent_pb2.McpToolResult(error=agent_pb2.McpToolError(error=text))
    items: list[agent_pb2.McpToolResultContentItem] = []
    content = message.get("content")
    if isinstance(content, str):
        items.append(
            agent_pb2.McpToolResultContentItem(
                text=agent_pb2.McpTextContent(text=content)
            )
        )
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                items.append(
                    agent_pb2.McpToolResultContentItem(
                        text=agent_pb2.McpTextContent(text=str(part.get("text") or ""))
                    )
                )
            elif part.get("type") == "image_url":
                decoded = _data_image(part)
                if decoded is None:
                    raise ValueError(
                        "Cursor supports inline base64 tool-result images only"
                    )
                mime_type, payload = decoded
                items.append(
                    agent_pb2.McpToolResultContentItem(
                        image=agent_pb2.McpImageContent(
                            data=payload, mime_type=mime_type
                        )
                    )
                )
            elif part.get("type") == "image" and isinstance(part.get("data"), str):
                payload = _decode_image_bytes(part["data"], "tool-result image")
                items.append(
                    agent_pb2.McpToolResultContentItem(
                        image=agent_pb2.McpImageContent(
                            data=payload,
                            mime_type=str(
                                part.get("mime_type")
                                or part.get("mimeType")
                                or "application/octet-stream"
                            ),
                        )
                    )
                )
    if not items:
        items.append(
            agent_pb2.McpToolResultContentItem(text=agent_pb2.McpTextContent(text=text))
        )
    return agent_pb2.McpToolResult(success=agent_pb2.McpSuccess(content=items))


def _tool_step(
    call: dict[str, Any], result: dict[str, Any] | None
) -> agent_pb2.ConversationStep:
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        raise TypeError("Assistant tool call is malformed")
    call_id = str(call.get("id") or "")
    name = str(function.get("name") or "")
    if not call_id or not name:
        raise ValueError("Assistant tool call is missing an id or name")
    arguments = _parse_tool_arguments(function.get("arguments"))
    mcp = agent_pb2.McpToolCall(
        args=agent_pb2.McpArgs(
            name=name,
            args={key: _value_bytes(value) for key, value in arguments.items()},
            tool_call_id=call_id,
            provider_identifier="hermes-agent",
            tool_name=name,
        )
    )
    if result is not None:
        mcp.result.CopyFrom(_mcp_result(result))
    return agent_pb2.ConversationStep(tool_call=agent_pb2.ToolCall(mcp_tool_call=mcp))


def _conversation_turns(
    messages: list[dict[str, Any]],
    store: dict[str, bytes],
    history_end: int,
) -> list[bytes]:
    results = {
        str(message.get("tool_call_id")): message
        for message in messages[:history_end]
        if message.get("role") == "tool" and message.get("tool_call_id")
    }
    turns: list[bytes] = []
    index = 0
    while index < history_end:
        message = messages[index]
        if message.get("role") not in {"user", "developer"}:
            index += 1
            continue
        text = _text(message.get("content"))
        images = _selected_images(message.get("content"))
        if not text and not images:
            index += 1
            continue
        turn_number = len(turns)
        user = agent_pb2.UserMessage(
            text=text,
            message_id=_stable_uuid(f"user:{turn_number}:{text}"),
        )
        if images:
            user.selected_context.CopyFrom(
                agent_pb2.SelectedContext(selected_images=images)
            )
        user_blob = _store_blob(store, user.SerializeToString())
        step_blobs: list[bytes] = []
        index += 1
        while index < history_end and messages[index].get("role") not in {
            "user",
            "developer",
        }:
            step_message = messages[index]
            if step_message.get("role") == "assistant":
                assistant_text = _text(step_message.get("content"))
                if assistant_text:
                    step = agent_pb2.ConversationStep(
                        assistant_message=agent_pb2.AssistantMessage(
                            text=assistant_text
                        )
                    )
                    step_blobs.append(_store_blob(store, step.SerializeToString()))
                for call in step_message.get("tool_calls") or []:
                    result = (
                        results.get(str(call.get("id")))
                        if isinstance(call, dict)
                        else None
                    )
                    step_blobs.append(
                        _store_blob(store, _tool_step(call, result).SerializeToString())
                    )
            index += 1
        turn = agent_pb2.ConversationTurnStructure(
            agent_conversation_turn=agent_pb2.AgentConversationTurnStructure(
                user_message=user_blob,
                steps=step_blobs,
            )
        )
        turns.append(_store_blob(store, turn.SerializeToString()))
    return turns


def build_mcp_tool_definitions(
    tools: list[dict[str, Any]] | None,
) -> list[agent_pb2.McpToolDefinition]:
    definitions: list[agent_pb2.McpToolDefinition] = []
    for tool in tools or []:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Cursor tool definition is missing a name")
        schema = tool.get("parameters") or {"type": "object", "properties": {}}
        value = struct_pb2.Value()
        json_format.ParseDict(schema, value)
        definitions.append(
            agent_pb2.McpToolDefinition(
                name=name,
                description=str(tool.get("description") or ""),
                provider_identifier="hermes-agent",
                tool_name=name,
                input_schema=value.SerializeToString(),
            )
        )
    return definitions


def build_run_request(
    *,
    messages: list[dict[str, Any]],
    system_prompt: list[str] | str | None,
    model_id: str,
    conversation_id: str,
) -> tuple[bytes, dict[str, bytes]]:
    wire_model = normalize_cursor_model_id(model_id)
    store: dict[str, bytes] = {}
    system_ids = _system_blob_ids(system_prompt, store)
    active_index = len(messages) - 1
    active = messages[active_index] if messages else None
    active_user = (
        active if active and active.get("role") in {"user", "developer"} else None
    )
    history_end = active_index if active_user is not None else len(messages)
    root_prompt = _root_prompt_blobs(messages, system_ids, store, history_end)
    turns = _conversation_turns(messages, store, history_end)
    state = agent_pb2.ConversationStateStructure(
        root_prompt_messages_json=root_prompt,
        turns=turns,
    )
    if active_user is not None:
        text = _text(active_user.get("content"))
        images = _selected_images(active_user.get("content"))
        if not text and not images:
            raise ValueError("Active Cursor user message is empty")
        user = agent_pb2.UserMessage(text=text, message_id=str(uuid.uuid4()))
        if images:
            user.selected_context.CopyFrom(
                agent_pb2.SelectedContext(selected_images=images)
            )
        action = agent_pb2.ConversationAction(
            user_message_action=agent_pb2.UserMessageAction(user_message=user)
        )
    else:
        action = agent_pb2.ConversationAction(resume_action=agent_pb2.ResumeAction())
    request = agent_pb2.AgentRunRequest(
        conversation_state=state,
        action=action,
        model_details=agent_pb2.ModelDetails(
            model_id=wire_model,
            display_model_id=wire_model,
            display_name=wire_model,
        ),
        requested_model=agent_pb2.RequestedModel(model_id=wire_model),
        conversation_id=conversation_id,
    )
    client_message = agent_pb2.AgentClientMessage(run_request=request)
    return client_message.SerializeToString(), store
