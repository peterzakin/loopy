"""Sensor IR (FRONTEND §2, §7). Defined in M0, populated/validated in M4.

`emits` is *declared* (a registered event name), never inferred from a return type.
`module` is a language-appropriate locator (dotted path for Python); a `lang`
discriminator is added when a second sensor language lands.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from loopy_core.span import Span


class SensorTrigger(BaseModel):
    """Distinct from the workflow `Trigger` — sensors fire on webhook or poll."""

    kind: Literal["webhook", "poll"]
    path: str | None = None  # kind == "webhook"
    interval: str | None = None  # kind == "poll", e.g. "5m"
    span: Span


class Sensor(BaseModel):
    name: str
    trigger: SensorTrigger
    emits: str  # declared, registered event name
    # `source` discriminates a user-authored sensor (a Python module the backend imports)
    # from a platform-shipped built-in (resolved from a runtime mapper registry by `emits`).
    # A built-in carries `provider` and leaves `module`/`fn` unset.
    source: Literal["module", "builtin"] = "module"
    provider: str | None = None  # set iff source == "builtin", e.g. "github"
    module: str | None = None  # language-appropriate locator (dotted path for Python)
    fn: str | None = None
    span: Span
