"""Focused production contracts for both authenticated exporter endpoints."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml
from app.models.deployment_models import DeployRequest, validate_export_references
from app.services import cfn_template_generator as cfn
from app.services.cfn_template_generator import CfnTemplateGenerator
from app.services.code_generator import (
    PROVIDER_STRANDS_EXTRA,
    _get_model_init_code,
    provider_strands_extras_for,
)
from app.services.downloadable_artifact import _portable_verifier_source
from app.services.python_exporter import accepted_lock_text, build_python_project
from app.services.region_capabilities import SUPPORTED_SINGLE_REGION_RELEASES
from app.services.runtime_artifact import MCP_LEAN_BUNDLE_KEY, STRANDS_BUNDLE_KEY
from app.services.runtime_deployer import canvas_model_providers
from fastapi.testclient import TestClient

MODEL_ID = "us.anthropic.claude-sonnet-5"
EXPORT_ROUTES = ("/api/generate-cfn-template", "/api/export-python")


def _client() -> TestClient:
    import app.deployment_handler as handler

    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "export-production-contract-owner",
                        "cognito:groups": ["g-users-default"],
                    }
                }
            }
        }
    }

    async def inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await handler.deployment_app(scope, receive, send)

    return TestClient(inject, raise_server_exceptions=False)


def _body(provider: str = "bedrock", *, multi_agent: dict | None = None) -> dict:
    return {
        "nodeId": "export-production-node",
        "config": {
            "name": "export-production-agent",
            "framework": "strands_agents",
            "modelProvider": provider,
            "model": {"modelId": MODEL_ID if provider == "bedrock" else f"{provider}-model"},
            "systemPrompt": "Help the customer.",
            "multiAgentPattern": "workflow" if multi_agent else "none",
            "multiAgentConfig": multi_agent,
        },
        "connectedTools": [],
        "gatewayTools": [],
    }


def _archive(response) -> tuple[bytes, dict]:
    assert response.status_code == 200, response.text[:1000]
    payload = response.json()
    data = base64.b64decode(payload["zip_base64"], validate=True)
    assert payload["archive_sha256"] == hashlib.sha256(data).hexdigest()
    with zipfile.ZipFile(io.BytesIO(data)) as artifact:
        manifest = next(name for name in artifact.namelist() if name.endswith("/artifact-manifest.json"))
        assert payload["artifact_manifest_sha256"] == hashlib.sha256(artifact.read(manifest)).hexdigest()
    return data, payload


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as artifact:
        root = artifact.namelist()[0].split("/", 1)[0] + "/"
        return {name.removeprefix(root): artifact.read(name) for name in artifact.namelist()}


def _strands_root_from_cfn_builder(script: str) -> str:
    match = re.search(r'^\s+"(strands-agents(?:\[[^]]+\])?)"\s*\\?$', script, re.MULTILINE)
    assert match, "the generated CFN recipe has no Strands root"
    return match.group(1)


def _complete_cfn_recipe(script: str) -> list[str]:
    block = script.split("    --quiet \\\n", 1)[1].split("\n\n# Strip caches", 1)[0]
    recipe = re.findall(r'^\s+"([^"]+)"\s*\\?$', block, re.MULTILINE)
    assert recipe
    return recipe


@pytest.mark.parametrize("provider", sorted(PROVIDER_STRANDS_EXTRA))
def test_every_provider_closure_is_identical_through_both_authenticated_endpoints(monkeypatch, provider):
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    body = _body(provider)
    python_files = _members(_archive(_client().post("/api/export-python", json=body))[0])
    cfn_files = _members(_archive(_client().post("/api/generate-cfn-template", json=body))[0])

    extras = provider_strands_extras_for(canvas_model_providers(DeployRequest.model_validate(body).config))
    suffix = f"[{','.join(extras)}]" if extras else ""
    expected = f"strands-agents{suffix}==1.56.0"
    requirements = python_files["requirements.txt"].decode()
    assert expected in requirements
    assert (
        _strands_root_from_cfn_builder(cfn_files["build-dependency-bundle.sh"].decode()) == expected.rsplit("==", 1)[0]
    )

    # SageMaker imports OpenAIModel at module scope; its pinned extra declares
    # OpenAI as a transitive, and the exact lock must carry that dependency.
    if provider == "sagemaker":
        assert "openai==2.54.0" in requirements.replace("-c requirements.lock", accepted_lock_text())
        assert "openai==2.54.0" in cfn_files["build-dependency-bundle.sh"].decode()


def test_portable_lock_verifier_rejects_unsorted_or_duplicate_provider_extras(tmp_path):
    project = build_python_project(DeployRequest.model_validate(_body("openai")))
    for name in ("requirements.txt", "requirements.lock", "verify-lock.py"):
        (tmp_path / name).write_text(project[name])

    def verify() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", "verify-lock.py", "--lock-only"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=20,
        )

    assert verify().returncode == 0
    original = (tmp_path / "requirements.txt").read_text()
    (tmp_path / "requirements.txt").write_text(
        original.replace("strands-agents[openai]", "strands-agents[sagemaker,openai]")
    )
    assert verify().returncode == 1
    (tmp_path / "requirements.txt").write_text(
        original.replace("strands-agents[openai]", "strands-agents[openai,openai]")
    )
    assert verify().returncode == 1


def _union_config() -> dict:
    return {
        "agents": [
            {"agentId": "writer", "modelProvider": "openai", "modelId": "gpt-4o"},
            {"agentId": "endpoint", "modelProvider": "sagemaker", "modelId": "endpoint-a"},
            {"agentId": "reviewer", "modelProvider": "mistral", "modelId": "mistral-large"},
        ],
        "edges": [
            {"source": "writer", "target": "endpoint"},
            {"source": "endpoint", "target": "reviewer"},
        ],
        "entryPoint": "writer",
    }


def test_multi_agent_union_covers_parent_and_all_subagents_through_both_endpoints(monkeypatch):
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    body = _body("anthropic", multi_agent=_union_config())
    expected = "strands-agents[anthropic,mistral,openai,sagemaker]"
    python_files = _members(_archive(_client().post("/api/export-python", json=body))[0])
    cfn_files = _members(_archive(_client().post("/api/generate-cfn-template", json=body))[0])
    assert f"{expected}==1.56.0" in python_files["requirements.txt"].decode()
    assert _strands_root_from_cfn_builder(cfn_files["build-dependency-bundle.sh"].decode()) == expected


def test_cfn_bundle_key_is_content_addressed_by_recipe_extras_and_constraint_digest(monkeypatch):
    base = cfn._content_addressed_bundle_key(STRANDS_BUNDLE_KEY)
    with_extra = cfn._content_addressed_bundle_key(STRANDS_BUNDLE_KEY, ("openai",))
    lean = cfn._content_addressed_bundle_key(MCP_LEAN_BUNDLE_KEY)
    assert len({base, with_extra, lean}) == 3
    for key in (base, with_extra, lean):
        assert re.fullmatch(r"agentcore-deps/export-[0-9a-f]{64}\.zip", key)

    original_recipe = list(cfn.DEPENDENCY_BUNDLE_PACKAGES[STRANDS_BUNDLE_KEY])
    monkeypatch.setitem(
        cfn.DEPENDENCY_BUNDLE_PACKAGES,
        STRANDS_BUNDLE_KEY,
        [*original_recipe, "reviewed-recipe-mutation"],
    )
    assert cfn._content_addressed_bundle_key(STRANDS_BUNDLE_KEY) != base
    monkeypatch.setitem(cfn.DEPENDENCY_BUNDLE_PACKAGES, STRANDS_BUNDLE_KEY, original_recipe)

    monkeypatch.setattr(cfn, "_DEPENDENCY_CONSTRAINTS_SHA256", "0" * 64)
    assert cfn._content_addressed_bundle_key(STRANDS_BUNDLE_KEY) != base


def test_explicit_operator_digest_accepts_matching_existing_bytes_without_rebuild(tmp_path):
    script = CfnTemplateGenerator().generate(DeployRequest.model_validate(_body())).deploy_sh
    section = re.search(
        r"^# 3\. Build or verify the reviewed dependency recipe.*?^# 4\. Package and upload assets",
        script,
        re.MULTILINE | re.DOTALL,
    )
    sha_helper = re.search(r"^sha256_stdin\(\).*?^\}", script, re.MULTILINE | re.DOTALL)
    assert section and sha_helper
    remote = tmp_path / "remote.zip"
    remote.write_bytes(b"operator-reviewed-bundle")
    digest = "sha256:" + hashlib.sha256(remote.read_bytes()).hexdigest()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    aws = fake_bin / "aws"
    aws.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "s3api" && "$2" == "head-object" ]]; then exit 0; fi\n'
        'if [[ "$1" == "s3" && "$2" == "cp" ]]; then cp "$REMOTE_BUNDLE" "$4"; exit 0; fi\n'
        "exit 64\n"
    )
    aws.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\nBUCKET=bucket\nREGION=us-east-1\n" + sha_helper.group(0) + "\n" + section.group(0),
        ],
        cwd=tmp_path,
        env={
            "DEPENDENCY_BUNDLE_DIGEST": digest,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "REMOTE_BUNDLE": str(remote),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Dependency bundle digest: {digest}" in result.stdout
    assert not (tmp_path / "build-dependency-bundle.sh").exists()


def _extract_with_portable_verifier(data: bytes, target: Path) -> Path:
    archive_path = target / "artifact.zip"
    archive_path.write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as artifact:
        verifier_name = next(name for name in artifact.namelist() if name.endswith("/verify-artifact.py"))
        verifier = target / "extractor.py"
        verifier.write_bytes(artifact.read(verifier_name))
    destination = target / "extract"
    result = subprocess.run(
        ["python3", "-I", str(verifier), "--archive", str(archive_path), "--extract", str(destination)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    roots = list(destination.iterdir())
    assert len(roots) == 1
    return roots[0]


@pytest.mark.parametrize(("route", "artifact_type"), [(EXPORT_ROUTES[0], "cfn"), (EXPORT_ROUTES[1], "python")])
def test_external_manifest_anchor_allows_runtime_state_but_rejects_declared_file_edits(
    monkeypatch, tmp_path, route, artifact_type
):
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    data, payload = _archive(_client().post(route, json=_body()))
    root = _extract_with_portable_verifier(data, tmp_path)
    (root / ".env").write_text("AWS_REGION=us-east-1\n")
    (root / ".venv").mkdir()
    (root / ".venv" / "runtime-state").write_text("ignored runtime state\n")
    env = {**os.environ, "EXPECTED_ARTIFACT_MANIFEST_SHA256": payload["artifact_manifest_sha256"]}

    clean = subprocess.run(
        ["python3", "-I", "verify-integrity.py"], cwd=root, env=env, capture_output=True, text=True, timeout=20
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
    readme = root / "README.md"
    readme.write_text(readme.read_text() + "intentional edit\n")
    mutant = subprocess.run(
        ["python3", "-I", "verify-integrity.py"], cwd=root, env=env, capture_output=True, text=True, timeout=20
    )
    assert mutant.returncode == 1
    assert "digest or size" in mutant.stderr


_SHADOW_HASHLIB = """import _hashlib
_ORIGINAL = {original!r}
_TAMPERED = {tampered!r}
class _Digest:
    def __init__(self, data=b""):
        self._inner = _hashlib.openssl_sha256(data)
    def update(self, data):
        self._inner.update(data)
    def hexdigest(self):
        value = self._inner.hexdigest()
        return _ORIGINAL if value == _TAMPERED else value
    def digest(self):
        return self._inner.digest()
def sha256(data=b""):
    return _Digest(data)
"""


@pytest.mark.parametrize(("route", "artifact_type"), [(EXPORT_ROUTES[0], "cfn"), (EXPORT_ROUTES[1], "python")])
def test_shadowed_stdlib_module_in_the_artifact_cannot_bless_a_tampered_member(
    monkeypatch, tmp_path, route, artifact_type
):
    """Measured bypass before the fix: `python3 verify-integrity.py` put the artifact directory
    first on sys.path, so an undeclared hashlib.py that lies about one digest made a same-size
    tampered agent.py report "verified". Both shipped verifiers now refuse a non-isolated
    interpreter, and under -I the shadow module is never imported."""
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    data, payload = _archive(_client().post(route, json=_body()))
    root = _extract_with_portable_verifier(data, tmp_path)
    env = {**os.environ, "EXPECTED_ARTIFACT_MANIFEST_SHA256": payload["artifact_manifest_sha256"]}
    env.pop("PYTHONPATH", None)
    manifest = json.loads((root / "artifact-manifest.json").read_bytes())
    agent_path = "agent.py" if artifact_type == "python" else "agent-code/agent.py"
    row = next(entry for entry in manifest["files"] if entry["path"] == agent_path)

    original = (root / agent_path).read_bytes()
    tampered = bytes([original[0] ^ 1]) + original[1:]
    assert len(tampered) == len(original)
    (root / agent_path).write_bytes(tampered)
    (root / "hashlib.py").write_text(
        _SHADOW_HASHLIB.format(original=row["sha256"], tampered=hashlib.sha256(tampered).hexdigest())
    )
    (root / "json.py").write_text('raise SystemExit("shadow json module was imported")\n')

    def run(*argv):
        return subprocess.run(["python3", *argv], cwd=root, env=env, capture_output=True, text=True, timeout=20)

    isolated = run("-I", "verify-integrity.py")
    assert isolated.returncode == 1
    assert "digest or size" in isolated.stderr and "shadow json" not in isolated.stderr
    isolated_dir = run("-I", "verify-artifact.py", "--directory", ".", "--artifact-type", artifact_type)
    assert isolated_dir.returncode == 1 and "shadow json" not in isolated_dir.stderr

    # A non-isolated invocation is refused outright rather than consulting the shadow module.
    for argv in (
        ("verify-integrity.py",),
        ("verify-artifact.py", "--directory", ".", "--artifact-type", artifact_type),
    ):
        plain = run(*argv)
        assert plain.returncode == 1
        assert "python3 -I" in plain.stderr and "verified" not in plain.stdout


def test_shipped_verifiers_check_isolation_before_any_other_import():
    """The guard is only sound if it precedes every import the artifact directory could satisfy."""
    for source in (cfn._entrypoint_integrity_verifier_source("python"), _portable_verifier_source()):
        lines = [line for line in source.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        first_import = next(index for index, line in enumerate(lines) if line.startswith(("import ", "from ")))
        assert lines[first_import] == "import sys"
        guard = next(index for index, line in enumerate(lines) if "sys.flags.isolated" in line)
        next_import = next(
            index for index, line in enumerate(lines) if index > first_import and line.startswith(("import ", "from "))
        )
        assert first_import < guard < next_import
        assert "sys.flags.ignore_environment != 1" in source and "sys.flags.no_user_site != 1" in source


def test_every_direct_entrypoint_verifies_integrity_before_side_effects():
    request = DeployRequest.model_validate(_body())
    bundle = CfnTemplateGenerator().generate(request)
    project = build_python_project(request)
    checks = (
        (bundle.deploy_sh, "aws sts get-caller-identity"),
        (bundle.teardown_sh, "aws cloudformation"),
        (bundle.build_bundle_sh, 'BUILD_DIR="$(mktemp -d)"'),
        (project["run.sh"], "exec .venv/bin/python"),
        (project["run-docker.sh"], "docker build"),
        (project["install-locked.sh"], '"$python_bin" -m venv'),
    )
    # Isolated mode must immediately precede the verifier whatever the interpreter spelling
    # (`python3` or the `"$python_bin"` override in install-locked.sh), and no generated text
    # may execute either verifier any other way. An "execution" is an interpreter token
    # followed by the script name on one line; prose that merely names the file is not.
    isolated = re.compile(r'(?:python3|"\$python_bin")\s+-I\s+verify-integrity\.py')
    interpreter = r'(?:python3?|"\$python_bin"|\$python_bin)'
    execution = re.compile(interpreter + r"((?:\s+-\S+)*)\s+(verify-(?:integrity|artifact)\.py)")
    for script, first_side_effect in checks:
        match = isolated.search(script)
        assert match, "entrypoint does not invoke the verifier in isolated mode"
        assert match.start() < script.index(first_side_effect)
    generated = [bundle.deploy_sh, bundle.teardown_sh, bundle.build_bundle_sh, bundle.readme, *project.values()]
    executions = 0
    for text in generated:
        for found in execution.finditer(text):
            executions += 1
            assert "-I" in found.group(1).split(), found.group(0)
    assert executions >= len(checks), "the invocation scanner found fewer verifier runs than entrypoints"


def test_docker_context_is_deny_by_default_with_only_reviewed_inputs():
    rules = build_python_project(DeployRequest.model_validate(_body()))[".dockerignore"].splitlines()
    assert rules[0] == "*"
    assert set(rules[1:]) == {
        "!.dockerignore",
        "!Dockerfile",
        "!agent.py",
        "!requirements.txt",
        "!requirements.lock",
        "!verify-integrity.py",
        "!verify-lock.py",
        "!check-installed.sh",
    }
    assert not any(name in "\n".join(rules) for name in (".env", ".git", "**"))

    def deny_first(candidate: list[str]) -> bool:
        return bool(candidate) and candidate[0] == "*" and all(line.startswith("!") for line in candidate[1:])

    assert deny_first(rules)
    assert not deny_first(rules[1:]), "removing the deny rule must make the static mutant fail"


@pytest.mark.skipif(
    os.environ.get("RUN_EXPORT_DOCKER_SENTINEL") != "1", reason="live Docker daemon-context sentinel pending"
)
def test_live_docker_daemon_never_receives_an_arbitrary_context_file(tmp_path):
    docker = shutil.which("docker")
    assert docker, "RUN_EXPORT_DOCKER_SENTINEL requires a Docker daemon and client"
    project = build_python_project(DeployRequest.model_validate(_body()))
    (tmp_path / ".dockerignore").write_text(project[".dockerignore"])
    (tmp_path / "arbitrary-secret.txt").write_text("must not enter context\n")
    (tmp_path / "Dockerfile.sentinel").write_text("FROM scratch\nCOPY arbitrary-secret.txt /leak\n")
    result = subprocess.run(
        [docker, "build", "--no-cache", "--progress=plain", "-f", "Dockerfile.sentinel", "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "arbitrary-secret.txt" in result.stdout + result.stderr


def test_generated_ci_is_protected_and_container_build_is_opt_in():
    request = DeployRequest.model_validate(_body())
    bundle = CfnTemplateGenerator().generate(request)
    with zipfile.ZipFile(io.BytesIO(bundle.to_zip())) as artifact:
        root = artifact.namelist()[0].split("/", 1)[0]
        workflow = artifact.read(f"{root}/.github/workflows/deploy.yml").decode()
        cfn_buildspec = artifact.read(f"{root}/buildspec.yml").decode()
    credentials = workflow.index("Configure short-lived AWS credentials")
    anchor = workflow.index("Verify externally anchored artifact integrity before credentials")
    assert anchor < credentials
    assert "environment: production" in workflow
    assert "github.event.repository.default_branch" in workflow
    assert "allowed-account-ids: ${{ vars.AWS_ACCOUNT_ID }}" in workflow
    assert "audience: sts.amazonaws.com" in workflow
    assert workflow.count("id-token: write") == 1
    assert "cfn-lint==1.57.0" in cfn_buildspec and "checkov==3.3.1" in cfn_buildspec

    project = build_python_project(request)
    assert 'BUILD_CONTAINER: "false"' in project["buildspec.yml"]
    assert 'if [ "$BUILD_CONTAINER" = "true" ]' in project["buildspec.yml"]
    assert "Docker" in project["CI-CD.md"] and "Python 3.13" in project["CI-CD.md"]


_BAD_REFERENCE_CASES = (
    (
        "raw-key",
        {
            "gatewayConfig": {
                "gatewayProvider": "litellm",
                "litellmBaseUrl": "https://proxy.example",
                "litellmApiKey": "marker-raw-key",  # pragma: allowlist secret
            }
        },
        "marker-raw-key",
    ),
    (
        "provider-namespace",
        {
            "config": {"providerApiKeyRef": "arn:aws:secretsmanager:us-east-1:123456789012:secret:wrong/provider"}
        },  # pragma: allowlist secret
        "wrong/provider",
    ),
    (
        "account-shape",
        {
            "config": {
                "providerApiKeyRef": "arn:aws:secretsmanager:us-east-1:not-account:secret:agentcore-provider/key"
            }
        },  # pragma: allowlist secret
        "not-account",
    ),
    (
        "oauth-name",
        {"identityConfig": {"provider": "okta", "clientSecretRef": "bare-secret-name"}},
        "bare-secret-name",
    ),  # pragma: allowlist secret
    (
        "otel-namespace",
        {
            "observabilityConfig": {
                "enabled": True,
                "authHeaderSecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:wrong/otel",  # pragma: allowlist secret
            }
        },
        "wrong/otel",
    ),
    (
        "token-http",
        {"gatewayConfig": {"client_info": {"token_endpoint": "http://token.example/marker-http"}}},
        "marker-http",
    ),
    (
        "url-userinfo",
        {"config": {"providerBaseUrl": "https://user:marker-password@models.example/v1"}},  # pragma: allowlist secret
        "marker-password",
    ),
    ("multiline", {"gatewayConfig": {"gatewayUrl": "https://gateway.example/marker-one\nmarker-two"}}, "marker-two"),
    (
        "bearer",
        {"observabilityConfig": {"enabled": True, "extraHeaders": {"Authorization": "Bearer marker-bearer"}}},
        "marker-bearer",
    ),
    # ConnectorConfig.secret_value is exclude=True: invisible to model_dump() and therefore to a
    # serialization-based scan. It must still be refused, by the typed attribute, on both routes.
    (
        "connector-typed-secret",
        {
            "connectors": [{"connectorId": "asana", "authMethod": "api_key", "secretValue": "marker-connector-raw"}]
        },  # pragma: allowlist secret
        "marker-connector-raw",
    ),
    (
        "external-mcp-raw-secret",
        {"externalMcpServers": [{"server_id": "direct-example", "secret_value": "marker-mcp-raw"}]},
        "marker-mcp-raw",
    ),
    (
        "external-mcp-oauth-client-secret",
        {"externalMcpServers": [{"server_id": "direct-example", "oauth": {"client_secret": "marker-mcp-oauth"}}]},
        "marker-mcp-oauth",
    ),
)


def _merge_case(body: dict, patch: dict) -> dict:
    result = {**body}
    for key, value in patch.items():
        if key == "config":
            result["config"] = {**body["config"], **value}
        else:
            result[key] = value
    return result


@pytest.mark.parametrize("route", EXPORT_ROUTES)
@pytest.mark.parametrize(
    ("_name", "patch", "secret_marker"), _BAD_REFERENCE_CASES, ids=[case[0] for case in _BAD_REFERENCE_CASES]
)
def test_export_reference_failures_never_echo_rejected_values(monkeypatch, route, _name, patch, secret_marker):
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    response = _client().post(route, json=_merge_case(_body(), patch))
    assert response.status_code in {400, 422}
    assert secret_marker not in response.text


def test_typed_write_only_connector_secret_is_refused_by_the_validator_itself_without_echo():
    """model_dump() drops secret_value (exclude=True), so a serialization scan cannot see it;
    the validator must reject the typed attribute directly and name only the field."""
    from app.models.deployment_models import ExportReferenceValidationError

    request = DeployRequest.model_validate(
        _merge_case(
            _body(),
            {
                "connectors": [{"connectorId": "asana", "authMethod": "api_key", "secretValue": "marker-typed-secret"}]
            },  # pragma: allowlist secret
        )
    )
    assert request.connectors[0].secret_value == "marker-typed-secret"  # pragma: allowlist secret
    assert "secretValue" not in request.connectors[0].model_dump(by_alias=True), "control: the field is hidden"
    with pytest.raises(ExportReferenceValidationError) as excinfo:
        validate_export_references(request)
    assert "connectors[0].secretValue" in str(excinfo.value)
    assert "marker-typed-secret" not in str(excinfo.value)


def test_valid_reference_namespaces_and_https_endpoints_pass_the_export_gate():
    request = DeployRequest.model_validate(
        _merge_case(
            _body("openai"),
            {
                "config": {
                    "providerApiKeyRef": "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-provider/openai-AbCdEf"  # pragma: allowlist secret
                },
                "gatewayConfig": {
                    "gatewayProvider": "litellm",
                    "litellmBaseUrl": "https://proxy.example/v1",
                    "litellmApiKeyRef": "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-connector/litellm-AbCdEf",  # pragma: allowlist secret
                    "client_info": {"token_endpoint": "https://idp.example/oauth/token"},
                },
                "observabilityConfig": {
                    "enabled": True,
                    "otlpEndpoint": "https://otel.example/v1/traces",
                    "authHeaderSecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/team-AbCdEf",  # pragma: allowlist secret
                },
            },
        )
    )
    validate_export_references(request)


def test_cfn_non_bedrock_provider_reference_is_serialized_by_reference_only():
    secret = "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-provider/openai-AbCdEf"  # pragma: allowlist secret
    body = _merge_case(
        _body("openai"),
        {"config": {"providerApiKeyRef": secret, "providerBaseUrl": "https://models.example/v1"}},
    )
    template = yaml.safe_load(CfnTemplateGenerator().generate(DeployRequest.model_validate(body)).template_yaml)
    parameter = template["Parameters"]["ProviderApiKeySecretArn"]
    assert parameter["Default"] == secret
    assert "agentcore-provider/" in parameter["AllowedPattern"]
    env = template["Resources"]["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]
    assert env["PROVIDER_API_KEY_SECRET_ARN"] == {"Ref": "ProviderApiKeySecretArn"}
    assert env["PROVIDER_BASE_URL"] == {"Ref": "ProviderBaseUrl"}
    statements = template["Resources"]["RuntimeExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    by_sid = {statement.get("Sid"): statement for statement in statements}
    assert by_sid["ModelProviderApiKey"]["Resource"] == [{"Ref": "ProviderApiKeySecretArn"}]
    assert by_sid["ModelProviderApiKeyDecrypt"]["Condition"]["StringEquals"]["kms:EncryptionContext:SecretARN"] == {
        "Ref": "ProviderApiKeySecretArn"
    }


def test_template_and_deploy_script_enforce_the_exact_seven_region_contract(tmp_path):
    bundle = CfnTemplateGenerator().generate(DeployRequest.model_validate(_body()))
    template = yaml.safe_load(bundle.template_yaml)
    assertion = template["Rules"]["SupportedReleaseRegion"]["Assertions"][0]
    assert assertion["Assert"]["Fn::Contains"][0] == sorted(SUPPORTED_SINGLE_REGION_RELEASES)
    assert assertion["Assert"]["Fn::Contains"][1] == {"Ref": "AWS::Region"}
    assert set(SUPPORTED_SINGLE_REGION_RELEASES) == {
        "eu-central-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
    for region in sorted(SUPPORTED_SINGLE_REGION_RELEASES):
        assert region in bundle.readme

    script = tmp_path / "deploy.sh"
    script.write_text(bundle.deploy_sh)
    (tmp_path / "verify-integrity.py").write_text("raise SystemExit(0)\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "aws.log"
    aws = fake_bin / "aws"
    aws.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$AWS_LOG"\nexit 97\n')
    aws.chmod(0o755)
    result = subprocess.run(
        ["bash", str(script), "stack", "ap-south-1", "bucket"],
        cwd=tmp_path,
        env={**os.environ, "AWS_LOG": str(log), "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert "unsupported deployment Region" in result.stderr
    assert not log.exists(), "unsupported Region reached an AWS command"


def test_bedrock_region_oracle_rejects_a_wrong_geography_before_aws(tmp_path):
    bundle = CfnTemplateGenerator().generate(DeployRequest.model_validate(_body()))
    script = tmp_path / "deploy.sh"
    script.write_text(bundle.deploy_sh)
    (tmp_path / "verify-integrity.py").write_text("raise SystemExit(0)\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "aws.log"
    aws = fake_bin / "aws"
    aws.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$AWS_LOG"\nexit 97\n')
    aws.chmod(0o755)
    result = subprocess.run(
        ["bash", str(script), "stack", "eu-west-1", "bucket"],
        cwd=tmp_path,
        env={**os.environ, "AWS_LOG": str(log), "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert "inference-profile prefix" in result.stderr
    assert not log.exists(), "wrong model geography reached an AWS command"


def test_matching_bedrock_model_override_passes_region_oracle(tmp_path):
    bundle = CfnTemplateGenerator().generate(DeployRequest.model_validate(_body()))
    script = tmp_path / "deploy.sh"
    script.write_text(bundle.deploy_sh)
    (tmp_path / "verify-integrity.py").write_text("raise SystemExit(0)\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "aws.log"
    aws = fake_bin / "aws"
    aws.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$AWS_LOG"\nexit 97\n')
    aws.chmod(0o755)
    result = subprocess.run(
        ["bash", str(script), "stack", "eu-west-1", "bucket"],
        cwd=tmp_path,
        env={
            **os.environ,
            "AWS_LOG": str(log),
            "MODEL_ID": "eu.anthropic.claude-sonnet-5",
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert "inference-profile prefix" not in result.stderr
    assert log.read_text().splitlines()[0].startswith("sts get-caller-identity")


_CLEAN_PROVIDERS = ("bedrock", "openai", "anthropic", "gemini", "litellm", "mistral", "ollama", "sagemaker", "llamaapi")


@pytest.mark.skipif(
    os.environ.get("RUN_EXPORT_CLEAN_INSTALL") != "1", reason="real clean install/import matrix pending"
)
@pytest.mark.parametrize("route", EXPORT_ROUTES)
@pytest.mark.parametrize("provider", _CLEAN_PROVIDERS)
def test_real_clean_install_and_provider_import_from_each_authenticated_endpoint(
    monkeypatch, tmp_path, route, provider
):
    """No compile fallback: success requires pip installation and real provider imports."""
    python313 = shutil.which("python3.13")
    assert python313, "RUN_EXPORT_CLEAN_INSTALL requires Python 3.13"
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    files = _members(_archive(_client().post(route, json=_body(provider)))[0])
    work = tmp_path / re.sub(r"[^a-z]", "", route)
    work.mkdir()
    if route.endswith("export-python"):
        (work / "requirements.txt").write_bytes(files["requirements.txt"])
        (work / "requirements.lock").write_bytes(files["requirements.lock"])
        install_args = ["-r", "requirements.txt"]
    else:
        builder = files["build-dependency-bundle.sh"].decode()
        constraints = builder.split("<<'CONSTRAINTS'\n", 1)[1].split("\nCONSTRAINTS", 1)[0] + "\n"
        recipe = _complete_cfn_recipe(builder)
        (work / "requirements.txt").write_text("-c requirements.lock\n" + "\n".join(recipe) + "\n")
        (work / "requirements.lock").write_text(constraints)
        install_args = ["-r", "requirements.txt"]
    venv = work / "venv"
    subprocess.run([python313, "-m", "venv", str(venv)], check=True, timeout=60)
    python = venv / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", *install_args],
        cwd=work,
        check=True,
        timeout=900,
    )
    import_line, _ = _get_model_init_code(provider, "model", "us-east-1")
    result = subprocess.run(
        [str(python), "-c", f"{import_line}; import bedrock_agentcore; print('import-ok')"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "import-ok"


@pytest.mark.skipif(
    os.environ.get("RUN_EXPORT_CLEAN_INSTALL") != "1", reason="real clean install/import matrix pending"
)
@pytest.mark.parametrize("route", EXPORT_ROUTES)
def test_real_clean_install_and_all_multi_agent_union_imports_from_each_endpoint(monkeypatch, tmp_path, route):
    """The union gate imports every provider module after one clean exact install."""
    python313 = shutil.which("python3.13")
    assert python313, "RUN_EXPORT_CLEAN_INSTALL requires Python 3.13"
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    body = _body("anthropic", multi_agent=_union_config())
    files = _members(_archive(_client().post(route, json=body))[0])
    work = tmp_path / re.sub(r"[^a-z]", "", route)
    work.mkdir()
    if route.endswith("export-python"):
        (work / "requirements.txt").write_bytes(files["requirements.txt"])
        (work / "requirements.lock").write_bytes(files["requirements.lock"])
    else:
        builder = files["build-dependency-bundle.sh"].decode()
        constraints = builder.split("<<'CONSTRAINTS'\n", 1)[1].split("\nCONSTRAINTS", 1)[0] + "\n"
        recipe = _complete_cfn_recipe(builder)
        (work / "requirements.txt").write_text("-c requirements.lock\n" + "\n".join(recipe) + "\n")
        (work / "requirements.lock").write_text(constraints)
    venv = work / "venv"
    subprocess.run([python313, "-m", "venv", str(venv)], check=True, timeout=60)
    python = venv / "bin" / "python"
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "-r",
            "requirements.txt",
        ],
        cwd=work,
        check=True,
        timeout=900,
    )
    import_lines = [
        _get_model_init_code(provider, "model", "us-east-1")[0]
        for provider in ("anthropic", "openai", "sagemaker", "mistral")
    ]
    result = subprocess.run(
        [str(python), "-c", "; ".join(import_lines) + "; import bedrock_agentcore; print('union-import-ok')"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "union-import-ok"
