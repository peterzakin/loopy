"""make_state_store: backend dispatch + SQLite path resolution under --root."""

from __future__ import annotations

import pytest

from loopy_runtime.state.factory import VALID_STATE, make_state_store
from loopy_runtime.state.inmemory import InMemoryStateStore
from loopy_runtime.state.sqlite import SqliteStateStore


def test_inproc_backend_is_in_memory():
    assert isinstance(make_state_store("inproc"), InMemoryStateStore)


def test_sqlite_relative_path_resolves_under_root_and_creates_parent(tmp_path):
    store = make_state_store("sqlite", ".loopy/state.db", root=tmp_path)
    assert isinstance(store, SqliteStateStore)
    assert store.path == tmp_path / ".loopy" / "state.db"
    assert store.path.parent.is_dir()  # .loopy/ created
    store.close()


def test_sqlite_absolute_path_kept(tmp_path):
    target = tmp_path / "abs.db"
    store = make_state_store("sqlite", target, root=tmp_path / "ignored")
    assert store.path == target
    store.close()


def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="unknown state store"):
        make_state_store("postgres")


def test_valid_state_names():
    assert VALID_STATE == ("inproc", "sqlite")
