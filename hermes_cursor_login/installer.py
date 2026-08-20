from __future__ import annotations

import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import atomic_write, dotenv_get, dotenv_set, reject_symlink_chain
from .profile import DEFAULT_BRIDGE_BASE_URL, FALLBACK_MODELS

ENV_KEY = "CURSOR_BRIDGE_API_KEY"
PLUGIN_INIT = f'''from providers import register_provider
from providers.base import ProviderProfile

cursor = ProviderProfile(
    name="cursor",
    aliases=("cursor-subscription", "cursor-native"),
    display_name="Cursor",
    description="Cursor subscription through a native authenticated bridge",
    signup_url="https://cursor.com",
    api_mode="chat_completions",
    env_vars=("{ENV_KEY}",),
    base_url={DEFAULT_BRIDGE_BASE_URL!r},
    auth_type="api_key",
    fallback_models={FALLBACK_MODELS!r},
    supports_health_check=True,
)

register_provider(cursor)
'''
PLUGIN_MANIFEST = """name: cursor
kind: model-provider
version: 0.1.0
description: Native Cursor subscription provider through an authenticated loopback bridge
"""
PLUGIN_FILES = {"__init__.py": PLUGIN_INIT, "plugin.yaml": PLUGIN_MANIFEST}
MODEL_CONFIG = {
    "provider": "cursor",
    "default": "default",
    "base_url": DEFAULT_BRIDGE_BASE_URL,
}


@dataclass(frozen=True)
class InstallResult:
    plugin_dir: Path
    env_path: Path
    backup_dir: Path | None


def validate_bridge_token(value: str) -> str:
    token = value.strip()
    if len(token) < 43 or len(set(token)) < 12:
        raise ValueError(
            "Bridge token must contain at least 256 bits of generated entropy"
        )
    if token != value or any(
        ord(character) < 33 or ord(character) == 127 for character in token
    ):
        raise ValueError("Bridge token must be a printable single-line value")
    return token


def _plugin_conflicts(plugin_dir: Path) -> bool:
    if not plugin_dir.exists():
        return False
    if not plugin_dir.is_dir():
        return True
    entries: set[str] = set()
    for path in plugin_dir.iterdir():
        if path.is_symlink():
            raise ValueError(f"Refusing symbolic-link plugin entry: {path}")
        if path.name != "__pycache__":
            entries.add(path.name)
    return entries != set(PLUGIN_FILES) or any(
        (plugin_dir / name).read_text(encoding="utf-8") != content
        for name, content in PLUGIN_FILES.items()
    )


def install_plugin(hermes_home: str | Path, *, force: bool = False) -> InstallResult:
    home = Path(hermes_home).expanduser()
    plugin_dir = home / "plugins" / "model-providers" / "cursor"
    env_path = home / ".env"
    reject_symlink_chain(home)
    reject_symlink_chain(plugin_dir, home)
    reject_symlink_chain(env_path, home)
    backup_dir: Path | None = None
    if _plugin_conflicts(plugin_dir):
        if not force:
            raise FileExistsError(
                f"A conflicting Cursor provider already exists: {plugin_dir}"
            )
        backup_dir = plugin_dir.with_name(
            f"cursor.backup-{int(time.time())}-{secrets.token_hex(4)}"
        )
        shutil.move(plugin_dir, backup_dir)
    plugin_dir.mkdir(parents=True, exist_ok=True)
    for name, content in PLUGIN_FILES.items():
        atomic_write(plugin_dir / name, content, 0o644)
    try:
        env_text = env_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        env_text = ""
    existing = dotenv_get(env_text, ENV_KEY)
    token = validate_bridge_token(existing) if existing else secrets.token_urlsafe(32)
    atomic_write(env_path, dotenv_set(env_text, ENV_KEY, token), 0o600)
    sync_hermes_model_config(home)
    return InstallResult(
        plugin_dir=plugin_dir, env_path=env_path, backup_dir=backup_dir
    )


def uninstall_plugin(hermes_home: str | Path, *, force: bool = False) -> None:
    home = Path(hermes_home).expanduser()
    plugin_dir = home / "plugins" / "model-providers" / "cursor"
    reject_symlink_chain(home)
    reject_symlink_chain(plugin_dir, home)
    if not plugin_dir.exists():
        return
    for name, content in PLUGIN_FILES.items():
        path = plugin_dir / name
        if not path.exists():
            continue
        if path.is_symlink():
            raise ValueError(f"Refusing symbolic-link plugin file: {path}")
        if not force and path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"Refusing to remove modified plugin file: {path}")
        path.unlink()
    cache = plugin_dir / "__pycache__"
    if cache.exists() and cache.is_dir() and not cache.is_symlink():
        shutil.rmtree(cache)
    try:
        plugin_dir.rmdir()
    except OSError:
        if force:
            shutil.rmtree(plugin_dir)
        else:
            raise FileExistsError(
                f"Cursor plugin directory contains unowned files: {plugin_dir}"
            )


def upsert_model_config(text: str) -> str:
    wanted = MODEL_CONFIG
    lines = text.splitlines()
    model_idx = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("model:") and not line[:1].isspace()
        ),
        None,
    )
    if model_idx is None:
        block = ["model:", *[f"  {key}: {value}" for key, value in wanted.items()]]
        body = "\n".join(lines).rstrip()
        extra = "\n".join(block) + "\n"
        return extra if not body else body + "\n\n" + extra
    end = len(lines)
    for index in range(model_idx + 1, len(lines)):
        line = lines[index]
        if line and not line[0].isspace() and not line.startswith("#"):
            end = index
            break
    section = lines[model_idx:end]
    current_provider = next(
        (
            line.strip().split(":", 1)[1].strip()
            for line in section
            if line.strip().startswith("provider:")
        ),
        "",
    )
    has_default = any(
        line.strip().startswith("default:") and line.strip().split(":", 1)[1].strip()
        for line in section
    )
    preserve_default = current_provider == "cursor" and has_default
    found: set[str] = set()
    rewritten: list[str] = []
    for line in section:
        stripped = line.strip()
        matched = False
        for key, value in wanted.items():
            if stripped.startswith(f"{key}:"):
                if key == "default" and preserve_default:
                    rewritten.append(line)
                else:
                    rewritten.append(f"  {key}: {value}")
                found.add(key)
                matched = True
                break
        if not matched:
            rewritten.append(line)
    insert_at = 1
    for key, value in wanted.items():
        if key not in found:
            rewritten.insert(insert_at, f"  {key}: {value}")
            insert_at += 1
    return "\n".join(lines[:model_idx] + rewritten + lines[end:]).rstrip() + "\n"


def sync_hermes_model_config(hermes_home: str | Path) -> Path:
    home = Path(hermes_home).expanduser()
    path = home / "config.yaml"
    reject_symlink_chain(home)
    reject_symlink_chain(path, home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    updated = upsert_model_config(text)
    if updated != text:
        atomic_write(path, updated, 0o644)
    return path


def model_config_is_current(hermes_home: str | Path) -> bool:
    path = Path(hermes_home).expanduser() / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return False
    lines = text.splitlines()
    return (
        "  provider: cursor" in lines
        and f"  base_url: {DEFAULT_BRIDGE_BASE_URL}" in lines
        and any(
            line.startswith("  default:") and line.split(":", 1)[1].strip()
            for line in lines
        )
    )
