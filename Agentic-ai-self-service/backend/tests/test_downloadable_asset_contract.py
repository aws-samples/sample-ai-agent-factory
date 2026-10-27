"""CI/CD-grade contract for customer-downloadable CloudFormation and Python ZIPs."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shlex
import stat
import subprocess
import sys
import warnings
import zipfile
from pathlib import Path

import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import CfnTemplateGenerator
from app.services.code_generator import PROVIDER_PACKAGES
from app.services.downloadable_artifact import (
    ARTIFACT_CHECKSUMS,
    ARTIFACT_MANIFEST,
    FIXED_ZIP_TIME,
    DownloadableArtifactError,
    verify_artifact_zip,
)
from app.services.python_exporter import accepted_lock_text, build_and_zip, build_python_project
from fastapi.testclient import TestClient

MODEL_ID = "us.anthropic.claude-sonnet-5"
GALLERY = (
    "web-search-agent",
    "strands-gateway-agent",
    "customer-support-assistant",
    "customer-support-blueprint",
    "mcp-server-gateway-target",
    "mcp-server-runtime",
)


def _client() -> TestClient:
    import app.deployment_handler as handler

    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "download-contract-owner",
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


def _gallery_body(template_id: str) -> dict:
    config: dict = {
        "name": f"download-{template_id}",
        "protocol": "HTTP",
        "framework": "strands_agents",
        "model": {"modelId": MODEL_ID},
        "modelProvider": "bedrock",
        "systemPrompt": "Help the customer with their request.",
    }
    body: dict = {
        "nodeId": f"node-{template_id}",
        "templateId": template_id,
        "config": config,
        "connectedTools": [],
        "gatewayTools": [],
    }
    if template_id == "mcp-server-runtime":
        body["config"] = {"name": "download-mcp-runtime", "protocol": "MCP", "enableOtel": False}
        return body
    if template_id in {
        "web-search-agent",
        "strands-gateway-agent",
        "customer-support-assistant",
        "customer-support-blueprint",
        "mcp-server-gateway-target",
    }:
        body["connectedTools"] = ["gateway"]
        body["gatewayConfig"] = {
            "name": "download-gateway",
            "targetType": "lambda",
            "targetConfig": {"type": "lambda"},
            "targets": [],
        }
    if template_id in {"customer-support-assistant", "customer-support-blueprint"}:
        body["connectedTools"].append("memory")
        body["memoryConfig"] = {"name": "download-memory", "enabled": True}
    if template_id == "mcp-server-gateway-target":
        body["mcpServerConfig"] = {
            "name": "download-mcp-server",
            "framework": "strands_agents",
            "systemPrompt": "Expose order tools.",
            "model": {"modelId": MODEL_ID},
            "tools": [],
        }
    return body


def _decode_response(response, artifact_type: str) -> bytes:
    assert response.status_code == 200, response.text[:1000]
    payload = response.json()
    archive = base64.b64decode(payload["zip_base64"], validate=True)
    assert payload["archive_sha256"] == hashlib.sha256(archive).hexdigest()
    assert payload["archive_bytes"] == len(archive)
    with zipfile.ZipFile(io.BytesIO(archive)) as artifact:
        manifests = [name for name in artifact.namelist() if name.endswith("/artifact-manifest.json")]
        assert len(manifests) == 1
        assert payload["artifact_manifest_sha256"] == hashlib.sha256(artifact.read(manifests[0])).hexdigest()
    verify_artifact_zip(archive, expected_artifact_type=artifact_type)
    return archive


def _write_verifier_and_extract(archive: bytes, tmp_path: Path, artifact_type: str) -> Path:
    archive_path = tmp_path / f"{artifact_type}.zip"
    archive_path.write_bytes(archive)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        verifier_name = next(name for name in zf.namelist() if name.endswith("/verify-artifact.py"))
        verifier = tmp_path / f"verify-{artifact_type}.py"
        verifier.write_bytes(zf.read(verifier_name))
    destination = tmp_path / f"extract-{artifact_type}"
    env = {
        "CI": "true",
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONHASHSEED": "0",
    }
    result = subprocess.run(
        [
            os.environ.get("PYTHON", "python3"),
            "-I",
            str(verifier),
            "--archive",
            str(archive_path),
            "--artifact-type",
            artifact_type,
            "--extract",
            str(destination),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    roots = list(destination.iterdir())
    assert len(roots) == 1 and roots[0].is_dir()
    return roots[0]


def _no_network_recipient_checks(root: Path, artifact_type: str, tmp_path: Path) -> None:
    env = {
        "CI": "true",
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONHASHSEED": "0",
    }
    verifier = subprocess.run(
        ["python3", "-I", "verify-artifact.py", "--directory", ".", "--artifact-type", artifact_type],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert verifier.returncode == 0, verifier.stdout + verifier.stderr
    for path in root.rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    for path in root.rglob("*.sh"):
        result = subprocess.run(
            ["bash", "-n", str(path)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"{path}: {result.stderr}"
    for path in root.rglob("*.yml"):
        yaml.safe_load(path.read_text(encoding="utf-8"))
    for path in root.rglob("buildspec.yml"):
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["version"] == 0.2
    if artifact_type == "cfn":
        assert yaml.compose((root / "template.yaml").read_text(encoding="utf-8")) is not None
    else:
        lock_check = subprocess.run(
            ["python3", "verify-lock.py", "--lock-only"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert lock_check.returncode == 0, lock_check.stdout + lock_check.stderr


@pytest.mark.parametrize(
    ("route", "artifact_type"),
    [("/api/generate-cfn-template", "cfn"), ("/api/export-python", "python")],
)
def test_clean_authenticated_recipient_starts_from_the_actual_endpoint(monkeypatch, tmp_path, route, artifact_type):
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    archive = _decode_response(_client().post(route, json=_gallery_body("strands-gateway-agent")), artifact_type)
    root = _write_verifier_and_extract(archive, tmp_path, artifact_type)
    _no_network_recipient_checks(root, artifact_type, tmp_path)

    ci_files = [
        path
        for path in root.rglob("*")
        if path.is_file() and ("ci/" in path.as_posix() or ".github/" in path.as_posix())
    ]
    assert ci_files
    for path in ci_files:
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text and "backend/src/" not in text and "from app." not in text


@pytest.mark.parametrize("template_id", GALLERY)
@pytest.mark.parametrize(
    ("route", "artifact_type"),
    [("/api/generate-cfn-template", "cfn"), ("/api/export-python", "python")],
)
def test_every_gallery_template_returns_a_verified_real_download(monkeypatch, route, artifact_type, template_id):
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    _decode_response(_client().post(route, json=_gallery_body(template_id)), artifact_type)


@pytest.mark.parametrize("provider", sorted(PROVIDER_PACKAGES))
def test_all_thirteen_model_providers_have_exact_runnable_export_roots(monkeypatch, provider):
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    body = _gallery_body("strands-gateway-agent")
    body["config"]["modelProvider"] = provider
    body["config"]["model"] = {"modelId": MODEL_ID if provider == "bedrock" else f"{provider}-model"}
    archive = _decode_response(_client().post("/api/export-python", json=body), "python")
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        requirements = zf.read(next(name for name in zf.namelist() if name.endswith("/requirements.txt"))).decode()
        source = zf.read(next(name for name in zf.namelist() if name.endswith("/agent.py"))).decode()
    assert "-c requirements.lock" in requirements
    assert all(line.startswith(("#", "-c ")) or "==" in line for line in requirements.splitlines() if line)
    compile(source, "agent.py", "exec")


def test_unsupported_provider_is_rejected_at_the_authenticated_api_boundary(monkeypatch):
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    body = _gallery_body("strands-gateway-agent")
    body["config"]["modelProvider"] = "unreviewed-provider"
    response = _client().post("/api/export-python", json=body)
    assert response.status_code == 422
    assert "modelProvider" in response.text
    assert "unreviewed-provider" not in response.text


def test_embedded_lock_is_byte_derived_from_the_committed_accepted_lock():
    source = Path(__file__).resolve().parents[1] / "agentcore-deps-constraints.txt"
    pins = [
        line.strip()
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    canonical = "\n".join(sorted(pins, key=str.casefold)) + "\n"
    assert accepted_lock_text() == canonical
    assert len(pins) == 169
    assert all(line.count("==") == 1 for line in pins)


def test_both_generators_are_byte_reproducible_for_identical_inputs():
    request = DeployRequest(
        nodeId="deterministic-node",
        config=RuntimeConfig(name="deterministic-agent", model={"modelId": MODEL_ID}),
    )
    cfn_one = CfnTemplateGenerator().generate(request).to_zip()
    cfn_two = CfnTemplateGenerator().generate(request).to_zip()
    python_one, _ = build_and_zip(request)
    python_two, _ = build_and_zip(request)
    assert cfn_one == cfn_two
    assert python_one == python_two


def _baseline_archive() -> bytes:
    request = DeployRequest(
        nodeId="mutation-node",
        config=RuntimeConfig(name="mutation-agent", model={"modelId": MODEL_ID}),
    )
    return build_and_zip(request)[0]


def _copy_info(
    info: zipfile.ZipInfo, *, name: str | None = None, mode: int | None = None, time=None
) -> zipfile.ZipInfo:
    copied = zipfile.ZipInfo(name or info.filename, date_time=time or info.date_time)
    copied.create_system = info.create_system
    copied.compress_type = info.compress_type
    copied.external_attr = (mode if mode is not None else info.external_attr >> 16) << 16
    copied.internal_attr = info.internal_attr
    copied.extra = info.extra
    copied.comment = info.comment
    return copied


def _mutate_archive(archive: bytes, mutation: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        rows = [(_copy_info(info), source.read(info)) for info in source.infolist()]
    prefix = rows[0][0].filename.split("/", 1)[0]

    if mutation == "tamper":
        index = next(i for i, (info, _data) in enumerate(rows) if info.filename.endswith("/agent.py"))
        rows[index] = (rows[index][0], rows[index][1] + b"# tampered\n")
    elif mutation == "duplicate":
        rows.append((_copy_info(rows[0][0]), rows[0][1]))
    elif mutation == "traversal":
        rows[0] = (_copy_info(rows[0][0], name=f"{prefix}/../escape"), rows[0][1])
    elif mutation == "absolute":
        rows[0] = (_copy_info(rows[0][0], name="/absolute"), rows[0][1])
    elif mutation == "device":
        rows[0] = (_copy_info(rows[0][0], name=f"{prefix}/CON.txt"), rows[0][1])
    elif mutation == "mode":
        rows[0] = (_copy_info(rows[0][0], mode=stat.S_IFLNK | 0o777), rows[0][1])
    elif mutation == "missing":
        rows.pop(next(i for i, (info, _data) in enumerate(rows) if info.filename.endswith("/README.md")))
    elif mutation == "extra":
        extra = zipfile.ZipInfo(f"{prefix}/unmanifested.txt", date_time=FIXED_ZIP_TIME)
        extra.create_system = 3
        extra.compress_type = zipfile.ZIP_STORED
        extra.external_attr = 0o100644 << 16
        rows.append((extra, b"extra\n"))
    elif mutation == "manifest":
        index = next(i for i, (info, _data) in enumerate(rows) if info.filename.endswith(f"/{ARTIFACT_MANIFEST}"))
        rows[index] = (rows[index][0], b"{}\n")
    elif mutation == "checksum":
        index = next(i for i, (info, _data) in enumerate(rows) if info.filename.endswith(f"/{ARTIFACT_CHECKSUMS}"))
        data = rows[index][1]
        rows[index] = (rows[index][0], (b"0" if data[:1] != b"0" else b"1") + data[1:])
    elif mutation == "comment":
        pass
    elif mutation == "timestamp":
        rows[0] = (_copy_info(rows[0][0], time=(2020, 1, 1, 0, 0, 0)), rows[0][1])
    elif mutation == "order":
        rows.reverse()
    else:  # pragma: no cover - test table owns values
        raise AssertionError(mutation)

    if mutation != "order":
        rows.sort(key=lambda row: row[0].filename)
    out = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as target:
            if mutation == "comment":
                target.comment = b"hidden metadata"
            for info, data in rows:
                target.writestr(info, data)
    return out.getvalue()


@pytest.mark.parametrize(
    "mutation",
    [
        "tamper",
        "duplicate",
        "traversal",
        "absolute",
        "device",
        "mode",
        "missing",
        "extra",
        "manifest",
        "checksum",
        "comment",
        "timestamp",
        "order",
    ],
)
def test_every_adversarial_archive_mutation_fails_closed(mutation):
    with pytest.raises(DownloadableArtifactError):
        verify_artifact_zip(_mutate_archive(_baseline_archive(), mutation), expected_artifact_type="python")


def test_portable_verifier_rejects_a_mutant(tmp_path):
    archive = _baseline_archive()
    mutant = _mutate_archive(archive, "traversal")
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        verifier_name = next(name for name in zf.namelist() if name.endswith("/verify-artifact.py"))
        verifier = tmp_path / "verify-artifact.py"
        verifier.write_bytes(zf.read(verifier_name))
    path = tmp_path / "mutant.zip"
    path.write_bytes(mutant)
    result = subprocess.run(
        ["python3", "-I", str(verifier), "--archive", str(path)],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "artifact verification failed" in result.stderr


def test_security_and_inertness_have_removal_controls():
    files = build_python_project(
        DeployRequest(nodeId="security-node", config=RuntimeConfig(name="security-agent", model={"modelId": MODEL_ID}))
    )
    launcher = files["run-docker.sh"]
    assert "-p 127.0.0.1:8080:8080" in launcher
    assert "-p 8080:8080" not in launcher

    def loopback_publish_is_safe(script: str) -> bool:
        return "-p 127.0.0.1:8080:8080" in script and "-p 8080:8080" not in script

    assert loopback_publish_is_safe(launcher)
    assert not loopback_publish_is_safe(launcher.replace("127.0.0.1:8080:8080", "8080:8080"))

    mcp_request = DeployRequest.model_validate(_gallery_body("mcp-server-runtime"))
    standalone_mcp = build_python_project(mcp_request)
    assert 'host=os.environ.get("MCP_HOST", "127.0.0.1")' in standalone_mcp["agent.py"]
    assert 'os.environ.get("PORT", "8080")' in standalone_mcp["agent.py"]
    assert 'host="0.0.0.0"' not in standalone_mcp["agent.py"]
    managed_mcp = CfnTemplateGenerator().generate(mcp_request).agent_code
    assert 'host="0.0.0.0"' in managed_mcp
    assert 'os.environ.get("PORT", "8000")' in managed_mcp

    assert "ALLOW_DEPLOY" not in files[".github/workflows/verify.yml"]
    assert "docker push" not in files[".github/workflows/verify.yml"]


def test_manifest_schema_and_checksum_file_are_strict_and_non_recursive():
    archive = _baseline_archive()
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        root = zf.namelist()[0].split("/", 1)[0]
        import json

        manifest = json.loads(zf.read(f"{root}/{ARTIFACT_MANIFEST}"))
        sums = zf.read(f"{root}/{ARTIFACT_CHECKSUMS}").decode("utf-8")
    assert set(manifest) == {"artifact_type", "files", "schema"}
    assert manifest["schema"] == "agentcore.downloadable-artifact/v1"
    assert all(set(row) == {"bytes", "mode", "path", "sha256", "type"} for row in manifest["files"])
    assert f"  {ARTIFACT_CHECKSUMS}\n" not in sums
    assert f"  {ARTIFACT_MANIFEST}\n" in sums


def test_container_base_is_immutable_and_cicd_is_inert_by_default():
    files = build_python_project(
        DeployRequest(nodeId="base-node", config=RuntimeConfig(name="base-agent", model={"modelId": MODEL_ID}))
    )
    from_line = next(line for line in files["Dockerfile"].splitlines() if line.startswith("FROM "))
    assert from_line == (
        "FROM docker.io/library/python:3.13.13-slim@"
        "sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286"
    )
    assert "default: false" in files[".github/workflows/verify.yml"]
    assert "docker push" not in files[".github/workflows/verify.yml"]

    cfn = CfnTemplateGenerator().generate(
        DeployRequest(nodeId="inert-node", config=RuntimeConfig(name="inert-agent", model={"modelId": MODEL_ID}))
    )
    with zipfile.ZipFile(io.BytesIO(cfn.to_zip())) as zf:
        root = zf.namelist()[0].split("/", 1)[0]
        workflow = zf.read(f"{root}/.github/workflows/deploy.yml").decode("utf-8")
        deploy = zf.read(f"{root}/ci/deploy.sh").decode("utf-8")
    assert "default: false" in workflow
    assert "${ALLOW_DEPLOY:-false}" in deploy
    assert deploy.index("./ci/verify.sh") < deploy.index("ALLOW_DEPLOY")
    for workflow_text in (workflow, files[".github/workflows/verify.yml"]):
        uses = [line.strip().split("@", 1)[1].split()[0] for line in workflow_text.splitlines() if "uses:" in line]
        assert uses and all(len(ref) == 40 and set(ref) <= set("0123456789abcdef") for ref in uses)


@pytest.mark.parametrize("artifact_type", ["cfn", "python"])
def test_every_shipped_shell_script_passes_shellcheck(artifact_type):
    import shutil

    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.fail("shellcheck is mandatory for downloadable artifacts")
    request = DeployRequest(nodeId="shell-node", config=RuntimeConfig(name="shell-agent", model={"modelId": MODEL_ID}))
    archive = CfnTemplateGenerator().generate(request).to_zip() if artifact_type == "cfn" else build_and_zip(request)[0]
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        infos = zf.infolist()
        scripts = [(info.filename, zf.read(info)) for info in infos if info.filename.endswith(".sh")]
        for info in infos:
            mode = (info.external_attr >> 16) & 0o777
            expected = 0o755 if info.filename.endswith(".sh") else 0o644
            assert mode == expected, f"{info.filename} has mode {mode:04o}, expected {expected:04o}"
    assert scripts
    for name, source in scripts:
        result = subprocess.run(
            [shellcheck, "--shell=bash", "-"],
            input=source,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, f"{name}:\n{result.stdout.decode()}{result.stderr.decode()}"


def test_portable_dependency_verifier_rejects_floating_and_drifted_roots(tmp_path):
    files = build_python_project(
        DeployRequest(nodeId="lock-node", config=RuntimeConfig(name="lock-agent", model={"modelId": MODEL_ID}))
    )
    for name in ("requirements.txt", "requirements.lock", "verify-lock.py"):
        (tmp_path / name).write_text(files[name], encoding="utf-8")
    env = {"HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def run():
        return subprocess.run(
            ["python3", "verify-lock.py", "--lock-only"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert run().returncode == 0
    requirements = (tmp_path / "requirements.txt").read_text(encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(requirements.replace("boto3==1.43.109", "boto3>=1.43.109"))
    assert run().returncode == 1
    (tmp_path / "requirements.txt").write_text(requirements)
    lock = (tmp_path / "requirements.lock").read_text(encoding="utf-8")
    (tmp_path / "requirements.lock").write_text(lock.replace("boto3==1.43.109\n", ""))
    assert run().returncode == 1


def _workflow_integrity_command(root: Path, workflow_name: str) -> str:
    workflow = yaml.safe_load((root / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8"))
    commands = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if str(step.get("name", "")).startswith("Verify externally anchored artifact integrity")
    ]
    assert commands
    assert len(set(commands)) == 1
    return commands[0]


def test_exact_workflow_integrity_command_passes_in_a_real_git_checkout_and_remains_strict(tmp_path):
    root = _write_verifier_and_extract(_baseline_archive(), tmp_path, "python")
    command = _workflow_integrity_command(root, "verify.yml")
    assert command == (
        "python3 -I verify-integrity.py\npython3 -I verify-artifact.py --directory . --artifact-type python\n"
    )
    expected_manifest = hashlib.sha256((root / "artifact-manifest.json").read_bytes()).hexdigest()

    git_env = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(tmp_path / "git-home"),
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "EXPECTED_ARTIFACT_MANIFEST_SHA256": expected_manifest,
    }
    Path(git_env["HOME"]).mkdir()
    for args in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.name", "Artifact Test"],
        ["git", "config", "user.email", "artifact-test@example.invalid"],
        ["git", "add", "--all"],
        ["git", "commit", "--quiet", "-m", "artifact fixture"],
    ):
        subprocess.run(args, cwd=root, env=git_env, check=True, capture_output=True, timeout=20)

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", "clone", "--quiet", str(root), str(checkout)],
        env=git_env,
        check=True,
        capture_output=True,
        timeout=20,
    )

    def verify():
        return subprocess.run(
            ["bash", "-c", command],
            cwd=checkout,
            env={**git_env, "CI": "true", "PYTHONHASHSEED": "0"},
            capture_output=True,
            text=True,
            timeout=20,
        )

    assert verify().returncode == 0
    for relative in ("unmanifested.txt", ".unmanifested"):
        mutant = checkout / relative
        mutant.write_text("must fail\n", encoding="utf-8")
        result = verify()
        assert result.returncode == 1
        assert "missing or extra artifact members" in result.stderr
        mutant.unlink()

    vcs_link = checkout / ".git" / "untrusted-link"
    vcs_link.symlink_to("../README.md")
    result = verify()
    assert result.returncode == 1
    assert "directory contains symlink" in result.stderr


def test_rich_cfn_ci_delegates_once_without_inline_template_validation(tmp_path):
    from tests.test_cfn_export_contract import COMPONENT_COMBINATIONS, _generate

    bundle = _generate(**COMPONENT_COMBINATIONS["everything"])
    assert len(bundle.template_yaml.encode("utf-8")) > 51_200
    root = _write_verifier_and_extract(bundle.to_zip(), tmp_path, "cfn")
    script = (root / "ci" / "deploy.sh").read_text(encoding="utf-8")
    assert "validate-template" not in script
    assert "--template-body" not in script
    assert script.count("./deploy.sh") == 1

    verify_log = tmp_path / "verify.log"
    deploy_log = tmp_path / "deploy.log"
    aws_log = tmp_path / "aws.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_aws = fake_bin / "aws"
    fake_aws.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$AWS_LOG"\nexit 97\n', encoding="utf-8")
    fake_aws.chmod(0o755)
    (root / "ci" / "verify.sh").write_text(
        '#!/usr/bin/env bash\nprintf "verified\\n" >> "$VERIFY_LOG"\n', encoding="utf-8"
    )
    (root / "ci" / "verify.sh").chmod(0o755)
    (root / "deploy.sh").write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$DEPLOY_LOG"\n', encoding="utf-8")
    (root / "deploy.sh").chmod(0o755)
    base_env = {
        "AWS_LOG": str(aws_log),
        "DEPLOY_LOG": str(deploy_log),
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "VERIFY_LOG": str(verify_log),
    }

    disabled = subprocess.run(
        ["bash", "ci/deploy.sh"],
        cwd=root,
        env={**base_env, "ALLOW_DEPLOY": "false"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert disabled.returncode == 2
    assert not deploy_log.exists()
    assert not aws_log.exists()

    enabled = subprocess.run(
        ["bash", "ci/deploy.sh"],
        cwd=root,
        env={
            **base_env,
            "ALLOW_DEPLOY": "true",
            "ARTIFACTS_BUCKET": "artifact-bucket",
            "AWS_REGION": "us-east-1",
            "STACK_NAME": "rich-stack",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert enabled.returncode == 0, enabled.stdout + enabled.stderr
    assert deploy_log.read_text(encoding="utf-8").splitlines() == ["rich-stack us-east-1 artifact-bucket"]
    assert not aws_log.exists(), "ci/deploy.sh called AWS outside the one authoritative deploy.sh delegation"


@pytest.mark.parametrize("failure", ["missing", "incompatible"])
def test_real_pip_check_rejects_broken_transitive_dependencies(tmp_path, failure):
    import shutil

    python313 = shutil.which("python3.13")
    if python313 is None:
        pytest.fail("Python 3.13 is required for the exported dependency contract")
    venv = tmp_path / "venv"
    subprocess.run([python313, "-m", "venv", str(venv)], check=True, capture_output=True, timeout=30)
    python = venv / "bin" / "python"
    purelib = Path(
        subprocess.run(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    )
    parent = purelib / "parent_fixture-1.0.dist-info"
    parent.mkdir()
    parent.joinpath("METADATA").write_text(
        "Metadata-Version: 2.1\nName: parent-fixture\nVersion: 1.0\nRequires-Dist: child-fixture>=2\n",
        encoding="utf-8",
    )
    if failure == "incompatible":
        child = purelib / "child_fixture-1.0.dist-info"
        child.mkdir()
        child.joinpath("METADATA").write_text(
            "Metadata-Version: 2.1\nName: child-fixture\nVersion: 1.0\n",
            encoding="utf-8",
        )

    files = build_python_project(
        DeployRequest(nodeId="pip-check-node", config=RuntimeConfig(name="pip-check", model={"modelId": MODEL_ID}))
    )
    (tmp_path / "check-installed.sh").write_text(files["check-installed.sh"], encoding="utf-8")
    result = subprocess.run(
        ["bash", "check-installed.sh", str(python)],
        cwd=tmp_path,
        env={"HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert "pip dependency compatibility check failed" in result.stderr
    assert "parent-fixture" in result.stderr.lower()


def test_pip_check_diagnostics_redact_urls_and_credential_labels(tmp_path):
    files = build_python_project(
        DeployRequest(nodeId="redaction-node", config=RuntimeConfig(name="redaction", model={"modelId": MODEL_ID}))
    )
    (tmp_path / "check-installed.sh").write_text(files["check-installed.sh"], encoding="utf-8")
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ] && [ "$3" = "check" ]; then\n'
        "  echo 'broken https://user:password@example.invalid/path token=top-secret' >&2\n"
        "  exit 1\n"
        "fi\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    result = subprocess.run(
        ["bash", "check-installed.sh", str(fake_python)],
        cwd=tmp_path,
        env={"HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "[url-redacted]" in result.stderr
    assert "password" not in result.stderr
    assert "top-secret" not in result.stderr


def test_pip_check_runs_after_install_and_again_before_ci_runtime_checks():
    files = build_python_project(
        DeployRequest(nodeId="order-node", config=RuntimeConfig(name="order", model={"modelId": MODEL_ID}))
    )
    installer = files["install-locked.sh"]
    ci = files["ci/verify.sh"]
    dockerfile = files["Dockerfile"]
    assert installer.index("pip install") < installer.index("check-installed.sh")
    assert ci.index("./install-locked.sh") < ci.index("bash check-installed.sh") < ci.index("compile(")
    assert dockerfile.index("pip install") < dockerfile.index("bash check-installed.sh")
    assert '"$python_bin" -m pip check' in files["check-installed.sh"]
