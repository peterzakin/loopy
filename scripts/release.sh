#!/usr/bin/env bash
#
# release.sh — cut a new loopy-computer release to PyPI.
#
# Usage:
#   scripts/release.sh <version>        # e.g. scripts/release.sh 0.2.0
#   scripts/release.sh <version> --dry-run   # build + checks, no publish/tag/push
#
# What it does, in order:
#   1. Preflight: clean working tree, on main, up to date with origin.
#   2. Refuse if <version> already exists on PyPI (PyPI never allows re-upload).
#   3. Write the version into pyproject.toml.
#   4. Build sdist + wheel into dist/.
#   5. Smoke-test the built wheel in a throwaway venv (CLI runs).
#   6. Commit the version bump.
#   7. Publish to PyPI (token from $PYPI_TOKEN, sourced from ~/.zprofile if unset).
#   8. Tag vX.Y.Z and push the commit + tag.
#
# The PyPI upload (step 7) is the point of no return — everything before it is
# local and reversible. On --dry-run we stop after step 5.

set -euo pipefail

# ---- locate repo root (script lives in scripts/) -----------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PKG_NAME="loopy-computer"
MAIN_BRANCH="main"

die() { echo "error: $*" >&2; exit 1; }

# ---- args --------------------------------------------------------------------
VERSION="${1:-}"
DRY_RUN=false
[ "${2:-}" = "--dry-run" ] && DRY_RUN=true

[ -n "$VERSION" ] || die "usage: scripts/release.sh <version> [--dry-run]"
# Plain semver X.Y.Z (optionally a prerelease suffix like 0.2.0rc1).
echo "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([a-z]+[0-9]+)?$' \
  || die "version '$VERSION' is not a valid X.Y.Z (e.g. 0.2.0 or 0.2.0rc1)"

echo "==> Releasing $PKG_NAME $VERSION${DRY_RUN:+ (dry run)}"

# ---- 1. preflight ------------------------------------------------------------
[ -z "$(git status --porcelain)" ] || die "working tree is dirty; commit or stash first"

branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "$MAIN_BRANCH" ] || die "on branch '$branch', expected '$MAIN_BRANCH'"

git fetch --quiet origin "$MAIN_BRANCH"
[ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$MAIN_BRANCH")" ] \
  || die "local $MAIN_BRANCH is not in sync with origin/$MAIN_BRANCH; pull/push first"

git rev-parse "v$VERSION" >/dev/null 2>&1 && die "git tag v$VERSION already exists"

# ---- 2. refuse if version already on PyPI ------------------------------------
echo "==> Checking PyPI for an existing $VERSION ..."
if curl -fsS "https://pypi.org/pypi/$PKG_NAME/$VERSION/json" >/dev/null 2>&1; then
  die "$PKG_NAME $VERSION is already on PyPI — bump to a new version"
fi

# ---- 3. set the version in pyproject.toml ------------------------------------
echo "==> Setting version in pyproject.toml ..."
# Replace only the first top-level `version = "..."` line.
perl -0pi -e 's/^version = "[^"]*"/version = "'"$VERSION"'"/m' pyproject.toml
grep -q "^version = \"$VERSION\"" pyproject.toml || die "failed to set version in pyproject.toml"

# ---- 4. build ----------------------------------------------------------------
echo "==> Building ..."
rm -rf dist
uv build

# ---- 5. smoke-test the built wheel -------------------------------------------
echo "==> Smoke-testing the built wheel ..."
wheel="$(ls dist/*.whl)"
uv run --refresh --no-project --python 3.12 --with "$wheel" loopy --help >/dev/null \
  || die "smoke test failed: 'loopy --help' did not run from the built wheel"
echo "    ok"

if $DRY_RUN; then
  echo "==> Dry run complete. Reverting pyproject.toml version bump."
  git checkout -- pyproject.toml
  echo "    Built artifacts left in dist/. Nothing published, committed, or tagged."
  exit 0
fi

# ---- 6. commit the bump ------------------------------------------------------
echo "==> Committing version bump ..."
git add pyproject.toml
git commit -m "chore: release v$VERSION"

# ---- 7. publish (point of no return) -----------------------------------------
if [ -z "${PYPI_TOKEN:-}" ] && [ -f "$HOME/.zprofile" ]; then
  # shellcheck disable=SC1091
  source "$HOME/.zprofile"
fi
[ -n "${PYPI_TOKEN:-}" ] || die "PYPI_TOKEN is not set (export it or add it to ~/.zprofile)"

echo "==> Publishing to PyPI ..."
uv publish --token "$PYPI_TOKEN"

# ---- 8. tag + push -----------------------------------------------------------
echo "==> Tagging and pushing ..."
git tag -a "v$VERSION" -m "Release $VERSION"
git push origin "$MAIN_BRANCH"
git push origin "v$VERSION"

echo "==> Done. $PKG_NAME $VERSION is live: https://pypi.org/project/$PKG_NAME/$VERSION/"
