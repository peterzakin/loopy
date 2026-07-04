"""`loopy deploy aws` — the offline half: rendering, packaging, and client wiring.

Everything here runs with no AWS account, no network, and no boto3 (the module imports
it lazily inside the command body). The boto3-touching helpers take injected clients,
so fakes stand in; the create/update flow itself is exercised through `_apply_stack`
and the `--destroy` path through a fake session.
"""

from __future__ import annotations

import base64
import io
import json
import re
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loopy_cli import app
from loopy_cli.aws import (
    TEMPLATE_PATH,
    _apply_stack,
    _clear_unusable_stack,
    _delete_stack_secrets,
    _engine_diagnostics,
    _put_secret_files,
    _refresh_instance,
    _require_default_vpc,
    build_engine_wheel,
    build_template_body,
    collect_secret_files,
    eip_public_dns,
    package_project,
    render_deploy_script,
    render_user_data,
    stack_param_path,
    wait_until_serving,
)

runner = CliRunner()


# ── pure helpers ─────────────────────────────────────────────────────────────────────


def test_eip_public_dns_us_east_1_keeps_legacy_zone():
    assert eip_public_dns("3.91.10.2", "us-east-1") == "ec2-3-91-10-2.compute-1.amazonaws.com"


def test_eip_public_dns_regional_zone():
    assert (
        eip_public_dns("18.196.1.9", "eu-central-1")
        == "ec2-18-196-1-9.eu-central-1.compute.amazonaws.com"
    )


def _render(**overrides) -> str:
    kwargs = dict(
        region="us-east-1",
        stack="loopy-engine",
        project_s3_uri="s3://loopy-deploy-1-us-east-1/loopy-engine/project.tgz",
        loopy_version="0.1.0",
        manifest_rel="manifest.json",
        engine_port=8000,
        secret_files=["loopy.env", "secrets/base.env"],
        engine_image_tag="0.1.0",
    )
    kwargs.update(overrides)
    return render_deploy_script(**kwargs)


def test_render_deploy_script_fills_every_token():
    script = _render()
    assert "__LOOPY_" not in script
    assert 'PARAM_PATH="/loopy/loopy-engine"' in script
    assert "loopy-computer[redis]==$LOOPY_VERSION" in script
    assert 'LOOPY_VERSION="0.1.0"' in script
    # One fetch line per secret file, back to its project-relative path.
    assert 'fetch_secret_file "loopy.env"' in script
    assert 'fetch_secret_file "secrets/base.env"' in script


def test_render_deploy_script_runs_the_same_collapsed_topology_as_compose():
    """The instance reproduces the bundled stack: redis bus, sqlite state, project ro."""
    script = _render(manifest_rel="build/manifest.json", engine_port=9001)
    assert "--bus redis --state sqlite --state-path /state/state.db" in script
    assert 'run "$MANIFEST_REL" --in-process' in script
    assert 'MANIFEST_REL="build/manifest.json"' in script
    assert 'ENGINE_PORT="9001"' in script
    assert "-v /opt/loopy/project:/project:ro" in script
    assert "redis-server --appendonly yes" in script


def test_render_deploy_script_is_rerunnable():
    """The restart path (rm -f then run) and skip-build-if-present make it safe to re-run."""
    script = _render()
    assert "docker rm -f loopy-redis loopy-engine" in script
    assert 'docker image inspect "loopy-engine:$ENGINE_IMAGE_TAG"' in script


def test_render_deploy_script_defaults_to_pypi_install():
    """Without --engine-source: install the pinned PyPI release, no wheel fetch, tag = version."""
    script = _render(engine_image_tag="0.1.0", engine_wheel_s3="")
    assert 'ENGINE_WHEEL_S3=""' in script
    assert 'ENGINE_IMAGE_TAG="0.1.0"' in script
    assert "loopy-computer[redis]==$LOOPY_VERSION" in script


def test_render_deploy_script_installs_shipped_wheel_when_source_built():
    """With --engine-source: the script fetches the wheel from S3 and installs it, and the
    tag carries the content hash so a changed wheel forces a rebuild."""
    wheel_uri = (
        "s3://loopy-deploy-1-us-east-1/loopy-engine/engine/"
        "loopy_computer-0.1.0-py3-none-any.whl"
    )
    script = _render(engine_image_tag="0.1.0-abc123def456", engine_wheel_s3=wheel_uri)
    assert "__LOOPY_" not in script
    assert f'ENGINE_WHEEL_S3="{wheel_uri}"' in script
    assert 'ENGINE_IMAGE_TAG="0.1.0-abc123def456"' in script
    # Wheel copied into a dir (keeps its PEP 427 name), then installed with the [redis] extra.
    assert 'aws s3 cp "$ENGINE_WHEEL_S3" /opt/loopy/image/wheels/' in script
    assert 'pip install --no-cache-dir "$(ls /tmp/wheels/*.whl)[redis]"' in script


def test_build_engine_wheel_rejects_a_non_checkout(tmp_path):
    """A directory with no pyproject.toml isn't a loopy checkout — fail clearly, not mid-build."""
    with pytest.raises(RuntimeError, match="not a loopy checkout"):
        build_engine_wheel(tmp_path, tmp_path / "out")


def test_render_user_data_embeds_the_deploy_script_as_base64():
    """First-boot user-data carries the deploy script (decodable) and runs it once."""
    deploy_script = _render()
    user_data = render_user_data(deploy_script)
    assert "__LOOPY_" not in user_data
    assert "bash /opt/loopy/deploy.sh" in user_data
    encoded = base64.b64encode(deploy_script.encode()).decode()
    assert encoded in user_data
    # It's the one-time host setup, not the repeatable body.
    assert "dnf install -y docker" in user_data
    assert "docker run -d --name loopy-engine" not in user_data


def test_build_template_body_injects_user_data():
    body = json.loads(build_template_body("#!/bin/bash\necho hi"))
    user_data = body["Resources"]["EngineInstance"]["Properties"]["UserData"]
    assert user_data == {"Fn::Base64": "#!/bin/bash\necho hi"}


def test_template_shape_matches_the_design():
    """The design doc's inventory: instance, CloudFront-only SG, EIP, /state volume,
    scoped role, and a distribution gated on the two-pass OriginDomain parameter."""
    template = json.loads(TEMPLATE_PATH.read_text())
    resources = template["Resources"]
    types = {r["Type"] for r in resources.values()}
    assert {
        "AWS::EC2::Instance",
        "AWS::EC2::SecurityGroup",
        "AWS::EC2::EIP",
        "AWS::EC2::Volume",
        "AWS::IAM::Role",
        "AWS::CloudFront::Distribution",
    } <= types
    # The distribution only exists once pass 2 fills OriginDomain.
    assert resources["Distribution"]["Condition"] == "HasOrigin"
    assert template["Parameters"]["OriginDomain"]["Default"] == ""
    # Ingress is the CloudFront origin-facing prefix list, nothing else — the EIP is a
    # locked-down origin, not a second public door.
    ingress = resources["EngineSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert len(ingress) == 1
    assert ingress[0]["SourcePrefixListId"] == {"Ref": "CloudFrontPrefixListId"}
    # Webhooks must arrive intact: every method, no caching (the managed policy ids).
    behavior = resources["Distribution"]["Properties"]["DistributionConfig"][
        "DefaultCacheBehavior"
    ]
    assert "POST" in behavior["AllowedMethods"]
    assert behavior["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"


def test_security_group_descriptions_use_ec2s_allowed_charset():
    """EC2 rejects a security-group description with any character outside
    `a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*` (so no apostrophe, no emdash) with an opaque
    CREATE_FAILED — catch it here instead of minutes into a deploy."""
    allowed = re.compile(r"^[A-Za-z0-9 ._:/()#,@\[\]+=&;{}!$*-]{1,255}$")
    resources = json.loads(TEMPLATE_PATH.read_text())["Resources"]
    sgs = {
        name: r
        for name, r in resources.items()
        if r["Type"] == "AWS::EC2::SecurityGroup"
    }
    assert sgs, "expected at least one security group in the template"
    for name, sg in sgs.items():
        desc = sg["Properties"]["GroupDescription"]
        assert allowed.match(desc), f"{name} GroupDescription has a char EC2 rejects: {desc!r}"


def test_cloudfront_comments_stay_within_the_128_char_limit():
    """CloudFront rejects a Function or Distribution `Comment` over 128 characters with an
    opaque `The parameter Comment is too big`. Keep them plain, short strings so a reworded
    comment can't quietly blow the limit and roll a deploy back at pass 2."""
    resources = json.loads(TEMPLATE_PATH.read_text())["Resources"]
    comments: dict[str, object] = {}
    for name, r in resources.items():
        props = r.get("Properties", {})
        if r["Type"] == "AWS::CloudFront::Function":
            comments[name] = props.get("FunctionConfig", {}).get("Comment")
        elif r["Type"] == "AWS::CloudFront::Distribution":
            comments[name] = props.get("DistributionConfig", {}).get("Comment")
    assert comments, "expected CloudFront resources with comments in the template"
    for name, comment in comments.items():
        assert isinstance(comment, str), (
            f"{name} Comment must be a plain string kept under 128 chars, not {comment!r} "
            "(an Fn::Sub can silently exceed the limit once resolved)"
        )
        assert len(comment) <= 128, (
            f"{name} Comment is {len(comment)} chars; CloudFront caps it at 128"
        )


def test_admin_is_blocked_at_the_edge():
    """No-domain mode: the edge->origin hop is plain HTTP, so /admin (bearer token) must not
    be reachable through CloudFront — a viewer-request function 403s it. The dashboard is
    reached over an SSM tunnel instead."""
    resources = json.loads(TEMPLATE_PATH.read_text())["Resources"]
    fn = resources["BlockAdminFunction"]
    assert fn["Type"] == "AWS::CloudFront::Function"
    assert "/admin/" in fn["Properties"]["FunctionCode"]
    assert "statusCode: 403" in fn["Properties"]["FunctionCode"]
    assoc = resources["Distribution"]["Properties"]["DistributionConfig"]["DefaultCacheBehavior"][
        "FunctionAssociations"
    ]
    assert assoc[0]["EventType"] == "viewer-request"
    assert assoc[0]["FunctionARN"] == {"Fn::GetAtt": ["BlockAdminFunction", "FunctionARN"]}


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    (root / "secrets").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "loopy.env").write_text("DAYTONA_API_KEY=dt-123\n")
    (root / "secrets" / "base.env").write_text("ANTHROPIC_API_KEY=sk-ant-1\n")
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "2",
                "registry": {
                    "sandboxes": {
                        "default": {"provider": "daytona", "env_file": ["secrets/base.env"]}
                    }
                },
            }
        )
    )
    return root, manifest


def test_collect_secret_files_finds_control_plane_and_sandbox_envs(tmp_path):
    root, manifest = _project(tmp_path)
    assert collect_secret_files(root, manifest) == ["loopy.env", "secrets/base.env"]


def test_collect_secret_files_skips_absent_files(tmp_path):
    root, manifest = _project(tmp_path)
    (root / "secrets" / "base.env").unlink()
    assert collect_secret_files(root, manifest) == ["loopy.env"]


def test_package_project_excludes_vcs_and_secret_files(tmp_path):
    root, _ = _project(tmp_path)
    data = package_project(root, ["loopy.env", "secrets/base.env"])
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert "manifest.json" in names
    assert not any(n.startswith(".git") for n in names)
    assert "loopy.env" not in names
    assert "secrets/base.env" not in names


# ── injected-client helpers ──────────────────────────────────────────────────────────


class _FakeSsm:
    def __init__(self, existing: list[str] | None = None):
        self.puts: list[dict] = []
        self.deleted: list[list[str]] = []
        self._existing = list(existing or [])

    def put_parameter(self, **kwargs):
        self.puts.append(kwargs)

    def get_parameters_by_path(self, **kwargs):
        return {"Parameters": [{"Name": n} for n in self._existing]}

    def delete_parameters(self, Names):  # noqa: N803 - boto3's casing
        self.deleted.append(list(Names))
        return {"DeletedParameters": Names}


def test_put_secret_files_are_securestrings_under_the_stack_path(tmp_path):
    root, _ = _project(tmp_path)
    ssm = _FakeSsm()
    _put_secret_files(ssm, "mystack", root, ["loopy.env", "secrets/base.env"])
    names = [p["Name"] for p in ssm.puts]
    assert names == ["/loopy/mystack/files/loopy.env", "/loopy/mystack/files/secrets/base.env"]
    assert all(p["Type"] == "SecureString" and p["Overwrite"] for p in ssm.puts)
    assert ssm.puts[0]["Value"] == "DAYTONA_API_KEY=dt-123\n"


def test_delete_stack_secrets_chunks_by_ten():
    names = [f"{stack_param_path('s')}/files/f{i}" for i in range(23)]
    ssm = _FakeSsm(existing=names)
    assert _delete_stack_secrets(ssm, "s") == 23
    assert [len(chunk) for chunk in ssm.deleted] == [10, 10, 3]


class _FakeEc2Vpcs:
    def __init__(self, has_default: bool):
        self._has_default = has_default

    def describe_vpcs(self, Filters):  # noqa: N803
        return {"Vpcs": [{"VpcId": "vpc-1"}] if self._has_default else []}


def test_require_default_vpc_passes_when_present():
    _require_default_vpc(_FakeEc2Vpcs(has_default=True))  # must not raise


def test_require_default_vpc_errors_when_missing():
    with pytest.raises(RuntimeError, match="no default VPC"):
        _require_default_vpc(_FakeEc2Vpcs(has_default=False))


class _FakeCommandSsm:
    """Enough SSM to drive _refresh_instance: send, then N polls to a terminal status."""

    def __init__(self, statuses: list[str]):
        self._statuses = list(statuses)
        self.sent: list[dict] = []

    def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, CommandId, InstanceId):  # noqa: N803
        status = self._statuses.pop(0)
        return {
            "Status": status,
            "StandardErrorContent": "boom" if status == "Failed" else "",
            "StandardOutputContent": (
                "…pip install\nERROR: requires a different Python" if status == "Failed" else ""
            ),
        }


def test_refresh_instance_repushes_the_script_then_runs_it():
    """The instance's stored deploy.sh is whatever first boot rendered; a refresh must
    rewrite it from the freshly rendered script before running, or a CLI upgrade that
    fixes the script keeps re-running the old broken one forever."""
    ssm = _FakeCommandSsm(["InProgress", "Success"])
    _refresh_instance(ssm, "i-123", "#!/bin/bash\necho hi\n", sleep=lambda _s: None)
    assert ssm.sent[0]["InstanceIds"] == ["i-123"]
    commands = ssm.sent[0]["Parameters"]["commands"]
    assert len(commands) == 2
    encoded = base64.b64encode(b"#!/bin/bash\necho hi\n").decode()
    assert commands[0] == f"echo {encoded} | base64 -d > /opt/loopy/deploy.sh"
    assert commands[1] == "bash /opt/loopy/deploy.sh"


def test_refresh_instance_failure_carries_the_scripts_stdout():
    """The script tees stderr into stdout (its log redirect), so SSM's stderr is just a
    generic 'exit status 1' — the error must include the stdout tail, where the reason is."""
    ssm = _FakeCommandSsm(["Failed"])
    with pytest.raises(RuntimeError, match="requires a different Python"):
        _refresh_instance(ssm, "i-123", "echo hi", sleep=lambda _s: None)


def test_engine_image_python_satisfies_requires_python():
    """Every deploy Dockerfile must use a base image new enough for the package's
    requires-python — pip inside the image build otherwise refuses the install, which
    surfaces as an opaque exit-1 minutes into a deploy (`python:3.11-slim` vs `>=3.12`)."""
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    spec = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', pyproject)
    assert spec, "expected a >=X.Y requires-python in pyproject.toml"
    minimum = (int(spec.group(1)), int(spec.group(2)))
    deploy_dir = TEMPLATE_PATH.parent
    dockerfiles = ["Dockerfile", "Dockerfile.pypi", "aws-deploy.sh"]
    for name in dockerfiles:
        text = (deploy_dir / name).read_text()
        base = re.search(r"FROM python:(\d+)\.(\d+)-slim", text)
        assert base, f"{name}: expected a python:X.Y-slim base image"
        version = (int(base.group(1)), int(base.group(2)))
        assert version >= minimum, (
            f"{name} builds FROM python:{version[0]}.{version[1]}-slim but the package "
            f"requires >= {minimum[0]}.{minimum[1]}; pip inside the build will refuse it"
        )


def test_wait_until_serving_returns_true_once_healthz_answers_200():
    """The URL 502s while the instance boots and CloudFront propagates, then answers."""
    calls = {"n": 0}
    seen = []

    def fetch(url):
        seen.append(url)
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("502")  # still booting / propagating
        return 200

    assert wait_until_serving("https://x.cloudfront.net", fetch=fetch, sleep=lambda _s: None)
    assert seen[0] == "https://x.cloudfront.net/healthz"  # polls /healthz, not /admin
    assert calls["n"] == 3


def test_wait_until_serving_times_out_without_raising():
    """A slow-but-fine boot must not fail the deploy; timeout returns False, no raise."""
    result = wait_until_serving(
        "https://x.cloudfront.net", fetch=lambda _u: 502, sleep=lambda _s: None, attempts=3
    )
    assert result is False


def test_wait_until_serving_emits_a_heartbeat_while_waiting():
    """A multi-minute wait must show life, not a frozen cursor, so it doesn't read as a hang."""
    lines: list[str] = []
    wait_until_serving(
        "https://x.cloudfront.net",
        fetch=lambda _u: 504,
        sleep=lambda _s: None,
        attempts=10,
        delay=10,
        echo=lines.append,
    )
    assert lines, "expected at least one heartbeat line"
    assert any("elapsed" in line and "504" in line for line in lines)


def test_engine_diagnostics_returns_logs_on_timeout():
    """When healthz times out, the deploy pulls container status + logs so the reason is
    visible inline instead of leaving the operator to SSM in by hand."""

    class _DiagSsm:
        def send_command(self, **kwargs):
            self.commands = kwargs["Parameters"]["commands"]
            return {"Command": {"CommandId": "cmd-diag"}}

        def get_command_invocation(self, CommandId, InstanceId):  # noqa: N803
            return {
                "Status": "Success",
                "StandardOutputContent": "loopy-engine   Exited (1)\n--- engine logs ---\nboom",
            }

    ssm = _DiagSsm()
    out = _engine_diagnostics(ssm, "i-123", sleep=lambda _s: None)
    assert "Exited" in out and "boom" in out
    assert any("docker logs" in c for c in ssm.commands)


def test_engine_diagnostics_never_raises_on_ssm_failure():
    """Diagnostics are best-effort — an SSM failure must not mask the original timeout."""

    class _BrokenSsm:
        def send_command(self, **kwargs):
            raise RuntimeError("ssm down")

    out = _engine_diagnostics(_BrokenSsm(), "i-123", sleep=lambda _s: None)
    assert "couldn't run diagnostics" in out


class _FakeWaiter:
    def wait(self, **kwargs):
        return None


class _FakeCf:
    """Enough CloudFormation to drive _apply_stack through both branches."""

    def __init__(self, exists: bool, update_error: str | None = None):
        self._exists = exists
        self._update_error = update_error
        self.created: list[dict] = []
        self.updated: list[dict] = []

    def describe_stacks(self, StackName):  # noqa: N803
        if not self._exists:
            raise RuntimeError(f"Stack with id {StackName} does not exist")
        return {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Outputs": []}]}

    def create_stack(self, **kwargs):
        self.created.append(kwargs)

    def update_stack(self, **kwargs):
        if self._update_error:
            raise RuntimeError(self._update_error)
        self.updated.append(kwargs)

    def get_waiter(self, name):
        return _FakeWaiter()


def test_apply_stack_creates_when_absent():
    cf = _FakeCf(exists=False)
    _apply_stack(cf, "s", "{}", {"OriginDomain": ""})
    assert len(cf.created) == 1 and not cf.updated
    assert cf.created[0]["Capabilities"] == ["CAPABILITY_IAM"]


def test_apply_stack_updates_when_present():
    cf = _FakeCf(exists=True)
    _apply_stack(cf, "s", "{}", {"OriginDomain": "ec2-1-2-3-4.compute-1.amazonaws.com"})
    assert len(cf.updated) == 1 and not cf.created


def test_apply_stack_treats_noop_update_as_success():
    cf = _FakeCf(exists=True, update_error="No updates are to be performed.")
    _apply_stack(cf, "s", "{}", {"OriginDomain": ""})  # must not raise


def test_apply_stack_surfaces_other_update_errors():
    cf = _FakeCf(exists=True, update_error="TemplateBody is malformed")
    with pytest.raises(RuntimeError, match="malformed"):
        _apply_stack(cf, "s", "{}", {})


class _FailingWaiter:
    def wait(self, **kwargs):
        raise RuntimeError("Waiter StackCreateComplete failed")


class _DeletedStackCf:
    """A create that fails and rolls back: the name no longer resolves, but the id does."""

    STACK_ID = "arn:aws:cloudformation:us-east-1:1:stack/s/abc123"

    def __init__(self):
        self.events_queried_with: list[str] = []

    def describe_stacks(self, StackName):  # noqa: N803 - stack absent ⇒ create branch
        raise RuntimeError(f"Stack with id {StackName} does not exist")

    def create_stack(self, **kwargs):
        return {"StackId": self.STACK_ID}

    def get_waiter(self, name):
        return _FailingWaiter()

    def describe_stack_events(self, StackName):  # noqa: N803
        self.events_queried_with.append(StackName)
        if StackName != self.STACK_ID:  # by name (deleted) CloudFormation would 400
            raise RuntimeError(f"Stack [{StackName}] does not exist")
        return {
            "StackEvents": [
                {
                    "LogicalResourceId": "EngineInstance",
                    "ResourceStatus": "CREATE_FAILED",
                    "ResourceStatusReason": "The instance type t3.small is not supported here",
                }
            ]
        }


class _StatusCf:
    """A CloudFormation whose stack sits in one fixed status; records delete calls."""

    def __init__(self, status: str | None):
        self._status = status
        self.deleted = 0

    def describe_stacks(self, StackName):  # noqa: N803
        if self._status is None:
            raise RuntimeError(f"Stack with id {StackName} does not exist")
        return {"Stacks": [{"StackStatus": self._status, "Outputs": []}]}

    def delete_stack(self, StackName):  # noqa: N803
        self.deleted += 1

    def get_waiter(self, name):
        return _FakeWaiter()


def test_clear_unusable_stack_deletes_a_rolled_back_stack():
    """A ROLLBACK_COMPLETE leftover can't be updated (and has no EIP output), so the deploy
    must delete it before creating — otherwise the update path KeyErrors on 'PublicIp'."""
    cf = _StatusCf("ROLLBACK_COMPLETE")
    _clear_unusable_stack(cf, "loopy-engine", echo=lambda *_: None)
    assert cf.deleted == 1


def test_clear_unusable_stack_waits_out_a_running_delete_without_redeleting():
    cf = _StatusCf("DELETE_IN_PROGRESS")
    _clear_unusable_stack(cf, "loopy-engine", echo=lambda *_: None)
    assert cf.deleted == 0  # already deleting; we just wait


def test_clear_unusable_stack_leaves_a_healthy_stack_untouched():
    cf = _StatusCf("CREATE_COMPLETE")
    _clear_unusable_stack(cf, "loopy-engine", echo=lambda *_: None)
    assert cf.deleted == 0


def test_clear_unusable_stack_noop_when_absent():
    cf = _StatusCf(None)
    _clear_unusable_stack(cf, "loopy-engine", echo=lambda *_: None)
    assert cf.deleted == 0


def test_apply_stack_reports_failure_reason_by_stack_id_after_rollback():
    """A rolled-back create is gone by name; we must query events by the stack id so the
    real cause surfaces instead of a bare 'stack does not exist'."""
    cf = _DeletedStackCf()
    with pytest.raises(RuntimeError, match="t3.small is not supported"):
        _apply_stack(cf, "s", "{}", {"OriginDomain": ""})
    assert cf.events_queried_with == [_DeletedStackCf.STACK_ID]


# ── CLI wiring ───────────────────────────────────────────────────────────────────────


def test_deploy_aws_help_does_not_touch_boto3():
    """Registration is weightless: help renders without importing/using boto3
    (it's a core dep but imported lazily inside the command body)."""
    result = runner.invoke(app, ["deploy", "aws", "--help"])
    assert result.exit_code == 0
    assert "CloudFront" in result.output


def test_destroy_path_with_fake_session(monkeypatch, tmp_path):
    """--destroy deletes the stack's SSM parameters, then the stack itself."""
    import loopy_cli.aws as aws_mod

    ssm = _FakeSsm(existing=["/loopy/loopy-engine/files/loopy.env"])
    cf = _FakeCf(exists=True)
    deleted: list[str] = []
    cf.delete_stack = lambda StackName: deleted.append(StackName)  # noqa: N803

    class _FakeSession:
        region_name = "us-east-1"

        def __init__(self, **kwargs):
            pass

        def client(self, name):
            return {"cloudformation": cf, "ssm": ssm}[name]

    monkeypatch.setattr(
        aws_mod, "_require_boto3", lambda: type("B", (), {"Session": _FakeSession})
    )
    result = runner.invoke(app, ["deploy", "aws", "--destroy", "--region", "us-east-1"])
    assert result.exit_code == 0, result.output
    assert deleted == ["loopy-engine"]
    assert ssm.deleted == [["/loopy/loopy-engine/files/loopy.env"]]
