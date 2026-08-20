from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from hermes_cursor_login.credentials import CredentialStore, CursorCredentials
from hermes_cursor_login.oauth import (
    CURSOR_POLL_URL,
    CURSOR_REFRESH_URL,
    CursorOAuthError,
    generate_cursor_auth_parameters,
    poll_cursor_auth,
    refresh_cursor_credentials,
    resolve_cursor_access_token,
    token_expiry_ms,
)


def jwt(expires: int, subject: str = "user") -> str:
    encode = lambda value: (
        base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    )
    return f"{encode({'alg': 'none'})}.{encode({'exp': expires, 'sub': subject})}.signature"


def test_pkce_login_url_binds_challenge_to_private_verifier() -> None:
    parameters = generate_cursor_auth_parameters()
    query = parse_qs(urlparse(parameters.login_url).query)
    expected = (
        base64.urlsafe_b64encode(
            hashlib.sha256(parameters.verifier.encode("ascii")).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )

    assert parameters.challenge == expected
    assert query == {
        "challenge": [expected],
        "uuid": [parameters.uuid],
        "mode": ["login"],
        "redirectTarget": ["cli"],
    }
    assert parameters.verifier not in parameters.login_url


def test_poll_waits_on_pending_login_then_returns_tokens() -> None:
    access = jwt(2_000_000_000)
    responses = iter(
        [
            (404, None),
            (200, {"accessToken": access, "refreshToken": "refresh-secret"}),
        ]
    )
    urls: list[str] = []
    sleeps: list[float] = []

    def request(url: str, **_: object):
        urls.append(url)
        return next(responses)

    result = poll_cursor_auth(
        "login-id",
        "private-verifier",
        request_json=request,
        sleep=sleeps.append,
        monotonic=lambda: 0,
    )

    assert result == CursorCredentials(access, "refresh-secret", 2_000_000_000_000)
    assert urls[0].startswith(CURSOR_POLL_URL)
    assert "uuid=login-id" in urls[0]
    assert "verifier=private-verifier" in urls[0]
    assert sleeps == [1.0]


def test_poll_fails_closed_after_repeated_transport_errors() -> None:
    def request(*_: object, **__: object):
        raise CursorOAuthError("network")

    with pytest.raises(CursorOAuthError, match="repeatedly"):
        poll_cursor_auth(
            "login-id",
            "private-verifier",
            request_json=request,
            sleep=lambda _: None,
            monotonic=lambda: 0,
        )


def test_refresh_uses_refresh_token_and_retains_it_when_not_rotated() -> None:
    old = CursorCredentials("old-access", "refresh-secret", 1)
    new_access = jwt(2_100_000_000)
    captured: dict[str, object] = {}

    def request(url: str, **kwargs: object):
        captured.update(url=url, **kwargs)
        return 200, {"accessToken": new_access}

    refreshed = refresh_cursor_credentials(old, request_json=request)

    assert refreshed == CursorCredentials(
        new_access, "refresh-secret", 2_100_000_000_000
    )
    assert captured["url"] == CURSOR_REFRESH_URL
    assert captured["method"] == "POST"
    assert captured["body"] == b"{}"
    assert captured["headers"] == {
        "Authorization": "Bearer refresh-secret",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def test_resolve_refreshes_expiring_token_and_persists_rotation(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "hermes")
    store.save(CursorCredentials("old-access", "old-refresh", 1000))
    new_access = jwt(2_200_000_000)

    def request(*_: object, **__: object):
        return 200, {"accessToken": new_access, "refreshToken": "new-refresh"}

    resolved = resolve_cursor_access_token(store, now_ms=1000, request_json=request)

    assert resolved == new_access
    assert store.load() == CursorCredentials(
        new_access, "new-refresh", 2_200_000_000_000
    )


def test_resolve_does_not_contact_refresh_endpoint_for_fresh_token(
    tmp_path: Path,
) -> None:
    store = CredentialStore(tmp_path / "hermes")
    stored = CursorCredentials("fresh-access", "refresh-secret", 2_000_000)
    store.save(stored)

    def request(*_: object, **__: object):
        raise AssertionError("refresh endpoint should not be called")

    assert (
        resolve_cursor_access_token(store, now_ms=1, request_json=request)
        == "fresh-access"
    )
    assert store.load() == stored


def test_non_jwt_token_gets_bounded_fallback_expiry() -> None:
    assert token_expiry_ms("opaque", now_ms=1000) == 3_601_000
