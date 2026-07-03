# Design: provision the Loopy engine on AWS from an operator's keys

Status: proposed · Scope: `loopy_cli` (new `deploy aws` command), `loopy_cli/deploy`, docs

## Problem

Loopy documents two ways to host the engine: the bundled `loopy run` Docker
stack on a VM you manage, and Render (one Web Service + managed Redis + a disk).
Both are hand-wired. We want a third path where an operator hands over AWS
credentials and Loopy stands up the hosting infrastructure itself, with as few
manual steps as DNS forces.

Two constraints frame the design:

1. **Agents stay in Daytona.** AWS hosts only the engine (the small `loopy run`
   process: sensor webhooks, scheduler, event bus, runtime, `/admin` dashboard).
   The sandbox side is untouched, so `DAYTONA_API_KEY` is passed to the engine as
   an ordinary control-plane cred, exactly as on a VM or Render. We are not
   building an AWS sandbox provider.
2. **The engine is a single writer.** Run history is SQLite behind the
   `StateStore` seam (`loopy_runtime/state/sqlite.py`), and the bundled compose
   stack (`loopy_cli/deploy/docker-compose.yml`) is explicitly "single replica."
   So the target is one instance with a durable disk, not a fleet. This is the
   same shape Render's docs pin to one Web Service.

## Decisions (the proposed design)

1. **CloudFormation, driven by boto3.** One template describes the stack; a new
   `loopy deploy aws` command calls `create_stack` / `update_stack` /
   `delete_stack` through boto3. The stack name (derived from the project, e.g.
   `loopy-engine-<project>`) is the idempotency key: a first run creates, a
   re-run updates, and a bad template rolls back on its own. Teardown is one
   `delete_stack`. This adds `boto3` as an optional extra (`loopy-computer[aws]`),
   asks nothing of the operator beyond AWS credentials, and needs no external
   binary (unlike Terraform or CDK).

2. **Single EC2 + the bundled compose stack + Caddy, not Fargate/ALB.** The
   instance runs the same `redis` + `loopy` compose stack the CLI already ships,
   brought up by cloud-init user-data. An EBS data volume mounted at `/state`
   holds the SQLite DB and the Redis append-only file across a reboot. A Caddy
   container in front terminates TLS with an automatic Let's Encrypt cert and
   proxies `:443 -> :8000`, because AWS gives you no public cert for a raw
   instance and GitHub/Sentry require HTTPS. This reuses shipped assets, is the
   cheapest shape that satisfies the single-writer constraint, and avoids the
   moving parts (ALB, ACM, ElastiCache, EFS) that a managed topology would add
   for no benefit at one replica.

3. **Secrets in SSM Parameter Store, read via an instance role.** The
   control-plane vs sandbox split the runtime already enforces
   (`loopy_runtime/secrets.py`) carries over unchanged. Engine creds go into SSM
   SecureStrings and are rendered into `loopy.env` on the instance at boot; the
   agents' model key (`ANTHROPIC_API_KEY`) goes into a SecureString and is
   written to the sandbox `env_file` path (e.g. `secrets/base.env`) on the
   instance, where the engine resolves it at runtime to inject into the Daytona
   sandbox. No secret is ever baked into the template, the AMI, or the compiled
   manifest.

## CloudFormation template inventory

The stack, in one region, using the account's default VPC and one public subnet:

- **`AWS::EC2::Instance`** — the engine host (default `t3.small`, overridable),
  a recent Amazon Linux or Ubuntu AMI, with the user-data below and an
  `IamInstanceProfile`.
- **`AWS::EC2::SecurityGroup`** — inbound `80` and `443` only (80 is for the
  ACME HTTP-01 challenge and a redirect to 443); all egress open, since the
  engine reaches the GitHub and Daytona APIs and Caddy reaches Let's Encrypt.
- **`AWS::EC2::EIP` + `AWS::EC2::EIPAssociation`** — a stable public address so
  the DNS record an operator sets survives a stop/start.
- **`AWS::EC2::Volume` + `AWS::EC2::VolumeAttachment`** — the `/state` data
  volume (default 8 GiB gp3). `DeletionPolicy` is `Delete` by default; the docs
  tell operators to snapshot before teardown if they want the history.
- **`AWS::IAM::Role` + `AWS::IAM::InstanceProfile`** — an instance role whose
  only permission is `ssm:GetParameter*` on this stack's parameter path
  (`/loopy/<stack>/*`) plus `kms:Decrypt` for the SecureString key.
- **`AWS::SSM::Parameter` (SecureString) x N** — one per secret listed below.
- **Outputs** — the Elastic IP (so the CLI can print the `A` record to set) and
  the resolved public URL.

**User-data responsibilities** (cloud-init, idempotent): install Docker and the
compose plugin; mount the `/state` volume; pull the SSM parameters and write
`loopy.env` and the sandbox `env_file`; fetch the project and `manifest.json`
(from S3 or a git ref supplied by the CLI); bring up the compose stack pinned to
this Loopy version (reusing `Dockerfile.pypi`); and run Caddy as the TLS front
door for `--domain`, proxying to the engine on `:8000`.

## The `loopy deploy aws` command (shape)

- **Inputs:** `--region`, `--profile` (or the standard AWS env vars, resolved by
  boto3's default chain), `--domain`, `--instance-type` (default `t3.small`),
  `--state-size-gb` (default 8), and the secret set (read from the project's
  `loopy.env` and sandbox `env_file`, or prompted, then written to SSM).
- **Create/update:** package the project, put the secrets to SSM, then
  `create_stack` (or `update_stack` if the stack exists), waiting on the stack
  event stream and surfacing failures.
- **After apply:** print the Elastic IP and the exact `A` record to create, then
  the public URL to set as `LOOPY_PUBLIC_URL` and register webhooks against.
- **`--destroy`:** `delete_stack`, with a reminder to snapshot `/state` first.

## IAM the operator's provisioning identity needs

The credentials passed to `loopy deploy aws` (a deploy user, not the instance
role) need: CloudFormation (`cloudformation:*Stack*`), EC2 (instance, security
group, EIP, volume, and their describes), IAM (create/pass the instance role and
profile), and SSM (`ssm:PutParameter` / `DeleteParameter` on `/loopy/<stack>/*`).
Documenting this lets an operator scope a least-privilege deploy user rather than
using root keys.

## Reuse

- `loopy_cli/deploy/docker-compose.yml` — the exact stack the instance runs.
- `loopy_cli/deploy/Dockerfile.pypi` — the version-pinned engine image, for a
  host with no source checkout (the same case Render hits).
- `loopy_runtime/secrets.py` — the control-plane vs sandbox split the SSM
  parameter layout mirrors.
- The provider-agnostic serve contract (`$PORT`, `/healthz`, `LOOPY_PUBLIC_URL`,
  TLS terminated at the front door) in `loopy_runtime/config.py` and the
  dashboard: AWS is just another host that satisfies it, so nothing in the engine
  branches on the provider. The dashboard auth model is unchanged; see
  [admin-auth.md](./admin-auth.md).

## Non-goals and future

- **No HA.** One SQLite writer, one instance. High availability waits on the
  networked `StateStore` and durable runtime already noted as future work.
- **Upgrade path.** When those land, the same command can target a managed
  topology behind the identical seams: Fargate for the engine, ElastiCache for
  the bus (`REDIS_URL`), and RDS/Postgres for the `StateStore`, with an ALB and
  an ACM cert replacing Caddy. The template gains resources; the engine does not
  change.
- **No AWS sandbox provider.** Agents run in Daytona. This design does not touch
  `loopy_runtime/sandbox`.
