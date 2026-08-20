from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .profile import DEFAULT_BRIDGE_BASE_URL

LAUNCHD_LABEL = "dev.hermes.cursor-bridge"
SYSTEMD_UNIT = "hermes-cursor-bridge.service"
WINDOWS_TASK = "HermesCursorBridge"
PID_FILE = "cursor-bridge.pid"
LOG_FILE = "cursor-bridge.log"
WINDOWS_CMD = "cursor-bridge.cmd"
DEFAULT_PORT = int(DEFAULT_BRIDGE_BASE_URL.rsplit(":", 1)[-1].split("/", 1)[0])


def pid_path(home: Path) -> Path:
    return home / PID_FILE


def log_path(home: Path) -> Path:
    return home / LOG_FILE


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def python_command() -> list[str]:
    return [sys.executable, "-m", "hermes_cursor_login"]


def serve_argv(home: Path, port: int) -> list[str]:
    return [*python_command(), "serve", "--hermes-home", str(home), "--port", str(port)]


def probe_listener(port: int, *, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def read_pid(home: Path) -> int | None:
    path = pid_path(home)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not text.isdigit():
        return None
    return int(text)


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def write_pid(home: Path, pid: int) -> None:
    path = pid_path(home)
    path.write_text(f"{pid}\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def stop_pid(home: Path, *, timeout: float = 5.0) -> bool:
    pid = read_pid(home)
    path = pid_path(home)
    if pid is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return False
    if process_is_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline and process_is_alive(pid):
            time.sleep(0.05)
        if process_is_alive(pid):
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return True


def wait_for_listener(port: int, *, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe_listener(port):
            return True
        time.sleep(0.05)
    return False


def spawn_detached(home: Path, port: int) -> int:
    home.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {
        "args": serve_argv(home, port),
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        kwargs["cwd"] = str(home)
    else:
        kwargs["start_new_session"] = True
        kwargs["cwd"] = "/"
    with open(log_path(home), "ab") as handle:
        process = subprocess.Popen(stdout=handle, stderr=handle, **kwargs)
    write_pid(home, process.pid)
    return process.pid


def launchd_plist(home: Path, port: int) -> str:
    args = "".join(
        f"    <string>{_xml(arg)}</string>\n" for arg in serve_argv(home, port)
    )
    log = _xml(str(log_path(home)))
    hermes_home = _xml(str(home))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{LAUNCHD_LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{args}"
        "  </array>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <true/>\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict>\n"
        "    <key>HERMES_HOME</key>\n"
        f"    <string>{hermes_home}</string>\n"
        "  </dict>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{log}</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{log}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def systemd_unit(home: Path, port: int) -> str:
    command = " ".join(_quote(arg) for arg in serve_argv(home, port))
    return (
        "[Unit]\n"
        "Description=Hermes Cursor loopback bridge\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        f"Environment=HERMES_HOME={_quote(str(home))}\n"
        "Restart=on-failure\n"
        f"StandardOutput=append:{log_path(home)}\n"
        f"StandardError=append:{log_path(home)}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _program_from_plist(data: bytes) -> str | None:
    import plistlib

    try:
        return str(plistlib.loads(data)["ProgramArguments"][0])
    except (plistlib.InvalidFileException, KeyError, IndexError, TypeError):
        return None


def _program_from_unit(text: str) -> str | None:
    import shlex

    for line in text.splitlines():
        if line.startswith("ExecStart="):
            try:
                return shlex.split(line[len("ExecStart=") :])[0]
            except (ValueError, IndexError):
                return None
    return None


def _program_from_cmd(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith('"'):
            end = line.find('"', 1)
            if end > 1:
                return line[1:end]
    return None


def service_program_path(home: Path) -> str | None:
    try:
        if sys.platform == "darwin":
            return _program_from_plist(launchd_plist_path().read_bytes())
        if sys.platform == "win32":
            path = Path(home).expanduser() / WINDOWS_CMD
            return _program_from_cmd(path.read_text(encoding="utf-8"))
        return _program_from_unit(systemd_unit_path().read_text(encoding="utf-8"))
    except OSError:
        return None


def persistence_kind() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform == "win32":
        return "logon-task"
    if shutil_which("systemctl"):
        return "systemd-user"
    return "session-process"


def user_service_is_installed() -> bool:
    if sys.platform == "darwin":
        return launchd_plist_path().is_file()
    if sys.platform == "win32":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", WINDOWS_TASK],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0
    return systemd_unit_path().is_file()


def install_user_service(home: Path, port: int) -> str:
    home = Path(home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        path = launchd_plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launchd_plist(home, port), encoding="utf-8")
        os.chmod(path, 0o644)
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
        _run(["launchctl", "enable", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        _run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
            check=True,
        )
        return str(path)
    if sys.platform == "win32":
        script = home / WINDOWS_CMD
        command = " ".join(_win_quote(arg) for arg in serve_argv(home, port))
        script.write_text(f"@echo off\r\n{command}\r\n", encoding="utf-8")
        task = f"cmd.exe /c {_win_quote(str(script))}"
        _run(
            [
                "schtasks",
                "/Create",
                "/TN",
                WINDOWS_TASK,
                "/SC",
                "ONLOGON",
                "/RL",
                "LIMITED",
                "/F",
                "/TR",
                task,
            ],
            check=True,
        )
        _run(["schtasks", "/Run", "/TN", WINDOWS_TASK], check=True)
        return WINDOWS_TASK
    if shutil_which("systemctl"):
        path = systemd_unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_unit(home, port), encoding="utf-8")
        os.chmod(path, 0o644)
        _run(["systemctl", "--user", "daemon-reload"], check=True)
        _run(["systemctl", "--user", "enable", "--now", path.name], check=True)
        _run(["loginctl", "enable-linger", str(os.getuid())])
        return str(path)
    spawn_detached(home, port)
    return "session-process"


def uninstall_user_service() -> None:
    if sys.platform == "darwin":
        path = launchd_plist_path()
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    if sys.platform == "win32":
        _run(["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"])
        return
    path = systemd_unit_path()
    _run(["systemctl", "--user", "disable", "--now", path.name])
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _run(["systemctl", "--user", "daemon-reload"])


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _win_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def _run(argv: list[str], *, check: bool = False) -> None:
    result = subprocess.run(argv, capture_output=True, check=False)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise OSError(detail or f"{argv[0]} failed")
