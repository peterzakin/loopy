"""Every cookbook example compiles green — the guard against cookbook/repo drift.

Each subdirectory of `examples/` is a self-contained project the README and the landing-site
cookbook point at. If one stops compiling (a renamed event, a dropped skill, a typo'd ref),
the docs are pointing at something broken. This test discovers every example directory and
asserts a clean compile, so a break is caught in CI on every push rather than by a reader.

Discovery is automatic: drop a new example under `examples/` and it's covered with no edit
here. An example directory is any immediate child of `examples/` that holds a `registry.yml`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loopy_core.compile.pipeline import compile_project

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


def _example_dirs() -> list[Path]:
    return sorted(p.parent for p in EXAMPLES_DIR.glob("*/registry.yml"))


def test_examples_directory_is_discovered():
    """Guard the guard: if discovery finds nothing, the test below would vacuously pass."""
    assert _example_dirs(), f"no example projects found under {EXAMPLES_DIR}"


@pytest.mark.parametrize("example", _example_dirs(), ids=lambda p: p.name)
def test_example_compiles_green(example: Path):
    result = compile_project(example)
    errors = [d.render() for d in result.diagnostics.errors]
    assert not errors, f"{example.name} does not compile:\n" + "\n".join(errors)
