# Roadmap

Read this first when coming back to the project. The design and the reasons behind it are in
[DESIGN.md](DESIGN.md). Update this file in the same commit as the work it describes.

Last updated: 2026-11-22.

## Where things stand

- **Version:** 0.2.0, not yet tagged; no image has been published. 0.1.0 was never released.
- **State:** restructured per [spec 0001](specs/0001-restructure-for-obot.md): package layout,
  three auth modes, container image, Obot catalog entry, CI (including a container smoke test)
  and image publishing workflows, docs. Nothing has run against real Google or a real Obot yet.
- **Checked locally:** 434 tests pass against in-process fakes with 100% line and branch
  coverage; every pre-commit hook passes; `uv build` produces the sdist and wheel; the server
  starts in `token` mode over HTTP with a dummy token and answers `GET /health`.
- **Not checked yet:** the Docker image has never been built (Docker was not available), the
  three GitHub workflows (including the pull request coverage comment) have never run, and
  the catalog entry has not been imported into Obot.
- **Next action:** commit, push and let CI build the image; then work through "Confirm against
  the real thing", starting with `token` mode.

## Done

- [x] Project: uv, hatch, ruff, ty, rumdl, taplo, pre-commit, pytest with branch coverage,
      CI on Linux and macOS, tag releases, Dependabot
- [x] Restructure per [spec 0001](specs/0001-restructure-for-obot.md): modules split along the
      `TasksApi` and `CredentialSource` seams; `token`, `resource-server` and `oauth-proxy`
      modes decoupled from transport; token-mode refresh on `401`; `MCP_AUTH_TOKEN`; read-only
      mode hides write tools; RFC 3339 validation; resources fetch all pages; `/health`;
      cyclopts CLI and structlog; `python -m` entry point fixed; non-root multi-stage image;
      Obot single-user catalog entry; user and developer docs
- [x] PyPI publishing: the release workflow uploads to PyPI through trusted publishing before
      the GitHub release; the package description links resolve to GitHub; a test keeps the
      image tags in the catalog entry and Kubernetes example equal to `__version__`
- [x] Pull requests get one comment with test results, failed tests and coverage below 100%
      (MishaKav/pytest-coverage-comment), plus a coverage table in each test job's summary
- [x] Example Kubernetes manifests in [contrib/kubernetes/](../contrib/kubernetes/): a
      `resource-server` Kustomize base and an `oauth-proxy` overlay with a PVC; both render
      with `kubectl kustomize`

## Confirm against the real thing

Behavior that has automated tests but has never run against the real service, data or
environment. Give the exact command to run, and tick each one when it is checked. Placeholders
in angle brackets come from an untracked `.env` (see [.env.example](../.env.example)).

- [ ] **`token` mode over stdio with a real refresh token.** Every read tool, then create, patch
      to completed, move and delete a task in a scratch list:

  ```bash
  set -a && . ./.env && set +a
  npx @modelcontextprotocol/inspector uv run mcp-google-tasks --transport stdio
  ```

- [ ] **`token` mode refresh on `401`.** Start with an expired `GOOGLE_OAUTH_ACCESS_TOKEN` plus
      the refresh token; the first `list_tasklists` succeeds after one refresh (check the log).

- [ ] **`token` mode over HTTP with `MCP_AUTH_TOKEN`.** Without the header: `401`; with it: works.

  ```bash
  uv run mcp-google-tasks --transport http --auth-mode token
  curl -si -X POST http://localhost:8000/mcp | head -n 1
  npx @modelcontextprotocol/inspector   # URL http://localhost:8000/mcp, bearer <mcp-auth-token>
  ```

- [ ] **`oauth-proxy` mode with real Google sign-in**, with `filetree` storage, surviving a
      restart without re-sign-in:

  ```bash
  MCP_AUTH_MODE=oauth-proxy TOKEN_STORAGE_BACKEND=filetree TOKEN_STORAGE_DIR=./.data \
    uv run mcp-google-tasks --transport http
  npx @modelcontextprotocol/inspector   # URL http://localhost:8000/mcp, OAuth flow
  ```

- [ ] **`oauth-proxy` key rotation.** Change `STORAGE_ENCRYPTION_KEY`, restart: the client is
      asked to sign in again and no `500` appears.

- [ ] **`resource-server` mode with a real Google token** (token from the OAuth Playground with
      the same client): accepted; a token from another client ID: `401`.

  ```bash
  MCP_AUTH_MODE=resource-server uv run mcp-google-tasks --transport http
  curl -fsS http://localhost:8000/.well-known/oauth-protected-resource/mcp
  ```

- [ ] **Read-only mode.** `GOOGLE_TASKS_READONLY=true`: the tool list has only the five read
      tools, and Google sign-in asks only for `tasks.readonly`.

- [ ] **Container image.** Builds, runs as uid 10001, health check turns healthy:

  ```bash
  docker build -t mcp-google-tasks:dev .
  docker run -d --name gt --env-file .env -p 8000:8000 mcp-google-tasks:dev
  docker inspect --format '{{.State.Health.Status}} {{.Config.User}}' gt
  ```

- [ ] **Kubernetes example.** Apply [contrib/kubernetes/base](../contrib/kubernetes/base/) to
      a cluster: the pod is admitted under the `restricted` Pod Security Standard, starts with
      the read-only root filesystem, and turns ready. Then the `oauth-proxy` overlay: a sign-in
      survives `kubectl rollout restart`.

  ```bash
  kubectl apply -k contrib/kubernetes/base
  kubectl -n google-tasks rollout status deployment/google-tasks
  kubectl -n google-tasks logs deployment/google-tasks
  ```

- [ ] **First PyPI release.** The `v0.2.0` tag publishes `mcp-google-tasks` 0.2.0 (this also
      claims the reserved name), the description renders with working links, and it runs:

  ```bash
  uvx mcp-google-tasks@0.2.0 --version
  ```

- [ ] **Multi-arch image pull** after the first release, on an amd64 and an arm64 host:

  ```bash
  docker buildx imagetools inspect ghcr.io/dcode/mcp-google-tasks:0.2.0
  docker run --rm ghcr.io/dcode/mcp-google-tasks:0.2.0 --version
  ```

- [ ] **Obot single-user catalog import.** Add this repository as a catalog source (or create the
      entry from `catalog/google-tasks.yaml`), configure it with a refresh token, and call
      `list_tasklists` from an Obot chat. Confirm that Obot honors the fixed `value: token` on
      `MCP_AUTH_MODE` (no prompt) and the `options` dropdown on `GOOGLE_TASKS_READONLY`; both
      fields are taken from Obot's docs, not from a tested entry.

- [ ] **Obot Remote + Static OAuth.** Deploy in `resource-server` mode at a public HTTPS URL,
      register `https://<obot-host>/oauth/mcp/callback` with Google, add a Remote entry with
      Static OAuth ([deployment-obot.md](deployment-obot.md)), and connect as two different
      users; each sees only their own lists.

- [ ] **`oauth-proxy` behind Obot Remote without Static OAuth.** Does Obot complete discovery and
      dynamic client registration against this server's OAuth proxy on its own? Deploy in
      `oauth-proxy` mode with
      `MCP_ALLOWED_CLIENT_REDIRECT_URIS=https://<obot-host>/oauth/mcp/callback`, add a Remote
      entry without Static OAuth, and connect. Record the result in DESIGN.md either way.

## Next up

Rough order. Each item is one pull request or one focused commit.

1. Run the `token`-mode checks above and fix what they find.
2. Run the Obot checks (single-user, then Remote + Static OAuth) and record the Obot version
   used.
3. Tag v0.2.0; confirm the PyPI upload and that the image publishes for both architectures.

## Later

Ideas with enough shape to keep, not yet scheduled. Move an item to "Next up" when it is
ready to start, and record any design decision in DESIGN.md.

- A change-harvesting tool (tasks updated since a timestamp, across lists, including hidden
  and deleted), as the sibling TypeScript server's `diff_tasks` does.
- A `complete_task` convenience tool, so models do not need to know the `patch_task` form.
- Accept date-only `due` values (`YYYY-MM-DD`) and normalize to UTC midnight.
- `_FILE` variants for secrets (`GOOGLE_OAUTH_CLIENT_SECRET_FILE`, ...) for Docker and
  Kubernetes secret mounts.
- A multi-user Containerized Obot entry once obot-platform/obot#5372 is resolved.
- A Helm chart, if the example manifests in `contrib/kubernetes/` prove not enough.

## Open questions

Decisions waiting on information or on the owner. Remove an entry once it is answered, and
record the answer where it belongs (DESIGN.md or the code).

- Does Obot's Remote runtime work with `oauth-proxy` mode through dynamic client registration?
  If so, is there any reason to prefer it over `resource-server` with Static OAuth?
- Should `clear_completed_tasks` stay? It is destructive for any agent that mirrors completions
  (the sibling server leaves it out on purpose); read-only mode and `destructiveHint` are the
  current guard.
- Should the container image publish `latest`, or only version tags?
