#!/usr/bin/env python3
"""sweep_orphans.py — plan/apply removal of project residue that no stack owns.

`final_teardown.py` removes what the live stacks own. Earlier deployments,
rolled-back creates and retained resources leave residue behind that no stack
owns any more: service log groups, `RETAIN` customer-managed keys, Cognito user
pools, workload identities, S3 buckets. This tool finds that residue with the
same name/description matchers as `residue_inventory.py` and removes it in two
reviewed steps:

    # 1. plan (read-only): writes the exact ids and prints them
    python3 scripts/sweep_orphans.py --expected-account <id> --region us-east-1 --plan-out plan.json
    # 2. apply exactly that plan (each entry is re-validated before it is touched)
    python3 scripts/sweep_orphans.py --expected-account <id> --region us-east-1 --apply plan.json

Fail-closed rules:

* the plan is refused while any project CloudFormation stack is still live in
  the region (sweep only after the stack teardown);
* an S3 bucket holding any object version under an active Object Lock
  retention (COMPLIANCE or GOVERNANCE) or a legal hold is PROTECTED: it is not
  emptied, and every KMS key that encrypts it (default encryption or any
  sampled object version) is PROTECTED too, so locked records stay readable;
* a KMS key used by a surviving log group is protected;
* keys are only ever scheduled for deletion (7-day window, cancellable), and
  only after everything else in the plan is gone;
* apply acts on plan entries only, re-checks each against the live predicate,
  and stops at the first unexpected error.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_SPEC = importlib.util.spec_from_file_location("residue_inventory", Path(__file__).with_name("residue_inventory.py"))
inv = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("residue_inventory", inv)
_SPEC.loader.exec_module(inv)

CFG = Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 5, "mode": "standard"})
KEY_PENDING_WINDOW_DAYS = 7
OBJECT_SAMPLE = 25
GONE = {"ResourceNotFoundException", "NoSuchBucket", "NotFoundException", "ParameterNotFound", "NotFoundException"}


def log(*parts: object) -> None:
    print(dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%SZ"), *parts, flush=True)


def delete_user_pool(cognito, pool_id: str) -> None:
    """Delete a user pool, switching deletion protection off first only when it
    is on. `UpdateUserPool` resets every setting it is not given, so it is not
    called on a pool that does not need it."""
    pool = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    if pool.get("DeletionProtection") == "ACTIVE":
        cognito.update_user_pool(UserPoolId=pool_id, DeletionProtection="INACTIVE")
    cognito.delete_user_pool(UserPoolId=pool_id)


def key_id_of(kms, reference: str) -> str:
    """A bucket's key reference may be a key id, a key ARN or an alias (name or ARN)."""
    if reference.startswith("alias/") or ":alias/" in reference:
        alias = reference if reference.startswith("alias/") else "alias/" + reference.split(":alias/", 1)[1]
        return kms.describe_key(KeyId=alias)["KeyMetadata"]["KeyId"]
    return reference.rsplit("/", 1)[-1]


class Sweep:
    def __init__(self, session: boto3.Session, region: str, now: dt.datetime | None = None) -> None:
        self.session, self.region = session, region
        self.now = now or dt.datetime.now(dt.timezone.utc)

    def client(self, name: str):
        return self.session.client(name, region_name=self.region, config=CFG)

    # ------------------------------------------------------------ guards

    def live_project_stacks(self) -> list[str]:
        cfn = self.client("cloudformation")
        live = [s for s in inv.paged(cfn, "list_stacks", "StackSummaries") if s["StackStatus"] != "DELETE_COMPLETE"]
        return sorted(s["StackName"] for s in live if inv.is_project_name(s["StackName"]))

    # ------------------------------------------------------------ S3

    def bucket_protection(self, bucket: str) -> tuple[str | None, set[str]]:
        """(reason it is protected or None, KMS key ids that encrypt it)."""
        s3 = self.client("s3")
        kms = self.client("kms")
        keys: set[str] = set()
        try:
            rules = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]["Rules"]
            for rule in rules:
                key = rule.get("ApplyServerSideEncryptionByDefault", {}).get("KMSMasterKeyID")
                if key:
                    keys.add(key_id_of(kms, key))
        except ClientError as error:
            if error.response["Error"]["Code"] != "ServerSideEncryptionConfigurationNotFoundError":
                raise
        locked_bucket = True
        try:
            s3.get_object_lock_configuration(Bucket=bucket)
        except ClientError as error:
            if error.response["Error"]["Code"] == "ObjectLockConfigurationNotFoundError":
                locked_bucket = False
            else:
                raise
        reason = None
        sampled = 0
        for version in inv.paged(s3, "list_object_versions", "Versions", Bucket=bucket):
            if sampled < OBJECT_SAMPLE:
                sampled += 1
                head = s3.head_object(Bucket=bucket, Key=version["Key"], VersionId=version["VersionId"])
                if head.get("SSEKMSKeyId"):
                    keys.add(key_id_of(kms, head["SSEKMSKeyId"]))
            if not locked_bucket or reason:
                continue
            try:
                retention = s3.get_object_retention(Bucket=bucket, Key=version["Key"], VersionId=version["VersionId"])["Retention"]
                until = retention.get("RetainUntilDate")
                if until and until > self.now:
                    reason = f"Object Lock {retention.get('Mode')} until {until.date()}"
            except ClientError as error:
                if error.response["Error"]["Code"] not in {"NoSuchObjectLockConfiguration", "InvalidRequest"}:
                    raise
            try:
                hold = s3.get_object_legal_hold(Bucket=bucket, Key=version["Key"], VersionId=version["VersionId"])
                if hold.get("LegalHold", {}).get("Status") == "ON":
                    reason = "legal hold"
            except ClientError as error:
                if error.response["Error"]["Code"] not in {"NoSuchObjectLockConfiguration", "InvalidRequest"}:
                    raise
        return reason, keys

    def buckets_in_region(self) -> list[str]:
        s3 = self.session.client("s3", config=CFG)
        out = []
        for bucket in s3.list_buckets().get("Buckets", []):
            name = bucket["Name"]
            if not inv.is_project_bucket(name):
                continue
            location = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
            if location == self.region:
                out.append(name)
        return sorted(out)

    # ------------------------------------------------------------ KMS

    def project_keys(self) -> list[dict]:
        kms = self.client("kms")
        aliases: dict[str, list[str]] = {}
        for alias in inv.paged(kms, "list_aliases", "Aliases"):
            if alias.get("TargetKeyId"):
                aliases.setdefault(alias["TargetKeyId"], []).append(alias["AliasName"])
        out = []
        for key in inv.paged(kms, "list_keys", "Keys"):
            meta = kms.describe_key(KeyId=key["KeyId"])["KeyMetadata"]
            if meta["KeyManager"] != "CUSTOMER" or meta["KeyState"] not in {"Enabled", "Disabled"}:
                continue
            names = aliases.get(key["KeyId"], [])
            if any(n.startswith(("alias/agenticai", "alias/AgenticAI")) for n in names) or inv.is_project_key_description(
                meta.get("Description", "")
            ):
                out.append({"id": key["KeyId"], "aliases": names, "description": meta.get("Description", "")})
        return out

    def keys_used_by_surviving_log_groups(self, doomed: set[str]) -> set[str]:
        used = set()
        for group in inv.paged(self.client("logs"), "describe_log_groups", "logGroups"):
            if group["logGroupName"] in doomed or not group.get("kmsKeyId"):
                continue
            used.add(group["kmsKeyId"].rsplit("/", 1)[-1])
        return used

    # ------------------------------------------------------------ plan

    def plan(self) -> dict:
        stacks = self.live_project_stacks()
        if stacks:
            raise SystemExit(f"REFUSING: project stacks are still live in {self.region}: {stacks}; run final_teardown.py first")
        plan: dict = {"region": self.region, "createdAt": self.now.isoformat(), "delete": {}, "protected": []}
        d = plan["delete"]
        d["logGroups"] = sorted(inv.Inventory(self.session, self.region).log_groups())
        control = self.client("bedrock-agentcore-control")
        d["workloadIdentities"] = sorted(
            w["name"] for w in inv.paged(control, "list_workload_identities", "workloadIdentities") if inv.is_project_name(w["name"])
        )
        d["userPools"] = sorted(
            [p["Id"], p["Name"]]
            for p in inv.paged(self.client("cognito-idp"), "list_user_pools", "UserPools", MaxResults=60)
            if inv.is_project_name(p["Name"])
        )
        d["buckets"], protected_keys = [], set()
        for bucket in self.buckets_in_region():
            reason, keys = self.bucket_protection(bucket)
            if reason:
                plan["protected"].append({"bucket": bucket, "reason": reason, "keys": sorted(keys)})
                protected_keys |= keys
            else:
                d["buckets"].append(bucket)
        protected_keys |= self.keys_used_by_surviving_log_groups(set(d["logGroups"]))
        d["kmsKeys"] = []
        for key in self.project_keys():
            if key["id"] in protected_keys:
                plan["protected"].append({"kmsKey": key["id"], "reason": "encrypts protected data", "description": key["description"]})
            else:
                d["kmsKeys"].append(key["id"])
        d["kmsKeys"].sort()
        return plan

    # ------------------------------------------------------------ apply

    def empty_and_delete_bucket(self, bucket: str) -> None:
        s3 = self.client("s3")
        while True:
            resp = s3.list_object_versions(Bucket=bucket, MaxKeys=1000)
            batch = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in resp.get("Versions", []) + resp.get("DeleteMarkers", [])]
            if not batch:
                break
            result = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            if result.get("Errors"):
                first = result["Errors"][0]
                raise SystemExit(f"STOPPED: could not empty {bucket}: {first.get('Code')} {first.get('Message')}")
        s3.delete_bucket(Bucket=bucket)

    def apply(self, plan: dict) -> None:
        if plan.get("region") != self.region:
            raise SystemExit(f"REFUSING: the plan is for {plan.get('region')}, not {self.region}")
        stacks = self.live_project_stacks()
        if stacks:
            raise SystemExit(f"REFUSING: project stacks are live again in {self.region}: {stacks}")
        live = self.plan()  # the live predicate; plan entries not in it are skipped, never widened
        d, now_d = plan["delete"], live["delete"]
        logs = self.client("logs")
        for group in d["logGroups"]:
            if group not in now_d["logGroups"]:
                log("   skip (no longer matches)", group)
                continue
            self._tolerate_gone(lambda: logs.delete_log_group(logGroupName=group), f"log group {group}")
        control = self.client("bedrock-agentcore-control")
        for name in d["workloadIdentities"]:
            if name in now_d["workloadIdentities"]:
                self._tolerate_gone(lambda: control.delete_workload_identity(name=name), f"workload identity {name}")
        cognito = self.client("cognito-idp")
        for pool_id, name in d["userPools"]:
            if [pool_id, name] not in now_d["userPools"]:
                continue

            def drop(pool_id=pool_id):
                delete_user_pool(cognito, pool_id)

            self._tolerate_gone(drop, f"user pool {name}")
        for bucket in d["buckets"]:
            if bucket not in now_d["buckets"]:
                log("   skip (now protected or gone)", bucket)
                continue
            self._tolerate_gone(lambda: self.empty_and_delete_bucket(bucket), f"bucket {bucket}")
        # Keys last, and only the ones the live predicate still frees.
        kms = self.client("kms")
        for key_id in d["kmsKeys"]:
            if key_id not in now_d["kmsKeys"]:
                log(f"   skip key ...{key_id[-4:]} (now protected or gone)")
                continue
            self._tolerate_gone(
                lambda: kms.schedule_key_deletion(KeyId=key_id, PendingWindowInDays=KEY_PENDING_WINDOW_DAYS),
                f"key ...{key_id[-4:]} (schedule deletion, {KEY_PENDING_WINDOW_DAYS} days)",
            )

    @staticmethod
    def _tolerate_gone(action, label: str) -> None:
        log("   delete", label)
        try:
            action()
        except ClientError as error:
            if error.response["Error"]["Code"] in GONE:
                log("      already gone")
            else:
                raise


def summarize(plan: dict) -> None:
    for surface, items in plan["delete"].items():
        log(f"{surface:20} {len(items)}")
        for item in items:
            label = item if isinstance(item, str) else " ".join(item[1:])
            log(f"{'':22}- {label if not label.startswith(('arn:',)) else label[-40:]}")
    for entry in plan["protected"]:
        log("PROTECTED", json.dumps(entry)[:200])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--expected-account", required=True)
    ap.add_argument("--region", required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-out", type=Path, help="compute the plan (read-only) and write it here")
    mode.add_argument("--apply", type=Path, help="apply this previously written plan")
    args = ap.parse_args()
    session = boto3.Session(region_name=args.region)
    actual = session.client("sts").get_caller_identity()["Account"]
    if actual != args.expected_account:
        raise SystemExit(f"REFUSING: credentials belong to ...{actual[-4:]}, expected ...{args.expected_account[-4:]}")
    sweep = Sweep(session, args.region)
    if args.plan_out:
        plan = sweep.plan()
        plan["account"] = actual
        args.plan_out.write_text(json.dumps(plan, indent=2, sort_keys=True))
        log("PLAN (read-only, nothing changed) ->", args.plan_out)
        summarize(plan)
        return 0
    plan = json.loads(args.apply.read_text())
    if plan.get("account") != actual:
        raise SystemExit("REFUSING: the plan was written for a different account")
    log("APPLY", args.apply)
    sweep.apply(plan)
    log("DONE; re-run residue_inventory.py to measure what is left")
    return 0


if __name__ == "__main__":
    sys.exit(main())
