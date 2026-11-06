#!/usr/bin/env python3
"""
omni_space_bootstrap.py -- post-deploy steps for a CloudWatch Omni hub space
that have no CloudFormation resource type (ADR-15).

    1. dataset-integration  create | verify | delete
         Forward the hub's log groups and traces into the CloudWatch Dataset
         (observabilityadmin CreateDatasetIntegration with the execution role
         the OmniHubStack created).
    2. access-grants        apply | verify | delete
         Create the space's access grants from a committed JSON file
         (cloudwatchomni CreateAccessGrant), resolving IAM Identity Center
         users and groups by name through SearchPrincipals. Grants only ever
         add; `delete` removes exactly the customer-managed grants the file
         names and nothing else.
    3. centralization      ensure-service-linked-roles | enable-context-graph | verify
         ensure-service-linked-roles (PRE-deploy, destination account): create
         AWSServiceRoleForObservabilityAdmin and
         AWSServiceRoleForObservabilityAdmin_LogsCentralization when trusted
         access was enabled through the Organizations API; the console creates
         them itself. The rule's tag-propagation role trusts the second one and
         IAM refuses a trust policy whose principal does not exist yet.
         enable-context-graph: opt the organization centralization rule into
         Context Graph centralization (cross-account application map). The
         Observability Admin API accepts SourceContextGraphConfiguration but
         the published CloudFormation schema does not yet, so it is applied here.
    4. inventory
         Read-only: the space, its grants and the Dataset integration, for
         the evidence bundle and for the zero-residue teardown check.

Every AWS call goes through a thin wrapper so the live gates can be
reproduced and the module can be unit-tested with botocore stubs. The script
never prints secrets and never deletes anything it did not create by name.

Run with the hub account's credentials, in the space Region, for example:

    python3 scripts/omni/omni_space_bootstrap.py --region eu-west-1 \
        dataset-integration create --role-arn arn:aws:iam::<hub>:role/CloudWatchOmniDatasetIntegrationRole-eu-west-1
    python3 scripts/omni/omni_space_bootstrap.py --region eu-west-1 \
        access-grants apply --domain-id d-xxxx --space-id <uuid> --grants-file assets/omni/grants.example.json

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import boto3
from botocore.exceptions import ClientError

PERMISSIONS = ("SPACE_ADMIN", "READ", "READ_WRITE_DELETE", "CUSTOM")
PRINCIPAL_TYPES = ("IDC_USER", "IDC_GROUP", "IAM_USER", "IAM_ROLE", "IAM_ROOT", "ACCESS_PROFILE", "ALERT", "AGENT")
# (service principal, role name) pairs the console creates when trusted access is enabled there.
CENTRALIZATION_SERVICE_LINKED_ROLES = (
    ("observabilityadmin.amazonaws.com", "AWSServiceRoleForObservabilityAdmin"),
    ("logs-centralization.observabilityadmin.amazonaws.com", "AWSServiceRoleForObservabilityAdmin_LogsCentralization"),
)


# --------------------------------------------------------------------------- clients


@dataclass
class Clients:
    """Lazily built boto3 clients; a test passes stubbed ones in."""

    region: str
    profile: str | None = None
    _omni: Any = None
    _obs: Any = None
    _iam: Any = None
    _factory: Callable[[str, str], Any] | None = None

    def __post_init__(self) -> None:
        if self._factory is None:
            session = boto3.session.Session(profile_name=self.profile, region_name=self.region)
            self._factory = lambda svc, region: session.client(svc, region_name=region)

    @property
    def factory(self) -> Callable[[str, str], Any]:
        assert self._factory is not None
        return self._factory

    @property
    def omni(self) -> Any:
        if self._omni is None:
            self._omni = self.factory("cloudwatchomni", self.region)
        return self._omni

    @property
    def iam(self) -> Any:
        if self._iam is None:
            self._iam = self.factory("iam", self.region)
        return self._iam

    @property
    def observabilityadmin(self) -> Any:
        if self._obs is None:
            self._obs = self.factory("observabilityadmin", self.region)
        return self._obs


# --------------------------------------------------------------- dataset integration


def dataset_integration_arn(region: str, account_id: str) -> str:
    return f"arn:aws:observabilityadmin:{region}:{account_id}:dataset-integration/default"


def create_dataset_integration(clients: Clients, role_arn: str, tags: dict[str, str] | None = None) -> dict[str, Any]:
    """CreateDatasetIntegration wrapper (one per account and Region)."""
    params: dict[str, Any] = {"RoleArn": role_arn}
    if tags:
        params["Tags"] = tags
    return clients.observabilityadmin.create_dataset_integration(**params)


def get_dataset_integration(clients: Clients, arn: str) -> dict[str, Any] | None:
    """GetDatasetIntegration wrapper; None when absent."""
    try:
        return clients.observabilityadmin.get_dataset_integration(Arn=arn)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("ResourceNotFoundException", "NotFoundException"):
            return None
        raise


def delete_dataset_integration(clients: Clients, arn: str) -> None:
    clients.observabilityadmin.delete_dataset_integration(Arn=arn)


def verify_dataset_integration(clients: Clients, arn: str, expected_role_arn: str) -> dict[str, Any]:
    """Fail closed unless the integration exists AND uses the expected execution role."""
    found = get_dataset_integration(clients, arn)
    if found is None:
        return {"ok": False, "reason": "dataset integration absent", "arn": arn}
    if found.get("RoleArn") != expected_role_arn:
        return {"ok": False, "reason": "dataset integration uses an unexpected role", "arn": arn, "roleArn": found.get("RoleArn")}
    return {"ok": True, "arn": found.get("Arn"), "roleArn": found.get("RoleArn")}


# ---------------------------------------------------------------------- access grants


def load_grants_file(path: str) -> list[dict[str, Any]]:
    """Read and validate the committed grants file.

    Shape (one entry per grant):
      {
        "name": "platform-sre-editor",
        "permission": "READ_WRITE_DELETE",
        "principal": {"principalType": "IDC_GROUP", "displayName": "agenticai-platform-sre"},
        "scopedActions": [...]           # optional: data scope / custom actions, as the API expects
      }
    A principal may give "principalId" directly instead of "displayName".
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    grants = data.get("grants") if isinstance(data, dict) else data
    if not isinstance(grants, list) or not grants:
        raise ValueError("grants file must contain a non-empty 'grants' list")
    names: set[str] = set()
    for grant in grants:
        validate_grant(grant)
        if grant["name"] in names:
            raise ValueError(f"duplicate grant name {grant['name']!r}")
        names.add(grant["name"])
    return grants


def validate_grant(grant: dict[str, Any]) -> None:
    name = grant.get("name")
    if not isinstance(name, str) or not name or len(name) > 64:
        raise ValueError("grant.name must be a 1-64 character string")
    if grant.get("permission") not in PERMISSIONS:
        raise ValueError(f"grant {name!r}: permission must be one of {PERMISSIONS}")
    principal = grant.get("principal") or {}
    if principal.get("principalType") not in PRINCIPAL_TYPES:
        raise ValueError(f"grant {name!r}: principal.principalType must be one of {PRINCIPAL_TYPES}")
    if not (principal.get("principalId") or principal.get("displayName")):
        raise ValueError(f"grant {name!r}: principal needs principalId or displayName")
    if grant["permission"] == "CUSTOM" and not grant.get("scopedActions"):
        raise ValueError(f"grant {name!r}: CUSTOM permission requires scopedActions")
    if grant["permission"] == "SPACE_ADMIN" and grant.get("scopedActions"):
        # A data scope on an admin limits reads but never the admin actions; refuse the ambiguity.
        raise ValueError(f"grant {name!r}: SPACE_ADMIN grants must not carry scopedActions in this blueprint")


def search_principal(clients: Clients, domain_id: str, principal_type: str, display_name: str) -> str:
    """Resolve exactly one principal id by display name through SearchPrincipals; fail closed otherwise."""
    matches: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {"domainId": domain_id, "searchQuery": display_name, "maxResults": 50}
        if token:
            params["nextToken"] = token
        page = clients.omni.search_principals(**params)
        for item in page.get("results", []):
            if item.get("principalType") == principal_type and item.get("displayName", "").lower() == display_name.lower():
                matches.append(item)
        token = page.get("nextToken")
        if not token:
            break
    if len(matches) != 1:
        raise LookupError(
            f"expected exactly one {principal_type} named {display_name!r} in domain {domain_id}, found {len(matches)}"
        )
    return matches[0]["principalId"]


def list_access_grants(clients: Clients, domain_id: str, space_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {"domainId": domain_id, "spaceId": space_id, "maxResults": 100}
        if token:
            params["nextToken"] = token
        page = clients.omni.list_access_grants(**params)
        items.extend(page.get("items", []))
        token = page.get("nextToken")
        if not token:
            break
    return items


def create_access_grant(clients: Clients, domain_id: str, space_id: str, grant: dict[str, Any], principal_id: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "domainId": domain_id,
        "spaceId": space_id,
        "name": grant["name"],
        "permission": grant["permission"],
        "principal": {"principalType": grant["principal"]["principalType"], "principalId": principal_id},
        "clientToken": str(uuid.uuid4()),
    }
    if grant.get("scopedActions"):
        params["scopedActions"] = grant["scopedActions"]
    if grant.get("tags"):
        params["tags"] = grant["tags"]
    return clients.omni.create_access_grant(**params)["accessGrant"]


def delete_access_grant(clients: Clients, grant_id: str) -> None:
    clients.omni.delete_access_grant(grantId=grant_id)


def resolve_principal_id(clients: Clients, domain_id: str, grant: dict[str, Any]) -> str:
    principal = grant["principal"]
    if principal.get("principalId"):
        return principal["principalId"]
    return search_principal(clients, domain_id, principal["principalType"], principal["displayName"])


def apply_access_grants(clients: Clients, domain_id: str, space_id: str, grants: list[dict[str, Any]]) -> dict[str, Any]:
    """Idempotent by grant name: existing names are left untouched, missing ones are created."""
    existing = {g.get("name"): g for g in list_access_grants(clients, domain_id, space_id)}
    created, skipped = [], []
    for grant in grants:
        if grant["name"] in existing:
            skipped.append(grant["name"])
            continue
        principal_id = resolve_principal_id(clients, domain_id, grant)
        result = create_access_grant(clients, domain_id, space_id, grant, principal_id)
        created.append({"name": grant["name"], "grantId": result.get("grantId"), "permission": grant["permission"]})
    return {"created": created, "skipped": skipped}


def verify_access_grants(clients: Clients, domain_id: str, space_id: str, grants: list[dict[str, Any]]) -> dict[str, Any]:
    """Every grant in the file exists with the expected permission; report unexpected customer-managed grants too."""
    existing = {g.get("name"): g for g in list_access_grants(clients, domain_id, space_id)}
    missing, mismatched = [], []
    for grant in grants:
        found = existing.get(grant["name"])
        if not found:
            missing.append(grant["name"])
        elif found.get("permission") != grant["permission"]:
            mismatched.append({"name": grant["name"], "expected": grant["permission"], "actual": found.get("permission")})
    expected_names = {g["name"] for g in grants}
    unexpected = [
        g.get("name")
        for g in existing.values()
        if g.get("grantType") == "CUSTOMER_MANAGED" and g.get("name") not in expected_names
    ]
    return {"ok": not missing and not mismatched, "missing": missing, "mismatched": mismatched, "unexpectedCustomerManaged": unexpected}


def delete_access_grants(clients: Clients, domain_id: str, space_id: str, grants: list[dict[str, Any]]) -> dict[str, Any]:
    """Delete only the customer-managed grants the file names; system-managed grants are never touched."""
    existing = {g.get("name"): g for g in list_access_grants(clients, domain_id, space_id)}
    deleted, absent, refused = [], [], []
    for grant in grants:
        found = existing.get(grant["name"])
        if not found:
            absent.append(grant["name"])
            continue
        if found.get("grantType") and found.get("grantType") != "CUSTOMER_MANAGED":
            refused.append(grant["name"])
            continue
        delete_access_grant(clients, found["grantId"])
        deleted.append(grant["name"])
    return {"deleted": deleted, "absent": absent, "refusedSystemManaged": refused}


# ------------------------------------------------------------- centralization rule


def ensure_service_linked_role(clients: Clients, service_name: str, role_name: str) -> dict[str, Any]:
    """CreateServiceLinkedRole wrapper, idempotent: IAM answers InvalidInput when the name is taken."""
    try:
        created = clients.iam.create_service_linked_role(AWSServiceName=service_name)
        return {"service": service_name, "roleName": role_name, "result": "created", "roleArn": created["Role"]["Arn"]}
    except ClientError as exc:
        error = exc.response.get("Error", {})
        if error.get("Code") == "InvalidInput" and "has been taken" in error.get("Message", ""):
            return {"service": service_name, "roleName": role_name, "result": "exists"}
        raise


def ensure_service_linked_roles(clients: Clients) -> dict[str, Any]:
    """Create, in the destination account, the roles console-enabled trusted access would have created."""
    roles = [ensure_service_linked_role(clients, service, role) for service, role in CENTRALIZATION_SERVICE_LINKED_ROLES]
    return {"ok": True, "serviceLinkedRoles": roles}


def get_centralization_rule(clients: Clients, rule_identifier: str) -> dict[str, Any]:
    return clients.observabilityadmin.get_centralization_rule_for_organization(RuleIdentifier=rule_identifier)


def context_graph_enabled(rule: dict[str, Any]) -> bool:
    return "SourceContextGraphConfiguration" in (rule.get("CentralizationRule") or {}).get("Source", {})


RULE_SHAPE_MEMBERS = {
    "CentralizationRuleSource": ("SourceMetricsConfiguration", "SourceContextGraphConfiguration"),
    "DestinationLogsConfiguration": ("TagPropagationConfiguration",),
    "LogsEncryptionConfiguration": ("EncryptionScope",),
}


def assert_sdk_knows_rule_shape(clients: Clients) -> None:
    """Fail closed before an update that re-submits a parsed rule.

    botocore drops response members its service model does not know. A client
    built from an older model would read the rule WITHOUT its metrics, tag
    propagation or encryption-scope settings and the re-submit would silently
    remove them (observed live: AWS CLI 2.34 hid all three while the raw
    response carried them).
    """
    model = clients.observabilityadmin.meta.service_model
    missing = [
        f"{shape}.{member}"
        for shape, members in RULE_SHAPE_MEMBERS.items()
        for member in members
        if member not in model.shape_for(shape).members
    ]
    if missing:
        raise SystemExit(
            "The installed botocore does not model " + ", ".join(missing) + "; upgrade boto3 before updating a centralization rule."
        )


def enable_context_graph(clients: Clients, rule_identifier: str) -> dict[str, Any]:
    """Idempotent: re-submit the rule with SourceContextGraphConfiguration present, nothing else changed."""
    assert_sdk_knows_rule_shape(clients)
    current = get_centralization_rule(clients, rule_identifier)
    rule = current["CentralizationRule"]
    if context_graph_enabled(current):
        return {"result": "already-enabled", "ruleArn": current.get("RuleArn")}
    updated = {**rule, "Source": {**rule["Source"], "SourceContextGraphConfiguration": {}}}
    response = clients.observabilityadmin.update_centralization_rule_for_organization(
        RuleIdentifier=rule_identifier, Rule=updated
    )
    return {"result": "enabled", "ruleArn": response.get("RuleArn")}


def verify_context_graph(clients: Clients, rule_identifier: str) -> dict[str, Any]:
    current = get_centralization_rule(clients, rule_identifier)
    return {
        "ok": context_graph_enabled(current),
        "ruleArn": current.get("RuleArn"),
        "ruleHealth": current.get("RuleHealth"),
        "failureReason": current.get("FailureReason"),
    }


# ------------------------------------------------------------------------ inventory


def get_space(clients: Clients, space_id: str) -> dict[str, Any] | None:
    try:
        return clients.omni.get_space(spaceId=space_id).get("space")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise


def inventory(clients: Clients, domain_id: str, space_id: str, account_id: str) -> dict[str, Any]:
    space = get_space(clients, space_id)
    grants = list_access_grants(clients, domain_id, space_id) if space else []
    integration = get_dataset_integration(clients, dataset_integration_arn(clients.region, account_id))
    return {
        "space": {k: space.get(k) for k in ("spaceId", "name", "status", "encryptionConfiguration")} if space else None,
        "accessGrants": [
            {k: g.get(k) for k in ("name", "permission", "grantType", "grantId")} for g in grants
        ],
        "datasetIntegration": {k: integration.get(k) for k in ("Arn", "RoleArn")} if integration else None,
    }


# ------------------------------------------------------------------------------ cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", required=True, help="Space Region (hub account credentials expected).")
    parser.add_argument("--profile", help="Named AWS profile for the hub account (default: the ambient credentials).")
    sub = parser.add_subparsers(dest="command", required=True)

    di = sub.add_parser("dataset-integration", help="Create, verify or delete the Dataset integration.")
    di.add_argument("action", choices=("create", "verify", "delete"))
    di.add_argument("--role-arn", help="Execution role ARN (OmniHubStack output OmniDatasetIntegrationRoleArn).")
    di.add_argument("--account-id", help="Hub account id (defaults to the caller's account).")

    ag = sub.add_parser("access-grants", help="Apply, verify or delete the grants in a JSON file.")
    ag.add_argument("action", choices=("apply", "verify", "delete"))
    ag.add_argument("--domain-id", required=True)
    ag.add_argument("--space-id", required=True)
    ag.add_argument("--grants-file", required=True)

    cz = sub.add_parser(
        "centralization",
        help="Create the service-linked roles (pre-deploy), or enable/verify Context Graph centralization on the rule.",
    )
    cz.add_argument("action", choices=("ensure-service-linked-roles", "enable-context-graph", "verify"))
    cz.add_argument("--rule", help="Centralization rule name or ARN (enable-context-graph and verify).")

    inv = sub.add_parser("inventory", help="Read-only inventory for evidence and residue checks.")
    inv.add_argument("--domain-id", required=True)
    inv.add_argument("--space-id", required=True)
    inv.add_argument("--account-id")
    return parser


def caller_account_id(clients: Clients) -> str:
    return clients.factory("sts", clients.region).get_caller_identity()["Account"]


def run(args: argparse.Namespace, clients: Clients | None = None) -> dict[str, Any]:
    clients = clients or Clients(region=args.region, profile=getattr(args, "profile", None))
    if args.command == "dataset-integration":
        account_id = args.account_id or caller_account_id(clients)
        arn = dataset_integration_arn(args.region, account_id)
        if args.action == "create":
            if not args.role_arn:
                raise SystemExit("--role-arn is required for create")
            existing = get_dataset_integration(clients, arn)
            if existing:
                return {"action": "create", "result": "already-exists", **verify_dataset_integration(clients, arn, args.role_arn)}
            created = create_dataset_integration(clients, args.role_arn, {"agenticai:managed-by": "omni_space_bootstrap"})
            return {"action": "create", "result": "created", "arn": created.get("Arn"), "roleArn": created.get("RoleArn")}
        if args.action == "verify":
            if not args.role_arn:
                raise SystemExit("--role-arn is required for verify")
            return {"action": "verify", **verify_dataset_integration(clients, arn, args.role_arn)}
        if get_dataset_integration(clients, arn) is None:
            return {"action": "delete", "result": "absent", "arn": arn}
        delete_dataset_integration(clients, arn)
        return {"action": "delete", "result": "deleted", "arn": arn}

    if args.command == "access-grants":
        grants = load_grants_file(args.grants_file)
        if args.action == "apply":
            return {"action": "apply", **apply_access_grants(clients, args.domain_id, args.space_id, grants)}
        if args.action == "verify":
            return {"action": "verify", **verify_access_grants(clients, args.domain_id, args.space_id, grants)}
        return {"action": "delete", **delete_access_grants(clients, args.domain_id, args.space_id, grants)}

    if args.command == "centralization":
        if args.action == "ensure-service-linked-roles":
            return {"action": "ensure-service-linked-roles", **ensure_service_linked_roles(clients)}
        if not args.rule:
            raise SystemExit(f"--rule is required for {args.action}")
        if args.action == "enable-context-graph":
            return {"action": "enable-context-graph", **enable_context_graph(clients, args.rule)}
        return {"action": "verify-context-graph", **verify_context_graph(clients, args.rule)}

    account_id = args.account_id or caller_account_id(clients)
    return {"action": "inventory", **inventory(clients, args.domain_id, args.space_id, account_id)}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run(args)
    print(json.dumps(result, indent=2, default=str))
    ok = result.get("ok", True)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
