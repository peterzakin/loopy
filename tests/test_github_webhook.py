"""GitHub webhook ingress: HMAC signature verification + one-URL fan-out.

Covers the runtime plumbing the `examples/github` example relies on — a signed delivery to a
single path is verified once and fanned out to every sensor on that path, with only the
matching sensor emitting.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from loopy_runtime.contract import Event
from loopy_runtime.scm.github_webhook import (
    SignatureError,
    signature_verifier,
    verify_signature,
)
from loopy_runtime.sensors.runner import FastAPISensorRunner

SECRET = "s3cr3t"


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class RecordingReceiver:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def receive(self, event: Event) -> None:
        self.seen.append(event.name)


def _pr_event(name: str) -> Event:
    return Event(name=name, fields={}, id="e", emitted_at=datetime.now(UTC))


# ── signature helper ──────────────────────────────────────────────────────────────
def test_verify_signature_roundtrip():
    body = b'{"action":"opened"}'
    verify_signature(SECRET, body, _sig(body))  # does not raise


def test_verify_signature_rejects_mismatch():
    body = b'{"action":"opened"}'
    with pytest.raises(SignatureError):
        verify_signature(SECRET, body, _sig(body, secret="wrong"))


def test_verify_signature_rejects_missing_header():
    with pytest.raises(SignatureError):
        verify_signature(SECRET, b"{}", None)


# ── fan-out: two sensors on one path, only the matching one emits ───────────────────
def test_fan_out_emits_only_matching_sensor():
    receiver = RecordingReceiver()
    host = FastAPISensorRunner(receiver)

    def opened(payload: dict) -> Event | None:
        return _pr_event("PullRequestOpened") if payload.get("action") == "opened" else None

    def merged(payload: dict) -> Event | None:
        pr = payload.get("pull_request") or {}
        if payload.get("action") == "closed" and pr.get("merged"):
            return _pr_event("PullRequestMerged")
        return None

    host.register_webhook("/hooks/github", opened)
    host.register_webhook("/hooks/github", merged)
    assert host.webhook_paths == ["/hooks/github"]  # one path, not two

    result = asyncio.run(host._dispatch_path("/hooks/github", {"action": "opened"}))
    assert result == {"emitted": ["PullRequestOpened"]}
    assert receiver.seen == ["PullRequestOpened"]


# ── signed route via the live ASGI app ──────────────────────────────────────────────
def _client(host: FastAPISensorRunner):
    from fastapi.testclient import TestClient

    return TestClient(host.app)


def test_signed_route_accepts_valid_signature():
    receiver = RecordingReceiver()
    host = FastAPISensorRunner(receiver)

    def opened(payload: dict) -> Event | None:
        return _pr_event("PullRequestOpened") if payload.get("action") == "opened" else None

    host.register_webhook("/hooks/github", opened, verify=signature_verifier(SECRET))
    body = json.dumps({"action": "opened"}).encode()
    resp = _client(host).post(
        "/hooks/github", content=body, headers={"X-Hub-Signature-256": _sig(body)}
    )
    assert resp.status_code == 200
    assert resp.json() == {"emitted": ["PullRequestOpened"]}
    assert receiver.seen == ["PullRequestOpened"]


def test_signed_route_rejects_bad_signature_with_401():
    host = FastAPISensorRunner(RecordingReceiver())
    host.register_webhook(
        "/hooks/github",
        lambda payload: _pr_event("PullRequestOpened"),
        verify=signature_verifier(SECRET),
    )
    body = json.dumps({"action": "opened"}).encode()
    resp = _client(host).post(
        "/hooks/github", content=body, headers={"X-Hub-Signature-256": _sig(body, secret="nope")}
    )
    assert resp.status_code == 401
