"""P7 — Python AST sensor inspector (FRONTEND §7).

Never imports or executes sensor modules: parses each `sensors/**/*.py` to an AST
and reads each `@sensor(...)`-decorated function's *declared* `emits` + trigger.
The return annotation is not consulted — `emits` is the source of truth.
"""

from __future__ import annotations

import ast
import re

from loopy_core.compile import codes
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.discovery import Inventory
from loopy_core.registry.model import Registry
from loopy_core.sensors.model import Sensor, SensorTrigger
from loopy_core.span import span_at

_POLL_RE = re.compile(r"^\d+[smhd]$")


def load_sensors(inv: Inventory, registry: Registry, diags: DiagnosticCollector) -> list[Sensor]:
    sensors: list[Sensor] = []
    webhook_paths: dict[str, str] = {}  # path -> sensor name (for S3 uniqueness)

    for path in inv.sensor_files:
        try:
            file = str(path.relative_to(inv.root))
            module = ".".join(path.relative_to(inv.root).with_suffix("").parts)
        except ValueError:
            file = path.name
            module = path.stem

        try:
            tree = ast.parse(path.read_text(), filename=file)
        except SyntaxError as exc:
            diags.error(
                codes.E402,
                f"{file}: not statically analyzable ({exc.msg})",
                span=span_at(file, exc.lineno or 0),
            )
            continue

        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            decorator = _sensor_decorator(node)
            if decorator is None:
                continue
            sensor = _build_sensor(decorator, node, file, module, registry, webhook_paths, diags)
            if sensor is not None:
                sensors.append(sensor)

    return sensors


def _sensor_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.expr | None:
    """Return the `@sensor`/`@sensor(...)` decorator node, or None."""
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id == "sensor":
            return dec
        if isinstance(target, ast.Attribute) and target.attr == "sensor":
            return dec
    return None


def _const_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _build_sensor(
    decorator: ast.expr,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    file: str,
    module: str,
    registry: Registry,
    webhook_paths: dict[str, str],
    diags: DiagnosticCollector,
) -> Sensor | None:
    span = span_at(file, fn.lineno)

    # S1 — emits must be declared as a statically readable string literal.
    kwargs = (
        {kw.arg: kw.value for kw in decorator.keywords} if isinstance(decorator, ast.Call) else {}
    )
    emits = _const_str(kwargs.get("emits"))
    if emits is None:
        diags.error(
            codes.E402,
            f"sensor '{fn.name}' must declare emits=\"<Event>\" as a string literal",
            span=span,
            hint='e.g. @sensor(webhook="/hooks/x", emits="Incident")',
        )
        return None

    # Trigger: exactly one of webhook= / poll=.
    trigger = _build_trigger(kwargs, fn.name, span, diags)
    if trigger is None:
        return None

    # S3 — webhook path uniqueness.
    if trigger.kind == "webhook":
        if trigger.path in webhook_paths:
            diags.error(
                codes.E403,
                f"duplicate webhook path '{trigger.path}' "
                f"(sensors '{webhook_paths[trigger.path]}' and '{fn.name}')",
                span=span,
            )
        else:
            webhook_paths[trigger.path] = fn.name

    # S2 — declared emits must be a registered event.
    if emits not in registry.events:
        diags.error(
            codes.E401,
            f"sensor '{fn.name}' emits unregistered event '{emits}'",
            span=span,
        )

    return Sensor(name=fn.name, trigger=trigger, emits=emits, module=module, fn=fn.name, span=span)


def _build_trigger(
    kwargs: dict, fn_name: str, span, diags: DiagnosticCollector
) -> SensorTrigger | None:
    webhook = _const_str(kwargs.get("webhook"))
    poll = _const_str(kwargs.get("poll"))

    if webhook is not None and poll is not None:
        diags.error(
            codes.E403,
            f"sensor '{fn_name}' declares both webhook= and poll= (choose one)",
            span=span,
        )
        return None
    if webhook is not None:
        return SensorTrigger(kind="webhook", path=webhook, span=span)
    if poll is not None:
        if not _POLL_RE.match(poll):
            diags.error(
                codes.E403,
                f"sensor '{fn_name}' has a malformed poll interval {poll!r} (expected e.g. '5m')",
                span=span,
            )
            return None
        return SensorTrigger(kind="poll", interval=poll, span=span)

    diags.error(
        codes.E403,
        f"sensor '{fn_name}' must declare a webhook= or poll= trigger",
        span=span,
    )
    return None
