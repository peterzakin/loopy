"""E2: authoring shim + real sensor loading/execution (no compile-time execution)."""

from __future__ import annotations

import asyncio
import sys

import pytest

from loopy_core.compile.pipeline import compile_project
from loopy_core.events.codegen import generate_events, write_events
from loopy_core.registry.model import Registry
from loopy_runtime.authoring import sensor, sensor_config
from loopy_runtime.manifest_model import SensorSpec, SensorTriggerSpec
from loopy_runtime.sensors.loader import load_webhook_sensor, normalize
from loopy_runtime.sensors.runner import FastAPISensorRunner
from tests.helpers import write_project

REGISTRY = (
    "events:\n"
    "  Incident:\n"
    "    fields:\n"
    "      source: enum[sentry]\n"
    "      issue_id: id\n"
    "      title: str\n"
    "      link: url\n"
)

SENSOR_MODULE = (
    "from loopy import sensor\n"
    "from loopy.events import Incident\n\n\n"
    '@sensor(webhook="/hooks/sentry", emits="Incident")\n'
    "def sentry(req) -> Incident:\n"
    '    i = req.json["issue"]\n'
    '    return Incident(source="sentry", issue_id=i["id"], title=i["title"], link=i["url"])\n'
)


def test_sensor_decorator_marks_function():
    @sensor(webhook="/x", emits="Incident")
    def s(req):
        return None

    assert sensor_config(s) == {"webhook": "/x", "poll": None, "emits": "Incident"}


class _FakeIncident:
    def model_dump(self):
        return {"issue_id": "1"}


def test_normalize_enforces_declared_emits():
    # Class name "_FakeIncident" != declared "Incident" -> contract violation.
    with pytest.raises(ValueError, match="declared emits"):
        normalize(_FakeIncident(), "Incident")
    # Matching name passes through as a single-element list; None emits nothing.
    assert [e.name for e in normalize(_FakeIncident(), "_FakeIncident")] == ["_FakeIncident"]
    assert normalize(None, "Incident") == []


def test_normalize_fans_out_multi_event_return():
    # A poll (or yield-ing) sensor returning many models normalizes to many events, each
    # still checked against the declared emits.
    events = normalize([_FakeIncident(), _FakeIncident()], "_FakeIncident")
    assert [e.name for e in events] == ["_FakeIncident", "_FakeIncident"]
    with pytest.raises(ValueError, match="declared emits"):
        normalize([_FakeIncident()], "Incident")


def test_codegen_init_reexports_sensor():
    init = generate_events(Registry())["loopy/__init__.py"]
    assert "from loopy_runtime.authoring import sensor" in init


def _purge_generated_modules() -> None:
    for name in list(sys.modules):
        if name == "loopy" or name.startswith(("loopy.", "sensors")):
            del sys.modules[name]


def test_load_and_run_real_sensor(tmp_path):
    # Generate the loopy.events package + author a sensor that imports it, then load &
    # run it the way the backend does at run time.
    write_project(tmp_path, {"registry.yml": REGISTRY, "sensors/sensors.py": SENSOR_MODULE})
    registry = compile_project(tmp_path).project.registry
    write_events(registry, tmp_path)  # writes <tmp>/loopy/{__init__,events}.py

    spec = SensorSpec(
        name="sentry",
        trigger=SensorTriggerSpec(kind="webhook", path="/hooks/sentry"),
        emits="Incident",
        module="sensors.sensors",
        fn="sentry",
    )
    payload = {"issue": {"id": "ISS-1", "title": "boom", "url": "https://x/1"}}
    try:
        invoke = load_webhook_sensor(spec, tmp_path)
        event = invoke(payload)
        assert event is not None
        assert event.name == "Incident"
        assert event.fields["issue_id"] == "ISS-1"
        assert event.fields["source"] == "sentry"

        # And it flows through the runner to the EventReceiver (the cascade entry point).
        seen: list[str] = []

        class _Receiver:
            async def receive(self, ev):
                seen.append(ev.name)

        host = FastAPISensorRunner(_Receiver())
        host.register_webhook("/hooks/sentry", invoke)
        result = asyncio.run(host._dispatch(invoke, payload))
        assert result == {"emitted": "Incident"}
        assert seen == ["Incident"]
    finally:
        if str(tmp_path.resolve()) in sys.path:
            sys.path.remove(str(tmp_path.resolve()))
        _purge_generated_modules()
