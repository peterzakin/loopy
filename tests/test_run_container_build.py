"""`loopy run` container mode — engine-image build selection (`_run_in_docker`).

Container mode requires Docker but *not* a source checkout: from a local source tree it builds
the tree, otherwise it builds the pinned PyPI release via `Dockerfile.pypi` (no build context).
The image is tagged by loopy version and reused; `--build` forces a rebuild. These tests capture
the `docker compose` invocation + its environment instead of actually building.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

import loopy_cli
from loopy_core import __version__

DEPLOY = Path(loopy_cli.__file__).resolve().parent / "deploy"


@pytest.fixture
def captured(monkeypatch):
    """Stub docker presence + `subprocess.call`; return a dict the call populates."""
    box: dict = {}

    def fake_call(cmd, env):
        box["cmd"] = cmd
        box["env"] = env
        return 0

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr("subprocess.call", fake_call)
    return box


def _invoke(tmp_path, **kwargs):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    defaults = dict(root=tmp_path, manifest=manifest, port=None, detach=False, build=False)
    defaults.update(kwargs)
    with pytest.raises(typer.Exit) as exc:  # _run_in_docker always exits with the call's rc
        loopy_cli._run_in_docker(**defaults)
    return exc.value


def test_docker_missing_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    with pytest.raises(typer.Exit) as exc:
        loopy_cli._run_in_docker(
            root=tmp_path, manifest=manifest, port=None, detach=False, build=False
        )
    assert exc.value.exit_code == 1


def test_source_checkout_builds_local_tree(tmp_path, monkeypatch, captured):
    source = tmp_path / "checkout"
    source.mkdir()
    monkeypatch.setattr("loopy_cli._source_root", lambda: source)

    _invoke(tmp_path)
    env = captured["env"]
    assert env["LOOPY_ENGINE_IMAGE"] == f"loopy-engine:{__version__}"
    assert env["LOOPY_VERSION"] == __version__
    assert env["LOOPY_BUILD_CONTEXT"] == str(source)
    assert env["LOOPY_DOCKERFILE"].endswith("deploy/Dockerfile")  # the source Dockerfile
    assert "--build" not in captured["cmd"]  # built once per version, then reused


def test_no_source_builds_from_pypi(tmp_path, monkeypatch, captured):
    monkeypatch.setattr("loopy_cli._source_root", lambda: None)  # a pip install, no checkout

    _invoke(tmp_path)
    env = captured["env"]
    assert env["LOOPY_BUILD_CONTEXT"] == str(DEPLOY)  # context is just the deploy dir
    assert env["LOOPY_DOCKERFILE"] == "Dockerfile.pypi"
    assert env["LOOPY_VERSION"] == __version__  # the pin Dockerfile.pypi installs
    assert env["LOOPY_ENGINE_IMAGE"] == f"loopy-engine:{__version__}"


def test_build_flag_forces_rebuild(tmp_path, monkeypatch, captured):
    monkeypatch.setattr("loopy_cli._source_root", lambda: None)
    _invoke(tmp_path, build=True)
    assert "--build" in captured["cmd"]


def test_pypi_dockerfile_is_shipped():
    """The PyPI build path is useless if the Dockerfile isn't packaged beside the CLI."""
    assert (DEPLOY / "Dockerfile.pypi").is_file()
