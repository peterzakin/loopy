from loopy import sensor
from loopy.events import Incident, MetricThreshold


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
