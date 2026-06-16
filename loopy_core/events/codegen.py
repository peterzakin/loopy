"""`loopy.events` codegen — emit typed event definitions from the registry so sensor
authors get types their own typechecker validates. Multi-target (Python `.pyi` first,
then `.d.ts`); written to `<project>/loopy/` by default. Author-facing only — Core
reads each sensor's declared `emits`, not these types (M4)."""

from __future__ import annotations
