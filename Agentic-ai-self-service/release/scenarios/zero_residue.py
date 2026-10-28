"""``lifecycle.zero_residual`` -- after the campaign, the Region holds nothing the campaign made.

Two scenario ids read the AWS control planes directly (never the platform's own tables as the
source of truth) and require zero campaign-owned residue, twice, 30 seconds apart, with both
reads agreeing:

* ``direct-service-inventories-zero`` -- AgentCore Runtimes and Gateways with campaign names,
  Cognito users with the campaign's synthetic-identity prefix, invocation budget and reservation
  rows the live tests create, and recipient buckets the download journey creates.
* ``aliasless-kms-and-log-groups-zero`` -- customer-managed KMS keys with no alias that the
  campaign created (the platform's own keys are aliased or stack-tagged), and log groups of
  campaign runtimes.

Both are *facets of the whole Region's campaign*: they block until every other live journey of
this deployment in this Region has passed (a teardown read before the campaign ran would prove
nothing), and they never delete anything -- residue is reported and the scenario fails.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from _live import (
    JOURNEY_CACHE_ROOT,
    ObservedStack,
    RunBinding,
    ScenarioBlocked,
    ScenarioFailed,
    boto3_session,
    bound_deployment,
    write_json,
)

CAMPAIGN_RUNTIME_PREFIXES = ("it_", "acfpr")
CAMPAIGN_USER_PREFIXES = ("acfpr.",)
CAMPAIGN_BUCKET_PREFIX = "acfpr-recipient-"
REQUIRED_JOURNEYS = ("core-lifecycle", "core-gateway", "core-features", "cfn-recipient")


def _require_campaign_complete(binding: RunBinding, observed: ObservedStack) -> dict[str, Any]:
    """Block unless every required journey passed on THIS deployment in THIS Region."""
    seen: dict[str, Any] = {}
    for family in REQUIRED_JOURNEYS:
        path = JOURNEY_CACHE_ROOT / binding.commit / binding.region / family / "journey.json"
        if not path.is_file():
            raise ScenarioBlocked(f"journey {family} has not run for {binding.commit[:12]} in {binding.region}")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("stack_id") != observed.stack_id or record.get("passed") is not True:
            raise ScenarioBlocked(f"journey {family} has not passed on this exact deployment")
        seen[family] = record["completed_at_epoch"]
    return seen


def _campaign_named(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.startswith(prefix) for prefix in CAMPAIGN_RUNTIME_PREFIXES)


def _service_inventory(binding: RunBinding, observed: ObservedStack) -> dict[str, Any]:
    session = boto3_session(binding.region)
    control = session.client("bedrock-agentcore-control")
    cognito = session.client("cognito-idp")
    dynamodb = session.client("dynamodb")
    s3 = session.client("s3")
    inventory: dict[str, Any] = {}

    runtimes = control.list_agent_runtimes(maxResults=100).get("agentRuntimes", [])
    inventory["campaign_runtimes"] = sorted(
        r["agentRuntimeName"] for r in runtimes if _campaign_named(r.get("agentRuntimeName", ""))
    )
    gateways = control.list_gateways(maxResults=100).get("items", [])
    inventory["campaign_gateways"] = sorted(g.get("name", "") for g in gateways if _campaign_named(g.get("name", "")))

    pool = observed.outputs["UserPoolId"]
    users: list[str] = []
    paginator = cognito.get_paginator("list_users")
    for page in paginator.paginate(UserPoolId=pool):
        for user in page.get("Users", []):
            email = next((a["Value"] for a in user.get("Attributes", []) if a["Name"] == "email"), "")
            if any(email.startswith(prefix) for prefix in CAMPAIGN_USER_PREFIXES):
                users.append(email)
    inventory["campaign_users"] = sorted(users)

    fixture_rows: dict[str, int] = {}
    for table in ("acf-production-budgets", "acf-production-reservations"):
        try:
            count = 0
            scan = dynamodb.get_paginator("scan")
            for page in scan.paginate(TableName=table, Select="ALL_ATTRIBUTES"):
                for item in page.get("Items", []):
                    blob = json.dumps(item, sort_keys=True).lower()
                    if "it_" in blob or "acfpr" in blob or "integration" in blob:
                        count += 1
            fixture_rows[table] = count
        except dynamodb.exceptions.ResourceNotFoundException:
            fixture_rows[table] = -1  # table not part of this platform version
    inventory["campaign_fixture_rows"] = fixture_rows

    buckets = s3.list_buckets().get("Buckets", [])
    recipient = []
    for bucket in buckets:
        name = bucket["Name"]
        if name.startswith(CAMPAIGN_BUCKET_PREFIX) and f"-{binding.region}-" in name:
            recipient.append(name)
    inventory["campaign_recipient_buckets"] = sorted(recipient)
    return inventory


def _kms_and_logs_inventory(binding: RunBinding) -> dict[str, Any]:
    session = boto3_session(binding.region)
    kms, logs = session.client("kms"), session.client("logs")
    aliased: set[str] = set()
    for page in kms.get_paginator("list_aliases").paginate():
        for alias in page.get("Aliases", []):
            if alias.get("TargetKeyId"):
                aliased.add(alias["TargetKeyId"])
    aliasless_campaign: list[str] = []
    for page in kms.get_paginator("list_keys").paginate():
        for key in page.get("Keys", []):
            key_id = key["KeyId"]
            if key_id in aliased:
                continue
            meta = kms.describe_key(KeyId=key_id)["KeyMetadata"]
            if meta.get("KeyManager") != "CUSTOMER" or meta.get("KeyState") in ("PendingDeletion", "Disabled"):
                continue
            try:
                tags = kms.list_resource_tags(KeyId=key_id).get("Tags", [])
            except Exception:  # noqa: BLE001 -- untaggable/foreign keys are simply not ours
                tags = []
            tag_blob = json.dumps(tags).lower()
            description = (meta.get("Description") or "").lower()
            stack_owned = "aws:cloudformation:stack-name" in tag_blob and "acf-production" in tag_blob
            if stack_owned:
                continue
            if "acfpr" in description or "acfpr" in tag_blob or "integration" in description or "it_" in description:
                aliasless_campaign.append(key_id)
    campaign_groups: list[str] = []
    for prefix in (
        "/aws/bedrock-agentcore/runtimes/it_",
        "/aws/bedrock-agentcore/runtimes/acfpr",
        "/aws/lambda/it_",
        "/acfpr",
    ):
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            campaign_groups += [g["logGroupName"] for g in page.get("logGroups", [])]
    return {"aliasless_campaign_kms_keys": sorted(aliasless_campaign), "campaign_log_groups": sorted(campaign_groups)}


def _two_reads(read, label: str, artifact_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    first = read()
    time.sleep(30)
    second = read()
    write_json(artifact_dir / f"{label}-first", first)
    write_json(artifact_dir / f"{label}-second", second)
    if first != second:
        raise ScenarioFailed(f"{label}: the two inventory reads disagree; the Region is not stable")
    return first, second


def _residue(inventory: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key, value in inventory.items():
        if isinstance(value, list) and value:
            found.append(f"{key}: {len(value)}")
        if isinstance(value, dict):
            for table, count in value.items():
                if isinstance(count, int) and count > 0:
                    found.append(f"{key}.{table}: {count}")
    return found


def direct_service_inventories_zero(args: argparse.Namespace, artifact_dir: Path) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    journeys = _require_campaign_complete(binding, observed)
    first, _ = _two_reads(lambda: _service_inventory(binding, observed), "service-inventory", artifact_dir)
    residue = _residue(first)
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journeys_completed_at": journeys,
        "inventory": first,
        "residue": residue,
    }
    write_json(artifact_dir / "report", report)
    if residue:
        raise ScenarioFailed("campaign residue in direct service inventories: " + "; ".join(residue))
    return report


def aliasless_kms_and_log_groups_zero(args: argparse.Namespace, artifact_dir: Path) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    journeys = _require_campaign_complete(binding, observed)
    first, _ = _two_reads(lambda: _kms_and_logs_inventory(binding), "kms-logs-inventory", artifact_dir)
    residue = _residue(first)
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journeys_completed_at": journeys,
        "inventory": first,
        "residue": residue,
    }
    write_json(artifact_dir / "report", report)
    if residue:
        raise ScenarioFailed("alias-less KMS keys or campaign log groups remain: " + "; ".join(residue))
    return report
