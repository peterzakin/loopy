---
on: PullRequestOpened
agent: Reviewer
output:
  verdict: str
  comments_posted: int
emits: ReviewPosted
budget: { wall_clock: 15, spend: { usd: 2 } }
---
A checkout of {{ event.repo }} is already in your workspace, with a GitHub token wired into
git and the `gh` CLI — so fetching branches and posting review comments just work.

Review pull request #{{ event.number }} — "{{ event.title }}" — which merges branch
`{{ event.branch }}` into `{{ event.base }}`.

1. Fetch the PR branch and produce the diff against `{{ event.base }}`.
2. Review it for correctness bugs, security issues, and clear simplifications. Focus on
   high-confidence findings; don't nitpick style.
3. Post each finding as an inline review comment on {{ event.url }} via `gh` (the injected
   token authenticates you). If it's clean, leave a single approving summary comment.

Return your overall verdict (e.g. "approve" / "changes requested") and the number of
comments you posted.
