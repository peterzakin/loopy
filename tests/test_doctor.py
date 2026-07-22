"""`loopy doctor` — the first-run preflight that catches runnable-vs-valid gaps.

The point of `doctor` is the inverse of `loopy compile`: a freshly scaffolded project compiles
green yet can't run, and these tests pin that exact gap — the placeholder API key, the
unpushable starter repo, and missing git auth — plus the clean state where all three are fixed.
The logic under test (`diagnose`) is pure; the tests drive it through real compiled projects.
"""

from __future__ import annotations

import pytest

from loopy_cli.doctor import (
    PLACEHOLDER_REPO,
    _placeholder_reason,
    _repo_slug,
    check_repo_access,
    diagnose,
    next_actions,
)
from loopy_cli.scaffold import scaffold_project
from loopy_core.compile.pipeline import compile_project
from loopy_runtime.scm import github_app
from loopy_runtime.secrets import _parse_dotenv, load_control_plane_env
from tests.helpers import write_project


def _diagnose(root, *, control_plane_env=None):
    """Wire `diagnose` exactly as the CLI does, but with an explicit control-plane env so the
    test never inherits a real `GITHUB_APP_ID` from the host environment."""
    result = compile_project(root)
    assert result.project is not None, [d.render() for d in result.diagnostics.items]

    def read_env(rel: str):
        path = root / rel
        return _parse_dotenv(path.read_text()) if path.is_file() else None

    merged = dict(control_plane_env or {})
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)
    return diagnose(result.project.registry, read_env=read_env, control_plane_env=merged)


def _fix_key(root) -> None:
    """Replace the placeholder model key — the one scaffold gap unrelated to repos/auth."""
    dev = root / "secrets/base.env"
    dev.write_text(dev.read_text().replace("sk-ant-...", "sk-ant-real-key"))


def test_starter_repo_scaffold_flags_all_three_gaps(tmp_path):
    # A project pointed at the unpushable starter repo trips every gap: placeholder key, the
    # repo itself, and (because it now declares a repo) missing git auth.
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["octocat/Hello-World"])

    findings = _diagnose(root)
    errors = {f.message for f in findings if f.level == "error"}
    warns = {f.message for f in findings if f.level == "warn"}

    assert any("ANTHROPIC_API_KEY" in m for m in errors)
    assert any("repos:" in m for m in errors)
    assert any("git auth" in m for m in warns)


def test_blank_repo_scaffold_flags_only_the_model_key(tmp_path):
    # The no-repo scaffold is a minimal registry with no workflow. The only gap is the
    # placeholder model key — there's no repo to push to, so no repo gap and no git auth to wire.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")

    findings = _diagnose(root)
    assert any(f.level == "error" and "ANTHROPIC_API_KEY" in f.message for f in findings)
    assert not any("repos:" in f.message for f in findings)
    assert not any("git auth" in f.message for f in findings)


def test_clean_project_has_no_findings(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["me/my-fork"])
    _fix_key(root)

    # A configured App (GITHUB_APP_ID present at the control plane) satisfies git auth, and the
    # scaffold's `provider: daytona` sandbox needs DAYTONA_API_KEY at the control plane too.
    findings = _diagnose(
        root, control_plane_env={"GITHUB_APP_ID": "123456", "DAYTONA_API_KEY": "dtn-real"}
    )
    assert findings == [], [f.message for f in findings]


def test_missing_env_file_is_an_error(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    (root / "secrets/base.env").unlink()  # compile still passes — env_file is a reference only

    findings = _diagnose(root)
    assert any(f.level == "error" and "missing" in f.message for f in findings)


def test_github_token_in_env_file_satisfies_auth(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["me/app"])  # a declared repo makes git auth relevant
    dev = root / "secrets/base.env"
    dev.write_text(dev.read_text().replace("# GITHUB_TOKEN=ghp_...", "GITHUB_TOKEN=ghp_real"))

    findings = _diagnose(root)
    # The key placeholder still flags, but git auth is now satisfied (no warn).
    assert not any(f.level == "warn" and "git auth" in f.message for f in findings)


def test_no_auth_warning_when_no_repos_declared(tmp_path):
    # The default scaffold declares no repos — nothing needs cloning, so auth isn't required.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)

    # Still needs the Daytona key: the default sandbox runs on provider: daytona.
    findings = _diagnose(root, control_plane_env={"DAYTONA_API_KEY": "dtn-real"})
    assert findings == [], [f.message for f in findings]


def test_missing_daytona_key_flags_error(tmp_path):
    # The scaffold sandbox runs on provider: daytona but ships DAYTONA_API_KEY commented out —
    # `run` dies mid-acquire with "DAYTONA_API_KEY is not set"; doctor must catch it up front.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)

    findings = _diagnose(root)  # no DAYTONA_API_KEY anywhere
    daytona = [f for f in findings if "DAYTONA_API_KEY" in f.message]
    assert len(daytona) == 1
    assert daytona[0].level == "error"
    assert "provider: daytona" in daytona[0].message


def test_daytona_key_present_clears_the_finding(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)

    findings = _diagnose(root, control_plane_env={"DAYTONA_API_KEY": "dtn-real"})
    assert not any("DAYTONA_API_KEY" in f.message for f in findings)


def test_non_daytona_provider_needs_no_daytona_key(tmp_path):
    # A sandbox on a different provider (docker/local) doesn't use Daytona, so its key is moot.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)
    reg = root / "registry.yml"
    reg.write_text(reg.read_text().replace("provider: daytona", "provider: docker"))

    findings = _diagnose(root)  # no DAYTONA_API_KEY, but no daytona sandbox either
    assert not any("DAYTONA_API_KEY" in f.message for f in findings)


def test_missing_tenki_key_flags_error_and_present_clears_it(tmp_path):
    # A sandbox on tenki needs an auth token at the control plane, same as daytona.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)
    reg = root / "registry.yml"
    reg.write_text(reg.read_text().replace("provider: daytona", "provider: tenki"))

    findings = _diagnose(root)  # no tenki token anywhere
    tenki = [f for f in findings if "TENKI_API_KEY" in f.message]
    assert len(tenki) == 1
    assert tenki[0].level == "error"
    assert "provider: tenki" in tenki[0].message

    # Either TENKI_API_KEY or TENKI_AUTH_TOKEN satisfies it (matching the provider's _ensure_client).
    cleared = _diagnose(root, control_plane_env={"TENKI_API_KEY": "tk-real"})
    assert not any("TENKI" in f.message for f in cleared)
    cleared_token = _diagnose(root, control_plane_env={"TENKI_AUTH_TOKEN": "ory_st_real"})
    assert not any("TENKI" in f.message for f in cleared_token)


@pytest.mark.parametrize(
    "url",
    [
        "octocat/Hello-World",
        "https://github.com/octocat/Hello-World",
        "https://github.com/octocat/Hello-World.git",
        "git@github.com:octocat/Hello-World.git",
    ],
)
def test_repo_slug_normalizes_to_starter(url):
    assert _repo_slug(url) == PLACEHOLDER_REPO


# --- the placeholder-value heuristic (warn, never error) --------------------


@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-...",  # leftover scaffold stub
        "ghp_abc123 # my prod token",  # inline comment folded into a literal dotenv value
        "<your-key-here>",  # angle-bracket template
        "changeme",
        "your-token",
        "REPLACE_ME",
        "example.com",
        "XXXXXXXX",
    ],
)
def test_placeholder_reason_flags_placeholder_values(value):
    assert _placeholder_reason(value) is not None


@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-real-key-9f3a2b",  # a plausible real key
        "ghp_16charsofrealtoken",
        "redis://localhost:6379",
        "",  # empty is "unset", not a placeholder
        "  ",
    ],
)
def test_placeholder_reason_passes_real_values(value):
    assert _placeholder_reason(value) is None


def test_inline_comment_in_env_file_is_a_warning(tmp_path):
    # A user pastes a real key but leaves an inline `# comment`; the literal dotenv value now
    # carries the comment and breaks at run. doctor must warn (not error) on it.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    dev = root / "secrets/base.env"
    dev.write_text(dev.read_text().replace("sk-ant-...", "sk-ant-real # prod key"))

    findings = _diagnose(root)
    hits = [f for f in findings if f.level == "warn" and "ANTHROPIC_API_KEY" in f.message]
    assert len(hits) == 1
    assert "#" in hits[0].message
    # ...and it is a warn, not the hard error reserved for the exact scaffold stub.
    assert not any(f.level == "error" and "ANTHROPIC_API_KEY" in f.message for f in findings)


def test_real_key_produces_no_placeholder_warning(tmp_path):
    # A genuine-looking value must not trip the heuristic (no false-positive warnings).
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)  # writes sk-ant-real-key

    findings = _diagnose(root, control_plane_env={"DAYTONA_API_KEY": "dtn-real"})
    assert findings == [], [f.message for f in findings]


# --- check_repo_access: the live install/repo-reachability preflight --------


def _registry(tmp_path, repos):
    """A compiled registry for a scaffold pointed at `repos` (drives the live check)."""
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=repos)
    result = compile_project(root)
    assert result.project is not None, [d.render() for d in result.diagnostics.items]
    return result.project.registry


def _stub_installation(monkeypatch, *, installations, reachable):
    """Stub the App's installation lookup + the repos a minted token can reach."""
    monkeypatch.setattr(github_app, "list_installations", lambda creds, **k: installations)
    monkeypatch.setattr(github_app, "mint_installation_token", lambda creds, i, **k: {"token": "t"})
    monkeypatch.setattr(
        github_app,
        "list_installation_repositories",
        lambda token, **k: {"repositories": [{"full_name": n} for n in reachable]},
    )


def test_check_repo_access_passes_when_repos_reachable(tmp_path, monkeypatch):
    registry = _registry(tmp_path, ["me/app", "me/lib"])
    _stub_installation(monkeypatch, installations=[{"id": 7}], reachable=["me/app", "me/lib"])
    assert check_repo_access(registry, object()) == []


def test_check_repo_access_flags_repo_not_in_selection(tmp_path, monkeypatch):
    # The App is installed, but a declared repo isn't among its selected repositories — the
    # exact gap that otherwise only surfaced as a clone 403 at run time.
    registry = _registry(tmp_path, ["me/app", "me/missing"])
    _stub_installation(monkeypatch, installations=[{"id": 7}], reachable=["me/app"])
    findings = check_repo_access(registry, object())
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "me/missing" in findings[0].message
    assert "selected repositories" in findings[0].message


def test_check_repo_access_flags_uninstalled_app(tmp_path, monkeypatch):
    registry = _registry(tmp_path, ["me/app"])
    monkeypatch.setattr(github_app, "list_installations", lambda creds, **k: [])
    findings = check_repo_access(registry, object())
    assert findings[0].level == "error"
    assert "not installed" in findings[0].message


def test_check_repo_access_skips_when_no_repos(tmp_path, monkeypatch):
    # A repo-less project clones nothing, so there's nothing to verify — and no network call.
    registry = _registry(tmp_path, [])

    def boom(*_a, **_k):
        raise AssertionError("should not call GitHub when no repos are declared")

    monkeypatch.setattr(github_app, "list_installations", boom)
    assert check_repo_access(registry, object()) == []


def test_check_repo_access_degrades_to_warn_on_error(tmp_path, monkeypatch):
    # A GitHub/network error must not fail the whole preflight — it degrades to one warn.
    registry = _registry(tmp_path, ["me/app"])

    def boom(creds, **_k):
        raise github_app.GitHubAppError("boom")

    monkeypatch.setattr(github_app, "list_installations", boom)
    findings = check_repo_access(registry, object())
    assert len(findings) == 1 and findings[0].level == "warn"


# ── built-in webhook secret (used provider, secret unset) ───────────────────

_SENTRY_PROJECT = {
    "registry.yml": (
        "defaults: { agent: { sandbox: default, model: claude-sonnet-4-6,"
        " harness: claude-code } }\n"
        "sandboxes: { default: { provider: local } }\n"
        "agents: { Investigator: {} }\n"
    ),
    "workflows/triage/t.md": "---\non: Sentry.IssueCreated\nagent: Investigator\n---\nTriage it.\n",
}


def test_used_builtin_provider_warns_when_secret_unset(tmp_path):
    write_project(tmp_path, _SENTRY_PROJECT)
    findings = _diagnose(tmp_path)  # no SENTRY_WEBHOOK_SECRET in env
    warn = next((f for f in findings if "SENTRY_WEBHOOK_SECRET" in f.message), None)
    assert warn is not None and warn.level == "warn"
    assert "loopy auth sentry" in (warn.hint or "")


def test_builtin_secret_warning_clears_when_set(tmp_path):
    write_project(tmp_path, _SENTRY_PROJECT)
    findings = _diagnose(tmp_path, control_plane_env={"SENTRY_WEBHOOK_SECRET": "s3cr3t"})
    assert not any("SENTRY_WEBHOOK_SECRET" in f.message for f in findings)


def test_unused_provider_never_warns(tmp_path):
    """A project that triggers on no built-in event gets no secret finding at all."""
    write_project(
        tmp_path,
        {
            "registry.yml": _SENTRY_PROJECT["registry.yml"] + "events:\n  CodeTask: { id: str }\n",
            "workflows/triage/t.md": "---\non: CodeTask\nagent: Investigator\n---\nWork.\n",
        },
    )
    findings = _diagnose(tmp_path)
    assert not any("WEBHOOK_SECRET" in f.message for f in findings)


def test_github_builtin_secret_is_not_double_warned(tmp_path):
    """GitHub's delivery wiring is reported by registration_findings, so the generic secret
    check skips it — a `Github.*` project with no secret gets no SENTRY-style finding here."""
    write_project(
        tmp_path,
        {
            "registry.yml": _SENTRY_PROJECT["registry.yml"],
            "workflows/review/r.md": (
                "---\non: Github.PullRequestOpened\nagent: Investigator\n---\nReview.\n"
            ),
        },
    )
    findings = _diagnose(tmp_path)
    assert not any("GITHUB_WEBHOOK_SECRET" in f.message for f in findings)


# --- next_actions: the guided ladder a bare `loopy` prints in a project dir -----------------


def _next_actions(root, *, control_plane_env=None):
    """Wire `next_actions` exactly as the bare-`loopy` path does, with an explicit control-plane
    env so the test never inherits a real `GITHUB_APP_ID`/`LOOPY_PUBLIC_URL` from the host."""
    result = compile_project(root)
    assert result.project is not None, [d.render() for d in result.diagnostics.items]

    def read_env(rel: str):
        path = root / rel
        return _parse_dotenv(path.read_text()) if path.is_file() else None

    merged = dict(control_plane_env or {})
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)
    return next_actions(result.project, read_env=read_env, control_plane_env=merged)


def test_next_actions_fresh_coding_project_wants_url_then_auth(tmp_path):
    # A coding scaffold clones a repo and listens on /hooks/github but has neither a public URL
    # nor git auth: the ladder is "get a URL, then wire auth", in that order. Webhooks can't be
    # registered yet (no App), so that rung is absent.
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["me/app"])

    actions = _next_actions(root)
    commands = [a.command for a in actions]
    assert commands == ["loopy deploy bootstrap", "loopy auth github"]


def test_next_actions_with_app_and_url_wants_webhooks(tmp_path):
    # Once an App and a public URL exist, the only step left is pointing GitHub's repo webhooks
    # at the engine — the ladder's third rung.
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["me/app"])

    actions = _next_actions(
        root, control_plane_env={"GITHUB_APP_ID": "123", "LOOPY_PUBLIC_URL": "https://x.example"}
    )
    assert [a.command for a in actions] == ["loopy webhooks github"]


def test_next_actions_token_auth_needs_no_auth_or_webhook_rung(tmp_path):
    # A GITHUB_TOKEN in the env_file satisfies git auth, so the auth rung is gone. Webhook
    # registration goes through App creds, so a token alone doesn't unlock it (manual wiring is
    # a fine setup) — with a URL already set, nothing is suggested.
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["me/app"])
    dev = root / "secrets/base.env"
    dev.write_text(dev.read_text().replace("# GITHUB_TOKEN=ghp_...", "GITHUB_TOKEN=ghp_real"))

    actions = _next_actions(root, control_plane_env={"LOOPY_PUBLIC_URL": "https://x.example"})
    assert actions == []


def test_next_actions_empty_when_nothing_left_to_wire(tmp_path):
    # A project that clones no repos and has no webhook sensors needs none of the three rungs.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")  # blank scaffold: no repos, no /hooks/github sensor

    assert _next_actions(root) == []
