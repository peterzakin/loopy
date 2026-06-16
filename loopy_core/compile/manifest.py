"""P9 — serialize a `Project` to the deterministic manifest (sorted keys, stable
ordering, content-hashable). `compiled_at`/`loopy_version` are stamped by the CLI
wrapper, outside the hashed core (M5)."""

from __future__ import annotations

from loopy_core.compile.model import Project


def to_manifest(project: Project) -> dict:
    """Emit the §9 manifest shape. Implemented in M5."""
    raise NotImplementedError("compile/manifest.to_manifest is implemented in M5")
