# Plans

Working plans for features and tasks, created with a coding agent before code is written.

## Convention
- Copy `TEMPLATE.md` to `YYYY-MM-DD-short-slug.md`, one file per task.
- Draft it with the agent, then have the agent tick off steps and log decisions as it works.
- Commit plans alongside the code they produce.
- Delete or archive a plan once its work merges — unless it captures a lasting
  architectural decision, in which case move the rationale somewhere durable
  (e.g. a `docs/decisions/` ADR) so it isn't lost when the plan is pruned.

Point your agent at this folder (e.g. from `CLAUDE.md`) so it reads the active plan
at the start of a session.
