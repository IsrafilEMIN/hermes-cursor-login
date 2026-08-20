from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .native.model_discovery import fetch_cursor_usable_models
from .native.request_builder import _text
from .native.stream_client import run_cursor_agent_turn
from .profile import FALLBACK_MODELS


@dataclass(frozen=True)
class NativeRunnerConfig:
    token_resolver: Callable[[], str]
    timeout_seconds: float = 1800.0
    max_response_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("Cursor timeout_seconds must be positive")
        if self.max_response_bytes <= 0:
            raise ValueError("Cursor max_response_bytes must be positive")


class NativeCursorRunner:
    def __init__(self, config: NativeRunnerConfig) -> None:
        self.config = config

    def close(self) -> None:
        pass

    def list_models(self) -> list[str]:
        models = fetch_cursor_usable_models(
            api_key=self._access_token(),
            timeout=min(self.config.timeout_seconds, 30.0),
        )
        return models or list(FALLBACK_MODELS)

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
        cancel_event: threading.Event | None = None,
        on_text_delta: Any = None,
        on_reasoning_delta: Any = None,
    ) -> dict[str, Any]:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Cursor request was cancelled")
        system_prompts, conversation = self._split_messages(messages)
        if not conversation:
            raise ValueError("Cursor requires at least one non-system message")
        tool_definitions, required_tool = self._prepare_tools(tools, tool_choice)
        model_id = model or "auto"
        response = run_cursor_agent_turn(
            api_key=self._access_token(),
            model_id=model_id,
            messages=conversation,
            system_prompt=system_prompts,
            tools=tool_definitions,
            conversation_id=str(uuid.uuid4()),
            signal=cancel_event,
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
            timeout_seconds=self.config.timeout_seconds,
            max_response_bytes=self.config.max_response_bytes,
        )
        choice = response.choices[0]
        message = choice.message
        tool_calls = [
            self._tool_call_payload(item) for item in message.tool_calls or []
        ]
        allowed_names = {tool["name"] for tool in tool_definitions}
        unexpected = [
            item["function"]["name"]
            for item in tool_calls
            if item["function"]["name"] not in allowed_names
        ]
        if unexpected:
            raise RuntimeError(
                f"Cursor returned an unoffered tool call: {unexpected[0]}"
            )
        if required_tool == "*" and not tool_calls:
            raise RuntimeError("Cursor did not call a required tool")
        if required_tool not in {None, "*"} and not any(
            item["function"]["name"] == required_tool for item in tool_calls
        ):
            raise RuntimeError(
                f"Cursor did not call the required tool: {required_tool}"
            )
        usage = getattr(response, "usage", None)
        prompt_tokens = self._estimate_prompt_tokens(messages, tools)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return {
            "id": f"chatcmpl-cursor-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": message.content,
                        "reasoning_content": message.reasoning_content,
                        "tool_calls": tool_calls or None,
                    },
                    "finish_reason": "tool_calls"
                    if tool_calls
                    else choice.finish_reason or "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def _access_token(self) -> str:
        token = self.config.token_resolver().strip()
        if not token:
            raise RuntimeError("Cursor access token is missing")
        return token

    @staticmethod
    def _split_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        system_prompts: list[str] = []
        conversation: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") != "system":
                conversation.append(message)
                continue
            text = _text(message.get("content"))
            if text:
                system_prompts.append(text)
        return system_prompts, conversation

    @staticmethod
    def _flatten_tool(tool: dict[str, Any]) -> dict[str, Any]:
        function = tool.get("function") if tool.get("type") == "function" else tool
        if (
            not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not function["name"]
        ):
            raise ValueError("Each Cursor tool must define a non-empty function name")
        parameters = function.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(parameters, dict):
            raise TypeError(f"Cursor tool schema must be an object: {function['name']}")
        return {
            "name": function["name"],
            "description": function.get("description", ""),
            "parameters": parameters,
        }

    @classmethod
    def _prepare_tools(
        cls,
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
    ) -> tuple[list[dict[str, Any]], str | None]:
        flattened = [cls._flatten_tool(tool) for tool in tools or []]
        names = [tool["name"] for tool in flattened]
        if len(names) != len(set(names)):
            raise ValueError("Cursor tool names must be unique")
        if tool_choice is None or tool_choice == "auto":
            return flattened, None
        if tool_choice == "none":
            return [], None
        if tool_choice == "required":
            if not flattened:
                raise ValueError("tool_choice required needs at least one offered tool")
            return flattened, "*"
        if isinstance(tool_choice, dict):
            function = tool_choice.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(name, str) or not name:
                raise ValueError("tool_choice function must name a tool")
            selected = [tool for tool in flattened if tool["name"] == name]
            if not selected:
                raise ValueError(f"Required tool was not offered: {name}")
            return selected, name
        raise ValueError("Unsupported Cursor tool_choice")

    @staticmethod
    def _tool_call_payload(tool_call: Any) -> dict[str, Any]:
        arguments = tool_call.function.arguments
        try:
            parsed = json.loads(arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Cursor returned malformed tool arguments") from exc
        if not isinstance(parsed, dict):
            raise TypeError("Cursor returned non-object tool arguments")
        return {
            "id": tool_call.id,
            "type": "function",
            "function": {
                "name": tool_call.function.name,
                "arguments": json.dumps(
                    parsed, separators=(",", ":"), ensure_ascii=False
                ),
            },
        }

    @staticmethod
    def _estimate_prompt_tokens(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> int:
        encoded = json.dumps(
            {"messages": messages, "tools": tools or []},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return max(1, len(encoded.encode("utf-8")) // 4)
