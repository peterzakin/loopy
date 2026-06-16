"""P0 — discovery. Walk a project directory and inventory its four input kinds.

Pure inventory (no diagnostics): a missing/unreadable `registry.yml` surfaces as
E001 in the registry loader (P1). File lists are sorted so compilation is
order-independent (a determinism requirement of the manifest, FRONTEND §12).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Inventory:
    root: Path
    registry_path: Path | None = None
    workflow_files: list[Path] = field(default_factory=list)
    skill_dirs: list[Path] = field(default_factory=list)
    sensor_files: list[Path] = field(default_factory=list)


def _sorted(paths) -> list[Path]:
    return sorted(paths, key=lambda p: p.as_posix())


def discover(root: str | Path) -> Inventory:
    """Inventory `registry.yml`, `workflows/*/*.md`, `skills/*`, and `sensors/**/*.py`."""
    root = Path(root)

    registry = root / "registry.yml"
    registry_path = registry if registry.is_file() else None

    workflows_dir = root / "workflows"
    workflow_files = _sorted(workflows_dir.glob("*/*.md")) if workflows_dir.is_dir() else []

    skills_dir = root / "skills"
    skill_dirs = (
        _sorted(p for p in skills_dir.iterdir() if p.is_dir()) if skills_dir.is_dir() else []
    )

    sensors_dir = root / "sensors"
    sensor_files = (
        _sorted(
            p
            for p in sensors_dir.rglob("*.py")
            if "__pycache__" not in p.parts and p.name != "__init__.py"
        )
        if sensors_dir.is_dir()
        else []
    )

    return Inventory(
        root=root,
        registry_path=registry_path,
        workflow_files=workflow_files,
        skill_dirs=skill_dirs,
        sensor_files=sensor_files,
    )
