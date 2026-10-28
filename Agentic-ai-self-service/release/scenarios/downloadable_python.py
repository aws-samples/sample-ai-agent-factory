"""Downloadable assets, Python: the customer's download-to-running-agent path, per Region.

``ui-authenticated-python-download-clean-recipient-lock-install-compile-start-invoke``
(``live_region`` of ``delivery.downloadable_assets``). A synthetic user authenticates the
way the UI does (Amplify USER_SRP_AUTH), calls ``POST /api/export-python`` through the
deployed CloudFront URL with a gallery template's own request body, and then does what the
README tells a recipient to do -- in a clean directory with nothing but the download:

1. The response carries exactly ``download_url`` or ``zip_base64`` plus ``archive_sha256``,
   ``archive_bytes`` and ``filename``; a presigned URL must be HTTPS to this Region's S3
   endpoint (the same checks ``frontend/src/utils/downloadArtifact.ts`` makes).
2. The bytes are fetched without credentials and must match ``archive_bytes`` and
   ``archive_sha256`` before anything is written.
3. ``python3 -I verify-artifact.py --archive ... --artifact-type python --extract`` and then
   ``--directory .`` from the artifact root, exactly as documented.
4. ``./install-locked.sh .venv`` with a Python 3.13 interpreter -- the bundle's own
   integrity, lock and ``pip check`` gates -- then every shipped module is byte-compiled.
5. ``agent.py`` is started on 127.0.0.1:8080 with the recipient's own AWS credentials,
   ``/ping`` must answer and ``POST /invocations`` must return a model-backed response.

Everything runs under the recipient directory, which is removed at the end; a listener
left behind or a user not deleted fails the scenario.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from _live import (
    BACKEND,
    ScenarioBlocked,
    ScenarioFailed,
    bound_deployment,
    redact,
    with_synthetic_user,
    write_json,
)

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.zip")
GALLERY_TEMPLATE = "strands-gateway-agent"


def _python313() -> Path:
    for candidate in ("python3.13", "/opt/homebrew/bin/python3.13", "/usr/local/bin/python3.13"):
        found = (
            shutil.which(candidate)
            if not candidate.startswith("/")
            else (candidate if Path(candidate).exists() else None)
        )
        if found:
            version = subprocess.run(
                [found, "-c", "import sys; print(sys.version_info[:2])"], capture_output=True, text=True
            )
            if version.stdout.strip() == "(3, 13)":
                return Path(found)
    raise ScenarioBlocked(
        "a Python 3.13 interpreter is required for the recipient install (install-locked.sh demands it)"
    )


def _template_request_body(template_id: str) -> dict:
    """The exact gallery request the integration suite (and the UI) send for this template."""
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from tests.integration.test_template_deployments import TEMPLATE_CASES, _template_request_payload

    case = next(item for item in TEMPLATE_CASES if item.template_id == template_id)
    payload = _template_request_payload(case, uuid.uuid4().hex[:16])
    payload.pop("deploymentMode", None)
    return payload


def _api_post(cloudfront_url: str, path: str, token: str, body: dict) -> tuple[int, dict | str]:
    request = urllib.request.Request(
        f"{cloudfront_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310 -- https CloudFront URL from the stack
            text = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", "replace")
        status = error.code
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


def _validate_metadata(record: dict, region: str) -> dict:
    keys = set(record)
    has_url, has_b64 = "download_url" in keys, "zip_base64" in keys
    if has_url == has_b64:
        raise ScenarioFailed("export response must carry exactly one of download_url / zip_base64")
    source_key = "download_url" if has_url else "zip_base64"
    if keys != {"archive_sha256", "archive_bytes", "filename", source_key}:
        raise ScenarioFailed(f"export response has unexpected keys: {sorted(keys)}")
    if not isinstance(record["archive_sha256"], str) or not SHA256_HEX.fullmatch(record["archive_sha256"]):
        raise ScenarioFailed("archive_sha256 is not a lowercase SHA-256 hex digest")
    size = record["archive_bytes"]
    if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_ARCHIVE_BYTES:
        raise ScenarioFailed("archive_bytes is not a bounded positive integer")
    if not isinstance(record["filename"], str) or not FILENAME.fullmatch(record["filename"]):
        raise ScenarioFailed("filename is not a plain .zip name")
    if has_url:
        url = urllib.parse.urlsplit(record["download_url"])
        host = url.hostname or ""
        if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
            raise ScenarioFailed("download_url is not a plain HTTPS URL")
        if not (host.endswith(f".s3.{region}.amazonaws.com") or host == f"s3.{region}.amazonaws.com"):
            raise ScenarioFailed(f"download_url host {host} is not this Region's S3 endpoint")
        query = urllib.parse.parse_qs(url.query)
        if query.get("X-Amz-Algorithm") != ["AWS4-HMAC-SHA256"] or "X-Amz-Signature" not in query:
            raise ScenarioFailed("download_url is not a SigV4 presigned URL")
    return record


def _fetch_bytes(record: dict) -> bytes:
    if "zip_base64" in record:
        import base64

        data = base64.b64decode(record["zip_base64"], validate=True)
    else:
        request = urllib.request.Request(record["download_url"], method="GET", headers={"Accept": "application/zip"})
        with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310 -- validated presigned URL
            data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) != record["archive_bytes"]:
        raise ScenarioFailed(f"archive is {len(data)} bytes, response said {record['archive_bytes']}")
    if hashlib.sha256(data).hexdigest() != record["archive_sha256"]:
        raise ScenarioFailed("archive SHA-256 does not match the response")
    return data


def _run(
    cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: int, log: Path, label: str
) -> subprocess.CompletedProcess:
    completed = subprocess.run(cmd, cwd=cwd, env=env, check=False, capture_output=True, text=True, timeout=timeout)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ [{label}] {' '.join(cmd)}\n(exit {completed.returncode})\n")
        handle.write(redact(completed.stdout[-20_000:]) + "\n" + redact(completed.stderr[-20_000:]) + "\n")
    if completed.returncode != 0:
        raise ScenarioFailed(f"{label} exited {completed.returncode}: {redact(completed.stderr.strip()[-600:])}")
    return completed


def _recipient_env(region: str) -> dict[str, str]:
    """A recipient's environment: PATH-class names plus the AWS file/profile names the broker set."""
    env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "TMPDIR") if name in os.environ}
    for name in (
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_EC2_METADATA_DISABLED",
    ):
        if name in os.environ:
            env[name] = os.environ[name]
    env.update({"AWS_REGION": region, "AWS_DEFAULT_REGION": region, "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"})
    return env


def _wait_for_listener(process: subprocess.Popen, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ScenarioFailed(f"agent.py exited early with {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.2)
    raise ScenarioFailed(f"agent.py did not listen on 127.0.0.1:{port} within {int(timeout)}s")


def _http(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=240)
    try:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


def ui_authenticated_python_download_clean_recipient(args: argparse.Namespace, artifact_dir: Path) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    python313 = _python313()
    log = artifact_dir / "recipient.log"
    report: dict = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "template": GALLERY_TEMPLATE,
        "customer_path": observed.cloudfront_url,
        "deployment_binding": comparison,
        "steps": [],
    }
    recipient_root = Path(tempfile.mkdtemp(prefix="acfpr-recipient-", dir=os.environ.get("TMPDIR")))
    agent: subprocess.Popen | None = None
    try:
        with with_synthetic_user(observed, label="pydl") as user:
            body = _template_request_body(GALLERY_TEMPLATE)
            status, response = _api_post(observed.cloudfront_url, "/api/export-python", user.token, body)
            if status != 200 or not isinstance(response, dict):
                raise ScenarioFailed(
                    f"POST /api/export-python returned {status}: {redact(str(response)[:400], user.token)}"
                )
            record = _validate_metadata(response, binding.region)
            report["steps"].append(
                {
                    "step": "export",
                    "status": status,
                    "source": "download_url" if "download_url" in record else "zip_base64",
                }
            )
            data = _fetch_bytes(record)
        archive = recipient_root / record["filename"]
        archive.write_bytes(data)
        report["archive"] = {"filename": record["filename"], "bytes": len(data), "sha256": record["archive_sha256"]}
        report["steps"].append({"step": "download-verified", "bytes": len(data)})

        # The shipped verifier, under -I, exactly as the README instructs: archive, then directory.
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as zipped:
            names = zipped.namelist()
            verifier = next((n for n in names if n.endswith("/verify-artifact.py") and n.count("/") == 1), None)
            manifests = [n for n in names if n.endswith("/artifact-manifest.json")]
            if verifier is None or len(manifests) != 1:
                raise ScenarioFailed("archive lacks a unique root verify-artifact.py / artifact-manifest.json")
            (recipient_root / "verify-artifact.py").write_bytes(zipped.read(verifier))
            manifest_sha256 = hashlib.sha256(zipped.read(manifests[0])).hexdigest()
        env = _recipient_env(binding.region)
        extracted = recipient_root / "verified-python"
        _run(
            [
                str(python313),
                "-I",
                "verify-artifact.py",
                "--archive",
                archive.name,
                "--artifact-type",
                "python",
                "--extract",
                extracted.name,
            ],
            cwd=recipient_root,
            env=env,
            timeout=300,
            log=log,
            label="verify-archive",
        )
        roots = [p for p in extracted.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise ScenarioFailed(f"extraction produced {len(roots)} roots, expected one artifact root")
        root = roots[0]
        _run(
            [str(python313), "-I", "verify-artifact.py", "--directory", ".", "--artifact-type", "python"],
            cwd=root,
            env=env,
            timeout=300,
            log=log,
            label="verify-directory",
        )
        report["steps"].append({"step": "verifier", "artifact_root": root.name, "manifest_sha256": manifest_sha256})

        # The bundle's own recipient workflow: install-locked.sh (integrity, lock, venv, pip check).
        _run(
            ["bash", "install-locked.sh", ".venv"],
            cwd=root,
            env={**env, "PYTHON_BIN": str(python313), "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
            timeout=1500,
            log=log,
            label="install-locked",
        )
        venv_python = root / ".venv" / "bin" / "python"
        modules = sorted(p.name for p in root.glob("*.py"))
        _run([str(venv_python), "-m", "py_compile", *modules], cwd=root, env=env, timeout=300, log=log, label="compile")
        report["steps"].append({"step": "install-and-compile", "modules": modules})

        # Start the real generated agent on loopback and invoke it.
        port = 8080
        agent = subprocess.Popen(
            [str(venv_python), "agent.py"],
            cwd=root,
            env={**env, "PYTHONUNBUFFERED": "1"},
            stdout=subprocess.DEVNULL,
            stderr=open(artifact_dir / "agent-stderr.log", "w", encoding="utf-8"),  # noqa: SIM115
            text=True,
        )
        _wait_for_listener(agent, port, 60)
        ping_status, ping_body = _http(port, "GET", "/ping")
        if ping_status != 200:
            raise ScenarioFailed(f"/ping returned {ping_status}: {ping_body[:200]}")
        prompt = "In one sentence, what is Amazon Bedrock AgentCore Runtime?"
        invoke_status, invoke_body = _http(port, "POST", "/invocations", {"prompt": prompt})
        if invoke_status != 200:
            raise ScenarioFailed(f"/invocations returned {invoke_status}: {invoke_body[:400]}")
        lowered = invoke_body.lower()
        if "agentcore" not in lowered and "bedrock" not in lowered and "runtime" not in lowered:
            raise ScenarioFailed(f"/invocations answer does not address the prompt: {invoke_body[:300]}")
        report["steps"].append(
            {
                "step": "start-and-invoke",
                "ping": ping_status,
                "invocations": invoke_status,
                "response_sha256": hashlib.sha256(invoke_body.encode()).hexdigest(),
                "response_bytes": len(invoke_body),
            }
        )
        write_json(
            artifact_dir / "invocation-response",
            {"prompt": prompt, "status": invoke_status, "body_excerpt": invoke_body[:1000]},
        )
    finally:
        if agent is not None and agent.poll() is None:
            agent.terminate()
            try:
                agent.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait(timeout=10)
        listener_gone = True
        try:
            with socket.create_connection(("127.0.0.1", 8080), timeout=0.25):
                listener_gone = False
        except OSError:
            pass
        shutil.rmtree(recipient_root, ignore_errors=True)
        report["cleanup"] = {"recipient_removed": not recipient_root.exists(), "listener_gone": listener_gone}
        write_json(artifact_dir / "report", report)
        if not listener_gone or recipient_root.exists():
            raise ScenarioFailed("recipient cleanup incomplete")
    from _live import write_provenance  # provenance already written by bound_deployment; refresh with archive digest

    write_provenance(
        artifact_dir,
        binding,
        observed,
        archive_sha256=record["archive_sha256"],
        artifact_manifest_sha256=manifest_sha256,
    )
    return report
