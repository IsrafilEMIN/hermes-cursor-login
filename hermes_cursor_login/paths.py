from __future__ import annotations

import os
import tempfile
from pathlib import Path


def reject_symlink_chain(
    path: Path,
    boundary: Path | None = None,
    *,
    error: type[Exception] = ValueError,
    link_message: str = "Refusing managed path containing a symbolic link: {path}",
    escape_message: str = "Managed path escapes its Hermes home: {path}",
) -> None:
    stop = boundary or path
    current = path
    while True:
        if current.is_symlink():
            raise error(link_message.format(path=current))
        if current == stop:
            return
        if stop not in current.parents:
            raise error(escape_message.format(path=path))
        current = current.parent


def chmod(target: int | str | Path, mode: int) -> None:
    try:
        if isinstance(target, int):
            os.fchmod(target, mode)
        else:
            os.chmod(target, mode)
    except OSError:
        if os.name != "nt":
            raise


def sync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError:
        if os.name != "nt":
            raise
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: str, mode: int) -> None:
    reject_symlink_chain(path, path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        chmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        chmod(path, mode)
        sync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def dotenv_get(text: str, key: str) -> str:
    prefix = f"{key}="
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if not line.startswith(prefix):
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if any(character in value for character in "\r\n\x00"):
            return ""
        return value
    return ""


def dotenv_set(text: str, key: str, value: str) -> str:
    replacement = f"{key}={value}"
    prefix = f"{key}="
    seen = False
    lines: list[str] = []
    for line in text.splitlines():
        if line.strip().removeprefix("export ").startswith(prefix):
            if seen:
                continue
            lines.append(replacement)
            seen = True
        else:
            lines.append(line)
    if not seen:
        lines.append(replacement)
    return "\n".join(lines).rstrip() + "\n"


def read_dotenv_value(path: Path, key: str) -> str:
    try:
        return dotenv_get(path.read_text(encoding="utf-8"), key)
    except (FileNotFoundError, OSError, UnicodeError):
        return ""
