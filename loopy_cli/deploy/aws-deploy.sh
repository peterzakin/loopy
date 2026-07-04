#!/bin/bash
# Internal deployment asset — rendered by `loopy deploy aws` (every __TOKEN__ is replaced
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

# ── The engine image: the pinned PyPI release, same recipe as the shipped Dockerfile.pypi
# (no source checkout on the instance). Built once per version, then reused across re-deploys.
if ! docker image inspect "loopy-engine:$LOOPY_VERSION" >/dev/null 2>&1; then
  mkdir -p /opt/loopy/image
  cat > /opt/loopy/image/Dockerfile <<EOF
FROM python:3.11-slim
RUN pip install --no-cache-dir "loopy-computer[redis]==$LOOPY_VERSION"
WORKDIR /project
ENTRYPOINT ["loopy"]
EOF
  docker build -t "loopy-engine:$LOOPY_VERSION" /opt/loopy/image
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
  "loopy-engine:$LOOPY_VERSION" \
  run "$MANIFEST_REL" --in-process --root /project \
  --host 0.0.0.0 --port "$ENGINE_PORT" \
  --bus redis --state sqlite --state-path /state/state.db

echo "loopy engine up on :$ENGINE_PORT (public URL: ${PUBLIC_URL:-pending})"
