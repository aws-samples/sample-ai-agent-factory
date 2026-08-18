#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fail when assets/ drifts from the git-tracked sources it mirrors.

Why this exists
---------------
Workshop Studio builds pull from two different places:

  * ``static/`` and ``content/`` come from the git repository, so a push
    updates them.
  * ``assets/`` is served from the shared assets bucket
    (``s3://ws-assets-us-east-1/<workshop-id>/``), which a push does NOT
    update. It is synced separately.

That split silently cost us real findings. A security fix to
``static/cfn/registry/compute-stack.yaml`` was pushed and the build's copy went
clean, while the stale pre-fix copy in the assets bucket kept failing the same
Checkov checks (CKV_AWS_2, CKV_AWS_103, CKV_AWS_260). The build reported the
leftovers as HIGH findings against ``assets/cfn/registry/...`` — the same code,
scanned twice, fixed once. Nothing in the repo could detect it, because the
repo copy was correct.

What parity means here
---------------------
  * ``assets/cfn/**`` must byte-match ``static/cfn/**`` at the same relative
    path. The registry nested templates live in the bucket because
    CloudFormation resolves their ``TemplateURL`` from there, so every
    ``static/cfn/registry/`` file must also be present under ``assets/cfn/``.
  * ``assets/source/**`` must byte-match ``source/**``, ignoring the build and
    virtualenv artifacts ``deploy-cfn.sh`` already excludes from its own sync.
  * ``assets/README.md`` is assets-only and allowed.

With ``--bucket s3://...`` the live bucket is compared too, which is the check
that would have caught the drift above. Local-only by default so the script
stays usable with no credentials.

Usage:
  verify-assets-parity.py [repo-root] [--bucket s3://bucket/prefix]
"""
import filecmp
import hashlib
import sys
from pathlib import Path

# Mirrors the --exclude list in deploy-cfn.sh: build/venv artifacts are never
# published to participants, so their absence from assets/ is not drift.
ARTIFACT_PARTS = {
    "__pycache__", ".venv", "node_modules", "cdk.out", ".pytest_cache",
    ".ipynb_checkpoints",
}
ARTIFACT_SUFFIXES = (".pyc", ".egg-info")
# Files that legitimately exist only under assets/.
ASSETS_ONLY = {"README.md"}


def is_artifact(rel: Path) -> bool:
    if any(p in ARTIFACT_PARTS for p in rel.parts):
        return True
    if rel.name.endswith(ARTIFACT_SUFFIXES):
        return True
    if rel.name.endswith(".state.json"):
        return True
    return any(p.endswith(".egg-info") for p in rel.parts)


def files_under(root: Path):
    if not root.is_dir():
        return {}
    return {
        p.relative_to(root): p
        for p in root.rglob("*")
        if p.is_file() and not is_artifact(p.relative_to(root))
    }


def compare_tree(label, assets_dir: Path, source_dir: Path, require_all_source,
                 missing_hint):
    """Return a list of human-readable drift lines for one mirrored tree.

    An absent assets subtree is not drift. The open-source branch ships an empty
    assets/ on purpose — deploy-cfn.sh syncs static/ and source/ straight to its
    own bucket, so there is no mirror to go stale. What this guards against is a
    copy that exists and disagrees.
    """
    problems = []
    if not assets_dir.is_dir():
        return problems
    a = files_under(assets_dir)
    s = files_under(source_dir)
    if not a:
        return problems

    for rel, path in sorted(a.items()):
        counterpart = s.get(rel)
        if counterpart is None:
            problems.append(
                f"{label}: assets/{assets_dir.name}/{rel} has no counterpart in "
                f"{source_dir.name}/{rel} (stale copy, or deleted upstream)"
            )
        elif not filecmp.cmp(path, counterpart, shallow=False):
            problems.append(
                f"{label}: assets/{assets_dir.name}/{rel} differs from "
                f"{source_dir.name}/{rel}"
            )

    # Only some source trees must be fully mirrored. static/cfn/ holds templates
    # (code-editor, IAM policy JSON) that are served from the build's static
    # host and deliberately absent from the bucket, so requiring all of it would
    # be a false positive; the nested registry templates DO have to be there.
    for rel in sorted(s):
        if require_all_source(rel) and rel not in a:
            problems.append(
                f"{label}: {source_dir.name}/{rel} is missing from "
                f"assets/{assets_dir.name}/ ({missing_hint})"
            )
    return problems


def compare_bucket(root: Path, bucket_uri: str):
    """Compare the live assets bucket against local assets/ by MD5."""
    problems = []
    without_scheme = bucket_uri[len("s3://"):] if bucket_uri.startswith("s3://") else bucket_uri
    bucket, _, prefix = without_scheme.partition("/")
    prefix = prefix.strip("/")

    # boto3 rather than shelling out to `aws s3api`. Two reasons, both of which
    # bit this function:
    #
    #  1. `list-objects-v2` returns at most 1000 keys per call. The previous
    #     implementation parsed a single response and ignored
    #     NextContinuationToken, so past 1000 objects every unlisted file looked
    #     locally-present-and-remotely-absent... except the loop below only
    #     reports that direction, meaning a large bucket would have produced a
    #     flood of false problems while a *stale remote* file beyond the first
    #     page went unnoticed. A paginator has no page limit to get wrong.
    #  2. `bucket`/`prefix` come from a command-line argument and were
    #     interpolated into an argv list. There is no shell involved, but a value
    #     beginning with `-` is still read by `aws` as an option rather than a
    #     value. Passing them as boto3 keyword arguments removes that entirely.
    #
    # Imported here, not at module scope, so the local-only default keeps working
    # in an environment with no boto3 and no credentials.
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return [
            f"bucket: boto3 is required for --bucket, so {bucket_uri} was not "
            f"verified. Install it (pip install boto3) or drop --bucket."
        ]

    remote = {}
    try:
        paginator = boto3.client("s3").get_paginator("list_objects_v2")
        kwargs = {"Bucket": bucket}
        if prefix:
            kwargs["Prefix"] = prefix + "/"
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                rel = key[len(prefix) + 1:] if prefix else key
                if rel:
                    remote[rel] = obj["ETag"].strip('"')
    except (BotoCoreError, ClientError) as exc:
        # A comparison that was explicitly asked for and could not be made is a
        # failure, not a pass. Expired credentials used to print this warning and
        # still exit 0, so `--bucket` reported parity precisely when it had
        # verified nothing -- the drift this script exists to catch.
        return [
            f"bucket: could not list {bucket_uri}, so nothing was verified: "
            f"{str(exc)[-300:]}"
        ]

    local_root = root / "assets"
    local = {
        str(p.relative_to(local_root)): p
        for p in local_root.rglob("*")
        if p.is_file() and not is_artifact(p.relative_to(local_root))
    }

    for rel, path in sorted(local.items()):
        etag = remote.get(rel)
        if etag is None:
            problems.append(f"bucket: assets/{rel} is not in {bucket_uri}")
            continue
        if "-" in etag:
            # Multipart upload: the ETag is not a plain MD5, so it cannot be
            # compared. Say so rather than reporting a false match.
            print(f"Note: {rel} was uploaded multipart; skipping checksum compare.")
            continue
        digest = hashlib.md5(path.read_bytes()).hexdigest()  # nosec B324 - S3 ETag compare, not security
        if digest != etag:
            problems.append(f"bucket: assets/{rel} differs from {bucket_uri}/{rel}")

    for rel in sorted(remote):
        if rel not in local:
            problems.append(f"bucket: {bucket_uri}/{rel} is not in local assets/ (stale)")
    return problems


def main() -> int:
    args = [a for a in sys.argv[1:]]
    bucket = None
    if "--bucket" in args:
        i = args.index("--bucket")
        try:
            bucket = args[i + 1]
        except IndexError:
            print("Error: --bucket needs an s3:// URI")
            return 2
        del args[i:i + 2]

    # Anything left starting with `-` is a typo or an unsupported flag. Without
    # this it became the repo root: `--buckte s3://...` resolved to a directory
    # that does not exist, which has no assets/, which printed "nothing to
    # check" and exited 0. A gate that passes on a typo is worse than no gate.
    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        print(f"Error: unrecognized argument(s): {' '.join(unknown)}")
        print("\n".join(__doc__.strip().splitlines()[-2:]))
        return 2
    if len(args) > 1:
        print(f"Error: expected at most one repo root, got: {' '.join(args)}")
        return 2
    root = Path(args[0] if args else ".").resolve()
    if not root.is_dir():
        print(f"Error: repo root does not exist: {root}")
        return 2

    assets = root / "assets"
    if not assets.is_dir():
        print("No assets/ directory — nothing to check.")
        return 0

    problems = []
    problems += compare_tree(
        "cfn", assets / "cfn", root / "static" / "cfn",
        # Every nested registry template must be present in the bucket.
        require_all_source=lambda rel: rel.parts[:1] == ("registry",),
        missing_hint="CloudFormation resolves this nested TemplateURL from the bucket",
    )
    problems += compare_tree(
        "source", assets / "source", root / "source",
        require_all_source=lambda rel: True,
        missing_hint="the IDE pulls the workshop code from the bucket",
    )

    for extra in sorted(p for p in assets.iterdir() if p.is_file()):
        if extra.name not in ASSETS_ONLY:
            problems.append(f"assets/{extra.name} is not a known assets-only file")

    if bucket:
        problems += compare_bucket(root, bucket)

    if problems:
        print(f"assets/ parity FAILED — {len(problems)} problem(s):")
        for p in problems:
            print(f"  {p}")
        print()
        print("Fix the drift, then re-sync the bucket:")
        print("  aws s3 sync assets/ s3://ws-assets-us-east-1/<workshop-id> --delete")
        return 1

    # Say how many files were actually compared. An empty assets/ is legitimate on
    # the open-source branch (see compare_tree), but then this line was claiming
    # "every mirrored file matches" after comparing nothing, which reads as a
    # verified pass. A count makes a vacuous pass self-evident.
    compared = len(files_under(assets / "cfn")) + len(files_under(assets / "source"))
    if compared:
        print(f"assets/ parity OK — {compared} mirrored file(s) match their git-tracked source.")
    else:
        print("assets/ parity OK — no local mirrors under assets/cfn or assets/source, "
              "so nothing was compared. This is expected on the open-source branch, "
              "where deploy-cfn.sh syncs static/cfn/ and source/ straight to the bucket.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
