from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .credentials import CredentialStore, CursorCredentials

CURSOR_LOGIN_URL = "https://cursor.com/loginDeepControl"
CURSOR_POLL_URL = "https://api2.cursor.sh/auth/poll"
CURSOR_REFRESH_URL = "https://api2.cursor.sh/auth/exchange_user_api_key"
POLL_TIMEOUT_SECONDS = 300.0
POLL_INITIAL_DELAY_SECONDS = 1.0
POLL_MAX_DELAY_SECONDS = 10.0
POLL_BACKOFF_MULTIPLIER = 1.2
REFRESH_SKEW_MS = 5 * 60 * 1000
MAX_RESPONSE_BYTES = 1024 * 1024


class CursorOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class CursorAuthParameters:
    verifier: str
    challenge: str
    uuid: str
    login_url: str


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def generate_cursor_auth_parameters() -> CursorAuthParameters:
    verifier = _base64url(secrets.token_bytes(32))
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    auth_uuid = str(uuid.uuid4())
    query = urlencode(
        {
            "challenge": challenge,
            "uuid": auth_uuid,
            "mode": "login",
            "redirectTarget": "cli",
        }
    )
    return CursorAuthParameters(
        verifier=verifier,
        challenge=challenge,
        uuid=auth_uuid,
        login_url=f"{CURSOR_LOGIN_URL}?{query}",
    )


def _read_json_response(response: Any) -> Any:
    payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise CursorOAuthError("Cursor authentication response exceeded the size limit")
    if not payload:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CursorOAuthError("Cursor authentication returned malformed JSON") from exc


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any]:
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, _read_json_response(response)
    except HTTPError as exc:
        try:
            payload = _read_json_response(exc)
        except CursorOAuthError:
            payload = None
        return exc.code, payload
    except (OSError, URLError) as exc:
        raise CursorOAuthError("Could not reach Cursor authentication service") from exc


def _extract_tokens(
    payload: Any, fallback_refresh_token: str | None = None
) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise CursorOAuthError("Cursor authentication response was not an object")
    access_token = (
        payload.get("accessToken")
        or payload.get("access_token")
        or payload.get("token")
    )
    refresh_token = (
        payload.get("refreshToken")
        or payload.get("refresh_token")
        or fallback_refresh_token
    )
    if not isinstance(access_token, str) or not access_token.strip():
        raise CursorOAuthError(
            "Cursor authentication response did not contain an access token"
        )
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise CursorOAuthError(
            "Cursor authentication response did not contain a refresh token"
        )
    return access_token.strip(), refresh_token.strip()


def _clock_ms(now_ms: int | None) -> int:
    return now_ms if now_ms is not None else int(time.time() * 1000)


def token_expiry_ms(token: str, now_ms: int | None = None) -> int:
    fallback = _clock_ms(now_ms) + 60 * 60 * 1000
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return fallback
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return fallback
    expires = payload.get("exp") if isinstance(payload, dict) else None
    if not isinstance(expires, (int, float)) or expires <= 0:
        return fallback
    return int(expires * 1000)


def poll_cursor_auth(
    auth_uuid: str,
    verifier: str,
    *,
    request_json: Callable[..., tuple[int, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: float = POLL_TIMEOUT_SECONDS,
) -> CursorCredentials:
    request = request_json or _request_json
    deadline = monotonic() + timeout_seconds
    delay = POLL_INITIAL_DELAY_SECONDS
    consecutive_errors = 0
    while monotonic() < deadline:
        query = urlencode({"uuid": auth_uuid, "verifier": verifier})
        try:
            status, payload = request(f"{CURSOR_POLL_URL}?{query}")
        except CursorOAuthError:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                raise CursorOAuthError(
                    "Cursor authentication polling failed repeatedly"
                )
        else:
            if status == 404:
                consecutive_errors = 0
            elif 200 <= status < 300:
                access_token, refresh_token = _extract_tokens(payload)
                return CursorCredentials(
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at_ms=token_expiry_ms(access_token),
                )
            else:
                raise CursorOAuthError(
                    f"Cursor authentication polling failed with HTTP {status}"
                )
        sleep(delay)
        delay = min(delay * POLL_BACKOFF_MULTIPLIER, POLL_MAX_DELAY_SECONDS)
    raise CursorOAuthError(
        "Cursor authentication timed out waiting for browser approval"
    )


def login_cursor(
    *,
    on_auth_url: Callable[[str], None],
    open_browser: bool = True,
    request_json: Callable[..., tuple[int, Any]] | None = None,
) -> CursorCredentials:
    parameters = generate_cursor_auth_parameters()
    on_auth_url(parameters.login_url)
    if open_browser:
        webbrowser.open(parameters.login_url)
    return poll_cursor_auth(
        parameters.uuid,
        parameters.verifier,
        request_json=request_json,
    )


def refresh_cursor_credentials(
    credentials: CursorCredentials,
    *,
    request_json: Callable[..., tuple[int, Any]] | None = None,
) -> CursorCredentials:
    request = request_json or _request_json
    status, payload = request(
        CURSOR_REFRESH_URL,
        method="POST",
        headers={
            "Authorization": f"Bearer {credentials.refresh_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        body=b"{}",
    )
    if not 200 <= status < 300:
        raise CursorOAuthError(f"Cursor token refresh failed with HTTP {status}")
    access_token, refresh_token = _extract_tokens(payload, credentials.refresh_token)
    return CursorCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=token_expiry_ms(access_token),
    )


def resolve_cursor_access_token(
    store: CredentialStore,
    *,
    now_ms: int | None = None,
    request_json: Callable[..., tuple[int, Any]] | None = None,
) -> str:
    current_time = _clock_ms(now_ms)

    def refresh_if_needed(credentials: CursorCredentials) -> CursorCredentials:
        if current_time + REFRESH_SKEW_MS < credentials.expires_at_ms:
            return credentials
        return refresh_cursor_credentials(credentials, request_json=request_json)

    return store.transform(refresh_if_needed).access_token
