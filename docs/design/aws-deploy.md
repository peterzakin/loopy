# Design: provision the Loopy engine on AWS from an operator's keys

Status: implemented (`loopy_cli/aws.py`, `loopy_cli/deploy/aws-stack.json`,
`loopy_cli/deploy/aws-userdata.sh`; tests in `tests/test_deploy_aws.py`) ·
Scope: `loopy_cli` (`deploy aws` command), `loopy_cli/deploy`, docs

## Problem

Loopy documents two ways to host the engine: the bundled `loopy run` Docker
stack on a VM you manage, and Render (one Web Service + managed Redis + a disk).
Both are hand-wired. We want a third path where an operator hands over AWS
credentials and nothing else, and Loopy stands up the hosting infrastructure
itself with one command and no manual step.

Three constraints frame the design:

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
3. **The operator brings no domain.** Webhooks need a valid cert, and a public CA
   will not issue one for a bare IP or an `amazonaws.com` name. Since we refuse to
   ask the operator for a domain, TLS has to come from an AWS endpoint that
   carries its own trusted name. That rules out terminating TLS on the instance
   (Caddy/Let's Encrypt would need a domain the operator controls) and points at
   CloudFront, whose distribution serves a managed cert on `*.cloudfront.net`.

## Decisions (the proposed design)

1. **CloudFormation, driven by boto3.** One template describes the stack; a new
   `loopy deploy aws` command calls `create_stack` / `update_stack` /
   `delete_stack` through boto3. The stack name (derived from the project, e.g.
   `loopy-engine-<project>`) is the idempotency key: a first run creates, a
   re-run updates, and a bad template rolls back on its own. Teardown is one
   `delete_stack`. This adds `boto3` as an optional extra (`loopy-computer[aws]`),
   asks nothing of the operator beyond AWS credentials, and needs no external
   binary (unlike Terraform or CDK).

2. **Single EC2 + the bundled compose stack, behind CloudFront for the cert.**
   The instance runs the same `redis` + `loopy` compose stack the CLI already
   ships, brought up by cloud-init user-data. An EBS data volume mounted at
   `/state` holds the SQLite DB and the Redis append-only file across a reboot.
   The instance does not terminate TLS: it serves plain HTTP on `:8000` and a
   CloudFront distribution in front is the public HTTPS endpoint, terminating TLS
   with an AWS-managed cert on its `*.cloudfront.net` name (constraint 3). This
   reuses shipped assets, keeps the single-writer VM shape, and avoids the moving
   parts (ALB, ElastiCache, EFS) that a managed topology would add for no benefit
   at one replica. CloudFront is configured to forward all HTTP methods and
   headers and cache nothing, so webhook POSTs and their signature headers reach
   the engine unaltered; the origin is the Elastic IP, reached over HTTP.

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
- **`AWS::EC2::SecurityGroup`** — inbound to the engine port (`8000`) only, and
  only from CloudFront's managed prefix list
  (`com.amazonaws.global.cloudfront.origin-facing`), so the Elastic IP is a
  locked-down origin rather than a second public door. All egress open, since the
  engine reaches the GitHub and Daytona APIs.
- **`AWS::CloudFront::Distribution`** — the public HTTPS endpoint. Default
  `*.cloudfront.net` viewer cert (no ACM cert, no domain); a custom origin
  pointing at the EIP's EC2 public DNS name over HTTP on the engine port
  (CloudFront origins take a DNS name, never a bare IP — every EIP has one, in
  EC2's fixed `ec2-<ip-dashes>.<zone>.amazonaws.com` scheme); the managed
  `CachingDisabled` cache policy and `AllViewer` origin-request policy, so POST
  webhooks and `X-Hub-Signature-256` pass through untouched.
- **`AWS::EC2::EIP` + `AWS::EC2::EIPAssociation`** — a stable origin address that
  survives a stop/start (the distribution's origin is pinned to its DNS name).
- **`AWS::EC2::Volume` + `AWS::EC2::VolumeAttachment`** — the `/state` data
  volume (default 8 GiB gp3). `DeletionPolicy` is `Delete` by default; the docs
  tell operators to snapshot before teardown if they want the history.
- **`AWS::IAM::Role` + `AWS::IAM::InstanceProfile`** — an instance role whose
  permissions are `ssm:GetParameter*` on this stack's parameter path
  (`/loopy/<stack>/*`), `kms:Decrypt` via the SSM service, `s3:GetObject` on the
  stack's project tarball, plus `AmazonSSMManagedInstanceCore` so the box is
  reachable through Session Manager (no SSH port exists).
- **Outputs** — the distribution's `*.cloudfront.net` domain (the public URL the
  CLI prints and webhooks target), the Elastic IP, and the instance id.

Two pieces live outside the template, both CLI-side, both deliberate:

- **Secrets are `put_parameter` calls, not template resources.** CloudFormation
  cannot create `SecureString` parameters, and routing secret values through the
  template body would put them in the CloudFormation console anyway. The CLI
  pushes each env file (`loopy.env`, every sandbox `env_file`) verbatim as one
  SecureString at `/loopy/<stack>/files/<relpath>` (Intelligent-Tiering, so a
  file past 4 KB — the GitHub App key case — still fits), and `--destroy` deletes
  the path before deleting the stack.
- **The project travels via a small deploy bucket.** User-data needs the project
  (manifest + sensors) before any stack resource can carry it, so the CLI ensures
  `loopy-deploy-<account>-<region>` (private, public access blocked), uploads the
  tarball (secret env files excluded — they ride SSM), and the instance role reads
  exactly that key. The bucket is kept on `--destroy`: it is a few kilobytes and
  reused by the next deploy.

**User-data responsibilities** (`loopy_cli/deploy/aws-userdata.sh`, rendered by
the CLI, run once by cloud-init): install Docker; wait for, format-if-blank, and
mount the `/state` volume; fetch the project tarball from S3; pull each env file
back from SSM to its project-relative path; poll SSM for the public URL (see
below) and append `LOOPY_PUBLIC_URL` to `loopy.env`; build the engine image from
the pinned PyPI release (the `Dockerfile.pypi` recipe, no source checkout); and
run the same collapsed topology as the bundled compose file with plain
`docker run` — a `redis:7-alpine` container (append-only, `/state/redis`) and the
engine (`--in-process --bus redis --state sqlite --state-path /state/state.db`,
project mounted read-only), both `--restart unless-stopped` so a reboot recovers
the stack. There is no TLS on the instance and no ACME client: CloudFront owns
the cert.

**Two passes, because CloudFront origins need a DNS name.** The origin is the
EIP's public DNS, which only exists once the address does — and a template
cannot string-transform an IP into that name. So every deploy applies the stack
twice with the same body: pass 1 with `OriginDomain=""` (a template condition
gates the distribution off), pass 2 with the computed name, which creates the
distribution on first deploy and is a no-op update after that (the distribution
and its URL are stable). The command then writes `https://<domain>` to the
`/loopy/<stack>/public-url` SSM parameter; a first boot that races the
distribution polls for it, and starts the engine regardless after a timeout —
the URL only gates printing delivery URLs, not serving.

## The `loopy deploy aws` command

- **Inputs:** `--region`, `--profile` (or the standard AWS env vars, resolved by
  boto3's default chain), `--stack` (default `loopy-engine` — the idempotency
  key), `--instance-type` (default `t3.small`), `--state-size-gb` (default 8),
  `--port` (default 8000), and the manifest/`--root` pair `loopy run` takes
  (compile-on-demand included). The secret set is read from the project's
  `loopy.env` and sandbox `env_file`s and written to SSM. No `--domain`: the
  operator brings none.
- **Create/update:** preflight (`manifest` under root, `loopy.env` present),
  push secrets, upload the tarball, then `create_stack` or `update_stack` with
  waiters; on failure the first failed-resource reasons from the event stream
  are surfaced (a failed first create rolls back and deletes itself).
- **After apply:** print the `https://<id>.cloudfront.net` URL, the origin IP,
  and the follow-ups (`loopy webhooks github`, `loopy admin --remote`). A fresh
  distribution takes a few minutes to deploy globally before the URL answers.
- **`--destroy`:** delete the `/loopy/<stack>/*` parameters, then `delete_stack`
  and wait, with a reminder that `/state` (run history) goes with it.

## IAM the operator's provisioning identity needs

The credentials passed to `loopy deploy aws` (a deploy user, not the instance
role) need: CloudFormation (`cloudformation:*Stack*`), EC2 (instance, security
group, EIP, volume, their describes, and `DescribeManagedPrefixLists` for the
CloudFront prefix-list lookup), CloudFront (create/update/delete the
distribution), IAM (create/pass the instance role and profile), SSM
(`ssm:PutParameter` / `GetParametersByPath` / `DeleteParameters` on
`/loopy/<stack>/*`), S3 on the deploy bucket (`CreateBucket` / `HeadBucket` /
`PutObject` on `loopy-deploy-<account>-<region>`), and `sts:GetCallerIdentity`.
Documenting this lets an operator scope a least-privilege deploy user rather
than using root keys.

## Reuse

- `loopy_cli/deploy/docker-compose.yml` — the exact stack the instance runs.
- `loopy_cli/deploy/Dockerfile.pypi` — the version-pinned engine image, for a
  host with no source checkout (the same case Render hits).
- `loopy_runtime/secrets.py` — the control-plane vs sandbox split the SSM
  parameter layout mirrors.
- The provider-agnostic serve contract (`$PORT`, `/healthz`, `LOOPY_PUBLIC_URL`,
  TLS terminated at the front door) in `loopy_runtime/config.py` and the
  dashboard: AWS is just another host that satisfies it, so nothing in the engine
  branches on the provider. Here the front door is CloudFront rather than a
  platform ingress, but the engine still speaks plain HTTP behind it and reads
  `LOOPY_PUBLIC_URL` as an env var, so no engine code changes. The dashboard auth
  model is unchanged; see [admin-auth.md](./admin-auth.md).

## Non-goals and future

- **No HA.** One SQLite writer, one instance. High availability waits on the
  networked `StateStore` and durable runtime already noted as future work.
- **Custom domain (optional).** An operator who wants a name of their own can
  attach it to the distribution later: add an ACM cert (in `us-east-1`, as
  CloudFront requires) and an alternate domain name, then a `CNAME` to the
  distribution. This is additive and never required to receive webhooks, so it
  stays out of the default path.
- **Upgrade path.** When the networked runtime lands, the same command can target
  a managed topology behind the identical seams: Fargate for the engine,
  ElastiCache for the bus (`REDIS_URL`), and RDS/Postgres for the `StateStore`,
  with an ALB behind the same CloudFront distribution. The template gains
  resources; the engine does not change.
- **No AWS sandbox provider.** Agents run in Daytona. This design does not touch
  `loopy_runtime/sandbox`.
