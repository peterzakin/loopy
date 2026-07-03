# AGENTS.md — working in the Loopy framework repo

This is the **framework** repo (the `loopy-computer` PyPI package), not a Loopy project.
If you're *authoring a Loopy project* (workflows, registry.yml, sensors), run
`loopy docs` for the authoring reference and see the `AGENTS.md` that `loopy init`
scaffolds into every project; the top-level [`README.md`](README.md) covers the same
ground with more prose.

## Layout

| Path | What it is |
|---|---|
| `loopy_core/` | the compiler: project loading, DAG build, diagnostics (`compile/codes.py` is the stable error catalog), `manifest.schema.json` |
| `loopy_runtime/` | the engine: bus, sensors, sandbox providers (local/docker/daytona), harnesses (claude-code/codex/opencode), run state |
| `loopy_cli/` | the `loopy` binary (Typer). `scaffold.py` = `loopy init` templates; `docs_md/` = `loopy docs` content; `deploy/` = the docker stack |
| `examples/` | the cookbook — each subdirectory is a self-contained project; `examples/codefix/` is the canonical run-locally quickstart |
| `tests/` | pytest; `tests/conformance/` compiles + runs examples on the offline stub harness (no creds, no network) |
| `manifest.json` (root) | a **committed compiled artifact** of `examples/incidents/` — build output kept as a sample, not source; don't hand-edit it |

## Dev loop

```bash
uv run pytest -q                 # full suite; no creds or network needed
uv run pytest tests/conformance -q
uv run ruff check .              # lint (config in pyproject.toml; line-length 100)
uv run loopy <cmd>               # exercise the CLI from source
```

## Conventions

- **Diagnostic codes are a contract.** Every compile error/warning is a `LOOPY-Exxx`/`Wxxx`
  from `loopy_core/compile/codes.py` — codes are never renumbered, each has a one-line
  description in `DESCRIPTIONS` (rendered by `loopy docs errors`), and the
  golden-negative suite has one fixture per code. A new code isn't done without both.
- **The scaffold must compile green.** `loopy init` output is guarded by
  `tests/test_init_scaffold.py`; if you touch `loopy_cli/scaffold.py`, run it.
- **Scaffolded docs live in `scaffold.py`; shipped docs live in `loopy_cli/docs_md/`.**
  If you change the authoring model (frontmatter keys, registry shape, event types),
  update the README, `docs_md/authoring.md`, and the scaffold's `AGENTS.md` template
  together — they are three renderings of the same contract.
- Heavy deps are imported lazily inside CLI command bodies so `loopy compile` stays
  runtime-free; keep new imports out of `loopy_cli/__init__.py`'s module top.
- Add a regression test alongside any bug fix.
