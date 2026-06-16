"""loopy-core — the Loopy frontend.

Parses a project (registry.yml, workflows/*.md, skills/, sensors/*.py), resolves
the DAG, statically checks every template reference, and emits the manifest.
Pure and offline: executes no workflow logic and no user code.
"""

__version__ = "0.1.0"
