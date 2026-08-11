#!/bin/bash
# Internal deployment asset — rendered by `loopy deploy bootstrap` (every __TOKEN__ is replaced
# before submission) and stored at /opt/loopy/deploy.sh. Run on first boot by user-data and
# again on every re-deploy by SSM RunCommand: the CLI refreshes the project tarball (S3) and
# the secret parameters (SSM) first, then this re-fetches both and restarts the containers.
# Idempotent and safe to re-run; that is the whole point (in-place updates, no instance churn).
set -euo pipefail
exec > >(tee /var/log/loopy-deploy.log) 2>&1  # tee so SSM RunCommand also captures the output

REGION="__LOOPY_REGION__"
PARAM_PATH="__LOOPY_PARAM_PATH__"
PROJECT_S3_URI="__LOOPY_PROJECT_S3_URI__"
LOOPY_VERSION="__LOOPY_VERSION__"
MANIFEST_REL="__LOOPY_MANIFEST_REL__"
ENGINE_PORT="__LOOPY_ENGINE_PORT__"
ENGINE_IMAGE_TAG="__LOOPY_ENGINE_IMAGE_TAG__"
ENGINE_WHEEL_S3="__LOOPY_ENGINE_WHEEL_S3__"

mkdir -p /opt/loopy /state/redis

# ── The project: manifest + sensors, packaged by the CLI (secret env files excluded).
aws s3 cp "$PROJECT_S3_URI" /opt/loopy/project.tgz --region "$REGION"
rm -rf /opt/loopy/project
mkdir -p /opt/loopy/project
tar -xzf /opt/loopy/project.tgz -C /opt/loopy/project

# ── Secrets: each env file the CLI pushed to SSM lands back at its project-relative path.
# `--output text` on a single string value writes it verbatim (newlines intact).
fetch_secret_file() {
  mkdir -p "$(dirname "/opt/loopy/project/$1")"
  aws ssm get-parameter --with-decryption --region "$REGION" \
    --name "$PARAM_PATH/files/$1" --query Parameter.Value --output text \
    > "/opt/loopy/project/$1"
  chmod 600 "/opt/loopy/project/$1"
}
__LOOPY_FETCH_SECRET_FILES__

# ── LOOPY_PUBLIC_URL: the CloudFront domain, written to SSM by the CLI once the distribution
# exists. First boot can race it, so poll; on a re-deploy the parameter is already there and
# this returns at once. Start regardless after the timeout: the URL only gates printing
# delivery URLs, not serving.
PUBLIC_URL=""
for _ in $(seq 1 90); do
  PUBLIC_URL="$(aws ssm get-parameter --region "$REGION" --name "$PARAM_PATH/public-url" \
    --query Parameter.Value --output text 2>/dev/null || true)"
  [ -n "$PUBLIC_URL" ] && break
  sleep 10
done
if [ -n "$PUBLIC_URL" ] && ! grep -q '^LOOPY_PUBLIC_URL=' /opt/loopy/project/loopy.env 2>/dev/null; then
  echo "LOOPY_PUBLIC_URL=$PUBLIC_URL" >> /opt/loopy/project/loopy.env
fi

# ── The engine image. By default the pinned PyPI release (same recipe as the shipped
# Dockerfile.pypi, no source checkout on the instance). With a wheel shipped by
# `loopy deploy bootstrap --engine-source`, it installs that instead, so an unreleased CLI can run
# its own engine (the PyPI version string is frozen, so PyPI can't carry unreleased code).
#
# Whether a cached image can be reused turns on what the tag identifies. A source build's tag
# carries the wheel's content hash, so an existing `loopy-engine:<tag>` IS the wheel we want —
# reuse it, and a changed wheel is a new tag that rebuilds. A PyPI build's tag is just the frozen
# version string (`0.1.0` throughout pre-release dev), which does NOT change when that version is
# republished with new code — so a cached image can be older than the manifest we just compiled,
# which boots the engine into a schema-mismatch crash-loop. Always rebuild the PyPI image with
# --pull --no-cache so it re-fetches the current base image and wheel rather than serving a stale
# layer or image.
build_engine_image() {
  mkdir -p /opt/loopy/image
  rm -rf /opt/loopy/image/wheels
  if [ -n "$ENGINE_WHEEL_S3" ]; then
    # Keep the wheel's real filename: pip rejects any name that isn't a valid PEP 427 wheel
    # (`<dist>-<ver>-<pytag>-<abi>-<plat>.whl`). Copying into a dir preserves the basename; the
    # RUN globs the sole wheel and appends the [redis,tenki] extras at build time (quoted heredoc, so
    # $(...) survives to the image build rather than expanding here).
    mkdir -p /opt/loopy/image/wheels
    aws s3 cp "$ENGINE_WHEEL_S3" /opt/loopy/image/wheels/ --region "$REGION"
    cat > /opt/loopy/image/Dockerfile <<'DOCKERFILE'
FROM python:3.12-slim
COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir "$(ls /tmp/wheels/*.whl)[redis,tenki]"
WORKDIR /project
ENTRYPOINT ["loopy"]
DOCKERFILE
    docker build -t "loopy-engine:$ENGINE_IMAGE_TAG" /opt/loopy/image
  else
    cat > /opt/loopy/image/Dockerfile <<EOF
FROM python:3.12-slim
RUN pip install --no-cache-dir "loopy-computer[redis,tenki]==$LOOPY_VERSION"
WORKDIR /project
ENTRYPOINT ["loopy"]
EOF
    docker build --pull --no-cache -t "loopy-engine:$ENGINE_IMAGE_TAG" /opt/loopy/image
  fi
}

if [ -n "$ENGINE_WHEEL_S3" ] && docker image inspect "loopy-engine:$ENGINE_IMAGE_TAG" >/dev/null 2>&1; then
  echo "engine image loopy-engine:$ENGINE_IMAGE_TAG already built (content-hash tag); reusing"
else
  build_engine_image
fi

# ── The stack: same collapsed single-node topology as the bundled compose file (redis bus,
# sqlite state, project read-only). `docker rm -f` then re-run is the restart on a re-deploy;
# `--restart unless-stopped` carries both containers across instance reboots.
docker network create loopy 2>/dev/null || true
docker rm -f loopy-redis loopy-engine 2>/dev/null || true
docker run -d --name loopy-redis --network loopy --restart unless-stopped \
  -v /state/redis:/data \
  redis:7-alpine redis-server --appendonly yes
docker run -d --name loopy-engine --network loopy --restart unless-stopped \
  -p "$ENGINE_PORT:$ENGINE_PORT" \
  --env-file /opt/loopy/project/loopy.env \
  -e REDIS_URL=redis://loopy-redis:6379 \
  -v /opt/loopy/project:/project:ro \
  -v /state:/state \
  "loopy-engine:$ENGINE_IMAGE_TAG" \
  run "$MANIFEST_REL" --in-process --root /project \
  --host 0.0.0.0 --port "$ENGINE_PORT" \
  --bus redis --state sqlite --state-path /state/state.db

echo "loopy engine up on :$ENGINE_PORT (public URL: ${PUBLIC_URL:-pending})"
