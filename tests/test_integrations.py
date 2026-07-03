"""`loopy integrations` — list built-in providers and show one by name.

Drives the plain-function core `run_integrations` against a real compiled project and reads
its output via capsys. Status comes from the compiled registry (which built-in events are
referenced) and the merged control-plane env (which secrets are set), so the tests build a
temp project and put config in its `loopy.env` rather than touching the process env.
"""

from __future__ import annotations

import pytest
import typer

from loopy_cli.integrations import run_integrations
from tests.helpers import write_project

REGISTRY = (
    "defaults: { agent: { sandbox: default, model: claude-sonnet-4-6, harness: claude-code } }\n"
    "sandboxes: { default: { provider: local } }\n"
    "agents: { Investigator: {} }\n"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate from any real provider config in the host environment."""
    for var in ("SENTRY_WEBHOOK_SECRET", "GITHUB_WEBHOOK_SECRET", "LOOPY_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)


def _sentry_project(root, *, secret=True, public=True):
    files = {
        "registry.yml": REGISTRY,
        "workflows/triage/t.md": (
            "---\non: Sentry.IssueCreated\nagent: Investigator\n---\nTriage {{ event.title }}.\n"
        ),
    }
    env = []
    if secret:
        env.append("SENTRY_WEBHOOK_SECRET=abc123")
    if public:
        env.append("LOOPY_PUBLIC_URL=https://loops.example.com")
    if env:
        files["loopy.env"] = "\n".join(env) + "\n"
    write_project(root, files)
    return root


# ── list view ───────────────────────────────────────────────────────────────


def test_list_shows_every_provider_with_usage_and_secret(tmp_path, capsys):
    _sentry_project(tmp_path, secret=True)
    run_integrations(None, tmp_path)
    out = capsys.readouterr().out
    assert "github" in out and "sentry" in out
    assert "/hooks/sentry" in out
    # sentry is used (1 event) and its secret is set; github is neither.
    sentry_line = next(ln for ln in out.splitlines() if ln.strip().startswith("sentry"))
    assert "1 event" in sentry_line
    github_line = next(ln for ln in out.splitlines() if ln.strip().startswith("github"))
    assert "unused" in github_line


def test_list_is_the_default_and_list_keyword_agrees(tmp_path, capsys):
    _sentry_project(tmp_path)
    run_integrations(None, tmp_path)
    default_out = capsys.readouterr().out
    run_integrations("list", tmp_path)
    list_out = capsys.readouterr().out
    assert default_out == list_out


# ── detail view ───────────────────────────────────────────────────────────────


def test_detail_marks_used_events_and_composes_delivery_url(tmp_path, capsys):
    _sentry_project(tmp_path, secret=True, public=True)
    run_integrations("sentry", tmp_path)
    out = capsys.readouterr().out
    assert "https://loops.example.com/hooks/sentry" in out
    assert "SENTRY_WEBHOOK_SECRET" in out
    # The referenced event is marked used; a sibling event is listed but not.
    used_line = next(ln for ln in out.splitlines() if "Sentry.IssueCreated" in ln)
    assert "(used)" in used_line
    resolved_line = next(ln for ln in out.splitlines() if "Sentry.IssueResolved" in ln)
    assert "(used)" not in resolved_line


def test_detail_without_public_url_prompts_to_set_it(tmp_path, capsys):
    _sentry_project(tmp_path, secret=False, public=False)
    run_integrations("sentry", tmp_path)
    out = capsys.readouterr().out
    assert "LOOPY_PUBLIC_URL" in out  # delivery URL line points at the missing var
    assert "loopy auth sentry" in out  # the secret fix hint


def test_detail_accepts_an_event_name(tmp_path, capsys):
    _sentry_project(tmp_path)
    run_integrations("Sentry.AlertTriggered", tmp_path)
    out = capsys.readouterr().out
    assert "\n  sentry" in out  # resolved to the sentry provider


def test_unknown_integration_exits_with_known_list(tmp_path, capsys):
    _sentry_project(tmp_path)
    with pytest.raises(typer.Exit):
        run_integrations("slack", tmp_path)
    err = capsys.readouterr().err
    assert "unknown integration 'slack'" in err
    assert "github" in err and "sentry" in err
