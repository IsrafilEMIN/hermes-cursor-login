# Security

## Supported scope

The bridge is intended to run as the same local user as Hermes. It supports macOS, Linux, WSL, and Windows.

## Trust boundaries

- Anyone with `CURSOR_BRIDGE_API_KEY` can send prompts through the configured Cursor account.
- Anyone with the Cursor refresh token can obtain new access tokens until Cursor revokes the session.
- The listener binds exactly to `127.0.0.1`; there is no LAN binding option.
- User-service logs at `$HERMES_HOME/cursor-bridge.log` must stay secret-free.
- Hermes controls tool execution. The bridge exposes offered Hermes functions as MCP tools and returns Cursor requests as OpenAI tool calls.
- Cursor-native filesystem, shell, fetch, screen, computer-use, and resource tools are rejected.
- `CURSOR_API_KEY` is an explicit process-environment override and is not persisted by the plugin.

## Credential handling

Cursor OAuth credentials are stored in `$HERMES_HOME/cursor-auth.json` with mode `0600`. Writes use a same-directory temporary file, `fsync`, and atomic replacement. Refresh rotation runs under an exclusive mode-`0600` lock at `$HERMES_HOME/.cursor-auth.lock`.

The store rejects:

- symbolic links in the managed path;
- non-regular credential files;
- credential files readable or writable by group or other users;
- empty, whitespace-padded, control-character, or oversized tokens;
- unknown credential schema versions.

The PKCE verifier is never included in the browser authorization URL. Access and refresh tokens are never printed or passed through process arguments.

The bridge bearer token is generated with `secrets.token_urlsafe(32)` and written to `$HERMES_HOME/.env` with mode `0600`. It is distinct from Cursor credentials.

## Network handling

Cursor OAuth uses TLS through Python's verified standard-library HTTP client. Native inference requires TLS plus HTTP/2 ALPN and fixes the destination to `https://api2.cursor.sh`; callers cannot redirect credentials to another host, port, path, query, or userinfo authority.

The bridge provides:

- pre-body bearer authentication;
- constant-time bearer comparison;
- an 8 MiB request-body limit;
- a 16 MiB Cursor response and Connect-frame limit;
- bounded concurrent upstream calls;
- bounded OAuth, model-discovery, and inference timeouts;
- HTTP/2 flow-control-aware request splitting;
- cancellation after client disconnect;
- generic downstream errors without upstream response bodies or credentials;
- no CORS policy.

## Tool handling

Only function names offered in the current Hermes request are accepted in the completion returned to Hermes. An unoffered tool name fails the request. Tool arguments must decode to a JSON object. A specific OpenAI `tool_choice` filters the advertised MCP catalog and is enforced on the response.

Remote image URLs are rejected. Inline base64 image payloads are decoded locally with a 20 MiB per-image limit.

## Quota endpoints

The plugin does not call `/auth/usage`, `cursor.com/api/usage-summary`, or `cursor.com/api/auth/me`. It never constructs `WorkosCursorSessionToken`. Cursor's inference backend remains responsible for subscription and quota enforcement.

## Uninstall behavior

`uninstall` removes only byte-matching provider-profile files unless `--force` is supplied. It preserves `$HERMES_HOME/.env` and Cursor credentials. Run `hermes-cursor-login logout` to remove Cursor credentials explicitly.

## Reporting

Do not include access tokens, refresh tokens, bridge credentials, authorization URLs from an active login, or credential-file contents in a report. Revoke the Cursor session before sharing diagnostics if credential exposure is suspected.
