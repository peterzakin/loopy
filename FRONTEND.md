# Loopy Frontend (`loopy-core`) — Technical Plan

The compile layer. Input: a project directory. Output: `manifest.json` (the contract in
`ARCHITECTURE.md` §2). Pure, dependency-free of any runtime, executes no workflow logic. This is `dbt
parse + compile`: discover → parse → resolve → validate → emit a declarative plan.

```
project/                          ┌─────────────── loopy compile ───────────────┐
  registry.yml      ──▶ discover ─▶ parse ─▶ resolve ─▶ validate ─▶ emit ──▶ manifest.json
  workflows/*/*.md                  (typed   (bind     (compile      (stable          + diagnostics
  skills/*/SKILL.md                  IR)      refs)     rules)         JSON)           (file:line)
  sensors/*.py
```

Non-negotiable: **collect all diagnostics, never fail-fast** — one run reports every error with
`file:line`, exits nonzero if any are errors.

---

## 1. Compile pipeline (passes)

| # | Pass | Module | Output |
|---|---|---|---|
| P0 | **Discover** | `discovery.py` | file inventory (registry, workflow dirs, skills, sensors) |
| P1 | **Parse registry** | `registry/loader.py` | typed `Registry` (defaults applied) |
| P2 | **Desugar registry field types → JSON Schema** | `registry/types.py` | JSON Schema fragment per event field (step `output:` desugared at P3 via the same fn) |
| P3 | **Parse workflows** | `workflow/loader.py` | `Step` per `.md` (frontmatter + body + source span) |
| P4 | **Build DAGs** | `workflow/dag.py` | one validated DAG per workflow |
| P5 | **Extract templates** | `template/parser.py` | `Ref` list per step body |
| P6 | **Resolve references** | `template/resolver.py` | bound refs; agent/skill/sandbox/event bindings |
| P7 | **Load sensors** | `sensors/loader.py` | sensor descriptors + emitted-event check (AST, no import) |
| P8 | **Validate** | `compile/pipeline.py` | diagnostics (errors + warnings) |
| P9 | **Emit** | `compile/manifest.py` | `manifest.json` + `loopy.events` stubs |

Passes accumulate into an in-memory IR; P8 runs cross-cutting checks over the whole IR; P9
serializes. Each pass attaches a **source span** (`file`, `line`, `col`) to every node so
diagnostics and the manifest can point back to source.

---

## 2. Intermediate representation (IR)

Pydantic models, one module per domain. Sketch (types abbreviated):

```python
# registry/model.py
class Sandbox(BaseModel):   name: str; provider: str; image: dict; network: list[str]; span: Span
class Harness(BaseModel):   runtime: str; model: str
class Agent(BaseModel):     name: str; harness: Harness; sandbox: str; tools: list[str]; skills: list[str]; span: Span
# field types are plain JSON Schema fragments (dict), not a custom type — see §3
class Event(BaseModel):     name: str; fields: dict[str, dict]; span: Span
class Registry(BaseModel):  sandboxes: dict[str,Sandbox]; agents: dict[str,Agent]; events: dict[str,Event]

# workflow/model.py
class Trigger(BaseModel):   kind: Literal["event","cron"]; event: str|None; expr: str|None; tz: str|None   # exactly one event (no unions)
class Budget(BaseModel):    wall_clock: int|None; spend: dict|None; window: int|None; latency: int|None
class Ref(BaseModel):       producer: str; field: str; raw: str; span: Span   # producer = "event" | <step name>
class Step(BaseModel):
    id: str                 # "<workflow>/<step>"
    workflow: str; name: str
    trigger: Trigger|None   # set iff entry step
    after: list[str]        # local step names
    agent: str; output: dict[str,dict]; emits: list[str]   # output values are JSON Schema fragments
    budget: Budget|None; body: str; refs: list[Ref]; span: Span
class Workflow(BaseModel):  name: str; entry: str; steps: dict[str,Step]; dag: "nx.DiGraph"

# sensors/model.py  (defined in M0, populated/validated in M4 — §7)
class SensorTrigger(BaseModel): kind: Literal["webhook","poll"]; path: str|None; interval: str|None; span: Span  # distinct from Trigger
class Sensor(BaseModel):        name: str; trigger: SensorTrigger; emits: str; module: str; fn: str; span: Span    # emits = declared registered event
```

The IR is the single source the manifest is serialized from — keep it lossless w.r.t. anything a
runtime needs.

---

## 3. Field types — JSON Schema + pydantic (no custom type system)

We own **no** type semantics. Field types in `registry.yml` (event `fields:`) and step `output:`
are represented canonically as **JSON Schema (draft 2020-12)** in the manifest, validated at
runtime by **pydantic v2**, and code-generated into `loopy.events` classes via
**`datamodel-code-generator`**. JSON Schema is the wire format; pydantic is the validator/codegen;
both round-trip with each other.

The README's terse forms (`str`, `enum[pass, fail]`, `id`) are kept purely as **authoring sugar** —
a small, mechanical desugar table, not a type system:

| Authored | JSON Schema emitted |
|---|---|
| `str` | `{"type": "string"}` |
| `int` / `float` / `bool` | `{"type": "integer" / "number" / "boolean"}` |
| `id` | `{"type": "string", "format": "loopy-id"}` |
| `url` | `{"type": "string", "format": "uri"}` |
| `enum[a, b]` | `{"type": "string", "enum": ["a", "b"]}` |

Rules:
- Authors may also write **raw JSON Schema inline** wherever sugar is too limited
  (`{type: array, items: {type: string}}`) — the desugarer passes through anything that's already
  a schema object. So expressiveness is unbounded without growing the sugar.
- P2 is just this desugar pass; unknown bare shorthand (not in the table and not a valid schema
  object) → diagnostic `LOOPY-E201`.
- Event-contract **evolution/compat** (events are a cross-workflow bus contract) is a JSON Schema
  concern we can adopt off-the-shelf later; Avro is the upgrade path if formal compatibility rules
  are needed.

---

## 4. Frontmatter & workflow parsing (P3)

- Split `---\n<yaml>\n---\n<body>` via `python-frontmatter`; parse the YAML with **`ruamel.yaml`**
  to retain line numbers (so a bad `after:` points at the right line).
- Track the body's starting line so template `Ref` spans are accurate.
- Step identity: `<workflow_dir>/<filename_stem>`. `after:` names are **local** to the workflow
  (matches README `after: fix`).
- `on:` names a **single** event or `cron(...)`; a list of events (`on: [A, B]`) is rejected —
  unions are unsupported in v1 (`LOOPY-E111`). `after:` / `emits:` accept scalar or list and
  normalize to lists internally.
- Desugar each step's `output:` map to JSON Schema here, reusing the P2 `registry/types.py`
  function (step outputs don't exist until this pass). This ensures P6's resolver sees desugared
  `output:` schemas; bad shorthand in a step output raises `LOOPY-E201` with the step's `file:line`.

---

## 5. DAG construction (P4)

Per workflow, build a `networkx.DiGraph` from `after:` edges; the `on:` step is the root.

Rules (each → a diagnostic):
- **W1** every `.md` has valid frontmatter + non-empty body.
- **W2** exactly one entry step (`on:`) per workflow — zero or many → error.
- **W3** every non-entry step has `after:`; neither `on:` nor `after:` → **orphan** error.
- **W4** `on:` and `after:` are mutually exclusive on a step (`emits:` allowed on any).
- **W5** every `after:` target exists in the same workflow.
- **W6** the `after:`-graph is **acyclic** (`nx.find_cycle`); loop-backs must go through *events*,
  not `after:`.
- **W7** step names unique within a workflow; `(workflow, step)` unique globally.

`cron(<expr>[, tz=...])` is parsed here: validate with **`croniter`** (5 fields), parse optional
`tz=` (IANA name). Malformed → `LOOPY-E110`.

---

## 6. Template extraction & static resolution (P5–P6)

**Restricted grammar (decision).** Bodies support only simple substitution refs
`{{ <producer>.<field> }}` — **no** Jinja control flow (`{% %}`), filters, or expressions. This is
what makes static validation total and runtime rendering trivial. A dedicated parser (regex/lexer)
extracts refs; arbitrary `{% %}` or multi-segment paths → `LOOPY-E301`.

**Resolution rules** (`<producer>` is `event` or a step name):
- **T1** `event.<field>` — `event` is the run's triggering event (a step triggers on **exactly
  one** event; unions are unsupported, so there's no intersection rule). `<field>` must exist in
  that event's contract.
- **T2** cron triggers expose the built-in fields `scheduled_at` and `last_run` (+ nothing else).
- **T3** `<step>.<field>` — `<step>` must be a **direct** `after:` predecessor (decision:
  direct-only, like dbt's explicit `ref()`; want a grandparent value → re-`after:` it or have the
  parent pass it through). `<field>` must exist in that step's `output:` schema.
- **T4** (soft, v1-optional) type compatibility where a ref feeds a typed slot — deferred; v1
  treats refs as opaque substitution and validates existence only.
- **T5 (flat keys — decision).** `<field>` is a **top-level key** of the producer's output object
  (or the event's `fields:`), so a ref is exactly two segments: `{{ producer.key }}`. There is **no**
  dotted descent into nested objects (`{{ fix.result.score }}` → `LOOPY-E301`). This keeps ref
  validation a single lookup against the schema's top-level `properties` and avoids array-index /
  `additionalProperties` / union ambiguities.
  - Authoring rule: treat a step's `output:` as its **public interface** — anything a downstream
    step references individually must be a top-level key. Nested objects are fine for values that
    travel as a *unit* (reference the whole object with `{{ fix.result }}`); if a consumer needs
    `score` on its own, the producer should expose `score` as a top-level output key.
  - *Future:* dotted descent could be added by walking JSON Schema `properties` segment-by-segment
    at resolve time (object descent only, no array indexing), keeping it statically checkable.
    Punted for v1 alongside T4.

Resolution binds each `Ref` to its producing node, which P9 records in the manifest so the backend
resolves `{{ }}` at run time against recorded values.

---

## 7. Sensors (P7)

- **Static inspection only — never import or execute sensor modules.** Parse each sensor file
  statically (Python → AST) and read each sensor's declared metadata. No user code runs at compile
  time, so the frontend stays pure and offline; `loopy.events` is a pure *output* artifact, never a
  compile input. The only fact the checker needs is the set of registered event names from P1.
- **`emits` is declared, not inferred.** Each sensor declares the event it emits as a literal
  alongside its trigger config (`webhook`/`poll`) — Core reads that literal directly. The return
  type annotation (`-> Incident`) becomes **optional sugar checked by the author's own typechecker**
  (mypy/tsc against `loopy.events`), not Core's source of truth. This is what keeps the inspector
  language-agnostic: every language exposes the same contract — *a statically readable `emits` +
  trigger* — so the per-language inspector locates a declaration rather than resolving a foreign
  type system. (Decision §13 #4.)
- **Two equivalent surfaces; pick the idiom per language** (both reduce to the same descriptor):
  - **decorator form** — `@sensor(webhook="/hooks/sentry", emits="Incident")` (idiomatic in Python,
    which has free-function decorators); or
  - **registry-literal form** — a single named export (`sensorRegistry`) that is a **statically
    analyzable object/array literal** of `{trigger, emits, handler}` entries (idiomatic in
    TypeScript and other languages without free-function decorators).
- **The hard constraint on both surfaces: the declaration must be a literal Core can read without
  executing anything.** A registry populated imperatively (loops, `.push()`, spreads from computed
  values) defeats static inspection → `LOOPY-E402` ("sensor declaration not statically analyzable"),
  not silent omission.
- **The per-language inspector is pluggable.** Each language ships an inspector that walks its
  source to the common descriptor `{name, trigger(webhook|poll), emits, module, fn}`; the Python
  AST inspector is the first implementation. Core depends only on the descriptor.
- **S1** every sensor declares a statically readable `emits` (and trigger); missing or
  non-statically-analyzable → `LOOPY-E402`.
- **S2** that event is **registered** in `registry.yml`; otherwise the project fails to compile
  (`LOOPY-E401`) — this is the README's hard rule.
- **S3** webhook `path` unique across sensors; `poll` interval well-formed.
- Record `{name, trigger(webhook|poll), emits, module, fn}` per sensor.

---

## 8. Cross-cutting validation (P8)

Beyond per-pass checks, the whole-IR pass adds:
- **X1** registry naming: entities Capitalized; `default` is the one reserved lowercase sandbox;
  no duplicate names.
- **X2** every `agent:` (step) resolves to a registered Agent; every `agent.sandbox` resolves;
  every `agent.skills[]` resolves to a skill in `skills/` — unresolved → error (no external skills).
- **X3** every *event-kind* `on:` / `emits:` event is registered in `registry.yml` (cron triggers
  are not events and need no registry entry).
- **X4** every `output:` and event `fields:` type parses via the shared desugar pass — event
  `fields:` at P2, step `output:` at P3 — surfacing `LOOPY-E201` on unknown shorthand.
- **X5** **lineage**: build the event graph (producers = sensors + steps that `emit`; consumers =
  steps whose `on:` names it). An `on:` event with **no** producer → **warning** `LOOPY-W501`
  (dead trigger). A terminal `emits:` with no consumer is **allowed** (e.g. `GoalShipped`).

Diagnostics carry `{severity, code, message, span, hint?}`; codes are stable
(`LOOPY-E###`/`LOOPY-W###`) so tests assert on them.

> Note on prefixes: rule-family labels (`W#` workflow, `T#` template, `S#` sensor, `X#`
> cross-cutting) are **not** diagnostic codes. In particular `W#` rules are *workflow* rules and
> mostly emit **errors** — don't confuse them with `LOOPY-W###` *warning* codes.

---

## 9. Manifest emission (P9)

Deterministic JSON — **sorted keys, stable ordering** — so the manifest diffs cleanly and can be
content-hashed for caching / change-detection (dbt uses its manifest exactly this way). Shape:

```jsonc
{
  "schema_version": "1",
  "registry": {
    "sandboxes": { "default": { "provider": "...", "image": {...}, "network": [...] } },
    "agents":    { "Investigator": { "harness": {...}, "sandbox": "default", "tools": [...], "skills": [...] } },
    "events":    { "WorkItem": { "fields": { "source": {"type":"string","enum":["sentry","linear","..."]}, "link": {"type":"string","format":"uri"} } } }
  },
  "workflows": {
    "triage": {
      "entry": "investigate",
      "steps": {
        "investigate": {
          "id": "triage/investigate",
          "trigger": { "kind": "event", "event": "Incident" },
          "after": [], "agent": "Investigator",
          "output": {}, "emits": ["WorkItem"],   // single-step workflow: no downstream consumer, so no output — it just emits WorkItem
          "budget": { "wall_clock": 20, "spend": {"usd": 4} },
          "body": "…prose with {{ event.issue_id }}…",
          "refs": [ { "producer": "event", "field": "issue_id", "raw": "{{ event.issue_id }}" } ]
        }
      }
    }
  },
  "sensors":  [ { "name": "sentry_issues", "trigger": {"kind":"webhook","path":"/hooks/sentry"},
                  "emits": "Incident", "module": "sensors.sensors", "fn": "sentry_issues" } ],
  "lineage":  { "events": { "WorkItem": { "producers": ["triage/investigate"], "consumers": ["resolve/arbitrate"] } } }
}
```

Notes:
- `body` stays a **template**; `refs` are pre-bound — the backend renders at run time. (Holes, per
  `ARCHITECTURE.md` §0.)
- `compiled_at` / `loopy_version` are stamped by the CLI wrapper, **outside** the deterministic
  core, so the hashable content stays reproducible.
- A companion **`manifest.schema.json`** (JSON Schema) validates the emitted manifest in CI and
  documents the contract backends consume.

**`loopy.events` codegen.** Emit typed event definitions from the registry so sensors can import
them and the author's typechecker validates the payload shape. Codegen is **multi-target**, one
target per supported sensor language: Python (a real module + `.pyi` stubs, via
`datamodel-code-generator` or hand-emitted pydantic classes — `from loopy.events import Incident`);
TypeScript (`.d.ts` types — `import type { Incident } from "loopy/events"`); further targets are
additive. These types are author-facing convenience only; Core never consumes them (it reads the
declared `emits` literal — §7).

---

## 10. Package layout

```
loopy_core/
  cli.py                 # `loopy compile [--out manifest.json]`
  discovery.py
  registry/   model.py · loader.py · types.py
  workflow/   frontmatter.py · model.py · loader.py · dag.py
  template/   parser.py · resolver.py
  sensors/    model.py · loader.py
  events/     codegen.py
  compile/    pipeline.py · diagnostics.py · manifest.py
  manifest.schema.json
```

Dependencies: `pydantic`, `ruamel.yaml`, `python-frontmatter`, `networkx`, `croniter`, `typer`,
`datamodel-code-generator`, `jsonschema` (validate field schemas + the manifest). No
runtime/agent/sandbox deps.

---

## 11. Build milestones (maps to ARCHITECTURE Phases 1–5)

| M | Deliverable | Gate |
|---|---|---|
| **M1** | discovery + registry loader + type DSL + diagnostics framework | registry round-trips; bad types reported |
| **M2** | frontmatter + step model + DAG build/validate | all W-rules fire on negative fixtures |
| **M3** | template parser + static ref resolution | T1–T3 + T5 fire (T4 deferred); valid refs bind |
| **M4** | `loopy.events` codegen + sensor loader (AST, no import) | S-rules fire; sensors AST-parsed + validated (both emit forms), no user code executed |
| **M5** | manifest emitter + `manifest.schema.json` + `loopy compile` CLI | README example compiles to a snapshot |

---

## 12. Testing strategy

- **Golden positive:** the README example project (incidents + autoresearch) compiles to a
  checked-in `manifest.json` snapshot; assert byte-stable output.
- **Golden negative:** one fixture per diagnostic code — a minimal broken project asserting the
  exact `LOOPY-E###`/`W###` + `file:line`. This is the bulk of the suite and the real spec.
- **Properties:** compile is idempotent and order-independent (shuffling file discovery yields an
  identical manifest); manifest validates against `manifest.schema.json`.
- **No network, no model, no sandbox** — the frontend is fully offline-testable, which is the
  point of front-loading it.

---

## 13. Key decisions (flag before M1)

1. **Template grammar is restricted** to `{{ producer.field }}` — no Jinja control flow. (Enables
   total static validation.) **DECIDED — accepted.**
2. **`after:` refs are direct-only**, not transitive — **DECIDED.** A step reads only its direct
   `after:` predecessors; to reach an earlier step, add it to that step's `after:` list (matches
   dbt `ref()`). May revisit transitive/ancestor refs later if needed.
3. **Union `on:` is not supported — DECIDED.** A step triggers on exactly one event; multi-source
   fan-in happens at the sensor layer (several sensors emit one normalized event, e.g. `Incident`).
   A first-class multi-trigger / source-group syntax may be revisited later.
4. **Sensors are validated by static inspection, never import — DECIDED.** No user code runs at
   compile time (supersedes the earlier "controlled subprocess" approach). **The emitted event is
   *declared*, not inferred from the return type:** each sensor states `emits` as a literal next to
   its trigger config, and Core reads it from a statically analyzable form — a decorator
   (`@sensor(emits="Incident")`, idiomatic in Python) or a named `sensorRegistry` object/array
   literal (idiomatic in TypeScript). The return annotation is optional sugar the author's own
   typechecker enforces. This supersedes the earlier annotation/tuple *inference* approach, which
   was Python-type-system-specific and didn't generalize; declaration keeps the per-language
   inspector a literal-read, not a foreign type resolver. A declaration Core can't read statically
   (imperatively built registry) is an error (`E402`), not a silent omission. The surface differs
   per language; the invariant — *a statically readable `emits` + trigger* — does not. (§7)
5. **Manifest is deterministic/hashable**; `compiled_at` lives outside core. **DECIDED — accepted**
   (content-hashing enables incremental runs later).
6. **Field types are JSON Schema; terse forms are sugar over it** (§3). **DECIDED — keep the sugar**
   (option B); authors may still drop to raw JSON Schema inline when needed.

---

## 14. Diagnostic codes (the test contract)

Every rule maps to a **stable** code; the golden-negative suite (§12) is **one fixture per code**.
Codes are grouped by family and never renumbered once shipped. `E###` = error, `W###` = warning.
Some codes are shared where the same failure surfaces from more than one place (noted).

| Code | Rule | Pass | Fires when |
|---|---|---|---|
| **E001** | — | P0 | `registry.yml` missing or unparseable; project layout unreadable |
| **E101** | W1 | P3 | `.md` has invalid frontmatter or an empty body |
| **E102** | W2 | P4 | a workflow has zero or more than one entry (`on:`) step |
| **E103** | W3 | P4 | orphan step — neither `on:` nor `after:` |
| **E104** | W4 | P4 | a step carries both `on:` and `after:` |
| **E105** | W5 | P4 | an `after:` target doesn't exist in the same workflow |
| **E106** | W6 | P4 | the `after:` graph has a cycle |
| **E107** | W7 | P4 | duplicate step name in a workflow / `(workflow, step)` id collision |
| **E110** | — | P4 | malformed `cron(<expr>[, tz=…])` |
| **E111** | — | P3/P4 | `on:` lists multiple events (unions unsupported) |
| **E201** | X4 | P2/P3 | unknown type shorthand in event `fields:` or step `output:` (desugar) |
| **E210** | X1 | P8 | entity name not Capitalized, or reserved `default` misused |
| **E211** | X1 | P8 | duplicate entity name in `registry.yml` |
| **E301** | T-grammar / T5 | P5/P6 | illegal template: control flow, filters, multi-segment / dotted path |
| **E302** | T1 | P6 | `event.<field>` not in the triggering event's contract |
| **E303** | T2 | P6 | cron trigger field other than `scheduled_at` / `last_run` |
| **E304** | T3 | P6 | `<step>` is not a **direct** `after:` predecessor |
| **E305** | T3 | P6 | `<field>` not a top-level key of that step's `output:` schema |
| **E401** | S2 | P7 | a sensor's declared `emits` event isn't registered |
| **E402** | S1 | P7 | sensor `emits` missing or not statically analyzable (e.g. imperatively built registry) |
| **E403** | S3 | P7 | duplicate webhook `path`, or malformed `poll` interval |
| **E501** | X2 | P8 | step `agent:` doesn't resolve to a registered Agent |
| **E502** | X2 | P8 | an agent's `sandbox` doesn't resolve |
| **E503** | X2 | P8 | an `agent.skills[]` entry doesn't resolve in `skills/` |
| **E504** | X3 | P8 | a step's `on:` / `emits:` names an unregistered event |
| **W501** | X5 | P8 | dead trigger — a registered `on:` event with no producer |

Notes:
- **T4** (type compatibility) is deferred — no code in v1.
- **E201** is shared: the P2/P3 desugar pass and X4 are the same check from two entry points.
- **E301** covers both the grammar restriction (§6) and the T5 no-dotted-descent rule.
- `E0xx` discovery, `E1xx` workflow/DAG/trigger, `E2xx` registry/types, `E3xx` templates,
  `E4xx` sensors, `E5xx` resolution/bindings, `W5xx` lineage warnings. New rules extend their
  family's range; reserved-but-unused numbers are fine.
