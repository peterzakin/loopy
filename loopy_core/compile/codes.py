"""Stable diagnostic codes (FRONTEND §14) — the test contract.

One named constant per code; the golden-negative suite is one fixture per code.
Codes are never renumbered once shipped. NOTE: there is intentionally no E108/E109.
"""

from __future__ import annotations

# E0xx — discovery
E001 = "LOOPY-E001"  # registry.yml missing/unparseable; layout unreadable

# E1xx — workflow / DAG / trigger
E101 = "LOOPY-E101"  # invalid frontmatter or empty body (W1)
E102 = "LOOPY-E102"  # zero or >1 entry (on:) step (W2)
E103 = "LOOPY-E103"  # orphan step — neither on: nor after: (W3)
E104 = "LOOPY-E104"  # step has both on: and after: (W4)
E105 = "LOOPY-E105"  # after: target doesn't exist (W5)
E106 = "LOOPY-E106"  # after: graph has a cycle (W6)
E107 = "LOOPY-E107"  # duplicate step name / id collision (W7)
E110 = "LOOPY-E110"  # malformed cron("<expr>"[, tz=...])
E111 = "LOOPY-E111"  # on: lists multiple events (unions unsupported)

# E2xx — registry / types
E201 = "LOOPY-E201"  # unknown type shorthand in event fields: or step output:
E210 = "LOOPY-E210"  # entity name not Capitalized / reserved `default` misused (X1)
E211 = "LOOPY-E211"  # duplicate entity name in registry.yml (X1)
E212 = "LOOPY-E212"  # malformed sandbox repos: entry (not owner/name string nor {url,...})
E213 = "LOOPY-E213"  # malformed registry limits: (e.g. cascade_spend without a numeric usd)
E214 = "LOOPY-E214"  # a sandbox definition is missing its required provider: (X1)

# E3xx — templates
E301 = "LOOPY-E301"  # illegal template: control flow / filters / dotted path
E302 = "LOOPY-E302"  # event.<field> not in the triggering event's contract (T1)
E303 = "LOOPY-E303"  # cron trigger field other than scheduled_at / last_run (T2)
E304 = "LOOPY-E304"  # <step> is not a direct after: predecessor (T3)
E305 = "LOOPY-E305"  # <field> not a top-level key of that step's output: (T3)

# E4xx — sensors
E401 = "LOOPY-E401"  # a sensor's declared emits event isn't registered (S2)
E402 = "LOOPY-E402"  # emits missing or not statically analyzable (S1)
E403 = "LOOPY-E403"  # duplicate webhook (path, emits), or malformed poll interval (S3)

# E5xx — resolution / bindings
E501 = "LOOPY-E501"  # step agent: doesn't resolve to a registered Agent (X2)
E502 = "LOOPY-E502"  # an agent's sandbox doesn't resolve (X2)
E503 = "LOOPY-E503"  # an agent.skills[] entry doesn't resolve in skills/ (X2)
E504 = "LOOPY-E504"  # on:/emits: names an unregistered event (X3)
E505 = "LOOPY-E505"  # registry limits.workflows names a workflow that doesn't exist (X2)
E506 = "LOOPY-E506"  # an agent declares no sandbox (every agent must name one) (X2)

# W5xx — lineage warnings
W501 = "LOOPY-W501"  # dead trigger — registered on: event with no producer (X5)


ALL_CODES: frozenset[str] = frozenset(
    {
        E001,
        E101,
        E102,
        E103,
        E104,
        E105,
        E106,
        E107,
        E110,
        E111,
        E201,
        E210,
        E211,
        E212,
        E213,
        E214,
        E301,
        E302,
        E303,
        E304,
        E305,
        E401,
        E402,
        E403,
        E501,
        E502,
        E503,
        E504,
        E505,
        E506,
        W501,
    }
)
