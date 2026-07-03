"""Built-in Sentry events: zero-config `Sentry.*` triggers.

The Sentry half of the built-in catalog (`tests/test_builtins.py` covers the GitHub half
and the shared contract/mapper drift guard): compile-time injection on `/hooks/sentry`,
the reserved-namespace guard (E215), unknown built-ins scoped to the Sentry catalog
(E112), the payload mappers against Sentry's documented `issue` webhook shape, and the
`Sentry-Hook-Signature` ingress check.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from loopy_core.builtins import SENTRY_EVENTS
from loopy_core.compile import codes
from loopy_core.compile.pipeline import compile_project
from loopy_runtime.scm.github_webhook import SignatureError
from loopy_runtime.scm.sentry_builtins import MAPPERS
from loopy_runtime.scm.sentry_webhook import verify_signature
from tests.helpers import assert_code, write_project
from tests.helpers import codes as result_codes

# A minimal project that compiles clean — one local sandbox + agent, no events declared.
REGISTRY = (
    "defaults: { agent: { sandbox: default, model: claude-sonnet-4-6, harness: claude-code } }\n"
    "sandboxes: { default: { provider: local } }\n"
    "agents: { Investigator: {} }\n"
)


def md(frontmatter: str, body: str = "Do the work.") -> str:
    return f"---\n{frontmatter}\n---\n{body}\n"


def issue_payload(action: str) -> dict:
    """A representative `issue` resource delivery (Sentry's documented payload shape)."""
    return {
        "action": action,
        "installation": {"uuid": "a0d64dfe-0000-0000-0000-000000000000"},
        "data": {
            "issue": {
                "id": "1170820242",
                "shortId": "SAMPLE-1",
                "title": "ReferenceError: blooopy is not defined",
                "culprit": "/(most recent call last)",
                "level": "error",
                "status": "unresolved",
                "project": {"id": 1, "name": "Sample", "slug": "sample"},
                "permalink": "https://sentry.io/organizations/acme/issues/1170820242/",
            }
        },
        "actor": {"type": "application", "id": "sentry", "name": "Sentry"},
    }


def event_alert_payload() -> dict:
    """A representative `event_alert` delivery (an issue-alert rule firing).

    Shape reconciled from Sentry's webhook docs; `data.event` is a full error event and
    `data.triggered_rule` names the rule. Reconcile field paths against a live delivery if
    Sentry's payload drifts (the mapper reads defensively, so extras are ignored)."""
    return {
        "action": "triggered",
        "installation": {"uuid": "a0d64dfe-0000-0000-0000-000000000000"},
        "data": {
            "event": {
                "event_id": "0b3b0adc94f74b1b9bde9d3f9b2b0f6e",
                "issue_id": "1170820242",
                "project": 1,  # a numeric id here, not a slug — why the contract omits project
                "title": "ReferenceError: widget is not defined",
                "culprit": "app/views.py in get_widgets",
                "level": "error",
                "web_url": "https://sentry.io/organizations/acme/issues/1170820242/events/0b3b/",
                "url": "https://sentry.io/api/0/projects/acme/backend/events/0b3b/",
                "issue_url": "https://sentry.io/api/0/organizations/acme/issues/1170820242/",
            },
            "triggered_rule": "High error rate",
        },
        "actor": {"type": "application", "id": "sentry", "name": "Sentry"},
    }


# ── compile-time injection ──────────────────────────────────────────────────


def test_workflow_triggers_on_sentry_builtin_with_no_declarations(tmp_path):
    """`on: Sentry.IssueCreated` compiles clean with no event entry and no sensor file."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/t.md": md(
                "on: Sentry.IssueCreated\nagent: Investigator",
                body="Triage {{ event.title }} ({{ event.level }}) at {{ event.url }}.",
            ),
        },
    )
    result = compile_project(tmp_path)
    assert not result.diagnostics.has_errors(), result_codes(result)

    # The event contract was injected as a real registered (built-in) event.
    event = result.project.registry.events["Sentry.IssueCreated"]
    assert event.builtin is True
    assert event.fields["level"]["enum"] == ["debug", "info", "warning", "error", "fatal"]

    # A built-in sensor was synthesized on /hooks/sentry with no user module.
    sensors = [s for s in result.project.sensors if s.emits == "Sentry.IssueCreated"]
    assert len(sensors) == 1
    s = sensors[0]
    assert s.source == "builtin"
    assert s.provider == "sentry"
    assert s.module is None and s.fn is None
    assert s.trigger.kind == "webhook" and s.trigger.path == "/hooks/sentry"


def test_github_and_sentry_builtins_coexist(tmp_path):
    """One project can trigger on both providers; each sensor lands on its own path."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: Investigator"),
            "workflows/triage/t.md": md("on: Sentry.IssueCreated\nagent: Investigator"),
        },
    )
    result = compile_project(tmp_path)
    assert not result.diagnostics.has_errors(), result_codes(result)
    paths = {s.emits: s.trigger.path for s in result.project.sensors if s.source == "builtin"}
    assert paths == {
        "Github.PullRequestOpened": "/hooks/github",
        "Sentry.IssueCreated": "/hooks/sentry",
    }


def test_unknown_sentry_builtin_reports_e112_scoped_to_sentry_catalog(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/t.md": md("on: Sentry.Nonexistent\nagent: Investigator"),
        },
    )
    result = compile_project(tmp_path)
    assert_code(result, codes.E112)
    message = next(d.message for d in result.diagnostics.items if d.code == codes.E112)
    # The "known built-ins" list is scoped to the matched provider, not the whole catalog.
    assert "Sentry.IssueCreated" in message
    assert "Github." not in message


def test_user_event_in_sentry_namespace_reports_e215(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY + "events:\n  Sentry.Custom: { x: str }\n",
            "workflows/triage/t.md": md("on: Sentry.IssueCreated\nagent: Investigator"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E215)


# ── payload mappers ─────────────────────────────────────────────────────────


def test_issue_created_maps_exactly_its_contract_fields():
    fields = MAPPERS["Sentry.IssueCreated"](issue_payload("created"))
    assert fields is not None
    assert set(fields) == set(SENTRY_EVENTS["Sentry.IssueCreated"])
    assert fields["issue_id"] == "1170820242"
    assert fields["level"] == "error"
    assert fields["project"] == "sample"
    assert fields["url"].endswith("/issues/1170820242/")


def test_issue_resolved_maps_exactly_its_contract_fields():
    fields = MAPPERS["Sentry.IssueResolved"](issue_payload("resolved"))
    assert fields is not None
    assert set(fields) == set(SENTRY_EVENTS["Sentry.IssueResolved"])
    assert fields["issue_id"] == "1170820242"


def test_alert_triggered_maps_exactly_its_contract_fields():
    fields = MAPPERS["Sentry.AlertTriggered"](event_alert_payload())
    assert fields is not None
    assert set(fields) == set(SENTRY_EVENTS["Sentry.AlertTriggered"])
    assert fields["issue_id"] == "1170820242"
    assert fields["title"] == "ReferenceError: widget is not defined"
    assert fields["level"] == "error"
    assert fields["rule"] == "High error rate"
    assert fields["url"].endswith("/events/0b3b/")


def test_alert_rule_falls_back_to_legacy_triggering_rules():
    """Older payloads carry `triggering_rules` (a list) instead of `triggered_rule`."""
    payload = event_alert_payload()
    del payload["data"]["triggered_rule"]
    payload["data"]["triggering_rules"] = ["Legacy rule"]
    assert MAPPERS["Sentry.AlertTriggered"](payload)["rule"] == "Legacy rule"


def test_mappers_ignore_unrelated_deliveries():
    """Each mapper returns None for a delivery that isn't its concern (the fan-out contract)."""
    # The other event's action.
    assert MAPPERS["Sentry.IssueCreated"](issue_payload("resolved")) is None
    assert MAPPERS["Sentry.IssueResolved"](issue_payload("created")) is None
    # An issue delivery is not an alert (no data.event); an alert is not an issue.
    assert MAPPERS["Sentry.AlertTriggered"](issue_payload("created")) is None
    assert MAPPERS["Sentry.IssueCreated"](event_alert_payload()) is None
    assert MAPPERS["Sentry.IssueResolved"](event_alert_payload()) is None
    # A non-issue, non-alert delivery (e.g. an installation event).
    installation = {"action": "created", "data": {"installation": {"uuid": "x"}}}
    assert MAPPERS["Sentry.IssueCreated"](installation) is None
    assert MAPPERS["Sentry.AlertTriggered"](installation) is None


def test_null_permalink_falls_back_to_empty_url():
    """Sentry's docs sample delivers `permalink: null`; the mapper must not crash or invent."""
    payload = issue_payload("created")
    payload["data"]["issue"]["permalink"] = None
    assert MAPPERS["Sentry.IssueCreated"](payload)["url"] == ""


def test_unknown_level_clamps_to_error():
    """The contract types `level` as a closed enum; an off-catalog string must still validate."""
    payload = issue_payload("created")
    payload["data"]["issue"]["level"] = "sample"
    assert MAPPERS["Sentry.IssueCreated"](payload)["level"] == "error"


# ── signature verification ──────────────────────────────────────────────────


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_a_valid_hmac():
    body = json.dumps(issue_payload("created")).encode()
    verify_signature("s3cr3t", body, _sign("s3cr3t", body))  # no raise


def test_verify_signature_rejects_missing_or_mismatched():
    body = b'{"action": "created"}'
    with pytest.raises(SignatureError):
        verify_signature("s3cr3t", body, None)
    with pytest.raises(SignatureError):
        verify_signature("s3cr3t", body, _sign("wrong-secret", body))
    # The exact received bytes are what's signed; any re-serialization must fail.
    with pytest.raises(SignatureError):
        verify_signature("s3cr3t", body + b" ", _sign("s3cr3t", body))


# ── end to end: signed delivery -> verifier -> built-in mapper -> typed event ──


def _sentry_runner(secret: str):
    """A live runner hosting the *real* built-in Sentry sensors behind the real verifier."""
    from loopy_runtime.manifest_model import SensorSpec, SensorTriggerSpec
    from loopy_runtime.scm.sentry_webhook import signature_verifier
    from loopy_runtime.sensors.loader import builtin_webhook_sensor
    from loopy_runtime.sensors.runner import FastAPISensorRunner

    class RecordingReceiver:
        def __init__(self) -> None:
            self.seen = []

        async def receive(self, event) -> None:
            self.seen.append(event)

    receiver = RecordingReceiver()
    host = FastAPISensorRunner(receiver)
    for emits in MAPPERS:
        spec = SensorSpec(
            name=f"builtin:{emits}",
            trigger=SensorTriggerSpec(kind="webhook", path="/hooks/sentry"),
            emits=emits,
            source="builtin",
            provider="sentry",
        )
        host.register_webhook(
            "/hooks/sentry", builtin_webhook_sensor(spec), verify=signature_verifier(secret)
        )
    return host, receiver


def test_signed_delivery_fans_out_to_the_matching_builtin():
    from fastapi.testclient import TestClient

    host, receiver = _sentry_runner("s3cr3t")
    body = json.dumps(issue_payload("created")).encode()
    resp = TestClient(host.app).post(
        "/hooks/sentry", content=body, headers={"Sentry-Hook-Signature": _sign("s3cr3t", body)}
    )
    assert resp.status_code == 200
    assert resp.json() == {"emitted": ["Sentry.IssueCreated"]}  # IssueResolved stayed quiet
    (event,) = receiver.seen
    assert event.name == "Sentry.IssueCreated"
    assert event.fields["issue_id"] == "1170820242"


def test_unsigned_delivery_is_rejected_with_401():
    from fastapi.testclient import TestClient

    host, receiver = _sentry_runner("s3cr3t")
    body = json.dumps(issue_payload("created")).encode()
    resp = TestClient(host.app).post(
        "/hooks/sentry", content=body, headers={"Sentry-Hook-Signature": _sign("nope", body)}
    )
    assert resp.status_code == 401
    assert receiver.seen == []
