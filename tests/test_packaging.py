"""Packaging guards: shipped data files (schema + PEP 561 markers) stay included."""

from __future__ import annotations

import importlib.resources as resources


def test_manifest_schema_is_packaged():
    assert (resources.files("loopy_core") / "manifest.schema.json").is_file()


def test_py_typed_markers_present():
    assert (resources.files("loopy_core") / "py.typed").is_file()
    assert (resources.files("loopy_runtime") / "py.typed").is_file()
