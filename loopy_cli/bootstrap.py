"""`loopy deploy` — provision hosting for the engine from an operator's cloud keys.

Each subcommand is one deploy target (see `loopy_cli.deploy_target`). `loopy deploy
bootstrap` is the loopy-provisioned starter stack — named for what it is (the
batteries-included bootstrap), not where it runs, so future custom targets can claim
provider names like `aws` without colliding with it.

The bootstrap target stands up the design in `docs/design/aws-deploy.md`: one
CloudFormation stack holding an EC2 instance (the bundled redis+loopy stack via
user-data), an Elastic IP, an EBS `/state` volume, and a CloudFront distribution
that terminates TLS with a managed cert on its own `*.cloudfront.net` name — so the
operator brings AWS credentials and nothing else (no domain, no DNS). Agents still
run in Daytona; AWS hosts only the engine.

Two passes per deploy, because CloudFront origins need a DNS name and the EIP's
public DNS is only known once the address exists: pass 1 creates/updates the stack
with everything but the distribution, pass 2 fills the `OriginDomain` parameter.
Both passes are the same idempotent create-or-update, so re-running is safe and the
distribution (and its URL) is stable across updates.

Secrets never ride the template: each env file (`loopy.env`, the sandboxes'
`env_file`s) is pushed as an SSM SecureString under `/loopy/<stack>/files/<relpath>`
and pulled back to the same project-relative path by user-data on the instance —
CLI-side `put_parameter`, because CloudFormation cannot create SecureStrings. The
project itself (minus those files) travels as a tarball via a small deploy bucket.

boto3 is a core dependency (like the Daytona SDK), imported lazily inside the
command body so `loopy compile` and the rest of the CLI never load it.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
import time
from pathlib import Path

import typer

deploy_app = typer.Typer(
    no_args_is_help=True,
    help="Provision hosting for the engine on a named deploy target (`loopy deploy bootstrap`).",
)

_DEPLOY_DIR = Path(__file__).resolve().parent / "deploy"
TEMPLATE_PATH = _DEPLOY_DIR / "aws-stack.json"
USER_DATA_PATH = _DEPLOY_DIR / "aws-userdata.sh"
DEPLOY_SCRIPT_PATH = _DEPLOY_DIR / "aws-deploy.sh"

# Where user-data installs the re-runnable deploy script; SSM RunCommand re-runs it on update.
INSTANCE_DEPLOY_SCRIPT = "/opt/loopy/deploy.sh"

# The marker in the template's UserData the CLI swaps for the rendered boot script.
USER_DATA_MARKER = "__LOOPY_USER_DATA__"

# CloudFront's origin-facing address ranges, as the region's managed prefix list. The
# instance security group admits only these, so the EIP can't be hit around the CDN.
CLOUDFRONT_PREFIX_LIST_NAME = "com.amazonaws.global.cloudfront.origin-facing"

# Never packaged into the project tarball: VCS/state dirs, and every secret env file
# (those travel as SSM SecureStrings instead — see module docstring).
TARBALL_EXCLUDE_DIRS = {".git", ".loopy", "__pycache__"}


def _require_boto3():
    """Import boto3 lazily (a core dep, so only a broken install fails here)."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - core dep; only a broken install hits
        raise RuntimeError(
            "boto3 failed to import; it ships as a core dependency of loopy-computer — "
            "reinstall with `pip install loopy-computer`"
        ) from exc
    return boto3


def eip_public_dns(public_ip: str, region: str) -> str:
    """The EC2 public DNS name for an Elastic IP — the CloudFront origin.

    CloudFront custom origins take a DNS name, never a bare IP. Every EIP already has
    one, following EC2's fixed scheme (us-east-1 keeps its legacy `compute-1` zone).
    """
    dashed = public_ip.replace(".", "-")
    zone = "compute-1" if region == "us-east-1" else f"{region}.compute"
    return f"ec2-{dashed}.{zone}.amazonaws.com"


def stack_param_path(stack: str) -> str:
    """The SSM namespace one stack's secrets live under (matches the template's IAM scope)."""
    return f"/loopy/{stack}"


def collect_secret_files(root: Path, manifest_path: Path) -> list[str]:
    """Project-relative env-file paths to push to SSM (and exclude from the tarball).

    `loopy.env` (control-plane creds) plus every sandbox `env_file` in the manifest —
    the same two surfaces `loopy_runtime.secrets` resolves at run time. Order is
    stable; missing files are skipped (presence is preflighted separately).
    """
    from loopy_runtime.manifest_model import load_manifest
    from loopy_runtime.secrets import CONTROL_PLANE_ENV_FILE, SENSOR_ENV_FILE

    rels: list[str] = []
    for rel in (CONTROL_PLANE_ENV_FILE, SENSOR_ENV_FILE):
        if (root / rel).is_file():
            rels.append(rel)
    manifest = load_manifest(manifest_path)
    for spec in manifest.registry.sandboxes.values():
        for rel in spec.env_file:
            if rel not in rels and (root / rel).is_file():
                rels.append(rel)
    return rels


def render_deploy_script(
    *,
    region: str,
    stack: str,
    project_s3_uri: str,
    loopy_version: str,
    manifest_rel: str,
    engine_port: int,
    secret_files: list[str],
    engine_image_tag: str,
    engine_wheel_s3: str = "",
) -> str:
    """Fill the re-runnable deploy script's tokens (fetch project+secrets, restart the stack).

    This is the code path shared by first boot and every re-deploy, so its inputs are all
    stable per stack: it re-reads whatever the CLI last pushed to S3/SSM. Pure string work,
    so tests cover it offline.

    `engine_image_tag` tags the built image (the version for a PyPI build; the version plus the
    wheel's content hash for a source build, so a changed wheel rebuilds). `engine_wheel_s3` is
    the S3 URI of a shipped engine wheel (`--engine-source`), or empty to install from PyPI.
    """
    fetch_lines = "\n".join(f'fetch_secret_file "{rel}"' for rel in secret_files)
    script = DEPLOY_SCRIPT_PATH.read_text()
    replacements = {
        "__LOOPY_REGION__": region,
        "__LOOPY_PARAM_PATH__": stack_param_path(stack),
        "__LOOPY_PROJECT_S3_URI__": project_s3_uri,
        "__LOOPY_VERSION__": loopy_version,
        "__LOOPY_MANIFEST_REL__": manifest_rel,
        "__LOOPY_ENGINE_PORT__": str(engine_port),
        "__LOOPY_ENGINE_IMAGE_TAG__": engine_image_tag,
        "__LOOPY_ENGINE_WHEEL_S3__": engine_wheel_s3,
        "__LOOPY_FETCH_SECRET_FILES__": fetch_lines,
    }
    for token, value in replacements.items():
        script = script.replace(token, value)
    if "__LOOPY_" in script:
        raise ValueError("deploy-script render left an unfilled __LOOPY_ token")
    return script


def build_engine_wheel(source: Path, out_dir: Path) -> Path:
    """Build a wheel of loopy-computer from `source` (a loopy checkout) into `out_dir`.

    Backs `deploy bootstrap --engine-source`: the instance installs the operator's exact code rather
    than the pinned PyPI release, the only way to run an *unreleased* build on AWS (the version
    string is frozen, so PyPI can't carry it). Shells out to `uv build`, the repo's build tool.
    """
    import shutil
    import subprocess

    if not (source / "pyproject.toml").is_file():
        raise RuntimeError(
            f"--engine-source {source} is not a loopy checkout (no pyproject.toml); point it "
            "at your local loopy repo."
        )
    if shutil.which("uv") is None:
        raise RuntimeError("--engine-source needs `uv` on PATH to build the engine wheel")
    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["uv", "build", "--wheel", str(source), "--out-dir", str(out_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"building the engine wheel failed:\n{result.stderr.strip()[:1000]}")
    wheels = sorted(out_dir.glob("*.whl"))
    if not wheels:
        raise RuntimeError("`uv build` reported success but produced no wheel")
    return wheels[-1]


def render_user_data(deploy_script: str) -> str:
    """First-boot user-data: one-time host setup, then the deploy script embedded as base64.

    The deploy script rides base64 so its quoting can't collide with cloud-init's parsing;
    first boot decodes it to /opt/loopy/deploy.sh and runs it, and re-deploys re-run that same
    file via SSM (never this user-data again).
    """
    encoded = base64.b64encode(deploy_script.encode()).decode()
    script = USER_DATA_PATH.read_text().replace("__LOOPY_DEPLOY_SH_B64__", encoded)
    if "__LOOPY_" in script:
        raise ValueError("user-data render left an unfilled __LOOPY_ token")
    return script


def build_template_body(user_data: str) -> str:
    """The template asset with the rendered boot script injected as the UserData."""
    template = json.loads(TEMPLATE_PATH.read_text())
    props = template["Resources"]["EngineInstance"]["Properties"]
    if props["UserData"] != {"Fn::Base64": USER_DATA_MARKER}:
        raise ValueError("aws-stack.json UserData marker moved; update loopy_cli.aws")
    props["UserData"] = {"Fn::Base64": user_data}
    return json.dumps(template)


def package_project(root: Path, exclude_rels: list[str]) -> bytes:
    """Gzip'd tar of the project (manifest + sensors), minus VCS dirs and secret files."""
    excluded = {Path(rel).as_posix() for rel in exclude_rels}

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        if any(part in TARBALL_EXCLUDE_DIRS for part in parts):
            return None
        if Path(info.name).as_posix() in excluded:
            return None
        return info

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for child in sorted(root.iterdir()):
            tar.add(child, arcname=child.name, filter=keep)
    return buffer.getvalue()


# ── boto3-touching helpers (each takes clients, so tests can inject fakes) ──────────


def _require_default_vpc(ec2) -> None:
    """Fail fast if the region has no default VPC.

    The template places the instance and its security group with no explicit subnet or
    VpcId, so they land in the default VPC's public subnets (a fresh account has one in
    every region). An account that deleted its default VPC would otherwise fail deep in a
    create-stack rollback with an opaque EC2 error; this turns that into one clear message
    before anything is provisioned.
    """
    response = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    if not response.get("Vpcs"):
        raise RuntimeError(
            "no default VPC in this region. The bootstrap target uses the default VPC's public "
            "subnets; recreate one (`aws ec2 create-default-vpc`) or pick a region that has "
            "it. A custom-VPC mode is not built yet."
        )


def _cloudfront_prefix_list_id(ec2) -> str:
    response = ec2.describe_managed_prefix_lists(
        Filters=[{"Name": "prefix-list-name", "Values": [CLOUDFRONT_PREFIX_LIST_NAME]}]
    )
    lists = response.get("PrefixLists", [])
    if not lists:
        raise RuntimeError(
            f"the {CLOUDFRONT_PREFIX_LIST_NAME} managed prefix list was not found in this "
            "region; is CloudFront available here?"
        )
    return lists[0]["PrefixListId"]


def _ensure_deploy_bucket(s3, bucket: str, region: str) -> None:
    """Create the small per-account deploy bucket if it doesn't exist (idempotent)."""
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except Exception:  # noqa: BLE001 - botocore raises ClientError; absent → create below
        pass
    create: dict = {"Bucket": bucket}
    if region != "us-east-1":  # us-east-1 rejects an explicit LocationConstraint
        create["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**create)
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )


def _put_secret_files(ssm, stack: str, root: Path, rels: list[str]) -> None:
    """Each env file becomes one SecureString at /loopy/<stack>/files/<relpath>.

    CLI-side put_parameter (not template resources): CloudFormation cannot create
    SecureStrings, and this keeps secret values out of the template body entirely.
    Intelligent-Tiering absorbs a file that crosses the 4 KB standard-tier line
    (the GitHub App private key is the realistic case).
    """
    base = stack_param_path(stack)
    for rel in rels:
        ssm.put_parameter(
            Name=f"{base}/files/{Path(rel).as_posix()}",
            Value=(root / rel).read_text(),
            Type="SecureString",
            Tier="Intelligent-Tiering",
            Overwrite=True,
        )


# Terminal states a stack can be stuck in that block a fresh deploy: a create that rolled
# back but wasn't cleaned up, or a failed rollback/delete. None can be updated — CloudFormation
# only allows deleting them — and a rolled-back stack has no EIP output, so the update path
# would `KeyError` on `PublicIp`. The deploy deletes such a stack and recreates instead.
DEAD_STACK_STATUSES = frozenset(
    {
        "ROLLBACK_COMPLETE",
        "ROLLBACK_FAILED",
        "CREATE_FAILED",
        "DELETE_FAILED",
        "UPDATE_ROLLBACK_FAILED",
    }
)


def _stack_status(cf, stack: str) -> str | None:
    """The stack's `StackStatus`, or None if it doesn't exist."""
    try:
        response = cf.describe_stacks(StackName=stack)
    except Exception:  # noqa: BLE001 - ClientError ValidationError == "does not exist"
        return None
    return response["Stacks"][0]["StackStatus"]


def _stack_exists(cf, stack: str) -> bool:
    status = _stack_status(cf, stack)
    if status is None or status == "REVIEW_IN_PROGRESS":  # absent, or a never-executed change set
        return False
    return True


def _clear_unusable_stack(cf, stack: str, *, echo=None) -> None:
    """Delete a stack stuck in a terminal failed state (or wait out a running delete).

    CloudFormation only allows deleting a rolled-back/failed stack, never updating it, and a
    rolled-back stack has no outputs — so the deploy's update path would `KeyError` on the EIP.
    Clearing it here makes the subsequent apply a clean create. A healthy or absent stack is
    left untouched; only the failure states in `DEAD_STACK_STATUSES` (plus an in-flight delete)
    are acted on.
    """
    echo = echo or typer.echo
    status = _stack_status(cf, stack)
    if status in DEAD_STACK_STATUSES:
        echo(
            f"deploy: existing stack {stack} is in {status} (a prior deploy failed); "
            "deleting it so this is a clean create…"
        )
        cf.delete_stack(StackName=stack)
    elif status == "DELETE_IN_PROGRESS":
        echo(f"deploy: stack {stack} is finishing an earlier delete; waiting for it…")
    else:
        return
    cf.get_waiter("stack_delete_complete").wait(
        StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 120}
    )


def _apply_stack(cf, stack: str, template_body: str, parameters: dict[str, str]) -> None:
    """create_stack or update_stack, then wait; a no-op update is success, not an error."""
    params = [{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()]
    # The failed-resource lookup queries by this. On a failed *create* the stack rolls back
    # and (OnFailure=DELETE) is gone, so its name no longer resolves — but the immutable stack
    # id (ARN) that create_stack returns keeps its events queryable, so the reason survives.
    stack_ref = stack
    if _stack_exists(cf, stack):
        try:
            cf.update_stack(
                StackName=stack,
                TemplateBody=template_body,
                Parameters=params,
                Capabilities=["CAPABILITY_IAM"],
            )
        except Exception as exc:  # noqa: BLE001 - botocore ClientError
            if "No updates are to be performed" in str(exc):
                return
            raise
        waiter = cf.get_waiter("stack_update_complete")
    else:
        response = cf.create_stack(
            StackName=stack,
            TemplateBody=template_body,
            Parameters=params,
            Capabilities=["CAPABILITY_IAM"],
            OnFailure="DELETE",
        )
        stack_ref = (response or {}).get("StackId") or stack
        waiter = cf.get_waiter("stack_create_complete")
    try:
        waiter.wait(StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 120})
    except Exception as exc:
        reasons = _failure_reasons(cf, stack_ref)
        raise RuntimeError(f"stack {stack} did not stabilize: {reasons}") from exc


def _failure_reasons(cf, stack_ref: str) -> str:
    """The first few failed-resource reasons — the part of the event stream worth reading.

    `stack_ref` should be the stack id (ARN) when we have it: a create that failed with
    `OnFailure=DELETE` no longer resolves by name, but its events stay queryable by id, so
    that is what surfaces the real cause (a bad AMI, an unavailable instance type, a hit
    service limit) instead of a bare "stack does not exist".
    """
    try:
        events = cf.describe_stack_events(StackName=stack_ref)["StackEvents"]
    except Exception:  # noqa: BLE001 - even the id can age out of CloudFormation's history
        return (
            "no stack events available. The failed create rolled back and deleted; open the "
            "CloudFormation console (toggle 'Deleted' stacks) to read the failed resource's reason"
        )
    reasons = [
        f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', e['ResourceStatus'])}"
        for e in events
        if e["ResourceStatus"].endswith("_FAILED")
    ]
    return "; ".join(reasons[:5]) or "see the CloudFormation console for events"


def _stack_outputs(cf, stack: str) -> dict[str, str]:
    described = cf.describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}


def _refresh_instance(ssm, instance_id: str, deploy_script: str, *, sleep=time.sleep) -> None:
    """Rewrite /opt/loopy/deploy.sh from the freshly rendered script, run it, and wait.

    This is the in-place update: the CLI has already pushed the new tarball (S3) and secrets
    (SSM), so re-running the deploy script re-fetches both and restarts the containers — no
    instance replacement, ~seconds of downtime. The script itself is re-pushed first (base64,
    same encoding as user-data) — the instance's stored copy is whatever its *first boot*
    rendered, so a CLI upgrade that fixes the script must not keep re-running the old one.
    Only called for an already-running stack, where the instance has had time to register with
    SSM; a fresh boot updates via user-data instead.

    On failure the error carries the tail of the script's *stdout*: the script tees everything
    (stderr included) into stdout via its log redirect, so that is where the real reason lives —
    SSM's stderr holds only a generic "exit status 1".
    """
    encoded = base64.b64encode(deploy_script.encode()).decode()
    command = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": [
                f"echo {encoded} | base64 -d > {INSTANCE_DEPLOY_SCRIPT}",
                f"bash {INSTANCE_DEPLOY_SCRIPT}",
            ]
        },
    )
    command_id = command["Command"]["CommandId"]
    for _ in range(120):  # ~20 min ceiling: a cold image build is the slow case
        sleep(10)
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except Exception:  # noqa: BLE001 - InvocationDoesNotExist until the agent picks it up
            continue
        status = result["Status"]
        if status in ("Success", "Cancelled", "TimedOut", "Failed"):
            if status != "Success":
                stderr = (result.get("StandardErrorContent") or "").strip()
                stdout_tail = (result.get("StandardOutputContent") or "").strip()[-1500:]
                detail = "\n".join(part for part in (stderr, stdout_tail) if part)
                raise RuntimeError(
                    f"in-place refresh {status.lower()} on {instance_id}: {detail or 'no output'}"
                )
            return
    raise RuntimeError(f"in-place refresh did not complete on {instance_id}")


def _http_status(url: str) -> int:
    """GET `url` and return the HTTP status (stdlib, short timeout). Raises on connect errors."""
    import urllib.request

    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - our own URL
        return response.status


def wait_until_serving(
    public_url: str,
    *,
    fetch=None,
    sleep=time.sleep,
    attempts: int = 60,
    delay: int = 10,
    echo=None,
) -> bool:
    """Poll `<public_url>/healthz` until it answers 200, so "deployed" means "serving".

    Closes the gap between CloudFormation's CREATE_COMPLETE (the instance *launched*) and the
    engine actually answering: a fresh deploy still has to finish user-data (image build +
    container start) and let the new CloudFront distribution propagate to the edge. Testing the
    real end-to-end URL covers both. `/healthz` is open and outside the blocked `/admin*` path.

    With `echo`, prints a heartbeat (elapsed time + the last status/error) periodically so the
    wait reads as progress, not a hang. Returns True once live, False on timeout (~10 min by
    default). Never raises — a slow but healthy boot must not fail the deploy; the caller
    surfaces diagnostics on a timeout.
    """
    fetch = fetch or _http_status
    url = public_url.rstrip("/") + "/healthz"
    last = "no response yet"
    for i in range(attempts):
        try:
            if fetch(url) == 200:
                return True
            last = f"HTTP {fetch(url)}"
        except Exception as exc:  # noqa: BLE001 - refused / 502 / 504 / DNS lag are all "not yet"
            last = type(exc).__name__
        # Heartbeat every ~30s (not on the first tick), so a multi-minute wait shows life.
        if echo and i and i % (max(30 // delay, 1)) == 0:
            elapsed = (i + 1) * delay
            echo(f"  … still waiting ({elapsed // 60}m{elapsed % 60:02d}s elapsed; last: {last})")
        sleep(delay)
    return False


def _engine_diagnostics(ssm, instance_id: str, *, sleep=time.sleep) -> str:
    """Best-effort container status + logs from the instance, to explain a healthz timeout.

    `docker run -d` in the deploy script returns as soon as the container *starts*, so a deploy
    can report success while the engine crashes a second later — the reason lives in the
    container's logs, not the deploy output. Pulls them (plus the boot log) over one SSM command.
    Never raises: this only runs to explain a deploy that didn't come up, so any failure degrades
    to a short note rather than masking the original timeout.
    """
    commands = [
        "docker ps -a || true",
        "echo '--- engine logs (tail) ---'",
        "docker logs --tail 60 loopy-engine 2>&1 || true",
        "echo '--- boot log (tail) ---'",
        "tail -40 /var/log/loopy-deploy.log 2>&1 || true",
    ]
    try:
        command = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": commands},
        )
        command_id = command["Command"]["CommandId"]
    except Exception as exc:  # noqa: BLE001 - botocore ClientError; diagnostics are best-effort
        return f"(couldn't run diagnostics via SSM: {exc})"
    for _ in range(18):  # ~3 min
        sleep(10)
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except Exception:  # noqa: BLE001 - InvocationDoesNotExist until the agent picks it up
            continue
        if result["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
            return (result.get("StandardOutputContent") or "").strip() or "(no output)"
    return "(diagnostics timed out)"


def _delete_stack_secrets(ssm, stack: str) -> int:
    """Remove every parameter under the stack's path; returns how many went."""
    names: list[str] = []
    token: str | None = None
    while True:
        kwargs = {"Path": stack_param_path(stack), "Recursive": True}
        if token:
            kwargs["NextToken"] = token
        page = ssm.get_parameters_by_path(**kwargs)
        names += [p["Name"] for p in page.get("Parameters", [])]
        token = page.get("NextToken")
        if not token:
            break
    for start in range(0, len(names), 10):  # delete_parameters caps at 10 per call
        ssm.delete_parameters(Names=names[start : start + 10])
    return len(names)


# ── the command ─────────────────────────────────────────────────────────────────────


@deploy_app.command()
def bootstrap(
    manifest: Path = typer.Argument(
        Path("manifest.json"),
        help="A manifest.json, or a project directory to compile (default: manifest.json).",
    ),
    root: Path = typer.Option(Path("."), "--root", help="Project root (env files + sensors)."),
    region: str | None = typer.Option(
        None, "--region", help="AWS region (falls back to the profile/env default)."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Named AWS profile (default: boto3's standard resolution)."
    ),
    stack: str = typer.Option(
        "loopy-engine", "--stack", help="CloudFormation stack name (the idempotency key)."
    ),
    instance_type: str = typer.Option("t3.small", "--instance-type", help="Engine EC2 size."),
    state_size_gb: int = typer.Option(
        8, "--state-size-gb", help="EBS /state volume size (run history + redis queue)."
    ),
    engine_port: int = typer.Option(8000, "--port", help="Engine port behind CloudFront."),
    engine_source: Path | None = typer.Option(
        None,
        "--engine-source",
        help="Build the engine image from this local loopy checkout instead of the pinned PyPI "
        "release — the way to run an unreleased build on AWS. Default: install from PyPI.",
    ),
    destroy: bool = typer.Option(
        False, "--destroy", help="Tear the stack down (snapshot /state first if it matters)."
    ),
) -> None:
    """Provision the starter stack: one EC2 instance behind CloudFront's managed cert.

    The `bootstrap` deploy target — loopy provisions the host for you, from your AWS
    credentials and nothing else (no domain, no DNS). The stack is an EC2 instance
    running the bundled redis+loopy stack, an Elastic IP, an EBS /state volume, and a
    CloudFront distribution whose *.cloudfront.net name is the public HTTPS URL webhooks
    target. Agents still run in Daytona (DAYTONA_API_KEY rides loopy.env). Re-running
    updates the same stack; `--destroy` removes it.
    """
    try:
        boto3 = _require_boto3()
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    session = boto3.Session(profile_name=profile, region_name=region)
    resolved_region = session.region_name
    if not resolved_region:
        typer.echo(
            "error: no AWS region; pass --region or set one on the profile/environment.",
            err=True,
        )
        raise typer.Exit(code=1)

    cf = session.client("cloudformation")
    ssm = session.client("ssm")

    if destroy:
        removed = _delete_stack_secrets(ssm, stack)
        typer.echo(f"deploy: removed {removed} SSM parameter(s) under {stack_param_path(stack)}")
        if not _stack_exists(cf, stack):
            typer.echo(f"deploy: no stack {stack} in {resolved_region}; nothing to delete")
            return
        cf.delete_stack(StackName=stack)
        typer.echo(f"deploy: deleting stack {stack} (the /state volume goes with it)…")
        cf.get_waiter("stack_delete_complete").wait(
            StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 120}
        )
        typer.echo("deploy: stack deleted. The deploy bucket and tarball are kept (cheap, reused).")
        return

    # ── preflight: a compiled manifest and the control-plane env file must exist.
    from loopy_cli import _resolve_manifest  # deferred: avoids a circular import at module load
    from loopy_core import __version__
    from loopy_runtime.secrets import CONTROL_PLANE_ENV_FILE

    manifest, root = _resolve_manifest(manifest, root)
    root_abs = root.resolve()
    manifest_abs = manifest if manifest.is_absolute() else root / manifest
    try:
        manifest_rel = manifest_abs.resolve().relative_to(root_abs).as_posix()
    except ValueError:
        typer.echo(
            f"error: manifest {manifest} is outside the project root {root_abs}; the instance "
            "mounts only the project, so the manifest must live under --root.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if not (root_abs / CONTROL_PLANE_ENV_FILE).is_file():
        typer.echo(
            f"error: {CONTROL_PLANE_ENV_FILE} not found under {root_abs}. The engine host needs "
            "the control-plane creds (DAYTONA_API_KEY at minimum); `loopy init` scaffolds it.",
            err=True,
        )
        raise typer.Exit(code=1)

    ec2 = session.client("ec2")
    s3 = session.client("s3")
    sts = session.client("sts")

    account = sts.get_caller_identity()["Account"]
    bucket = f"loopy-deploy-{account}-{resolved_region}"
    key = f"{stack}/project.tgz"

    # ── secrets to SSM, project to S3.
    secret_files = collect_secret_files(root_abs, manifest_abs)
    _put_secret_files(ssm, stack, root_abs, secret_files)
    typer.echo(
        f"deploy: pushed {len(secret_files)} env file(s) to SSM under "
        f"{stack_param_path(stack)}/files/"
    )
    _ensure_deploy_bucket(s3, bucket, resolved_region)
    s3.put_object(Bucket=bucket, Key=key, Body=package_project(root_abs, secret_files))
    typer.echo(f"deploy: uploaded project to s3://{bucket}/{key}")

    # Engine source: PyPI by default; a `--engine-source` checkout is built into a wheel, shipped
    # to S3, and installed on the instance instead — the way to run an unreleased build. The image
    # tag carries the wheel's content hash so a changed wheel forces a rebuild (the frozen version
    # string alone wouldn't).
    engine_image_tag = __version__
    engine_wheel_s3 = ""
    if engine_source is not None:
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            try:
                wheel = build_engine_wheel(engine_source.resolve(), Path(tmp))
            except RuntimeError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            wheel_bytes = wheel.read_bytes()
            engine_image_tag = f"{__version__}-{hashlib.sha256(wheel_bytes).hexdigest()[:12]}"
            # Keep the wheel's real filename in the key — pip on the instance rejects any name
            # that isn't a valid PEP 427 wheel (`<dist>-<ver>-<pytag>-<abi>-<plat>.whl`).
            engine_key = f"{stack}/engine/{wheel.name}"
            s3.put_object(Bucket=bucket, Key=engine_key, Body=wheel_bytes)
            engine_wheel_s3 = f"s3://{bucket}/{engine_key}"
        typer.echo(
            f"deploy: built engine wheel from {engine_source} → s3://{bucket}/{engine_key} "
            f"(image loopy-engine:{engine_image_tag})"
        )

    # The deploy script (re-fetch project+secrets, restart) is shared by first boot and every
    # re-deploy; user-data embeds it and runs it once, SSM re-runs it thereafter.
    deploy_script = render_deploy_script(
        region=resolved_region,
        stack=stack,
        project_s3_uri=f"s3://{bucket}/{key}",
        loopy_version=__version__,
        manifest_rel=manifest_rel,
        engine_port=engine_port,
        secret_files=secret_files,
        engine_image_tag=engine_image_tag,
        engine_wheel_s3=engine_wheel_s3,
    )
    template_body = build_template_body(render_user_data(deploy_script))
    base_parameters = {
        "InstanceType": instance_type,
        "StateVolumeSizeGiB": str(state_size_gb),
        "CloudFrontPrefixListId": _cloudfront_prefix_list_id(ec2),
        "EnginePort": str(engine_port),
    }

    # A stack left in a failed/rolled-back state (a prior deploy that rolled back without
    # cleaning up, a stuck delete) can't be updated and has no EIP output for the update path
    # to read. Clear it first so this deploy is a clean create rather than a KeyError.
    _clear_unusable_stack(cf, stack)

    # An existing stack already has its EIP (and distribution), so apply once at the final
    # state — never blank the OriginDomain, which would tear the distribution down and mint a
    # new URL. A fresh stack needs two passes: the EIP's DNS name (the CloudFront origin) only
    # exists after pass 1 creates the address.
    existed = _stack_exists(cf, stack)
    if existed:
        origin = eip_public_dns(_stack_outputs(cf, stack)["PublicIp"], resolved_region)
        typer.echo(
            f"deploy: updating stack {stack} in {resolved_region} "
            "(this can take a few minutes; longer if it replaces the instance)…"
        )
        _apply_stack(cf, stack, template_body, {**base_parameters, "OriginDomain": origin})
    else:
        _require_default_vpc(ec2)  # fail fast before provisioning if the region has none
        typer.echo(
            "deploy: this is a first deploy — it runs several minutes end to end (instance boot, "
            "image build, then CloudFront). Safe to leave it; the URL is printed when it's live."
        )
        typer.echo(
            f"deploy: creating stack {stack} in {resolved_region} (instance + address; ~2-4 min)…"
        )
        _apply_stack(cf, stack, template_body, {**base_parameters, "OriginDomain": ""})
        origin = eip_public_dns(_stack_outputs(cf, stack)["PublicIp"], resolved_region)
        typer.echo(
            f"deploy: fronting {origin} with CloudFront "
            "(a fresh distribution takes ~5-10 min to deploy globally)…"
        )
        _apply_stack(cf, stack, template_body, {**base_parameters, "OriginDomain": origin})

    outputs = _stack_outputs(cf, stack)
    public_ip = outputs["PublicIp"]
    public_url = f"https://{outputs['DistributionDomain']}"

    # The instance reads this to append LOOPY_PUBLIC_URL to loopy.env (first boot polls for it;
    # a re-deploy's refresh reads it straight away). Put it before the refresh below.
    ssm.put_parameter(
        Name=f"{stack_param_path(stack)}/public-url",
        Value=public_url,
        Type="String",
        Overwrite=True,
    )

    # Mirror the URL into the operator's *local* loopy.env — the same value the instance gets
    # via SSM. The CloudFront name only exists once the distribution does, so `loopy init`
    # couldn't record it; writing it here is what makes `loopy webhooks github` runnable
    # straight after a deploy, with no hand-copy. Idempotent (the URL is stable across
    # re-deploys). We deliberately do *not* register webhooks ourselves — that stays one
    # explicit `loopy webhooks github` step. The instance id and engine port are client-side
    # hints for the `loopy admin` SSM tunnel (which the CloudFront URL auto-routes to); the
    # engine reads none of these.
    from loopy_cli.deploy_target import (
        BOOTSTRAP_ENGINE_PORT_ENV,
        BOOTSTRAP_INSTANCE_ID_ENV,
    )
    from loopy_runtime.secrets import write_control_plane_env

    write_control_plane_env(
        root_abs,
        {
            "LOOPY_PUBLIC_URL": public_url,
            BOOTSTRAP_INSTANCE_ID_ENV: outputs["InstanceId"],
            BOOTSTRAP_ENGINE_PORT_ENV: str(engine_port),
        },
    )

    # On an update, the running instance still holds the old project/secrets (user-data ran
    # once). Re-run its deploy script in place so it picks up what we just pushed. A fresh
    # instance is doing this via user-data already, so only existing stacks need the nudge.
    if existed:
        typer.echo(
            "deploy: refreshing the engine in place (re-fetch project + secrets, restart; "
            "usually under a minute, a cold image build a few minutes)…"
        )
        _refresh_instance(ssm, outputs["InstanceId"], deploy_script)

    stack_flag = f" --stack {stack}" if stack != "loopy-engine" else ""
    instance_id = outputs["InstanceId"]

    # "Stack complete" only means the instance launched. Wait for the engine to actually answer
    # through CloudFront (user-data's image build + a fresh distribution's edge propagation both
    # trail CREATE_COMPLETE) so the printed URL works when the user hits it.
    typer.echo(
        f"deploy: waiting for the engine to answer at {public_url}/healthz "
        "(first boot builds the image and the CDN propagates; up to ~10 min)…"
    )
    serving = wait_until_serving(public_url, echo=typer.echo)

    typer.echo("")
    verb = "updated" if existed else "done"
    typer.echo(f"deploy: {verb}. Engine at {public_url}")
    if serving:
        typer.echo("  status:    live (/healthz is answering)")
    else:
        typer.echo("  status:    not answering after ~10 min. Pulling the engine's logs from the")
        typer.echo("             instance to show why (a crash on boot shows here; a fresh")
        typer.echo("             CloudFront distribution still propagating does not)…")
        import textwrap

        diagnostics = _engine_diagnostics(ssm, instance_id)
        typer.echo(textwrap.indent(diagnostics, "    "))
        typer.echo(f"    (re-check any time: curl {public_url}/healthz)")
    typer.echo(f"  origin:    {public_ip} (CloudFront-only ingress; direct hits are refused)")
    typer.echo(f"  url:       wrote LOOPY_PUBLIC_URL={public_url} to loopy.env")
    typer.echo("  webhooks:  loopy webhooks github  (registers GitHub delivery to the URL above)")
    # /admin is blocked at CloudFront on this mode (the edge->origin hop is plain HTTP, so the
    # bearer token must not travel it). Reach the dashboard over an SSM tunnel instead — bare
    # `loopy admin` recognizes the CloudFront URL, prints the tunnel command (from the instance
    # id/port recorded in loopy.env above), and proxies to it.
    typer.echo("  dashboard: loopy admin  (recognizes the CloudFront URL; opens the SSM tunnel)")
    typer.echo(f"  teardown:  loopy deploy bootstrap --destroy{stack_flag}")
