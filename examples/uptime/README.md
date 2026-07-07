# uptime — a poll sensor and the workflow it wakes

The smallest look at the **sensor layer**. A poll sensor checks a health endpoint every
few minutes and emits an `Incident` only when it is down. A second workflow reacts by
opening a GitHub issue — it has no idea a poll produced the event; it just consumes it.

```
every 5m ─▶ health_check() ── 200? ──▶ (nothing)
                └─ non-200 ──▶ Incident ──▶ workflows/respond/open-issue.md ──▶ Acknowledged
```

| Piece | File | What it does |
| --- | --- | --- |
| Sensor | `sensors/sensors.py` | polls `HEALTH_URL`; returns `None` when healthy, an `Incident` when not |
| Workflow | `workflows/respond/open-issue.md` | opens a GitHub issue for the outage |

## The sensor

`@sensor(poll="5m", emits="Incident")` runs the function on the interval; **returning is
emitting**. A healthy check returns `None`, so nothing goes on the bus — turning a raw
signal into a typed event, and only when it matters, is the whole job of a sensor. Point
`HEALTH_URL` at your own endpoint.

## Run it

```
cp examples/uptime/base.env.example examples/uptime/secrets/base.env  # fill it in
loopy compile examples/uptime --out manifest.json
loopy run --in-process manifest.json
```

### Try it without an outage

Fire the `Incident` by hand instead of waiting for the endpoint to go down:

```
loopy trigger examples/uptime --event Incident \
  --fields '{"url":"https://example.com/health","status":503}'
```
