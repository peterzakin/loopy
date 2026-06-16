"""P0 — discovery. Walk a project directory and inventory its four input kinds.

M0: the `Inventory` shape + a no-op walk. M1 fills in the real inventory and the
missing/unreadable `registry.yml` -> E001 check.
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


def discover(root: Path) -> Inventory:
    """M0 skeleton: return an empty inventory rooted at `root` (no walk yet)."""
    return Inventory(root=Path(root))
