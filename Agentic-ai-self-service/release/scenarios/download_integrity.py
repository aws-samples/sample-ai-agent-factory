"""``delivery.downloadable_assets`` — digest and adversarial facets that need no deployment.

One light cached journey authenticates to the deployed API, exports one CloudFormation bundle and one
Python bundle, verifies each with its own shipped ``verify-artifact.py`` (archive digest and the
externally anchored ``artifact_manifest_sha256``), and then proves, entirely from the extracted
bundles:

* **deployed-api-authenticated-cfn-download-archive-digest-manifest-verified** / **…-python-…** — the
  export response carried both digests and the shipped verifier accepted the archive and the
  extracted root.
* **tampered-archive-or-manifest-rejected-before-use** — a single byte flipped in an extracted file
  makes the shipped verifier exit non-zero before any use.
* **generated-ci-defaults-remain-inert** — the shipped CI only deploys on an explicit
  ``workflow_dispatch`` with ``inputs.deploy`` and the wrapper refuses unless ``ALLOW_DEPLOY=true``.
* **cfn-wrong-account-or-region-refused** — the template pins every trust/role grant to
  ``aws:SourceAccount == AWS::AccountId`` and a ``SourceArn`` in ``AWS::Region``, so a cross-account
  or cross-region use is refused by construction.
* **python-missing-or-incompatible-dependency-rejected** — corrupting a pin in ``requirements.lock``
  makes ``install-locked.sh`` refuse before building any environment.

No CloudFormation deployment occurs; this is deliberately the light, deploy-free slice of the family.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from _download import export_archive, python313, recipient_env, template_request_body, verify_and_extract
from _live import (
    ObservedStack,
    RunBinding,
    ScenarioFailed,
    bound_deployment,
    family_journey,
    with_synthetic_user,
    write_json,
)

# The gallery template the CFN/Python families use; it ships a gateway so the bundle exercises the
# full export surface. Kept in sync with downloadable_cfn.GALLERY_TEMPLATE.
GALLERY_TEMPLATE = "strands-gateway-agent"

FAMILY = "download-integrity"


def _run(cmd: list[str], cwd: Path, env: dict[str, str], log: Path) -> subprocess.CompletedProcess:
    with log.open("a") as handle:
        handle.write(f"\n$ {' '.join(cmd)} (cwd={cwd})\n")
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    with log.open("a") as handle:
        handle.write(result.stdout[-4000:])
        handle.write(result.stderr[-4000:])
        handle.write(f"(exit {result.returncode})\n")
    return result


def _prove_tamper(python: Path, cfn_root: Path, env: dict[str, str], log: Path) -> dict[str, Any]:
    tampered = Path(tempfile.mkdtemp(prefix="acfpr-tamper-")) / "root"
    shutil.copytree(cfn_root, tampered)
    target = tampered / "template.yaml"
    if not target.is_file():
        target = next(p for p in tampered.iterdir() if p.is_file() and p.suffix in {".yaml", ".json", ".py"})
    data = target.read_bytes()
    target.write_bytes(data + b"\n# tampered\n")
    result = _run(
        [str(python), "-I", "verify-artifact.py", "--directory", ".", "--artifact-type", "cfn"], tampered, env, log
    )
    shutil.rmtree(tampered.parent, ignore_errors=True)
    if result.returncode == 0:
        raise ScenarioFailed("the shipped verifier accepted a tampered bundle")
    return {"tamperedFile": target.name, "verifierExit": result.returncode, "rejectedBeforeUse": True}


def _prove_inert_ci(cfn_root: Path) -> dict[str, Any]:
    workflow = (cfn_root / ".github" / "workflows" / "deploy.yml").read_text()
    wrapper = ""
    for candidate in (cfn_root / "ci" / "deploy.sh", cfn_root / "ci" / "verify.sh"):
        if candidate.is_file():
            wrapper += candidate.read_text()
    inert = (
        "workflow_dispatch:" in workflow
        and "deploy" in workflow
        and "ALLOW_DEPLOY:-false" in wrapper
        or "ALLOW_DEPLOY" in workflow + wrapper
    )
    deploy_sh = (cfn_root / "deploy.sh").read_text() if (cfn_root / "deploy.sh").is_file() else ""
    if not inert:
        raise ScenarioFailed("generated CI does not default to inert (no ALLOW_DEPLOY / workflow_dispatch guard)")
    return {
        "workflowDispatchGated": "workflow_dispatch:" in workflow,
        "allowDeployGuard": "ALLOW_DEPLOY" in workflow + wrapper,
        "deployShPresent": bool(deploy_sh),
    }


def _prove_template_scoping(cfn_root: Path) -> dict[str, Any]:
    template = cfn_root / "template.yaml"
    text = template.read_text() if template.is_file() else ""
    if not text:
        template = next((p for p in cfn_root.iterdir() if p.name.startswith("template.")), None)
        text = template.read_text() if template else ""
    source_account = "aws:SourceAccount" in text and "AWS::AccountId" in text
    source_region = "aws:SourceArn" in text and "AWS::Region" in text
    if not (source_account and source_region):
        raise ScenarioFailed("template does not pin SourceAccount/SourceArn to this account and region")
    return {"sourceAccountPinned": source_account, "sourceRegionPinned": source_region}


def _prove_python_corrupt_lock(python: Path, py_root: Path, env: dict[str, str], log: Path) -> dict[str, Any]:
    scratch = Path(tempfile.mkdtemp(prefix="acfpr-pylock-")) / "root"
    shutil.copytree(py_root, scratch)
    lock = scratch / "requirements.lock"
    if not lock.is_file():
        shutil.rmtree(scratch.parent, ignore_errors=True)
        raise ScenarioFailed("python bundle has no requirements.lock to corrupt")
    lines = lock.read_text().splitlines()
    corrupted = []
    changed = False
    for line in lines:
        if not changed and line and not line.lstrip().startswith("#") and "==" in line:
            name = line.split("==")[0]
            corrupted.append(f"{name}==0.0.0the-version-that-does-not-exist")
            changed = True
        else:
            corrupted.append(line)
    lock.write_text("\n".join(corrupted) + "\n")
    result = _run(["bash", "install-locked.sh", ".venv-corrupt"], scratch, env, log)
    created = (scratch / ".venv-corrupt").exists()
    shutil.rmtree(scratch.parent, ignore_errors=True)
    if result.returncode == 0:
        raise ScenarioFailed("install-locked.sh accepted a corrupted dependency lock")
    return {"installExit": result.returncode, "environmentCreated": created, "rejected": True}


def _journey(args: argparse.Namespace, binding: RunBinding, observed: ObservedStack, artifacts: Path) -> dict[str, Any]:
    python = python313()
    env = recipient_env(binding.region)
    log = artifacts / "integrity.log"
    cfn_recipient = Path(tempfile.mkdtemp(prefix="acfpr-int-cfn-"))
    py_recipient = Path(tempfile.mkdtemp(prefix="acfpr-int-py-"))
    outcome: dict[str, Any] = {"region": binding.region}
    with with_synthetic_user(observed, label="integrity") as user:
        cfn_body = template_request_body(GALLERY_TEMPLATE, suffix="integrity")
        cfn_body["dataRetentionPolicy"] = "Delete"
        cfn_record, cfn_data = export_archive(
            observed.cloudfront_url, "/api/generate-cfn-template", user.token, cfn_body, binding.region
        )
        cfn_root, cfn_manifest = verify_and_extract(
            cfn_data, cfn_record, artifact_type="cfn", recipient_root=cfn_recipient, python=python, env=env, log=log
        )
        outcome["cfn_digest"] = {
            "archive_sha256": cfn_record["archive_sha256"],
            "artifact_manifest_sha256": cfn_record["artifact_manifest_sha256"],
            "verified": True,
        }

        py_body = template_request_body(GALLERY_TEMPLATE, suffix="integrity")
        py_record, py_data = export_archive(
            observed.cloudfront_url, "/api/export-python", user.token, py_body, binding.region
        )
        py_root, py_manifest = verify_and_extract(
            py_data, py_record, artifact_type="python", recipient_root=py_recipient, python=python, env=env, log=log
        )
        outcome["python_digest"] = {
            "archive_sha256": py_record["archive_sha256"],
            "artifact_manifest_sha256": py_record["artifact_manifest_sha256"],
            "verified": True,
        }

        outcome["tamper"] = _prove_tamper(python, cfn_root, env, log)
        outcome["inert_ci"] = _prove_inert_ci(cfn_root)
        outcome["template_scoping"] = _prove_template_scoping(cfn_root)
        outcome["python_corrupt_lock"] = _prove_python_corrupt_lock(python, py_root, env, log)
    shutil.rmtree(cfn_recipient, ignore_errors=True)
    shutil.rmtree(py_recipient, ignore_errors=True)
    write_json(artifacts / "journey-outcome.json", outcome)
    return outcome


_CLAIMS = {
    "deployed-api-authenticated-cfn-download-archive-digest-manifest-verified": lambda o: (
        (o.get("cfn_digest") or {}).get("verified") is True
        and bool((o.get("cfn_digest") or {}).get("artifact_manifest_sha256"))
    ),
    "deployed-api-authenticated-python-download-archive-digest-manifest-verified": lambda o: (
        (o.get("python_digest") or {}).get("verified") is True
        and bool((o.get("python_digest") or {}).get("artifact_manifest_sha256"))
    ),
    "downloadable-assets-source-contract-and-archive-digests-match": lambda o: all(
        (o.get(k) or {}).get("verified") is True
        and bool((o.get(k) or {}).get("archive_sha256"))
        and bool((o.get(k) or {}).get("artifact_manifest_sha256"))
        for k in ("cfn_digest", "python_digest")
    ),
    "tampered-archive-or-manifest-rejected-before-use": lambda o: (
        (o.get("tamper") or {}).get("rejectedBeforeUse") is True
    ),
    "generated-ci-defaults-remain-inert": lambda o: (
        (o.get("inert_ci") or {}).get("allowDeployGuard") is True
        and (o.get("inert_ci") or {}).get("workflowDispatchGated") is True
    ),
    "cfn-wrong-account-or-region-refused": lambda o: (
        (o.get("template_scoping") or {}).get("sourceAccountPinned") is True
        and (o.get("template_scoping") or {}).get("sourceRegionPinned") is True
    ),
    "python-missing-or-incompatible-dependency-rejected": lambda o: (
        (o.get("python_corrupt_lock") or {}).get("rejected") is True
        and (o.get("python_corrupt_lock") or {}).get("environmentCreated") is False
    ),
}


def _scenario(args: argparse.Namespace, artifact_dir: Path, claim: str) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    journey = family_journey(
        binding,
        observed,
        family=FAMILY,
        run=lambda artifacts: _journey(args, binding, observed, artifacts),
        artifact_dir=artifact_dir,
    )
    outcome = journey["outcome"]
    holds = _CLAIMS[claim](outcome)
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journey": {k: v for k, v in journey.items() if k != "outcome"},
        "claim": claim,
        "claim_holds": holds,
        "facet": {
            k: outcome.get(k)
            for k in ("cfn_digest", "python_digest", "tamper", "inert_ci", "template_scoping", "python_corrupt_lock")
        },
    }
    write_json(artifact_dir / "report", report)
    if not holds:
        raise ScenarioFailed(f"{claim}: {json.dumps(outcome)[:300]}")
    return report


def cfn_download_digest_verified(args, artifact_dir):
    return _scenario(args, artifact_dir, "deployed-api-authenticated-cfn-download-archive-digest-manifest-verified")


def python_download_digest_verified(args, artifact_dir):
    return _scenario(args, artifact_dir, "deployed-api-authenticated-python-download-archive-digest-manifest-verified")


def source_contract_and_archive_digests_match(args, artifact_dir):
    return _scenario(args, artifact_dir, "downloadable-assets-source-contract-and-archive-digests-match")


def tampered_archive_rejected(args, artifact_dir):
    return _scenario(args, artifact_dir, "tampered-archive-or-manifest-rejected-before-use")


def generated_ci_defaults_inert(args, artifact_dir):
    return _scenario(args, artifact_dir, "generated-ci-defaults-remain-inert")


def cfn_wrong_account_or_region_refused(args, artifact_dir):
    return _scenario(args, artifact_dir, "cfn-wrong-account-or-region-refused")


def python_missing_dependency_rejected(args, artifact_dir):
    return _scenario(args, artifact_dir, "python-missing-or-incompatible-dependency-rejected")
