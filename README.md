# mcp-google-tasks

Google Tasks MCP server for Obot: multi-user OAuth or single-user tokens.

An [MCP](https://modelcontextprotocol.io) server that exposes the whole Google Tasks REST API v1
(task lists and tasks: list, get, create, replace, patch, delete, move, clear completed) as 15
tools and 2 resources. It runs locally over stdio for one person, or as a container over
streamable HTTP for one person or for many, and it was built to be hosted by
[Obot](https://obot.ai) but should work for other self-hosted MCP environments.

## What it does

- **Full API surface.** Task-list CRUD, task CRUD, move and reparent across lists, and clearing
  completed tasks. Every tool returns structured output with a schema. See
  [docs/tools.md](docs/tools.md).
- **Three ways to authenticate**, chosen independently of the transport (table below).
- **Read-only mode.** `GOOGLE_TASKS_READONLY=true` requests the `tasks.readonly` scope and does
  not register the write tools at all, so a model never sees them.
- **Safe to host.** Non-root container, `GET /health` for probes, Fernet-encrypted token storage
  on a volume or in Redis, audience-checked bearer tokens in gateway mode, logs on stderr with
  secrets redacted.

## Authentication modes

Set the mode with `MCP_AUTH_MODE` (or `--auth-mode`). The default is `token` over stdio and
`oauth-proxy` over HTTP.

| Mode | Users | Who runs the Google OAuth flow | Transport | Use it when |
|---|---|---|---|---|
| `token` | One | You, once, ahead of time | stdio or HTTP | Local use, or an Obot single-user catalog entry |
| `resource-server` | Many | A gateway (Obot) in front of this server | HTTP | Multi-user on Obot (Remote runtime with Static OAuth) |
| `oauth-proxy` | Many | This server, as an OAuth proxy for MCP clients | HTTP | Multi-user without a gateway, for MCP clients that do OAuth themselves |

[docs/authentication.md](docs/authentication.md) covers the Google Cloud setup every mode needs
and how to get a refresh token for `token` mode.

## Quickstart

All three start from a Google Cloud project with the Tasks API enabled and an OAuth client:
follow [docs/authentication.md](docs/authentication.md) first.

### Local, single user, over stdio

Get a refresh token (see [docs/authentication.md](docs/authentication.md#single-user-refresh-token)),
then register the server with your MCP client. With Claude Code:

```bash
claude mcp add google-tasks \
  -e GOOGLE_OAUTH_CLIENT_ID=<client-id> \
  -e GOOGLE_OAUTH_CLIENT_SECRET=<client-secret> \
  -e GOOGLE_OAUTH_REFRESH_TOKEN=<refresh-token> \
  -- uvx --from git+https://github.com/dcode/mcp-google-tasks mcp-google-tasks --transport stdio
```

Or any client that reads the common `mcpServers` JSON shape, running from a clone with
`uv run`:

```json
{
  "mcpServers": {
    "google-tasks": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/mcp-google-tasks",
        "mcp-google-tasks", "--transport", "stdio"
      ],
      "env": {
        "GOOGLE_OAUTH_CLIENT_ID": "<client-id>",
        "GOOGLE_OAUTH_CLIENT_SECRET": "<client-secret>",
        "GOOGLE_OAUTH_REFRESH_TOKEN": "<refresh-token>"
      }
    }
  }
}
```

Over stdio the auth mode defaults to `token`, so nothing else is needed.

### Docker, single user, over HTTP

Copy [.env.example](.env.example) to `.env`, uncomment and fill in the `token` section, and
set `MCP_AUTH_TOKEN` to a long random string so only your client can call the server. Replace
`<version>` with a released version:

```bash
docker run --rm -p 8000:8000 --env-file .env -e MCP_AUTH_MODE=token \
  ghcr.io/dcode/mcp-google-tasks:<version>
```

The MCP endpoint is `http://localhost:8000/mcp` and the health check is
`http://localhost:8000/health`. Point a client at it with the bearer token:

```bash
claude mcp add --transport http google-tasks http://localhost:8000/mcp \
  --header "Authorization: Bearer <mcp-auth-token>"
```

### Multi-user

On Obot, run the image in `resource-server` mode at a public HTTPS address and add it as a
**Remote** catalog entry with **Static OAuth**; Obot runs the Google sign-in for each user.
[docs/deployment-obot.md](docs/deployment-obot.md) has the full procedure, including why the
Containerized runtime cannot do this yet.

```bash
docker run -d -p 8000:8000 \
  -e MCP_AUTH_MODE=resource-server \
  -e MCP_BASE_URL=https://tasks.example.com \
  -e GOOGLE_OAUTH_CLIENT_ID=<client-id> \
  ghcr.io/dcode/mcp-google-tasks:<version>
```

For Kubernetes, [contrib/kubernetes/](contrib/kubernetes/) has example Kustomize manifests
for this mode and for `oauth-proxy`.

Without a gateway, use `oauth-proxy` mode: the server itself sends each user through Google
sign-in and issues its own tokens to MCP clients. Give it a stable signing key and persistent,
encrypted storage so sign-ins survive restarts:

```bash
docker run -d -p 8000:8000 -v google-tasks-data:/data \
  -e MCP_AUTH_MODE=oauth-proxy \
  -e MCP_BASE_URL=https://tasks.example.com \
  -e GOOGLE_OAUTH_CLIENT_ID=<client-id> \
  -e GOOGLE_OAUTH_CLIENT_SECRET=<client-secret> \
  -e JWT_SIGNING_KEY=<random-string> \
  -e TOKEN_STORAGE_BACKEND=filetree \
  -e STORAGE_ENCRYPTION_KEY=<fernet-key> \
  ghcr.io/dcode/mcp-google-tasks:<version>
```

## Documentation

| Document | For | Covers |
|---|---|---|
| [docs/authentication.md](docs/authentication.md) | Operators | Google Cloud setup, scopes, refresh tokens, key generation and rotation |
| [docs/configuration.md](docs/configuration.md) | Operators | Every environment variable and CLI flag, per mode, and the startup errors |
| [docs/deployment-obot.md](docs/deployment-obot.md) | Obot admins, self-hosters | Single-user and multi-user Obot setups, storage, hardening |
| [contrib/kubernetes/](contrib/kubernetes/) | Kubernetes operators | Example Kustomize manifests for `resource-server` and `oauth-proxy` |
| [docs/tools.md](docs/tools.md) | MCP users | Every tool and resource, its parameters, and read-only visibility |
| [docs/development.md](docs/development.md) | Contributors | Layout, seams, tests, release, local container builds |

## Design and roadmap

[docs/DESIGN.md](docs/DESIGN.md) records the design: the problem, the decisions made and the
reasons for them. [docs/ROADMAP.md](docs/ROADMAP.md) tracks what is done, what still needs
checking, and what comes next. Development specifications live in [docs/specs/](docs/specs/).

## Development

Install the git hooks once after cloning:

```bash
uv run pre-commit install
```

The hooks lint and format Python (ruff), Markdown (rumdl) and TOML (taplo), and type-check
with ty. To run everything by hand:

```bash
uv run pre-commit run --all-files
```

```bash
uv run pytest
```

[docs/development.md](docs/development.md) explains the code layout and the test strategy.

### Continuous integration

GitHub Actions runs on every pull request and on every push to `main`:

| Job | What it does |
|---|---|
| Lint, format and type-check | All pre-commit hooks: ruff, rumdl, taplo and ty |
| Test | The test suite with the coverage threshold, on Linux and macOS; on pull requests, one comment with test results and coverage |
| Build | Builds the sdist and wheel, installs the wheel into a clean environment, and uploads both as an artifact |
| Container | Builds the Docker image (not pushed), runs it in `token` mode and waits for `GET /health` to return 200 |

A separate container workflow publishes multi-arch images to `ghcr.io/dcode/mcp-google-tasks`:
`edge` and `sha-<short>` on every push to `main`, and `<version>`, `<major>.<minor>` and
`latest` on a `v*` tag.

Dependabot opens weekly pull requests for GitHub Actions, Python dependencies and the Docker
base images.

### Releasing

1. Bump the version: `hatch version minor` (or `patch`, `major`), then commit.

2. Tag that commit with the same version, prefixed with `v`, and push the tag:

   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   ```

The release workflow checks that the tag matches `__version__`, runs the full CI suite, and
publishes a GitHub release with the sdist and wheel attached and generated release notes. It
does not publish to PyPI. The container image is published to `ghcr.io/dcode/mcp-google-tasks`;
see [docs/development.md](docs/development.md#releasing).

### With hatch

The same checks are available as [hatch](https://hatch.pypa.io/) commands, which manage their
own environments:

| Command | What it runs |
|---|---|
| `hatch test` | The test suite with branch coverage and the 100% threshold |
| `hatch test -k name --no-cov` | A subset of tests, without the coverage threshold |
| `hatch fmt` | ruff lint (with fixes) and format, using this project's settings |
| `hatch fmt --check` | The same, reporting only |
| `hatch run types` | ty type check |
| `hatch run hooks` | Every pre-commit hook on all files |
| `hatch run check` | Hooks, then tests |
| `hatch build` | Build the sdist and wheel into `dist/` |

Tool settings live in `pyproject.toml`, except taplo's, which it only reads from
`.taplo.toml`.

## License

[Apache License 2.0](LICENSE).
