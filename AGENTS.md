# mcp-google-tasks

Google Tasks MCP server for Obot: multi-user OAuth or single-user tokens.

## Start here

1. Read [docs/ROADMAP.md](docs/ROADMAP.md): where things stand, what is next, and what is
   waiting on a check or a decision.
2. Read the relevant part of [docs/DESIGN.md](docs/DESIGN.md) before changing how something
   works.
3. Read [docs/development.md](docs/development.md) for the layout, the `TasksApi` and
   `CredentialSource` seams, and the test strategy.
4. Development specifications live in [docs/specs/](docs/specs/), numbered
   (`0001-restructure-for-obot.md`). An accepted spec is the authority for the work it
   describes; read it fully before starting that work.

## Rules

- Specifications for development go in `docs/specs/NNNN-short-title.md`, never elsewhere.
  Write one before a change that spans several modules or changes behavior users rely on.

- Update `docs/ROADMAP.md` (including "Last updated") in the same commit as the work it
  describes. A new decision, and the reason for it, goes in `docs/DESIGN.md`.

- User-facing changes update the user docs in the same commit: environment variables in
  `docs/configuration.md` and `.env.example`, tools in `docs/tools.md`, auth in
  `docs/authentication.md`, Obot in `docs/deployment-obot.md`.

- Before every commit both must pass:

  ```bash
  uv run pre-commit run --all-files
  ```

  ```bash
  uv run pytest
  ```

- Every behavior change comes with a test. Branch coverage must stay at 100%; a line that
  truly cannot be tested gets `# pragma: no cover - <why>`. Tests never contact a real
  service: Google is faked with `httpx.MockTransport` in `tests/conftest.py`.

- Tools never make HTTP calls themselves; they go through `TasksApi`. Every tool declares
  annotations, and write tools are registered only when not read-only.

- FastMCP is 4.2.x, not 2.x or 3.x. Check a signature in the installed package under `.venv`
  before using an API.

- Strict typing: annotate everything, `X | None` not `Optional`, built-in generics. ty
  warnings are errors.

- `async` for I/O. Google-style docstrings on every module and public object.

- ruff runs with the security (`S`) and pytest-style (`PT`) rules on. Suppressions name the
  rule and the reason: `# noqa: S603 - <why>`.

- Logs go to stderr only (stdout carries stdio MCP) and never contain secrets.

- Add dependencies with `uv add` / `uv add --dev`; never edit `uv.lock` by hand.

- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `build:`,
  `ci:`). American English.

- Never commit secrets, real hostnames or account details; use `example.com` placeholders and
  untracked `.env` files.

## Commands

| Task | Command |
|---|---|
| Install / sync | `uv sync` |
| All hooks | `uv run pre-commit run --all-files` |
| Tests with coverage | `uv run pytest` |
| One test, no coverage gate | `uv run pytest -k name --no-cov` |
| Type check | `uv run ty check` |
| Hooks then tests | `hatch run check` |
| Run over stdio | `uv run mcp-google-tasks --transport stdio` |
| Run over HTTP | `uv run mcp-google-tasks --transport http` |
| Build | `uv build` |
| Container | `docker build -t mcp-google-tasks:dev .` |
| Release | `hatch version minor`, commit, `git tag v<version>`, push the tag |
