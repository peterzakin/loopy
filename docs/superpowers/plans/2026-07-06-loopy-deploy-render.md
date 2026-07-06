# `loopy deploy render` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One command that deploys a loopy project to Render end to end: guided preflight, loopy-init-style setup wizard, service create-or-update via the Render REST API, secret files + env vars pushed, deploy polled to live, `LOOPY_PUBLIC_URL` written back, webhooks auto-registered.

**Architecture:** A new `loopy_cli/render.py` module registered on the shared `deploy_app` typer group (which moves to a neutral `loopy_cli/deploy_cmd.py`). All Render calls go through a thin `RenderClient` on `httpx` (already a core dep). Reuses bootstrap's `_StatusBoard`/`wait_until_serving`, the secrets helpers (`collect_secret_files`, `write_control_plane_env`), and `sync_github_webhooks`. Spec: `docs/superpowers/specs/2026-07-06-loopy-deploy-render-design.md`.

**Tech Stack:** Python 3.12, typer, httpx (+ `httpx.MockTransport` in tests), pytest with `typer.testing.CliRunner`.

## Global Constraints

- **No new dependencies.** `httpx>=0.27` is already in `pyproject.toml`; everything else is stdlib.
- **Lazy imports in command bodies** — `loopy compile` must never pay for deploy code (see `bootstrap.py`'s pattern).
- **Secrets are never logged or echoed** — key prompts use `hide_input=True`; env/secret values never appear in output.
- **House style for interactive output:** two-space indent, `typer.style("✓", fg=typer.colors.GREEN)` / `"!"` yellow / `"✗"` red, `typer.prompt`/`typer.confirm`, never re-ask for recorded values.
- **Headless-clean:** every prompt has a flag/env equivalent; a non-TTY run with missing input exits 1 naming the flag, before any network mutation.
- **Run tests with:** `uv run pytest tests/<file> -q` from the repo root. Full check: `uv run pytest -q`.
- **Commit style:** imperative subject like existing history (`cli: …`, `admin: …`); this feature uses `deploy render: …` or `cli: …`.

## File Structure

| File | Responsibility |
|---|---|
| `loopy_cli/deploy_cmd.py` (create) | Neutral home for the shared `deploy_app` typer group |
| `loopy_cli/render.py` (create) | Everything Render: `RenderClient`, preflight checks, wizard, encoding, the `render` command |
| `loopy_cli/bootstrap.py` (modify) | Stop defining `deploy_app`; import it from `deploy_cmd` |
| `loopy_cli/__init__.py` (modify) | Import both provider modules; extract `deploy_env_block()`; Dockerfile shim; init 3-way hosting choice |
| `loopy_cli/deploy_target.py` (modify) | `TARGET_RENDER`, `RENDER_SERVICE_ID_ENV` |
| `tests/test_deploy_render.py` (create) | Unit + command tests for the render target |
| `tests/test_deploy_generators.py` (modify) | `deploy_env_block` + Dockerfile-shim assertions |
| `tests/test_init_scaffold.py` (modify) | 3-way hosting choice |
| `loopy_cli/docs_md/deployment.md`, `docs/design/render-deploy.md` | Docs |

---

### Task 1: Move `deploy_app` to a neutral `loopy_cli/deploy_cmd.py`

**Files:**
- Create: `loopy_cli/deploy_cmd.py`
- Modify: `loopy_cli/bootstrap.py:44-47` (delete the `deploy_app = typer.Typer(...)` block, import instead)
- Modify: `loopy_cli/__init__.py:30` (`from loopy_cli.bootstrap import deploy_app`)

**Interfaces:**
- Produces: `loopy_cli.deploy_cmd.deploy_app` (a `typer.Typer`) — Task 10 registers `render` on it; `bootstrap.py` keeps registering `bootstrap` on it.

- [ ] **Step 1: Create `loopy_cli/deploy_cmd.py`**

```python
"""`loopy deploy` — the shared command group deploy targets register into.

One subcommand per deploy target (see `loopy_cli.deploy_target`): `bootstrap`
(the AWS starter stack, `loopy_cli.bootstrap`) and `render` (Render.com,
`loopy_cli.render`). The group lives in its own module so no provider module
owns it; `loopy_cli/__init__.py` imports each provider module for the side
effect of registering its command.
"""

from __future__ import annotations

import typer

deploy_app = typer.Typer(
    no_args_is_help=True,
    help="Provision hosting for the engine on a named deploy target "
    "(`loopy deploy bootstrap`, `loopy deploy render`).",
)
```

- [ ] **Step 2: Point `bootstrap.py` at it**

In `loopy_cli/bootstrap.py`, delete lines 44–47 (the `deploy_app = typer.Typer(...)` assignment) and add to the imports below `import typer`:

```python
from loopy_cli.deploy_cmd import deploy_app
```

- [ ] **Step 3: Fix the import in `loopy_cli/__init__.py`**

Replace line 30 (`from loopy_cli.bootstrap import deploy_app`) with:

```python
from loopy_cli import bootstrap as _bootstrap_module  # noqa: F401 - registers `loopy deploy bootstrap`
from loopy_cli.deploy_cmd import deploy_app
```

(The module import is what registers the `bootstrap` command on the group — keep the noqa comment so it isn't "cleaned up".)

- [ ] **Step 4: Run the existing deploy tests**

Run: `uv run pytest tests/test_deploy_bootstrap.py tests/test_deploy_target.py -q`
Expected: all PASS (pure relocation, no behavior change). Also run `uv run loopy deploy --help` and confirm `bootstrap` is listed.

- [ ] **Step 5: Commit**

```bash
git add loopy_cli/deploy_cmd.py loopy_cli/bootstrap.py loopy_cli/__init__.py
git commit -m "cli: move the deploy command group to a neutral module"
```

---

### Task 2: `TARGET_RENDER` + `RENDER_SERVICE_ID_ENV` in `deploy_target.py`

**Files:**
- Modify: `loopy_cli/deploy_target.py`
- Test: `tests/test_deploy_target.py`

**Interfaces:**
- Produces: `TARGET_RENDER = "render"`, `RENDER_SERVICE_ID_ENV = "LOOPY_RENDER_SERVICE_ID"` — used by Tasks 10–12.

- [ ] **Step 1: Write the failing test** (append to `tests/test_deploy_target.py`)

```python
def test_render_target_constants():
    from loopy_cli.deploy_target import RENDER_SERVICE_ID_ENV, TARGET_RENDER

    assert TARGET_RENDER == "render"
    assert RENDER_SERVICE_ID_ENV == "LOOPY_RENDER_SERVICE_ID"
```

- [ ] **Step 2: Run it** — `uv run pytest tests/test_deploy_target.py -q` — Expected: FAIL (ImportError).

- [ ] **Step 3: Implement.** In `loopy_cli/deploy_target.py` after `TARGET_BOOTSTRAP = "bootstrap"` (line 37):

```python
TARGET_RENDER = "render"

# Written by `loopy deploy render` alongside LOOPY_PUBLIC_URL: the Render service id, a
# client-side hint (fast idempotent lookup on re-deploy, the --destroy target). The
# engine never reads it.
RENDER_SERVICE_ID_ENV = "LOOPY_RENDER_SERVICE_ID"
```

Also update the module docstring: in the `byo` bullet (line 13) drop "Render" from the examples (`a platform (Fly, Railway)`), and add a bullet:

```
- `render` — Render.com: `loopy deploy render` creates the web service from your repo
  via Render's API and writes LOOPY_PUBLIC_URL back at deploy, like bootstrap.
```

and in the bootstrap bullet change "(`aws`, `render`, …)" to "(`aws`, …; `render` is now real — see above)".

- [ ] **Step 4: Run** — `uv run pytest tests/test_deploy_target.py -q` — Expected: PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "deploy render: name the target and its loopy.env hint key"`

---

### Task 3: Extract `deploy_env_block()` from `loopy env`

**Files:**
- Modify: `loopy_cli/__init__.py:2366-2443` (`_ENV_DEPLOY_SKIP`, the `env` command)
- Test: `tests/test_deploy_generators.py`

**Interfaces:**
- Produces: `loopy_cli.deploy_env_block(root: Path) -> dict[str, str]` — sorted, parsed (quotes stripped), laptop-side keys removed. Task 10 calls it for the platform env push.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_deploy_generators.py`)

```python
# ── deploy_env_block ────────────────────────────────────────────────────────────


def test_deploy_env_block_skips_laptop_and_render_keys(tmp_path):
    from loopy_cli import deploy_env_block

    project = _project(tmp_path)
    (project / "loopy.env").write_text(
        _LOOPY_ENV
        + "RENDER_API_KEY=rnd_secret\n"
        + "LOOPY_RENDER_SERVICE_ID=srv-123\n"
    )
    block = deploy_env_block(project)
    assert block["DAYTONA_API_KEY"] == "dt-real"
    assert block["LOOPY_ADMIN_TOKEN"] == "loopy_sk_admin"
    for absent in ("LOOPY_PUBLIC_URL", "REDIS_URL", "RENDER_API_KEY", "LOOPY_RENDER_SERVICE_ID"):
        assert absent not in block


def test_deploy_env_block_strips_quotes(tmp_path):
    # Regression: a quoted dotenv value must reach the platform unquoted — a stray
    # `"` in e.g. SENTRY_AUTH_TOKEN breaks the consumer at run time, not at set time.
    from loopy_cli import deploy_env_block

    project = _project(tmp_path)
    (project / "loopy.env").write_text('DAYTONA_API_KEY="dt-quoted"\n')
    assert deploy_env_block(project)["DAYTONA_API_KEY"] == "dt-quoted"
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_deploy_generators.py -q -k deploy_env_block` — Expected: FAIL (ImportError).

- [ ] **Step 3: Implement.** In `loopy_cli/__init__.py`, extend `_ENV_DEPLOY_SKIP` (line 2369) and add the helper right below it:

```python
_ENV_DEPLOY_SKIP = frozenset(
    {
        "LOOPY_PUBLIC_URL",
        "REDIS_URL",
        "LOOPY_ADMIN_TOKEN_NEXT",
        # Render-target keys: the API key drives the deploy from the laptop and the
        # service id is a client-side hint — the engine needs neither.
        "RENDER_API_KEY",
        "LOOPY_RENDER_SERVICE_ID",
    }
)


def deploy_env_block(root: Path) -> dict[str, str]:
    """The engine env block a hosted deploy sets on the platform, from local loopy.env.

    Everything in the control-plane dotenv minus laptop-side/platform-specific keys
    (`_ENV_DEPLOY_SKIP`). Values come from the dotenv parser, so quoted values arrive
    unquoted — platforms store what they're given verbatim, and a stray quote breaks
    the consumer at run time. Shared by `loopy env` (prints it) and `loopy deploy
    render` (pushes it via API).
    """
    from loopy_runtime.secrets import load_control_plane_env

    control = load_control_plane_env(root)
    return {key: control[key] for key in sorted(control) if key not in _ENV_DEPLOY_SKIP}
```

Then refactor the `env` command body (lines 2427–2433) to use it:

```python
    _compile_or_exit(path)
    root = Path(path)
    block = deploy_env_block(root)

    lines = ["# --- Control plane (engine) — infra creds; set these on the platform ---"]
    lines.extend(f"{key}={block[key]}" for key in block)
```

(keep the trailing `REDIS_URL` placeholder + `LOOPY_PUBLIC_URL` comment lines unchanged, and drop the now-unused `load_control_plane_env` import from the command body).

- [ ] **Step 4: Run** — `uv run pytest tests/test_deploy_generators.py -q` — Expected: PASS (including the existing `loopy env` tests).

- [ ] **Step 5: Commit** — `git add -A && git commit -m "cli: extract the platform env block from loopy env for reuse"`

---

### Task 4: Dockerfile template — secret-file shim + `$PORT`

**Files:**
- Modify: `loopy_cli/__init__.py:2323-2347` (`_DOCKERFILE_TEMPLATE`)
- Test: `tests/test_deploy_generators.py`

**Interfaces:**
- Produces: generated Dockerfiles whose start command (a) links `/etc/secrets/<name-with-__>` to `./<name-with-/>` before exec (Render mounts secret files flat at `/etc/secrets/`; loopy's `env_file` paths are nested), and (b) binds `${PORT:-8000}` (Render injects `PORT`). Inert off Render: no `/etc/secrets`, no `PORT` → behavior unchanged.

- [ ] **Step 1: Update the existing test + add the shim test.** In `tests/test_deploy_generators.py`, `test_dockerfile_stdout_is_version_pinned` currently asserts `'"--in-process"' in result.stdout` (JSON-array CMD). Replace those CMD assertions and add:

```python
def test_dockerfile_stdout_is_version_pinned(tmp_path):
    result = runner.invoke(app, ["dockerfile", str(_project(tmp_path)), "--stdout"])
    assert result.exit_code == 0
    assert f'"loopy-computer[redis]=={__version__}"' in result.stdout
    assert "--in-process" in result.stdout
    assert "--bus" not in result.stdout  # no bus flag; auto-selected from REDIS_URL
    assert "RUN loopy compile ." in result.stdout  # build gate
    assert not (tmp_path / "Dockerfile").exists()


def test_dockerfile_start_command_links_secret_files_and_honors_port(tmp_path):
    result = runner.invoke(app, ["dockerfile", str(_project(tmp_path)), "--stdout"])
    assert result.exit_code == 0
    assert "/etc/secrets" in result.stdout  # Render secret files get linked into place
    assert "s|__|/|g" in result.stdout  # flat names decode back to nested paths
    assert "${PORT:-8000}" in result.stdout  # platform-injected port wins
    assert "/state/state.db" in result.stdout  # a mounted /state disk gets durable history
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_deploy_generators.py -q -k dockerfile` — Expected: the new test FAILS.

- [ ] **Step 3: Implement.** Replace the tail of `_DOCKERFILE_TEMPLATE` (the comment block at line 2341 through `CMD [...]` at 2346) with:

```python
# One long-lived process: sensor webhooks + scheduler + runtime. No --bus flag — the engine uses
# Redis when REDIS_URL is set (managed Redis in production) and the in-process bus otherwise. Run
# history lives at .loopy/state.db; for durability across redeploys, mount a volume and add
# `--state-path /state/state.db` to the command below.
#
# The sh wrapper does two platform-friendly things before exec'ing the engine:
#   * Render mounts Secret Files flat at /etc/secrets/<name>; loopy's env_file paths are
#     nested (secrets/base.env). `loopy deploy render` uploads each file with `/` encoded
#     as `__`, and the loop below links every one back to the path the manifest names.
#     (Paths containing a literal `__` are rejected at deploy time, so the decode is safe.)
#     No /etc/secrets (local docker, other platforms) -> the loop is a no-op.
#   * $PORT is honored when the platform injects one (Render does); 8000 otherwise.
#   * A mounted /state disk (Render `--disk-gb`, or any platform volume) moves run
#     history to /state/state.db so it survives redeploys; no disk -> default path.
ENTRYPOINT []
CMD ["/bin/sh", "-c", "\\
if [ -d /etc/secrets ]; then \\
  for f in /etc/secrets/*; do \\
    [ -f \\"$f\\" ] || continue; \\
    rel=$(basename \\"$f\\" | sed 's|__|/|g'); \\
    mkdir -p \\"$(dirname \\"./$rel\\")\\"; \\
    ln -sf \\"$f\\" \\"./$rel\\"; \\
  done; \\
fi; \\
if [ -d /state ]; then STATE_ARGS=\\"--state-path /state/state.db\\"; else STATE_ARGS=\\"\\"; fi; \\
exec loopy run manifest.json --in-process --root . --host 0.0.0.0 --port \\"${{PORT:-8000}}\\" $STATE_ARGS"]
"""
```

**Careful:** the template goes through `.format(version=…)`, so the shell's `${PORT:-8000}` must be written `${{PORT:-8000}}` in the Python source (doubled braces), and every `"` inside the CMD JSON string is `\\"` in the Python triple-quoted source (one backslash in the emitted file). After implementing, run `uv run loopy dockerfile . --stdout | tail -20` and eyeball that the emitted CMD is valid JSON-array-with-one-sh-string and the braces came out single.

- [ ] **Step 4: Run** — `uv run pytest tests/test_deploy_generators.py tests/test_run_container_build.py -q` — Expected: PASS. (`test_run_container_build.py` covers the other Dockerfiles; it should be untouched but confirm.)

- [ ] **Step 5: Commit** — `git add -A && git commit -m "cli: dockerfile start command links Render secret files and honors \$PORT"`

---

### Task 5: `loopy_cli/render.py` — secret-file name encoding

**Files:**
- Create: `loopy_cli/render.py`
- Test: `tests/test_deploy_render.py` (create)

**Interfaces:**
- Produces: `encode_secret_file_name(rel_path: str) -> str` (raises `ValueError` on `__` in the path). Task 10 uses it to build the secret-files upload; the Task 4 shim is its inverse.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_deploy_render.py`:

```python
"""`loopy deploy render` — the Render.com deploy target."""

from __future__ import annotations

import pytest

# ── secret-file name encoding ───────────────────────────────────────────────────


def test_encode_secret_file_name_flattens_paths():
    from loopy_cli.render import encode_secret_file_name

    assert encode_secret_file_name("loopy.env") == "loopy.env"
    assert encode_secret_file_name("secrets/base.env") == "secrets__base.env"
    assert encode_secret_file_name("sensors/.env") == "sensors__.env"
    assert encode_secret_file_name("a/b/c.env") == "a__b__c.env"


def test_encode_secret_file_name_rejects_ambiguous_paths():
    from loopy_cli.render import encode_secret_file_name

    with pytest.raises(ValueError, match="__"):
        encode_secret_file_name("secrets/my__file.env")
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_deploy_render.py -q` — Expected: FAIL (no module `loopy_cli.render`).

- [ ] **Step 3: Implement.** Create `loopy_cli/render.py`:

```python
"""`loopy deploy render` — provision the engine on Render.com from the operator's API key.

The Render deploy target (see `loopy_cli.deploy_target`): Render builds the project's
generated Dockerfile from its git repo (git-push CD), so this command's job is the
gap the dashboard would otherwise fill — verify the repo/Dockerfile/secrets are
deployable (preflight), create-or-update the web service via `api.render.com/v1`,
push env vars + secret files, poll the deploy to live, write LOOPY_PUBLIC_URL back
to loopy.env, and register GitHub webhooks. Design: docs/design/render-deploy.md.

httpx is a core dependency (the admin proxy uses it); imported lazily in the client
so `loopy compile` never loads it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import typer

from loopy_cli.deploy_cmd import deploy_app

RENDER_API_URL = "https://api.render.com/v1"
RENDER_API_KEY_ENV = "RENDER_API_KEY"
RENDER_KEYS_URL = "https://dashboard.render.com/settings#api-keys"

# Render Secret Files mount flat at /etc/secrets/<name> (Docker services see them ONLY
# there), so nested env_file paths encode `/` as `__`; the generated Dockerfile's start
# command decodes (see _DOCKERFILE_TEMPLATE in loopy_cli). Kept in sync by the tests.
SECRET_NAME_SEP = "__"


def encode_secret_file_name(rel_path: str) -> str:
    """Flatten a project-relative env-file path into a Render secret-file name.

    Lossy if a segment itself contains `__` — rejected here so the deploy fails loudly
    at upload time instead of the boot shim linking the wrong path.
    """
    if SECRET_NAME_SEP in rel_path:
        raise ValueError(
            f"secret file path {rel_path!r} contains {SECRET_NAME_SEP!r}, which the "
            "flat-name encoding reserves for '/'; rename the file"
        )
    return rel_path.replace("/", SECRET_NAME_SEP)
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_deploy_render.py -q` — Expected: PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "deploy render: secret-file name encoding"`

---

### Task 6: `RenderClient` + `RenderAPIError`

**Files:**
- Modify: `loopy_cli/render.py`
- Test: `tests/test_deploy_render.py`

**Interfaces:**
- Produces (all used by Tasks 9–11):
  - `RenderAPIError(status: int, message: str, hint: str = "")` — `.status`, `.message`, `.hint`; str form is `"Render API error <status>: <message>"` plus an indented `→ hint` line when there is one.
  - `RenderClient(api_key, *, transport=None)` with methods `owners() -> list[dict]`, `find_service(name) -> dict | None`, `get_service(id) -> dict | None` (None on 404), `create_service(payload) -> dict`, `update_service(id, payload) -> dict`, `put_env_vars(id, env: dict[str,str])`, `put_secret_files(id, files: dict[str,str])`, `trigger_deploy(id) -> dict`, `get_deploy(id, deploy_id) -> dict`, `delete_service(id)`.

**Endpoint note:** paths below follow the current public API (`GET /owners`, `GET/POST /services`, `PUT /services/{id}/env-vars`, `PUT /services/{id}/secret-files`, `POST /services/{id}/deploys`, `DELETE /services/{id}`). Before starting this task, confirm the secret-files path and body shape against api-docs.render.com (context7 library `/websites/render`, query "REST API update secret files on a service"); if it differs, change ONLY `put_secret_files` and its test to match — the rest of the plan consumes the method, not the endpoint.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_deploy_render.py`):

```python
import json

import httpx

# ── RenderClient ────────────────────────────────────────────────────────────────


def _client(handler):
    from loopy_cli.render import RenderClient

    return RenderClient("rnd_test", transport=httpx.MockTransport(handler))


def test_client_sends_bearer_and_unwraps_owners():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[{"owner": {"id": "own-1", "name": "Vivek"}, "cursor": "c"}])

    owners = _client(handler).owners()
    assert seen["auth"] == "Bearer rnd_test"
    assert seen["url"] == "https://api.render.com/v1/owners"
    assert owners == [{"id": "own-1", "name": "Vivek"}]


def test_client_maps_401_to_mint_hint():
    from loopy_cli.render import RENDER_KEYS_URL, RenderAPIError

    def handler(request):
        return httpx.Response(401, json={"message": "unauthorized"})

    with pytest.raises(RenderAPIError) as excinfo:
        _client(handler).owners()
    assert excinfo.value.status == 401
    assert RENDER_KEYS_URL in excinfo.value.hint


def test_find_service_matches_exact_name_only():
    def handler(request):
        assert request.url.params["name"] == "loopy-demo"
        return httpx.Response(
            200,
            json=[
                {"service": {"id": "srv-other", "name": "loopy-demo-2"}},
                {"service": {"id": "srv-1", "name": "loopy-demo"}},
            ],
        )

    service = _client(handler).find_service("loopy-demo")
    assert service["id"] == "srv-1"


def test_get_service_returns_none_on_404():
    def handler(request):
        return httpx.Response(404, json={"message": "not found"})

    assert _client(handler).get_service("srv-gone") is None


def test_put_env_vars_and_secret_files_send_sorted_arrays():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[])

    client = _client(handler)
    client.put_env_vars("srv-1", {"B": "2", "A": "1"})
    client.put_secret_files("srv-1", {"secrets__base.env": "K=v\n"})
    assert calls[0] == ("PUT", "/v1/services/srv-1/env-vars", [{"key": "A", "value": "1"}, {"key": "B", "value": "2"}])
    assert calls[1] == ("PUT", "/v1/services/srv-1/secret-files", [{"name": "secrets__base.env", "content": "K=v\n"}])


def test_repo_not_connected_hint():
    from loopy_cli.render import RenderAPIError

    def handler(request):
        return httpx.Response(400, json={"message": "repo not found or not connected"})

    with pytest.raises(RenderAPIError) as excinfo:
        _client(handler).create_service({})
    assert "connect" in excinfo.value.hint.lower()
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_deploy_render.py -q` — Expected: new tests FAIL.

- [ ] **Step 3: Implement** (append to `loopy_cli/render.py`; `import httpx` stays inside the class to keep module import light — but since only the CLI imports this module lazily anyway, a top-of-function import is enough):

```python
class RenderAPIError(RuntimeError):
    """A Render API failure with the platform's own message and the one next action."""

    def __init__(self, status: int, message: str, hint: str = ""):
        self.status = status
        self.message = message
        self.hint = hint
        text = f"Render API error {status}: {message}"
        if hint:
            text += f"\n  → {hint}"
        super().__init__(text)


def _hint_for(status: int, message: str) -> str:
    """The one next action for an API failure — every error names its fix."""
    lowered = message.lower()
    if status == 401:
        return f"key invalid or revoked — mint a new one at {RENDER_KEYS_URL}"
    if status in (402, 403) and ("plan" in lowered or "payment" in lowered):
        return "the chosen plan needs a payment method on the workspace (dashboard → Billing)"
    if status == 429:
        return "rate limited — wait a minute and re-run (the command is idempotent)"
    if "repo" in lowered and ("not found" in lowered or "connect" in lowered or "permission" in lowered):
        return (
            "private repo? connect GitHub to Render once (dashboard.render.com → New → "
            "Web Service → connect your account), then re-run"
        )
    return ""


class RenderClient:
    """Thin `api.render.com/v1` wrapper: bearer auth, JSON, readable errors.

    `transport` is injectable for tests (httpx.MockTransport); production uses the default.
    """

    def __init__(self, api_key: str, *, transport=None):
        import httpx

        self._http = httpx.Client(
            base_url=RENDER_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )

    def _request(self, method: str, path: str, **kwargs):
        import httpx

        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RenderAPIError(
                0,
                f"{type(exc).__name__}: {exc}",
                "network problem reaching api.render.com — check connectivity and re-run",
            ) from exc
        if response.status_code >= 400:
            try:
                message = response.json().get("message") or response.text
            except ValueError:
                message = response.text
            raise RenderAPIError(response.status_code, message, _hint_for(response.status_code, message))
        return response

    def owners(self) -> list[dict]:
        return [item["owner"] for item in self._request("GET", "/owners").json()]

    def find_service(self, name: str) -> dict | None:
        items = self._request("GET", "/services", params={"name": name, "limit": 20}).json()
        for item in items:
            service = item.get("service", item)
            if service.get("name") == name:
                return service
        return None

    def get_service(self, service_id: str) -> dict | None:
        try:
            return self._request("GET", f"/services/{service_id}").json()
        except RenderAPIError as exc:
            if exc.status == 404:
                return None
            raise

    def create_service(self, payload: dict) -> dict:
        body = self._request("POST", "/services", json=payload).json()
        return body.get("service", body)  # create returns {service, deployId}

    def update_service(self, service_id: str, payload: dict) -> dict:
        return self._request("PATCH", f"/services/{service_id}", json=payload).json()

    def put_env_vars(self, service_id: str, env: dict[str, str]) -> None:
        body = [{"key": key, "value": env[key]} for key in sorted(env)]
        self._request("PUT", f"/services/{service_id}/env-vars", json=body)

    def put_secret_files(self, service_id: str, files: dict[str, str]) -> None:
        body = [{"name": name, "content": files[name]} for name in sorted(files)]
        self._request("PUT", f"/services/{service_id}/secret-files", json=body)

    def trigger_deploy(self, service_id: str) -> dict:
        return self._request("POST", f"/services/{service_id}/deploys", json={}).json()

    def get_deploy(self, service_id: str, deploy_id: str) -> dict:
        return self._request("GET", f"/services/{service_id}/deploys/{deploy_id}").json()

    def delete_service(self, service_id: str) -> None:
        self._request("DELETE", f"/services/{service_id}")
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_deploy_render.py -q` — Expected: PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "deploy render: RenderClient with readable API errors"`

---

### Task 7: Git preflight helpers

**Files:**
- Modify: `loopy_cli/render.py`
- Test: `tests/test_deploy_render.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Check(key: str, label: str, ok: bool, warn: bool = False, fix: str = "")` — `warn=True` failures are confirmable; `warn=False` failures are fatal.
  - `@dataclass(frozen=True) RepoInfo(branch: str = "", repo_url: str = "")`
  - `normalize_repo_url(remote: str) -> str | None`
  - `git_checks(root: Path, branch: str | None) -> tuple[list[Check], RepoInfo]`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_deploy_render.py`):

```python
import subprocess

# ── git preflight ───────────────────────────────────────────────────────────────


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_project(tmp_path, *, remote: str | None = "https://github.com/acme/demo.git"):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "registry.yml").write_text("agents: {}\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    if remote:
        _git(tmp_path, "remote", "add", "origin", remote)
    return tmp_path


def test_normalize_repo_url_matrix():
    from loopy_cli.render import normalize_repo_url

    for raw in (
        "git@github.com:acme/demo.git",
        "https://github.com/acme/demo.git",
        "https://github.com/acme/demo",
        "ssh://git@github.com/acme/demo.git",
    ):
        assert normalize_repo_url(raw) == "https://github.com/acme/demo"
    assert normalize_repo_url("git@gitlab.com:acme/demo.git") == "https://gitlab.com/acme/demo"
    assert normalize_repo_url("https://example.com/acme/demo.git") is None


def test_git_checks_not_a_repo(tmp_path):
    from loopy_cli.render import git_checks

    checks, info = git_checks(tmp_path, None)
    assert [c.key for c in checks] == ["repo"]
    assert not checks[0].ok and not checks[0].warn
    assert "git init" in checks[0].fix
    assert info.repo_url == ""


def test_git_checks_no_usable_remote(tmp_path):
    from loopy_cli.render import git_checks

    checks, info = git_checks(_git_project(tmp_path, remote=None), None)
    by_key = {c.key: c for c in checks}
    assert by_key["repo"].ok
    assert not by_key["remote"].ok and not by_key["remote"].warn
    assert "git remote add origin" in by_key["remote"].fix


def test_git_checks_never_pushed_is_fatal_and_dirty_is_warn(tmp_path):
    from loopy_cli.render import git_checks

    project = _git_project(tmp_path)
    (project / "scratch.txt").write_text("wip")
    checks, info = git_checks(project, None)
    by_key = {c.key: c for c in checks}
    assert info.branch == "main"
    assert info.repo_url == "https://github.com/acme/demo"
    assert not by_key["clean"].ok and by_key["clean"].warn
    assert not by_key["pushed"].ok and not by_key["pushed"].warn
    assert "git push -u origin main" in by_key["pushed"].fix


def test_git_checks_all_green_via_local_bare_remote(tmp_path):
    from loopy_cli.render import git_checks

    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    project = _git_project(tmp_path / "proj_dir", remote=None)
    # (mkdir happens inside _git_project via git init? no — create the dir first)
    checks, info = git_checks(project, None)
```

**Note to implementer:** the last test needs the project dir to exist before `git init` — write it as:

```python
def test_git_checks_all_green_via_local_bare_remote(tmp_path):
    from loopy_cli.render import git_checks

    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    project = _git_project(project_dir, remote=None)
    _git(project, "remote", "add", "origin", str(bare))
    _git(project, "push", "-u", "origin", "main")
    checks, info = git_checks(project, None)
    by_key = {c.key: c for c in checks}
    # A local-path remote isn't GitHub/GitLab, so `remote` fails — assert the rest:
    assert by_key["clean"].ok
    assert by_key["pushed"].ok
```

(This keeps the pushed/clean logic honestly tested without needing a network remote; the `remote` check's pass case is covered by `normalize_repo_url` + the fatal/no-remote tests.)

- [ ] **Step 2: Run** — `uv run pytest tests/test_deploy_render.py -q -k git` — Expected: FAIL.

- [ ] **Step 3: Implement** (append to `loopy_cli/render.py`):

```python
@dataclass(frozen=True)
class Check:
    """One preflight verdict. `warn=True` failures are confirmable; others are fatal."""

    key: str
    label: str
    ok: bool
    warn: bool = False
    fix: str = ""


@dataclass(frozen=True)
class RepoInfo:
    """What the git preflight learned: the branch to deploy and the https repo URL."""

    branch: str = ""
    repo_url: str = ""


_REMOTE_PATTERNS = (
    re.compile(r"^git@(github\.com|gitlab\.com):([^/]+/[^/]+?)(?:\.git)?$"),
    re.compile(r"^(?:ssh://)?git@(github\.com|gitlab\.com)/([^/]+/[^/]+?)(?:\.git)?$"),
    re.compile(r"^https://(github\.com|gitlab\.com)/([^/]+/[^/]+?)(?:\.git)?/?$"),
)


def normalize_repo_url(remote: str) -> str | None:
    """`origin`'s URL as the https form Render's API takes, or None if not GitHub/GitLab."""
    remote = remote.strip()
    for pattern in _REMOTE_PATTERNS:
        match = pattern.match(remote)
        if match:
            return f"https://{match.group(1)}/{match.group(2)}"
    return None


def _run_git(root: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def git_checks(root: Path, branch: str | None) -> tuple[list[Check], RepoInfo]:
    """The deployability of the repo Render will build from — checks 5–8 of the preflight.

    Render builds the *pushed* tree of a connected repo, so each check maps to a way a
    deploy silently builds the wrong thing (or nothing): not a repo, no forge remote,
    uncommitted work (warn), never-pushed branch (fatal) / unpushed commits (warn).
    """
    code, _ = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        return (
            [
                Check(
                    "repo",
                    "directory is a git repository",
                    False,
                    fix='git init -b main && git add -A && git commit -m "loopy project"',
                )
            ],
            RepoInfo(),
        )
    checks = [Check("repo", "directory is a git repository", True)]
    if not branch:
        _, branch = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")

    code, remote = _run_git(root, "remote", "get-url", "origin")
    repo_url = normalize_repo_url(remote) if code == 0 else None
    if repo_url is None:
        checks.append(
            Check(
                "remote",
                "origin points at GitHub or GitLab",
                False,
                fix=(
                    "git remote add origin https://github.com/<you>/<repo>.git\n"
                    "(no repo yet? `gh repo create --source . --private --push` "
                    "creates and pushes one)"
                ),
            )
        )
        return checks, RepoInfo(branch=branch)
    checks.append(Check("remote", f"origin → {repo_url}", True))

    _, dirty = _run_git(root, "status", "--porcelain")
    checks.append(
        Check(
            "clean",
            "working tree is committed",
            not dirty,
            warn=True,
            fix="Render builds the *pushed* tree — uncommitted changes will not deploy",
        )
    )

    code, ahead = _run_git(root, "rev-list", "--count", f"origin/{branch}..{branch}")
    if code != 0:
        checks.append(
            Check(
                "pushed",
                f"branch {branch} is pushed to origin",
                False,
                fix=f"git push -u origin {branch}",
            )
        )
    else:
        unpushed = int(ahead or "0")
        checks.append(
            Check(
                "pushed",
                f"branch {branch} is pushed and up to date",
                unpushed == 0,
                warn=True,
                fix=f"{unpushed} unpushed commit(s) — `git push` first, or the build is stale",
            )
        )
    return checks, RepoInfo(branch=branch, repo_url=repo_url)
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_deploy_render.py -q` — Expected: PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "deploy render: git preflight (repo, remote, clean, pushed)"`

---

### Task 8: Project preflight + checklist rendering

**Files:**
- Modify: `loopy_cli/render.py`
- Test: `tests/test_deploy_render.py`

**Interfaces:**
- Produces:
  - `project_checks(root: Path, manifest_abs: Path) -> list[Check]` — loopy.env/DAYTONA_API_KEY (fatal), LOOPY_ADMIN_TOKEN (warn), sandbox env_files exist (fatal), Dockerfile present (fatal, fixable) / pin current (warn).
  - `print_checks(checks: list[Check]) -> tuple[bool, bool]` — prints house-style lines, returns `(any_fatal, any_warn)`.
  - `has_cron_workflows(manifest_abs: Path) -> bool` — for the plan prompt.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_deploy_render.py`; reuse the `_project` fixture idea from `test_deploy_generators.py`):

```python
_REGISTRY = (
    "sandboxes:\n"
    "  BaseSandbox:\n"
    "    provider: daytona\n"
    "    env_file: secrets/base.env\n"
    "agents:\n"
    "  Worker: { model: claude-sonnet-4-6, harness: claude-code, sandbox: BaseSandbox }\n"
)


def _loopy_project(tmp_path):
    (tmp_path / "registry.yml").write_text(_REGISTRY)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "base.env").write_text("ANTHROPIC_API_KEY=sk-ant-x\n")
    (tmp_path / "loopy.env").write_text(
        "DAYTONA_API_KEY=dt-real\nLOOPY_ADMIN_TOKEN=loopy_sk_admin\nRENDER_API_KEY=rnd_k\n"
    )
    return tmp_path


def _compiled_manifest(project) -> "Path":
    from typer.testing import CliRunner

    from loopy_cli import app

    result = CliRunner().invoke(app, ["compile", str(project)])
    assert result.exit_code == 0, result.output
    return project / "manifest.json"


# ── project preflight ───────────────────────────────────────────────────────────


def test_project_checks_green_project_still_needs_dockerfile(tmp_path):
    from loopy_cli.render import project_checks

    project = _loopy_project(tmp_path)
    manifest = _compiled_manifest(project)
    by_key = {c.key: c for c in project_checks(project, manifest)}
    assert by_key["loopy_env"].ok
    assert by_key["admin_token"].ok
    assert by_key["env_files"].ok
    assert not by_key["dockerfile"].ok and not by_key["dockerfile"].warn
    assert "loopy dockerfile" in by_key["dockerfile"].fix


def test_project_checks_flags_missing_env_file_and_token(tmp_path):
    from loopy_cli.render import project_checks

    project = _loopy_project(tmp_path)
    manifest = _compiled_manifest(project)
    (project / "secrets" / "base.env").unlink()
    (project / "loopy.env").write_text("DAYTONA_API_KEY=dt-real\n")
    by_key = {c.key: c for c in project_checks(project, manifest)}
    assert not by_key["env_files"].ok and not by_key["env_files"].warn
    assert "secrets/base.env" in by_key["env_files"].fix
    assert not by_key["admin_token"].ok and by_key["admin_token"].warn


def test_project_checks_stale_dockerfile_pin_is_warn(tmp_path):
    from loopy_cli.render import project_checks

    project = _loopy_project(tmp_path)
    manifest = _compiled_manifest(project)
    (project / "Dockerfile").write_text("FROM python:3.12-slim\nRUN pip install loopy-computer==0.0.0\n")
    by_key = {c.key: c for c in project_checks(project, manifest)}
    assert not by_key["dockerfile"].ok and by_key["dockerfile"].warn


def test_print_checks_reports_fatal_and_warn():
    from loopy_cli.render import Check, print_checks

    fatal, warn = print_checks(
        [
            Check("a", "fine", True),
            Check("b", "soft", False, warn=True, fix="do the soft thing"),
            Check("c", "hard", False, fix="do the hard thing"),
        ]
    )
    assert fatal is True and warn is True


def test_has_cron_workflows(tmp_path):
    from loopy_cli.render import has_cron_workflows

    project = _loopy_project(tmp_path)
    workflow = project / "workflows" / "tick"
    workflow.mkdir(parents=True)
    (workflow / "step.md").write_text(
        "---\non: cron(\"0 * * * *\")\nagent: Worker\n---\nDo the hourly thing.\n"
    )
    manifest = _compiled_manifest(project)
    assert has_cron_workflows(manifest) is True

    plain = _loopy_project(tmp_path / "plain")
    assert has_cron_workflows(_compiled_manifest(plain)) is False
```

**Note:** if the workflow frontmatter in `test_has_cron_workflows` doesn't compile in this repo's format, copy a minimal cron workflow from an existing test (grep `tests/test_b7_cron_scheduler.py` for its fixture) — the assertion is about `cron_entries()`, not authoring syntax. `tmp_path / "plain"` needs `mkdir()` first.

- [ ] **Step 2: Run** — `uv run pytest tests/test_deploy_render.py -q -k "project_checks or print_checks or cron"` — Expected: FAIL.

- [ ] **Step 3: Implement** (append to `loopy_cli/render.py`):

```python
def project_checks(root: Path, manifest_abs: Path) -> list[Check]:
    """Checks 2–4 and 9–10 of the preflight: creds, secrets on disk, Dockerfile."""
    from loopy_core import __version__
    from loopy_runtime.manifest_model import load_manifest
    from loopy_runtime.secrets import (
        ADMIN_TOKEN_ENV,
        CONTROL_PLANE_ENV_FILE,
        load_control_plane_env,
    )

    checks: list[Check] = []
    control = load_control_plane_env(root)
    checks.append(
        Check(
            "loopy_env",
            f"{CONTROL_PLANE_ENV_FILE} has control-plane creds",
            bool(control.get("DAYTONA_API_KEY")),
            fix="run `loopy init` — the engine needs DAYTONA_API_KEY at minimum (agents run in Daytona)",
        )
    )
    checks.append(
        Check(
            "admin_token",
            "LOOPY_ADMIN_TOKEN present (gates the /admin dashboard)",
            bool(control.get(ADMIN_TOKEN_ENV)),
            warn=True,
            fix="without it the deployed engine serves webhooks but never mounts /admin",
        )
    )

    manifest = load_manifest(manifest_abs)
    missing = [
        f"{rel} (sandbox {name})"
        for name, spec in manifest.registry.sandboxes.items()
        for rel in spec.env_file
        if not (root / rel).is_file()
    ]
    checks.append(
        Check(
            "env_files",
            "every sandbox env_file exists on disk",
            not missing,
            fix=("missing: " + ", ".join(missing) + " — the engine refuses to start without them")
            if missing
            else "",
        )
    )

    dockerfile = root / "Dockerfile"
    if not dockerfile.is_file():
        checks.append(
            Check(
                "dockerfile",
                "Dockerfile at the project root",
                False,
                fix="generate with `loopy dockerfile`, then commit and push it — Render builds from the remote",
            )
        )
    else:
        checks.append(
            Check(
                "dockerfile",
                f"Dockerfile pin matches installed loopy ({__version__})",
                f"loopy-computer[redis]=={__version__}" in dockerfile.read_text(),
                warn=True,
                fix=f"regenerate with `loopy dockerfile` (pins {__version__}), commit and push",
            )
        )
    return checks


def print_checks(checks: list[Check]) -> tuple[bool, bool]:
    """Render the checklist in the house style; return (any_fatal, any_warn)."""
    any_fatal = any_warn = False
    for check in checks:
        if check.ok:
            typer.echo("  " + typer.style("✓", fg=typer.colors.GREEN) + f" {check.label}")
            continue
        mark, color = ("!", typer.colors.YELLOW) if check.warn else ("✗", typer.colors.RED)
        if check.warn:
            any_warn = True
        else:
            any_fatal = True
        typer.echo("  " + typer.style(mark, fg=color) + f" {check.label}")
        for line in check.fix.splitlines():
            typer.echo(f"      {line}")
    return any_fatal, any_warn


def has_cron_workflows(manifest_abs: Path) -> bool:
    """Whether any workflow is cron-triggered — free-tier spin-down misses cron ticks."""
    from loopy_runtime.manifest_model import load_manifest

    return bool(load_manifest(manifest_abs).cron_entries())
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_deploy_render.py -q` — Expected: PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "deploy render: project preflight and checklist rendering"`

---

### Task 9: Setup wizard — API key, workspace, plan

**Files:**
- Modify: `loopy_cli/render.py`
- Test: `tests/test_deploy_render.py`

**Interfaces:**
- Produces:
  - `_interactive() -> bool` (module-level, monkeypatchable: `sys.stdin.isatty() and sys.stdout.isatty()`)
  - `_client_factory(api_key: str) -> RenderClient` (module-level seam for tests)
  - `connect(root: Path) -> tuple[RenderClient, dict]` — resolves + verifies the key (interactive re-prompt on 401), writes a newly-entered key to `loopy.env`, returns `(client, owner)`; multiple workspaces → numbered choice.
  - `choose_plan(*, has_cron: bool, plan_flag: str | None) -> str` — flag wins; TTY prompt (default starter); non-TTY without flag exits 1.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_deploy_render.py`):

```python
# ── wizard ──────────────────────────────────────────────────────────────────────


def _fake_client(owners):
    class FakeClient:
        def __init__(self, key):
            self.key = key

        def owners(self):
            from loopy_cli.render import RenderAPIError

            if self.key == "rnd_bad":
                raise RenderAPIError(401, "unauthorized", "mint a new one")
            return owners

    return FakeClient


def test_connect_uses_recorded_key_and_sole_owner(tmp_path, monkeypatch, capsys):
    from loopy_cli import render as render_mod

    project = _loopy_project(tmp_path)  # loopy.env already has RENDER_API_KEY=rnd_k
    monkeypatch.setattr(render_mod, "_client_factory", _fake_client([{"id": "own-1", "name": "V"}]))
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    client, owner = render_mod.connect(project)
    assert client.key == "rnd_k"
    assert owner["id"] == "own-1"


def test_connect_headless_without_key_exits_naming_the_fix(tmp_path, monkeypatch):
    import typer

    from loopy_cli import render as render_mod

    project = _loopy_project(tmp_path)
    (project / "loopy.env").write_text("DAYTONA_API_KEY=dt\n")
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    with pytest.raises(typer.Exit):
        render_mod.connect(project)


def test_connect_prompts_writes_key_back(tmp_path, monkeypatch):
    from loopy_cli import render as render_mod
    from loopy_runtime.secrets import load_control_plane_env

    project = _loopy_project(tmp_path)
    (project / "loopy.env").write_text("DAYTONA_API_KEY=dt\n")
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    monkeypatch.setattr(render_mod, "_client_factory", _fake_client([{"id": "own-1", "name": "V"}]))
    monkeypatch.setattr(render_mod, "_interactive", lambda: True)
    answers = iter(["rnd_new"])
    monkeypatch.setattr(render_mod.typer, "prompt", lambda *a, **k: next(answers))
    client, owner = render_mod.connect(project)
    assert load_control_plane_env(project)["RENDER_API_KEY"] == "rnd_new"


def test_choose_plan_flag_wins_and_headless_requires_it(monkeypatch):
    import typer

    from loopy_cli import render as render_mod

    assert render_mod.choose_plan(has_cron=True, plan_flag="standard") == "standard"
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    with pytest.raises(typer.Exit):
        render_mod.choose_plan(has_cron=False, plan_flag=None)


def test_choose_plan_prompt_defaults_to_starter(monkeypatch, capsys):
    from loopy_cli import render as render_mod

    monkeypatch.setattr(render_mod, "_interactive", lambda: True)
    monkeypatch.setattr(render_mod.typer, "prompt", lambda *a, **k: k.get("default", "2"))
    assert render_mod.choose_plan(has_cron=True, plan_flag=None) == "starter"
    out = capsys.readouterr().out
    assert "WILL miss" in out  # cron-aware annotation
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_deploy_render.py -q -k "connect or choose_plan"` — Expected: FAIL.

- [ ] **Step 3: Implement** (append to `loopy_cli/render.py`):

```python
def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _client_factory(api_key: str) -> "RenderClient":
    return RenderClient(api_key)


def _ok(text: str) -> None:
    typer.echo("  " + typer.style("✓", fg=typer.colors.GREEN) + f" {text}")


def _fail(text: str) -> None:
    typer.echo("  " + typer.style("✗", fg=typer.colors.RED) + f" {text}")


def connect(root: Path) -> tuple["RenderClient", dict]:
    """Resolve + verify the Render API key and pick the workspace; wizard steps 1–2.

    Key precedence: process env, then loopy.env; a missing key prompts on a TTY (and
    is written back on first success) or exits 1 headless. Verification is immediate
    (`GET /owners`) so a bad key fails here, not mid-provision; the same call yields
    the ownerId every create needs.
    """
    from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env

    recorded = (
        os.environ.get(RENDER_API_KEY_ENV, "").strip()
        or load_control_plane_env(root).get(RENDER_API_KEY_ENV, "").strip()
    )
    key = recorded
    prompted = False
    while True:
        if not key:
            if not _interactive():
                typer.echo(
                    f"error: {RENDER_API_KEY_ENV} not set — mint one at {RENDER_KEYS_URL} "
                    "and put it in loopy.env (or the environment).",
                    err=True,
                )
                raise typer.Exit(code=1)
            typer.echo("  1. Render API key")
            typer.echo(f"     Mint one at {RENDER_KEYS_URL} (Account Settings → API Keys)")
            key = typer.prompt("  API key", hide_input=True).strip()
            prompted = True
            if not key:
                continue
        client = _client_factory(key)
        try:
            owners = client.owners()
        except RenderAPIError as exc:
            if exc.status == 401 and _interactive():
                _fail("that key was rejected (401) — try again")
                key = ""
                continue
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        break

    if not owners:
        typer.echo("error: the key verified but has no workspaces — check the account.", err=True)
        raise typer.Exit(code=1)
    if len(owners) == 1:
        owner = owners[0]
    elif _interactive():
        typer.echo("  2. Workspace")
        for index, candidate in enumerate(owners, start=1):
            typer.echo(f"     {index}) {candidate.get('name', candidate['id'])}")
        while True:
            raw = typer.prompt(f"  Choose 1-{len(owners)}", default="1").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(owners):
                owner = owners[int(raw) - 1]
                break
    else:
        owner = owners[0]
        typer.echo(f"  using workspace {owner.get('name', owner['id'])} (first of {len(owners)})")

    if prompted:
        write_control_plane_env(root, {RENDER_API_KEY_ENV: key})
        _ok(
            f"key verified (workspace: {owner.get('name', owner['id'])}) — "
            f"wrote {RENDER_API_KEY_ENV} to loopy.env"
        )
    else:
        _ok(f"Render key verified (workspace: {owner.get('name', owner['id'])})")
    return client, owner


def choose_plan(*, has_cron: bool, plan_flag: str | None) -> str:
    """Wizard step 4. The flag always wins; a TTY gets the trade-off prompt; headless
    without a flag exits 1 — never silently pick a paid plan or a cron-breaking free one."""
    if plan_flag:
        return plan_flag
    if not _interactive():
        typer.echo(
            "error: no TTY to ask about the service plan — pass --plan "
            "(free|starter|standard|pro).",
            err=True,
        )
        raise typer.Exit(code=1)
    if has_cron:
        cron_line = (
            "your project has cron workflows and free-tier spin-down WILL miss their ticks"
        )
    else:
        cron_line = "webhooks wake it, with a ~30-60s cold start after idle"
    typer.echo("  4. Plan")
    typer.echo(f"     1) free     — $0. Spins down after ~15 min idle: {cron_line}.")
    typer.echo("                   Run history is lost on restart (no disk on free).")
    typer.echo("     2) starter  — ~$7/mo. Always on (cron fires), can attach a persistent disk.")
    while True:
        raw = typer.prompt("  Choose 1 or 2", default="2").strip()
        if raw in ("1", "2"):
            plan = "free" if raw == "1" else "starter"
            _ok(f"{plan} plan")
            return plan
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_deploy_render.py -q` — Expected: PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "deploy render: setup wizard (key, workspace, plan)"`

---

### Task 10: The `render` command — provision, deploy, write-back, webhooks, summary

**Files:**
- Modify: `loopy_cli/render.py` (the command + `build_create_payload` + `_poll_deploy` + `_register_webhooks`)
- Modify: `loopy_cli/__init__.py` (import `loopy_cli.render` beside the bootstrap import from Task 1)
- Test: `tests/test_deploy_render.py`

**Interfaces:**
- Consumes: everything from Tasks 2–9, plus from `loopy_cli.bootstrap`: `_StatusBoard`, `wait_until_serving`, `collect_secret_files`; from `loopy_cli`: `_resolve_manifest`, `deploy_env_block`, `dockerfile` generation; from `loopy_cli.webhooks`: `sync_github_webhooks`, `render_sync_report`, `github_hook_events`; from `loopy_cli.doctor`: `_declared_repo_slugs`.
- Produces: `loopy deploy render` (typer command), `build_create_payload(...) -> dict`, `_poll_deploy(client, service_id, deploy_id, *, sleep, attempts, delay) -> str`, `_register_webhooks(root, public_url) -> list[str]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_deploy_render.py`):

```python
# ── the command ─────────────────────────────────────────────────────────────────


def test_build_create_payload_shape():
    from loopy_cli.render import RepoInfo, build_create_payload

    payload = build_create_payload(
        name="loopy-demo",
        owner_id="own-1",
        info=RepoInfo(branch="main", repo_url="https://github.com/acme/demo"),
        plan="starter",
        region="oregon",
    )
    assert payload == {
        "type": "web_service",
        "name": "loopy-demo",
        "ownerId": "own-1",
        "repo": "https://github.com/acme/demo",
        "branch": "main",
        "autoDeploy": "yes",
        "serviceDetails": {
            "runtime": "docker",
            "plan": "starter",
            "region": "oregon",
            "envSpecificDetails": {"dockerfilePath": "./Dockerfile"},
        },
    }


def test_build_create_payload_with_disk():
    from loopy_cli.render import RepoInfo, build_create_payload

    payload = build_create_payload(
        name="loopy-demo",
        owner_id="own-1",
        info=RepoInfo(branch="main", repo_url="https://github.com/acme/demo"),
        plan="starter",
        region="oregon",
        disk_gb=5,
    )
    # The generated Dockerfile's start command uses /state/state.db when /state exists.
    assert payload["serviceDetails"]["disk"] == {"name": "state", "mountPath": "/state", "sizeGB": 5}


def test_poll_deploy_returns_terminal_status():
    from loopy_cli.render import _poll_deploy

    statuses = iter(["build_in_progress", "update_in_progress", "live"])

    class FakeClient:
        def get_deploy(self, service_id, deploy_id):
            return {"id": deploy_id, "status": next(statuses)}

    final = _poll_deploy(FakeClient(), "srv-1", "dep-1", sleep=lambda s: None)
    assert final == "live"


class _ScriptedClient:
    """A full fake for the command test: records calls, plays a create-flow script."""

    def __init__(self, key):
        self.key = key
        self.calls = []

    def owners(self):
        return [{"id": "own-1", "name": "V"}]

    def get_service(self, service_id):
        return None

    def find_service(self, name):
        self.calls.append(("find", name))
        return None

    def create_service(self, payload):
        self.calls.append(("create", payload))
        return {
            "id": "srv-9",
            "name": payload["name"],
            "serviceDetails": {"url": "https://loopy-demo.onrender.com"},
        }

    def put_env_vars(self, service_id, env):
        self.calls.append(("env", service_id, dict(env)))

    def put_secret_files(self, service_id, files):
        self.calls.append(("secrets", service_id, dict(files)))

    def trigger_deploy(self, service_id):
        self.calls.append(("deploy", service_id))
        return {"id": "dep-1", "status": "created"}

    def get_deploy(self, service_id, deploy_id):
        return {"id": deploy_id, "status": "live"}


def test_deploy_render_happy_path_create(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from loopy_cli import app
    from loopy_cli import render as render_mod
    from loopy_cli.render import Check, RepoInfo
    from loopy_runtime.secrets import load_control_plane_env

    project = _loopy_project(tmp_path)
    (project / "Dockerfile").write_text("# Generated by `loopy dockerfile`\n")
    scripted = {}

    def factory(key):
        scripted["client"] = _ScriptedClient(key)
        return scripted["client"]

    monkeypatch.setattr(render_mod, "_client_factory", factory)
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    monkeypatch.setattr(
        render_mod,
        "git_checks",
        lambda root, branch: (
            [Check("repo", "git", True), Check("remote", "origin", True)],
            RepoInfo(branch="main", repo_url="https://github.com/acme/demo"),
        ),
    )
    # Pin: the Dockerfile stub above doesn't carry the real version pin — silence the warn.
    monkeypatch.setattr(
        render_mod, "project_checks", lambda root, manifest: [Check("ok", "project", True)]
    )
    monkeypatch.setattr(render_mod, "wait_until_serving", lambda url, **kw: True)
    monkeypatch.setattr(render_mod, "_register_webhooks", lambda root, url: ["registered"])

    result = CliRunner().invoke(
        app, ["deploy", "render", str(project), "--root", str(project), "--plan", "free"]
    )
    assert result.exit_code == 0, result.output

    client = scripted["client"]
    kinds = [c[0] for c in client.calls]
    assert kinds == ["find", "create", "env", "secrets", "deploy"]
    env_pushed = client.calls[2][2]
    assert env_pushed["DAYTONA_API_KEY"] == "dt-real"
    assert "RENDER_API_KEY" not in env_pushed
    # loopy.env is NOT uploaded: env vars already carry the control plane, and the file
    # holds RENDER_API_KEY itself — the deployed service must never receive that.
    assert client.calls[3][2] == {"secrets__base.env": "ANTHROPIC_API_KEY=sk-ant-x\n"}
    control = load_control_plane_env(project)
    assert control["LOOPY_PUBLIC_URL"] == "https://loopy-demo.onrender.com"
    assert control["LOOPY_RENDER_SERVICE_ID"] == "srv-9"
    assert "loopy deploy render --destroy" in result.output


def test_deploy_render_update_path_uses_recorded_id(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from loopy_cli import app
    from loopy_cli import render as render_mod
    from loopy_cli.render import Check, RepoInfo

    project = _loopy_project(tmp_path)
    (project / "Dockerfile").write_text("# Generated by `loopy dockerfile`\n")
    (project / "loopy.env").write_text(
        "DAYTONA_API_KEY=dt-real\nRENDER_API_KEY=rnd_k\nLOOPY_RENDER_SERVICE_ID=srv-9\n"
    )

    class UpdateClient(_ScriptedClient):
        def get_service(self, service_id):
            self.calls.append(("get", service_id))
            return {
                "id": "srv-9",
                "name": "loopy-demo",
                "serviceDetails": {"url": "https://loopy-demo.onrender.com", "plan": "free"},
            }

    scripted = {}

    def factory(key):
        scripted["client"] = UpdateClient(key)
        return scripted["client"]

    monkeypatch.setattr(render_mod, "_client_factory", factory)
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    monkeypatch.setattr(
        render_mod,
        "git_checks",
        lambda root, branch: (
            [Check("repo", "git", True)],
            RepoInfo(branch="main", repo_url="https://github.com/acme/demo"),
        ),
    )
    monkeypatch.setattr(
        render_mod, "project_checks", lambda root, manifest: [Check("ok", "project", True)]
    )
    monkeypatch.setattr(render_mod, "wait_until_serving", lambda url, **kw: True)
    monkeypatch.setattr(render_mod, "_register_webhooks", lambda root, url: [])

    result = CliRunner().invoke(app, ["deploy", "render", str(project), "--root", str(project)])
    assert result.exit_code == 0, result.output
    kinds = [c[0] for c in scripted["client"].calls]
    # Recorded id → straight to the update path: no find, no create, no plan prompt.
    assert kinds == ["get", "env", "secrets", "deploy"]
    assert "updating in place" in result.output
```

**Note:** the manifest argument for the command is the project dir (the command compiles it via `_resolve_manifest`, same as bootstrap). If `_resolve_manifest(project_dir, project_dir)` needs a compiled `manifest.json` first, call `_compiled_manifest(project)` in the test before invoking. Check bootstrap's tests (`tests/test_deploy_bootstrap.py`) for how they feed it and copy that arrangement.

- [ ] **Step 2: Run** — `uv run pytest tests/test_deploy_render.py -q -k "payload or poll or happy"` — Expected: FAIL.

- [ ] **Step 3: Implement** (append to `loopy_cli/render.py`). Import the reused bootstrap pieces at module top: `from loopy_cli.bootstrap import _StatusBoard, collect_secret_files, wait_until_serving`.

```python
RENDER_REGIONS = ("oregon", "frankfurt", "ohio", "singapore", "virginia")
_DEPLOY_TERMINAL = {"live", "build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated"}


def build_create_payload(
    *, name: str, owner_id: str, info: RepoInfo, plan: str, region: str, disk_gb: int | None = None
) -> dict:
    """The POST /services body for the engine web service (docker runtime, git-push CD)."""
    details: dict = {
        "runtime": "docker",
        "plan": plan,
        "region": region,
        "envSpecificDetails": {"dockerfilePath": "./Dockerfile"},
    }
    if disk_gb:
        # Mounted at /state — the generated Dockerfile's start command detects the mount
        # and moves run history to /state/state.db so it survives redeploys.
        details["disk"] = {"name": "state", "mountPath": "/state", "sizeGB": disk_gb}
    return {
        "type": "web_service",
        "name": name,
        "ownerId": owner_id,
        "repo": info.repo_url,
        "branch": info.branch,
        "autoDeploy": "yes",
        "serviceDetails": details,
    }


def _poll_deploy(client, service_id: str, deploy_id: str, *, sleep=time.sleep, attempts: int = 120, delay: int = 5) -> str:
    """Poll one deploy to a terminal status (~10 min budget); returns the final status."""
    status = "created"
    for _ in range(attempts):
        status = client.get_deploy(service_id, deploy_id).get("status", "")
        if status in _DEPLOY_TERMINAL:
            return status
        sleep(delay)
    return status


def _register_webhooks(root: Path, public_url: str) -> list[str]:
    """Register GitHub webhooks at the fresh URL; summary lines, never raises.

    Deliberate divergence from bootstrap's nudge-only convention: this command's promise
    is "full setup end to end", so it runs the `loopy webhooks github` step itself. A
    failure is non-fatal — the deploy already succeeded — and degrades to the retry command.
    """
    from loopy_cli.doctor import _declared_repo_slugs
    from loopy_cli.webhooks import github_hook_events, sync_github_webhooks
    from loopy_core.compile.pipeline import compile_project

    try:
        project = compile_project(root).project
        if project is None:
            return ["skipped (project no longer compiles?) — run `loopy webhooks github`"]
        repos = _declared_repo_slugs(project.registry)
        events = github_hook_events(project.sensors)
        if not repos or not events:
            return []  # no GitHub sensors/repos — nothing to register, say nothing
        report = sync_github_webhooks(root, repos=repos, events=events, public_url=public_url)
        lines = [f"{r.repo}: {r.action}" + (f" — {r.detail}" if r.action == "error" else "") for r in report.results]
        if report.secret_written:
            lines.append("wrote GITHUB_WEBHOOK_SECRET to loopy.env")
        return lines
    except Exception as exc:  # noqa: BLE001 - post-deploy nicety must never fail the deploy
        return [f"not registered ({exc}) — run `loopy webhooks github` to retry"]


@deploy_app.command()
def render(
    manifest: Path = typer.Argument(
        Path("manifest.json"),
        help="A manifest.json, or a project directory to compile (default: manifest.json).",
    ),
    root: Path = typer.Option(Path("."), "--root", help="Project root (env files + sensors)."),
    plan: str | None = typer.Option(
        None, "--plan", help="Service plan (free|starter|standard|pro). Prompted on a TTY."
    ),
    service_name: str | None = typer.Option(
        None, "--service-name", help="Render service name (default: loopy-<project-dir>)."
    ),
    region: str | None = typer.Option(
        None, "--region", help=f"Render region ({'|'.join(RENDER_REGIONS)}; default oregon)."
    ),
    branch: str | None = typer.Option(
        None, "--branch", help="Branch to deploy (default: the repo's current branch)."
    ),
    disk_gb: int | None = typer.Option(
        None,
        "--disk-gb",
        help="Attach a persistent disk (GB) at /state for run history (paid plans only).",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Accept safe fixups (generate Dockerfile, proceed past warnings)."
    ),
    destroy: bool = typer.Option(False, "--destroy", help="Delete the Render service."),
) -> None:
    """Deploy the engine to Render end to end: preflight, create-or-update, go live.

    The render deploy target — Render builds your repo's generated Dockerfile from a
    git push, so this command verifies the repo is deployable, creates or updates the
    web service via Render's API (env vars + secret files included), waits for the
    deploy to answer /healthz, writes LOOPY_PUBLIC_URL back to loopy.env, and
    registers GitHub webhooks. Re-running is safe; --destroy tears the service down.
    """
    from loopy_cli import _resolve_manifest, deploy_env_block
    from loopy_cli.deploy_target import RENDER_SERVICE_ID_ENV
    from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env

    root_abs = Path(root).resolve()

    if destroy:
        _destroy(root_abs, service_name, yes)  # Task 11
        return

    # ── preflight: compile, project, git — all before any Render call.
    manifest_path, root = _resolve_manifest(manifest, Path(root))
    root_abs = root.resolve()
    manifest_abs = (manifest_path if manifest_path.is_absolute() else root / manifest_path).resolve()

    typer.echo("preflight:")
    checks = project_checks(root_abs, manifest_abs)
    git_results, info = git_checks(root_abs, branch)
    fatal, warn = print_checks(checks + git_results)

    # Fixable gap: no Dockerfile → offer to generate, then stop for commit+push.
    by_key = {c.key: c for c in checks}
    if not by_key["dockerfile"].ok and not by_key["dockerfile"].warn:
        if yes or (_interactive() and typer.confirm("  Generate the Dockerfile now?", default=True)):
            from loopy_cli import dockerfile as generate_dockerfile

            generate_dockerfile(root_abs, stdout=False)
            typer.echo(
                "  → commit and push the new Dockerfile + .dockerignore, then re-run "
                "`loopy deploy render` (Render builds the pushed tree)."
            )
        raise typer.Exit(code=1)
    if fatal:
        typer.echo("deploy: fix the ✗ items above and re-run.", err=True)
        raise typer.Exit(code=1)
    if warn and not yes:
        if not _interactive() or not typer.confirm("  Proceed past the ! warnings?", default=False):
            raise typer.Exit(code=1)

    # ── wizard: key + workspace, then service identity + plan.
    client, owner = connect(root_abs)
    control = load_control_plane_env(root_abs)
    name = service_name or f"loopy-{root_abs.name}"
    region_value = region or "oregon"

    service = None
    recorded_id = control.get(RENDER_SERVICE_ID_ENV, "").strip()
    if recorded_id:
        service = client.get_service(recorded_id)
    if service is None:
        service = client.find_service(name)

    plan_value = choose_plan(has_cron=has_cron_workflows(manifest_abs), plan_flag=plan) if service is None else (plan or "")
    if service is not None and plan and plan != (service.get("serviceDetails") or {}).get("plan"):
        typer.echo("  note: changing an existing service's plan is a dashboard action; --plan ignored.")

    # ── provision.
    if service is None:
        if _interactive():
            typer.echo("  3. Service")
            if service_name is None:
                name = typer.prompt("  Service name", default=name).strip() or name
            if region is None:
                choices = "  ".join(f"{i}) {r}" for i, r in enumerate(RENDER_REGIONS, start=1))
                typer.echo(f"  Region — {choices}")
                while True:
                    raw = typer.prompt(f"  Choose 1-{len(RENDER_REGIONS)}", default="1").strip()
                    if raw.isdigit() and 1 <= int(raw) <= len(RENDER_REGIONS):
                        region_value = RENDER_REGIONS[int(raw) - 1]
                        break
        service = client.create_service(
            build_create_payload(
                name=name,
                owner_id=owner["id"],
                info=info,
                plan=plan_value,
                region=region_value,
                disk_gb=disk_gb,
            )
        )
        _ok(f"created web service {service['name']} in {region_value}, deploying branch {info.branch}")
    else:
        _ok(f"found existing service {service.get('name', service['id'])} — updating in place")

    service_id = service["id"]
    client.put_env_vars(service_id, deploy_env_block(root_abs))
    from loopy_runtime.secrets import CONTROL_PLANE_ENV_FILE

    # loopy.env stays home: the env-var push above already carries the control plane, and
    # the file holds RENDER_API_KEY itself — the deployed service must never receive that.
    secret_files = {
        encode_secret_file_name(rel): (root_abs / rel).read_text()
        for rel in collect_secret_files(root_abs, manifest_abs)
        if rel != CONTROL_PLANE_ENV_FILE
    }
    client.put_secret_files(service_id, secret_files)
    _ok(f"pushed {len(secret_files)} secret file(s) and the engine env block")

    deploy = client.trigger_deploy(service_id)
    board = _StatusBoard(
        "deploy: render",
        [
            ("build", "build & deploy on Render", "~2-6 min"),
            ("healthz", "engine answering", "~30s"),
        ],
    )
    board.enter("build")
    final = _poll_deploy(client, service_id, deploy["id"])
    board.complete("build", ok=final == "live")

    public_url = (service.get("serviceDetails") or {}).get("url") or f"https://{name}.onrender.com"
    if final != "live":
        typer.echo(
            f"deploy: {final}. Build logs: https://dashboard.render.com/web/{service_id}",
            err=True,
        )
        raise typer.Exit(code=1)

    board.enter("healthz")
    serving = wait_until_serving(public_url, echo=lambda line: board.detail("healthz", line))
    board.complete("healthz", ok=serving)

    write_control_plane_env(
        root_abs, {"LOOPY_PUBLIC_URL": public_url, RENDER_SERVICE_ID_ENV: service_id}
    )

    webhook_lines = _register_webhooks(root_abs, public_url)

    # ── summary (bootstrap's format).
    import shutil

    typer.echo("")
    typer.echo(f"deploy: done. Engine at {public_url}")
    typer.echo(f"  status:    {'live (/healthz is answering)' if serving else 'not answering yet — check the dashboard build logs'}")
    plan_note = (service.get("serviceDetails") or {}).get("plan") or plan_value
    if plan_note == "free":
        typer.echo("  plan:      free (spins down after ~15 min idle; webhooks wake it)")
    else:
        typer.echo(f"  plan:      {plan_note} (always on)")
    typer.echo(f"  url:       wrote LOOPY_PUBLIC_URL={public_url} to loopy.env")
    if webhook_lines:
        typer.echo(f"  webhooks:  {webhook_lines[0]}")
        for line in webhook_lines[1:]:
            typer.echo(f"             {line}")
    typer.echo("  dashboard: loopy admin  (proxies to /admin with your bearer token)")
    if shutil.which("render"):
        typer.echo(f"  logs:      render logs --tail {service_id}")
    typer.echo(f"  cd:        git push origin {info.branch} redeploys automatically")
    typer.echo("  teardown:  loopy deploy render --destroy")
```

Also in `loopy_cli/__init__.py`, extend Task 1's import:

```python
from loopy_cli import bootstrap as _bootstrap_module  # noqa: F401 - registers `loopy deploy bootstrap`
from loopy_cli import render as _render_module  # noqa: F401 - registers `loopy deploy render`
from loopy_cli.deploy_cmd import deploy_app
```

(`_destroy` doesn't exist yet — add a stub `def _destroy(root_abs, service_name, yes): raise typer.Exit(code=1)` so the module imports; Task 11 replaces it.)

**Import-cycle watch:** `render.py` imports `loopy_cli` (for `_resolve_manifest`/`deploy_env_block`) *inside the command body* — never at module top — because `loopy_cli/__init__.py` imports `render.py`. Same rule bootstrap already follows (`bootstrap.py:788`).

- [ ] **Step 4: Run** — `uv run pytest tests/test_deploy_render.py -q` then `uv run loopy deploy render --help` — Expected: PASS; help renders with all flags.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "deploy render: the end-to-end command (create-or-update, deploy, write-back, webhooks)"`

---

### Task 11: `--destroy`

**Files:**
- Modify: `loopy_cli/render.py` (replace the `_destroy` stub)
- Test: `tests/test_deploy_render.py`

**Interfaces:**
- Consumes: `connect`, `RENDER_SERVICE_ID_ENV`, `write_control_plane_env`.
- Produces: `_destroy(root_abs: Path, service_name: str | None) -> None` — deletes by recorded id (fallback find-by-name), clears the hint keys, clean no-op when nothing exists.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_deploy_render.py`):

```python
# ── destroy ─────────────────────────────────────────────────────────────────────


class _DestroyClient:
    def __init__(self, key):
        self.key = key
        self.deleted = []
        self.services = {"srv-9": {"id": "srv-9", "name": "loopy-demo", "serviceDetails": {"url": "https://loopy-demo.onrender.com"}}}

    def owners(self):
        return [{"id": "own-1", "name": "V"}]

    def get_service(self, service_id):
        return self.services.get(service_id)

    def find_service(self, name):
        return next((s for s in self.services.values() if s["name"] == name), None)

    def delete_service(self, service_id):
        self.deleted.append(service_id)
        self.services.pop(service_id, None)


def test_destroy_by_recorded_id_clears_hints(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from loopy_cli import app
    from loopy_cli import render as render_mod
    from loopy_runtime.secrets import load_control_plane_env

    project = _loopy_project(tmp_path)
    (project / "loopy.env").write_text(
        "DAYTONA_API_KEY=dt\nRENDER_API_KEY=rnd_k\n"
        "LOOPY_RENDER_SERVICE_ID=srv-9\nLOOPY_PUBLIC_URL=https://loopy-demo.onrender.com\n"
    )
    holder = {}

    def factory(key):
        holder["client"] = _DestroyClient(key)
        return holder["client"]

    monkeypatch.setattr(render_mod, "_client_factory", factory)
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    result = CliRunner().invoke(app, ["deploy", "render", "--root", str(project), "--destroy", "--yes"])
    assert result.exit_code == 0, result.output
    assert holder["client"].deleted == ["srv-9"]
    control = load_control_plane_env(project)
    assert not control.get("LOOPY_RENDER_SERVICE_ID")
    assert not control.get("LOOPY_PUBLIC_URL")  # pointed at the deleted service → cleared


def test_destroy_nothing_recorded_is_a_clean_noop(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from loopy_cli import app
    from loopy_cli import render as render_mod

    project = _loopy_project(tmp_path)  # no LOOPY_RENDER_SERVICE_ID recorded

    def factory(key):
        client = _DestroyClient(key)
        client.services = {}
        return client

    monkeypatch.setattr(render_mod, "_client_factory", factory)
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    result = CliRunner().invoke(app, ["deploy", "render", "--root", str(project), "--destroy"])
    assert result.exit_code == 0, result.output
    assert "nothing to delete" in result.output
```

**Note:** `--destroy` must run without a compiled manifest (a teardown shouldn't require a compiling project), which the Task 10 command body already honors by branching to `_destroy` before `_resolve_manifest`. The `--yes` flag doubles as the destroy confirmation skip; interactive destroys `typer.confirm` first.

- [ ] **Step 2: Run** — `uv run pytest tests/test_deploy_render.py -q -k destroy` — Expected: FAIL (stub exits 1).

- [ ] **Step 3: Implement** — replace the `_destroy` stub (same 3-arg signature Task 10 already calls):

```python
def _destroy(root_abs: Path, service_name: str | None, yes: bool) -> None:
    """Tear down the Render service: recorded id first, find-by-name fallback, clean no-op."""
    from loopy_cli.deploy_target import RENDER_SERVICE_ID_ENV
    from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env

    client, _owner = connect(root_abs)
    control = load_control_plane_env(root_abs)
    service = None
    recorded_id = control.get(RENDER_SERVICE_ID_ENV, "").strip()
    if recorded_id:
        service = client.get_service(recorded_id)
    if service is None:
        service = client.find_service(service_name or f"loopy-{root_abs.name}")
    if service is None:
        typer.echo("deploy: no Render service found — nothing to delete.")
        return

    label = f"{service.get('name', service['id'])} ({service['id']})"
    if not yes:
        if not _interactive() or not typer.confirm(f"  Delete Render service {label}?", default=False):
            typer.echo("deploy: aborted; nothing deleted.")
            raise typer.Exit(code=1)
    client.delete_service(service["id"])

    # Clear the client-side hints. write_control_plane_env merges (it can't delete), so
    # blank the values — every reader `.strip()`s, and a blank reads as unset.
    updates = {RENDER_SERVICE_ID_ENV: ""}
    service_url = (service.get("serviceDetails") or {}).get("url", "")
    if service_url and control.get("LOOPY_PUBLIC_URL", "").strip() == service_url:
        updates["LOOPY_PUBLIC_URL"] = ""
    write_control_plane_env(root_abs, updates)
    typer.echo(f"deploy: deleted {label}. Cleared its keys from loopy.env.")
```

**Check while implementing:** confirm `load_control_plane_env`'s parser drops or blanks `KEY=` lines (it keeps them as `""` — see `_parse_dotenv`), and that the places reading `LOOPY_PUBLIC_URL` treat `""` as unset (`webhooks.resolve_public_url`, `loopy admin` routing). If any reader does a bare `in` check instead of truthiness, fix that reader in this task with a one-line `.strip()`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_deploy_render.py -q` — Expected: PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "deploy render: --destroy with loopy.env cleanup"`

---

### Task 12: `loopy init` names render as a hosting choice

**Files:**
- Modify: `loopy_cli/__init__.py:835-875` (`_choose_deploy_target`), `:957+` (`_report_remaining_setup`), `:1031+` (`_explain_github_webhooks`)
- Test: `tests/test_init_scaffold.py`

**Interfaces:**
- Consumes: `TARGET_RENDER` from Task 2.
- Produces: `_choose_deploy_target` returns `"render"` on choice `3`; render behaves like bootstrap everywhere init defers the URL (`_report_remaining_setup`, `_explain_github_webhooks` — find every `== TARGET_BOOTSTRAP` / `deploy_target == TARGET_BOOTSTRAP` comparison in `__init__.py` and decide: URL-deferral logic → also `TARGET_RENDER`; AWS-specific copy → bootstrap only).

- [ ] **Step 1: Write the failing test** (append to `tests/test_init_scaffold.py`, following the existing monkeypatch pattern at lines 227/258):

```python
def test_choose_deploy_target_offers_render(monkeypatch):
    import loopy_cli
    import typer

    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "3")
    assert loopy_cli._choose_deploy_target(Path(".")) == "render"


def test_choose_deploy_target_default_is_byo(monkeypatch):
    import loopy_cli
    import typer

    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "")
    assert loopy_cli._choose_deploy_target(Path(".")) == "byo"
```

(Import `Path` at the top of the test file if not already there.)

- [ ] **Step 2: Run** — `uv run pytest tests/test_init_scaffold.py -q -k choose_deploy` — Expected: FAIL.

- [ ] **Step 3: Implement.** In `_choose_deploy_target` replace the two-option block with:

```python
    from loopy_cli.deploy_target import TARGET_BOOTSTRAP, TARGET_RENDER

    typer.echo("  How will you host the engine?")
    typer.echo(
        "    1) I'll provide the public URL — a domain, a dev tunnel, or a platform I manage"
    )
    typer.echo(
        "    2) Provision a starter stack for me — `loopy deploy bootstrap` stands up the "
        "host on AWS and mints the URL"
    )
    typer.echo(
        "    3) Deploy to Render — `loopy deploy render` creates the service from your repo "
        "and sets LOOPY_PUBLIC_URL at deploy"
    )
    choice = typer.prompt("  Choose 1, 2 or 3", default="1").strip()
    chosen = {"2": TARGET_BOOTSTRAP, "3": TARGET_RENDER}.get(choice, TARGET_BYO)

    if chosen != TARGET_BYO:
        command = "loopy deploy bootstrap" if chosen == TARGET_BOOTSTRAP else "loopy deploy render"
        typer.echo(
            "  "
            + typer.style("✓", fg=typer.colors.GREEN)
            + f" {chosen} target — `{command}` sets LOOPY_PUBLIC_URL for you at deploy."
        )
    typer.echo()
    return chosen
```

Update the function's docstring bullets to name the third choice. Then sweep the other `TARGET_BOOTSTRAP` comparisons in `__init__.py` (`grep -n TARGET_BOOTSTRAP loopy_cli/__init__.py`): in `_report_remaining_setup` and `_explain_github_webhooks`, change URL-deferral branches from `deploy_target == TARGET_BOOTSTRAP` to `deploy_target in (TARGET_BOOTSTRAP, TARGET_RENDER)` and parameterize the printed command (`loopy deploy bootstrap` vs `loopy deploy render`) the same way as above. Leave AWS-only copy (SSM tunnel, CloudFront) on the bootstrap branch.

- [ ] **Step 4: Run** — `uv run pytest tests/test_init_scaffold.py -q` — Expected: PASS (including all pre-existing init tests — they monkeypatch `_choose_deploy_target` itself, so they should be unaffected; if any asserts on the old two-option prompt text, update it to the new copy).

- [ ] **Step 5: Commit** — `git add -A && git commit -m "init: offer Render as a hosting choice"`

---

### Task 13: Docs

**Files:**
- Modify: `loopy_cli/docs_md/deployment.md`
- Create: `docs/design/render-deploy.md`
- Modify: `loopy_cli/__init__.py:57-59` (the deploy group comment), `loopy_cli/__init__.py:7-12` (the module docstring's command list line for `loopy deploy`)

- [ ] **Step 1: Add the Render-target section to `loopy_cli/docs_md/deployment.md`**, after "The provider-agnostic serve contract" section (keep the existing provider table):

```markdown
## The render target — `loopy deploy render`

The one-command path onto Render: preflights that your repo is deployable (a pushed
GitHub/GitLab repo with a generated `Dockerfile`), then creates or updates the web
service via Render's API — engine env vars from `loopy.env` (the `loopy env` block),
every sandbox `env_file` as a Render Secret File, git-push auto-deploy on — waits for
`/healthz`, writes `LOOPY_PUBLIC_URL` back to `loopy.env`, and registers GitHub
webhooks. Re-running updates the same service; `loopy deploy render --destroy` removes it.

- **Auth:** `RENDER_API_KEY` in `loopy.env` or the environment (the command prompts and
  records it on first run; mint at dashboard → Account Settings → API Keys).
- **Secret files:** Render mounts them flat at `/etc/secrets/<name>`; the deploy encodes
  each project-relative path (`secrets/base.env` → `secrets__base.env`) and the generated
  Dockerfile links them back into place at boot, so `env_file:` paths work unchanged.
- **Plans:** free spins down after ~15 min idle — webhooks wake it (cold start), but
  cron workflows MISS ticks while spun down, and run history (`.loopy/state.db`) does not
  survive restarts. `--plan starter` keeps the engine always on.
- **Dashboard:** the `*.onrender.com` URL is a normal HTTPS URL, so `loopy admin`
  proxies to `/admin` with your bearer token — no tunnel.
```

- [ ] **Step 2: Create `docs/design/render-deploy.md`** — copy the approved spec (`docs/superpowers/specs/2026-07-06-loopy-deploy-render-design.md`) and retitle it "Render deploy target — design" with a first line pointing at `loopy_cli/render.py`; drop the "Decisions already made (with the user)" framing (fold those bullets into a "Decisions" section, same content).

- [ ] **Step 3: Update the two `__init__.py` references.** Module docstring line 9 area: mention render alongside bootstrap for `loopy deploy`; comment block at lines 57–59: `# One subcommand per deploy target: bootstrap (AWS starter stack), render (Render.com).`

- [ ] **Step 4: Verify docs render + full suite**

Run: `uv run loopy docs deployment | head -60` (see the new section), then `uv run pytest -q`
Expected: docs print with the render section; full suite PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "docs: render deploy target (deployment reference + design doc)"`

---

### Task 14: Live validation (manual, not CI)

**Files:** none (a checklist; findings feed fixes as follow-up commits)

- [ ] **Step 1:** In a scratch loopy project with a pushed GitHub repo and a real `RENDER_API_KEY`: run `loopy deploy render --plan free`, confirm the wizard, checklist, create, secret files (check the dashboard shows `secrets__base.env`), `/healthz` answer, `loopy.env` write-back, and webhook registration.
- [ ] **Step 2:** Re-run the same command — confirm it finds the service by recorded id, re-pushes env + secrets, and stays idempotent.
- [ ] **Step 3:** `loopy admin` — confirm the dashboard proxies to the Render URL.
- [ ] **Step 4:** `loopy deploy render --destroy` — confirm deletion + `loopy.env` cleanup.
- [ ] **Step 5:** If the secret-files endpoint shape differed from Task 6's assumption, the fix landed there — re-run this checklist after.
- [ ] **Step 6:** Commit any fixes as `deploy render: <what the live run taught us>`.
