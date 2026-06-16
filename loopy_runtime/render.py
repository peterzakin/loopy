"""Runtime template rendering — fill the manifest's recorded `{{ }}` holes (B3).

The frontend validated every ref and recorded `{producer, field, raw}` on each step;
here we substitute the literal `raw` text with the resolved run value.
"""

from __future__ import annotations

from collections.abc import Mapping

from loopy_runtime.contract import Event, StepOutput
from loopy_runtime.manifest_model import RefSpec, StepSpec


class TemplateRenderer:
    def render(self, step: StepSpec, event: Event, upstream: Mapping[str, StepOutput]) -> str:
        body = step.body
        for ref in step.refs:
            body = body.replace(ref.raw, str(self._resolve(ref, event, upstream)))
        return body

    def _resolve(self, ref: RefSpec, event: Event, upstream: Mapping[str, StepOutput]):
        if ref.producer == "event":
            return event.fields.get(ref.field, "")
        out = upstream.get(ref.producer)
        return out.fields.get(ref.field, "") if out is not None else ""
