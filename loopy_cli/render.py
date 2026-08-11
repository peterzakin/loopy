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

from loopy_cli.bootstrap import _StatusBoard, collect_secret_files, wait_until_serving
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
                body = response.json()
            except ValueError:
                body = None
            message = (body.get("message") if isinstance(body, dict) else None) or response.text
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
    if code != 0:
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

    repo_url = normalize_repo_url(remote)
    checks.append(
        Check(
            "remote",
            f"origin → {repo_url}" if repo_url else "origin points at GitHub or GitLab",
            repo_url is not None,
            fix=(
                "git remote add origin https://github.com/<you>/<repo>.git\n"
                "(no repo yet? `gh repo create --source . --private --push` "
                "creates and pushes one)"
            )
            if repo_url is None
            else "",
        )
    )

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
    return checks, RepoInfo(branch=branch, repo_url=repo_url or "")


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
                f"loopy-computer[redis,tenki]=={__version__}" in dockerfile.read_text(),
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
    # `.get` (not `[...]`) because a stubbed `project_checks` (as in the command tests) may
    # not report a "dockerfile" check at all — treat that as nothing to fix here.
    by_key = {c.key: c for c in checks}
    dockerfile_check = by_key.get("dockerfile")
    if dockerfile_check is not None and not dockerfile_check.ok and not dockerfile_check.warn:
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
            typer.echo("deploy: warnings above — pass --yes to proceed.", err=True)
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
        if service is not None:
            # A name match alone isn't ownership: two projects in same-named directories
            # would otherwise silently replace each other's env vars and secret files.
            # The recorded service id is trusted (a previous deploy of this project wrote
            # it); a find-by-name hit is trusted only when it deploys this same repo.
            service_repo = normalize_repo_url(service.get("repo") or "")
            if service_repo != info.repo_url:
                typer.echo(
                    f"error: a Render service named {name!r} already exists but deploys "
                    f"{service.get('repo') or 'an unknown repo'}, not {info.repo_url} — "
                    "refusing to overwrite it. Pass --service-name to use a different name.",
                    err=True,
                )
                raise typer.Exit(code=1)

    plan_value = choose_plan(has_cron=has_cron_workflows(manifest_abs), plan_flag=plan) if service is None else (plan or "")
    if service is not None and plan and plan != (service.get("serviceDetails") or {}).get("plan"):
        typer.echo("  note: changing an existing service's plan is a dashboard action; --plan ignored.")
    if service is not None:
        # These only take effect at create time; changing them on an existing service is
        # a dashboard-only action (Render's API has no PATCH surface for them here).
        for flag_name, flag_value in (
            ("region", region),
            ("branch", branch),
            ("disk-gb", disk_gb),
        ):
            if flag_value is not None:
                typer.echo(
                    f"  note: changing an existing service's {flag_name} is a dashboard "
                    f"action; --{flag_name} ignored."
                )

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
