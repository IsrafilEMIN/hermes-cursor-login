from __future__ import annotations

import stat
from pathlib import Path

import pytest

from hermes_cursor_login.installer import (
    PLUGIN_INIT,
    PLUGIN_MANIFEST,
    install_plugin,
    model_config_is_current,
    uninstall_plugin,
    upsert_model_config,
)


def test_checked_in_profile_matches_installed_bytes() -> None:
    root = Path(__file__).resolve().parents[1] / "plugin" / "model-providers" / "cursor"

    assert (root / "__init__.py").read_text() == PLUGIN_INIT
    assert (root / "plugin.yaml").read_text() == PLUGIN_MANIFEST


def test_install_preserves_env_and_writes_private_bridge_secret(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    env = home / ".env"
    env.write_text("OPENROUTER_API_KEY=existing\n")

    result = install_plugin(home)

    text = env.read_text()
    assert "OPENROUTER_API_KEY=existing" in text
    assert "CURSOR_BRIDGE_API_KEY=" in text
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert result.backup_dir is None


def test_install_reuses_existing_valid_bridge_secret(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    token = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop"
    (home / ".env").write_text(f"CURSOR_BRIDGE_API_KEY={token}\n")

    install_plugin(home)
    install_plugin(home)

    assert (home / ".env").read_text().count("CURSOR_BRIDGE_API_KEY=") == 1
    assert token in (home / ".env").read_text()


def test_conflicting_provider_requires_force_and_is_backed_up(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    plugin = home / "plugins" / "model-providers" / "cursor"
    plugin.mkdir(parents=True)
    (plugin / "custom.py").write_text("custom = True\n")

    with pytest.raises(FileExistsError, match="conflicting"):
        install_plugin(home)

    result = install_plugin(home, force=True)
    assert result.backup_dir is not None
    assert (result.backup_dir / "custom.py").read_text() == "custom = True\n"
    assert (result.plugin_dir / "__init__.py").read_text() == PLUGIN_INIT


def test_install_rejects_symlinked_provider_directory(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    provider_root = home / "plugins" / "model-providers"
    provider_root.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    (provider_root / "cursor").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        install_plugin(home)


def test_uninstall_preserves_credentials_env_and_unowned_files(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    result = install_plugin(home)
    auth = home / "cursor-auth.json"
    auth.write_text("secret")
    unowned = result.plugin_dir / "notes.txt"
    unowned.write_text("keep")

    with pytest.raises(FileExistsError, match="unowned"):
        uninstall_plugin(home)

    assert auth.read_text() == "secret"
    assert (home / ".env").exists()
    assert unowned.read_text() == "keep"


def test_install_allows_user_selected_ancestor_alias(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    result = install_plugin(alias / "hermes")

    assert result.plugin_dir.is_dir()
    assert (target / "hermes" / ".env").is_file()


def test_install_selects_cursor_in_hermes_config(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: old\n"
    )
    install_plugin(home)
    text = (home / "config.yaml").read_text()
    assert "provider: cursor" in text
    assert "default: default" in text
    assert "base_url: http://127.0.0.1:8765/v1" in text


def test_install_preserves_user_default_when_provider_already_cursor(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: cursor\n  default: cursor-grok-4.6-medium\n"
        "  base_url: http://127.0.0.1:8765/v1\n"
    )
    install_plugin(home)
    text = (home / "config.yaml").read_text()
    assert "default: cursor-grok-4.6-medium" in text
    assert "default: default" not in text
    assert model_config_is_current(home)


def test_upsert_inserts_default_when_missing() -> None:
    updated = upsert_model_config("model:\n  provider: cursor\n")
    assert "  default: default" in updated.splitlines()

