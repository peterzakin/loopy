"""Tolerant JSON extraction (#11) + failure transcript surfacing (#10).

Agents wrap their final JSON in code fences or a trailing sentence; `_extract_json_object`
recovers the object instead of failing the whole run, and parse/exit failures now carry the
offending output so a mismatch is debuggable.
"""

from __future__ import annotations

from loopy_runtime.harness.base import (
    _balanced_json_objects,
    _extract_json_object,
    _tail,
)


def test_extracts_plain_object():
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_strips_json_code_fence():
    assert _extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json_object("```\n{\"a\": 1}\n```") == {"a": 1}


def test_recovers_object_after_prose():
    text = 'Sure! Here is the result:\n{"output": {"pr": "url"}}\nHope that helps.'
    assert _extract_json_object(text) == {"output": {"pr": "url"}}


def test_takes_last_balanced_object():
    # An LLM that "thinks out loud" with an example object then emits the real one.
    text = 'example: {"output": {}}\nfinal: {"output": {"goal": "ship"}}'
    assert _extract_json_object(text) == {"output": {"goal": "ship"}}


def test_braces_inside_strings_dont_break_balance():
    assert _extract_json_object('{"msg": "a } b { c"}') == {"msg": "a } b { c"}


def test_non_json_returns_none():
    assert _extract_json_object("I could not complete the task.") is None
    assert _extract_json_object("") is None


def test_array_is_not_an_object():
    assert _extract_json_object("[1, 2, 3]") is None


def test_balanced_objects_finds_top_level_spans():
    assert _balanced_json_objects('a {"x": {"y": 1}} b {"z": 2}') == ['{"x": {"y": 1}}', '{"z": 2}']


def test_tail_truncates_from_the_end():
    assert _tail("abcdef", limit=3) == "…def"
    assert _tail("  hi  ", limit=10) == "hi"
