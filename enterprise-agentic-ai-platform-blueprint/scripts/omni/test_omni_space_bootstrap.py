"""Unit tests for scripts/omni/omni_space_bootstrap.py using botocore stubs (no network)."""
from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import sys

import boto3
import pytest
from botocore.stub import ANY, Stubber

HERE = pathlib.Path(__file__).parent
spec = importlib.util.spec_from_file_location("omni_space_bootstrap", HERE / "omni_space_bootstrap.py")
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
# dataclasses resolve postponed annotations through sys.modules, so register before executing.
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

REGION = "eu-west-1"
ACCOUNT = "222222222222"
DOMAIN = "d-1a2b3c4d5e"
SPACE = "a1b2c3d4-5e6f-4a3b-8c9d-0e1f2a3b4c5d"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/CloudWatchOmniDatasetIntegrationRole-{REGION}"
INTEGRATION_ARN = f"arn:aws:observabilityadmin:{REGION}:{ACCOUNT}:dataset-integration/default"
NOW = datetime.datetime(2026, 11, 6, tzinfo=datetime.timezone.utc)


def _clients():
    session = boto3.session.Session(region_name=REGION, aws_access_key_id="x", aws_secret_access_key="y")
    omni = session.client("cloudwatchomni")
    obs = session.client("observabilityadmin")
    clients = mod.Clients(region=REGION, _omni=omni, _obs=obs)
    return clients, Stubber(omni), Stubber(obs)


def _item(name, permission, grant_type, grant_id, principal_type="IDC_GROUP", principal_id="p-1"):
    return {
        "grantId": grant_id,
        "grantArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:access-grant/{grant_id}",
        "name": name,
        "domainId": DOMAIN,
        "spaceId": SPACE,
        "permission": permission,
        "grantType": grant_type,
        "principal": {"principalType": principal_type, "principalId": principal_id},
    }


def _grants_file(tmp_path, grants):
    path = tmp_path / "grants.json"
    path.write_text(json.dumps({"grants": grants}))
    return str(path)


SRE_GRANT = {
    "name": "platform-sre-editor",
    "permission": "READ_WRITE_DELETE",
    "principal": {"principalType": "IDC_GROUP", "displayName": "agenticai-platform-sre"},
}
TEAM_GRANT = {
    "name": "team-retail-viewer",
    "permission": "READ",
    "principal": {"principalType": "IDC_GROUP", "principalId": "94b6c7d8-0000-4c3d-9e4f-5a6b7c8d9e0f"},
    "scopedActions": [
        {
            "actions": ["cloudwatch:GetRecords", "cloudwatch:ListMetrics", "cloudwatch:GetMetricData"],
            "resources": [
                {
                    "resourceType": "DataSet",
                    "signalTypes": ["LOGS", "TRACES"],
                    "rowScopeGroups": [[{"operator": "IN", "field": "resource['attributes']['cloud.account.id']", "values": ["444444444444"]}]],
                }
            ],
        }
    ],
}


def test_dataset_integration_create_then_verify():
    clients, _, obs = _clients()
    obs.add_client_error("get_dataset_integration", "ResourceNotFoundException")
    obs.add_response(
        "create_dataset_integration",
        {"Arn": INTEGRATION_ARN, "RoleArn": ROLE, "CreatedAt": NOW, "UpdatedAt": NOW},
        {"RoleArn": ROLE, "Tags": {"agenticai:managed-by": "omni_space_bootstrap"}},
    )
    obs.add_response("get_dataset_integration", {"Arn": INTEGRATION_ARN, "RoleArn": ROLE, "CreatedAt": NOW, "UpdatedAt": NOW}, {"Arn": INTEGRATION_ARN})
    with obs:
        args = mod.build_parser().parse_args(
            ["--region", REGION, "dataset-integration", "create", "--role-arn", ROLE, "--account-id", ACCOUNT]
        )
        created = mod.run(args, clients)
        assert created["result"] == "created"
        verify = mod.verify_dataset_integration(clients, INTEGRATION_ARN, ROLE)
        assert verify["ok"] is True


def test_dataset_integration_verify_fails_closed_on_wrong_role():
    clients, _, obs = _clients()
    obs.add_response("get_dataset_integration", {"Arn": INTEGRATION_ARN, "RoleArn": "arn:aws:iam::222222222222:role/Other", "CreatedAt": NOW, "UpdatedAt": NOW}, {"Arn": INTEGRATION_ARN})
    with obs:
        result = mod.verify_dataset_integration(clients, INTEGRATION_ARN, ROLE)
    assert result["ok"] is False
    assert "unexpected role" in result["reason"]


def test_access_grants_apply_resolves_names_and_skips_existing(tmp_path):
    clients, omni, _ = _clients()
    omni.add_response("list_access_grants", {"items": [_item("team-retail-viewer", "READ", "CUSTOMER_MANAGED", "g-existing")]}, {"domainId": DOMAIN, "spaceId": SPACE, "maxResults": 100})
    omni.add_response(
        "search_principals",
        {"results": [
            {"principalType": "IDC_GROUP", "principalId": "grp-1", "displayName": "agenticai-platform-sre"},
            {"principalType": "IDC_USER", "principalId": "usr-1", "displayName": "agenticai-platform-sre"},
        ]},
        {"domainId": DOMAIN, "searchQuery": "agenticai-platform-sre", "maxResults": 50},
    )
    omni.add_response(
        "create_access_grant",
        {"accessGrant": {**_item("platform-sre-editor", "READ_WRITE_DELETE", "CUSTOMER_MANAGED", "g-new", principal_id="grp-1"),
                         "accountId": ACCOUNT, "createdBy": f"arn:aws:iam::{ACCOUNT}:role/Deploy", "createdAt": NOW, "updatedAt": NOW}},
        {"domainId": DOMAIN, "spaceId": SPACE, "name": "platform-sre-editor", "permission": "READ_WRITE_DELETE",
         "principal": {"principalType": "IDC_GROUP", "principalId": "grp-1"}, "clientToken": ANY},
    )
    with omni:
        result = mod.apply_access_grants(clients, DOMAIN, SPACE, mod.load_grants_file(_grants_file(tmp_path, [SRE_GRANT, TEAM_GRANT])))
    assert result["skipped"] == ["team-retail-viewer"]
    assert [c["grantId"] for c in result["created"]] == ["g-new"]


def test_access_grants_apply_fails_closed_on_ambiguous_principal(tmp_path):
    clients, omni, _ = _clients()
    omni.add_response("list_access_grants", {"items": []}, {"domainId": DOMAIN, "spaceId": SPACE, "maxResults": 100})
    omni.add_response(
        "search_principals",
        {"results": [
            {"principalType": "IDC_GROUP", "principalId": "grp-1", "displayName": "agenticai-platform-sre"},
            {"principalType": "IDC_GROUP", "principalId": "grp-2", "displayName": "AGENTICAI-PLATFORM-SRE"},
        ]},
        {"domainId": DOMAIN, "searchQuery": "agenticai-platform-sre", "maxResults": 50},
    )
    with omni, pytest.raises(LookupError, match="found 2"):
        mod.apply_access_grants(clients, DOMAIN, SPACE, mod.load_grants_file(_grants_file(tmp_path, [SRE_GRANT])))


def test_access_grants_verify_reports_missing_and_unexpected(tmp_path):
    clients, omni, _ = _clients()
    omni.add_response("list_access_grants", {"items": [
        _item("team-retail-viewer", "READ_WRITE_DELETE", "CUSTOMER_MANAGED", "g1"),
        _item("rogue", "SPACE_ADMIN", "CUSTOMER_MANAGED", "g2"),
        _item("creator", "SPACE_ADMIN", "SYSTEM_MANAGED", "g3", principal_type="IAM_ROLE"),
    ]}, {"domainId": DOMAIN, "spaceId": SPACE, "maxResults": 100})
    with omni:
        result = mod.verify_access_grants(clients, DOMAIN, SPACE, mod.load_grants_file(_grants_file(tmp_path, [SRE_GRANT, TEAM_GRANT])))
    assert result["ok"] is False
    assert result["missing"] == ["platform-sre-editor"]
    assert result["mismatched"] == [{"name": "team-retail-viewer", "expected": "READ", "actual": "READ_WRITE_DELETE"}]
    assert result["unexpectedCustomerManaged"] == ["rogue"]


def test_access_grants_delete_never_touches_system_managed(tmp_path):
    clients, omni, _ = _clients()
    omni.add_response("list_access_grants", {"items": [
        _item("platform-sre-editor", "READ_WRITE_DELETE", "SYSTEM_MANAGED", "g-sys", principal_type="IAM_ROLE"),
        _item("team-retail-viewer", "READ", "CUSTOMER_MANAGED", "g-team"),
    ]}, {"domainId": DOMAIN, "spaceId": SPACE, "maxResults": 100})
    omni.add_response("delete_access_grant", {}, {"grantId": "g-team"})
    with omni:
        result = mod.delete_access_grants(clients, DOMAIN, SPACE, mod.load_grants_file(_grants_file(tmp_path, [SRE_GRANT, TEAM_GRANT])))
    assert result == {"deleted": ["team-retail-viewer"], "absent": [], "refusedSystemManaged": ["platform-sre-editor"]}


@pytest.mark.parametrize(
    "grant, message",
    [
        ({**SRE_GRANT, "permission": "OWNER"}, "permission must be one of"),
        ({**SRE_GRANT, "principal": {"principalType": "IDC_GROUP"}}, "needs principalId or displayName"),
        ({**SRE_GRANT, "permission": "CUSTOM"}, "CUSTOM permission requires scopedActions"),
        ({**SRE_GRANT, "permission": "SPACE_ADMIN", "scopedActions": [{"actions": ["x"]}]}, "SPACE_ADMIN grants must not carry"),
    ],
)
def test_grant_validation(grant, message):
    with pytest.raises(ValueError, match=message):
        mod.validate_grant(grant)


def test_inventory_handles_missing_space():
    clients, omni, obs = _clients()
    omni.add_client_error("get_space", "ResourceNotFoundException")
    obs.add_client_error("get_dataset_integration", "ResourceNotFoundException")
    with omni, obs:
        result = mod.inventory(clients, DOMAIN, SPACE, ACCOUNT)
    assert result == {"space": None, "accessGrants": [], "datasetIntegration": None}


RULE_ARN = f"arn:aws:observabilityadmin:{REGION}:{ACCOUNT}:organization-centralization-rule/agenticai-fleet-eu-west-1"
RULE_BODY = {
    "Source": {"Regions": [REGION], "Scope": "AccountId IN ('444444444444')",
               "SourceLogsConfiguration": {"LogGroupSelectionCriteria": "LogGroupName LIKE '/agenticai/%'", "EncryptedLogGroupStrategy": "ALLOW"}},
    "Destination": {"Account": ACCOUNT, "Region": REGION},
}


def test_enable_context_graph_updates_rule_once():
    clients, _, obs = _clients()
    obs.add_response("get_centralization_rule_for_organization",
                     {"RuleArn": RULE_ARN, "RuleName": "agenticai-fleet-eu-west-1", "CentralizationRule": RULE_BODY, "RuleHealth": "HEALTHY"},
                     {"RuleIdentifier": "agenticai-fleet-eu-west-1"})
    expected = {**RULE_BODY, "Source": {**RULE_BODY["Source"], "SourceContextGraphConfiguration": {}}}
    obs.add_response("update_centralization_rule_for_organization", {"RuleArn": RULE_ARN},
                     {"RuleIdentifier": "agenticai-fleet-eu-west-1", "Rule": expected})
    obs.add_response("get_centralization_rule_for_organization",
                     {"RuleArn": RULE_ARN, "RuleName": "agenticai-fleet-eu-west-1", "CentralizationRule": expected, "RuleHealth": "HEALTHY"},
                     {"RuleIdentifier": "agenticai-fleet-eu-west-1"})
    with obs:
        assert mod.enable_context_graph(clients, "agenticai-fleet-eu-west-1")["result"] == "enabled"
        assert mod.enable_context_graph(clients, "agenticai-fleet-eu-west-1")["result"] == "already-enabled"


def test_ensure_service_linked_roles_is_idempotent_and_reraises_other_errors():
    session = boto3.session.Session(region_name=REGION, aws_access_key_id="x", aws_secret_access_key="y")
    iam = session.client("iam")
    clients = mod.Clients(region=REGION, _iam=iam)
    stub = Stubber(iam)
    slr_arn = f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/observabilityadmin.amazonaws.com/AWSServiceRoleForObservabilityAdmin"
    stub.add_response(
        "create_service_linked_role",
        {"Role": {"Path": "/aws-service-role/observabilityadmin.amazonaws.com/", "RoleName": "AWSServiceRoleForObservabilityAdmin",
                  "RoleId": "AROAEXAMPLEEXAMPLE12", "Arn": slr_arn, "CreateDate": NOW}},
        {"AWSServiceName": "observabilityadmin.amazonaws.com"},
    )
    stub.add_client_error(
        "create_service_linked_role",
        service_error_code="InvalidInput",
        service_message="Service role name AWSServiceRoleForObservabilityAdmin_LogsCentralization has been taken in this account, please try a different suffix.",
        expected_params={"AWSServiceName": "logs-centralization.observabilityadmin.amazonaws.com"},
    )
    with stub:
        result = mod.ensure_service_linked_roles(clients)
    assert result["ok"] is True
    assert [r["result"] for r in result["serviceLinkedRoles"]] == ["created", "exists"]
    assert result["serviceLinkedRoles"][0]["roleArn"] == slr_arn

    # Any other IAM error is not swallowed.
    stub.add_client_error("create_service_linked_role", service_error_code="AccessDenied", service_message="denied",
                          expected_params={"AWSServiceName": "observabilityadmin.amazonaws.com"})
    with stub, pytest.raises(Exception, match="AccessDenied"):
        mod.ensure_service_linked_role(clients, *mod.CENTRALIZATION_SERVICE_LINKED_ROLES[0])


def test_centralization_rule_actions_require_rule_argument():
    args = mod.build_parser().parse_args(["--region", REGION, "centralization", "verify"])
    with pytest.raises(SystemExit, match="--rule is required"):
        mod.run(args, clients=mod.Clients(region=REGION))
