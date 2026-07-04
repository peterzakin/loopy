"""`loopy deploy` — provision hosting for the engine from an operator's cloud keys.

`loopy deploy aws` stands up the design in `docs/design/aws-deploy.md`: one
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
    no_args_is_help=True, help="Provision hosting for the engine (`loopy deploy aws`)."
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
) -> str:
    """Fill the re-runnable deploy script's tokens (fetch project+secrets, restart the stack).

    This is the code path shared by first boot and every re-deploy, so its inputs are all
    stable per stack: it re-reads whatever the CLI last pushed to S3/SSM. Pure string work,
    so tests cover it offline.
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
        "__LOOPY_FETCH_SECRET_FILES__": fetch_lines,
    }
    for token, value in replacements.items():
        script = script.replace(token, value)
    if "__LOOPY_" in script:
        raise ValueError("deploy-script render left an unfilled __LOOPY_ token")
    return script


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


def _stack_exists(cf, stack: str) -> bool:
    try:
        response = cf.describe_stacks(StackName=stack)
    except Exception:  # noqa: BLE001 - ClientError ValidationError == "does not exist"
        return False
    status = response["Stacks"][0]["StackStatus"]
    if status == "REVIEW_IN_PROGRESS":  # a never-executed change set, not a real stack
        return False
    return True


def _apply_stack(cf, stack: str, template_body: str, parameters: dict[str, str]) -> None:
    """create_stack or update_stack, then wait; a no-op update is success, not an error."""
    params = [{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()]
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
        cf.create_stack(
            StackName=stack,
            TemplateBody=template_body,
            Parameters=params,
            Capabilities=["CAPABILITY_IAM"],
            OnFailure="DELETE",
        )
        waiter = cf.get_waiter("stack_create_complete")
    try:
        waiter.wait(StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 120})
    except Exception as exc:
        reasons = _failure_reasons(cf, stack)
        raise RuntimeError(f"stack {stack} did not stabilize: {reasons}") from exc


def _failure_reasons(cf, stack: str) -> str:
    """The first few failed-resource reasons — the part of the event stream worth reading."""
    try:
        events = cf.describe_stack_events(StackName=stack)["StackEvents"]
    except Exception:  # noqa: BLE001 - the stack may already be gone (OnFailure=DELETE)
        return "no stack events available (the failed create may have rolled back and deleted)"
    reasons = [
        f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', e['ResourceStatus'])}"
        for e in events
        if e["ResourceStatus"].endswith("_FAILED")
    ]
    return "; ".join(reasons[:5]) or "see the CloudFormation console for events"


def _stack_outputs(cf, stack: str) -> dict[str, str]:
    described = cf.describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}


def _refresh_instance(ssm, instance_id: str, *, sleep=time.sleep) -> None:
    """Re-run /opt/loopy/deploy.sh on the instance via SSM RunCommand, then wait.

    This is the in-place update: the CLI has already pushed the new tarball (S3) and secrets
    (SSM), so re-running the deploy script re-fetches both and restarts the containers — no
    instance replacement, ~seconds of downtime. Only called for an already-running stack, where
    the instance has had time to register with SSM; a fresh boot updates via user-data instead.
    """
    command = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [f"bash {INSTANCE_DEPLOY_SCRIPT}"]},
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
                detail = (result.get("StandardErrorContent") or "").strip()[:500]
                raise RuntimeError(f"in-place refresh {status.lower()} on {instance_id}: {detail}")
            return
    raise RuntimeError(f"in-place refresh did not complete on {instance_id}")


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
def aws(
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
    destroy: bool = typer.Option(
        False, "--destroy", help="Tear the stack down (snapshot /state first if it matters)."
    ),
) -> None:
    """Provision the engine on AWS: one EC2 instance behind CloudFront's managed cert.

    You bring AWS credentials and nothing else — no domain, no DNS. The stack is an EC2
    instance running the bundled redis+loopy stack, an Elastic IP, an EBS /state volume,
    and a CloudFront distribution whose *.cloudfront.net name is the public HTTPS URL
    webhooks target. Agents still run in Daytona (DAYTONA_API_KEY rides loopy.env).
    Re-running updates the same stack; `--destroy` removes it.
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
    )
    template_body = build_template_body(render_user_data(deploy_script))
    base_parameters = {
        "InstanceType": instance_type,
        "StateVolumeSizeGiB": str(state_size_gb),
        "CloudFrontPrefixListId": _cloudfront_prefix_list_id(ec2),
        "EnginePort": str(engine_port),
    }

    # An existing stack already has its EIP (and distribution), so apply once at the final
    # state — never blank the OriginDomain, which would tear the distribution down and mint a
    # new URL. A fresh stack needs two passes: the EIP's DNS name (the CloudFront origin) only
    # exists after pass 1 creates the address.
    existed = _stack_exists(cf, stack)
    if existed:
        origin = eip_public_dns(_stack_outputs(cf, stack)["PublicIp"], resolved_region)
        typer.echo(f"deploy: updating stack {stack} in {resolved_region}…")
        _apply_stack(cf, stack, template_body, {**base_parameters, "OriginDomain": origin})
    else:
        typer.echo(f"deploy: creating stack {stack} in {resolved_region} (instance + address)…")
        _apply_stack(cf, stack, template_body, {**base_parameters, "OriginDomain": ""})
        origin = eip_public_dns(_stack_outputs(cf, stack)["PublicIp"], resolved_region)
        typer.echo(
            f"deploy: fronting {origin} with CloudFront (a fresh distribution takes a few minutes)…"
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

    # On an update, the running instance still holds the old project/secrets (user-data ran
    # once). Re-run its deploy script in place so it picks up what we just pushed. A fresh
    # instance is doing this via user-data already, so only existing stacks need the nudge.
    if existed:
        typer.echo("deploy: refreshing the engine in place (re-fetch project + secrets, restart)…")
        _refresh_instance(ssm, outputs["InstanceId"])

    stack_flag = f" --stack {stack}" if stack != "loopy-engine" else ""
    instance_id = outputs["InstanceId"]
    typer.echo("")
    verb = "updated" if existed else "done"
    typer.echo(f"deploy: {verb}. Engine at {public_url}")
    typer.echo(f"  origin:    {public_ip} (CloudFront-only ingress; direct hits are refused)")
    typer.echo(f"  webhooks:  set LOOPY_PUBLIC_URL={public_url} in loopy.env,")
    typer.echo("             then `loopy webhooks github`")
    # /admin is blocked at CloudFront on this mode (the edge->origin hop is plain HTTP, so the
    # bearer token must not travel it). Reach the dashboard over an SSM tunnel instead.
    typer.echo("  dashboard: aws ssm start-session --target " + instance_id)
    typer.echo("             --document-name AWS-StartPortForwardingSession \\")
    typer.echo(f"             --parameters '{{\"portNumber\":[\"{engine_port}\"],"
               '"localPortNumber":["9000"]}\'')
    typer.echo("             then: loopy admin --remote http://localhost:9000")
    typer.echo(f"  teardown:  loopy deploy aws --destroy{stack_flag}")
