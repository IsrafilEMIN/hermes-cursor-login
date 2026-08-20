from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .paths import atomic_write, chmod, reject_symlink_chain, sync_directory

CREDENTIAL_VERSION = 1
CREDENTIAL_FILE = "cursor-auth.json"
LOCK_FILE = ".cursor-auth.lock"


class CredentialError(RuntimeError):
    pass


class CredentialSecurityError(CredentialError):
    pass


@dataclass(frozen=True)
class CursorCredentials:
    access_token: str
    refresh_token: str
    expires_at_ms: int

    def __post_init__(self) -> None:
        _validate_token(self.access_token, "access token")
        _validate_token(self.refresh_token, "refresh token")
        if self.expires_at_ms <= 0:
            raise CredentialError("Cursor credential expiry must be positive")


def _validate_token(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CredentialError(f"Cursor {label} is missing or malformed")
    if len(value) > 131072 or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise CredentialError(f"Cursor {label} contains invalid bytes")


def _guard(path: Path, boundary: Path | None = None) -> None:
    reject_symlink_chain(
        path,
        boundary,
        error=CredentialSecurityError,
        link_message="Refusing Cursor credential path containing a symbolic link: {path}",
        escape_message="Cursor credential path escapes its managed home: {path}",
    )


class CredentialStore:
    def __init__(self, hermes_home: str | Path) -> None:
        self.home = Path(hermes_home).expanduser()
        self.path = self.home / CREDENTIAL_FILE
        self.lock_path = self.home / LOCK_FILE

    def load(self) -> CursorCredentials | None:
        with self._lock():
            return self._load_unlocked()

    def save(self, credentials: CursorCredentials) -> None:
        with self._lock():
            self._write_unlocked(credentials)

    def delete(self) -> bool:
        with self._lock():
            if not self.path.exists():
                return False
            if self.path.is_symlink():
                raise CredentialSecurityError(
                    f"Refusing to remove symbolic-link credential path: {self.path}"
                )
            self.path.unlink()
            sync_directory(self.home)
            return True

    def transform(
        self, callback: Callable[[CursorCredentials], CursorCredentials]
    ) -> CursorCredentials:
        with self._lock():
            current = self._load_unlocked()
            if current is None:
                raise CredentialError(
                    "Cursor is not logged in; run `hermes-cursor-login login`"
                )
            updated = callback(current)
            if updated != current:
                self._write_unlocked(updated)
            return updated

    @contextmanager
    def _lock(self) -> Iterator[None]:
        _guard(self.home)
        self.home.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.home, 0o700)
        _guard(self.path, self.home)
        _guard(self.lock_path, self.home)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            chmod(descriptor, 0o600)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            _lock_fd(descriptor, exclusive=True)
            yield
        finally:
            _lock_fd(descriptor, exclusive=False)
            os.close(descriptor)

    def _load_unlocked(self) -> CursorCredentials | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink():
            raise CredentialSecurityError(
                f"Refusing symbolic-link credential path: {self.path}"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise CredentialSecurityError(
                    f"Cursor credential path is not a regular file: {self.path}"
                )
            if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
                raise CredentialSecurityError(
                    f"Cursor credential file must have mode 0600: {self.path}"
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CredentialError(f"Could not read Cursor credentials: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return self._decode(payload)

    def _decode(self, payload: Any) -> CursorCredentials:
        if (
            not isinstance(payload, dict)
            or payload.get("version") != CREDENTIAL_VERSION
        ):
            raise CredentialError("Cursor credential file has an unsupported format")
        try:
            return CursorCredentials(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                expires_at_ms=int(payload["expires_at_ms"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CredentialError("Cursor credential file is incomplete") from exc

    def _write_unlocked(self, credentials: CursorCredentials) -> None:
        _guard(self.path, self.home)
        payload = {"version": CREDENTIAL_VERSION, **asdict(credentials)}
        atomic_write(
            self.path,
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
            0o600,
        )


def _lock_fd(descriptor: int, *, exclusive: bool) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK if exclusive else msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_UN)
