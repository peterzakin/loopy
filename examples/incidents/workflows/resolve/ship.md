---
after: review
agent: Releaser
emits: GoalShipped
---
If {{ review.verdict }} is pass, merge the PR and ship the change.
