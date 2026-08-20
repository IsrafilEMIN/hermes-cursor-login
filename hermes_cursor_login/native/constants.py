from __future__ import annotations

from urllib.parse import urlsplit

CURSOR_API_URL = "https://api2.cursor.sh"
CURSOR_CLIENT_VERSION = "cli-2026.07.23-e383d2b"
CURSOR_AGENT_RUN_PATH = "/agent.v1.AgentService/Run"
CURSOR_GET_USABLE_MODELS_PATH = "/agent.v1.AgentService/GetUsableModels"
CONNECT_END_STREAM_FLAG = 0x02


class CursorTransportError(RuntimeError):
    pass


MODEL_ALIASES = {
    "auto": "default",
    "cursor-composer": "composer-2.5",
    "cursor-composer-2.5": "composer-2.5",
}


def normalize_cursor_model_id(model_id: str) -> str:
    normalized = model_id.strip()
    return MODEL_ALIASES.get(normalized, normalized)


def validate_cursor_api_url(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api2.cursor.sh"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Cursor API endpoint must be exactly https://api2.cursor.sh")
    return "api2.cursor.sh", 443
