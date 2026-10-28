"""Shared recipient-side helpers for the downloadable-asset scenarios.

The customer's path is the same for both artifact kinds up to the point where the bundle is
used: an SRP-authenticated export call through CloudFront, a response that must carry exactly
the integrity anchors the shipped UI parser accepts, bytes fetched without credentials and
verified before any write, the shipped ``verify-artifact.py`` run under ``python3 -I`` on the
archive and then on the extracted root, and ``EXPECTED_ARTIFACT_MANIFEST_SHA256`` exported from
the authenticated response before any bundle script runs.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

from _live import BACKEND, ScenarioBlocked, ScenarioFailed, redact

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.zip")
RESPONSE_KEYS = frozenset({"archive_sha256", "archive_bytes", "artifact_manifest_sha256", "filename"})


def python313() -> Path:
    """The recipient's Python 3.13 (install-locked.sh and the bundle README demand 3.13)."""
    for candidate in ("python3.13", "/opt/homebrew/bin/python3.13", "/usr/local/bin/python3.13"):
        found = (
            shutil.which(candidate)
            if not candidate.startswith("/")
            else (candidate if Path(candidate).exists() else None)
        )
        if found:
            version = subprocess.run(
                [found, "-c", "import sys; print(sys.version_info[:2])"], capture_output=True, text=True, timeout=30
            )
            if version.stdout.strip() == "(3, 13)":
                return Path(found)
    raise ScenarioBlocked("a Python 3.13 interpreter is required for the recipient steps")


def template_request_body(template_id: str, *, suffix: str | None = None, system_prompt: str | None = None) -> dict:
    """The exact gallery request the integration suite (and the UI) send for this template.

    ``suffix`` fixes the node id (and so the deployment/stack name) so a second export of the
    same template is an UPDATE of the same stack; ``system_prompt`` changes the agent's
    behaviour between versions so an invocation can tell which version answered.
    """
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from tests.integration.test_template_deployments import TEMPLATE_CASES, _template_request_payload

    case = next(item for item in TEMPLATE_CASES if item.template_id == template_id)
    payload = _template_request_payload(case, suffix or uuid.uuid4().hex[:16])
    payload.pop("deploymentMode", None)
    if system_prompt is not None:
        payload["config"]["systemPrompt"] = system_prompt
    return payload


def api_post(cloudfront_url: str, path: str, token: str, body: dict) -> tuple[int, dict | str]:
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


def export_archive(cloudfront_url: str, path: str, token: str, body: dict, region: str) -> tuple[dict, bytes]:
    """Call the export endpoint and return the validated metadata and the verified archive bytes."""
    status, response = api_post(cloudfront_url, path, token, body)
    if status != 200 or not isinstance(response, dict):
        raise ScenarioFailed(f"POST {path} returned {status}: {redact(str(response)[:400], token)}")
    record = validate_metadata(response, region)
    return record, fetch_bytes(record)


def validate_metadata(record: dict, region: str) -> dict:
    """Exactly the contract the shipped UI parser (downloadArtifact.ts) enforces."""
    keys = set(record)
    has_url, has_b64 = "download_url" in keys, "zip_base64" in keys
    if has_url == has_b64:
        raise ScenarioFailed("export response must carry exactly one of download_url / zip_base64")
    source_key = "download_url" if has_url else "zip_base64"
    if keys != RESPONSE_KEYS | {source_key}:
        raise ScenarioFailed(f"export response has unexpected keys: {sorted(keys)}")
    for digest_key in ("archive_sha256", "artifact_manifest_sha256"):
        if not isinstance(record[digest_key], str) or not SHA256_HEX.fullmatch(record[digest_key]):
            raise ScenarioFailed(f"{digest_key} is not a lowercase SHA-256 hex digest")
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


def fetch_bytes(record: dict) -> bytes:
    """Fetch the archive without credentials; size and digest must match before any write."""
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


def run_logged(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    log: Path,
    label: str,
    secrets: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    """Run a recipient command, append its redacted output to ``log``, fail the scenario on non-zero exit."""
    completed = subprocess.run(cmd, cwd=cwd, env=env, check=False, capture_output=True, text=True, timeout=timeout)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ [{label}] {' '.join(cmd)}\n(exit {completed.returncode})\n")
        handle.write(
            redact(completed.stdout[-20_000:], *secrets) + "\n" + redact(completed.stderr[-20_000:], *secrets) + "\n"
        )
    if completed.returncode != 0:
        raise ScenarioFailed(
            f"{label} exited {completed.returncode}: {redact(completed.stderr.strip()[-600:], *secrets)}"
        )
    return completed


def recipient_env(region: str) -> dict[str, str]:
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


def anchored_env(env: dict[str, str], record: dict, **extra: str) -> dict[str, str]:
    """The recipient environment with the external manifest anchor every bundle script requires."""
    return {**env, "EXPECTED_ARTIFACT_MANIFEST_SHA256": record["artifact_manifest_sha256"], **extra}


def verify_and_extract(
    data: bytes,
    record: dict,
    *,
    artifact_type: str,
    recipient_root: Path,
    python: Path,
    env: dict[str, str],
    log: Path,
) -> tuple[Path, str]:
    """Run the shipped verifier exactly as documented; return (artifact root, manifest sha256).

    The manifest digest is re-derived from the archive and must equal the response's
    ``artifact_manifest_sha256``; the extracted root is verified again from inside it.
    """
    archive = recipient_root / record["filename"]
    recipient_root.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        names = zipped.namelist()
        verifier = next((n for n in names if n.endswith("/verify-artifact.py") and n.count("/") == 1), None)
        manifests = [n for n in names if n.endswith("/artifact-manifest.json")]
        if verifier is None or len(manifests) != 1:
            raise ScenarioFailed("archive lacks a unique root verify-artifact.py / artifact-manifest.json")
        (recipient_root / "verify-artifact.py").write_bytes(zipped.read(verifier))
        manifest_sha256 = hashlib.sha256(zipped.read(manifests[0])).hexdigest()
    if manifest_sha256 != record["artifact_manifest_sha256"]:
        raise ScenarioFailed("the archive's artifact-manifest.json does not match artifact_manifest_sha256")
    extracted = recipient_root / f"verified-{artifact_type}"
    run_logged(
        [
            str(python),
            "-I",
            "verify-artifact.py",
            "--archive",
            archive.name,
            "--artifact-type",
            artifact_type,
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
    run_logged(
        [str(python), "-I", "verify-artifact.py", "--directory", ".", "--artifact-type", artifact_type],
        cwd=root,
        env=env,
        timeout=300,
        log=log,
        label="verify-directory",
    )
    return root, manifest_sha256
