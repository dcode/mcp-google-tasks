# Configuration

The server reads its configuration from environment variables at startup; a few can be overridden
with CLI flags. Empty values count as unset. Every problem is reported before the server starts,
as one message on stderr with exit code 2. [.env.example](../.env.example) lists every variable
with placeholder values.

## CLI

```text
mcp-google-tasks [--transport http|streamable-http|stdio] [--host HOST] [--port PORT]
                 [--auth-mode oauth-proxy|resource-server|token]
                 [--log-level debug|info|warning|error] [--version] [--help]
```

| Flag | Overrides | Notes |
|---|---|---|
| `--transport` | `MCP_TRANSPORT` | `streamable-http` is accepted as an alias of `http` |
| `--host` | `MCP_HOST` | HTTP only |
| `--port` | `MCP_PORT` | HTTP only |
| `--auth-mode` | `MCP_AUTH_MODE` | |
| `--log-level` | `MCP_LOG_LEVEL` | |
| `--version` | | Prints the version and exits |

`python -m mcp_google_tasks` behaves the same as the `mcp-google-tasks` command.

## Common

| Variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `http` | `http` (streamable HTTP, endpoint `/mcp`) or `stdio`. `streamable-http` is an alias |
| `MCP_AUTH_MODE` | `token` with stdio, `oauth-proxy` with HTTP | `token`, `resource-server` or `oauth-proxy` |
| `MCP_HOST` | `127.0.0.1` (the image sets `0.0.0.0`) | Bind address for HTTP |
| `MCP_PORT` | `8000` | Bind port for HTTP, 1 to 65535 |
| `MCP_BASE_URL` | `http://localhost:{MCP_PORT}` | Public `http(s)` URL clients reach the server at, without `/mcp` and without a trailing slash. Fallback: `FASTMCP_SERVER_AUTH_GOOGLE_BASE_URL` |
| `MCP_LOG_LEVEL` | `info` | Lowest level written to stderr: `debug`, `info`, `warning` or `error` |
| `GOOGLE_TASKS_READONLY` | `false` | `1`, `true`, `yes` or `on`: request `tasks.readonly` and do not register write tools. `0`, `false`, `no` or `off` keep writes on; any other value is a startup error |
| `GOOGLE_OAUTH_CLIENT_ID` | | Google OAuth client ID. Fallback: `FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_ID` |
| `GOOGLE_OAUTH_CLIENT_SECRET` | | Google OAuth client secret. Fallback: `FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_SECRET` |

The HTTP server always serves `GET /health` without authentication; it answers `200` with
`{"status": "ok"}`.

## `token` mode (single user)

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_OAUTH_REFRESH_TOKEN` | One of these two | Long-lived refresh token; needs the client ID and secret |
| `GOOGLE_OAUTH_ACCESS_TOKEN` | One of these two | Access token used until Google rejects it, then refreshed if a refresh token is set |
| `GOOGLE_OAUTH_CLIENT_ID` | With a refresh token | The client the refresh token was issued to |
| `GOOGLE_OAUTH_CLIENT_SECRET` | With a refresh token | Its secret |
| `MCP_AUTH_TOKEN` | Recommended over HTTP | Clients must send `Authorization: Bearer <value>`. Without it, an HTTP server logs a warning at startup and serves anyone who can reach it |

Works over stdio and HTTP. `MCP_AUTH_TOKEN` has no effect over stdio.

## `resource-server` mode (multi user, behind a gateway)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `MCP_BASE_URL` | Yes, in practice | see above | Public HTTPS URL; advertised in the RFC 9728 metadata |
| `GOOGLE_OAUTH_AUDIENCE` | One of these two | | Comma-separated client IDs whose tokens are accepted |
| `GOOGLE_OAUTH_CLIENT_ID` | One of these two | | Used as the single accepted audience when `GOOGLE_OAUTH_AUDIENCE` is unset |
| `TOKEN_CACHE_TTL_SECONDS` | No | `300` | How long a verified token is cached before Google is asked again; `0` turns the cache off |

HTTP only. The client secret is not needed: the server only verifies tokens. Each bearer token is
checked against Google's `tokeninfo` endpoint; its `aud` must be one of the accepted audiences
and it must carry the Tasks scope (`tasks`, or with `GOOGLE_TASKS_READONLY=true` either
`tasks` or `tasks.readonly`). Protected-resource metadata (RFC 9728) is served at
`{MCP_BASE_URL}/.well-known/oauth-protected-resource/mcp`, naming
`https://accounts.google.com` as the authorization server.

## `oauth-proxy` mode (multi user, server runs OAuth)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | Yes | | Web client ID |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Yes | | Web client secret |
| `MCP_BASE_URL` | Yes, in practice | see above | Public URL; Google redirects users to `{MCP_BASE_URL}{GOOGLE_OAUTH_REDIRECT_PATH}` |
| `GOOGLE_OAUTH_REDIRECT_PATH` | No | `/auth/callback` | Callback path; a missing leading `/` is added. Fallback: `FASTMCP_SERVER_AUTH_GOOGLE_REDIRECT_PATH` |
| `JWT_SIGNING_KEY` | With persistent storage | derived from the client secret | Signs tokens the server issues to MCP clients |
| `TOKEN_STORAGE_BACKEND` | No | `default` | `default`, `filetree` or `redis` (below) |
| `STORAGE_ENCRYPTION_KEY` | With `filetree` or `redis` | | Fernet key that encrypts stored tokens |
| `TOKEN_STORAGE_DIR` | No | `/data/oauth-tokens` | Directory for `filetree` |
| `REDIS_URL` | With `redis` | | For example `redis://redis.example.com:6379/0` |
| `STORAGE_KEY_PREFIX` | No | | Prefix for storage collections, to share one Redis between deployments |
| `MCP_ALLOWED_CLIENT_REDIRECT_URIS` | No | any loopback or registered URI | Comma-separated patterns (`*` wildcards) for the redirect URIs MCP clients may register |

HTTP only. Storage backends:

- **`default`**: FastMCP's own encrypted file store under its home directory. Inside a container
  that directory is not on the volume, so every restart signs all users out. Use it only for
  local testing.
- **`filetree`**: encrypted files under `TOKEN_STORAGE_DIR`; mount a persistent volume there
  (the image declares `/data`).
- **`redis`**: encrypted entries in Redis; use it when running more than one replica.

`MCP_ALLOWED_CLIENT_REDIRECT_URIS` limits which MCP clients can register through dynamic client
registration. For example, to admit only Obot:
`MCP_ALLOWED_CLIENT_REDIRECT_URIS=https://obot.example.com/*`.

## Startup errors

Each is a `ConfigurationError`: the message names the variable and what to do, the process
exits with code 2, and no secret value is ever printed.

| Condition | Fix |
|---|---|
| `MCP_TRANSPORT` is not `http`, `streamable-http` or `stdio` | Use one of those |
| `MCP_AUTH_MODE` is not one of the three modes | Use one of those |
| `oauth-proxy` or `resource-server` with `stdio` | Use HTTP, or `token` mode |
| `MCP_PORT` is not an integer from 1 to 65535 | Fix the port |
| `MCP_BASE_URL` is not an `http://` or `https://` URL | Fix the URL |
| `MCP_LOG_LEVEL` is not `debug`, `info`, `warning` or `error` | Use one of those |
| `GOOGLE_TASKS_READONLY` is not a recognized true/false word | Use `true` or `false` |
| `TOKEN_CACHE_TTL_SECONDS` is not a whole number of 0 or more | Fix the value |
| `token` mode with neither an access token nor a usable refresh token (a refresh token without the client ID and secret is not usable) | Set `GOOGLE_OAUTH_ACCESS_TOKEN`, or the refresh token with client ID and secret |
| `oauth-proxy` without client ID or secret | Set both |
| `resource-server` without `GOOGLE_OAUTH_AUDIENCE` or `GOOGLE_OAUTH_CLIENT_ID` | Set one; the audience check is mandatory |
| `TOKEN_STORAGE_BACKEND` is not `default`, `filetree` or `redis` | Use one of those |
| `filetree` or `redis` without `STORAGE_ENCRYPTION_KEY` | Generate a Fernet key ([authentication.md](authentication.md#multi-user-keys)) |
| `filetree` or `redis` without `JWT_SIGNING_KEY` | Set a stable random string |
| `STORAGE_ENCRYPTION_KEY` is not a valid Fernet key | Generate a new one; it is 44 url-safe base64 characters |
| `redis` without `REDIS_URL` | Set it |

Variables that do not apply to the selected mode are ignored.

## Logging

Logs go to stderr only, because stdout carries the MCP protocol over stdio. They are rendered
for people on a terminal and as JSON lines otherwise. Fields whose names contain `token`,
`secret`, `authorization`, `password` or `key` are redacted before they are written.
