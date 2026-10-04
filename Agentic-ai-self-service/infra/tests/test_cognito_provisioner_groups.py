"""A provisioned user joins the standard-user groups, and an upgrade never resets a password.

The API Lambdas enforce scopes by default (RBAC_ENFORCE=true), so a user in no g-*
group holds no scopes and every call 403s. The provisioner created users and never
put them in a group, which made enforcing-by-default lock out every provisioned user.

Adding the ``Groups`` property changes every existing user custom resource, so the
upgrade deploy sends each one an Update. The Update path used to re-run the create,
whose UsernameExists branch resets the password to a new temporary one: the upgrade
itself would have forced every live user back to FORCE_CHANGE_PASSWORD. An Update
that keeps the same pool and email now only syncs groups.

The handler is a CDK Provider's onEvent function, and the framework sends the one
reply. The handler used to PUT its own reply to the ResponseURL too, with a
different physical id (``pool:email``, against the framework's RequestId: the
deployed id is a UUID). Had CloudFormation taken the handler's reply on the
upgrade Update, the id change would have made it a replacement, and the cleanup
Delete would have removed the very user being updated.
"""

from __future__ import annotations

import importlib
import pathlib
import re
import sys
import urllib.request

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from botocore.exceptions import ClientError
from stacks.platform.cognito_auth import PROVISIONED_USER_GROUPS
from stacks.platform_stack import PlatformStack

_HANDLER_DIR = pathlib.Path(__file__).resolve().parents[1] / "stacks" / "cognito_user_provisioner"


class _Cognito:
    def __init__(self, existing=()):
        self.users = set(existing)
        self.calls = []

    def admin_create_user(self, UserPoolId, Username, **_kw):  # noqa: N803
        self.calls.append(("create", Username))
        if Username in self.users:
            raise ClientError({"Error": {"Code": "UsernameExistsException"}}, "AdminCreateUser")
        self.users.add(Username)

    def admin_set_user_password(self, UserPoolId, Username, **_kw):  # noqa: N803
        self.calls.append(("set_password", Username))

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):  # noqa: N803
        self.calls.append(("add_to_group", Username, GroupName))

    def admin_delete_user(self, UserPoolId, Username):  # noqa: N803
        self.calls.append(("delete", Username))


@pytest.fixture
def provisioner(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.syspath_prepend(str(_HANDLER_DIR))
    sys.modules.pop("handler", None)
    module = importlib.import_module("handler")
    opened = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: opened.append(a))
    yield module, opened
    sys.modules.pop("handler", None)


DEPLOYED_ID = "2285ba32-2a50-4f8e-aaab-8d65af30ece4"  # the framework's id on a live resource


def _event(
    request_type, *, email="u@example.com", pool="us-east-1_POOL", groups=("g-users-default", "t-user"), old=None
):
    event = {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation-custom-resource-response.example/presigned",
        "ResourceProperties": {"UserPoolId": pool, "Email": email, "Groups": list(groups)},
    }
    if request_type != "Create":
        event["PhysicalResourceId"] = DEPLOYED_ID
    if old is not None:
        event["OldResourceProperties"] = old
    return event


def test_a_created_user_joins_its_groups(provisioner, monkeypatch):
    module, opened = provisioner
    fake = _Cognito()
    monkeypatch.setattr(module, "cognito", fake)
    assert module.handler(_event("Create"), None) == {"PhysicalResourceId": "us-east-1_POOL:u@example.com"}
    assert fake.calls == [
        ("create", "u@example.com"),
        ("add_to_group", "u@example.com", "g-users-default"),
        ("add_to_group", "u@example.com", "t-user"),
    ]
    assert opened == []


def test_the_upgrade_update_adds_groups_and_never_resets_the_password(provisioner, monkeypatch):
    """The Update every existing user receives when ``Groups`` first appears."""
    module, opened = provisioner
    fake = _Cognito(existing={"u@example.com"})
    monkeypatch.setattr(module, "cognito", fake)
    out = module.handler(_event("Update", old={"UserPoolId": "us-east-1_POOL", "Email": "u@example.com"}), None)
    assert not [c for c in fake.calls if c[0] in ("create", "set_password", "delete")], fake.calls
    assert ("add_to_group", "u@example.com", "g-users-default") in fake.calls
    # A changed id is a replacement, and the replacement's cleanup Delete removes the user.
    assert out == {"PhysicalResourceId": DEPLOYED_ID}
    assert opened == []


def test_an_update_to_a_new_email_creates_that_user(provisioner, monkeypatch):
    """A new id, so CloudFormation deletes the old user rather than leaking it."""
    module, _opened = provisioner
    fake = _Cognito()
    monkeypatch.setattr(module, "cognito", fake)
    out = module.handler(
        _event("Update", email="new@example.com", old={"UserPoolId": "us-east-1_POOL", "Email": "u@example.com"}), None
    )
    assert fake.calls[0] == ("create", "new@example.com")
    assert ("add_to_group", "new@example.com", "t-user") in fake.calls
    assert out == {"PhysicalResourceId": "us-east-1_POOL:new@example.com"}


def test_a_delete_keeps_the_id_and_removes_the_user(provisioner, monkeypatch):
    """The framework refuses a Delete whose id changes."""
    module, opened = provisioner
    fake = _Cognito(existing={"u@example.com"})
    monkeypatch.setattr(module, "cognito", fake)
    assert module.handler(_event("Delete"), None) == {"PhysicalResourceId": DEPLOYED_ID}
    assert fake.calls == [("delete", "u@example.com")]
    assert opened == []


def test_a_failed_group_grant_fails_the_resource(provisioner, monkeypatch):
    """A user created but left in no group is locked out under enforcement; say so.

    Raising is how the handler says so: the framework turns it into the FAILED reply."""
    module, opened = provisioner
    fake = _Cognito()

    def refuse(**_kw):
        raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "AdminAddUserToGroup")

    fake.admin_add_user_to_group = refuse
    monkeypatch.setattr(module, "cognito", fake)
    with pytest.raises(ClientError):
        module.handler(_event("Create"), None)
    assert opened == []


def test_the_handler_has_no_way_to_reply_itself(provisioner):
    """The framework owns the reply; a second one races it with a different id."""
    module, _opened = provisioner
    source = (_HANDLER_DIR / "handler.py").read_text()
    assert "urllib" not in source and "urlopen" not in source
    assert '["ResponseURL"]' not in source and 'get("ResponseURL")' not in source
    assert not hasattr(module, "_send_cfn_response")


@pytest.fixture(scope="module")
def template() -> dict:
    app = cdk.App(context={"cognito_users": "a@example.com"})
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="agentcore-workflow",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    return Template.from_stack(stack).to_json()


def _user_resources(tpl):
    return {
        lid: r
        for lid, r in tpl["Resources"].items()
        if r["Type"] == "AWS::CloudFormation::CustomResource" and "Email" in r.get("Properties", {})
    }


def test_every_provisioned_user_is_granted_the_standard_groups(template):
    users = _user_resources(template)
    assert users
    for lid, resource in users.items():
        assert resource["Properties"]["Groups"] == list(PROVISIONED_USER_GROUPS), lid


def test_the_groups_exist_before_the_user_joins_them(template):
    """AdminAddUserToGroup on a group CloudFormation has not created yet fails."""
    groups = {
        r["Properties"]["GroupName"]: lid
        for lid, r in template["Resources"].items()
        if r["Type"] == "AWS::Cognito::UserPoolGroup"
    }
    for lid, resource in _user_resources(template).items():
        for group in PROVISIONED_USER_GROUPS:
            assert groups[group] in resource.get("DependsOn", []), (lid, group)


def test_the_provisioner_may_add_to_groups_only_in_its_own_pool(template):
    statements = [
        s
        for r in template["Resources"].values()
        if r["Type"] == "AWS::IAM::Policy"
        for s in r["Properties"]["PolicyDocument"]["Statement"]
        if "cognito-idp:AdminAddUserToGroup" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    ]
    assert len(statements) == 1, statements
    resource = statements[0]["Resource"]
    assert isinstance(resource, dict) and "Fn::GetAtt" in resource, resource
    assert resource["Fn::GetAtt"][1] == "Arn"


def test_the_standard_groups_are_groups_the_backend_grants_scopes_to():
    rbac_src = (pathlib.Path(__file__).resolve().parents[2] / "backend/src/app/services/rbac.py").read_text()
    default_group = re.search(r'"g-users-default"\s*:\s*\{(?P<body>[^}]*)\}', rbac_src, re.DOTALL)
    assert default_group, "g-users-default is missing from the backend scope map"
    body = default_group.group("body")
    assert "SCOPE_INVOKE" in body
    assert '"agent:read"' in body
    assert '"agent:write"' in body
    assert '"tag:read"' in body
    assert set(PROVISIONED_USER_GROUPS) == {"g-users-default", "t-user"}
