# Sandbox `image:` mapping — spec (implemented)

**Status:** done
**Owner:** peter
**Date:** 2026-06-16

Finalized after review. Replaces the v1 base-string `_image` shortcut.

## Canonical `image:` schema (provider-agnostic)

Exactly one **base selector**, plus optional **layers**. A bare string ≡ `{base: <str>}`.

```yaml
image:
  debian_slim: "3.12"        # base (default if none given, with a warning)
  base: "python:3.12-slim"   # base: any registry ref
  dockerfile: "./Dockerfile" # base: build from a Dockerfile path
  snapshot: "snap-abc"       # skip build; create from a prebuilt snapshot (exclusive)

  workdir: /home/daytona
  env: { LOG_LEVEL: info }   # BUILD-time, non-secret only (secrets rejected)
  apt: [git, curl]
  pip: [numpy, pandas]
  pip_requirements: requirements.txt
  run: ["make build"]
  user: daytona
  entrypoint: ["/bin/sh"]
  cmd: ["-c", "..."]
```

## Daytona translation

| key | Daytona `Image` |
|---|---|
| `debian_slim` / `base` / `dockerfile` | `Image.debian_slim(v)` / `Image.base(ref)` / `Image.from_dockerfile(path)` |
| `snapshot` | `CreateSandboxFromSnapshotParams(snapshot=...)` (no Image build) |
| `workdir` | `.workdir(p)` |
| `env` | `.env({...})` |
| `apt` | one `.run_commands("apt-get update && apt-get install -y <pkgs> && rm -rf /var/lib/apt/lists/*")` |
| `pip` | `.pip_install([...])` |
| `pip_requirements` | `.pip_install_from_requirements(path)` |
| `run` | `.run_commands(*cmds)` |
| `user` | `.dockerfile_commands(["USER <user>"])` |
| `entrypoint` / `cmd` | `.entrypoint([...])` / `.cmd([...])` |

## Decisions (from review)

- **No base selector → default `debian_slim`** with a warning.
- **`apt` → one combined `run_commands` layer** (cleaner snapshot cache).
- **`files:` dropped** from v1.
- **Runtime validation only** (no frontend `image:` lint): unknown keys, multiple base
  selectors, snapshot+layers, and secrets in `image.env` all raise clear errors in the mapper.
- **Deterministic layer order:** base → workdir → env → apt → pip → pip_requirements → run →
  user → entrypoint → cmd.
- **Secrets never in `image.env`** (would bake into the cached snapshot) — injected at run time
  via the sandbox `env_file`.

## Implementation

- `sandbox/daytona_image.py`: `plan_image` (pure: validate + order → `ImageBuild`) +
  `apply_image_plan` (replay onto Daytona's `Image`). `DaytonaSandboxProvider._create_params`
  uses it. Unit-tested purely (no SDK/network) + an API-drift guard asserting the `Image` methods
  exist.

## Still deferred

Full Dockerfile-context uploads / `files:`, network-allowlist enforcement, real-service build test.
