"""Governance tags must cross the Custom Resource boundary.

The CloudFormation template can look fully tagged while the resources created by its
provider Lambda are not. These tests cover both halves of that boundary: the generated
``ResourceTags`` properties and the exact AWS calls/IAM actions each handler needs.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl

import botocore.session
import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import (
    _CUSTOM_RESOURCE_TAG_COVERED_BY,
    _CUSTOM_RESOURCE_TAG_LIMITS,
    CfnExportUnsupportedError,
    CfnTemplateGenerator,
    _apply_resource_tags,
)
from app.services.deployment_payload_validation import _FORBIDDEN_SECRET_TOKENS
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters


def _import_provider():
    provider_dir = Path(__file__).resolve().parents[1] / "src" / "app" / "services" / "cfn_provider"
    if str(provider_dir) not in sys.path:
        sys.path.insert(0, str(provider_dir))
    import handler  # noqa: PLC0415

    return handler


provider = _import_provider()

ACCOUNT = "111122223333"
STACK_ID = f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/tag-test/abc"
TAGS = {"platform:owner": "ECB Team", "CostCentre": "ECB-42"}
VALID_CEDAR = (
    "permit(principal is AgentCore::OAuthUser, "
    'action in [AgentCore::Action::"MCPServerRuntime___get_order"], '
    "resource == AgentCore::Gateway::"
    '"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-abc");'
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(provider.time, "sleep", lambda _seconds: None)


def _request(resource_tags=None) -> DeployRequest:
    return DeployRequest(
        node_id="tag-node",
        template_id="mcp-server-gateway-target",
        config=RuntimeConfig(
            name="tag-agent",
            model={"model_id": "us.anthropic.claude-sonnet-5"},
            system_prompt="tag test",
            entrypoint="agent.py",
        ),
        gateway_config={"gateway_provider": "agentcore", "targetType": "lambda"},
        mcp_server_config={"tools": []},
        policy_config={"policies": [{"name": "p", "statement": VALID_CEDAR}]},
        resource_tags=resource_tags,
    )


def _template(resource_tags=None) -> dict:
    return yaml.safe_load(CfnTemplateGenerator().generate(_request(resource_tags)).template_yaml)


def _actions(template: dict, policy_name: str) -> set[str]:
    policies = template["Resources"]["CfnProviderRole"]["Properties"]["Policies"]
    policy = next(item for item in policies if item["PolicyName"] == policy_name)
    actions: set[str] = set()
    for statement in policy["PolicyDocument"]["Statement"]:
        value = statement.get("Action", [])
        actions.update([value] if isinstance(value, str) else value)
    return actions


class TestGeneratedContract:
    def test_every_supported_custom_resource_receives_the_effective_tags(self):
        template = _template(TAGS)
        custom = {
            logical_id: resource
            for logical_id, resource in template["Resources"].items()
            if resource["Type"].startswith("Custom::")
        }

        assert {resource["Type"] for resource in custom.values()} == (
            set(_CUSTOM_RESOURCE_TAG_LIMITS) | set(_CUSTOM_RESOURCE_TAG_COVERED_BY)
        )
        assert custom
        for logical_id, resource in custom.items():
            if resource["Type"] in _CUSTOM_RESOURCE_TAG_LIMITS:
                assert resource["Properties"]["ResourceTags"] == TAGS, logical_id
            else:
                assert "ResourceTags" not in resource["Properties"], logical_id

        policy_engine_tags = template["Resources"]["PolicyEngine"]["Properties"]["Tags"]
        normalized = {item["Key"]: item["Value"] for item in policy_engine_tags}
        assert TAGS.items() <= normalized.items()

    def test_a_new_custom_resource_cannot_silently_escape_the_tagging_table(self):
        template = {"Resources": {"Future": {"Type": "Custom::FutureResource", "Properties": {}}}}

        with pytest.raises(CfnExportUnsupportedError, match=r"Future \(Custom::FutureResource\)"):
            _apply_resource_tags(template, TAGS)

    def test_an_untaggable_policy_must_have_the_tagged_owner_it_claims(self):
        template = {
            "Resources": {
                "Policy": {
                    "Type": "Custom::AgentCorePolicy",
                    "Properties": {},
                }
            }
        }

        with pytest.raises(CfnExportUnsupportedError, match="contains no such owning resource"):
            _apply_resource_tags(template, TAGS)

    def test_a_policy_owner_with_a_broken_tag_shape_is_not_accepted_as_coverage(self):
        template = {
            "Resources": {
                "Engine": {
                    "Type": "AWS::BedrockAgentCore::PolicyEngine",
                    "Properties": {"Tags": "not-a-tag-list"},
                },
                "Policy": {
                    "Type": "Custom::AgentCorePolicy",
                    "Properties": {},
                },
            }
        }

        with pytest.raises(CfnExportUnsupportedError, match="owner is missing requested tag keys"):
            _apply_resource_tags(template, TAGS)

    def test_a_malformed_tag_collection_is_refused_instead_of_normalized(self):
        template = {"Resources": {}}

        with pytest.raises(CfnExportUnsupportedError, match="must be a map"):
            _apply_resource_tags(template, [])  # type: ignore[arg-type]

    def test_the_s3_object_limit_is_refused_instead_of_dropping_the_eleventh_tag(self):
        tags = {f"k{i}": f"v{i}" for i in range(11)}

        with pytest.raises(CfnExportUnsupportedError, match="accepts at most 10"):
            _template(tags)

    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "Api_Key",
            "SECRET",
            "platform:api-key",
            "vendor/Access.Token",
            "vendor.Access.Token",
            "platform-api-key",
            "namespace_private_key",
        ],
    )
    def test_a_credential_designating_tag_key_never_reaches_stack_events(self, key):
        with pytest.raises(CfnExportUnsupportedError, match="designates credential material"):
            _template({key: "value-is-deliberately-not-echoed"})

    def test_the_flat_provider_uses_the_same_secret_token_vocabulary(self):
        assert provider._RESOURCE_TAG_SECRET_TOKENS == _FORBIDDEN_SECRET_TOKENS

    def test_an_invalid_tag_value_is_never_repeated_in_an_export_error(self):
        accidental_secret = "not-a-real-secret,value"

        with pytest.raises(CfnExportUnsupportedError) as exc:
            _template({"classification": accidental_secret})

        assert accidental_secret not in str(exc.value)
        assert "value is not repeated" in str(exc.value)

    def test_the_generated_readme_explains_all_custom_resource_tag_sinks(self):
        readme = CfnTemplateGenerator().generate(_request(TAGS)).readme

        assert "S3 permits at most 10 object tags" in readme
        assert "reconciles the effective governance tags onto those groups" in readme
        assert "reconciles the effective governance tags onto the provider" in readme
        assert "AgentCore policy children do not support tags" in readme

    def test_builtin_tags_are_counted_toward_a_native_resources_limit(self):
        template = {
            "Resources": {
                "Gateway": {
                    "Type": "AWS::BedrockAgentCore::Gateway",
                    "Properties": {"Tags": {"ManagedBy": "CloudFormation", "Stack": "demo"}},
                }
            }
        }
        tags = {f"k{i}": "v" for i in range(50)}

        with pytest.raises(CfnExportUnsupportedError, match="would receive 52 tags"):
            _apply_resource_tags(template, tags)

    def test_tag_permissions_exist_only_when_the_handlers_can_reach_tag_calls(self):
        tagged = _template(TAGS)
        untagged = _template()

        assert "s3:PutObjectTagging" in _actions(tagged, "CodePackagingPolicy")
        assert {
            "logs:TagResource",
            "logs:TagLogGroup",
            "logs:UntagLogGroup",
            "logs:ListTagsForResource",
        } <= _actions(tagged, "RuntimeLogGroupGovernance")
        assert {
            "bedrock-agentcore:TagResource",
            "bedrock-agentcore:UntagResource",
        }.isdisjoint(_actions(tagged, "AgentCorePolicyManagement"))
        assert {
            "bedrock-agentcore:ListTagsForResource",
            "bedrock-agentcore:TagResource",
            "bedrock-agentcore:UntagResource",
        } <= _actions(tagged, "CredentialProviderPolicy")

        assert "s3:PutObjectTagging" not in _actions(untagged, "CodePackagingPolicy")
        assert "logs:TagResource" not in _actions(untagged, "RuntimeLogGroupGovernance")
        assert {
            "logs:TagLogGroup",
            "logs:UntagLogGroup",
            "logs:ListTagsForResource",
        } <= _actions(untagged, "RuntimeLogGroupGovernance")
        assert {
            "bedrock-agentcore:ListTagsForResource",
            "bedrock-agentcore:TagResource",
            "bedrock-agentcore:UntagResource",
        } <= _actions(untagged, "CredentialProviderPolicy")
        assert {
            "bedrock-agentcore:TagResource",
            "bedrock-agentcore:UntagResource",
        }.isdisjoint(_actions(untagged, "AgentCorePolicyManagement"))


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return out.getvalue()


class _Body:
    def __init__(self, data: bytes):
        self.data = data

    def read(self) -> bytes:
        return self.data


class FakeS3:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.objects = {
            "agent.zip": _zip_bytes({"agent.py": b"print('hello')"}),
            "bundle.zip": _zip_bytes({"dependency.py": b"VERSION = 1"}),
        }

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        return {"Body": _Body(self.objects[kwargs["Key"]])}

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))


def _code_event(tags: dict[str, str]) -> dict:
    return {
        "RequestType": "Create",
        "StackId": STACK_ID,
        "LogicalResourceId": "AgentCodePackage",
        "ResourceType": "Custom::AgentCodePackage",
        "ResourceProperties": {
            "ArtifactsBucket": "artifact-bucket",
            "AgentCodeKey": "agent.zip",
            "DependencyBundleKey": "bundle.zip",
            "OutputKey": "deployments/demo/code.zip",
            "ResourceTags": tags,
        },
    }


class TestCodePackageTags:
    def test_the_merged_object_is_written_with_url_encoded_tags(self, monkeypatch):
        s3 = FakeS3()
        monkeypatch.setattr(provider.boto3, "client", lambda service: s3 if service == "s3" else None)

        provider._handle_code_package_create_update(_code_event(TAGS))

        operation, kwargs = s3.calls[-1]
        assert operation == "put_object"
        assert dict(parse_qsl(kwargs["Tagging"], keep_blank_values=True)) == TAGS
        assert kwargs["ExpectedBucketOwner"] == ACCOUNT
        shape = botocore.session.get_session().get_service_model("s3").operation_model("PutObject").input_shape
        validate_parameters(kwargs, shape)

    def test_more_than_ten_tags_fails_before_any_s3_call(self, monkeypatch):
        monkeypatch.setattr(
            provider.boto3,
            "client",
            lambda *_args, **_kwargs: pytest.fail("S3 was reached before the object-tag limit was checked"),
        )

        with pytest.raises(provider.ProviderError, match="at most 10"):
            provider._handle_code_package_create_update(_code_event({f"k{i}": "v" for i in range(11)}))

    def test_a_credential_designating_key_fails_before_any_s3_call(self, monkeypatch):
        monkeypatch.setattr(
            provider.boto3,
            "client",
            lambda *_args, **_kwargs: pytest.fail("S3 was reached before tag-key validation"),
        )

        with pytest.raises(provider.ProviderError, match="designates credential material"):
            provider._handle_code_package_create_update(_code_event({"platform:client-secret": "not-a-real-secret"}))

    def test_a_non_map_fails_before_any_s3_call(self, monkeypatch):
        monkeypatch.setattr(
            provider.boto3,
            "client",
            lambda *_args, **_kwargs: pytest.fail("S3 was reached before tag-map validation"),
        )
        event = _code_event({})
        event["ResourceProperties"]["ResourceTags"] = []

        with pytest.raises(provider.ProviderError, match="must be a map"):
            provider._handle_code_package_create_update(event)


class _Exceptions:
    class ValidationException(Exception):
        pass

    class ResourceNotFoundException(Exception):
        pass


class FakeOAuth:
    def __init__(
        self,
        *,
        create_error: Exception | None = None,
        tag_error: Exception | None = None,
        list_error_once: Exception | None = None,
        delete_error: Exception | None = None,
        existing_client_id: str = "client-a",
        initial_tags: dict[str, str] | None = None,
    ):
        self.exceptions = _Exceptions
        self.create_error = create_error
        self.tag_error = tag_error
        self.list_error_once = list_error_once
        self.delete_error = delete_error
        self.existing_client_id = existing_client_id
        self.tags = dict(initial_tags or {})
        self.calls: list[tuple[str, dict]] = []
        self.arn = (
            "arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/oauth2credentialprovider/provider-a"
        )

    def create_oauth2_credential_provider(self, **kwargs):
        self.calls.append(("create", kwargs))
        if self.create_error:
            raise self.create_error
        self.tags = dict(kwargs.get("tags") or {})
        return {"credentialProviderArn": self.arn}

    def get_oauth2_credential_provider(self, **kwargs):
        self.calls.append(("get", kwargs))
        return {
            "credentialProviderArn": self.arn,
            "oauth2ProviderConfigOutput": {
                "customOauth2ProviderConfig": {
                    "clientId": self.existing_client_id,
                    "oauthDiscovery": {"discoveryUrl": "https://issuer.example/.well-known/openid-configuration"},
                }
            },
        }

    def update_oauth2_credential_provider(self, **kwargs):
        self.calls.append(("update", kwargs))
        return {"credentialProviderArn": self.arn}

    def tag_resource(self, **kwargs):
        self.calls.append(("tag", kwargs))
        if self.tag_error:
            error = self.tag_error
            self.tag_error = None
            raise error
        self.tags.update(kwargs["tags"])

    def untag_resource(self, **kwargs):
        self.calls.append(("untag", kwargs))
        for key in kwargs["tagKeys"]:
            self.tags.pop(key, None)

    def list_tags_for_resource(self, **kwargs):
        self.calls.append(("list_tags", kwargs))
        if self.list_error_once is not None:
            error = self.list_error_once
            self.list_error_once = None
            raise error
        return {"tags": dict(self.tags)}

    def delete_oauth2_credential_provider(self, **kwargs):
        self.calls.append(("delete", kwargs))
        if self.delete_error is not None:
            raise self.delete_error


def _oauth_event(request_type: str, tags: dict[str, str], old_tags=None) -> dict:
    event = {
        "RequestType": request_type,
        "StackId": STACK_ID,
        "LogicalResourceId": "McpOAuth2CredentialProvider",
        "ResourceType": "Custom::OAuth2CredentialProvider",
        "ResourceProperties": {
            "ProviderName": "provider-a",
            "DiscoveryUrl": "https://issuer.example/.well-known/openid-configuration",
            "ClientId": "client-a",
            "ClientSecret": "not-a-real-secret",
            "ResourceTags": tags,
        },
    }
    if request_type == "Update":
        event["PhysicalResourceId"] = (
            "arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/oauth2credentialprovider/provider-a"
        )
        event["OldResourceProperties"] = {"ResourceTags": old_tags or {}}
    return event


def _validate_agentcore(operation: str, kwargs: dict) -> None:
    shape = (
        botocore.session.get_session()
        .get_service_model("bedrock-agentcore-control")
        .operation_model(operation)
        .input_shape
    )
    validate_parameters(kwargs, shape)


class TestOAuthTags:
    def test_create_passes_tags_atomically_to_the_service(self, monkeypatch):
        ctrl = FakeOAuth()
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        provider._handle_oauth2_cred_create(_oauth_event("Create", TAGS))

        operation, kwargs = ctrl.calls[0]
        assert operation == "create"
        assert kwargs["tags"] == TAGS
        _validate_agentcore("CreateOauth2CredentialProvider", kwargs)
        assert [name for name, _kwargs in ctrl.calls] == ["create", "list_tags"]
        _validate_agentcore("ListTagsForResource", ctrl.calls[1][1])

    def test_update_sets_desired_tags_and_removes_only_its_stale_key(self, monkeypatch):
        old = {"platform:owner": "old", "Legacy": "remove-me"}
        ctrl = FakeOAuth(initial_tags={**old, "Foreign": "preserve"})
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)
        desired = {"platform:owner": "new", "Environment": "prod"}

        provider._handle_oauth2_cred_update(_oauth_event("Update", desired, old))

        assert [name for name, _kwargs in ctrl.calls] == [
            "list_tags",
            "tag",
            "untag",
            "list_tags",
            "update",
        ]
        assert ctrl.tags == {**desired, "Foreign": "preserve"}
        assert ctrl.calls[1][1] == {"resourceArn": ctrl.arn, "tags": desired}
        assert ctrl.calls[2][1] == {"resourceArn": ctrl.arn, "tagKeys": ["Legacy"]}
        _validate_agentcore("TagResource", ctrl.calls[1][1])
        _validate_agentcore("UntagResource", ctrl.calls[2][1])

    def test_adoption_proves_the_client_binding_before_it_writes_tags(self, monkeypatch):
        ctrl = FakeOAuth(
            create_error=_Exceptions.ValidationException("already exists"),
            initial_tags={"Foreign": "preserve"},
        )
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        provider._handle_oauth2_cred_create(_oauth_event("Create", TAGS))

        assert [name for name, _kwargs in ctrl.calls] == [
            "create",
            "get",
            "list_tags",
            "tag",
            "list_tags",
            "update",
            "list_tags",
        ]
        assert ctrl.tags == {**TAGS, "Foreign": "preserve"}
        assert ctrl.calls.index(next(call for call in ctrl.calls if call[0] == "get")) < ctrl.calls.index(
            next(call for call in ctrl.calls if call[0] == "tag")
        )

    def test_removing_the_final_managed_tag_preserves_foreign_tags(self, monkeypatch):
        old = {"Legacy": "remove-me"}
        ctrl = FakeOAuth(initial_tags={**old, "Foreign": "preserve"})
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        provider._handle_oauth2_cred_update(_oauth_event("Update", {}, old))

        assert [name for name, _kwargs in ctrl.calls] == [
            "list_tags",
            "untag",
            "list_tags",
            "update",
        ]
        assert ctrl.tags == {"Foreign": "preserve"}

    def test_a_tag_failure_stops_before_the_provider_configuration_is_updated(self, monkeypatch):
        ctrl = FakeOAuth(tag_error=ClientError({"Error": {"Code": "AccessDeniedException"}}, "TagResource"))
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        with pytest.raises(provider.ProviderError, match="failing rather than reporting"):
            provider._handle_oauth2_cred_update(_oauth_event("Update", TAGS, {}))

        assert [name for name, _kwargs in ctrl.calls] == ["list_tags", "tag", "list_tags"]

    def test_a_tagged_provider_removed_out_of_band_is_recreated(self, monkeypatch):
        ctrl = FakeOAuth(list_error_once=_Exceptions.ResourceNotFoundException("gone"))
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        provider._handle_oauth2_cred_update(_oauth_event("Update", TAGS, TAGS))

        assert [name for name, _kwargs in ctrl.calls] == ["list_tags", "create", "list_tags"]
        assert ctrl.tags == TAGS

    def test_a_failed_create_readback_compensates_the_new_provider(self, monkeypatch):
        ctrl = FakeOAuth(
            list_error_once=ClientError(
                {"Error": {"Code": "AccessDeniedException"}},
                "ListTagsForResource",
            )
        )
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        with pytest.raises(provider.ProviderError, match="could not be read"):
            provider._handle_oauth2_cred_create(_oauth_event("Create", TAGS))

        assert [name for name, _kwargs in ctrl.calls] == ["create", "list_tags", "delete"]

    def test_a_now_forbidden_old_key_can_still_be_removed(self, monkeypatch):
        old = {"client-secret": "the-value-is-never-inspected"}
        ctrl = FakeOAuth(initial_tags={"client-secret": "old", "Foreign": "preserve"})
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        provider._handle_oauth2_cred_update(_oauth_event("Update", {}, old))

        assert ctrl.tags == {"Foreign": "preserve"}
        assert ctrl.calls[1] == (
            "untag",
            {"resourceArn": ctrl.arn, "tagKeys": ["client-secret"]},
        )

    def test_malformed_old_properties_fail_before_any_tag_write(self, monkeypatch):
        ctrl = FakeOAuth(initial_tags={"Legacy": "old"})
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)
        event = _oauth_event("Update", {}, {"Legacy": "old"})
        event["OldResourceProperties"] = []

        with pytest.raises(provider.ProviderError, match="OldResourceProperties must be a map"):
            provider._handle_oauth2_cred_update(event)

        assert ctrl.calls == []

    def test_a_tag_key_can_be_replaced_at_the_fifty_tag_ceiling(self, monkeypatch):
        old = {"Legacy": "old"}
        current = {"Legacy": "old", **{f"Foreign{i}": "v" for i in range(49)}}
        ctrl = FakeOAuth(initial_tags=current)
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        provider._handle_oauth2_cred_update(_oauth_event("Update", {"Replacement": "new"}, old))

        assert [name for name, _kwargs in ctrl.calls] == [
            "list_tags",
            "untag",
            "tag",
            "list_tags",
            "update",
        ]
        assert len(ctrl.tags) == 50
        assert "Legacy" not in ctrl.tags
        assert ctrl.tags["Replacement"] == "new"

    def test_a_failed_capacity_swap_restores_the_previous_tag_set(self, monkeypatch):
        current = {"Legacy": "old", **{f"Foreign{i}": "v" for i in range(49)}}
        ctrl = FakeOAuth(
            initial_tags=current,
            tag_error=_client_error("AccessDeniedException", "TagResource"),
        )
        monkeypatch.setattr(provider, "_get_agentcore_ctrl", lambda: ctrl)

        with pytest.raises(provider.ProviderError, match="could not be reconciled"):
            provider._handle_oauth2_cred_update(_oauth_event("Update", {"Replacement": "new"}, {"Legacy": "old"}))

        assert ctrl.tags == current
        assert [name for name, _kwargs in ctrl.calls] == [
            "list_tags",
            "untag",
            "tag",
            "list_tags",
            "tag",
        ]
        assert "update" not in [name for name, _kwargs in ctrl.calls]


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeLogs:
    def __init__(
        self,
        *,
        exists: bool,
        tag_error: Exception | None = None,
        list_error: Exception | None = None,
        initial_tags: dict[str, str] | None = None,
        drop_create_tags: bool = False,
        drop_tag_writes: bool = False,
    ):
        self.exists = exists
        self.tag_error = tag_error
        self.list_error = list_error
        self.tags = dict(initial_tags or {})
        self.drop_create_tags = drop_create_tags
        self.drop_tag_writes = drop_tag_writes
        self.calls: list[tuple[str, dict]] = []
        self.meta = type("Meta", (), {"region_name": "us-east-1"})()

    def create_log_group(self, **kwargs):
        self.calls.append(("create", kwargs))
        if self.exists:
            raise _client_error("ResourceAlreadyExistsException", "CreateLogGroup")
        if not self.drop_create_tags:
            self.tags = dict(kwargs.get("tags") or {})

    def tag_log_group(self, **kwargs):
        self.calls.append(("tag", kwargs))
        if self.tag_error:
            error = self.tag_error
            self.tag_error = None
            raise error
        if not self.drop_tag_writes:
            self.tags.update(kwargs["tags"])

    def untag_log_group(self, **kwargs):
        self.calls.append(("untag", kwargs))
        for key in kwargs["tags"]:
            self.tags.pop(key, None)

    def list_tags_for_resource(self, **kwargs):
        self.calls.append(("list_tags", kwargs))
        if self.list_error:
            raise self.list_error
        return {"tags": dict(self.tags)}

    def disassociate_kms_key(self, **kwargs):
        self.calls.append(("disassociate", kwargs))

    def put_retention_policy(self, **kwargs):
        self.calls.append(("retention", kwargs))


LOG_GROUP = "/aws/bedrock-agentcore/runtimes/runtime_demo-a1b2c3d4e5-DEFAULT"


def _log_event(tags: dict[str, str], old_tags=None) -> dict:
    return {
        "RequestType": "Update",
        "StackId": STACK_ID,
        "LogicalResourceId": "RuntimeLogGroups",
        "ResourceType": "Custom::RuntimeLogGroup",
        "PhysicalResourceId": "runtime-log-groups/RuntimeLogGroups",
        "ResourceProperties": {
            "LogGroupNames": [LOG_GROUP],
            "RetentionInDays": "7",
            "ResourceTags": tags,
        },
        "OldResourceProperties": {"ResourceTags": old_tags or {}},
    }


def _validate_logs(operation: str, kwargs: dict) -> None:
    shape = botocore.session.get_session().get_service_model("logs").operation_model(operation).input_shape
    validate_parameters(kwargs, shape)


class TestRuntimeLogGroupTags:
    def test_a_new_group_gets_tags_in_the_create_call(self, monkeypatch):
        logs = FakeLogs(exists=False)
        monkeypatch.setattr(provider.boto3, "client", lambda service: logs)

        provider._handle_runtime_log_group_create_update(_log_event(TAGS))

        assert [name for name, _kwargs in logs.calls] == ["create", "list_tags", "retention"]
        assert logs.calls[0][1]["tags"] == TAGS
        _validate_logs("CreateLogGroup", logs.calls[0][1])
        _validate_logs("ListTagsForResource", logs.calls[1][1])

    def test_an_existing_group_is_tagged_before_other_governance_mutations(self, monkeypatch):
        logs = FakeLogs(
            exists=True,
            initial_tags={
                "platform:owner": "old",
                "Obsolete": "yes",
                "Foreign": "preserve",
            },
        )
        monkeypatch.setattr(provider.boto3, "client", lambda service: logs)

        provider._handle_runtime_log_group_create_update(_log_event(TAGS, {"platform:owner": "old", "Obsolete": "yes"}))

        assert [name for name, _kwargs in logs.calls] == [
            "create",
            "list_tags",
            "tag",
            "untag",
            "list_tags",
            "disassociate",
            "retention",
        ]
        assert logs.calls[2][1] == {"logGroupName": LOG_GROUP, "tags": TAGS}
        assert logs.calls[3][1] == {"logGroupName": LOG_GROUP, "tags": ["Obsolete"]}
        assert logs.tags == {**TAGS, "Foreign": "preserve"}
        _validate_logs("TagLogGroup", logs.calls[2][1])
        _validate_logs("UntagLogGroup", logs.calls[3][1])

    def test_a_denied_tag_write_stops_before_retention_or_key_changes(self, monkeypatch):
        logs = FakeLogs(
            exists=True,
            tag_error=_client_error("AccessDeniedException", "TagLogGroup"),
        )
        monkeypatch.setattr(provider.boto3, "client", lambda service: logs)

        with pytest.raises(provider.ProviderError, match="failing rather than reporting"):
            provider._handle_runtime_log_group_create_update(_log_event(TAGS))

        assert [name for name, _kwargs in logs.calls] == [
            "create",
            "list_tags",
            "tag",
            "list_tags",
        ]

    def test_removing_the_final_managed_tag_uses_the_old_properties(self, monkeypatch):
        logs = FakeLogs(exists=True, initial_tags={"Legacy": "remove-me"})
        monkeypatch.setattr(provider.boto3, "client", lambda service: logs)

        provider._handle_runtime_log_group_create_update(_log_event({}, {"Legacy": "remove-me"}))

        assert [name for name, _kwargs in logs.calls] == [
            "create",
            "list_tags",
            "untag",
            "list_tags",
            "disassociate",
            "retention",
        ]
        assert logs.calls[2][1] == {"logGroupName": LOG_GROUP, "tags": ["Legacy"]}

    def test_a_create_that_drops_tags_fails_before_retention(self, monkeypatch):
        logs = FakeLogs(exists=False, drop_create_tags=True, drop_tag_writes=True)
        monkeypatch.setattr(provider.boto3, "client", lambda service: logs)

        with pytest.raises(provider.ProviderError, match="did not converge"):
            provider._handle_runtime_log_group_create_update(_log_event(TAGS))

        assert [name for name, _kwargs in logs.calls] == ["create", "list_tags", "tag", "list_tags"]
        assert "retention" not in [name for name, _kwargs in logs.calls]

    def test_a_tag_key_can_be_replaced_at_the_fifty_tag_ceiling(self, monkeypatch):
        current = {"Legacy": "old", **{f"Foreign{i}": "v" for i in range(49)}}
        logs = FakeLogs(exists=True, initial_tags=current)
        monkeypatch.setattr(provider.boto3, "client", lambda service: logs)

        provider._handle_runtime_log_group_create_update(_log_event({"Replacement": "new"}, {"Legacy": "old"}))

        assert [name for name, _kwargs in logs.calls][:5] == [
            "create",
            "list_tags",
            "untag",
            "tag",
            "list_tags",
        ]
        assert len(logs.tags) == 50
        assert "Legacy" not in logs.tags
        assert logs.tags["Replacement"] == "new"

    def test_a_failed_log_capacity_swap_restores_the_previous_tag_set(self, monkeypatch):
        current = {"Legacy": "old", **{f"Foreign{i}": "v" for i in range(49)}}
        logs = FakeLogs(
            exists=True,
            initial_tags=current,
            tag_error=_client_error("AccessDeniedException", "TagLogGroup"),
        )
        monkeypatch.setattr(provider.boto3, "client", lambda service: logs)

        with pytest.raises(provider.ProviderError, match="could not be reconciled"):
            provider._handle_runtime_log_group_create_update(_log_event({"Replacement": "new"}, {"Legacy": "old"}))

        assert logs.tags == current
        assert [name for name, _kwargs in logs.calls] == [
            "create",
            "list_tags",
            "untag",
            "tag",
            "list_tags",
            "tag",
        ]
        assert "retention" not in [name for name, _kwargs in logs.calls]
