"""RoutingSandboxProvider: sandbox selection lives in the manifest (each sandbox's
`provider:`), not a launch flag. The runtime dispatches each `acquire` to the backend the
spec names, building concrete providers lazily so unused backends' deps stay optional.
"""

from __future__ import annotations

import asyncio

from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.sandbox import factory
from loopy_runtime.sandbox.factory import RoutingSandboxProvider


class _FakeProvider:
    def __init__(self, name: str):
        self.name = name
        self.acquired: list[SandboxSpec] = []
        self.closed = False

    async def acquire(self, spec, secrets):
        self.acquired.append(spec)
        return f"sandbox::{self.name}"

    async def aclose(self):
        self.closed = True


def _patched(monkeypatch):
    """Replace the concrete-provider factory with a recording fake, keyed by name."""
    built: dict[str, _FakeProvider] = {}

    def fake_make(name: str = "local") -> _FakeProvider:
        built[name] = _FakeProvider(name)
        return built[name]

    monkeypatch.setattr(factory, "make_sandbox_provider", fake_make)
    return built


def test_acquire_routes_to_provider_named_by_spec(monkeypatch):
    built = _patched(monkeypatch)
    router = RoutingSandboxProvider()

    got = asyncio.run(router.acquire(SandboxSpec(provider="daytona"), {}))

    assert got == "sandbox::daytona"
    assert set(built) == {"daytona"}  # only the used backend was built


def test_omitted_provider_defaults_to_remote_daytona(monkeypatch):
    # Remote is the absolute default: a sandbox that declares no provider runs in the cloud.
    # Anything host-bound (local/docker) is an explicit opt-in in the registry.
    built = _patched(monkeypatch)
    router = RoutingSandboxProvider()

    asyncio.run(router.acquire(SandboxSpec(), {}))

    assert set(built) == {"daytona"}


def test_default_is_overridable(monkeypatch):
    built = _patched(monkeypatch)
    router = RoutingSandboxProvider(default="docker")

    asyncio.run(router.acquire(SandboxSpec(), {}))

    assert set(built) == {"docker"}


def test_providers_are_cached_per_name(monkeypatch):
    built = _patched(monkeypatch)
    router = RoutingSandboxProvider()

    asyncio.run(router.acquire(SandboxSpec(provider="daytona"), {}))
    asyncio.run(router.acquire(SandboxSpec(provider="daytona"), {}))

    # One fake per name, reused across acquires (so its session/cache isn't thrown away).
    assert len(built["daytona"].acquired) == 2


def test_aclose_closes_every_built_provider(monkeypatch):
    built = _patched(monkeypatch)
    router = RoutingSandboxProvider()
    asyncio.run(router.acquire(SandboxSpec(provider="daytona"), {}))
    asyncio.run(router.acquire(SandboxSpec(provider="local"), {}))

    asyncio.run(router.aclose())

    assert all(p.closed for p in built.values())
    assert set(built) == {"daytona", "local"}
