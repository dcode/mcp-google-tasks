# Spec 0001: restructure for Obot multi-user and single-user deployment

Status: accepted (2026-11-22). This is the development specification for bringing the
original single-module server up to the project standards; DESIGN.md records the resulting
decisions and ROADMAP.md tracks progress.

Repo: dcode/mcp-google-tasks, image ghcr.io/dcode/mcp-google-tasks.
Original code: src/mcp_google_tasks/server.py (single 1385-line module) (all 15 tools + 2 resources
and every behavior must be preserved unless this spec changes it).
Reference: the house python-project-scaffold (kind `mcp`) templates and conventions.
Sibling projects: dcode/monarch-mcp2-obot (Python, Obot containerized single-user) and
dcode/google-tasks-mcp (TypeScript; verified Obot multi-user write-up).

Installed: fastmcp 4.2.4 (NOT 2.x/3.x: check signatures in the installed package before using an
API), mcp 2.3, httpx 0.28, pydantic 2.14, py-key-value-aio 0.5, cryptography. Add cyclopts +
structlog (uv add). Python: requires-python >=3.14; add `.python-version` with `3.14` and re-sync so
the venv is 3.14.

## Standards (from the scaffold — adopt)

hatchling build with version from `__version__` in `__init__.py`; ruff
(E,F,I,UP,B,D,ANN,ASYNC,PT,S,RUF; google docstrings; line-length 100); ty with error-on-warning;
pytest + pytest-asyncio auto + pytest-cov with branch coverage `fail_under = 100`; pre-commit:
ruff-check/ruff-format, rumdl, taplo, ty (local). Take the [tool.*] sections from the scaffold
pyproject.toml verbatim except: keep our runtime deps, ty include src+tests. AGENTS.md,
docs/DESIGN.md, docs/ROADMAP.md mandatory. No test touches the network. Use `type` aliases,
`X | None`, `@override`, frozen dataclasses.

## Three auth modes (decoupled from transport)

`MCP_AUTH_MODE` = `oauth-proxy` | `resource-server` | `token` (default: `token` when transport is
stdio, else `oauth-proxy`). Transport `MCP_TRANSPORT` = `http` | `stdio` (default `http`; accept
`streamable-http` as alias of `http`).

1. **oauth-proxy** (multi-user, server runs the OAuth flow): FastMCP `GoogleProvider` (OAuth proxy,
   DCR/CIMD) with GOOGLE_OAUTH_CLIENT_ID/SECRET, MCP_BASE_URL, redirect path, required scopes,
   JWT_SIGNING_KEY, optional persistent Fernet-encrypted storage (filetree/redis, prefix wrapper)
   exactly as the original `build_client_storage`, plus optional `MCP_ALLOWED_CLIENT_REDIRECT_URIS`
   (comma list -> allowed_client_redirect_uris) to restrict which MCP clients may register (e.g.
   only Obot's callback). Requires http transport.
2. **resource-server** (multi-user, a gateway such as Obot does OAuth and forwards
   `Authorization: Bearer <google token>`): FastMCP `RemoteAuthProvider` with
   `token_verifier=GoogleTokenVerifier(...)` (audience = `GOOGLE_OAUTH_CLIENT_ID` or the
   `GOOGLE_OAUTH_AUDIENCE` list, required scopes = the tasks scope,
   `cache_ttl_seconds=TOKEN_CACHE_TTL_SECONDS`, default 300),
   `authorization_servers=["https://accounts.google.com"]`, `base_url=MCP_BASE_URL` and a
   `resource_name`, so RFC 9728 metadata is served. Audience is REQUIRED (confused-deputy
   protection: reject tokens minted for other OAuth apps). Requires http transport. This is the
   verified Obot path (Obot v0.25.2 Containerized runtime never brokers OAuth —
   obot-platform/obot#5372 — so multi-user on Obot = Remote catalog entry + Static OAuth; see
   sibling TS README).
3. **token** (single-user): GOOGLE_OAUTH_ACCESS_TOKEN and/or GOOGLE_OAUTH_REFRESH_TOKEN (+ client
   id/secret for refresh). Works over stdio AND http (Obot containerized `singleUser` entries pass
   these as env vars and talk streamable HTTP). Fix the original bug: a supplied access token was
   treated as valid forever even when a refresh token exists. New rule: use the supplied access
   token until Google returns 401, then refresh (if refresh creds exist) and retry the request once;
   refreshed tokens expire per `expires_in` minus 60 s skew; refresh guarded by an asyncio.Lock.
   Optional `MCP_AUTH_TOKEN`: when set in token+http mode, require `Authorization: Bearer <it>`
   (custom TokenVerifier using `hmac.compare_digest`); log a warning at startup when token+http runs
   without it.

Read-only: `GOOGLE_TASKS_READONLY=true` -> request `tasks.readonly` scope AND do not register write
tools at all. Scopes: openid + userinfo.email + tasks (or tasks.readonly) for oauth-proxy;
resource-server requires only the tasks scope (either full or readonly when readonly mode).

## Module layout (src/mcp_google_tasks/)

- `__init__.py`: module docstring, `__version__ = "0.1.0"`. Remove `hello()`.
- `__main__.py`: `from mcp_google_tasks.cli import main` + `main()` under
  `if __name__ == "__main__"` (original never called main — bug).
- `errors.py`: `GoogleTasksError` base; `ConfigurationError`, `AuthenticationError`,
  `ConnectivityError`, `ApiError(status_code, message, api_status)`; messages safe for users, never
  contain secrets.
- `config.py`: `Settings` frozen slots dataclass; secrets typed `pydantic.SecretStr` (or
  field(repr=False)) so repr never leaks them. `load_settings(env: Mapping[str, str], *,`
  `transport=None, host=None, port=None, auth_mode=None) -> Settings` is PURE (env
  injected; cli passes os.environ). All validation raises ConfigurationError with actionable text.
  Keep every original env var + FASTMCP_SERVER_AUTH_GOOGLE_* fallbacks. Validate port range
  1-65535, MCP_BASE_URL is http(s), Fernet key validity checked at load time. Defaults: host
  127.0.0.1, port 8000, base_url `http://localhost:{port}`.
- `models.py`: the pydantic resource models from the original (Task, TaskList, TasksResponse,
  TaskListsResponse, OperationResult, CurrentUserInfo, link/assignment sub-models) unchanged in
  shape.
- `credentials.py`: `CredentialSource` Protocol: `async def bearer_token(self) -> str` and
  `async def refresh(self) -> bool` (returns True if a new token was obtained so the caller may
  retry). `RequestCredentials` (reads `fastmcp.server.dependencies.get_access_token()`; refresh() ->
  False) and `StaticCredentials` (token mode, refresh via <https://oauth2.googleapis.com/token>
  using the shared httpx client).
- `api.py`: `TasksApi` Protocol with one async method per Google endpoint (list_tasklists,
  get_tasklist, insert_tasklist, update_tasklist, patch_tasklist, delete_tasklist, list_tasks,
  get_task, insert_task, update_task, patch_task, delete_task, move_task, clear_tasks, get_userinfo)
  and `GoogleTasksClient(http: httpx.AsyncClient, credentials: CredentialSource)`. Path segments:
  reject empty, `.`, `..`, and control chars with ValueError-derived error; percent-encode with
  `quote(seg, safe="@")` only for the literal `@default`/`@me` otherwise `safe=""`. Map httpx errors
  -> ConnectivityError (no token in message), non-2xx -> ApiError (Google error JSON parsing as
  original), 401 -> credentials.refresh() then retry once. Explicit timeouts. Resources fetch ALL
  pages (loop on nextPageToken, maxResults=100, cap 50 pages).
- `tools.py`: `register_tools(mcp, api_factory, *, readonly)` and `register_resources(...)`. Same 15
  tool names, parameters, descriptions and behavior as original (get_current_user, list_tasklists,
  get_tasklist, create_tasklist, update_tasklist, patch_tasklist, delete_tasklist, list_tasks,
  get_task, create_task, update_task, patch_task, delete_task, move_task, clear_completed_tasks).
  Timestamps: accept str but validate RFC 3339 (datetime.fromisoformat after Z->+00:00, must be
  tz-aware) with a clear error. Annotations via `mcp.types.ToolAnnotations` or dict with
  readOnlyHint/destructiveHint/idempotentHint/openWorldHint. `anticipated` wrapper converts
  GoogleTasksError/ValueError into fastmcp `ToolError` with the message. Tool docstrings are the
  model's manual (when to call, returns, on failure). get_current_user: token mode -> userinfo
  endpoint via api (raise on failure, do not swallow); multi-user -> token claims
  - scopes; in resource-server mode claims come from GoogleTokenVerifier.
- `server.py`: `build_server(settings, *, http_client: httpx.AsyncClient | None = None) -> FastMCP`.
  No module-level mutable singletons: lifespan owns one pooled httpx.AsyncClient (limits, timeout 30
  s, Accept json) unless one is injected (tests use httpx.MockTransport). Custom route `GET /health`
  -> 200 JSON {"status":"ok"} unauthenticated. Server instructions text as original.
- `auth.py`: `build_auth_provider(settings) -> AuthProvider | None` for the three modes + `storage`
  builder (or put the storage builder in `storage.py`).
- `logs.py`: structlog to stderr only (stdout carries stdio MCP), JSON when not a TTY, redact keys
  matching token|secret|authorization|password|key.
- `cli.py`: cyclopts app, `main()`; default command runs the server; flags --transport --host --port
  --auth-mode --version; ConfigurationError -> message on stderr, exit 2. Console script
  `mcp-google-tasks = "mcp_google_tasks.cli:main"`.

## Container / Obot

Multi-stage Dockerfile: builder `python:3.14-slim` + pinned `ghcr.io/astral-sh/uv:<version>`,
`uv sync --locked --no-dev --no-editable`; runtime python:3.14-slim, non-root uid 10001, ENV
MCP_TRANSPORT=http MCP_HOST=0.0.0.0 MCP_PORT=8000 TOKEN_STORAGE_DIR=/data/oauth-tokens, VOLUME
/data, EXPOSE 8000, HEALTHCHECK via python urllib on /health, OCI labels, ENTRYPOINT the console
script. `.dockerignore`. MCP endpoint path is `/mcp`. Obot catalog: `catalog/google-tasks.yaml`
single-user containerized entry in the same format as monarch's (serverUserType: singleUser,
runtime: containerized, containerizedConfig image/port/path/healthz, env list with
GOOGLE_OAUTH_REFRESH_TOKEN, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
GOOGLE_OAUTH_ACCESS_TOKEN optional, GOOGLE_TASKS_READONLY optional, MCP_AUTH_MODE=token fixed).
Multi-user Obot setup is documented in docs (Remote runtime

- Static OAuth + Obot callback `https://<obot-host>/oauth/mcp/callback`), not as a catalog file.

## Out of scope / decided

- No dry-run default for write tools (deviation from the house MCP convention): this is a personal
  task manager where hosts already gate writes via destructiveHint; GOOGLE_TASKS_READONLY is the off
  switch. Record in DESIGN.md.
- Do NOT git commit, push, or tag.
