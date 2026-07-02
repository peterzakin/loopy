"""Stable diagnostic codes — the test contract.

One named constant per code; the golden-negative suite is one fixture per code.
Codes are never renumbered once shipped. NOTE: there is intentionally no E108/E109.

`DESCRIPTIONS` is the catalog itself — one human line per code, keyed by the code string.
It is the single source the CLI renders (`loopy docs errors`), so a new code isn't done
until its line is here; `ALL_CODES` is derived from it and the golden suite pins both.
"""

from __future__ import annotations

# E0xx — discovery
E001 = "LOOPY-E001"

# E1xx — workflow / DAG / trigger
E101 = "LOOPY-E101"
E102 = "LOOPY-E102"
E103 = "LOOPY-E103"
E104 = "LOOPY-E104"
E105 = "LOOPY-E105"
E106 = "LOOPY-E106"
E107 = "LOOPY-E107"
E110 = "LOOPY-E110"
E111 = "LOOPY-E111"
E112 = "LOOPY-E112"

# E2xx — registry / types
E201 = "LOOPY-E201"
E210 = "LOOPY-E210"
E211 = "LOOPY-E211"
E212 = "LOOPY-E212"
E213 = "LOOPY-E213"
E214 = "LOOPY-E214"
E215 = "LOOPY-E215"

# E3xx — templates
E301 = "LOOPY-E301"
E302 = "LOOPY-E302"
E303 = "LOOPY-E303"
E304 = "LOOPY-E304"
E305 = "LOOPY-E305"

# E4xx — sensors
E401 = "LOOPY-E401"
E402 = "LOOPY-E402"
E403 = "LOOPY-E403"

# E5xx — resolution / bindings
E501 = "LOOPY-E501"
E502 = "LOOPY-E502"
E503 = "LOOPY-E503"
E504 = "LOOPY-E504"
E505 = "LOOPY-E505"
E506 = "LOOPY-E506"
E507 = "LOOPY-E507"
E508 = "LOOPY-E508"

# W5xx — lineage warnings
W501 = "LOOPY-W501"


DESCRIPTIONS: dict[str, str] = {
    # E0xx — discovery
    E001: "registry.yml missing/unparseable; layout unreadable",
    # E1xx — workflow / DAG / trigger
    E101: "invalid frontmatter or empty body (W1)",
    E102: "zero or >1 entry (on:) step (W2)",
    E103: "orphan step — neither on: nor after: (W3)",
    E104: "step has both on: and after: (W4)",
    E105: "after: target doesn't exist (W5)",
    E106: "after: graph has a cycle (W6)",
    E107: "duplicate step name / id collision (W7)",
    E110: 'malformed cron("<expr>"[, tz=...])',
    E111: "on: lists multiple events (unions unsupported)",
    E112: "on: names an unknown built-in event (reserved namespace, not in catalog)",
    # E2xx — registry / types
    E201: "unknown type shorthand in an event field or step output:",
    E210: "entity name not Capitalized / reserved `default` misused (X1)",
    E211: "duplicate entity name in registry.yml (X1)",
    E212: "malformed sandbox repos: entry (not owner/name string nor {url,...})",
    E213: "malformed registry limits: (e.g. cascade_spend without a numeric usd)",
    E214: "a sandbox definition is missing its required provider: (X1)",
    E215: "a user event/sensor uses a reserved built-in namespace (e.g. Github.*)",
    # E3xx — templates
    E301: "illegal template: control flow / filters / dotted path",
    E302: "event.<field> not in the triggering event's contract (T1)",
    E303: "cron trigger field other than scheduled_at / last_run (T2)",
    E304: "<step> is not a direct after: predecessor (T3)",
    E305: "<field> not a top-level key of that step's output: (T3)",
    # E4xx — sensors
    E401: "a sensor's declared emits event isn't registered (S2)",
    E402: "emits missing or not statically analyzable (S1)",
    E403: "duplicate webhook (path, emits), or malformed poll interval (S3)",
    # E5xx — resolution / bindings
    E501: "step agent: doesn't resolve to a registered Agent (X2)",
    E502: "an agent's sandbox doesn't resolve (X2)",
    E503: "an agent.skills[] entry doesn't resolve in skills/ (X2)",
    E504: "on:/emits: names an unregistered event (X3)",
    E505: "registry limits.workflows names a workflow that doesn't exist (X2)",
    E506: "an agent declares no sandbox (every agent must name one) (X2)",
    E507: "an agent declares no model/harness (both keys are mandatory) (X2)",
    E508: "an agent's model/harness pairing is ineligible or the harness is unknown (X2)",
    # W5xx — lineage warnings
    W501: "dead trigger — registered on: event with no producer (X5)",
}

ALL_CODES: frozenset[str] = frozenset(DESCRIPTIONS)
