"""M4: sensor AST inspection (declared emits, S1–S3) + loopy.events codegen."""

from __future__ import annotations

from loopy_core.compile import codes
from loopy_core.compile.pipeline import compile_project
from loopy_core.events.codegen import write_events
from tests.helpers import assert_code, write_project
from tests.helpers import codes as result_codes

REGISTRY = (
    "events:\n"
    "  Incident:\n"
    "    fields:\n"
    "      source: enum[sentry, linear]\n"
    "      issue_id: str\n"
    "      title: str\n"
    "      link: url\n"
    "  MetricThreshold:\n"
    "    fields:\n"
    "      goal_id: str\n"
)

S_CODES = {codes.E401, codes.E402, codes.E403}


def sensor_py(decorator_args: str, fn: str = "s", body: str = "    return None") -> str:
    return f"from loopy import sensor\n\n\n@sensor({decorator_args})\ndef {fn}(req):\n{body}\n"


def assert_no_sensor_errors(result) -> None:
    fired = set(result_codes(result)) & S_CODES
    assert not fired, f"unexpected sensor codes: {fired}"


def test_valid_webhook_sensor_recorded(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "sensors/sensors.py": sensor_py(
                'webhook="/hooks/sentry", emits="Incident"', fn="sentry_issues"
            ),
        },
    )
    result = compile_project(tmp_path)
    assert_no_sensor_errors(result)
    sensors = result.project.sensors
    assert len(sensors) == 1
    s = sensors[0]
    assert s.name == "sentry_issues"
    assert s.emits == "Incident"
    assert s.trigger.kind == "webhook"
    assert s.trigger.path == "/hooks/sentry"
    assert s.module == "sensors.sensors"
    assert s.fn == "sentry_issues"


def test_valid_poll_sensor(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "sensors/metrics.py": sensor_py('poll="5m", emits="MetricThreshold"'),
        },
    )
    result = compile_project(tmp_path)
    assert_no_sensor_errors(result)
    s = result.project.sensors[0]
    assert s.trigger.kind == "poll"
    assert s.trigger.interval == "5m"


def test_module_dotted_path_for_subdir(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "sensors/github/issues.py": sensor_py('webhook="/gh", emits="Incident"'),
        },
    )
    s = compile_project(tmp_path).project.sensors[0]
    assert s.module == "sensors.github.issues"


def test_missing_emits_reports_e402(tmp_path):
    write_project(
        tmp_path,
        {"registry.yml": REGISTRY, "sensors/s.py": sensor_py('webhook="/x"')},
    )
    assert_code(compile_project(tmp_path), codes.E402)


def test_non_literal_emits_reports_e402(tmp_path):
    # emits=Incident (a name, not a string literal) is not statically readable.
    write_project(
        tmp_path,
        {"registry.yml": REGISTRY, "sensors/s.py": sensor_py('webhook="/x", emits=Incident')},
    )
    assert_code(compile_project(tmp_path), codes.E402)


def test_syntax_error_reports_e402_no_execution(tmp_path):
    write_project(
        tmp_path,
        {"registry.yml": REGISTRY, "sensors/s.py": "def broken(:\n    pass\n"},
    )
    assert_code(compile_project(tmp_path), codes.E402)


def test_unregistered_emits_reports_e401(tmp_path):
    write_project(
        tmp_path,
        {"registry.yml": REGISTRY, "sensors/s.py": sensor_py('webhook="/x", emits="Ghost"')},
    )
    assert_code(compile_project(tmp_path), codes.E401)


def test_duplicate_webhook_path_same_event_reports_e403(tmp_path):
    # Same path AND same emits is the ambiguous duplicate we reject.
    body = (
        sensor_py('webhook="/dup", emits="Incident"', fn="a")
        + "\n"
        + sensor_py('webhook="/dup", emits="Incident"', fn="b")
    )
    write_project(tmp_path, {"registry.yml": REGISTRY, "sensors/s.py": body})
    assert_code(compile_project(tmp_path), codes.E403)


def test_shared_webhook_path_distinct_events_is_allowed(tmp_path):
    # Fan-out: many sensors on one URL emitting different events (the GitHub model).
    body = (
        sensor_py('webhook="/hooks/github", emits="Incident"', fn="a")
        + "\n"
        + sensor_py('webhook="/hooks/github", emits="MetricThreshold"', fn="b")
    )
    write_project(tmp_path, {"registry.yml": REGISTRY, "sensors/s.py": body})
    result = compile_project(tmp_path)
    assert_no_sensor_errors(result)
    assert {s.name for s in result.project.sensors} == {"a", "b"}


def test_malformed_poll_interval_reports_e403(tmp_path):
    write_project(
        tmp_path,
        {"registry.yml": REGISTRY, "sensors/s.py": sensor_py('poll="soon", emits="Incident"')},
    )
    assert_code(compile_project(tmp_path), codes.E403)


def test_no_trigger_reports_e403(tmp_path):
    write_project(
        tmp_path,
        {"registry.yml": REGISTRY, "sensors/s.py": sensor_py('emits="Incident"')},
    )
    assert_code(compile_project(tmp_path), codes.E403)


def test_sensor_body_is_never_executed(tmp_path):
    # Import-time side effects would crash if Core imported the module; it must not.
    src = (
        "import this_module_does_not_exist_xyz\n"
        "raise RuntimeError('sensors must not be executed at compile time')\n\n"
        "from loopy import sensor\n\n\n"
        '@sensor(webhook="/x", emits="Incident")\n'
        "def s(req):\n    return None\n"
    )
    write_project(tmp_path, {"registry.yml": REGISTRY, "sensors/s.py": src})
    result = compile_project(tmp_path)
    # The sensor is still discovered statically, and nothing blew up.
    assert [s.name for s in result.project.sensors] == ["s"]


def test_codegen_module_is_importable_and_typed(tmp_path):
    import importlib.util
    import sys

    write_project(tmp_path, {"registry.yml": REGISTRY})
    registry = compile_project(tmp_path).project.registry

    # write_events lands under <out>/loopy/.
    written = write_events(registry, tmp_path)
    assert any(p.name == "events.py" for p in written)
    assert any(p.name == "events.pyi" for p in written)

    # Import the generated module as a real module so pydantic resolves its types.
    mod_name = "loopy_generated_events_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, tmp_path / "loopy" / "events.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
        incident = module.Incident(source="sentry", issue_id="1", title="t", link="u")
        assert incident.source == "sentry"
        assert incident.issue_id == "1"
    finally:
        del sys.modules[mod_name]
