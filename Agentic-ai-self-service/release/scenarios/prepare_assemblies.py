#!/usr/bin/env python3
"""Seal the candidate's CDK assembly per release Region for deployment binding.

Live scenarios prove a deployed stack *is* the candidate by comparing the live template
digest and every Lambda ``CodeSha256`` with the assembly synthesized from the candidate
commit. This script produces that assembly -- once per Region, from a clean checkout,
with the project's pinned CDK CLI and Python dependencies -- under
``build/release-assemblies/<commit>/<region>/`` and writes ``manifest.json`` beside it.

It refuses a dirty working tree (the synth would bake ``SourceDirty=true`` and no
scenario could bind it) and refuses an assembly whose ``SourceCommit`` output disagrees
with HEAD. It never touches AWS: ``--no-lookups`` is passed and no credential is read.

Usage::

    release/scenarios/prepare_assemblies.py --region us-east-1 --region eu-west-3
    release/scenarios/prepare_assemblies.py --all-release-regions
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INFRA = PROJECT_ROOT / "infra"
CDK_BIN = INFRA / "node_modules" / ".bin" / "cdk"
INFRA_PYTHON = INFRA / ".venv" / "bin" / "python"
ASSEMBLIES_ROOT = PROJECT_ROOT / "build" / "release-assemblies"
REGIONS_PATH = PROJECT_ROOT / "release" / "agentcore-regions.json"
STACK_NAME = "acf-production"
ROOT_WORKFLOW_RELATIVE = ".github/workflows/agentic-ai-self-service-ci.yml"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-P", *args], cwd=PROJECT_ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(document: object) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def release_regions() -> list[str]:
    document = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))
    return [entry["region"] for entry in document["regions"] if entry.get("production_release")]


def source_identity() -> tuple[str, str]:
    repo = Path(_git("rev-parse", "--show-toplevel")).resolve()
    project_rel = PROJECT_ROOT.resolve().relative_to(repo).as_posix()
    commit = _git("rev-parse", "HEAD")
    tree = _git("rev-parse", f"{commit}:{project_rel}")
    dirty = _git("status", "--porcelain=v1", "--untracked-files=all", "--", project_rel, ROOT_WORKFLOW_RELATIVE)
    if dirty:
        raise SystemExit("refusing to seal an assembly from a dirty working tree:\n" + dirty)
    return commit, tree


def synth(region: str, commit: str, tree: str, *, python: Path, cdk_bin: Path) -> Path:
    target = ASSEMBLIES_ROOT / commit / region
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"assembly-{region}-") as scratch:
        out = Path(scratch) / "cdk.out"
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": "C.UTF-8",
            "CDK_PYTHON": str(python),
            "CDK_DISABLE_VERSION_CHECK": "1",
            "CDK_HOME": str(Path(scratch) / "cdk-home"),
            "TMPDIR": scratch,
        }
        completed = subprocess.run(
            [
                str(cdk_bin),
                "synth",
                STACK_NAME,
                "--strict",
                "--no-lookups",
                "--quiet",
                "-c",
                "project_name=acf",
                "-c",
                "environment_name=production",
                "-c",
                f"aws_region={region}",
                "-o",
                str(out),
            ],
            cwd=INFRA,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if completed.returncode != 0:
            raise SystemExit(f"cdk synth failed for {region}:\n{completed.stderr[-4000:]}")
        shutil.copytree(out, target, symlinks=False)
    template_path = target / f"{STACK_NAME}.template.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    outputs = {key: value.get("Value") for key, value in template.get("Outputs", {}).items()}
    if outputs.get("SourceCommit") != commit or outputs.get("SourceProjectTree") != tree:
        raise SystemExit(f"the synthesized {region} template does not identify itself as {commit[:12]}/{tree[:12]}")
    if outputs.get("SourceDirty") != "false":
        raise SystemExit(f"the synthesized {region} template reports SourceDirty={outputs.get('SourceDirty')!r}")
    lambdas = {}
    for logical_id, resource in template.get("Resources", {}).items():
        if resource.get("Type") != "AWS::Lambda::Function":
            continue
        key = resource["Properties"]["Code"]["S3Key"]
        asset = target / f"asset.{key}"
        if not asset.is_file():
            raise SystemExit(f"{region}: asset {asset.name} for {logical_id} is missing from the assembly")
        lambdas[logical_id] = {
            "s3_key": key,
            "asset_sha256": _sha256(asset),
            "code_sha256": base64.b64encode(hashlib.sha256(asset.read_bytes()).digest()).decode("ascii"),
        }
    manifest = {
        "schema_version": 1,
        "stack_name": STACK_NAME,
        "region": region,
        "commit": commit,
        "project_tree": tree,
        "template_sha256": _canonical(template),
        "template_file_sha256": _sha256(template_path),
        "lambda_functions": lambdas,
        "cdk_cli": subprocess.run(
            [str(cdk_bin), "--version"], capture_output=True, text=True, check=False
        ).stdout.strip(),
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", action="append", default=[], help="Region to seal (repeatable)")
    parser.add_argument("--all-release-regions", action="store_true")
    parser.add_argument("--python", type=Path, default=INFRA_PYTHON, help="interpreter holding the pinned CDK deps")
    parser.add_argument("--cdk-bin", type=Path, default=CDK_BIN)
    args = parser.parse_args(argv)
    regions = release_regions() if args.all_release_regions else args.region
    if not regions:
        parser.error("name at least one --region or pass --all-release-regions")
    unknown = sorted(set(regions) - set(release_regions()))
    if unknown:
        parser.error(f"not release Regions: {unknown}")
    if not args.cdk_bin.is_file() or not args.python.is_file():
        raise SystemExit("the pinned CDK CLI (infra/node_modules/.bin/cdk) and infra Python environment are required")
    commit, tree = source_identity()
    for region in regions:
        target = synth(region, commit, tree, python=args.python, cdk_bin=args.cdk_bin)
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        print(
            f"{region}: sealed {len(manifest['lambda_functions'])} Lambda assets, "
            f"template {manifest['template_sha256'][:16]} -> {target}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
