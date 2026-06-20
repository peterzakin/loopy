# loopy.yaml — deployment config mapping to CLI flags

**Status:** SHIPPED — `loopy_runtime/config.py` (`LoopyConfig` + `load_config` + `resolve`), the
`--config` flag on `run`, flag>yaml>default precedence, and `REDIS_URL`-from-env are all built
(`tests/test_config.py`; documented in `DEPLOYMENT.md`). The reserved `state:` selector also landed
(durable SQLite default). The steps below are kept as the original draft record.
**Owner:** peter
**Date:** 2026-06-18

## Goal
Introduce a `loopy.yaml` config file for `loopy run` so deployment defaults (sensor-webhook
bind address, event-bus backend) live in a checked-in file instead of being passed as CLI
flags every invocation. Keys map ~1:1 to existing flags; the CLI flag stays as an override.

## Context
Backend selection and server binding are CLI-only today (`loopy_cli/__init__.py`):

- `--host` / `--port` — bind the FastAPI sensor-webhook listener. Consumed *only* at
  `loopy_cli/__init__.py:159` (`sensor_runner.start(host, port)`); if a manifest has no webhook
  sensors, uvicorn isn't started at all (`:158-163`) and these go unused. They do **not** govern
  the runtime drain loop, bus consumer, or poll/cron scheduler.
- `--bus` (`inproc | redis`) + `--redis-url` — selected via `make_event_bus()`
  (`loopy_runtime/bus/factory.py:13-27`).

There is no config file; selection is `argv` only. This plan adds one, scoped tightly.

Related backlog: durable StateStore is **TODO #2** (`TODOS.md:20`, B10); cumulative spend cap /
`--max-tokens` is **TODO #1** (`TODOS.md:10`). Both will eventually want config keys — reserved
here, not built (see Non-goals).

## Constraints & non-goals
- **Backward compatible:** absent `loopy.yaml` ⇒ all current defaults; behavior unchanged.
- **Precedence:** explicit CLI flag › `loopy.yaml` › built-in default.
- **Secrets/connection strings go to ENV, never YAML.** `redis_url` is resolved from the
  `--redis-url` flag › `REDIS_URL` env var › default — it is **not** a YAML key.
- **`run`-only.** `trigger`'s overridable options (`--sandbox`, `--root`) are out of scope, so the
  config is a `run` concern; `trigger` reads no config.
- Non-goals (deliberately excluded from v1 YAML):
  - `sandbox` — already first-class in the registry (`loopy_core/registry/model.py:19`, per-agent
    `sandbox:` override at `loader.py:116`). Not a deploy default.
  - `root` — per-invocation path, stays a CLI flag.
  - `state` / durability selector — **reserved shape only** (see below); no second backend exists
    yet (only `InMemoryStateStore`), so a one-value selector would be misleading. Lands with TODO #2.
  - `limits` (e.g. `max_tokens`) — **reserved shape only**; lands with TODO #1.

## Approach
A small `LoopyConfig` dataclass loaded from `loopy.yaml`, then merged with CLI flags using a
sentinel pattern: overridable `run` options default to `None` so "user passed the flag" is
distinguishable from "used the default", and the real value is resolved after loading config.

`bus` collapses to a scalar (no sub-section) because its only companion, `redis_url`, moves to
ENV. `host`/`port` sit under `sensor_server:` — named for what they actually bind (the sensor
webhook HTTP listener), not the broader process; the name also covers the deferred `POST /events`
intake (sensor-ingress Stage 3, `TODOS.md:67`) that will share this listener.

### v1 schema
```yaml
# loopy.yaml — deployment defaults for `loopy run`.
sensor_server:
  host: 127.0.0.1
  port: 8000

bus: inproc   # inproc | redis   (redis connection via REDIS_URL env var)
```

### Reserved-but-not-built (documented, NOT parsed in v1)
```yaml
# state: memory      # memory | durable  — TODO #2; DSN via DATABASE_URL env var
# limits:            # TODO #1
#   max_tokens: ...
```
Both mirror the `bus` pattern: scalar selector in YAML, connection string in ENV.

### Coupling note
The StateStore backs the bus's at-least-once dedupe (`loopy_cli/__init__.py:81-82`), so
`bus: redis` + in-memory state is a half-measure: broker survives restart, dedupe/history/
watermarks don't. The reserved `state:` knob is what makes `bus: redis` fully durable later.

## Steps
- [ ] `loopy_runtime/config.py`: `LoopyConfig(host, port, bus)` + `load_config(path) -> LoopyConfig`.
      Missing file ⇒ defaults; unknown keys ⇒ warning (not fatal); malformed YAML ⇒ clear error.
- [ ] Add `--config PATH` to `run` (default `./loopy.yaml`).
- [ ] Refactor `run` options to sentinel-`None`; resolve flag › config › default for host/port/bus.
      Resolve `redis_url` as flag › `REDIS_URL` env › default (no YAML).
- [ ] Confirm PyYAML availability (transitive via compile frontend) or add the dep explicitly.
- [ ] Tests: yaml-only, flag-overrides-yaml, missing-file-defaults, `REDIS_URL` honored,
      malformed-YAML diagnostic.
- [ ] Docs: document `loopy.yaml` in `DEPLOYMENT.md`; graduate the precedence rule into
      `ARCHITECTURE.md` per `plans/README.md:31`.
- [ ] On merge: `git mv` this plan to `plans/past/config/`.

## Files likely to change
- `loopy_runtime/config.py` — new: schema + loader.
- `loopy_cli/__init__.py` — `run`: `--config` flag, sentinel options, resolution wiring.
- `DEPLOYMENT.md` — document `loopy.yaml`.
- `ARCHITECTURE.md` — precedence + ENV-for-secrets decision.
- `pyproject.toml` — only if PyYAML must be made an explicit dependency.

## Open questions
- ENV layer breadth: only `REDIS_URL` for now, or also `LOOPY_HOST`/`LOOPY_PORT`/`LOOPY_BUS`?
  (Leaning: just `REDIS_URL`; add others if a deploy need appears.)
- Keep the runtime `--sandbox` provider selector (`local | daytona`, `sandbox/factory.py`) CLI-only?
  (Leaning: yes — distinct axis from the registry's named sandboxes.)

## Notes / decisions
- Dropped `sandbox` and `root` from config; `sandbox` is registry-defined, `root` is per-invocation.
- `redis_url` → ENV (`REDIS_URL`), not YAML — connection strings don't belong in the checked-in file.
- `bus` is a scalar (no sub-section) once `redis_url` left it.
- Section named `sensor_server` (not `server`) — host/port bind only the sensor webhook listener.
- `state`/`limits` reserved as future keys tied to TODO #2 / TODO #1; not parsed in v1.
