"""Downloadable assets, CloudFormation: download -> clean recipient -> deploy -> invoke -> update ->
rollback -> teardown -> nothing left, per Region.

Three scenario ids, one live journey (``_live.family_journey``):

* ``api-authenticated-cfn-download-clean-recipient-deploy-invoke-update-rollback-teardown``
  (``live_region`` of ``delivery.downloadable_assets``) -- the whole journey must hold.
* ``downloaded-cfn-update-rollback-restores-prior-behavior`` (``rollback``) -- after the
  version-2 update, redeploying the exact version-1 bundle makes the agent answer as version 1.
* ``downloaded-cfn-teardown-direct-service-inventories-zero`` (``teardown_zero``) -- after the
  bundle's own ``teardown.sh``, the stack, the AgentCore Runtime, every S3 object under the
  stack's prefixes, and the runtime log groups are gone, read directly from each service.

The recipient is a throwaway directory with nothing but the download, plus a throwaway S3
bucket the recipient owns (the bundle's ``deploy.sh`` requires one). Both are removed at the
end. Version markers are distinct phrases in the system prompt, and each invocation is made
directly against the deployed Runtime with ``bedrock-agentcore:InvokeAgentRuntime`` -- the
recipient's own call path, not the platform's.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from _download import (
    anchored_env,
    export_archive,
    python313,
    recipient_env,
    run_logged,
    template_request_body,
    verify_and_extract,
)
from _live import (
    ObservedStack,
    RunBinding,
    ScenarioFailed,
    boto3_session,
    bound_deployment,
    family_journey,
    redact,
    with_synthetic_user,
    write_json,
    write_provenance,
)

FAMILY = "cfn-recipient"
GALLERY_TEMPLATE = "strands-gateway-agent"
MARKER_ONE = "The release certification version marker is ALPHA-ONE."
MARKER_TWO = "The release certification version marker is BRAVO-TWO."
PROMPT = "State your release certification version marker exactly, then stop."


def _system_prompt(marker: str) -> str:
    return f"Answer ordinary factual questions concisely. {marker} Do not call external tools."


def _recipient_bucket(binding: RunBinding, artifacts: Path) -> str:
    name = f"acfpr-recipient-{binding.region}-{uuid.uuid4().hex[:12]}"
    s3 = boto3_session(binding.region).client("s3")
    params: dict[str, Any] = {"Bucket": name}
    if binding.region != "us-east-1":
        params["CreateBucketConfiguration"] = {"LocationConstraint": binding.region}
    s3.create_bucket(**params)
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    write_json(artifacts / "recipient-bucket.json", {"bucket": name, "region": binding.region})
    return name


def _empty_and_delete_bucket(region: str, bucket: str) -> dict[str, int]:
    s3 = boto3_session(region).client("s3")
    deleted = 0
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        objects = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", [])]
        objects += [{"Key": m["Key"], "VersionId": m["VersionId"]} for m in page.get("DeleteMarkers", [])]
        for start in range(0, len(objects), 1000):
            chunk = objects[start : start + 1000]
            if chunk:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})
                deleted += len(chunk)
    s3.delete_bucket(Bucket=bucket)
    return {"objects_deleted": deleted}


def _stack_outputs(region: str, stack_name: str) -> dict[str, str]:
    cfn = boto3_session(region).client("cloudformation")
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    if not stack["StackStatus"].endswith("_COMPLETE") or "ROLLBACK" in stack["StackStatus"]:
        raise ScenarioFailed(f"recipient stack {stack_name} is {stack['StackStatus']}")
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def _runtime_arn(outputs: dict[str, str]) -> str:
    for key, value in outputs.items():
        if "Runtime" in key and value.startswith("arn:aws:bedrock-agentcore:") and ":runtime/" in value:
            return value
    raise ScenarioFailed(f"no Runtime ARN among stack outputs {sorted(outputs)}")


def _invoke(region: str, runtime_arn: str, expected_marker: str, label: str, artifacts: Path) -> dict[str, Any]:
    """Invoke the deployed Runtime directly (the recipient's own path) until it answers, bounded."""
    client = boto3_session(region).client("bedrock-agentcore")
    deadline = time.monotonic() + 600
    attempt = 0
    last_error = ""
    while time.monotonic() < deadline:
        attempt += 1
        try:
            response = client.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=f"acfpr-{uuid.uuid4().hex}-{uuid.uuid4().hex[:8]}",
                payload=json.dumps({"prompt": PROMPT}).encode("utf-8"),
                contentType="application/json",
                accept="application/json",
            )
            body = (
                response["response"].read().decode("utf-8", "replace")
                if hasattr(response["response"], "read")
                else str(response["response"])
            )
        except Exception as exc:  # noqa: BLE001 -- a cold Runtime returns transient errors right after deploy
            last_error = redact(f"{type(exc).__name__}: {exc}")
            time.sleep(20)
            continue
        marker_token = expected_marker.split()[-1].rstrip(".")
        if marker_token in body:
            record = {
                "label": label,
                "attempts": attempt,
                "expected_marker": marker_token,
                "answer_excerpt": body[:400],
            }
            write_json(artifacts / f"invoke-{label}.json", record)
            return record
        last_error = f"answer lacked {marker_token}: {body[:200]}"
        time.sleep(15)
    raise ScenarioFailed(f"invocation ({label}) did not prove {expected_marker!r}: {last_error}")


def _plant_decoys(region: str, bucket: str, stack_name: str) -> dict[str, dict[str, str]]:
    """Resources adjacent to everything teardown.sh sweeps, owned by 'someone else'.

    Sibling stack prefixes in the SAME bucket (``cfn-assets/<other>/``, ``deployments/<other>/``)
    and a sibling AgentCore runtime log group. Each decoy's expected content digest is returned
    so preservation is proven byte for byte, not by existence alone.
    """
    import hashlib

    session = boto3_session(region)
    s3, logs = session.client("s3"), session.client("logs")
    other = f"{stack_name}-decoy"
    decoys: dict[str, dict[str, str]] = {}
    for key in (
        f"cfn-assets/{other}/agent-code.zip",
        f"deployments/{other}/abc/code.zip",
        f"cfn-assets/{stack_name}decoy/x",
    ):
        body = f"foreign decoy {key} {uuid.uuid4().hex}".encode()
        s3.put_object(Bucket=bucket, Key=key, Body=body)
        decoys[f"s3://{bucket}/{key}"] = {
            "kind": "s3",
            "bucket": bucket,
            "key": key,
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    group = f"/aws/bedrock-agentcore/runtimes/decoy_{uuid.uuid4().hex[:12]}-DEFAULT"
    logs.create_log_group(logGroupName=group)
    decoys[group] = {"kind": "log_group", "name": group}
    return decoys


def _verify_decoys(region: str, bucket: str, decoys: dict[str, dict[str, str]]) -> dict[str, bool]:
    import hashlib

    session = boto3_session(region)
    s3, logs = session.client("s3"), session.client("logs")
    preserved: dict[str, bool] = {}
    for label, decoy in decoys.items():
        try:
            if decoy["kind"] == "s3":
                body = s3.get_object(Bucket=decoy["bucket"], Key=decoy["key"])["Body"].read()
                preserved[label] = hashlib.sha256(body).hexdigest() == decoy["sha256"]
            else:
                groups = logs.describe_log_groups(logGroupNamePrefix=decoy["name"]).get("logGroups", [])
                preserved[label] = any(g["logGroupName"] == decoy["name"] for g in groups)
        except Exception:  # noqa: BLE001 -- a missing decoy is exactly the failure being measured
            preserved[label] = False
    return preserved


def _inventory(region: str, stack_name: str, bucket: str, runtime_id: str | None) -> dict[str, Any]:
    session = boto3_session(region)
    cfn, s3, logs, control = (session.client(n) for n in ("cloudformation", "s3", "logs", "bedrock-agentcore-control"))
    inventory: dict[str, Any] = {}
    try:
        status = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
    except Exception:  # noqa: BLE001 -- ValidationError "does not exist" is the absence we want
        status = "ABSENT"
    inventory["stack"] = status
    keys: list[str] = []
    for prefix in (f"cfn-assets/{stack_name}/", f"deployments/{stack_name}/"):
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
            keys += [v["Key"] for v in page.get("Versions", [])] + [m["Key"] for m in page.get("DeleteMarkers", [])]
    inventory["stack_prefixed_objects"] = len(keys)
    runtimes = [
        r
        for r in control.list_agent_runtimes(maxResults=100).get("agentRuntimes", [])
        if (runtime_id and r.get("agentRuntimeId") == runtime_id)
        or stack_name.replace("-", "_") in r.get("agentRuntimeName", "")
    ]
    inventory["runtimes"] = [r.get("agentRuntimeName") for r in runtimes]
    groups = logs.describe_log_groups(logGroupNamePrefix="/aws/bedrock-agentcore/runtimes/").get("logGroups", [])
    inventory["runtime_log_groups"] = [
        g["logGroupName"] for g in groups if runtime_id and runtime_id in g["logGroupName"]
    ]
    cognito = session.client("cognito-idp")
    compact = stack_name.replace("-", "")
    pools: list[str] = []
    for page in cognito.get_paginator("list_user_pools").paginate(MaxResults=60):
        pools += [
            p["Name"]
            for p in page.get("UserPools", [])
            if compact in p["Name"].replace("-", "") or stack_name in p["Name"]
        ]
    inventory["user_pools"] = pools
    return inventory


def _journey(binding: RunBinding, observed: ObservedStack, artifacts: Path) -> dict[str, Any]:
    python = python313()
    log = artifacts / "recipient.log"
    env = recipient_env(binding.region)
    outcome: dict[str, Any] = {"region": binding.region, "customer_path": observed.cloudfront_url, "steps": []}
    recipient_root = Path(tempfile.mkdtemp(prefix="acfpr-cfn-recipient-", dir=os.environ.get("TMPDIR")))
    bucket: str | None = None
    stack_name: str | None = None
    runtime_id: str | None = None
    decoy_log_group: str | None = None
    suffix = uuid.uuid4().hex[:16]
    try:
        # Two exports of the SAME stack (same node id) with different version markers.
        exports: dict[str, dict[str, Any]] = {}
        with with_synthetic_user(observed, label="cfndl") as user:
            for version, marker in (("v1", MARKER_ONE), ("v2", MARKER_TWO)):
                body = template_request_body(GALLERY_TEMPLATE, suffix=suffix, system_prompt=_system_prompt(marker))
                # A disposable recipient deployment: the export's default Retain policy keeps the
                # user pool and log groups on delete BY DESIGN (and teardown.sh documents how to
                # remove them by hand); zero residue is certified under the documented Delete policy.
                body["dataRetentionPolicy"] = "Delete"
                record, data = export_archive(
                    observed.cloudfront_url, "/api/generate-cfn-template", user.token, body, binding.region
                )
                root, manifest_sha256 = verify_and_extract(
                    data,
                    record,
                    artifact_type="cfn",
                    recipient_root=recipient_root / version,
                    python=python,
                    env=env,
                    log=log,
                )
                exports[version] = {
                    "record": record,
                    "root": root,
                    "manifest_sha256": manifest_sha256,
                    "marker": marker,
                }
                outcome["steps"].append(
                    {
                        "step": f"export-{version}",
                        "archive_sha256": record["archive_sha256"],
                        "manifest_sha256": manifest_sha256,
                    }
                )
        v1, v2 = exports["v1"], exports["v2"]
        outcome["archives"] = {
            k: {"archive_sha256": v["record"]["archive_sha256"], "manifest_sha256": v["manifest_sha256"]}
            for k, v in exports.items()
        }
        if v1["record"]["archive_sha256"] == v2["record"]["archive_sha256"]:
            raise ScenarioFailed("the two exports are byte-identical; the version marker did not reach the bundle")
        stack_name = v1["root"].name.removesuffix("-cfn")
        if v2["root"].name != v1["root"].name:
            raise ScenarioFailed("the two exports name different stacks; an update needs the same stack name")

        bucket = _recipient_bucket(binding, artifacts)
        outcome["recipient_bucket"] = bucket

        def deploy(version: str) -> dict[str, str]:
            export = exports[version]
            run_logged(
                ["bash", "deploy.sh", stack_name, binding.region, bucket],
                cwd=export["root"],
                env=anchored_env(env, export["record"], PIP_DISABLE_PIP_VERSION_CHECK="1"),
                timeout=2400,
                log=log,
                label=f"deploy-{version}",
            )
            return _stack_outputs(binding.region, stack_name)

        outputs = deploy("v1")
        runtime_arn = _runtime_arn(outputs)
        runtime_id = runtime_arn.rsplit("/", 1)[-1]
        outcome["stack_name"], outcome["runtime_arn"] = stack_name, runtime_arn
        outcome["invoke_v1"] = _invoke(binding.region, runtime_arn, MARKER_ONE, "v1", artifacts)

        outputs_v2 = deploy("v2")
        if _runtime_arn(outputs_v2) != runtime_arn:
            raise ScenarioFailed("the update replaced the Runtime instead of updating it in place")
        outcome["invoke_v2"] = _invoke(binding.region, runtime_arn, MARKER_TWO, "v2", artifacts)

        # Rollback = redeploy the exact prior bundle; production must answer as version 1 again.
        outputs_rb = deploy("v1")
        if _runtime_arn(outputs_rb) != runtime_arn:
            raise ScenarioFailed("the rollback replaced the Runtime instead of updating it in place")
        outcome["invoke_after_rollback"] = _invoke(binding.region, runtime_arn, MARKER_ONE, "rollback", artifacts)
        outcome["rollback"] = {"restored_prior_behavior": True, "method": "redeploy exact version-1 bundle"}

        # Foreign decoys: resources that LOOK adjacent to what the teardown sweeps but belong to
        # someone else -- sibling stack prefixes in the same bucket, a sibling runtime log group.
        # A teardown that reaches past its own stack would take these with it.
        decoys = _plant_decoys(binding.region, bucket, stack_name)
        outcome["decoys"] = {"planted": sorted(decoys)}
        decoy_log_group = next(name for name, d in decoys.items() if d["kind"] == "log_group")

        run_logged(
            ["bash", "teardown.sh", stack_name, binding.region],
            cwd=v1["root"],
            env=anchored_env(env, v1["record"]),
            timeout=1800,
            log=log,
            label="teardown",
        )
        first = _inventory(binding.region, stack_name, bucket, runtime_id)
        time.sleep(30)
        second = _inventory(binding.region, stack_name, bucket, runtime_id)
        outcome["teardown_inventory"] = {"first": first, "second": second}
        residue = any(
            [
                first["stack"] != "ABSENT" and first["stack"] != "DELETE_COMPLETE",
                second["stack"] != "ABSENT" and second["stack"] != "DELETE_COMPLETE",
                first["stack_prefixed_objects"] or second["stack_prefixed_objects"],
                first["runtimes"] or second["runtimes"],
                first["runtime_log_groups"] or second["runtime_log_groups"],
                first["user_pools"] or second["user_pools"],
            ]
        )
        outcome["teardown_zero"] = not residue
        if residue:
            raise ScenarioFailed(f"teardown left residue: {json.dumps(second)[:600]}")
        preserved = _verify_decoys(binding.region, bucket, decoys)
        outcome["decoys"]["preserved"] = preserved
        if not all(preserved.values()):
            raise ScenarioFailed(f"teardown reached foreign resources: {preserved}")
    finally:
        cleanup: dict[str, Any] = {}
        if decoy_log_group is not None:
            try:
                boto3_session(binding.region).client("logs").delete_log_group(logGroupName=decoy_log_group)
                cleanup["decoy_log_group_removed"] = True
            except Exception as exc:  # noqa: BLE001
                cleanup["decoy_log_group_error"] = redact(f"{type(exc).__name__}: {exc}")
        if bucket is not None:
            try:
                cleanup["bucket"] = _empty_and_delete_bucket(binding.region, bucket)
            except Exception as exc:  # noqa: BLE001
                cleanup["bucket_error"] = redact(f"{type(exc).__name__}: {exc}")
        shutil.rmtree(recipient_root, ignore_errors=True)
        cleanup["recipient_removed"] = not recipient_root.exists()
        outcome["cleanup"] = cleanup
        write_json(artifacts / "journey-outcome.json", outcome)
    if outcome["cleanup"].get("bucket_error") or outcome["cleanup"].get("decoy_log_group_error"):
        raise ScenarioFailed(f"recipient cleanup incomplete: {outcome['cleanup']}")
    if not outcome["cleanup"]["recipient_removed"]:
        raise ScenarioFailed(f"recipient cleanup incomplete: {outcome['cleanup']}")
    return outcome


def _scenario(args: argparse.Namespace, artifact_dir: Path, claim: str) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    journey = family_journey(
        binding,
        observed,
        family=FAMILY,
        run=lambda artifacts: _journey(binding, observed, artifacts),
        artifact_dir=artifact_dir,
    )
    outcome = journey["outcome"]
    if claim == "full-journey":
        holds = (
            outcome.get("invoke_v1") is not None
            and outcome.get("invoke_v2") is not None
            and (outcome.get("rollback") or {}).get("restored_prior_behavior") is True
            and outcome.get("teardown_zero") is True
        )
    elif claim == "rollback":
        holds = (outcome.get("rollback") or {}).get("restored_prior_behavior") is True and outcome.get(
            "invoke_after_rollback"
        ) is not None
    elif claim == "teardown-zero":
        holds = outcome.get("teardown_zero") is True
    elif claim == "decoys-preserved":
        preserved = (outcome.get("decoys") or {}).get("preserved") or {}
        holds = bool(preserved) and all(preserved.values()) and outcome.get("teardown_zero") is True
    else:
        raise ScenarioFailed(f"unknown claim {claim}")
    archives = outcome.get("archives") or {}
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journey": {k: v for k, v in journey.items() if k != "outcome"},
        "claim": claim,
        "claim_holds": holds,
        "stack_name": outcome.get("stack_name"),
        "archives": archives,
        "teardown_inventory": outcome.get("teardown_inventory"),
        "decoys": outcome.get("decoys"),
    }
    write_json(artifact_dir / "report", report)
    v1 = archives.get("v1") or {}
    if v1:
        write_provenance(
            artifact_dir,
            binding,
            observed,
            archive_sha256=v1["archive_sha256"],
            artifact_manifest_sha256=v1["manifest_sha256"],
        )
    if not holds:
        raise ScenarioFailed(f"{claim} does not hold in the journey outcome")
    return report


def api_authenticated_cfn_download_full_journey(args: argparse.Namespace, artifact_dir: Path) -> dict:
    return _scenario(args, artifact_dir, "full-journey")


def downloaded_cfn_update_rollback_restores_prior_behavior(args: argparse.Namespace, artifact_dir: Path) -> dict:
    return _scenario(args, artifact_dir, "rollback")


def downloaded_cfn_teardown_direct_service_inventories_zero(args: argparse.Namespace, artifact_dir: Path) -> dict:
    return _scenario(args, artifact_dir, "teardown-zero")


def foreign_decoys_preserved(args: argparse.Namespace, artifact_dir: Path) -> dict:
    """``lifecycle.zero_residual``: the teardown that proved zero residue also left every adjacent
    foreign resource byte-identical -- it removed exactly its own stack and nothing beside it."""
    return _scenario(args, artifact_dir, "decoys-preserved")
