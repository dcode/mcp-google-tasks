# mcp-google-tasks — Google Tasks MCP server for Obot: multi-user OAuth or single-user tokens

The design record: the problem, the decisions made and the reasons for them. What is done and
what comes next is tracked in [ROADMAP.md](ROADMAP.md). The restructuring that produced the
current shape is specified in [specs/0001-restructure-for-obot.md](specs/0001-restructure-for-obot.md).

## Problem Statement

People who self-host [Obot](https://obot.ai), and individuals running a local MCP client, want an
agent to read and maintain their Google Tasks. The existing Google Tasks MCP servers either cover
only part of the API (no task-list operations), assume a single local user with a token file on
disk, or bundle all of Google Workspace.

Hosting adds constraints. A team on Obot needs each person to act as their own Google account
without the operator handling anyone's refresh token. Obot's Containerized runtime (v0.25.2)
never brokers OAuth for the servers it runs
([obot-platform/obot#5372](https://github.com/obot-platform/obot/issues/5372)), so multi-user
OAuth there only works for Remote entries with Static OAuth, because Google offers no dynamic
client registration. A single person on Obot, by contrast, is best served by a Containerized
entry that takes a refresh token as a sensitive field. Locally, the same server has to work over
stdio.

## Solution

One package, `mcp_google_tasks`, exposing the whole Google Tasks v1 API as 15 tools and 2
resources, with three authentication modes chosen independently of the transport:

- **`token`**: one user's access and/or refresh token from the environment, over stdio or HTTP.
  This serves local clients and the Obot single-user Containerized catalog entry.
- **`resource-server`**: a gateway (Obot with Static OAuth) runs Google sign-in and forwards
  each user's Google access token; the server verifies it with Google, checks its audience and
  scope, and serves RFC 9728 metadata. This is the supported Obot multi-user path.
- **`oauth-proxy`**: the server runs the OAuth flow itself through FastMCP's `GoogleProvider`,
  for MCP clients that connect directly, with Fernet-encrypted persistent token storage.

It ships as a non-root container image and an Obot catalog entry, with documentation for
operators, MCP users and contributors.

## User Stories

### Single user

1. As a person with a local MCP client, I want to run the server over stdio with my refresh
   token, so that my agent can manage my tasks without any hosting.
2. As an Obot user, I want to add Google Tasks from the catalog by pasting my credentials into
   sensitive fields, so that I do not have to deploy anything.
3. As a single user running the container over HTTP, I want to require a bearer token, so that
   nobody else who reaches the port can act on my tasks.

### Multi user

1. As an Obot admin, I want to register one deployment as a Remote entry with Static OAuth, so
   that every user signs in with their own Google account and I never handle their tokens.
2. As an operator, I want the server to reject Google tokens minted for other OAuth apps, so that
   it cannot be used as a confused deputy.
3. As an operator of the OAuth-proxy mode, I want user tokens encrypted at rest on a volume or in
   Redis, so that restarts do not sign everyone out and a leaked disk does not leak tokens.
4. As an operator, I want to restrict which MCP clients may register, so that only the clients I
   expect can start a sign-in.

### Everyone

1. As an operator, I want a read-only mode that removes write tools, so that an agent cannot
   change tasks where it only needs to read them.
2. As an MCP user, I want every Tasks operation (lists, tasks, move, clear completed), so that
   the agent never has to tell me it cannot do something the API supports.
3. As an operator, I want configuration errors reported at startup with the variable to fix, so
   that a misconfigured deployment fails fast instead of on the first tool call.
4. As an operator, I want an unauthenticated health endpoint, so that container platforms can
   probe the server.

## Implementation Decisions

### Overall shape

- `config.py` turns the environment into a frozen `Settings`; `load_settings(env, ...)` is pure
  and does all validation, raising `ConfigurationError`. Secrets are typed so `repr` never shows
  them.
- `auth.py` builds one FastMCP auth provider per mode (or none), and the encrypted storage.
- `credentials.py` defines the `CredentialSource` seam (bearer token plus refresh); `api.py`
  defines the `TasksApi` seam and its only adapter, `GoogleTasksClient`.
- `tools.py` registers tools and resources against a `TasksApi`; `server.py` wires everything in
  `build_server(settings, http_client=None)`, with the HTTP client owned by the lifespan instead
  of a module-level singleton.
- `cli.py` (cyclopts) and `logs.py` (structlog to stderr) are the edges.

A new tool touches `api.py` (if it needs a new endpoint), `tools.py`, [tools.md](tools.md) and
tests. Details are in [development.md](development.md).

### Auth modes are decoupled from the transport

**Decision:** `MCP_AUTH_MODE` (`token`, `resource-server`, `oauth-proxy`) is separate from
`MCP_TRANSPORT` (`http`, `stdio`). The default is `token` over stdio and `oauth-proxy` over
HTTP; the multi-user modes refuse stdio.

**Why:** the original equated HTTP with the OAuth proxy, but the Obot single-user Containerized
runtime talks streamable HTTP while needing token auth, and the verified Obot multi-user path
needs a resource server, not a proxy. The defaults keep the original behavior for anyone who set
only `MCP_TRANSPORT`.

### Resource-server mode requires an audience

**Decision:** `resource-server` verifies each bearer token with FastMCP's `GoogleTokenVerifier`
against `GOOGLE_OAUTH_AUDIENCE` (or `GOOGLE_OAUTH_CLIENT_ID`) and the Tasks scope, caches results
for `TOKEN_CACHE_TTL_SECONDS` (300 s), and serves RFC 9728 metadata naming
`https://accounts.google.com` through `RemoteAuthProvider`. Startup fails without an audience.

**Why:** any app can mint a Google access token with the Tasks scope. Without the `aud` check, a
token a user granted to some unrelated app would be accepted here (a confused deputy). The cache
bounds the cost of a `tokeninfo` call per request; 300 s is short against a one-hour token
lifetime. The metadata lets Obot discover Google as the authorization server.

### Token mode refreshes on 401

**Decision:** in `token` mode a supplied access token is used until Google answers `401`; then,
if refresh credentials exist, the server refreshes and retries the request once. Refreshed tokens
are reused until `expires_in` minus 60 s. An `asyncio.Lock` ensures one refresh at a time.

**Why:** the original treated a supplied access token as valid forever even with a refresh token
available, so every call failed after an hour. The expiry of a supplied token is unknown, so the
`401` is the only reliable signal; the retry is limited to once so a revoked refresh token
surfaces as an error instead of a loop.

### Token mode over HTTP can require a shared bearer token

**Decision:** `MCP_AUTH_TOKEN` makes a `token`-mode HTTP server require
`Authorization: Bearer <value>`, compared with `hmac.compare_digest`. Without it the server logs
a warning but still starts.

**Why:** a token-mode HTTP server acts as its owner for anyone who can reach it. Inside Obot's
Containerized runtime only the gateway can reach it, so the token is optional there; anywhere
else it should be set.

### Read-only mode hides write tools

**Decision:** `GOOGLE_TASKS_READONLY=true` requests `tasks.readonly` and does not register the
nine write tools at all.

**Why:** the narrower scope makes Google refuse writes, but a model that still sees write tools
will try them and fail. Not registering them keeps the tool list honest and gives a
prompt-injected model nothing to call.

### No dry-run default for write tools

**Decision:** write tools act immediately. This deviates from the house MCP convention that write
tools default to a dry run until writes are enabled.

**Why:** this is a personal task manager. Every write is small and visible in the user's own
Tasks app, hosts already gate writes through `destructiveHint` and `readOnlyHint`, and
`GOOGLE_TASKS_READONLY` is the off switch. A dry run would add a configuration step that every
user would turn off.

### FastMCP rather than the raw MCP SDK

**Decision:** keep FastMCP (4.2.x) for the server.

**Why:** `GoogleProvider` (an OAuth proxy with dynamic client registration and client ID
metadata documents, encrypted storage and its own token issuing) and `RemoteAuthProvider` with
`GoogleTokenVerifier` (RFC 9728 metadata, audience and scope checks) are exactly the two
multi-user modes. Rebuilding them on the raw `mcp` SDK would be the largest and riskiest part of
the code.

### Encrypted storage as in the original

**Decision:** `oauth-proxy` storage is `default` (FastMCP's own), `filetree` or `redis`, with an
optional prefix wrapper and a Fernet encryption wrapper that treats decryption failures as cache
misses. Persistent backends require `STORAGE_ENCRYPTION_KEY` and `JWT_SIGNING_KEY`; the Fernet key
is validated at startup.

**Why:** upstream Google tokens at rest must be encrypted; rotating the key should sign users out
rather than produce `500` errors; and a stable signing key keeps issued tokens valid across
restarts.

### Kubernetes manifests are an example, with one replica

**Decision:** [contrib/kubernetes/](../contrib/kubernetes/) holds plain Kustomize manifests (a
`resource-server` base, an `oauth-proxy` overlay with a PVC), not a Helm chart. They run one
replica with a read-only root filesystem under the `restricted` Pod Security Standard.
`contrib` is listed in `.ignoreobotcatalogs` so Obot does not read the manifests as catalog
entries.

**Why:** Kustomize ships with `kubectl` and keeps the example readable; a chart is worth its
upkeep only if people ask for one. Streamable HTTP sessions are held in the pod's memory, so a
second replica breaks sessions unless the ingress pins them by `Mcp-Session-Id`.

### Published to PyPI through trusted publishing

**Decision:** each `v*` tag publishes the sdist and wheel to PyPI as `mcp-google-tasks`, from the
release workflow, through PyPI trusted publishing (OIDC, the `pypi` environment), before the
GitHub release. The version lives only in `__init__.py`; the image tags that repeat it are
checked against it by a test.

**Why:** `uvx mcp-google-tasks` is the simplest way to run the server locally, and a PyPI
release gives the package a version badge and an index others can depend on. Trusted publishing
stores no token. Publishing to PyPI first means a failure leaves nothing half-released that
cannot be retried.

### Tooling and code style

- Python 3.14+, uv for environments, hatchling build with the version read from
  `__version__`.
- Async for all I/O, Google-style docstrings, strict annotations throughout (`X | None`, never
  `Optional`; built-in generics such as `list[]`).
- ruff (lint and format, line length 100, including the `S` security and `PT` pytest
  rules), ty (type check, warnings are errors), rumdl
  (Markdown), taplo (TOML), all through pre-commit. Settings live in `pyproject.toml`, except
  taplo's, which live in `.taplo.toml`.
- pytest with branch coverage held at 100%. hatch scripts: `hatch test`,
  `hatch fmt`, `hatch run types`, `hatch run hooks`, `hatch run check`, `hatch build`.
- American English everywhere. Conventional-commit messages.
- GitHub Actions: pre-commit job, test job on Linux and macOS, build job that installs the
  wheel into a clean environment. A `v*` tag publishes to PyPI (trusted publishing) and then a
  GitHub release, after checking that the tag matches `__version__`. Dependabot runs weekly
  for Actions and Python.
- Development specifications for larger changes go in [docs/specs/](specs/), numbered.

## Testing Decisions

- Automated tests run against in-process fakes. Google is an `httpx.MockTransport` handler
  injected through `build_server(..., http_client=...)`. Nothing in the suite contacts a real
  service.
- Tools are tested through `build_server(...).call_tool(...)`, including failure paths and
  read-only registration. Configuration is tested through `load_settings` with a dict for the
  environment.
- Branch coverage is held at 100%; a line that cannot be tested is excluded with
  `# pragma: no cover` and a reason.
- What can only be verified by hand (real Google sign-in, Obot, published images) is listed in
  ROADMAP.md under "Confirm against the real thing", with the command to run.

## Out of Scope

- **A multi-user Obot Containerized entry.** Obot v0.25.2 does not broker OAuth for Containerized
  servers (obot-platform/obot#5372). Revisit when that issue is resolved.
- **An interactive sign-in command for token mode.** The OAuth Playground with the operator's own
  client produces a refresh token without the server ever handling a browser flow
  ([authentication.md](authentication.md#single-user-refresh-token)).
- **Dry-run writes.** See the decision above.
- **Google Workspace beyond Tasks.**

## Further Notes

- The Obot facts (Remote-only `CheckForMCPAuth`, the fixed Static OAuth callback
  `https://<obot-host>/oauth/mcp/callback`, the public-address requirement for Remote URLs) were
  verified against Obot v0.25.2 for the sibling TypeScript server dcode/google-tasks-mcp.
- Google Tasks quirks the tools document: `due` keeps only the date; tasks completed in Google's
  apps become hidden, so `show_hidden` is needed to see them; `position` changes only through
  `move_task`; `tasks.clear` hides completed tasks.
