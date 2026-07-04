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
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loopy_cli import app
from loopy_cli.aws import (
    TEMPLATE_PATH,
    _apply_stack,
    _delete_stack_secrets,
    _put_secret_files,
    _refresh_instance,
    _require_default_vpc,
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
    assert 'docker image inspect "loopy-engine:$LOOPY_VERSION"' in script


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
        return {"Status": status, "StandardErrorContent": "boom" if status == "Failed" else ""}


def test_refresh_instance_reruns_the_deploy_script_and_waits_for_success():
    ssm = _FakeCommandSsm(["InProgress", "Success"])
    _refresh_instance(ssm, "i-123", sleep=lambda _s: None)
    assert ssm.sent[0]["InstanceIds"] == ["i-123"]
    assert ssm.sent[0]["Parameters"]["commands"] == ["bash /opt/loopy/deploy.sh"]


def test_refresh_instance_raises_on_failed_command():
    ssm = _FakeCommandSsm(["Failed"])
    with pytest.raises(RuntimeError, match="refresh failed on i-123: boom"):
        _refresh_instance(ssm, "i-123", sleep=lambda _s: None)


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
