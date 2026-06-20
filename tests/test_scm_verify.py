"""SCM output verification (#19) — confirm a claimed `pr_url` exists on GitHub.

Stubs `github_app.get_pull_request` (the module's single network boundary for this check)
so the parsing and status mapping are exercised without touching the wire.
"""

from __future__ import annotations

import pytest

from loopy_runtime.scm import github_app, verify
from loopy_runtime.scm.verify import (
    SCMVerification,
    parse_pr_url,
    pr_url_targets,
    verify_pr_url,
)

# --- target extraction -------------------------------------------------------


def test_pr_url_targets_picks_pr_url_field():
    assert pr_url_targets({"pr_url": "https://github.com/o/r/pull/7", "summary": "x"}) == [
        ("pr_url", "https://github.com/o/r/pull/7")
    ]


def test_pr_url_targets_ignores_missing_blank_and_non_string():
    assert pr_url_targets({}) == []
    assert pr_url_targets({"pr_url": "   "}) == []
    assert pr_url_targets({"pr_url": 123}) == []


def test_pr_url_targets_strips_whitespace():
    assert pr_url_targets({"pr_url": "  https://github.com/o/r/pull/7  "}) == [
        ("pr_url", "https://github.com/o/r/pull/7")
    ]


# --- URL parsing -------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/octo/repo/pull/42", ("octo", "repo", 42)),
        ("http://github.com/octo/repo/pull/1", ("octo", "repo", 1)),
        ("see https://github.com/a/b/pull/9 please", ("a", "b", 9)),
        ("https://example.com/a/b/pull/9", None),
        ("https://github.com/a/b/issues/9", None),
        ("not a url", None),
    ],
)
def test_parse_pr_url(url, expected):
    assert parse_pr_url(url) == expected


# --- verification status mapping ---------------------------------------------


def test_verify_confirmed_on_200(monkeypatch):
    monkeypatch.setattr(github_app, "get_pull_request", lambda *a, **k: {"number": 42})
    result = verify_pr_url("pr_url", "https://github.com/o/r/pull/42", "tok")
    assert result.status == "confirmed"
    assert result.confirmed and not result.fabricated


def test_verify_malformed_url_does_not_call_api(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("API should not be called for a malformed URL")

    monkeypatch.setattr(github_app, "get_pull_request", boom)
    result = verify_pr_url("pr_url", "https://example.com/nope", "tok")
    assert result.status == "malformed"
    assert result.fabricated


def test_verify_not_found_on_404(monkeypatch):
    def raise_404(*a, **k):
        raise github_app.GitHubAPIError(404, "Not Found")

    monkeypatch.setattr(github_app, "get_pull_request", raise_404)
    result = verify_pr_url("pr_url", "https://github.com/o/r/pull/99", "tok")
    assert result.status == "not_found"
    assert result.fabricated


def test_verify_unverifiable_on_other_api_error(monkeypatch):
    def raise_500(*a, **k):
        raise github_app.GitHubAPIError(503, "Service Unavailable")

    monkeypatch.setattr(github_app, "get_pull_request", raise_500)
    result = verify_pr_url("pr_url", "https://github.com/o/r/pull/5", "tok")
    assert result.status == "unverifiable"
    assert not result.fabricated  # transient — not the agent's fault


def test_verify_unverifiable_on_transport_error(monkeypatch):
    def raise_conn(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(github_app, "get_pull_request", raise_conn)
    result = verify_pr_url("pr_url", "https://github.com/o/r/pull/5", "tok")
    assert result.status == "unverifiable"
    assert not result.fabricated


def test_verify_passes_parsed_components_to_api(monkeypatch):
    seen = {}

    def capture(token, owner, repo, number, *, base_url):
        seen.update(token=token, owner=owner, repo=repo, number=number, base_url=base_url)
        return {"number": number}

    monkeypatch.setattr(github_app, "get_pull_request", capture)
    verify_pr_url("pr_url", "https://github.com/octo/repo/pull/42", "tok", base_url="https://api.x")
    assert seen == {
        "token": "tok",
        "owner": "octo",
        "repo": "repo",
        "number": 42,
        "base_url": "https://api.x",
    }


def test_as_dict_shape():
    v = SCMVerification("pr_url", "u", "not_found", "no PR")
    assert v.as_dict() == {"field": "pr_url", "url": "u", "status": "not_found", "detail": "no PR"}


def test_module_exposes_verify_alias():
    # The runtime imports the helpers by name; guard against a rename.
    assert hasattr(verify, "verify_pr_url") and hasattr(verify, "pr_url_targets")
