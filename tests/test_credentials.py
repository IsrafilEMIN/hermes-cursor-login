from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hermes_cursor_login.credentials import (
    CredentialSecurityError,
    CredentialStore,
    CursorCredentials,
)


def credentials(suffix: str = "a") -> CursorCredentials:
    return CursorCredentials(
        access_token=f"access-{suffix}",
        refresh_token=f"refresh-{suffix}",
        expires_at_ms=2_000_000_000_000,
    )


def test_save_is_private_atomic_and_round_trips(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "hermes")

    store.save(credentials())

    assert store.load() == credentials()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert [
        path
        for path in store.home.glob(".cursor-auth.*")
        if path.name != ".cursor-auth.lock"
    ] == []


def test_transform_serializes_refresh_and_persists_result(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "hermes")
    store.save(credentials("old"))

    updated = store.transform(lambda _: credentials("new"))

    assert updated == credentials("new")
    assert store.load() == credentials("new")


def test_load_rejects_credentials_readable_by_other_users(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "hermes")
    store.save(credentials())
    os.chmod(store.path, 0o644)

    with pytest.raises(CredentialSecurityError, match="0600"):
        store.load()


def test_store_rejects_symlinked_credential_path(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    target = tmp_path / "target"
    target.write_text("secret")
    (home / "cursor-auth.json").symlink_to(target)
    store = CredentialStore(home)

    with pytest.raises(CredentialSecurityError, match="symbolic link"):
        store.load()


def test_delete_removes_only_cursor_credential_file(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "hermes")
    unrelated = store.home / "config.yaml"
    store.home.mkdir(parents=True)
    unrelated.write_text("model: cursor\n")
    store.save(credentials())

    assert store.delete() is True
    assert store.delete() is False
    assert unrelated.read_text() == "model: cursor\n"


def test_store_allows_user_selected_ancestor_alias(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    store = CredentialStore(alias / "hermes")

    store.save(credentials())

    assert store.load() == credentials()
    assert (target / "hermes" / "cursor-auth.json").is_file()
