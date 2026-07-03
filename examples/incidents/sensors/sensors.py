from loopy import sensor
from loopy.events import Incident, MetricThreshold


# Teaching example: this shapes Sentry's payload into the project's own normalized
# `Incident` (fanned in from several sources). To consume the raw Sentry event instead,
# the built-in `on: Sentry.IssueCreated` needs no sensor at all.
@sensor(webhook="/hooks/sentry", emits="Incident")
def sentry_issues(req) -> Incident:
    issue = req.json["data"]["issue"]
    return Incident(
        source="sentry",
        issue_id=issue["id"],
        title=issue["title"],
        link=issue["permalink"],
    )


@sensor(poll="5m", emits="MetricThreshold")
def metric_watch(req) -> MetricThreshold:
    return MetricThreshold(goal_id="g-123")
