"""Downloadable assets, Python: the customer's download-to-running-agent path, per Region.

``ui-authenticated-python-download-clean-recipient-lock-install-compile-start-invoke``
(``live_region`` of ``delivery.downloadable_assets``). A synthetic user authenticates the
way the UI does (Amplify USER_SRP_AUTH), calls ``POST /api/export-python`` through the
deployed CloudFront URL with a gallery template's own request body, and then does what the
README tells a recipient to do -- in a clean directory with nothing but the download:

1. The response carries exactly the integrity anchors the shipped UI parser accepts; a
   presigned URL must be HTTPS to this Region's S3 endpoint.
2. The bytes are fetched without credentials and must match ``archive_bytes`` and
   ``archive_sha256`` before anything is written.
3. ``python3 -I verify-artifact.py`` on the archive (extract) and on the extracted root.
4. ``install-locked.sh`` REFUSES without ``EXPECTED_ARTIFACT_MANIFEST_SHA256`` and creates no
   environment; with the digest from the response it installs into a fresh venv (the bundle's
   own integrity, lock and ``pip check`` gates), then every shipped module is byte-compiled.
5. ``run.sh`` starts the real generated agent on 127.0.0.1:8080 with the recipient's own AWS
   credentials; ``/ping`` must answer and ``POST /invocations`` must return a model-backed
   response that addresses the prompt.

The recipient directory is removed at the end; a listener left behind, a synthetic user not
deleted, or any step failing fails the scenario.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from _download import (
    anchored_env,
    export_archive,
    python313,
    recipient_env,
    run_logged,
    template_request_body,
    verify_and_extract,
)
from _live import ScenarioFailed, bound_deployment, with_synthetic_user, write_json, write_provenance

GALLERY_TEMPLATE = "strands-gateway-agent"
PORT = 8080


def _wait_for_listener(process: subprocess.Popen, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ScenarioFailed(f"run.sh exited early with {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.2)
    raise ScenarioFailed(f"the agent did not listen on 127.0.0.1:{port} within {int(timeout)}s")


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


def _listener_present(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def ui_authenticated_python_download_clean_recipient(args: argparse.Namespace, artifact_dir: Path) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    python = python313()
    log = artifact_dir / "recipient.log"
    report: dict = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "template": GALLERY_TEMPLATE,
        "customer_path": observed.cloudfront_url,
        "deployment_binding": comparison,
        "steps": [],
    }
    if _listener_present(PORT):
        raise ScenarioFailed(f"127.0.0.1:{PORT} is already in use; the recipient start cannot be attributed")
    recipient_root = Path(tempfile.mkdtemp(prefix="acfpr-recipient-", dir=os.environ.get("TMPDIR")))
    agent: subprocess.Popen | None = None
    record: dict = {}
    manifest_sha256 = ""
    try:
        with with_synthetic_user(observed, label="pydl") as user:
            body = template_request_body(GALLERY_TEMPLATE)
            record, data = export_archive(
                observed.cloudfront_url, "/api/export-python", user.token, body, binding.region
            )
        report["steps"].append(
            {"step": "export", "source": "download_url" if "download_url" in record else "zip_base64"}
        )
        report["archive"] = {"filename": record["filename"], "bytes": len(data), "sha256": record["archive_sha256"]}
        report["steps"].append({"step": "download-verified", "bytes": len(data)})

        env = recipient_env(binding.region)
        root, manifest_sha256 = verify_and_extract(
            data, record, artifact_type="python", recipient_root=recipient_root, python=python, env=env, log=log
        )
        report["steps"].append({"step": "verifier", "artifact_root": root.name, "manifest_sha256": manifest_sha256})

        # Every shipped script runs verify-integrity.py, which refuses to proceed unless the
        # external anchor names the manifest digest: prove the refusal, then supply the digest.
        unanchored = subprocess.run(
            ["bash", "install-locked.sh", ".venv-unanchored"],
            cwd=root,
            env={**env, "PYTHON_BIN": str(python)},
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        refused = unanchored.returncode != 0 and "EXPECTED_ARTIFACT_MANIFEST_SHA256" in (
            unanchored.stderr + unanchored.stdout
        )
        if not refused:
            raise ScenarioFailed("install-locked.sh ran without the external manifest anchor; it must refuse")
        if (root / ".venv-unanchored").exists():
            raise ScenarioFailed("install-locked.sh created an environment before the integrity check")
        report["steps"].append({"step": "install-refused-without-anchor", "exit": unanchored.returncode})

        env_anchored = anchored_env(env, record, PYTHON_BIN=str(python), PIP_DISABLE_PIP_VERSION_CHECK="1")
        run_logged(
            ["bash", "install-locked.sh", ".venv"],
            cwd=root,
            env=env_anchored,
            timeout=1500,
            log=log,
            label="install-locked",
        )
        venv_python = root / ".venv" / "bin" / "python"
        modules = sorted(p.name for p in root.glob("*.py"))
        run_logged(
            [str(venv_python), "-m", "py_compile", *modules], cwd=root, env=env, timeout=300, log=log, label="compile"
        )
        report["steps"].append({"step": "install-and-compile", "modules": modules})

        # The bundle's own run.sh: verify-integrity.py under the anchor, then exec .venv/bin/python agent.py.
        agent = subprocess.Popen(
            ["bash", "run.sh"],
            cwd=root,
            env={**env_anchored, "PYTHONUNBUFFERED": "1"},
            stdout=subprocess.DEVNULL,
            stderr=(artifact_dir / "agent-stderr.log").open("w", encoding="utf-8"),
            text=True,
        )
        _wait_for_listener(agent, PORT, 60)
        ping_status, ping_body = _http(PORT, "GET", "/ping")
        if ping_status != 200:
            raise ScenarioFailed(f"/ping returned {ping_status}: {ping_body[:200]}")
        prompt = "In one sentence, what is Amazon Bedrock AgentCore Runtime?"
        invoke_status, invoke_body = _http(PORT, "POST", "/invocations", {"prompt": prompt})
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
        listener_gone = not _listener_present(PORT)
        shutil.rmtree(recipient_root, ignore_errors=True)
        report["cleanup"] = {"recipient_removed": not recipient_root.exists(), "listener_gone": listener_gone}
        write_json(artifact_dir / "report", report)
        if not listener_gone or recipient_root.exists():
            raise ScenarioFailed("recipient cleanup incomplete")
    write_provenance(
        artifact_dir,
        binding,
        observed,
        archive_sha256=record["archive_sha256"],
        artifact_manifest_sha256=manifest_sha256,
    )
    return report
