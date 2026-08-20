from __future__ import annotations

import json
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import h2.events
from google.protobuf import json_format, struct_pb2
from google.protobuf.message import DecodeError

from .connect_framing import (
    frame_connect_message,
    parse_connect_end_stream,
    parse_connect_frames,
)
from .constants import (
    CONNECT_END_STREAM_FLAG,
    CURSOR_AGENT_RUN_PATH,
    CURSOR_API_URL,
    CursorTransportError,
    normalize_cursor_model_id,
    validate_cursor_api_url,
)
from .http2 import close_h2, cursor_connect_headers, open_cursor_h2, send_h2
from .proto import agent_pb2
from .request_builder import build_mcp_tool_definitions, build_run_request


def _protobuf_value(payload: bytes) -> Any:
    value = struct_pb2.Value()
    try:
        value.ParseFromString(payload)
        return json_format.MessageToDict(value)
    except DecodeError:
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return payload.decode("utf-8", errors="replace")


def _mcp_arguments(arguments: agent_pb2.McpArgs) -> dict[str, Any]:
    return {name: _protobuf_value(payload) for name, payload in arguments.args.items()}


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments, separators=(",", ":"), ensure_ascii=False),
        ),
    )


def _append_mcp_call(
    state: dict[str, Any], call_id: str, name: str, arguments: dict[str, Any]
) -> None:
    if not call_id:
        call_id = str(uuid.uuid4())
    if call_id in state["tool_call_ids"]:
        return
    if not name:
        raise RuntimeError("Cursor returned an MCP tool call without a name")
    state["tool_call_ids"].add(call_id)
    state["tool_calls"].append(_tool_call(call_id, name, arguments))
    state["finish_reason"] = "tool_calls"
    state["completed"] = True


def _request_context(
    tools: list[agent_pb2.McpToolDefinition],
) -> agent_pb2.RequestContextResult:
    return agent_pb2.RequestContextResult(
        success=agent_pb2.RequestContextSuccess(
            request_context=agent_pb2.RequestContext(
                rules=[],
                repository_info=[],
                tools=tools,
                git_repos=[],
                project_layouts=[],
                mcp_instructions=[],
                file_contents={},
                custom_subagents=[],
            )
        )
    )


def _reject(result: Any, reason: str, **fields: Any) -> None:
    for name, value in fields.items():
        setattr(result.rejected, name, value)
    result.rejected.reason = reason


def _error(target: Any, message: str, **fields: Any) -> None:
    if hasattr(target, "error") and hasattr(target.error, "error"):
        target.error.error = message
        for name, value in fields.items():
            setattr(target.error, name, value)
        return
    _reject(target, message, **fields)


def _exec_response(
    message: agent_pb2.ExecServerMessage,
    tools: list[agent_pb2.McpToolDefinition],
) -> agent_pb2.ExecClientMessage:
    response = agent_pb2.ExecClientMessage(id=message.id, exec_id=message.exec_id)
    disabled_fs = "Native Cursor filesystem tools are disabled"
    disabled_shell = "Native Cursor shell tools are disabled"
    case = message.WhichOneof("message")
    if case == "request_context_args":
        response.request_context_result.CopyFrom(_request_context(tools))
    elif case == "read_args":
        _reject(response.read_result, disabled_fs, path=message.read_args.path)
    elif case == "write_args":
        _reject(response.write_result, disabled_fs, path=message.write_args.path)
    elif case == "delete_args":
        _reject(response.delete_result, disabled_fs, path=message.delete_args.path)
    elif case == "diagnostics_args":
        _reject(
            response.diagnostics_result, disabled_fs, path=message.diagnostics_args.path
        )
    elif case == "shell_args":
        _reject(
            response.shell_result,
            disabled_shell,
            command=message.shell_args.command,
            working_directory=message.shell_args.working_directory,
            is_readonly=False,
        )
    elif case == "ls_args":
        _reject(response.ls_result, disabled_fs, path=message.ls_args.path)
    elif case == "grep_args":
        _error(response.grep_result, disabled_fs)
    elif case == "mcp_args":
        _reject(response.mcp_result, "MCP call returned to Hermes", is_readonly=False)
    elif case == "list_mcp_resources_exec_args":
        response.list_mcp_resources_exec_result.success.CopyFrom(
            agent_pb2.ListMcpResourcesSuccess(resources=[])
        )
    elif case == "read_mcp_resource_exec_args":
        response.read_mcp_resource_exec_result.not_found.uri = (
            message.read_mcp_resource_exec_args.uri
        )
    elif case == "write_shell_stdin_args":
        _error(response.write_shell_stdin_result, disabled_shell)
    elif case == "fetch_args":
        _error(
            response.fetch_result,
            "Native Cursor fetch tools are disabled",
            url=message.fetch_args.url,
        )
    elif case == "record_screen_args":
        response.record_screen_result.failure.error = (
            "Native Cursor screen tools are disabled"
        )
    elif case == "computer_use_args":
        _error(
            response.computer_use_result, "Native Cursor computer tools are disabled"
        )
    return response


def _kv_response(
    message: agent_pb2.KvServerMessage,
    blob_store: dict[str, bytes],
) -> agent_pb2.KvClientMessage:
    response = agent_pb2.KvClientMessage(id=message.id)
    message_case = message.WhichOneof("message")
    if message_case == "get_blob_args":
        payload = blob_store.get(message.get_blob_args.blob_id.hex())
        if payload is not None:
            response.get_blob_result.blob_data = payload
    elif message_case == "set_blob_args":
        blob_store[message.set_blob_args.blob_id.hex()] = (
            message.set_blob_args.blob_data
        )
        response.set_blob_result.CopyFrom(agent_pb2.SetBlobResult())
    return response


def _new_state() -> dict[str, Any]:
    return {
        "content": [],
        "reasoning": [],
        "tool_calls": [],
        "tool_call_ids": set(),
        "current_tool_call": None,
        "completion_tokens": 0,
        "finish_reason": "stop",
        "completed": False,
    }


def _interaction_update(
    update: agent_pb2.InteractionUpdate,
    state: dict[str, Any],
    on_text_delta: Any,
    on_reasoning_delta: Any,
) -> None:
    update_case = update.WhichOneof("message")
    if update_case == "text_delta":
        state["content"].append(update.text_delta.text)
        if on_text_delta is not None:
            on_text_delta(update.text_delta.text)
    elif update_case == "thinking_delta":
        state["reasoning"].append(update.thinking_delta.text)
        if on_reasoning_delta is not None:
            on_reasoning_delta(update.thinking_delta.text)
    elif update_case == "token_delta":
        state["completion_tokens"] += max(0, update.token_delta.tokens)
    elif update_case == "tool_call_started":
        started = update.tool_call_started
        if started.tool_call.WhichOneof("tool") == "mcp_tool_call":
            arguments = started.tool_call.mcp_tool_call.args
            state["current_tool_call"] = {
                "id": arguments.tool_call_id or started.call_id,
                "name": arguments.tool_name or arguments.name,
                "arguments": _mcp_arguments(arguments),
                "partial": "",
            }
    elif update_case == "partial_tool_call":
        current = state.get("current_tool_call")
        if current is not None:
            current["partial"] += update.partial_tool_call.args_text_delta
            try:
                parsed = json.loads(current["partial"])
                if isinstance(parsed, dict):
                    current["arguments"] = parsed
            except json.JSONDecodeError:
                pass
    elif update_case == "tool_call_completed":
        completed = update.tool_call_completed
        if completed.tool_call.WhichOneof("tool") == "mcp_tool_call":
            arguments = completed.tool_call.mcp_tool_call.args
            current = state.get("current_tool_call") or {}
            _append_mcp_call(
                state,
                arguments.tool_call_id
                or completed.call_id
                or str(current.get("id") or ""),
                arguments.tool_name or arguments.name or str(current.get("name") or ""),
                _mcp_arguments(arguments) or dict(current.get("arguments") or {}),
            )
            state["current_tool_call"] = None
    elif update_case == "turn_ended":
        state["completed"] = True


def _server_payload(
    payload: bytes,
    *,
    state: dict[str, Any],
    blob_store: dict[str, bytes],
    tools: list[agent_pb2.McpToolDefinition],
    send_message: Any,
    on_text_delta: Any,
    on_reasoning_delta: Any,
) -> None:
    message = agent_pb2.AgentServerMessage()
    message.ParseFromString(payload)
    message_case = message.WhichOneof("message")
    if message_case == "interaction_update":
        _interaction_update(
            message.interaction_update, state, on_text_delta, on_reasoning_delta
        )
    elif message_case == "kv_server_message":
        send_message(
            agent_pb2.AgentClientMessage(
                kv_client_message=_kv_response(message.kv_server_message, blob_store)
            )
        )
    elif message_case == "exec_server_message":
        execution = message.exec_server_message
        if execution.WhichOneof("message") == "mcp_args":
            arguments = execution.mcp_args
            _append_mcp_call(
                state,
                arguments.tool_call_id or str(execution.exec_id or execution.id),
                arguments.tool_name or arguments.name,
                _mcp_arguments(arguments),
            )
        else:
            send_message(
                agent_pb2.AgentClientMessage(
                    exec_client_message=_exec_response(execution, tools)
                )
            )
    elif message_case == "interaction_query":
        raise RuntimeError("Cursor requested an unsupported native interaction")


def _response(state: dict[str, Any], model_id: str) -> SimpleNamespace:
    message = SimpleNamespace(
        role="assistant",
        content="".join(state["content"]) or None,
        reasoning_content="".join(state["reasoning"]) or None,
        tool_calls=state["tool_calls"] or None,
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                message=message,
                finish_reason=state["finish_reason"],
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=state["completion_tokens"],
            total_tokens=state["completion_tokens"],
        ),
        model=model_id,
    )


def run_cursor_agent_turn(
    *,
    api_key: str,
    model_id: str,
    messages: list[dict[str, Any]],
    system_prompt: list[str] | str | None,
    tools: list[dict[str, Any]] | None,
    conversation_id: str,
    signal: threading.Event | None = None,
    on_text_delta: Any = None,
    on_reasoning_delta: Any = None,
    timeout_seconds: float = 1800.0,
    max_response_bytes: int = 16 * 1024 * 1024,
    base_url: str = CURSOR_API_URL,
) -> SimpleNamespace:
    token = api_key.strip()
    if not token:
        raise ValueError("Cursor access token is required")
    if timeout_seconds <= 0 or max_response_bytes <= 0:
        raise ValueError("Cursor transport limits must be positive")
    host, port = validate_cursor_api_url(base_url)
    wire_model = normalize_cursor_model_id(model_id)
    request_payload, blob_store = build_run_request(
        messages=messages,
        system_prompt=system_prompt,
        model_id=wire_model,
        conversation_id=conversation_id,
    )
    request_tools = build_mcp_tool_definitions(tools)
    send_lock = threading.RLock()
    pending = bytearray()
    heartbeat_stop = threading.Event()
    deadline = time.monotonic() + timeout_seconds
    stream_id = 0
    network_socket = None
    tls_socket = None
    connection = None
    heartbeat_thread: threading.Thread | None = None

    def flush_locked() -> None:
        if tls_socket is None or connection is None or stream_id == 0:
            return
        while pending:
            window = connection.local_flow_control_window(stream_id)
            size = min(len(pending), window, connection.max_outbound_frame_size)
            if size <= 0:
                break
            chunk = bytes(pending[:size])
            del pending[:size]
            connection.send_data(stream_id, chunk, end_stream=False)
        send_h2(tls_socket, connection)

    def send_frame(frame: bytes) -> None:
        with send_lock:
            pending.extend(frame)
            flush_locked()

    def send_message(message: agent_pb2.AgentClientMessage) -> None:
        send_frame(frame_connect_message(message.SerializeToString()))

    def heartbeat() -> None:
        while not heartbeat_stop.wait(5.0):
            if signal is not None and signal.is_set():
                return
            if time.monotonic() >= deadline:
                return
            try:
                send_message(
                    agent_pb2.AgentClientMessage(
                        client_heartbeat=agent_pb2.ClientHeartbeat()
                    )
                )
            except (OSError, RuntimeError):
                return

    response_status: str | None = None
    grpc_status: str | None = None
    grpc_message = ""
    received = 0
    buffer = bytearray()
    state = _new_state()
    stream_ended = False

    try:
        network_socket, tls_socket, connection = open_cursor_h2(
            host,
            port,
            connect_timeout=min(30.0, timeout_seconds),
            io_timeout=min(1.0, timeout_seconds),
        )
        stream_id = connection.get_next_available_stream_id()
        with send_lock:
            connection.send_headers(
                stream_id,
                cursor_connect_headers(
                    path=CURSOR_AGENT_RUN_PATH,
                    host=host,
                    token=token,
                    content_type="application/connect+proto",
                ),
                end_stream=False,
            )
            pending.extend(frame_connect_message(request_payload))
            flush_locked()
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()

        while not stream_ended and not state["completed"]:
            if signal is not None and signal.is_set():
                raise InterruptedError("Cursor request was cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("Cursor request timed out")
            try:
                data = tls_socket.recv(65535)
            except TimeoutError:
                continue
            if not data:
                break
            with send_lock:
                events = connection.receive_data(data)
                send_h2(tls_socket, connection)
            for event in events:
                if isinstance(event, h2.events.ResponseReceived):
                    response_status = str(dict(event.headers).get(":status", ""))
                elif isinstance(event, h2.events.TrailersReceived):
                    trailers = dict(event.headers)
                    grpc_status = str(trailers.get("grpc-status", "") or "")
                    grpc_message = str(trailers.get("grpc-message", "") or "")
                elif isinstance(event, h2.events.DataReceived):
                    received += len(event.data)
                    if received > max_response_bytes:
                        raise RuntimeError("Cursor response exceeded the size limit")
                    with send_lock:
                        connection.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id
                        )
                        flush_locked()
                    buffer.extend(event.data)
                    for flags, payload in parse_connect_frames(
                        buffer, max_frame_bytes=max_response_bytes
                    ):
                        if flags & CONNECT_END_STREAM_FLAG:
                            error = parse_connect_end_stream(payload)
                            if error is not None and not state["completed"]:
                                raise error
                        else:
                            _server_payload(
                                payload,
                                state=state,
                                blob_store=blob_store,
                                tools=request_tools,
                                send_message=send_message,
                                on_text_delta=on_text_delta,
                                on_reasoning_delta=on_reasoning_delta,
                            )
                        if state["completed"]:
                            break
                elif isinstance(event, h2.events.WindowUpdated):
                    with send_lock:
                        flush_locked()
                elif isinstance(event, h2.events.StreamReset):
                    raise CursorTransportError(
                        f"Cursor reset the HTTP/2 stream: {event.error_code}"
                    )
                elif isinstance(event, h2.events.ConnectionTerminated):
                    raise CursorTransportError(
                        f"Cursor terminated the HTTP/2 connection: {event.error_code}"
                    )
                elif isinstance(event, h2.events.StreamEnded):
                    stream_ended = True

        if response_status and response_status != "200":
            raise RuntimeError(f"Cursor request failed with HTTP {response_status}")
        if grpc_status and grpc_status != "0":
            raise RuntimeError(
                f"Cursor gRPC error {grpc_status}: {unquote(grpc_message)}"
            )
        if not state["completed"]:
            raise RuntimeError("Cursor stream ended before completing the turn")
        return _response(state, wire_model)
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        close_h2(tls_socket, network_socket)
