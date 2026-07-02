"""loopy-runtime — the backend half of Loopy.

Consumes `(manifest, triggering event)` and runs it. Never reads `.md` files; the
manifest is the complete IR. v1 is a non-durable, single-process, in-memory engine;
the interfaces in `contract.py` are frozen so durable and networked variants drop in
behind them later.
"""

__version__ = "0.1.0"
