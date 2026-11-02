"""``crypto.target_kms`` — the evaluation target-CMK controls on the deployed stack.

One cached journey proves, against ``acf-production``:

* **target-cmk-encrypts-batch-evaluation** — the evaluation KMS key configured on the platform's
  deployments is a CUSTOMER-managed, single-Region ``ENCRYPT_DECRYPT`` key aliased
  ``alias/acf-production-promotion-evidence`` whose key policy grants only
  ``kms:GenerateDataKey``/``kms:Decrypt`` to ``bedrock-agentcore.amazonaws.com`` scoped by
  ``aws:SourceAccount`` and a ``batch-evaluate/*`` ``SourceArn``; and CloudTrail shows
  ``bedrock-agentcore`` used exactly that key to ``GenerateDataKey`` for real batch evaluations
  (``batchEvaluationArn`` encryption context, no error, in-Region).
* **wrong-account-key-denied** / **wrong-region-key-denied** — configuring a region target with an
  evaluation-key ARN in another account, or another Region, is refused 400 with the exact detail and
  writes no target row.

To reach the region-config validator the journey briefly holds the platform's
``deploy_targets_enabled`` flag under a fixture-owned lease and a private artifact bucket; both, and
the synthetic super-admin, are torn down and the flag/target config is verified back to its exact
baseline in a ``finally``.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from _durable import sha256_bytes
from _live import (
    REQUIRED_USER_GROUPS,
    BearerSession,
    ObservedStack,
    RunBinding,
    ScenarioFailed,
    boto3_session,
    bound_deployment,
    family_journey,
    with_synthetic_user,
    write_json,
)

FAMILY = "kms-controls"
KMS_GROUPS = REQUIRED_USER_GROUPS | {"g-admins-super"}  # admin persona for /api/admin/deploy-targets
SETTINGS_TABLE = "acf-production-tag-policy"
DEPLOYMENTS_TABLE = "acf-production-deployments"
FLAG_KEY = {"org_id": "default", "sk": "SETTING#deploy_targets_enabled"}
EVAL_ALIAS = "alias/acf-production-promotion-evidence"
WRONG_ACCOUNT = "111122223333"


def _wrong_region(region: str) -> str:
    return "eu-central-1" if region != "eu-central-1" else "us-east-1"


def _plain(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


# --------------------------------------------------------------------------- deploy-targets admin API


def _api_state(http: Any) -> dict[str, Any]:
    response = http.get(f"{http.base_url}/api/admin/deploy-targets", timeout=60)
    if response.status_code != 200:
        raise ScenarioFailed(f"deploy-targets GET returned {response.status_code}: {response.text[:200]}")
    return response.json()


def _wait_enabled(http: Any, expected: bool) -> dict[str, Any]:
    deadline = time.monotonic() + 45
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _api_state(http)
        if last.get("enabled") is expected:
            return last
        time.sleep(2)
    raise ScenarioFailed(f"deploy-targets gate did not become enabled={expected}: {last}")


def _deny(http: Any, region: str, bucket: str, key_arn: str, expected_detail: str) -> dict[str, Any]:
    response = http.post(
        f"{http.base_url}/api/admin/deploy-targets/regions",
        json={"region": region, "evaluation_kms_key_arn": key_arn, "artifact_bucket": bucket},
        timeout=90,
    )
    if response.status_code != 400:
        raise ScenarioFailed(f"bad evaluation key was not refused 400: {response.status_code} {response.text[:200]}")
    detail = str(response.json().get("detail") or "")
    if detail != expected_detail:
        raise ScenarioFailed(f"denial detail differed:\n  got: {detail}\n  want: {expected_detail}")
    return {"status": 400, "detail": detail, "rejectedKeyArn": key_arn}


# --------------------------------------------------------------------------- flag lease + baseline


def _target_rows(table: Any) -> list[dict[str, Any]]:
    rows = table.query(
        KeyConditionExpression="org_id = :org",
        ExpressionAttributeValues={":org": "default"},
        ConsistentRead=True,
    ).get("Items", [])
    return [_plain(r) for r in rows if str(r.get("sk", "")).startswith("DEPLOY_TARGET#")]


def _settings_state(table: Any) -> dict[str, Any]:
    flag = table.get_item(Key=FLAG_KEY, ConsistentRead=True).get("Item")
    return {"flag": _plain(flag), "targets": _target_rows(table)}


def _acquire_flag(table: Any, fixture_id: str) -> None:
    table.update_item(
        Key=FLAG_KEY,
        UpdateExpression="SET #value = :enabled, fixture_id = :fixture, expires_epoch = :expires",
        ConditionExpression="attribute_not_exists(org_id) AND attribute_not_exists(sk)",
        ExpressionAttributeNames={"#value": "value"},
        ExpressionAttributeValues={":enabled": "true", ":fixture": fixture_id, ":expires": int(time.time()) + 30 * 60},
    )


def _release_flag(table: Any, fixture_id: str) -> None:
    from botocore.exceptions import ClientError

    try:
        table.delete_item(
            Key=FLAG_KEY,
            ConditionExpression="fixture_id = :fixture AND #value = :enabled",
            ExpressionAttributeNames={"#value": "value"},
            ExpressionAttributeValues={":fixture": fixture_id, ":enabled": "true"},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        if table.get_item(Key=FLAG_KEY, ConsistentRead=True).get("Item") is None:
            return
        raise ScenarioFailed("refusing to remove a deploy-target flag not owned by this fixture") from error


# --------------------------------------------------------------------------- private bucket


def _create_bucket(session: Any, region: str, name: str) -> None:
    s3 = session.client("s3")
    kwargs: dict[str, Any] = {"Bucket": name}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kwargs)
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=name,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )


def _delete_bucket(session: Any, name: str) -> bool:
    from botocore.exceptions import ClientError

    s3 = session.client("s3")
    try:
        s3.head_bucket(Bucket=name)
    except ClientError:
        return False
    s3.delete_bucket(Bucket=name)
    return True


# --------------------------------------------------------------------------- positive CMK + CloudTrail


def _eval_cmk_arn(session: Any) -> str:
    table = session.resource("dynamodb").Table(DEPLOYMENTS_TABLE)
    arns: set[str] = set()
    kwargs: dict[str, Any] = {"ProjectionExpression": "evaluation_kms_key_arn", "ConsistentRead": True}
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            arn = item.get("evaluation_kms_key_arn")
            if arn:
                arns.add(str(arn))
        key = page.get("LastEvaluatedKey")
        if not key:
            break
        kwargs["ExclusiveStartKey"] = key
    if len(arns) != 1:
        raise ScenarioFailed(f"expected exactly one evaluation KMS key across deployments, found {sorted(arns)}")
    return arns.pop()


def _verify_cmk(session: Any, key_arn: str, region: str, account_id: str) -> dict[str, Any]:
    kms = session.client("kms")
    metadata = kms.describe_key(KeyId=key_arn)["KeyMetadata"]
    checks = {
        "arn": metadata["Arn"] == key_arn,
        "account": metadata["AWSAccountId"] == account_id,
        "customer_managed": metadata["KeyManager"] == "CUSTOMER",
        "symmetric": metadata["KeySpec"] == "SYMMETRIC_DEFAULT",
        "encrypt_decrypt": metadata["KeyUsage"] == "ENCRYPT_DECRYPT",
        "enabled": metadata["KeyState"] == "Enabled" and metadata["Enabled"] is True,
        "single_region": metadata["MultiRegion"] is False,
    }
    aliases = sorted(str(a["AliasName"]) for a in kms.list_aliases(KeyId=metadata["KeyId"]).get("Aliases", []))
    checks["alias"] = aliases == [EVAL_ALIAS]
    raw_policy = kms.get_key_policy(KeyId=key_arn, PolicyName="default")["Policy"]
    policy = json.loads(raw_policy) if isinstance(raw_policy, str) else raw_policy
    service = next(
        (
            s
            for s in policy.get("Statement", [])
            if s.get("Principal") == {"Service": "bedrock-agentcore.amazonaws.com"}
        ),
        None,
    )
    expected_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:batch-evaluate/*"
    checks["key_policy"] = bool(
        service
        and set(service.get("Action") or []) == {"kms:GenerateDataKey", "kms:Decrypt"}
        and service.get("Condition", {}).get("StringEquals") == {"aws:SourceAccount": account_id}
        and service.get("Condition", {}).get("ArnLike") == {"aws:SourceArn": expected_arn}
    )
    if not all(checks.values()):
        raise ScenarioFailed(f"evaluation CMK does not match the exact target-CMK contract: {checks}")
    return {
        "keyArn": key_arn,
        "keyMaterialId": metadata.get("CurrentKeyMaterialId"),
        "aliases": aliases,
        "checks": checks,
    }


def _cloudtrail_positive(session: Any, key_arn: str, region: str, key_material_id: str | None) -> dict[str, Any]:
    ct = session.client("cloudtrail")
    paginator = ct.get_paginator("lookup_events")
    end = datetime.now(UTC)
    start = end - timedelta(hours=24)
    events: list[dict[str, Any]] = []
    for page in paginator.paginate(
        LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "GenerateDataKey"}],
        StartTime=start,
        EndTime=end,
    ):
        for event in page.get("Events", []):
            detail = json.loads(event.get("CloudTrailEvent") or "{}")
            rp = detail.get("requestParameters") or {}
            ctx = rp.get("encryptionContext") or {}
            batch_arn = str(ctx.get("aws:bedrock-agentcore:batchEvaluationArn") or "")
            if not batch_arn or rp.get("keyId") != key_arn:
                continue
            if detail.get("userIdentity", {}).get("invokedBy") != "bedrock-agentcore.amazonaws.com":
                continue
            if detail.get("errorCode") is not None or detail.get("awsRegion") != region:
                continue
            if key_material_id and detail.get("additionalEventData", {}).get("keyMaterialId") not in (
                None,
                key_material_id,
            ):
                continue
            events.append(
                {
                    "eventId": detail.get("eventID"),
                    "eventTime": detail.get("eventTime"),
                    "batchEvaluationArn": batch_arn,
                }
            )
    if len(events) < 2:
        raise ScenarioFailed(
            f"expected >=2 bedrock-agentcore batch-evaluation GenerateDataKey events on the CMK, found {len(events)}"
        )
    return {"batchGenerateDataKeyEvents": events[:8], "count": len(events)}


# --------------------------------------------------------------------------- journey


def _journey(args: argparse.Namespace, binding: RunBinding, observed: ObservedStack, artifacts: Path) -> dict[str, Any]:
    region = binding.region
    account_id = str(args.account_id)
    token = uuid.uuid4().hex[:12]
    fixture_id = f"itkms-{token}"
    bucket = f"agentcore-flows-artifacts-{account_id}-{fixture_id}"
    outcome: dict[str, Any] = {"region": region, "run_token": token}
    session = boto3_session(region)
    settings = session.resource("dynamodb").Table(SETTINGS_TABLE)
    baseline = _settings_state(settings)
    if baseline["flag"] is not None:
        raise ScenarioFailed("deploy_targets_enabled flag is already held; refusing to contend")
    flag_held = bucket_made = False
    with with_synthetic_user(observed, label="kms", groups=KMS_GROUPS) as user:
        http = BearerSession(user, observed.cloudfront_url)
        try:
            baseline_api = _wait_enabled(http, False)
            outcome["baseline_api_digest"] = sha256_bytes(json.dumps(baseline_api, sort_keys=True).encode())
            _create_bucket(session, region, bucket)
            bucket_made = True
            _acquire_flag(settings, fixture_id)
            flag_held = True
            enabled_api = _wait_enabled(http, True)
            if enabled_api.get("regions") not in ([], None) or enabled_api.get("accounts") not in ([], None):
                raise ScenarioFailed(f"enabling the gate unexpectedly populated targets: {enabled_api}")

            wrong_account_arn = f"arn:aws:kms:{region}:{WRONG_ACCOUNT}:key/00000000-0000-4000-8000-000000000001"
            outcome["wrong-account-key-denied"] = _deny(
                http,
                region,
                bucket,
                wrong_account_arn,
                f"Evaluation KMS key belongs to account {WRONG_ACCOUNT}, not target account {account_id}",
            )
            if _target_rows(settings):
                raise ScenarioFailed("wrong-account denial wrote a target row")

            wrong_region = _wrong_region(region)
            wrong_region_arn = f"arn:aws:kms:{wrong_region}:{account_id}:key/00000000-0000-4000-8000-000000000002"
            outcome["wrong-region-key-denied"] = _deny(
                http,
                region,
                bucket,
                wrong_region_arn,
                f"Evaluation KMS key is in region {wrong_region}, not target region {region}",
            )
            if _target_rows(settings):
                raise ScenarioFailed("wrong-region denial wrote a target row")

            key_arn = _eval_cmk_arn(session)
            cmk = _verify_cmk(session, key_arn, region, account_id)
            positive = _cloudtrail_positive(session, key_arn, region, cmk.get("keyMaterialId"))
            outcome["target-cmk-encrypts-batch-evaluation"] = {**cmk, **positive}
        finally:
            cleanup: dict[str, Any] = {}
            if flag_held:
                _release_flag(settings, fixture_id)
            after = _settings_state(settings)
            cleanup["flag_restored"] = after == baseline
            if not cleanup["flag_restored"]:
                raise ScenarioFailed(f"deploy-target settings did not return to baseline: {after}")
            restored_api = _wait_enabled(http, False)
            cleanup["api_restored"] = sha256_bytes(json.dumps(restored_api, sort_keys=True).encode()) == outcome.get(
                "baseline_api_digest"
            )
            if bucket_made:
                cleanup["bucket_removed"] = _delete_bucket(session, bucket)
            outcome["cleanup"] = cleanup
            http.close()
    write_json(artifacts / "journey-outcome.json", outcome)
    return outcome


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
    facet = outcome.get(claim) or {}
    if claim == "target-cmk-encrypts-batch-evaluation":
        holds = bool(facet.get("checks")) and all(facet["checks"].values()) and facet.get("count", 0) >= 2
    elif claim in ("wrong-account-key-denied", "wrong-region-key-denied"):
        holds = facet.get("status") == 400 and bool(facet.get("detail"))
    else:
        raise ScenarioFailed(f"unknown KMS claim {claim}")
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journey": {k: v for k, v in journey.items() if k != "outcome"},
        "claim": claim,
        "claim_holds": holds,
        "facet": facet,
        "cleanup": outcome.get("cleanup"),
    }
    write_json(artifact_dir / "report", report)
    if not holds:
        raise ScenarioFailed(f"{claim}: {json.dumps(facet)[:300]}")
    return report


def target_cmk_encrypts_batch_evaluation(args, artifact_dir):
    return _scenario(args, artifact_dir, "target-cmk-encrypts-batch-evaluation")


def wrong_account_key_denied(args, artifact_dir):
    return _scenario(args, artifact_dir, "wrong-account-key-denied")


def wrong_region_key_denied(args, artifact_dir):
    return _scenario(args, artifact_dir, "wrong-region-key-denied")
