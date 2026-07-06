"""`loopy deploy render` — the Render.com deploy target."""

from __future__ import annotations

import pytest

# ── secret-file name encoding ───────────────────────────────────────────────────


def test_encode_secret_file_name_flattens_paths():
    from loopy_cli.render import encode_secret_file_name

    assert encode_secret_file_name("loopy.env") == "loopy.env"
    assert encode_secret_file_name("secrets/base.env") == "secrets__base.env"
    assert encode_secret_file_name("sensors/.env") == "sensors__.env"
    assert encode_secret_file_name("a/b/c.env") == "a__b__c.env"


def test_encode_secret_file_name_rejects_ambiguous_paths():
    from loopy_cli.render import encode_secret_file_name

    with pytest.raises(ValueError, match="__"):
        encode_secret_file_name("secrets/my__file.env")
