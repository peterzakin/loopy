#!/bin/bash
# Internal deployment asset — rendered by `loopy deploy aws` and run once by cloud-init on
# the engine instance's first boot. It does the one-time host setup (Docker, the /state
# volume) that a re-deploy must not repeat, then installs the re-runnable deploy script
# (`aws-deploy.sh`, embedded base64 below) and runs it. Every subsequent deploy re-runs that
# script in place via SSM RunCommand — never this file again.
set -euo pipefail
exec > /var/log/loopy-userdata.log 2>&1

dnf install -y docker
systemctl enable --now docker

# ── /state: the durable half (sqlite run history + redis AOF) on the attached EBS volume.
# On nitro instances the /dev/sdf attachment surfaces as an nvme device; the data volume is
# the one that is not the root disk. Wait for the attachment, format only a blank disk.
ROOT_DISK="$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" | head -n1 || true)"
STATE_DEV=""
for _ in $(seq 1 60); do
  for dev in /dev/nvme1n1 /dev/nvme2n1 /dev/sdf /dev/xvdf; do
    name="$(basename "$dev")"
    if [ -b "$dev" ] && [ "$name" != "$ROOT_DISK" ]; then
      STATE_DEV="$dev"
      break
    fi
  done
  [ -n "$STATE_DEV" ] && break
  sleep 2
done
[ -n "$STATE_DEV" ] || { echo "state volume never attached" >&2; exit 1; }
blkid "$STATE_DEV" >/dev/null || mkfs -t xfs "$STATE_DEV"
mkdir -p /state
grep -q " /state " /etc/fstab || echo "$STATE_DEV /state xfs defaults,nofail 0 2" >> /etc/fstab
mountpoint -q /state || mount /state

# ── Install the re-runnable deploy script and run it. Re-deploys invoke this same file at
# /opt/loopy/deploy.sh via SSM RunCommand, so first boot and update share one code path.
mkdir -p /opt/loopy
echo "__LOOPY_DEPLOY_SH_B64__" | base64 -d > /opt/loopy/deploy.sh
chmod +x /opt/loopy/deploy.sh
bash /opt/loopy/deploy.sh
