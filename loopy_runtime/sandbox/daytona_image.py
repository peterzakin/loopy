"""Map a loopy `image:` spec to a Daytona image build (pure, provider-agnostic plan).

`plan_image` validates the `image:` dict and produces an ordered `ImageBuild` (base +
chained ops, or a snapshot reference). `apply_image_plan` replays it onto Daytona's
`Image` builder. Keeping the plan pure makes the mapping fully unit-testable without
the Daytona SDK or any network.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from loopy_runtime.providers import RECOGNIZED_MODEL_KEYS

_BASE_KEYS = ("debian_slim", "base", "dockerfile", "snapshot")
_LAYER_KEYS = (
    "workdir",
    "env",
    "apt",
    "pip",
    "pip_requirements",
    "run",
    "user",
    "entrypoint",
    "cmd",
)
_KNOWN_KEYS = frozenset(_BASE_KEYS + _LAYER_KEYS)
_DEFAULT_BASE = ("debian_slim", ())


@dataclass
class ImageBuild:
    base: tuple[str, tuple] | None  # (Image factory method, args), or None for a snapshot
    ops: list[tuple[str, tuple]] = field(default_factory=list)  # ordered (method, args)
    snapshot: str | None = None  # if set, skip the build and create from this snapshot
    warnings: list[str] = field(default_factory=list)


def _looks_like_secret(key: str) -> bool:
    return key in RECOGNIZED_MODEL_KEYS or key.endswith(("_API_KEY", "_TOKEN", "_SECRET"))


def plan_image(image: dict | str | None) -> ImageBuild:
    if image is None:
        image = {}
    if isinstance(image, str):
        image = {"base": image}
    if not isinstance(image, dict):
        raise ValueError(f"image: must be a mapping or string, got {type(image).__name__}")

    unknown = set(image) - _KNOWN_KEYS
    if unknown:
        raise ValueError(f"unknown image keys: {sorted(unknown)}")

    base_keys = [k for k in _BASE_KEYS if k in image]
    if len(base_keys) > 1:
        raise ValueError(f"image: declares multiple base selectors {base_keys}; choose one")

    # Snapshot short-circuits the build and is exclusive of build layers.
    if "snapshot" in image:
        extra = [k for k in image if k != "snapshot"]
        if extra:
            raise ValueError(f"image.snapshot cannot be combined with build layers {sorted(extra)}")
        return ImageBuild(base=None, snapshot=str(image["snapshot"]))

    build = ImageBuild(base=_DEFAULT_BASE)
    if not base_keys:
        build.warnings.append("no base image specified; defaulting to debian_slim")
    elif "debian_slim" in image:
        version = image["debian_slim"]
        build.base = ("debian_slim", (str(version),) if version else ())
    elif "base" in image:
        build.base = ("base", (str(image["base"]),))
    elif "dockerfile" in image:
        build.base = ("from_dockerfile", (str(image["dockerfile"]),))

    _append_layers(image, build)
    return build


def _append_layers(image: dict, build: ImageBuild) -> None:
    if "workdir" in image:
        build.ops.append(("workdir", (str(image["workdir"]),)))
    if "env" in image:
        env = dict(image["env"])
        leaked = sorted(k for k in env if _looks_like_secret(k))
        if leaked:
            raise ValueError(
                f"image.env must not contain secrets {leaked}; secrets are injected at run "
                "time via the sandbox env_file, not baked into the image"
            )
        build.ops.append(("env", (env,)))
    if "apt" in image:
        pkgs = " ".join(image["apt"])
        build.ops.append(
            (
                "run_commands",
                (f"apt-get update && apt-get install -y {pkgs} && rm -rf /var/lib/apt/lists/*",),
            )
        )
    if "pip" in image:
        build.ops.append(("pip_install", (list(image["pip"]),)))
    if "pip_requirements" in image:
        build.ops.append(("pip_install_from_requirements", (str(image["pip_requirements"]),)))
    if "run" in image:
        build.ops.append(("run_commands", tuple(image["run"])))
    if "user" in image:
        _append_user_layers(image, build)
    if "entrypoint" in image:
        build.ops.append(("entrypoint", (list(image["entrypoint"]),)))
    if "cmd" in image:
        build.ops.append(("cmd", (list(image["cmd"]),)))


def _append_user_layers(image: dict, build: ImageBuild) -> None:
    """Emit the layers that make `image.user` actually usable, then switch to it.

    Declaring `user: daytona` on a bare base (e.g. `debian_slim`) is a trap two ways:
      1. The user doesn't exist, so the container fails to *start* with a cryptic
         `unable to find user daytona: no matching entries in passwd file`.
      2. `WORKDIR` runs as root and creates the workdir root-owned *before* any user
         exists, so the agent (running as `user`) lands in a cwd it can't write to.

    The user declared the intent; the build should make it true. So for a non-root
    user we create it (idempotently — a base image that already defines it is fine)
    and hand it ownership of the workdir, all while still root, before the `USER`
    switch. `root` needs none of this and is left untouched.
    """
    user = str(image["user"])
    if user != "root":
        quoted = shlex.quote(user)
        # `id -u … || useradd` keeps this idempotent: a base that already ships the
        # user (so `useradd` would fail) is left alone; a bare base gets it created.
        cmds = [f"id -u {quoted} >/dev/null 2>&1 || useradd -m -s /bin/bash {quoted}"]
        if "workdir" in image:
            workdir = shlex.quote(str(image["workdir"]))
            cmds.append(f"chown -R {quoted}:{quoted} {workdir}")
        build.ops.append(("run_commands", tuple(cmds)))
    build.ops.append(("dockerfile_commands", ([f"USER {user}"],)))


def apply_image_plan(build: ImageBuild, image_cls):
    """Replay a build plan onto Daytona's `Image` class (or a recording fake)."""
    factory, args = build.base
    image = getattr(image_cls, factory)(*args)
    for method, method_args in build.ops:
        image = getattr(image, method)(*method_args)
    return image
