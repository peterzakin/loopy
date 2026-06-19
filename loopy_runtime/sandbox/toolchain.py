"""Compose a harness `ToolchainLayer` onto a sandbox's `image:` spec (#16).

The sandbox `image:` stays harness-agnostic in the manifest; the runtime composes the
active harness's toolchain into the *effective* image just before `acquire`, so the
CLI/runtime the harness shells out to is present by construction. Pure + provider-agnostic:
both the Docker and Daytona image planners consume the resulting dict unchanged.

The toolchain's layers are prepended ahead of the user's own (`apt`/`pip`/`run`), so the
toolchain installs first and the user's `run:` steps can build on it. `image.env` is the
exception — the user's values win, since the layer's env are only sensible defaults
(e.g. a `PATH` addition).
"""

from __future__ import annotations

from loopy_runtime.contract import ToolchainLayer


def compose_image(image: dict | str | None, layer: ToolchainLayer) -> dict | str | None:
    """Return `image` with `layer`'s additive layers prepended.

    A `snapshot:` image is prebuilt and can't take build layers, so composition is skipped —
    a snapshot is expected to already bundle the toolchain, and the runtime's post-acquire
    probe is the backstop that catches it if not. Non-dict/str/None images are returned
    unchanged so the image planner raises its own validation error downstream.
    """
    if image is None:
        img: dict = {}
    elif isinstance(image, str):
        img = {"base": image}
    elif isinstance(image, dict):
        img = dict(image)
    else:
        return image  # let plan_image raise on the bad type

    if "snapshot" in img:
        return img

    if layer.apt:
        img["apt"] = list(layer.apt) + list(img.get("apt", []))
    if layer.pip:
        img["pip"] = list(layer.pip) + list(img.get("pip", []))
    if layer.run:
        img["run"] = list(layer.run) + list(img.get("run", []))
    if layer.env:
        img["env"] = {**layer.env, **img.get("env", {})}  # user env wins
    return img
