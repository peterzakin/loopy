"""Daytona image mapping: pure plan_image validation/ordering + apply replay + API guard."""

from __future__ import annotations

import pytest

from loopy_runtime.sandbox.daytona_image import apply_image_plan, plan_image

APT_GIT = "apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*"


def test_default_base_warns():
    build = plan_image({})
    assert build.base == ("debian_slim", ())
    assert build.warnings


def test_example_image_maps_in_order():
    build = plan_image(
        {"debian_slim": "3.12", "apt": ["git"], "workdir": "/home/daytona", "user": "daytona"}
    )
    assert build.base == ("debian_slim", ("3.12",))
    assert build.ops == [
        ("workdir", ("/home/daytona",)),
        ("run_commands", (APT_GIT,)),
        # A non-root user is created (idempotently) and handed the workdir before the
        # USER switch — else the container can't start and the workdir is root-owned.
        (
            "run_commands",
            (
                "id -u daytona >/dev/null 2>&1 || useradd -m -s /bin/bash daytona",
                "chown -R daytona:daytona /home/daytona",
            ),
        ),
        ("dockerfile_commands", (["USER daytona"],)),
    ]


def test_non_root_user_is_created_even_without_workdir():
    build = plan_image({"base": "x", "user": "agent"})
    assert build.ops == [
        ("run_commands", ("id -u agent >/dev/null 2>&1 || useradd -m -s /bin/bash agent",)),
        ("dockerfile_commands", (["USER agent"],)),
    ]


def test_root_user_is_not_created():
    # root always exists; no useradd/chown, just the USER switch.
    build = plan_image({"base": "x", "workdir": "/w", "user": "root"})
    assert build.ops == [
        ("workdir", ("/w",)),
        ("dockerfile_commands", (["USER root"],)),
    ]


def test_bare_string_is_base():
    assert plan_image("python:3.12").base == ("base", ("python:3.12",))


def test_pip_and_run_layers():
    build = plan_image({"base": "x", "pip": ["numpy", "pandas"], "run": ["make build"]})
    assert build.ops == [
        ("pip_install", (["numpy", "pandas"],)),
        ("run_commands", ("make build",)),
    ]


def test_multiple_base_selectors_error():
    with pytest.raises(ValueError, match="multiple base"):
        plan_image({"debian_slim": "3.12", "base": "x"})


def test_unknown_key_errors():
    with pytest.raises(ValueError, match="unknown image keys"):
        plan_image({"base": "x", "files": {}})  # files dropped from v1 schema


def test_snapshot_is_exclusive():
    assert plan_image({"snapshot": "snap-1"}).snapshot == "snap-1"
    with pytest.raises(ValueError, match="cannot be combined"):
        plan_image({"snapshot": "snap-1", "apt": ["git"]})


def test_secret_in_build_env_errors():
    with pytest.raises(ValueError, match="must not contain secrets"):
        plan_image({"base": "x", "env": {"ANTHROPIC_API_KEY": "k"}})
    with pytest.raises(ValueError, match="must not contain secrets"):
        plan_image({"base": "x", "env": {"GITHUB_TOKEN": "t"}})


# ── apply replay onto a recording fake (no Daytona SDK needed) ──────────────────


class _FakeImage:
    def __init__(self, calls):
        self._calls = calls

    def __getattr__(self, name):
        def method(*args):
            self._calls.append((name, args))
            return self

        return method


class _FakeImageClass:
    def __init__(self):
        self.calls: list = []

    def debian_slim(self, *args):
        self.calls.append(("debian_slim", args))
        return _FakeImage(self.calls)

    def base(self, *args):
        self.calls.append(("base", args))
        return _FakeImage(self.calls)


def test_apply_replays_plan_onto_image():
    fake = _FakeImageClass()
    apply_image_plan(plan_image({"debian_slim": "3.12", "workdir": "/w", "pip": ["numpy"]}), fake)
    assert fake.calls == [
        ("debian_slim", ("3.12",)),
        ("workdir", ("/w",)),
        ("pip_install", (["numpy"],)),
    ]


def test_daytona_image_api_has_expected_methods():
    # Guards against SDK drift without calling the service.
    from daytona import Image

    for factory in ("debian_slim", "base", "from_dockerfile"):
        assert hasattr(Image, factory)
    instance = Image.base("debian:12.9")
    for method in (
        "pip_install",
        "pip_install_from_requirements",
        "run_commands",
        "env",
        "workdir",
        "dockerfile_commands",
        "entrypoint",
        "cmd",
    ):
        assert hasattr(instance, method), f"daytona Image missing {method}"
