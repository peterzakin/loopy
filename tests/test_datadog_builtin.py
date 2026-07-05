"""Built-in Datadog events: zero-config `Datadog.*` triggers.

The Datadog half of the built-in catalog (`tests/test_builtins.py` covers the GitHub half and
the shared contract/mapper drift guard, which is registry-driven so it already covers Datadog):
compile-time injection on `/hooks/datadog`, the reserved-namespace guard (E215), unknown
built-ins scoped to the Datadog catalog (E112), the payload mappers against the canonical
monitor template, and the header-token ingress check.

Datadog differs from GitHub/Sentry in two ways these tests pin explicitly: the payload is the
loopy-prescribed template (not a provider-fixed shape), and the ingress auth is a shared secret
in a header, not an HMAC over the body — so `test_verify_token_ignores_the_body` asserts a
tampered body still verifies, documenting that difference as intentional.
"""

from __future__ import annotations

import json

import pytest

from loopy_core.builtins import DATADOG_EVENTS
from loopy_core.compile import codes
from loopy_core.compile.pipeline import compile_project
from loopy_runtime.scm.datadog_builtins import MAPPERS
from loopy_runtime.scm.datadog_webhook import TOKEN_HEADER, token_verifier, verify_token
from loopy_runtime.scm.github_webhook import SignatureError
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


def monitor_payload(transition: str) -> dict:
    """A representative delivery from the canonical monitor template."""
    return {
        "event": "monitor",
        "id": "1234567",
        "transition": transition,
        "alert_type": "error",
        "priority": "P1",
        "title": "[Triggered] High request latency",
        "body": "p99 latency is 2.3s (threshold 1s)",
        "metric": "trace.http.request.duration",
        "scope": "service:checkout,env:prod",
        "host": "web-1",
        "tags": "service:checkout,env:prod",
        "url": "https://app.datadoghq.com/monitors/1234567",
        "date": "1735689600000",
    }


# ── compile-time injection ──────────────────────────────────────────────────


def test_workflow_triggers_on_datadog_builtin_with_no_declarations(tmp_path):
    """`on: Datadog.MonitorAlerted` compiles clean with no event entry and no sensor file."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/t.md": md(
                "on: Datadog.MonitorAlerted\nagent: Investigator",
                body="Triage {{ event.title }} ({{ event.alert_type }}) at {{ event.url }}.",
            ),
        },
    )
    result = compile_project(tmp_path)
    assert not result.diagnostics.has_errors(), result_codes(result)

    event = result.project.registry.events["Datadog.MonitorAlerted"]
    assert event.builtin is True
    assert event.fields["alert_type"]["enum"] == ["error", "warning", "info", "success"]

    sensors = [s for s in result.project.sensors if s.emits == "Datadog.MonitorAlerted"]
    assert len(sensors) == 1
    s = sensors[0]
    assert s.source == "builtin"
    assert s.provider == "datadog"
    assert s.module is None and s.fn is None
    assert s.trigger.kind == "webhook" and s.trigger.path == "/hooks/datadog"


def test_all_three_providers_coexist(tmp_path):
    """One project can trigger on GitHub, Sentry, and Datadog; each lands on its own path."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: Investigator"),
            "workflows/sentry/s.md": md("on: Sentry.IssueCreated\nagent: Investigator"),
            "workflows/datadog/d.md": md("on: Datadog.MonitorAlerted\nagent: Investigator"),
        },
    )
    result = compile_project(tmp_path)
    assert not result.diagnostics.has_errors(), result_codes(result)
    paths = {s.emits: s.trigger.path for s in result.project.sensors if s.source == "builtin"}
    assert paths == {
        "Github.PullRequestOpened": "/hooks/github",
        "Sentry.IssueCreated": "/hooks/sentry",
        "Datadog.MonitorAlerted": "/hooks/datadog",
    }


def test_unknown_datadog_builtin_reports_e112_scoped_to_datadog_catalog(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/t.md": md("on: Datadog.Nonexistent\nagent: Investigator"),
        },
    )
    result = compile_project(tmp_path)
    assert_code(result, codes.E112)
    message = next(d.message for d in result.diagnostics.items if d.code == codes.E112)
    assert "Datadog.MonitorAlerted" in message
    assert "Github." not in message and "Sentry." not in message


def test_user_event_in_datadog_namespace_reports_e215(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY + "events:\n  Datadog.Custom: { x: str }\n",
            "workflows/triage/t.md": md("on: Datadog.MonitorAlerted\nagent: Investigator"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E215)


def test_user_sensor_in_datadog_namespace_reports_e215(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/t.md": md("on: Datadog.MonitorAlerted\nagent: Investigator"),
            "sensors/s.py": (
                "from loopy import sensor\n\n\n"
                '@sensor(webhook="/hooks/datadog", emits="Datadog.MonitorAlerted")\n'
                "def hijack(req):\n    return None\n"
            ),
        },
    )
    assert_code(compile_project(tmp_path), codes.E215)


# ── payload mappers ─────────────────────────────────────────────────────────


def test_monitor_alerted_maps_exactly_its_contract_fields():
    fields = MAPPERS["Datadog.MonitorAlerted"](monitor_payload("Triggered"))
    assert fields is not None
    assert set(fields) == set(DATADOG_EVENTS["Datadog.MonitorAlerted"])
    assert fields["monitor_id"] == "1234567"
    assert fields["alert_type"] == "error"
    assert fields["priority"] == "P1"
    assert fields["transition"] == "Triggered"
    assert fields["url"].endswith("/monitors/1234567")


def test_monitor_recovered_maps_exactly_its_contract_fields():
    fields = MAPPERS["Datadog.MonitorRecovered"](monitor_payload("Recovered"))
    assert fields is not None
    assert set(fields) == set(DATADOG_EVENTS["Datadog.MonitorRecovered"])
    assert fields["monitor_id"] == "1234567"


@pytest.mark.parametrize("transition", ["Triggered", "Re-Triggered", "Warn"])
def test_alert_transitions_all_map_to_monitor_alerted(transition):
    """All three in-alert transitions fold into MonitorAlerted (severity is in the fields)."""
    assert MAPPERS["Datadog.MonitorAlerted"](monitor_payload(transition)) is not None
    assert MAPPERS["Datadog.MonitorRecovered"](monitor_payload(transition)) is None


def test_mappers_ignore_unrelated_deliveries():
    """Each mapper returns None for a delivery that isn't its concern (the fan-out contract)."""
    # The other event's transition.
    assert MAPPERS["Datadog.MonitorAlerted"](monitor_payload("Recovered")) is None
    assert MAPPERS["Datadog.MonitorRecovered"](monitor_payload("Triggered")) is None
    # "No Data" is deferred — neither v1 event claims it.
    assert MAPPERS["Datadog.MonitorAlerted"](monitor_payload("No Data")) is None
    assert MAPPERS["Datadog.MonitorRecovered"](monitor_payload("No Data")) is None
    # A non-monitor delivery (no event marker, no transition).
    assert MAPPERS["Datadog.MonitorAlerted"]({"foo": "bar"}) is None
    assert MAPPERS["Datadog.MonitorRecovered"]({"foo": "bar"}) is None


def test_unknown_alert_type_clamps_to_error():
    """The contract types `alert_type` as a closed enum; an off-catalog string must validate."""
    payload = monitor_payload("Triggered")
    payload["alert_type"] = "user_update"
    assert MAPPERS["Datadog.MonitorAlerted"](payload)["alert_type"] == "error"


def test_missing_optional_fields_fall_back_to_empty():
    """A non-metric, non-host monitor omits some `$`-vars (Datadog sends ""); no crash/invent."""
    payload = {"event": "monitor", "id": 42, "transition": "Triggered", "title": "t"}
    fields = MAPPERS["Datadog.MonitorAlerted"](payload)
    assert set(fields) == set(DATADOG_EVENTS["Datadog.MonitorAlerted"])
    assert fields["monitor_id"] == "42"  # numeric id is stringified
    assert fields["metric"] == "" and fields["host"] == "" and fields["url"] == ""


def test_canonical_payload_keys_cover_the_mappers():
    """The prescribed template (source of truth in datadog_app) must carry every key the
    mappers read, or a real delivery would arrive with fields missing."""
    from loopy_runtime.scm.datadog_app import CANONICAL_PAYLOAD

    template = json.loads(CANONICAL_PAYLOAD)
    # Keys the mappers read out of the body.
    read_keys = {
        "event",
        "id",
        "transition",
        "alert_type",
        "priority",
        "title",
        "body",
        "metric",
        "scope",
        "host",
        "url",
    }
    assert read_keys <= set(template), read_keys - set(template)


# ── token verification (header compare, not body HMAC) ───────────────────────


def test_verify_token_accepts_bearer_and_bare_secret():
    verify_token("s3cr3t", "Bearer s3cr3t")  # no raise
    verify_token("s3cr3t", "s3cr3t")  # a bespoke header carries the bare secret


def test_verify_token_rejects_missing_or_mismatched():
    with pytest.raises(SignatureError):
        verify_token("s3cr3t", None)
    with pytest.raises(SignatureError):
        verify_token("s3cr3t", "Bearer nope")
    with pytest.raises(SignatureError):
        verify_token("s3cr3t", "")


def test_verify_token_ignores_the_body():
    """Datadog signs no body, so the verifier consults only the header — a tampered body still
    verifies. This pins the intended difference from GitHub/Sentry so a future 'add HMAC'
    refactor is a conscious contract change, not an accident."""
    verify = token_verifier("s3cr3t")
    verify(b'{"transition": "Triggered"}', {TOKEN_HEADER: "Bearer s3cr3t"})  # no raise
    verify(b"COMPLETELY DIFFERENT BODY", {TOKEN_HEADER: "Bearer s3cr3t"})  # still no raise
    with pytest.raises(SignatureError):
        verify(b'{"transition": "Triggered"}', {TOKEN_HEADER: "Bearer wrong"})


# ── end to end: authed delivery -> verifier -> built-in mapper -> typed event ──


def _datadog_runner(secret: str):
    """A live runner hosting the *real* built-in Datadog sensors behind the real verifier."""
    from loopy_runtime.manifest_model import SensorSpec, SensorTriggerSpec
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
            trigger=SensorTriggerSpec(kind="webhook", path="/hooks/datadog"),
            emits=emits,
            source="builtin",
            provider="datadog",
        )
        host.register_webhook(
            "/hooks/datadog", builtin_webhook_sensor(spec), verify=token_verifier(secret)
        )
    return host, receiver


def test_authed_delivery_fans_out_to_the_matching_builtin():
    from fastapi.testclient import TestClient

    host, receiver = _datadog_runner("s3cr3t")
    body = json.dumps(monitor_payload("Triggered")).encode()
    resp = TestClient(host.app).post(
        "/hooks/datadog", content=body, headers={"Authorization": "Bearer s3cr3t"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"emitted": ["Datadog.MonitorAlerted"]}  # Recovered stayed quiet
    (event,) = receiver.seen
    assert event.name == "Datadog.MonitorAlerted"
    assert event.fields["monitor_id"] == "1234567"


def test_unauthed_delivery_is_rejected_with_401():
    from fastapi.testclient import TestClient

    host, receiver = _datadog_runner("s3cr3t")
    body = json.dumps(monitor_payload("Triggered")).encode()
    resp = TestClient(host.app).post(
        "/hooks/datadog", content=body, headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401
    assert receiver.seen == []
