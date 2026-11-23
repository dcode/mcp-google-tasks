# Development

How the code is organized, where the seams are, how it is tested, and how it is released. Read
[DESIGN.md](DESIGN.md) for the reasons behind the structure and [ROADMAP.md](ROADMAP.md) for
what is in progress. Development specifications, the agreed plan for a larger change before it
is built, live in [docs/specs/](specs/), numbered (`0001-restructure-for-obot.md`).

## Setup

```bash
uv sync
uv run pre-commit install
```

Python 3.14 (pinned in `.python-version`); uv installs it if needed. Add dependencies with
`uv add` or `uv add --dev`, never by editing `uv.lock`.

Run the server from the working tree:

```bash
GOOGLE_OAUTH_ACCESS_TOKEN=<access-token> uv run mcp-google-tasks --transport stdio
```

```bash
uv run mcp-google-tasks --transport http --auth-mode token   # with token-mode variables set
```

## Layout

```text
src/mcp_google_tasks/
  __init__.py      package docstring and __version__ (hatch reads the version from here)
  __main__.py      python -m mcp_google_tasks -> cli.main()
  cli.py           cyclopts app: flags, ConfigurationError -> stderr and exit 2
  config.py        Settings (frozen) and load_settings(env, ...): pure, all validation here
  errors.py        GoogleTasksError and its subclasses; messages safe to show, never secrets
  logs.py          structlog to stderr, JSON when not a TTY, secret redaction
  auth.py          build_auth_provider(settings): one AuthProvider per mode; token storage
  credentials.py   CredentialSource protocol: RequestCredentials, StaticCredentials
  api.py           TasksApi protocol and GoogleTasksClient (the only code that calls Google)
  models.py        pydantic models for Google's resources and tool results
  tools.py         register_tools / register_resources: the MCP surface
  server.py        build_server(settings, http_client=None): wires it all, /health route
tests/             mirrors src/: test_<module>.py, shared fakes in conftest.py
catalog/           Obot catalog entry for the single-user containerized setup
contrib/kubernetes/ example Kustomize manifests (resource-server base, oauth-proxy overlay)
Dockerfile         multi-stage image, non-root
docs/              user docs, DESIGN.md, ROADMAP.md, specs/
```

A request flows `tools.py` → `TasksApi` → `GoogleTasksClient` → `CredentialSource` for the
bearer token → Google. `server.py` decides which `CredentialSource` the tools get and which auth
provider guards the HTTP endpoint, from the `Settings` built by `config.py`.

## Seams

Two protocols keep the layers apart and make every layer testable without Google:

- **`TasksApi`** (`api.py`): one async method per Google endpoint (`list_tasklists`,
  `insert_task`, `move_task`, `clear_tasks`, `get_userinfo`, ...). Tools receive one through an
  `api_factory` and never make HTTP calls themselves. `GoogleTasksClient` is the only
  implementation; it encodes and validates path segments, maps transport failures to
  `ConnectivityError` and non-2xx responses to `ApiError`, and on `401` asks its credentials to
  refresh and retries once.
- **`CredentialSource`** (`credentials.py`): `bearer_token()` returns the token for this call;
  `refresh()` returns `True` when it obtained a new one, so the caller may retry.
  `RequestCredentials` reads the verified token of the current MCP request (multi-user modes; it
  cannot refresh). `StaticCredentials` holds the configured access and refresh tokens (`token`
  mode), refreshes through Google's token endpoint under an `asyncio.Lock`, and honors
  `expires_in` minus 60 seconds.

Adding a tool means: a method on `TasksApi` and `GoogleTasksClient` if it needs a new endpoint,
the tool in `tools.py` with annotations and a docstring written for the model (when to call it,
what it returns, what to do on failure), registration only when not read-only if it writes, a row
in [tools.md](tools.md), and tests.

`config.load_settings` takes the environment as a mapping, so tests pass a dict instead of
patching `os.environ`. `build_server` accepts an `httpx.AsyncClient`, so tests inject one; in
production the server's lifespan owns a single pooled client.

## Tests

```bash
uv run pytest                      # full suite, branch coverage must be 100%
uv run pytest -k name --no-cov     # a subset, without the coverage gate
```

- **No test touches the network.** Google is faked in-process with an `httpx.MockTransport`
  handler that routes on method and path (Tasks API, token endpoint, userinfo). It lives in
  `tests/conftest.py` and is injected through `build_server(..., http_client=...)`. FastMCP's
  `GoogleTokenVerifier` uses its own HTTP client, so resource-server tests replace it with a
  stub instead.
- **Tools are tested through the server**: FastMCP's in-memory `Client` against
  `build_server(settings)` and the fake, including each failure path (API error, connectivity
  error, invalid input) and read-only registration.
- **Auth modes** are tested at their seams: settings validation per mode, the provider each mode
  builds, token-mode refresh on `401` and expiry, the `MCP_AUTH_TOKEN` verifier, and
  resource-server audience rejection.
- Assert on what a caller sees: return values, error types and messages, log events. Not on
  private state.
- `asyncio_mode = "auto"`, so `async def test_...` needs no decorator. Tests are plain
  functions, parametrized rather than copied.

Anything that can only be checked against the real Google, a real Obot or a published image is
listed in [ROADMAP.md](ROADMAP.md) under "Confirm against the real thing", with the command to
run.

## Gates

Both must pass before every commit:

```bash
uv run pre-commit run --all-files
```

```bash
uv run pytest
```

The hooks run ruff (lint with the `S` security and `PT` pytest rules, and format), rumdl
(Markdown, line length 100), taplo (TOML) and ty (strict, warnings are errors). Suppressions name
the rule and the reason. CI runs the same hooks, the tests on Linux and macOS, a wheel build
installed into a clean environment, and a container build that must answer `GET /health`.

On a pull request the Linux test job posts one comment, updated on every push, with the test
counts, the names and output of failed tests, and any file below 100% coverage; uncovered added
lines are annotated in the diff. Each test job also writes a coverage table to its run summary.
Pull requests from forks get only the summary, since their token cannot write comments.

## Container

Build and run the image locally:

```bash
docker build -t mcp-google-tasks:dev .
```

```bash
docker run --rm -p 8000:8000 --env-file .env mcp-google-tasks:dev
curl -fsS http://localhost:8000/health
```

The builder stage installs the locked dependencies with uv (`uv sync --locked --no-dev
--no-editable`); the runtime stage is `python:3.14-slim` running as uid 10001 with
`MCP_TRANSPORT=http`, `MCP_HOST=0.0.0.0`, `MCP_PORT=8000` and a `/data` volume. To check that the
image builds for both architectures the registry serves:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t mcp-google-tasks:dev .
```

## Releasing

1. Update [ROADMAP.md](ROADMAP.md), bump the version with `hatch version minor` (or `patch`,
   `major`), set the same version as the image tag in
   [catalog/google-tasks.yaml](../catalog/google-tasks.yaml), and commit.

2. Tag that commit `v<version>` and push the tag:

   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   ```

The release workflow checks that the tag matches `__version__`, runs CI, and publishes a GitHub
release with the sdist and wheel. The container image is published to
`ghcr.io/dcode/mcp-google-tasks` with the version as its tag, which the catalog entry now pins.
Nothing is published to PyPI.
