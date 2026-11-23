# Deploying on Obot

[Obot](https://obot.ai) can host this server two ways, and they map to different auth modes:

| Setup | Obot runtime | Auth mode | Who holds Google credentials |
|---|---|---|---|
| [Single-user](#single-user-containerized-catalog-entry) | Containerized (Obot runs the image per user) | `token` | Each user pastes their own refresh token into the entry |
| [Multi-user](#multi-user-remote-runtime-with-static-oauth) | Remote (you run one deployment) | `resource-server` | Obot runs Google sign-in per user and forwards their access token |

Both use the image `ghcr.io/dcode/mcp-google-tasks`, which serves streamable HTTP on port 8000
at `/mcp` and a health check at `/health`. Do the [Google Cloud setup](authentication.md#google-cloud-setup)
first. Facts about Obot below were verified against Obot v0.25.2.

## Single-user: Containerized catalog entry

The repository ships [catalog/google-tasks.yaml](../catalog/google-tasks.yaml), a
`serverUserType: singleUser`, `runtime: containerized` entry. Obot starts one container per user
from the image, talking streamable HTTP on port 8000 at `/mcp` and probing `/health`. The entry
fixes `MCP_AUTH_MODE=token` and asks each user for:

| Field | Required | Sensitive | Notes |
|---|---|---|---|
| `GOOGLE_OAUTH_REFRESH_TOKEN` | Yes | Yes | [How to get one](authentication.md#single-user-refresh-token) |
| `GOOGLE_OAUTH_CLIENT_ID` | Yes | No | The client the refresh token was issued to |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Yes | Yes | Its secret |
| `GOOGLE_OAUTH_ACCESS_TOKEN` | No | Yes | Rarely useful; refreshed on the first `401` |
| `GOOGLE_TASKS_READONLY` | No | No | `true` hides every write tool |

To add it, either register this repository as a catalog source in Obot's admin settings, so Obot
reads the YAML from it, or create a catalog entry by hand with the same image, port, path, health
check and fields. Pin the image to a released version rather than `latest`.

Each user's container sees only that user's credentials, and the container is reachable only
through Obot's gateway, so `MCP_AUTH_TOKEN` is not needed here.

The drawback is that every user needs a Google OAuth client and a refresh token of their own (or
the admin's client ID and secret, which then have to be shared). For a team, use the multi-user
setup.

## Multi-user: Remote runtime with Static OAuth

Obot runs the Google sign-in for each user, stores their tokens, and forwards each request to
this server as `Authorization: Bearer <that user's Google access token>`. The server runs in
`resource-server` mode: it never runs an OAuth flow and stores nothing; it verifies the token
with Google and calls the Tasks API with it.

### Why Remote and not Containerized

As of Obot v0.25.2 the gateway runs its OAuth discovery and static-OAuth broker logic
(`CheckForMCPAuth` in `pkg/api/handlers/mcpgateway/oauth/mcpoauthhandler.go`) only for catalog
entries with the **Remote** runtime. Containerized servers are treated as "no OAuth required"
and Obot never contacts the authorization server for them, whatever the server answers. This is
an open gap, [obot-platform/obot#5372](https://github.com/obot-platform/obot/issues/5372), not
a configuration mistake. So you deploy the container yourself and register its URL.

### 1. Deploy the server

Run one deployment of the image somewhere Obot's gateway reaches over HTTPS:

```bash
docker run -d --name google-tasks -p 8000:8000 \
  -e MCP_AUTH_MODE=resource-server \
  -e MCP_BASE_URL=https://tasks.example.com \
  -e GOOGLE_OAUTH_CLIENT_ID=<client-id> \
  ghcr.io/dcode/mcp-google-tasks:<version>
```

| Variable | Value |
|---|---|
| `MCP_AUTH_MODE` | `resource-server` |
| `MCP_BASE_URL` | The public HTTPS URL, without `/mcp` and without a trailing slash |
| `GOOGLE_OAUTH_CLIENT_ID` | The same client Obot will use. Only the ID: it is the accepted token audience |
| `GOOGLE_OAUTH_AUDIENCE` | Optional, instead of the above: a comma-separated list of accepted client IDs |
| `TOKEN_CACHE_TTL_SECONDS` | Optional, default `300` |
| `GOOGLE_TASKS_READONLY` | Optional |

On Kubernetes, [contrib/kubernetes/](../contrib/kubernetes/) has example Kustomize manifests
for this mode (Deployment, Service, Ingress) and an overlay for `oauth-proxy`.

The address must be public. Obot's remote MCP URL validation blocks loopback and private
addresses by default, so an in-cluster Service address is rejected unless that validation is
relaxed. Terminate TLS in front of the container (ingress, load balancer or reverse proxy) and
forward to port 8000.

The audience check is what stops confused-deputy attacks: without it, any Google access token
with the Tasks scope, minted for any other app, would be accepted. That is why the server will
not start in this mode without an audience.

Check it before going on:

```bash
curl -fsS https://tasks.example.com/health
curl -fsS https://tasks.example.com/.well-known/oauth-protected-resource/mcp
curl -si -X POST https://tasks.example.com/mcp | head -n 1   # expect 401
```

The metadata (RFC 9728) names `https://accounts.google.com` as the authorization server, and
every `401` points at it through its `WWW-Authenticate` header.

### 2. Register Obot's callback with Google

On the same Google OAuth client, add Obot's callback as an authorized redirect URI:

```text
https://<obot-host>/oauth/mcp/callback
```

This is one fixed path for the whole Obot instance, shared by every Remote entry that uses
Static OAuth; Obot routes each completed grant back to the right request through the OAuth
`state` parameter. Register it once.

### 3. Add the Remote catalog entry

In Obot, create a catalog entry with:

| Field | Value |
|---|---|
| Runtime | **Remote** |
| URL | `https://tasks.example.com/mcp` |
| Advanced → Static OAuth | **Required.** Client ID and client secret of the Google client, and the scopes |

Google does not support OAuth dynamic client registration, so MCP discovery alone cannot give
Obot a client. Static OAuth is the way to supply one. Scopes to enter:
`https://www.googleapis.com/auth/tasks` (or `tasks.readonly` for a read-only deployment), plus
`openid` and `https://www.googleapis.com/auth/userinfo.email` if you want `get_current_user` to
return the email.

When a user first connects, Obot sends them through Google sign-in, stores their tokens, and
from then on forwards their access token to the server.

## Alternative: `oauth-proxy` mode for other MCP clients

Without a gateway, the server can run the OAuth flow itself. In `oauth-proxy` mode it acts as an
OAuth authorization server for MCP clients (dynamic client registration and client ID metadata
documents), sends each user through Google sign-in, keeps their Google tokens, and issues its own
tokens to the client. Use it for MCP clients that connect directly, such as desktop or CLI
clients.

| Variable | Value |
|---|---|
| `MCP_AUTH_MODE` | `oauth-proxy` |
| `MCP_BASE_URL` | Public URL; register `{MCP_BASE_URL}/auth/callback` with Google |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | Web client |
| `JWT_SIGNING_KEY` | Stable random string |
| `TOKEN_STORAGE_BACKEND` | `filetree` or `redis` |
| `STORAGE_ENCRYPTION_KEY` | Fernet key |
| `MCP_ALLOWED_CLIENT_REDIRECT_URIS` | Recommended: only the clients you expect |

Whether Obot's Remote runtime can use this mode through dynamic client registration, without
Static OAuth, has not been confirmed; the supported Obot path is `resource-server`. See
[ROADMAP.md](ROADMAP.md).

## Persistent storage

Only `oauth-proxy` mode stores anything: client registrations, issued-token bookkeeping and each
user's upstream Google tokens, all Fernet-encrypted.

- **Single replica:** `TOKEN_STORAGE_BACKEND=filetree` with a persistent volume (a PVC on
  Kubernetes) mounted at `/data`; files go to `TOKEN_STORAGE_DIR`, default `/data/oauth-tokens`.
  The image runs as uid 10001, so the volume must be writable by it (on Kubernetes,
  `securityContext.fsGroup: 10001`, as in
  [contrib/kubernetes/oauth-proxy/](../contrib/kubernetes/oauth-proxy/)).
- **Several replicas:** `TOKEN_STORAGE_BACKEND=redis` with `REDIS_URL`, and
  `STORAGE_KEY_PREFIX` if the Redis is shared.
- Both require `STORAGE_ENCRYPTION_KEY` and `JWT_SIGNING_KEY`, kept in a secret store and stable
  across restarts. Rotating either signs everyone out
  ([key rotation](authentication.md#key-rotation)).

`resource-server` and `token` modes are stateless; they need no volume.

## Health check

`GET /health` answers `200` with `{"status": "ok"}` without authentication, in every mode. The
image's Docker `HEALTHCHECK` uses it, and so does the catalog entry. On Kubernetes, use it for
both liveness and readiness probes. It reports that the process serves HTTP, not that Google is
reachable or that any token is valid.

## Security hardening

- **Read-only mode.** `GOOGLE_TASKS_READONLY=true` requests only `tasks.readonly` and does not
  register write tools, so a prompt-injected model has nothing to call. Use it wherever writes
  are not needed.
- **`MCP_AUTH_TOKEN`.** A `token`-mode server on HTTP acts as you for anyone who can reach it.
  Set `MCP_AUTH_TOKEN` unless the port is reachable only by a trusted gateway (the Obot
  Containerized case). The server logs a warning at startup when it is missing.
- **`MCP_ALLOWED_CLIENT_REDIRECT_URIS`.** In `oauth-proxy` mode, restrict which MCP clients may
  register, for example `https://obot.example.com/*`. Without it any client that can reach the
  server can register and ask users to sign in.
- **TLS.** Terminate TLS in front of the server; the server itself speaks plain HTTP. Bearer
  tokens and OAuth codes must never cross a network unencrypted. Set `MCP_BASE_URL` to the
  `https://` address.
- **Audience.** In `resource-server` mode, accept only the client IDs you actually use.
- **Non-root image.** The image runs as uid 10001 from a slim base with no build tools. Keep it
  that way: drop all capabilities, disallow privilege escalation, and pin the image by version
  or digest. A read-only root filesystem with `/data` as the only writable mount is a good
  further step once verified for your mode.
- **Secrets.** Pass client secrets, refresh tokens and keys through your platform's secret
  mechanism (Obot sensitive fields, Kubernetes Secrets), never baked into an image or committed.
  Logs redact token, secret, key and authorization fields.
