"""`loopy webhooks` — event derivation, the registration sync, and the wizard offer.

The sync's network boundary is `loopy_runtime.scm.github_app`; these tests stub its
functions rather than hitting the wire, and assert convergence semantics: create when
absent, update in place when present, report-only under `--check`, and per-repo errors
that never abort the rest.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import loopy_cli.webhooks as webhooks
from loopy_cli import app
from loopy_cli.scaffold import scaffold_project
from loopy_cli.webhooks import (
    ALL_HOOK_EVENTS,
    BUILTIN_HOOK_EVENTS,
    HookSync,
    SyncReport,
    github_hook_events,
    registration_findings,
    sync_github_webhooks,
)
from loopy_core.builtins import GITHUB_EVENTS
from loopy_core.compile.pipeline import compile_project
from loopy_core.sensors.model import Sensor, SensorTrigger
from loopy_core.span import span_at
from loopy_runtime.scm import github_app
from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env

runner = CliRunner()

_SPAN = span_at("<test>")
_URL = "https://loopy.example.com"
_DELIVERY = f"{_URL}/hooks/github"


def _sensor(path="/hooks/github", *, emits="Github.Push", source="builtin", kind="webhook"):
    return Sensor(
        name=f"{source}:{emits}",
        trigger=SensorTrigger(kind=kind, path=path if kind == "webhook" else None, span=_SPAN),
        emits=emits,
        source=source,
        provider="github" if source == "builtin" else None,
        module=None if source == "builtin" else "sensors.sensors",
        fn=None if source == "builtin" else "fn",
        span=_SPAN,
    )


def _write_app_creds(target):
    write_control_plane_env(target, {"GITHUB_APP_ID": "42", "GITHUB_APP_PRIVATE_KEY": "pem"})


def _stub_github(monkeypatch, *, hooks_by_repo=None, find_errors=None, list_error=None):
    """Stub the github_app network functions; return the recorded create/update calls."""
    hooks_by_repo = hooks_by_repo or {}
    find_errors = find_errors or {}
    calls = {"created": [], "updated": [], "minted": []}

    def find_installation(creds, owner, repo, **kw):
        slug = f"{owner}/{repo}"
        if slug in find_errors:
            raise find_errors[slug]
        return {"id": 1}

    def mint_installation_token(creds, installation_id, **kw):
        calls["minted"].append(installation_id)
        return {"token": "tok"}

    def list_repo_hooks(token, owner, repo, **kw):
        if list_error is not None:
            raise list_error
        return hooks_by_repo.get(f"{owner}/{repo}", [])

    def create_repo_hook(token, owner, repo, *, url, secret, events, **kw):
        calls["created"].append((f"{owner}/{repo}", url, secret, list(events)))
        return {"id": 99}

    def update_repo_hook(token, owner, repo, hook_id, *, url, secret, events, **kw):
        calls["updated"].append((f"{owner}/{repo}", hook_id, url, secret, list(events)))
        return {"id": hook_id}

    monkeypatch.setattr(github_app, "find_installation", find_installation)
    monkeypatch.setattr(github_app, "mint_installation_token", mint_installation_token)
    monkeypatch.setattr(github_app, "list_repo_hooks", list_repo_hooks)
    monkeypatch.setattr(github_app, "create_repo_hook", create_repo_hook)
    monkeypatch.setattr(github_app, "update_repo_hook", update_repo_hook)
    return calls


# --- event derivation --------------------------------------------------------


def test_builtin_hook_events_in_lockstep_with_catalog():
    """Every built-in event must name the GitHub webhook event that delivers it — a new
    built-in can't ship without its registration mapping (and vice versa)."""
    assert set(BUILTIN_HOOK_EVENTS) == set(GITHUB_EVENTS)


def test_hook_events_derived_from_builtin_sensors():
    sensors = [
        _sensor(emits="Github.Push"),
        _sensor(emits="Github.PullRequestOpened"),
        _sensor(emits="Github.PullRequestMerged"),  # same GitHub event as Opened — deduped
        _sensor("/hooks/sentry", emits="Incident", source="module"),  # other path: ignored
        _sensor(kind="poll", emits="CodeTask", source="module"),  # not a webhook: ignored
    ]
    assert github_hook_events(sensors) == ["pull_request", "push"]


def test_hook_events_widen_to_full_set_for_custom_github_sensor():
    """A user sensor on the shared path could need any event — over-deliver, never gap."""
    sensors = [_sensor(emits="Github.Push"), _sensor(emits="MyEvent", source="module")]
    assert github_hook_events(sensors) == ALL_HOOK_EVENTS


def test_hook_events_default_to_full_set_with_no_github_sensors():
    """Forward-looking wiring: a later-added `on: Github.*` fires with no re-registration."""
    assert github_hook_events([]) == ALL_HOOK_EVENTS


# --- the registration sync ---------------------------------------------------


def test_sync_creates_hook_and_lands_secret(tmp_path, monkeypatch):
    """No hook yet → one is created at base+path, and the generated signing secret is
    written to loopy.env so `loopy run` verifies deliveries with no manual step."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    _write_app_creds(tmp_path)
    calls = _stub_github(monkeypatch)

    report = sync_github_webhooks(tmp_path, repos=["me/app"], events=["push"], public_url=_URL)

    assert [r.action for r in report.results] == ["created"]
    (slug, url, secret, events) = calls["created"][0]
    assert (slug, url, events) == ("me/app", _DELIVERY, ["push"])
    assert report.secret_written
    assert load_control_plane_env(tmp_path)["GITHUB_WEBHOOK_SECRET"] == secret


def test_sync_updates_hook_already_on_our_url(tmp_path, monkeypatch):
    """A hook already delivering to our URL is converged in place, never duplicated."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    _write_app_creds(tmp_path)
    existing = {"id": 7, "config": {"url": _DELIVERY}, "events": ["push"], "active": True}
    calls = _stub_github(monkeypatch, hooks_by_repo={"me/app": [existing]})

    report = sync_github_webhooks(
        tmp_path, repos=["me/app"], events=["push", "issues"], public_url=_URL
    )

    assert [r.action for r in report.results] == ["updated"]
    assert calls["created"] == []
    assert calls["updated"][0][:2] == ("me/app", 7)
    assert calls["updated"][0][4] == ["issues", "push"]  # events converged (sorted)


def test_sync_reuses_existing_secret(tmp_path, monkeypatch):
    """A GITHUB_WEBHOOK_SECRET already in loopy.env is what GitHub gets — never regenerated,
    so a re-run can't silently invalidate the secret `loopy run` verifies with."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    _write_app_creds(tmp_path)
    write_control_plane_env(tmp_path, {"GITHUB_WEBHOOK_SECRET": "sekrit"})
    calls = _stub_github(monkeypatch)

    report = sync_github_webhooks(tmp_path, repos=["me/app"], events=["push"], public_url=_URL)

    assert calls["created"][0][2] == "sekrit"
    assert not report.secret_written
    assert load_control_plane_env(tmp_path)["GITHUB_WEBHOOK_SECRET"] == "sekrit"


def test_sync_check_reports_without_writing(tmp_path, monkeypatch):
    """--check classifies each repo (ok / stale / missing) and never writes — no hook
    calls, no generated secret in loopy.env."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    _write_app_creds(tmp_path)
    ok = {"id": 1, "config": {"url": _DELIVERY}, "events": ["push"], "active": True}
    stale = {"id": 2, "config": {"url": _DELIVERY}, "events": ["issues"], "active": True}
    calls = _stub_github(
        monkeypatch, hooks_by_repo={"me/ok": [ok], "me/stale": [stale], "me/missing": []}
    )

    report = sync_github_webhooks(
        tmp_path,
        repos=["me/ok", "me/stale", "me/missing"],
        events=["push"],
        public_url=_URL,
        check=True,
    )

    assert [r.action for r in report.results] == ["ok", "stale", "missing"]
    assert calls["created"] == [] and calls["updated"] == []
    assert not report.secret_written
    assert "GITHUB_WEBHOOK_SECRET" not in load_control_plane_env(tmp_path)


def test_sync_per_repo_error_does_not_abort_the_rest(tmp_path, monkeypatch):
    """An uninstalled repo lands as an actionable `error`; the other repos still register."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    _write_app_creds(tmp_path)
    calls = _stub_github(
        monkeypatch, find_errors={"me/gone": github_app.GitHubAPIError(404, "Not Found")}
    )

    report = sync_github_webhooks(
        tmp_path, repos=["me/gone", "me/app"], events=["push"], public_url=_URL
    )

    by_repo = {r.repo: r for r in report.results}
    assert by_repo["me/gone"].action == "error"
    assert "not installed" in by_repo["me/gone"].detail
    assert by_repo["me/app"].action == "created"
    assert [c[0] for c in calls["created"]] == ["me/app"]


def test_sync_403_names_the_missing_permission(tmp_path, monkeypatch):
    """A 403 is almost always the pre-existing App without the Webhooks permission —
    say so, with the fix, instead of echoing a bare status code."""
    _write_app_creds(tmp_path)
    _stub_github(monkeypatch, list_error=github_app.GitHubAPIError(403, "Forbidden"))

    report = sync_github_webhooks(tmp_path, repos=["me/app"], events=["push"], public_url=_URL)

    assert report.results[0].action == "error"
    assert "Webhooks" in report.results[0].detail
    assert "--force" in report.results[0].detail


def test_sync_without_app_raises_missing_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    with pytest.raises(github_app.MissingCredentials):
        sync_github_webhooks(tmp_path, repos=["me/app"], events=["push"], public_url=_URL)


# --- the CLI commands --------------------------------------------------------


def _github_project(tmp_path, *, repos=("me/app",)):
    """A compiled project that actually listens for GitHub webhooks: the coding scaffold
    plus one workflow triggered by a built-in `Github.*` event."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo", repos=list(repos))
    step = target / "workflows" / "watch" / "on-push.md"
    step.parent.mkdir(parents=True)
    step.write_text("---\non: Github.Push\nagent: Claude\n---\nSay hi to {{ event.pusher }}.\n")
    result = compile_project(target)
    assert result.diagnostics.items == [], [d.render() for d in result.diagnostics.items]
    return target, result.project


def test_cli_github_registers(tmp_path, monkeypatch):
    target, _ = _github_project(tmp_path)
    _write_app_creds(target)
    write_control_plane_env(target, {"LOOPY_PUBLIC_URL": _URL})
    monkeypatch.delenv("LOOPY_PUBLIC_URL", raising=False)
    seen = {}

    def fake_sync(root, *, repos, events, public_url, check=False):
        seen.update(repos=repos, events=events, public_url=public_url, check=check)
        return SyncReport(results=[HookSync("me/app", "created", f"→ {_DELIVERY}")])

    monkeypatch.setattr(webhooks, "sync_github_webhooks", fake_sync)
    result = runner.invoke(app, ["webhooks", "github", "--root", str(target)])

    assert result.exit_code == 0, result.output
    assert seen == {
        "repos": ["me/app"],
        "events": ["push"],  # derived from the one Github.Push trigger — not the full set
        "public_url": _URL,
        "check": False,
    }
    assert "me/app: created" in result.output


def test_cli_github_check_exits_nonzero_on_gaps(tmp_path, monkeypatch):
    target, _ = _github_project(tmp_path)
    _write_app_creds(target)
    write_control_plane_env(target, {"LOOPY_PUBLIC_URL": _URL})
    monkeypatch.delenv("LOOPY_PUBLIC_URL", raising=False)
    monkeypatch.setattr(
        webhooks,
        "sync_github_webhooks",
        lambda root, **kw: SyncReport(results=[HookSync("me/app", "missing", "no webhook")]),
    )

    result = runner.invoke(app, ["webhooks", "github", "--check", "--root", str(target)])

    assert result.exit_code == 1
    assert "me/app: missing" in result.output
    assert "loopy webhooks github" in result.output  # points at the fix


def test_cli_github_requires_public_url(tmp_path, monkeypatch):
    """No public URL: the one missing-URL error names both ways to get one — set it by hand
    (bring-your-own) or let `loopy deploy bootstrap` mint it — since nothing is recorded to
    tell the two apart."""
    target, _ = _github_project(tmp_path)
    _write_app_creds(target)
    monkeypatch.delenv("LOOPY_PUBLIC_URL", raising=False)

    result = runner.invoke(app, ["webhooks", "github", "--root", str(target)])

    assert result.exit_code == 1
    assert "LOOPY_PUBLIC_URL" in result.output
    assert "loopy deploy bootstrap" in result.output


def test_cli_github_requires_repos(tmp_path, monkeypatch):
    """The minimal no-repo scaffold has no repos — nothing to hang a repo webhook on."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo")  # repo-less
    monkeypatch.delenv("LOOPY_PUBLIC_URL", raising=False)

    result = runner.invoke(app, ["webhooks", "github", "--url", _URL, "--root", str(target)])

    assert result.exit_code == 1
    assert "no repos declared" in result.output


def test_cli_github_url_flag_overrides_and_normalizes(tmp_path, monkeypatch):
    target, _ = _github_project(tmp_path)
    _write_app_creds(target)
    seen = {}

    def fake_sync(root, *, repos, events, public_url, check=False):
        seen["public_url"] = public_url
        return SyncReport(results=[HookSync("me/app", "created")])

    monkeypatch.setattr(webhooks, "sync_github_webhooks", fake_sync)
    result = runner.invoke(
        app, ["webhooks", "github", "--url", "loopy.example.com/", "--root", str(target)]
    )

    assert result.exit_code == 0, result.output
    assert seen["public_url"] == _URL  # scheme defaulted, trailing slash stripped


def test_cli_list_shows_delivery_urls(tmp_path, monkeypatch):
    target, _ = _github_project(tmp_path)
    write_control_plane_env(target, {"LOOPY_PUBLIC_URL": _URL})
    monkeypatch.delenv("LOOPY_PUBLIC_URL", raising=False)

    result = runner.invoke(app, ["webhooks", "list", "--root", str(target)])

    assert result.exit_code == 0, result.output
    assert _DELIVERY in result.output  # the full paste-ready URL
    assert "builtin:Github.Push" in result.output
    assert "loopy webhooks github" in result.output  # the github path names its register cmd


def test_cli_list_without_url_points_at_the_setting(tmp_path, monkeypatch):
    target, _ = _github_project(tmp_path)
    monkeypatch.delenv("LOOPY_PUBLIC_URL", raising=False)

    result = runner.invoke(app, ["webhooks", "list", "--root", str(target)])

    assert result.exit_code == 0, result.output
    assert "/hooks/github" in result.output
    assert "LOOPY_PUBLIC_URL" in result.output


def test_cli_list_with_no_webhook_sensors(tmp_path):
    target = tmp_path / "demo"
    scaffold_project(target, "demo")  # minimal scaffold — no sensors at all

    result = runner.invoke(app, ["webhooks", "list", "--root", str(target)])

    assert result.exit_code == 0, result.output
    assert "no webhook sensors" in result.output


# --- doctor findings ---------------------------------------------------------


def test_findings_empty_without_github_sensors(tmp_path):
    target = tmp_path / "demo"
    scaffold_project(target, "demo", repos=["me/app"])
    project = compile_project(target).project
    assert registration_findings(project, target, control_env={}) == []


def test_findings_flag_missing_public_url(tmp_path):
    target, project = _github_project(tmp_path)
    findings = registration_findings(project, target, control_env={})
    assert len(findings) == 1
    assert findings[0].level == "warn"
    assert "LOOPY_PUBLIC_URL" in findings[0].message
    assert "loopy webhooks github" in findings[0].hint


def test_findings_flag_unregistered_repos(tmp_path, monkeypatch):
    target, project = _github_project(tmp_path)
    _write_app_creds(target)
    monkeypatch.setattr(
        webhooks,
        "sync_github_webhooks",
        lambda root, **kw: SyncReport(results=[HookSync("me/app", "missing", "no webhook")]),
    )
    findings = registration_findings(
        project, target, control_env={"LOOPY_PUBLIC_URL": _URL, "GITHUB_APP_ID": "42"}
    )
    assert len(findings) == 1
    assert "me/app" in findings[0].message
    assert "loopy webhooks github" in findings[0].hint


def test_findings_quiet_without_app(tmp_path, monkeypatch):
    """No App ⇒ a hand-registered webhook is a legitimate setup we can't see into — the
    URL finding still fires (that's local), but no unverifiable live check."""
    target, project = _github_project(tmp_path)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    findings = registration_findings(project, target, control_env={"LOOPY_PUBLIC_URL": _URL})
    assert findings == []
