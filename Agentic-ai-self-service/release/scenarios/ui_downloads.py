"""``delivery.downloadable_assets`` — the ``ui_cloudfront`` facet.

* **deployed-cloudfront-ui-authenticated-cfn-python-downloads-verified** — headless Chromium
  (Playwright pinned by docs-site, driven by ``ui_downloads_driver.mjs``) signs in through the
  EXACT deployed CloudFront UI's Amplify form (the UI mints the session; nothing is injected into
  storage), seeds the canvas through the product's own template gallery, opens the deploy panel
  and clicks ``Download CloudFormation Template`` and ``Export as Python``. For each artifact:

  - the bytes the browser saved pass the shipped ``verify-artifact.py`` exactly as a recipient
    runs it (``_download.verify_and_extract``: archive, re-derived manifest digest, extracted root);
  - the receipt the UI rendered after its own in-browser SHA-256 check equals the digest of the
    saved bytes, and the rendered ``EXPECTED_ARTIFACT_MANIFEST_SHA256`` equals the digest the
    verifier re-derived from the archive's ``artifact-manifest.json``;
  - the UI's export call to the authenticated API returned 200 (observed status only).

The driver never echoes the password or any JWT; the journey writes ``journey-outcome.json``,
``browser-downloads.json`` and the saved archives under the artifact directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from _download import python313, recipient_env, verify_and_extract
from _live import (
    PROJECT_ROOT,
    REQUIRED_USER_GROUPS,
    ObservedStack,
    RunBinding,
    ScenarioBlocked,
    ScenarioFailed,
    bound_deployment,
    family_journey,
    redact,
    with_synthetic_user,
    write_json,
)

FAMILY = "ui-downloads"
SCENARIO_ID = "deployed-cloudfront-ui-authenticated-cfn-python-downloads-verified"
DRIVER = Path(__file__).with_name("ui_downloads_driver.mjs")
PLAYWRIGHT_PACKAGE_JSON = PROJECT_ROOT.parent / "docs-site" / "package.json"
EXPORT_ROUTES = {"cfn": "/api/generate-cfn-template", "python": "/api/export-python"}


def _playwright_module() -> Path:
    if not PLAYWRIGHT_PACKAGE_JSON.is_file():
        raise ScenarioBlocked("docs-site/package.json (pinned Playwright) is not available")
    probe = subprocess.run(
        ["node", "-e", "const p=require('playwright');console.log(p.chromium.executablePath())"],
        cwd=PLAYWRIGHT_PACKAGE_JSON.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0 or not probe.stdout.strip() or not Path(probe.stdout.strip()).exists():
        raise ScenarioBlocked("Playwright's Chromium is not installed for the pinned version")
    return PLAYWRIGHT_PACKAGE_JSON


def _run_driver(brief: dict[str, Any], artifacts: Path, secrets: tuple[str, ...]) -> dict[str, Any]:
    completed = subprocess.run(
        ["node", str(DRIVER)],
        input=json.dumps(brief),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=1500,
    )
    (artifacts / "driver-stderr.log").write_text(redact(completed.stderr[-20_000:], *secrets), encoding="utf-8")
    if completed.returncode != 0:
        raise ScenarioFailed("browser driver failed: " + redact(completed.stderr.strip()[-800:], *secrets))
    if any(secret and secret in completed.stdout for secret in secrets):
        raise ScenarioFailed("the browser driver echoed a secret")
    result = json.loads(completed.stdout)
    write_json(artifacts / "browser-downloads.json", result)
    return result


def _verify_saved(kind: str, saved: dict[str, Any], *, region: str, log: Path) -> dict[str, Any]:
    """Treat the browser-saved file exactly as a recipient would; return the facet record."""
    path = Path(str(saved.get("savedPath") or ""))
    if not path.is_file():
        raise ScenarioFailed(f"{kind}: the browser reported no saved file")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    receipt_archive = saved.get("receiptArchiveSha256")
    receipt_manifest = saved.get("receiptManifestSha256")
    if digest != saved.get("sha256") or digest != receipt_archive:
        raise ScenarioFailed(
            f"{kind}: saved bytes {digest[:12]} disagree with the UI receipt {str(receipt_archive)[:12]}"
        )
    if not isinstance(receipt_manifest, str) or len(receipt_manifest) != 64:
        raise ScenarioFailed(f"{kind}: the UI rendered no EXPECTED_ARTIFACT_MANIFEST_SHA256 receipt")
    recipient = Path(tempfile.mkdtemp(prefix=f"acfpr-ui-{kind}-"))
    record = {"filename": path.name, "artifact_manifest_sha256": receipt_manifest}
    root, manifest_sha256 = verify_and_extract(
        data,
        record,
        artifact_type=kind,
        recipient_root=recipient,
        python=python313(),
        env=recipient_env(region),
        log=log,
    )
    return {
        "filename": path.name,
        "bytes": len(data),
        "archive_sha256": digest,
        "receipt_archive_sha256": receipt_archive,
        "artifact_manifest_sha256": manifest_sha256,
        "receipt_manifest_sha256": receipt_manifest,
        "verified": True,
        "artifact_root": root.name,
    }


def _journey(binding: RunBinding, observed: ObservedStack, artifacts: Path) -> dict[str, Any]:
    playwright_module = _playwright_module()
    downloads = artifacts / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    outcome: dict[str, Any] = {"region": binding.region, "customer_path": observed.cloudfront_url}
    with with_synthetic_user(observed, label="ui-downloads", groups=REQUIRED_USER_GROUPS) as user:
        secrets = user.secrets()
        brief = {
            "cloudfrontUrl": observed.cloudfront_url,
            "username": user.username,
            "password": user.password,
            "downloadDir": str(downloads),
            "playwrightModule": str(playwright_module),
        }
        result = _run_driver(brief, artifacts, secrets)
        outcome["signed_in_through_ui"] = result.get("signedIn") is True
        outcome["template_used"] = result.get("templateUsed")
        outcome["blocked_reasons"] = result.get("blockedReasons") or {}
        outcome["console_errors"] = result.get("consoleErrors") or []
        calls = [c for c in (result.get("apiCalls") or []) if isinstance(c, dict)]
        outcome["export_calls"] = {
            kind: [c.get("status") for c in calls if c.get("path") == route and c.get("method") == "POST"]
            for kind, route in EXPORT_ROUTES.items()
        }
        log = artifacts / "verify.log"
        for kind in ("cfn", "python"):
            saved = result.get(kind)
            if not isinstance(saved, dict):
                raise ScenarioFailed(
                    f"{kind}: the UI produced no download ({json.dumps(outcome['blocked_reasons'])[:300]})"
                )
            outcome[f"{kind}_download"] = _verify_saved(kind, saved, region=binding.region, log=log)
    write_json(artifacts / "journey-outcome.json", outcome)
    return outcome


def _claim_holds(outcome: dict[str, Any]) -> bool:
    return outcome.get("signed_in_through_ui") is True and all(
        (outcome.get(f"{kind}_download") or {}).get("verified") is True
        and (outcome.get(f"{kind}_download") or {}).get("archive_sha256")
        == (outcome.get(f"{kind}_download") or {}).get("receipt_archive_sha256")
        and (outcome.get(f"{kind}_download") or {}).get("artifact_manifest_sha256")
        == (outcome.get(f"{kind}_download") or {}).get("receipt_manifest_sha256")
        and (outcome.get("export_calls") or {}).get(kind) == [200]
        for kind in ("cfn", "python")
    )


def _scenario(args: argparse.Namespace, artifact_dir: Path) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    journey = family_journey(
        binding,
        observed,
        family=FAMILY,
        run=lambda artifacts: _journey(binding, observed, artifacts),
        artifact_dir=artifact_dir,
    )
    outcome = journey["outcome"]
    holds = _claim_holds(outcome)
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journey": {k: v for k, v in journey.items() if k != "outcome"},
        "claim": SCENARIO_ID,
        "claim_holds": holds,
        "facet": {
            k: outcome.get(k)
            for k in (
                "customer_path",
                "signed_in_through_ui",
                "template_used",
                "export_calls",
                "cfn_download",
                "python_download",
                "blocked_reasons",
            )
        },
    }
    write_json(artifact_dir / "report", report)
    if not holds:
        raise ScenarioFailed(f"{SCENARIO_ID}: {json.dumps(report['facet'], default=str)[:400]}")
    return report


def ui_downloads_verified(args, artifact_dir):
    return _scenario(args, artifact_dir)
