from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .bridge import BridgeApplication, create_http_server
from .credentials import CredentialError, CredentialStore
from .installer import (
    ENV_KEY,
    install_plugin,
    model_config_is_current,
    sync_hermes_model_config,
    uninstall_plugin,
)
from .native_runner import NativeCursorRunner, NativeRunnerConfig
from .oauth import (
    REFRESH_SKEW_MS,
    CursorOAuthError,
    login_cursor,
    resolve_cursor_access_token,
)
from .paths import read_dotenv_value
from .service import (
    DEFAULT_PORT,
    install_user_service,
    persistence_kind,
    probe_listener,
    service_program_path,
    spawn_detached,
    stop_pid,
    uninstall_user_service,
    user_service_is_installed,
    wait_for_listener,
)


def _default_hermes_home() -> str:
    return os.getenv("HERMES_HOME", "").strip() or str(Path.home() / ".hermes")


def is_supported_platform(platform_name: str | None = None) -> bool:
    return (platform_name or os.name) in {"posix", "nt"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-cursor-login",
        description="Native Cursor subscription provider for Hermes Agent",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--hermes-home", default=_default_hermes_home())
        return command

    add("install", "install the Hermes model-provider profile").add_argument(
        "--force", action="store_true"
    )
    add("uninstall", "remove the Hermes model-provider profile").add_argument(
        "--force", action="store_true"
    )
    add("login", "authenticate with Cursor in a browser").add_argument(
        "--no-browser", action="store_true"
    )
    add("logout", "remove stored Cursor credentials")
    add("status", "show Cursor credential status")
    serve = add("serve", "run the authenticated native Cursor bridge")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--timeout-seconds", type=float, default=1800.0)
    serve.add_argument(
        "--background",
        action="store_true",
        help="daemonize after binding the loopback listener",
    )
    serve.add_argument(
        "--install-service",
        action="store_true",
        help="install and start a user service that keeps the bridge running",
    )
    serve.add_argument(
        "--uninstall-service",
        action="store_true",
        help="stop and remove the user service",
    )
    add("stop", "stop a background bridge started with --background")
    add("doctor", "check plugin installation, credentials, and listener")
    return parser


def _store(hermes_home: str) -> CredentialStore:
    return CredentialStore(Path(hermes_home).expanduser())


def _fail(exc: BaseException) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return 2


def _install(args: argparse.Namespace) -> int:
    try:
        result = install_plugin(args.hermes_home, force=args.force)
        config_path = sync_hermes_model_config(args.hermes_home)
    except (FileExistsError, OSError, ValueError) as exc:
        return _fail(exc)
    print(f"Installed Cursor provider: {result.plugin_dir}")
    if result.backup_dir is not None:
        print(f"Previous provider backup: {result.backup_dir}")
    print(f"Bridge credential stored in: {result.env_path}")
    print(f"Hermes model config updated: {config_path}")
    print("Run `hermes-cursor-login login` once; the bridge survives reboot.")
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    home = Path(args.hermes_home).expanduser()
    try:
        uninstall_plugin(home, force=args.force)
        uninstall_user_service()
        stop_pid(home)
    except (FileExistsError, OSError, ValueError) as exc:
        return _fail(exc)
    print("Removed Cursor provider profile; credentials were preserved.")
    return 0


def _login(args: argparse.Namespace) -> int:
    store = _store(args.hermes_home)

    def show_url(url: str) -> None:
        print(f"Open this URL to authenticate with Cursor:\n{url}")
        print("Waiting for browser authentication...")

    try:
        credentials = login_cursor(
            on_auth_url=show_url, open_browser=not args.no_browser
        )
        store.save(credentials)
    except KeyboardInterrupt:
        print("Cursor login cancelled.", file=sys.stderr)
        return 130
    except (CredentialError, CursorOAuthError, OSError) as exc:
        return _fail(exc)
    print(f"Cursor credentials stored in: {store.path}")
    return _enable_user_service(Path(args.hermes_home).expanduser(), DEFAULT_PORT)


def _logout(args: argparse.Namespace) -> int:
    try:
        removed = _store(args.hermes_home).delete()
    except (CredentialError, OSError) as exc:
        return _fail(exc)
    print(
        "Removed stored Cursor credentials."
        if removed
        else "Cursor is already logged out."
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    try:
        credentials = _store(args.hermes_home).load()
    except (CredentialError, OSError) as exc:
        return _fail(exc)
    if credentials is None:
        print("Cursor: logged out")
        return 1
    now_ms = int(time.time() * 1000)
    state = (
        "refresh needed"
        if now_ms + REFRESH_SKEW_MS >= credentials.expires_at_ms
        else "ready"
    )
    expires = datetime.fromtimestamp(credentials.expires_at_ms / 1000, UTC).isoformat()
    print(f"Cursor: {state}")
    print(f"Credential expires: {expires}")
    return 0


def _cursor_api_key() -> str:
    return os.getenv("CURSOR_API_KEY", "").strip()


def _token_resolver(hermes_home: str) -> Callable[[], str]:
    store = _store(hermes_home)

    def resolve() -> str:
        return _cursor_api_key() or resolve_cursor_access_token(store)

    return resolve


def _bridge_token(home: Path) -> str:
    return os.getenv(ENV_KEY, "").strip() or read_dotenv_value(home / ".env", ENV_KEY)


def _enable_user_service(home: Path, port: int) -> int:
    kind = persistence_kind()
    try:
        path = install_user_service(home, port)
    except OSError as exc:
        spawn_detached(home, port)
        path = "session-process"
        kind = "session-process"
        print(f"warning: persistent user service unavailable ({exc})", file=sys.stderr)
    else:
        print(f"Installed Cursor bridge user service: {path}")
    if wait_for_listener(port):
        print(f"Cursor bridge listening on http://127.0.0.1:{port}/v1")
        if kind == "session-process":
            print(
                "warning: bridge will stop at logout; install launchd/systemd/logon task",
                file=sys.stderr,
            )
        return 0
    print(
        "error: bridge listener did not start; see cursor-bridge.log",
        file=sys.stderr,
    )
    return 1


def _serve(args: argparse.Namespace) -> int:
    home = Path(args.hermes_home).expanduser()
    if args.uninstall_service:
        try:
            uninstall_user_service()
            stop_pid(home)
        except OSError as exc:
            return _fail(exc)
        print("Removed the Cursor bridge user service.")
        return 0
    if args.install_service:
        return _enable_user_service(home, args.port)
    bridge_token = _bridge_token(home)
    if not bridge_token:
        print(
            "error: bridge credential is missing; run `hermes-cursor-login install`",
            file=sys.stderr,
        )
        return 2
    try:
        runner = NativeCursorRunner(
            NativeRunnerConfig(
                token_resolver=_token_resolver(args.hermes_home),
                timeout_seconds=args.timeout_seconds,
            )
        )
        if not _cursor_api_key():
            resolve_cursor_access_token(_store(args.hermes_home))
        app = BridgeApplication(runner=runner, token=bridge_token)
        server = create_http_server(host="127.0.0.1", port=args.port, app=app)
    except (CredentialError, CursorOAuthError, OSError, ValueError) as exc:
        return _fail(exc)
    bound = server.server_address[1]
    print(f"Cursor bridge listening on http://127.0.0.1:{bound}/v1")
    if args.background:
        server.server_close()
        runner.close()
        spawn_detached(home, bound)
        if wait_for_listener(bound):
            return 0
        print("error: background bridge did not start", file=sys.stderr)
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        runner.close()
    return 0


def _stop(args: argparse.Namespace) -> int:
    home = Path(args.hermes_home).expanduser()
    if stop_pid(home):
        print("Stopped the background Cursor bridge.")
        return 0
    print("No background Cursor bridge PID file was found.")
    return 1


def _doctor(args: argparse.Namespace) -> int:
    home = Path(args.hermes_home).expanduser()
    plugin_dir = home / "plugins" / "model-providers" / "cursor"
    plugin_ok = (plugin_dir / "__init__.py").is_file() and (
        plugin_dir / "plugin.yaml"
    ).is_file()
    bridge_ok = bool(_bridge_token(home))
    try:
        auth_ok = bool(
            _cursor_api_key() or resolve_cursor_access_token(_store(args.hermes_home))
        )
        auth_detail = "configured"
    except (CredentialError, CursorOAuthError, OSError) as exc:
        auth_ok = False
        auth_detail = str(exc)
    listener_ok = probe_listener(DEFAULT_PORT)
    service_ok = True
    service_program = ""
    if user_service_is_installed():
        service_program = service_program_path(home) or ""
        service_ok = not service_program or Path(service_program).exists()
    config_ok = model_config_is_current(home)
    print(f"hermes home: {home}")
    print(f"plugin: {'ok' if plugin_ok else 'missing'}")
    print(f"hermes model config: {'ok' if config_ok else 'missing'}")
    print(f"bridge credential: {'configured' if bridge_ok else 'missing'}")
    print(f"Cursor credentials: {auth_detail}")
    print(f"bridge listener: {'ok' if listener_ok else 'not running'}")
    print(
        "reboot persistence: "
        + (
            persistence_kind()
            if user_service_is_installed()
            else "none (will not survive logout or reboot)"
        )
    )
    if service_program:
        print(
            "service interpreter: "
            + ("ok" if service_ok else f"missing ({service_program})")
        )
    if not listener_ok:
        print("fix: hermes-cursor-login login   # starts the background bridge")
    if not config_ok:
        print("fix: hermes-cursor-login install   # writes model.provider=cursor")
    if not service_ok:
        print("fix: reinstall, then hermes-cursor-login login   # re-pins the service")
    return 0 if plugin_ok and bridge_ok and auth_ok and listener_ok and config_ok and service_ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if not is_supported_platform():
        print("error: unsupported platform", file=sys.stderr)
        return 2
    handlers = {
        "install": _install,
        "uninstall": _uninstall,
        "login": _login,
        "logout": _logout,
        "status": _status,
        "serve": _serve,
        "stop": _stop,
        "doctor": _doctor,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
