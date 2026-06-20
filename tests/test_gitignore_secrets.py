"""Guard the secret-ignore rules in `.gitignore` against the inline-comment trap.

git has no inline comments: a `#` placed after a pattern on the same line becomes
part of the pattern, so a rule like `**/secrets/   # note` matches nothing and the
secrets it was meant to hide become one `git add .` away from a public commit. This
test asserts the known secret paths are actually ignored, so the rule can't silently
regress (e.g. someone "tidies up" by appending an inline comment).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Representative paths the docs/quickstart tell users to create. Each holds real
# credentials and MUST be ignored.
SECRET_PATHS = [
    "loopy.env",
    "examples/codefix/secrets/dev.env",
    "examples/incidents/secrets/default.env",
    "sensors/.env",
    ".loopy/key.pem",
    ".env",
]


def _is_ignored(path: str) -> bool:
    # `git check-ignore` exits 0 when the path is ignored, 1 when it is not.
    result = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=REPO_ROOT,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.parametrize("path", SECRET_PATHS)
def test_secret_path_is_gitignored(path: str) -> None:
    assert _is_ignored(path), (
        f"{path!r} is NOT ignored — a credential-leak path. Check `.gitignore` for an "
        "inline `#` comment (git has no inline comments; put comments on their own line)."
    )
