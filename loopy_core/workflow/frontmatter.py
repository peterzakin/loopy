"""P3 — split each workflow `.md` into YAML frontmatter + prose body.

Parses the frontmatter with ruamel.yaml so keys keep line numbers, and tracks the
body's start line so template `Ref` spans (M3) are accurate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

_yaml = YAML()

# A frontmatter key at ruamel 0-based line `r` sits at file line `r + _META_OFFSET`:
# line 1 is the opening `---`, so the first YAML line is file line 2.
_META_OFFSET = 2


@dataclass
class ParsedDoc:
    meta: Mapping
    body: str
    body_start_line: int  # 1-based file line of the first body line

    def abs_line(self, node: object, key: object) -> int:
        """1-based file line of `key` within a ruamel CommentedMap node, or 0."""
        try:
            return node.lc.data[key][0] + _META_OFFSET  # type: ignore[attr-defined]
        except (AttributeError, KeyError, TypeError):
            return 0

    def line_of(self, key: object) -> int:
        return self.abs_line(self.meta, key)


def parse_document(text: str) -> ParsedDoc | None:
    """Return the parsed doc, or None if the frontmatter is missing/unparseable
    (the caller raises E101)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        return None

    yaml_block = "\n".join(lines[1:close])
    body = "\n".join(lines[close + 1 :])
    body_start_line = close + 2

    try:
        meta = _yaml.load(yaml_block) if yaml_block.strip() else {}
    except YAMLError:
        return None
    if meta is None:
        meta = {}
    if not isinstance(meta, Mapping):
        return None

    return ParsedDoc(meta=meta, body=body, body_start_line=body_start_line)
