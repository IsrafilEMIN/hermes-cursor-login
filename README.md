# Hermes Cursor Login

Native Cursor subscription authentication and model access for [Hermes Agent](https://github.com/hermes-agent-org/hermes).

Hermes talks to an authenticated OpenAI-compatible bridge on localhost. The bridge authenticates with Cursor through browser PKCE and talks directly to Cursor's HTTP/2 Connect/protobuf Agent API. It does not launch `cursor-agent`, give Cursor a workspace, or let Cursor execute native filesystem and shell tools.

## Architecture

```text
Hermes Agent
  -> OpenAI Chat Completions
  -> http://127.0.0.1:8765/v1
  -> authenticated local bridge
  -> Cursor HTTP/2 Connect/protobuf Agent API
  -> Cursor subscription
```

Hermes tools are advertised to Cursor as MCP tools. Cursor tool requests are returned as standard OpenAI tool calls, executed by Hermes, and replayed to Cursor with the result. Hermes remains responsible for tool policy and execution.

## Requirements

- Python 3.11 or newer
- macOS, Linux, WSL, or Windows
- Hermes Agent with user model-provider plugin discovery
- A Cursor account with subscription model access

Windows uses a logon scheduled task for the bridge. macOS uses LaunchAgent. Linux uses a systemd user unit with lingering so the bridge survives logout and reboot.

## Install

Pick one installation path. Every path provides the `hermes-cursor-login` command. The background bridge service installed by `login` pins the exact Python installation the command runs from, so prefer an isolated tool install that outlives your checkout.

Recommended — isolated tool install (`uv` or `pipx` puts `hermes-cursor-login` on your PATH):

```bash
uv tool install hermes-cursor-login   # after publication
uv tool install .                     # from a checkout
# or: pipx install hermes-cursor-login
# or: pipx install .
```

Development checkout — editable venv. The command lives in `.venv/bin`; the service will also pin that Python. If you delete or move the checkout, the bridge stops until you reinstall and re-run `login`. Put that `bin` directory on your PATH so you can run `hermes-cursor-login` without the prefix:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .

# zsh (default on macOS)
echo 'export PATH="'"$PWD"'/.venv/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# bash
# echo 'export PATH="'"$PWD"'/.venv/bin:$PATH"' >> ~/.bashrc
# source ~/.bashrc

hermes-cursor-login install
hermes-cursor-login login
hermes-cursor-login doctor
```

`$PWD` must be the checkout root when you append that line. After that, `install` / `login` / `doctor` work from any directory.

After a tool install (`uv` / `pipx`), the command is already on PATH:

```bash
hermes-cursor-login install
hermes-cursor-login login
hermes-cursor-login doctor
```

`install` writes the model-provider plugin, the bridge secret, and `config.yaml` (`model.provider=cursor`). `login` authenticates with Cursor and installs a user service so the loopback bridge keeps running after you close the terminal, quit Hermes, or reboot.

You do **not** need to leave `serve` running. Cursor appears in the Hermes **model selector**, not in Desktop's Plugins UI.

`login` starts a macOS LaunchAgent or Linux systemd user unit so the loopback bridge stays up without a terminal. Logs go to `$HERMES_HOME/cursor-bridge.log`.

`install` writes:

```text
$HERMES_HOME/plugins/model-providers/cursor/__init__.py
$HERMES_HOME/plugins/model-providers/cursor/plugin.yaml
$HERMES_HOME/.env                         CURSOR_BRIDGE_API_KEY only
$HERMES_HOME/config.yaml                  model.provider/default/base_url
```

`login` stores Cursor credentials separately at:

```text
$HERMES_HOME/cursor-auth.json             mode 0600
$HERMES_HOME/.cursor-auth.lock            mode 0600
```

For another Hermes home:

```bash
hermes-cursor-login install --hermes-home /path/to/hermes-home
hermes-cursor-login login --hermes-home /path/to/hermes-home
```

## Configure Hermes

`install` already sets `model.provider`, `model.default`, and `model.base_url` in `$HERMES_HOME/config.yaml`. Restart Hermes Desktop or the CLI so it reloads that home. Cursor shows in the **model selector**, not Plugins.

Hermes named profiles (`~/.hermes/profiles/<name>/`) are separate `HERMES_HOME` trees. Creating a profile clones `config.yaml` (`model.provider: cursor`) but not `plugins/`, so the new profile fails with `Unknown provider 'cursor'`. `install` copies the Cursor provider into every existing profile under that home. After `hermes profile create`, run `install` again, or:

```bash
hermes-cursor-login install --hermes-home ~/.hermes/profiles/<name>
```


If models disappear after a restart or reboot, run `hermes-cursor-login doctor`. Persistence should be `launchd` (macOS), `systemd-user` (Linux), or `logon-task` (Windows). `none` means run `login` again. `doctor` also reports `service interpreter: missing (...)` when the service points at a Python installation that no longer exists (typical after deleting a checkout venv); the fix is to reinstall and re-run `login`.

Foreground/`--background` serve is only for debugging:

```bash
hermes-cursor-login serve --install-service
hermes-cursor-login serve --background
hermes-cursor-login serve
```

Then run Hermes:

```bash
hermes --oneshot 'Reply exactly CURSOR_OK' --provider cursor --model default --safe-mode
```

The bridge exposes:

```text
GET  /health
GET  /v1/models
POST /v1/chat/completions
```

Every route requires:

```http
Authorization: Bearer <CURSOR_BRIDGE_API_KEY>
```

## Commands

```text
hermes-cursor-login install [--hermes-home PATH] [--force]
hermes-cursor-login login [--hermes-home PATH] [--no-browser]
hermes-cursor-login status [--hermes-home PATH]
hermes-cursor-login logout [--hermes-home PATH]
hermes-cursor-login doctor [--hermes-home PATH]
hermes-cursor-login serve [--hermes-home PATH] [--port PORT] [--timeout-seconds SECONDS]
                         [--background | --install-service | --uninstall-service]
hermes-cursor-login stop [--hermes-home PATH]
hermes-cursor-login uninstall [--hermes-home PATH] [--force]
```

`CURSOR_API_KEY` can provide an ephemeral access-token override. It is read from the process environment and is never written by the plugin.

## Authentication

The login flow is:

1. Generate a random PKCE verifier and SHA-256 challenge.
2. Open Cursor's `loginDeepControl` browser URL with the challenge and a login UUID.
3. Poll Cursor with the UUID and private verifier.
4. Store the returned access and refresh tokens in an atomic mode-`0600` credential file.
5. Refresh the access token under a cross-process lock before it reaches the five-minute expiry window.

No Cursor token is printed, placed in process arguments, or used as the local bridge credential.

## Security boundaries

- Fixed `127.0.0.1` binding with no non-loopback option
- Random local bridge bearer token on every route
- Cursor endpoint fixed to `https://api2.cursor.sh`
- TLS certificate verification and HTTP/2 ALPN required
- Atomic mode-`0600` credential and environment files
- Cross-process credential lock around refresh-token rotation
- Symlink rejection on managed credential and plugin paths
- Bounded request bodies, response frames, concurrency, and wall time
- Pre-body bridge authentication
- Client-disconnect cancellation
- Offered-tool allowlisting
- Cursor-native shell, filesystem, screen, fetch, and computer tools rejected
- Inline base64 images only; the bridge never fetches remote image URLs
- Generic HTTP errors and secret-free logs

See [SECURITY.md](SECURITY.md) for the detailed trust model.

## Quota data

The plugin does not call Cursor's dashboard usage endpoints and does not construct `WorkosCursorSessionToken`. Those endpoints only display remaining quota pools; they are not required for login, model discovery, inference, or Cursor's server-side plan enforcement.

## Current limitations

- Cursor's Agent API and protobuf schema are not a stable public compatibility contract.
- `login` and `serve --install-service` install a macOS LaunchAgent, Linux systemd user unit, or Windows logon task. `--background` starts a session process that does not survive reboot.
- Native Windows uses a per-user logon scheduled task; WSL and Linux use systemd user lingering when available.
- Remote image URLs are rejected instead of fetched.
- Dashboard quota bars are intentionally out of scope.

## Development

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

The suite covers PKCE, refresh rotation, credential permissions, symlink rejection, provider installation, bridge admission, OpenAI response shapes, streaming disconnects, protobuf history, MCP tool boundaries, image handling, and model-catalog normalization.

## Provenance

The project is an independent implementation. Protocol behavior was cross-checked against the MIT-licensed [Oh My Pi Cursor provider](https://github.com/can1357/oh-my-pi) and the native transport proposed in [Hermes PR #40876](https://github.com/NousResearch/hermes-agent/pull/40876). The Cursor protobuf schema and generated Python bindings retain Nous Research's MIT attribution.

This project is not affiliated with or endorsed by Nous Research, Anysphere, Cursor, or the Oh My Pi maintainers.