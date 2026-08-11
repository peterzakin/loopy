# Internal deployment asset — built by `loopy run --docker`, not by hand. The build
# context is the loopy source root, so the COPY paths below are root-relative.
#
# The engine image: the single long-lived process `loopy run` drives — sensor webhooks
# + scheduler + event-bus consumer + runtime, all in one. Agents do NOT run here; they
# run in Daytona sandboxes (a separate domain).
FROM python:3.12-slim

# Declared so the shared compose build arg is accepted here too; the source build ignores it —
# it installs from the COPYed tree below, not from PyPI (see Dockerfile.pypi for the pip path).
ARG LOOPY_VERSION

# uv for a fast, cached dependency resolve (the repo's install tool).
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy only what the wheel needs (see .dockerignore for what's excluded), so a
# source-only change doesn't bust the dependency layer.
COPY pyproject.toml uv.lock README.md ./
COPY loopy_core ./loopy_core
COPY loopy_runtime ./loopy_runtime
COPY loopy_cli ./loopy_cli

# [redis] for `--bus redis`, [tenki] for `provider: tenki` sandboxes; Daytona (the default
# sandbox) ships in the core deps.
RUN uv pip install --system --no-cache ".[redis,tenki]"

# Commands run against the bind-mounted project (manifest + sensors + env files).
WORKDIR /project

ENTRYPOINT ["loopy"]
