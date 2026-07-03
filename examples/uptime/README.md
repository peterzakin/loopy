# uptime — a poll sensor that fires only on failure

The smallest example of the **sensor layer**: a poll sensor checks a health endpoint every
few minutes and emits an `Incident` only when it's down. A second workflow reacts by opening
a GitHub issue.

```
health_check (poll 5m) ─▶ Incident ─▶ respond/open-issue (Responder) ─▶ Acknowledged
       emits only on failure           opens a GitHub issue for the outage
```

Where [`standup`](../standup/) and [`issue-triage`](../issue-triage/) lean on built-in
triggers, this one writes a real (tiny) sensor. It's the clearest illustration of what
sensors are for.

## What it shows

- **A conditional emit.** `sensors/sensors.py` returns `None` on a healthy check, so nothing
  goes on the bus and no run starts. Only a non-200 becomes an `Incident`. A sensor's job is
  to turn a raw signal into a typed event, and only when it matters.
- **The event seam between two workflows.** The sensor emits `Incident`; a separate workflow
  subscribes with `on: Incident`. Neither knows about the other beyond the event contract.
- **Poll mechanics.** `@sensor(poll="5m")` runs every 5 minutes; ticks never overlap.

## Run it

> From a checkout of this repo, prefix each command with `uv run`.

```bash
# 1. edit sensors/sensors.py: point HEALTH_URL at the endpoint you want to watch
# 2. set ANTHROPIC_API_KEY (and wire git auth for the gh issue)
cp examples/uptime/base.env.example examples/uptime/secrets/base.env

# 3. compile
loopy compile examples/uptime

# 4. run the engine — the sensor polls on its own; you can also fire an Incident by hand:
loopy trigger examples/uptime \
  --event Incident \
  --fields '{"url": "https://example.com/health", "status": 503}' \
  --json
```

The hand-fired `Incident` warns `LOOPY-W501 dead trigger` only if no sensor produces it; here
the poll sensor does, so it drives the respond workflow exactly as a real outage would.
