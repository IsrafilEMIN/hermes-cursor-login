from __future__ import annotations

from pathlib import Path

from hermes_cursor_login import cli
from hermes_cursor_login.credentials import CredentialStore, CursorCredentials


def test_login_persists_credentials_without_printing_secrets(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "hermes"
    credentials = CursorCredentials(
        "access-secret", "refresh-secret", 2_000_000_000_000
    )
    monkeypatch.setattr(cli, "login_cursor", lambda **_: credentials)
    monkeypatch.setattr(cli, "install_user_service", lambda *_: home / "service")
    monkeypatch.setattr(cli, "wait_for_listener", lambda *_: True)

    exit_code = cli.main(["login", "--hermes-home", str(home), "--no-browser"])

    output = capsys.readouterr()
    assert exit_code == 0
    assert CredentialStore(home).load() == credentials
    assert "access-secret" not in output.out + output.err
    assert "refresh-secret" not in output.out + output.err
    assert "user service" in output.out


def test_logout_preserves_unrelated_hermes_files(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    store = CredentialStore(home)
    store.save(CursorCredentials("access-secret", "refresh-secret", 2_000_000_000_000))
    config = home / "config.yaml"
    config.write_text("model: cursor\n")

    assert cli.main(["logout", "--hermes-home", str(home)]) == 0

    assert store.load() is None
    assert config.read_text() == "model: cursor\n"


def test_status_never_prints_stored_tokens(tmp_path: Path, capsys) -> None:
    home = tmp_path / "hermes"
    CredentialStore(home).save(
        CursorCredentials("access-secret", "refresh-secret", 2_000_000_000_000)
    )

    assert cli.main(["status", "--hermes-home", str(home)]) == 0

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "access-secret" not in output
    assert "refresh-secret" not in output


def test_install_does_not_print_generated_bridge_token(tmp_path: Path, capsys) -> None:
    home = tmp_path / "hermes"

    assert cli.main(["install", "--hermes-home", str(home)]) == 0

    output = capsys.readouterr()
    token = next(
        line.split("=", 1)[1]
        for line in (home / ".env").read_text().splitlines()
        if line.startswith("CURSOR_BRIDGE_API_KEY=")
    )
    assert token
    assert token not in output.out + output.err


def test_serve_requires_installation_before_authentication(
    tmp_path: Path, capsys
) -> None:
    home = tmp_path / "hermes"

    assert cli.main(["serve", "--hermes-home", str(home)]) == 2
    assert "bridge credential is missing" in capsys.readouterr().err


def test_native_serve_help_has_no_cursor_agent_or_workspace_controls(capsys) -> None:
    assert cli.main(["serve", "--help"]) == 0

    output = capsys.readouterr().out
    assert "--cursor-command" not in output
    assert "--cursor-arg" not in output
    assert "--workspace" not in output
    assert "--mode" not in output


def test_windows_and_posix_are_supported() -> None:
    assert cli.is_supported_platform("posix") is True
    assert cli.is_supported_platform("nt") is True


def test_login_starts_background_bridge(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "hermes"
    credentials = CursorCredentials(
        "access-secret", "refresh-secret", 2_000_000_000_000
    )
    monkeypatch.setattr(cli, "login_cursor", lambda **_: credentials)
    called: list[object] = []

    def enable(path, port):
        called.append((path, port))
        return 0

    monkeypatch.setattr(cli, "_enable_user_service", enable)
    assert cli.main(["login", "--hermes-home", str(home), "--no-browser"]) == 0
    assert called == [(home, 8765)]
