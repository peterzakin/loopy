#!/usr/bin/env bash
# Live end-to-end smoke test for the codefix example: compile → trigger a tiny edit →
# assert a PR URL comes back. The counterpart to the offline tests/conformance/test_codefix.py
# (which needs no creds); this one drives a *real* agent against a *real* repo.
#
# Usage:
#   TARGET_REPO=you/sandbox-repo ./examples/codefix/smoke.sh
#
# Requires:
#   - loopy on PATH (or run via `uv run`)
#   - a model key (ANTHROPIC_API_KEY in secrets/dev.env, or OAuth via HOME)
#   - git auth: a GitHub App (`loopy auth github`) OR GITHUB_TOKEN in secrets/dev.env
#   - TARGET_REPO: a repo you can push to (defaults to the registry's repos: entry)
#   - SANDBOX: docker (default) | local | daytona
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sandbox="${SANDBOX:-docker}"
branch="codefix/smoke-$(date +%s)"
task="${TASK:-Add a line to the end of README (create it if absent) noting this repo is a codefix smoke target.}"

# If TARGET_REPO is set, run against a temp copy of the project with repos: swapped, so the
# committed example stays untouched. Otherwise use the project as-is.
project="$here"
if [[ -n "${TARGET_REPO:-}" ]]; then
  project="$(mktemp -d)/codefix"
  cp -r "$here" "$project"
  trap 'rm -rf "$(dirname "$project")"' EXIT
  sed -i.bak -E "s#(repos:\s*\[)[^]]*(\])#\1${TARGET_REPO}\2#" "$project/registry.yml"
  rm -f "$project/registry.yml.bak"
  echo "smoke: targeting $TARGET_REPO (sandbox=$sandbox)"
fi

manifest="$(mktemp).json"
loopy compile "$project" --out "$manifest"

out="$(loopy trigger "$manifest" \
  --event CodeTask \
  --fields "{\"task\": \"${task}\", \"branch\": \"${branch}\"}" \
  --root "$project" \
  --sandbox "$sandbox" \
  --json)"
echo "$out"

# The whole point: a PR URL came back. Fail loudly if not.
if echo "$out" | grep -q '"pr_url": *"https'; then
  echo "smoke: PASS — PR opened on branch $branch"
else
  echo "smoke: FAIL — no pr_url in run record" >&2
  exit 1
fi
