"""Shared machinery for LIVE release scenarios.

Every live scenario in this directory does the same four things before it proves
anything about the product, and they live here so each scenario is only its own
journey:

1. **Identity.** ``sts:GetCallerIdentity`` must name the account the recorder bound the
   run to. A probe that ran under some other account's credentials is not evidence of
   anything -- it is refused before the first product call.
2. **Observation.** The platform stack in the scenario's Region is read: status, outputs,
   the live template digest, and every Lambda function's ``CodeSha256``.
3. **Binding.** The stack must *be* the candidate: its ``SourceCommit`` /
   ``SourceProjectTree`` outputs equal the run's source, ``SourceDirty`` is ``false``, and
   the live template digest and Lambda code digests equal the candidate's sealed
   assembly for that Region (``build/release-assemblies/<commit>/<region>/``, written by
   ``prepare_assemblies.py`` from the same commit with the pinned toolchain). Any
   disagreement is a failure, a missing assembly is *blocked* -- never a pass.
4. **Provenance.** Only then is the ``provenance`` artifact written, from what was
   observed, in the shape ``scripts/release_evidence.py`` validates.

Secrets (temporary passwords, access tokens) live in process memory and child stdin
only; every error message is passed through :func:`redact` before it can reach a log.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import string
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND = PROJECT_ROOT / "backend"
FRONTEND_PACKAGE_JSON = PROJECT_ROOT / "frontend" / "package.json"
SRP_CHILD = Path(__file__).with_name("cognito_srp_auth.mjs")
ASSEMBLIES_ROOT = PROJECT_ROOT / "build" / "release-assemblies"
STACK_NAME = "acf-production"
RELEASE_REGIONS_PATH = PROJECT_ROOT / "release" / "agentcore-regions.json"

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_SAFE_GIT_OBJECT = re.compile(r"[0-9a-f]{40}")
_SAFE_REGION = re.compile(r"[a-z]{2}-[a-z]+-\d")


class ScenarioBlocked(RuntimeError):
    """The scenario cannot run here; recorded as blocked, never as pass."""


class ScenarioFailed(RuntimeError):
    """The scenario ran and the production claim did not hold."""


# --------------------------------------------------------------------------- utilities


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(document: Any) -> str:
    """Digest of a JSON document independent of key order and whitespace."""
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def redact(text: str, *secrets_to_hide: str) -> str:
    out = text
    for value in secrets_to_hide:
        if value:
            out = out.replace(value, "[REDACTED]")
    return _JWT.sub("[REDACTED_JWT]", out)[:2000]


def boto3_session(region: str):
    import boto3  # local import: the offline dispatcher path must not need boto3

    return boto3.Session(region_name=region)


# --------------------------------------------------------------------------- the run


@dataclass(frozen=True)
class RunBinding:
    run_dir: Path
    commit: str
    project_tree: str
    account_id: str
    region: str

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> RunBinding:
        run = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
        source = run["source"]
        account_id = run.get("account_id")
        if not isinstance(account_id, str) or not re.fullmatch(r"\d{12}", account_id):
            raise ScenarioFailed("the run is not bound to a 12-digit AWS account")
        if args.account_id != account_id:
            raise ScenarioFailed("--account-id disagrees with the run's bound account")
        region = args.region
        if not isinstance(region, str) or not _SAFE_REGION.fullmatch(region):
            raise ScenarioFailed("a live scenario needs --region")
        return cls(args.run_dir, source["commit"], source["project_tree"], account_id, region)


def release_region_profile(region: str) -> dict[str, Any]:
    """The reviewed capability profile for ``region`` from release/agentcore-regions.json."""
    document = json.loads(RELEASE_REGIONS_PATH.read_text(encoding="utf-8"))
    for entry in document.get("regions", []):
        if entry.get("region") == region:
            return entry
    raise ScenarioFailed(f"{region} is not in the release Region matrix")


# --------------------------------------------------------------------------- identity


def verify_identity(binding: RunBinding) -> dict[str, str]:
    identity = boto3_session(binding.region).client("sts").get_caller_identity()
    account = identity.get("Account")
    if account != binding.account_id:
        raise ScenarioFailed(f"credentials belong to account {account}, run is bound to {binding.account_id}")
    return {"account_id": account, "arn": identity.get("Arn", "")}


# --------------------------------------------------------------------------- observation


@dataclass
class ObservedStack:
    region: str
    stack_id: str
    status: str
    outputs: dict[str, str]
    template_sha256: str
    lambda_code_sha256: dict[str, str] = field(default_factory=dict)  # logical id -> base64 CodeSha256

    @property
    def cloudfront_url(self) -> str:
        return self.outputs["CloudFrontUrl"].rstrip("/")


def observe_stack(binding: RunBinding, *, with_lambda_hashes: bool = True) -> ObservedStack:
    session = boto3_session(binding.region)
    cfn = session.client("cloudformation")
    try:
        stack = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
    except Exception as error:  # noqa: BLE001 -- any failure here is "the platform is not deployed"
        raise ScenarioBlocked(f"{STACK_NAME} is not readable in {binding.region}: {redact(str(error))}") from error
    status = stack["StackStatus"]
    if not status.endswith("_COMPLETE") or "ROLLBACK" in status or status.startswith("DELETE"):
        raise ScenarioFailed(f"{STACK_NAME} in {binding.region} is {status}")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
    template = cfn.get_template(StackName=STACK_NAME, TemplateStage="Original")["TemplateBody"]
    if isinstance(template, str):
        template = json.loads(template)
    observed = ObservedStack(
        region=binding.region,
        stack_id=stack["StackId"],
        status=status,
        outputs=outputs,
        template_sha256=canonical_json_sha256(template),
    )
    if with_lambda_hashes:
        lam = session.client("lambda")
        paginator = cfn.get_paginator("list_stack_resources")
        for page in paginator.paginate(StackName=STACK_NAME):
            for resource in page["StackResourceSummaries"]:
                if resource["ResourceType"] != "AWS::Lambda::Function":
                    continue
                configuration = lam.get_function(FunctionName=resource["PhysicalResourceId"])["Configuration"]
                observed.lambda_code_sha256[resource["LogicalResourceId"]] = configuration["CodeSha256"]
    return observed


# --------------------------------------------------------------------------- binding


def assembly_dir(binding: RunBinding) -> Path:
    return ASSEMBLIES_ROOT / binding.commit / binding.region


def _directory_content_sha256(root: Path) -> str:
    """Same digest prepare_assemblies.py seals: sorted relative path + file bytes, symlinks refused."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ScenarioFailed(f"symlink inside a sealed asset: {path.name}")
        if path.is_file():
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big") + relative)
            digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _zip_content_sha256(data: bytes) -> str:
    """The directory digest of a zip's members, so a published archive can be compared to a sealed tree."""
    import io
    import zipfile

    digest = hashlib.sha256()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        for info in sorted(members, key=lambda item: item.filename):
            relative = info.filename.encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big") + relative)
            digest.update(hashlib.sha256(archive.read(info)).digest())
    return digest.hexdigest()


def _published_asset(binding: RunBinding, key: str, cache_dir: Path) -> bytes:
    """Bytes of s3://cdk-hnb659fds-assets-<account>-<region>/<key>, cached by ETag + size."""
    bucket = f"cdk-hnb659fds-assets-{binding.account_id}-{binding.region}"
    s3 = boto3_session(binding.region).client("s3")
    head = s3.head_object(Bucket=bucket, Key=key)
    etag, size = head["ETag"], int(head["ContentLength"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / key
    stamp = cache_dir / f"{key}.etag"
    if cached.is_file() and stamp.is_file() and stamp.read_text() == f"{etag} {size}" and cached.stat().st_size == size:
        return cached.read_bytes()
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if len(body) != size:
        raise ScenarioFailed(f"published asset {key} is {len(body)} bytes, HEAD said {size}")
    cached.write_bytes(body)
    stamp.write_text(f"{etag} {size}")
    return body


def bind_lambda_code(
    binding: RunBinding, observed: ObservedStack, template: dict[str, Any], assembly: Path
) -> list[str]:
    """Every live Lambda's code must be the object CDK published for the candidate's asset, and that
    object's content must be the sealed asset -- byte for byte for zips, file by file for directories."""
    problems: list[str] = []
    expected_ids = {
        logical_id
        for logical_id, resource in template.get("Resources", {}).items()
        if resource.get("Type") == "AWS::Lambda::Function"
    }
    if expected_ids != set(observed.lambda_code_sha256):
        return ["the live stack and the sealed assembly do not have the same Lambda functions"]
    cache_dir = assembly / "published"
    verified_keys: dict[str, str] = {}
    for logical_id in sorted(expected_ids):
        key = template["Resources"][logical_id]["Properties"]["Code"].get("S3Key")
        if not isinstance(key, str) or not key.endswith(".zip"):
            problems.append(f"{logical_id} has no S3 asset key in the sealed template")
            continue
        if key not in verified_keys:
            data = _published_asset(binding, key, cache_dir)
            zip_asset = assembly / f"asset.{key}"
            dir_asset = assembly / f"asset.{key.removesuffix('.zip')}"
            if zip_asset.is_file():
                if hashlib.sha256(data).hexdigest() != sha256_file(zip_asset):
                    problems.append(f"published {key} differs from the sealed zip asset")
            elif dir_asset.is_dir():
                if _zip_content_sha256(data) != _directory_content_sha256(dir_asset):
                    problems.append(f"published {key} content differs from the sealed asset directory")
            else:
                raise ScenarioBlocked(f"sealed assembly lacks the asset for {key}")
            verified_keys[key] = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
        if observed.lambda_code_sha256[logical_id] != verified_keys[key]:
            problems.append(f"{logical_id} CodeSha256 is not the published candidate asset")
    return problems


def bind_deployment(binding: RunBinding, observed: ObservedStack) -> dict[str, Any]:
    """Prove the observed stack IS the candidate; return the comparison for the report."""
    problems: list[str] = []
    claimed_commit = observed.outputs.get("SourceCommit", "")
    claimed_tree = observed.outputs.get("SourceProjectTree", "")
    claimed_dirty = observed.outputs.get("SourceDirty", "")
    if claimed_commit != binding.commit:
        problems.append(f"SourceCommit output is {claimed_commit or '<absent>'}, candidate is {binding.commit}")
    if claimed_tree != binding.project_tree:
        problems.append(
            f"SourceProjectTree output is {claimed_tree or '<absent>'}, candidate is {binding.project_tree}"
        )
    if claimed_dirty != "false":
        problems.append(f"SourceDirty output is {claimed_dirty or '<absent>'}; only a clean synth can be certified")

    assembly = assembly_dir(binding)
    template_path = assembly / f"{STACK_NAME}.template.json"
    if not template_path.is_file():
        raise ScenarioBlocked(
            f"no sealed assembly for {binding.commit[:12]} in {binding.region}; run release/scenarios/prepare_assemblies.py"
        )
    template = json.loads(template_path.read_text(encoding="utf-8"))
    expected_template_sha256 = canonical_json_sha256(template)
    if expected_template_sha256 != observed.template_sha256:
        problems.append("live template digest differs from the sealed assembly")
    lambda_count = sum(1 for r in template.get("Resources", {}).values() if r.get("Type") == "AWS::Lambda::Function")
    if observed.lambda_code_sha256:
        problems.extend(bind_lambda_code(binding, observed, template, assembly))
    comparison = {
        "candidate_commit": binding.commit,
        "candidate_project_tree": binding.project_tree,
        "source_outputs": {"commit": claimed_commit, "project_tree": claimed_tree, "dirty": claimed_dirty},
        "template_sha256": {"live": observed.template_sha256, "assembly": expected_template_sha256},
        "lambda_functions_compared": lambda_count,
        "problems": problems,
    }
    if problems:
        raise ScenarioFailed("deployment is not the candidate: " + "; ".join(problems))
    return comparison


def write_provenance(
    artifact_dir: Path, binding: RunBinding, observed: ObservedStack, **optional: str
) -> dict[str, str]:
    document = {
        "account_id": binding.account_id,
        "region": binding.region,
        "stack_id": observed.stack_id,
        "deployed_commit": binding.commit,
        "deployed_project_tree": binding.project_tree,
        "endpoint": observed.cloudfront_url,
        "deployed_build": observed.template_sha256,
    }
    for key in ("archive_sha256", "artifact_manifest_sha256"):
        if key in optional:
            document[key] = optional[key]
    write_json(artifact_dir / "provenance", document)
    return document


def bound_deployment(args: argparse.Namespace, artifact_dir: Path) -> tuple[RunBinding, ObservedStack, dict[str, Any]]:
    """Identity -> observation -> binding -> provenance, in that order. Every live scenario starts here."""
    binding = RunBinding.from_args(args)
    identity = verify_identity(binding)
    observed = observe_stack(binding)
    comparison = bind_deployment(binding, observed)
    write_provenance(artifact_dir, binding, observed)
    write_json(
        artifact_dir / "deployment-binding", {"identity": identity, **comparison, "stack_status": observed.status}
    )
    return binding, observed, comparison


# --------------------------------------------------------------------------- synthetic identity

REQUIRED_USER_GROUPS = frozenset({"g-users-default", "t-user"})


def generate_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#%_-"
    return "Aa9!" + "".join(secrets.choice(alphabet) for _ in range(32))


@dataclass
class SyntheticUser:
    region: str
    user_pool_id: str
    user_pool_client_id: str
    username: str
    groups: frozenset[str] = REQUIRED_USER_GROUPS
    _password: str = field(default="", repr=False)
    _token: str = field(default="", repr=False)
    created: bool = False
    deleted: bool = False

    @classmethod
    def for_stack(cls, observed: ObservedStack, *, groups: frozenset[str] = REQUIRED_USER_GROUPS, label: str = "live"):
        return cls(
            region=observed.region,
            user_pool_id=observed.outputs["UserPoolId"],
            user_pool_client_id=observed.outputs["UserPoolClientId"],
            username=f"acfpr.{label}.{uuid.uuid4().hex}@example.com",
            groups=groups,
        )

    def _client(self):
        return boto3_session(self.region).client("cognito-idp")

    def create(self) -> None:
        client = self._client()
        self._password = generate_password()
        client.admin_create_user(
            UserPoolId=self.user_pool_id,
            Username=self.username,
            TemporaryPassword=self._password,
            MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": self.username}, {"Name": "email_verified", "Value": "true"}],
        )
        self.created = True
        client.admin_set_user_password(
            UserPoolId=self.user_pool_id, Username=self.username, Password=self._password, Permanent=True
        )
        for group in sorted(self.groups):
            client.admin_add_user_to_group(UserPoolId=self.user_pool_id, Username=self.username, GroupName=group)
        observed = {
            item["GroupName"]
            for item in client.admin_list_groups_for_user(UserPoolId=self.user_pool_id, Username=self.username)[
                "Groups"
            ]
        }
        if observed != self.groups:
            raise ScenarioFailed(f"synthetic user groups are {sorted(observed)}, wanted {sorted(self.groups)}")

    def authenticate(self) -> str:
        """USER_SRP_AUTH through the frontend's pinned Amplify; the token stays in memory."""
        request = {
            "frontendPackageJson": str(FRONTEND_PACKAGE_JSON),
            "userPoolId": self.user_pool_id,
            "userPoolClientId": self.user_pool_client_id,
            "username": self.username,
            "password": self._password,
        }
        completed = subprocess.run(
            ["node", str(SRP_CHILD)],
            input=json.dumps(request),
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode != 0:
            raise ScenarioFailed("Amplify SRP sign-in failed: " + redact(completed.stderr.strip(), self._password))
        result = json.loads(completed.stdout)
        token = result.get("accessToken")
        if result.get("tokenUse") != "access" or not isinstance(token, str) or not token:
            raise ScenarioFailed("Cognito did not return an access token")
        if set(result.get("groups") or []) != self.groups:
            raise ScenarioFailed(f"access-token groups are {sorted(result.get('groups') or [])}")
        self._token = token
        return token

    @property
    def token(self) -> str:
        return self._token

    def secrets(self) -> tuple[str, ...]:
        return (self._password, self._token, self.username)

    def delete(self) -> bool:
        client = self._client()
        try:
            client.admin_delete_user(UserPoolId=self.user_pool_id, Username=self.username)
        except client.exceptions.UserNotFoundException:
            pass
        try:
            client.admin_get_user(UserPoolId=self.user_pool_id, Username=self.username)
        except client.exceptions.UserNotFoundException:
            self.deleted = True
        self._password = ""
        self._token = ""
        return self.deleted


# --------------------------------------------------------------------------- integration phases

BACKEND_PYTHON = BACKEND / ".venv" / "bin" / "python"
SECRET_ENV_NAMES = ("API_BEARER_TOKEN", "INTEGRATION_BEARER_TOKEN", "COGNITO_ACCESS_TOKEN")


def run_integration_phase(
    *,
    phase: str,
    selector: str,
    observed: ObservedStack,
    user: SyntheticUser,
    artifact_dir: Path,
    timeout: int,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one tracked ``backend/tests/integration`` selector against the live stack.

    The access token travels only through the child's environment (never argv, never a
    file); the JUnit report and the sanitized tail of the output become artifacts.
    """
    python = BACKEND_PYTHON if BACKEND_PYTHON.is_file() else Path(sys.executable)
    junit = artifact_dir / f"junit-{phase}.xml"
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AWS_ACCESS") and not name.startswith("AWS_SECRET") and name != "AWS_SESSION_TOKEN"
    }
    env.update(
        {
            "AWS_REGION": observed.region,
            "AWS_DEFAULT_REGION": observed.region,
            "CLOUDFRONT_URL": observed.cloudfront_url,
            "API_GATEWAY_URL": "",
            "API_BEARER_TOKEN": user.token,
            "INTEGRATION_BEARER_TOKEN": "",
            "COGNITO_ACCESS_TOKEN": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    env.update(extra_env or {})
    started = time.time()
    completed = subprocess.run(
        [
            str(python),
            "-m",
            "pytest",
            selector,
            "-m",
            "integration",
            "-vv",
            "--tb=short",
            "--color=no",
            "-p",
            "no:cacheprovider",
            f"--junit-xml={junit}",
            "-o",
            "junit_family=xunit1",
        ],
        cwd=BACKEND,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    tail = redact((completed.stdout + "\n" + completed.stderr)[-20_000:], *user.secrets())
    (artifact_dir / f"pytest-{phase}.log").write_text(tail, encoding="utf-8")
    record = {
        "phase": phase,
        "selector": selector,
        "exit_status": completed.returncode,
        "seconds": round(time.time() - started, 1),
        "junit": junit.name if junit.is_file() else None,
    }
    if completed.returncode != 0:
        record["failed"] = True
    return record


def with_synthetic_user(observed: ObservedStack, *, label: str, groups: frozenset[str] = REQUIRED_USER_GROUPS):
    """Context manager: create + authenticate a synthetic user, always delete it."""

    class _Context:
        def __enter__(self) -> SyntheticUser:
            self.user = SyntheticUser.for_stack(observed, groups=groups, label=label)
            self.user.create()
            self.user.authenticate()
            return self.user

        def __exit__(self, exc_type, exc, tb) -> bool:
            deleted = self.user.delete() if self.user.created else True
            if not deleted:
                raise ScenarioFailed(f"synthetic user {self.user.username} could not be deleted") from exc
            return False

    return _Context()
