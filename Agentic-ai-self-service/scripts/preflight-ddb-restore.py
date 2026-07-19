#!/usr/bin/env python3
"""Preflight: recreate DynamoDB tables deleted out-of-band (e.g. by an
account-level resource reaper) before running ``cdk deploy``.

Problem this solves
-------------------
If a table in the stack is deleted outside CloudFormation, CFN's state still
says CREATE_COMPLETE. The next stack UPDATE that contains an
``Fn::GetAtt <Table>.Arn`` (IAM policies do) fails with::

    Unable to retrieve Arn attribute for AWS::DynamoDB::Table,
    with error message Table: <name> does not exist.

and the stack rolls back (UPDATE_ROLLBACK_COMPLETE). CFN will not recreate
the table itself because, per its state, the table already exists.

Fix: recreate each missing table EMPTY with the exact schema from the
*deployed* stack template (the one CFN reconciles against), wait for ACTIVE,
re-apply TTL/PITR, then let ``cdk deploy`` proceed normally. Data is lost —
acceptable for dev state tables; in prod, PITR/backups are the answer.

Usage:
    python3 preflight-ddb-restore.py [--stack-name NAME] [--region REGION]

Exits 0 when nothing was missing (healthy / fresh deploy) or when all
missing tables were restored. Exits 1 on restore failure.
"""

import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError


def get_deployed_table_specs(cfn, stack_name):
    """Return {physical_table_name: cfn_properties} for every DynamoDB table
    in the *deployed* stack template. Empty dict if the stack doesn't exist
    (fresh deploy — nothing to reconcile)."""
    try:
        template = cfn.get_template(StackName=stack_name, TemplateStage="Processed")["TemplateBody"]
    except ClientError as exc:
        if "does not exist" in str(exc):
            return {}
        raise

    if not isinstance(template, dict):  # YAML-bodied templates arrive as str
        import json

        template = json.loads(template)

    logical_specs = {
        lid: res.get("Properties", {})
        for lid, res in template.get("Resources", {}).items()
        if res.get("Type") == "AWS::DynamoDB::Table"
    }
    if not logical_specs:
        return {}

    # Map logical ids -> physical names via the stack's actual resources
    # (don't trust Properties.TableName: it may be absent for generated names).
    specs = {}
    paginator = cfn.get_paginator("list_stack_resources")
    for page in paginator.paginate(StackName=stack_name):
        for res in page["StackResourceSummaries"]:
            lid = res["LogicalResourceId"]
            if lid in logical_specs and res.get("PhysicalResourceId"):
                specs[res["PhysicalResourceId"]] = logical_specs[lid]
    return specs


def table_exists(ddb, name):
    try:
        status = ddb.describe_table(TableName=name)["Table"]["TableStatus"]
        return status != "DELETING"
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise


def create_table_from_spec(ddb, name, props):
    """Recreate a table from its CloudFormation properties block."""
    kwargs = {
        "TableName": name,
        "AttributeDefinitions": props["AttributeDefinitions"],
        "KeySchema": props["KeySchema"],
        "BillingMode": props.get("BillingMode", "PROVISIONED"),
    }
    if props.get("GlobalSecondaryIndexes"):
        # CFN GSI shape matches create_table for on-demand tables.
        kwargs["GlobalSecondaryIndexes"] = [
            {k: v for k, v in gsi.items() if k in ("IndexName", "KeySchema", "Projection")}
            for gsi in props["GlobalSecondaryIndexes"]
        ]
    sse = props.get("SSESpecification")
    if sse and sse.get("SSEEnabled"):
        kwargs["SSESpecification"] = {"Enabled": True}
        if sse.get("SSEType"):
            kwargs["SSESpecification"]["SSEType"] = sse["SSEType"]
        if sse.get("KMSMasterKeyId"):
            kwargs["SSESpecification"]["KMSMasterKeyId"] = sse["KMSMasterKeyId"]
    if props.get("Tags"):
        kwargs["Tags"] = props["Tags"]
    if props.get("DeletionProtectionEnabled"):
        kwargs["DeletionProtectionEnabled"] = True

    ddb.create_table(**kwargs)
    ddb.get_waiter("table_exists").wait(TableName=name, WaiterConfig={"Delay": 5, "MaxAttempts": 60})

    # TTL and PITR can't be set at create time — re-apply post-ACTIVE.
    ttl = props.get("TimeToLiveSpecification")
    if ttl and ttl.get("Enabled"):
        ddb.update_time_to_live(
            TableName=name,
            TimeToLiveSpecification={
                "Enabled": True,
                "AttributeName": ttl["AttributeName"],
            },
        )
    pitr = props.get("PointInTimeRecoverySpecification")
    if pitr and pitr.get("PointInTimeRecoveryEnabled"):
        # Continuous backups finish enabling shortly after the table goes
        # ACTIVE; retry on ContinuousBackupsUnavailableException.
        for attempt in range(12):
            try:
                ddb.update_continuous_backups(
                    TableName=name,
                    PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
                )
                break
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code != "ContinuousBackupsUnavailableException" or attempt == 11:
                    raise
                time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack-name", default="agentcore-workflow-dev")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    cfn = session.client("cloudformation")
    ddb = session.client("dynamodb")

    specs = get_deployed_table_specs(cfn, args.stack_name)
    if not specs:
        print(
            f"[preflight] Stack '{args.stack_name}' has no deployed DynamoDB tables (fresh deploy?) — nothing to check."
        )
        return 0

    missing = {n: p for n, p in specs.items() if not table_exists(ddb, n)}
    if not missing:
        print(f"[preflight] All {len(specs)} DynamoDB tables present — OK.")
        return 0

    print(
        f"[preflight] {len(missing)}/{len(specs)} tables deleted "
        "out-of-band; recreating empty with the deployed schema:"
    )
    failures = []
    for name, props in sorted(missing.items()):
        print(f"[preflight]   restoring {name} ...", flush=True)
        try:
            create_table_from_spec(ddb, name, props)
            print(f"[preflight]   {name} ACTIVE.")
        except ClientError as exc:
            print(f"[preflight]   FAILED to restore {name}: {exc}", file=sys.stderr)
            failures.append(name)

    if failures:
        print(f"[preflight] Restore failed for: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(
        f"[preflight] Restored {len(missing)} table(s). "
        "Note: table data was lost when the tables were deleted; "
        "they have been recreated empty."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
