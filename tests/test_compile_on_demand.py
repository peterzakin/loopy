"""`loopy run`/`trigger` compile-on-demand — `_resolve_manifest`.

`run`/`trigger` consume a manifest, but the dev loop shouldn't require a separate `loopy compile`
first (dbt doesn't make you `dbt compile` before `dbt run`). `_resolve_manifest` compiles a
project-directory target on the fly and recompiles a stale manifest, while leaving a prebuilt
manifest with no source beside it (the deploy unit) untouched.
"""

from __future__ import annotations

import time

import pytest
import typer

from loopy_cli import _resolve_manifest
from loopy_cli.scaffold import scaffold_project


def _project(tmp_path):
    target = tmp_path / "proj"
    scaffold_project(target, "proj")
    return target


def test_directory_target_compiles_and_becomes_root(tmp_path):
    proj = _project(tmp_path)
    manifest, root = _resolve_manifest(proj, tmp_path)  # --root deliberately != project dir

    assert manifest == (proj / "manifest.json").resolve()
    assert manifest.is_file()
    assert root == proj.resolve()  # a directory target is its own root


def test_fresh_manifest_is_not_recompiled(tmp_path):
    proj = _project(tmp_path)
    manifest, _ = _resolve_manifest(proj, tmp_path)
    mtime = manifest.stat().st_mtime

    time.sleep(0.02)
    again, _ = _resolve_manifest(proj / "manifest.json", proj)
    assert again.stat().st_mtime == mtime  # up to date → left alone


def test_stale_manifest_is_recompiled(tmp_path):
    proj = _project(tmp_path)
    manifest, _ = _resolve_manifest(proj, tmp_path)
    mtime = manifest.stat().st_mtime

    time.sleep(0.02)
    (proj / "registry.yml").touch()  # a source file is now newer than the manifest
    again, _ = _resolve_manifest(proj / "manifest.json", proj)
    assert again.stat().st_mtime > mtime


def test_prebuilt_manifest_without_source_is_loaded_verbatim(tmp_path):
    """The deploy unit: a manifest.json with no registry.yml beside it must never be recompiled."""
    proj = _project(tmp_path)
    src_manifest, _ = _resolve_manifest(proj, tmp_path)

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    manifest = deploy / "manifest.json"
    manifest.write_text(src_manifest.read_text())
    mtime = manifest.stat().st_mtime

    time.sleep(0.02)
    resolved, root = _resolve_manifest(manifest, deploy)
    assert resolved == manifest.resolve()
    assert manifest.stat().st_mtime == mtime  # untouched — no source to compile from


def test_missing_manifest_without_source_errors(tmp_path):
    with pytest.raises(typer.Exit) as exc:
        _resolve_manifest(tmp_path / "nope.json", tmp_path)  # no file, no registry.yml
    assert exc.value.exit_code == 1


def test_missing_manifest_with_source_compiles(tmp_path):
    """`loopy run` from a project root with no manifest yet should just compile one."""
    proj = _project(tmp_path)
    manifest = proj / "manifest.json"
    assert not manifest.exists()

    resolved, _ = _resolve_manifest(manifest, proj)
    assert resolved == manifest.resolve()
    assert manifest.is_file()


def test_directory_target_with_compile_errors_exits(tmp_path):
    proj = _project(tmp_path)
    (proj / "registry.yml").write_text("this: is: not: valid: yaml: [")  # break the compile
    with pytest.raises(typer.Exit) as exc:
        _resolve_manifest(proj, tmp_path)
    assert exc.value.exit_code == 1
