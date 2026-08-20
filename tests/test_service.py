from __future__ import annotations

import sys
from pathlib import Path

from hermes_cursor_login import cli
from hermes_cursor_login.service import (
    _program_from_cmd,
    _program_from_plist,
    _program_from_unit,
    launchd_plist,
    probe_listener,
    systemd_unit,
)


def test_launchd_plist_runs_module_serve(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "hermes_cursor_login.service.sys.executable", "/opt/venv/bin/python"
    )
    home = tmp_path / "hermes"
    text = launchd_plist(home, 8765)
    assert "dev.hermes.cursor-bridge" in text
    assert "<string>-m</string>" in text
    assert "<string>hermes_cursor_login</string>" in text
    assert "<string>serve</string>" in text
    assert f"<string>{home}</string>" in text
    assert "<true/>" in text
    assert "HERMES_HOME" in text


def test_systemd_unit_quotes_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "hermes_cursor_login.service.sys.executable", "/opt/venv/bin/python"
    )
    home = tmp_path / "hermes home"
    text = systemd_unit(home, 8765)
    assert "hermes_cursor_login" in text
    assert "'serve'" in text


def test_probe_listener_false_on_closed_port() -> None:
    assert probe_listener(1) is False


def test_serve_help_documents_background(capsys) -> None:
    assert cli.main(["serve", "--help"]) == 0
    output = capsys.readouterr().out
    assert "--background" in output
    assert "--install-service" in output


def test_stop_without_pid_file(tmp_path: Path, capsys) -> None:
    assert cli.main(["stop", "--hermes-home", str(tmp_path / "hermes")]) == 1
    assert "No background" in capsys.readouterr().out


def test_program_parsers_extract_interpreter(tmp_path: Path) -> None:
    assert _program_from_plist(
        launchd_plist(tmp_path, 8765).encode()
    ) == sys.executable
    assert _program_from_unit(systemd_unit(tmp_path, 8765)) == sys.executable
    assert _program_from_cmd('@echo off\r\n"C:\\py\\python.exe" "-m" "x"\r\n') == "C:\\py\\python.exe"
    assert _program_from_plist(b"not a plist") is None
    assert _program_from_unit("[Service]\n") is None

