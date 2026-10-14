"""Lifecycle contract for Custom::ManagedWebSearchTarget.

The provider is exercised with a stateful AgentCore control-plane stand-in so
failure-only paths are deterministic. Real service creation/invocation/deletion
is covered by the release live matrix; these tests pin idempotency, exact request
shape, ownership, synchronization scope, and rollback cleanup.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from botocore.session import Session
from botocore.validate import validate_parameters


def _import_provider():
    provider_dir = Path(__file__).resolve().parents[1] / "src" / "app" / "services" / "cfn_provider"
    if str(provider_dir) not in sys.path:
        sys.path.insert(0, str(provider_dir))
    import handler  # noqa: PLC0415

    return handler


provider = _import_provider()

ACCOUNT = "111122223333"
STACK_ID = f"arn:aws:cloudformation:eu-west-1:{ACCOUNT}:stack/search-demo/stack-id"
GATEWAY_ID = "gateway_demo-1234567890"
TARGET_ID = "target_demo-1234567890"


class _Context:
    def get_remaining_time_in_millis(self):
        return 300_000


class _ExpiredContext:
    def get_remaining_time_in_millis(self):
        return 40_000


class _Exceptions:
    class ResourceNotFoundException(Exception):
        pass


class FakeGatewayControl:
    """Stateful minimum of bedrock-agentcore-control used by the provider."""

    def __init__(
        self,
        event: dict,
        *,
        create_raises=None,
        sync_target_id: str = TARGET_ID,
        advance_sync: bool = True,
    ):
        self.exceptions = _Exceptions
        self.event = event
        self.create_raises = create_raises
        self.sync_target_id = sync_target_id
        self.advance_sync = advance_sync
        self.calls: list[tuple[str, dict]] = []
        self.target: dict | None = None
        self.list_pages: list[dict] | None = None
        self._page = 0

    def exact_target(self, *, status="READY", synchronized=True, **changes):
        request = provider._managed_web_search_target_request(self.event, GATEWAY_ID)
        target = {
            "gatewayArn": f"arn:aws:bedrock-agentcore:eu-west-1:{ACCOUNT}:gateway/{GATEWAY_ID}",
            "targetId": TARGET_ID,
            "name": request["name"],
            "description": request["description"],
            "targetConfiguration": request["targetConfiguration"],
            "credentialProviderConfigurations": request["credentialProviderConfigurations"],
            "status": status,
        }
        if synchronized:
            target["lastSynchronizedAt"] = "2026-10-14T00:00:00Z"
        target.update(changes)
        return target

    def create_gateway_target(self, **kwargs):
        self.calls.append(("create", kwargs))
        if self.create_raises:
            raise self.create_raises
        self.target = self.exact_target(synchronized=False)
        return {"targetId": TARGET_ID, "status": "CREATING"}

    def get_gateway_target(self, **kwargs):
        self.calls.append(("get", kwargs))
        if self.target is None:
            raise self.exceptions.ResourceNotFoundException()
        return dict(self.target)

    def list_gateway_targets(self, **kwargs):
        self.calls.append(("list", kwargs))
        if self.list_pages is not None:
            page = self.list_pages[min(self._page, len(self.list_pages) - 1)]
            self._page += 1
            return page
        if self.target is None:
            return {"items": []}
        return {"items": [{"name": self.target["name"], "targetId": self.target["targetId"]}]}

    def synchronize_gateway_targets(self, **kwargs):
        self.calls.append(("sync", kwargs))
        if self.target is not None:
            self.target["status"] = "READY"
            if self.advance_sync:
                self.target["lastSynchronizedAt"] = "2026-10-14T00:00:01Z"
        returned = dict(self.target or {})
        returned["targetId"] = self.sync_target_id
        return {"targets": [returned]}

    def update_gateway_target(self, **kwargs):
        self.calls.append(("update", kwargs))
        self.target = {
            **(self.target or {}),
            "targetId": kwargs["targetId"],
            "name": kwargs["name"],
            "description": kwargs["description"],
            "targetConfiguration": kwargs["targetConfiguration"],
            "credentialProviderConfigurations": kwargs["credentialProviderConfigurations"],
            "status": "READY",
        }
        return dict(self.target)

    def delete_gateway_target(self, **kwargs):
        self.calls.append(("delete", kwargs))
        self.target = None
        return {"targetId": kwargs["targetId"], "status": "DELETING"}


def _event(request_type="Create", **overrides):
    event = {
        "RequestType": request_type,
        "StackId": STACK_ID,
        "LogicalResourceId": "ManagedWebSearchTarget",
        "ResourceType": "Custom::ManagedWebSearchTarget",
        "ResourceProperties": {
            "GatewayIdentifier": GATEWAY_ID,
            "Region": "eu-west-1",
            "TargetName": "AmazonWebSearch",
            "ConnectorId": "web-search",
            "ConnectorVersion": "1.2.0",
            "ToolName": "WebSearch",
        },
    }
    if request_type in {"Update", "Delete"}:
        event["PhysicalResourceId"] = provider._managed_web_search_physical_id(GATEWAY_ID, TARGET_ID)
    event.update(overrides)
    return event


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(provider.time, "sleep", lambda _seconds: None)


class TestManagedWebSearchCreate:
    def test_create_pins_exact_connector_and_synchronizes_only_its_target(self, monkeypatch):
        event = _event()
        ctrl = FakeGatewayControl(event)
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        data, physical_id = provider._handle_managed_web_search_create(event, _Context())

        create = next(kwargs for call, kwargs in ctrl.calls if call == "create")
        assert create["gatewayIdentifier"] == GATEWAY_ID
        assert create["name"] == "AmazonWebSearch"
        assert create["targetConfiguration"] == {
            "mcp": {
                "connector": {
                    "source": {"connectorId": "web-search", "version": "1.2.0"},
                    "configurations": [{"name": "WebSearch", "parameterValues": {}}],
                }
            }
        }
        assert create["credentialProviderConfigurations"] == [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
        assert len(create["clientToken"]) == 64
        assert next(kwargs for call, kwargs in ctrl.calls if call == "sync") == {
            "gatewayIdentifier": GATEWAY_ID,
            "targetIdList": [TARGET_ID],
        }
        assert data["ConnectorVersion"] == "1.2.0"
        assert physical_id == f"managed-web-search-target/{GATEWAY_ID}/{TARGET_ID}"

    def test_exact_create_and_update_requests_match_the_pinned_sdk_model(self):
        event = _event()
        request = provider._managed_web_search_target_request(event, GATEWAY_ID)
        model = Session().get_service_model("bedrock-agentcore-control")
        validate_parameters(
            {**request, "clientToken": provider._managed_web_search_client_token(event, GATEWAY_ID)},
            model.operation_model("CreateGatewayTarget").input_shape,
        )
        validate_parameters(
            {**request, "targetId": TARGET_ID},
            model.operation_model("UpdateGatewayTarget").input_shape,
        )

    def test_conflict_adopts_only_the_exact_owned_configuration(self, monkeypatch):
        event = _event()
        ctrl = FakeGatewayControl(event, create_raises=_client_error("ConflictException", "CreateGatewayTarget"))
        ctrl.target = ctrl.exact_target()
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        data, physical_id = provider._handle_managed_web_search_create(event, _Context())

        assert data["TargetId"] == TARGET_ID
        assert physical_id.endswith(f"/{TARGET_ID}")
        assert [name for name, _kwargs in ctrl.calls].count("list") == 1
        assert any(name == "sync" for name, _kwargs in ctrl.calls)

    @pytest.mark.parametrize("mutation", ["owner", "version"])
    def test_conflict_refuses_foreign_or_drifted_target(self, monkeypatch, mutation):
        event = _event()
        ctrl = FakeGatewayControl(event, create_raises=_client_error("ConflictException", "CreateGatewayTarget"))
        ctrl.target = ctrl.exact_target()
        if mutation == "owner":
            ctrl.target["description"] = "somebody else's target"
        else:
            ctrl.target["targetConfiguration"]["mcp"]["connector"]["source"]["version"] = "1.1.0"
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        with pytest.raises(provider.ProviderError):
            provider._handle_managed_web_search_create(event, _Context())

    def test_wrong_target_in_synchronize_response_fails_closed(self, monkeypatch):
        event = _event()
        ctrl = FakeGatewayControl(event, sync_target_id="target_outside-1234567890")
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        with pytest.raises(provider.ProviderError, match="outside the requested target id"):
            provider._handle_managed_web_search_create(event, _Context())

    def test_stale_synchronization_receipt_fails_closed(self, monkeypatch):
        event = _event()
        ctrl = FakeGatewayControl(event, advance_sync=False)
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        with pytest.raises(provider.ProviderError, match="receipt did not advance"):
            provider._handle_managed_web_search_create(event, _ExpiredContext())

    def test_unsupported_region_fails_before_any_aws_client(self, monkeypatch):
        event = _event(
            StackId=f"arn:aws:cloudformation:eu-central-1:{ACCOUNT}:stack/search-demo/stack-id",
            ResourceProperties={**_event()["ResourceProperties"], "Region": "eu-central-1"},
        )
        monkeypatch.setattr(
            provider.boto3,
            "client",
            lambda *_a, **_k: pytest.fail("an AWS client was constructed before region admission"),
        )

        with pytest.raises(provider.ProviderError, match="not available in eu-central-1"):
            provider._handle_managed_web_search_create(event, _Context())

    @pytest.mark.parametrize("field", ["TargetName", "ConnectorId", "ConnectorVersion", "ToolName"])
    def test_contract_mutations_fail_before_any_aws_client(self, monkeypatch, field):
        props = {**_event()["ResourceProperties"], field: "removed-or-wrong"}
        event = _event(ResourceProperties=props)
        monkeypatch.setattr(
            provider.boto3,
            "client",
            lambda *_a, **_k: pytest.fail("an AWS client was constructed for an invalid contract"),
        )

        with pytest.raises(provider.ProviderError, match="pinned contract"):
            provider._handle_managed_web_search_create(event, _Context())


class TestManagedWebSearchUpdateAndDelete:
    def test_update_reapplies_exact_contract_and_resynchronizes(self, monkeypatch):
        event = _event("Update")
        ctrl = FakeGatewayControl(event)
        ctrl.target = ctrl.exact_target()
        ctrl.target["targetConfiguration"]["mcp"]["connector"]["source"]["version"] = "1.1.0"
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        data, physical_id = provider._handle_managed_web_search_update(event, _Context())

        update = next(kwargs for call, kwargs in ctrl.calls if call == "update")
        assert update["targetId"] == TARGET_ID
        assert update["targetConfiguration"]["mcp"]["connector"]["source"] == {
            "connectorId": "web-search",
            "version": "1.2.0",
        }
        assert any(call == "sync" for call, _kwargs in ctrl.calls)
        assert data["ConnectorVersion"] == "1.2.0"
        assert physical_id == event["PhysicalResourceId"]

    def test_update_refuses_to_overwrite_a_target_with_lost_ownership(self, monkeypatch):
        event = _event("Update")
        ctrl = FakeGatewayControl(event)
        ctrl.target = ctrl.exact_target(description="foreign")
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        with pytest.raises(provider.ProviderError, match="refusing to overwrite"):
            provider._handle_managed_web_search_update(event, _Context())
        assert not any(call == "update" for call, _kwargs in ctrl.calls)

    def test_delete_removes_exact_target_and_polls_absent(self, monkeypatch):
        event = _event("Delete")
        ctrl = FakeGatewayControl(event)
        ctrl.target = ctrl.exact_target()
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        data, physical_id = provider._handle_managed_web_search_delete(event, _Context())

        assert next(kwargs for call, kwargs in ctrl.calls if call == "delete") == {
            "gatewayIdentifier": GATEWAY_ID,
            "targetId": TARGET_ID,
        }
        assert data == {"DeletedTargetId": TARGET_ID}
        assert physical_id == event["PhysicalResourceId"]
        assert ctrl.target is None

    def test_failed_create_delete_recovers_owned_target_by_marker(self, monkeypatch):
        event = _event("Delete", PhysicalResourceId="ManagedWebSearchTarget")
        ctrl = FakeGatewayControl(event)
        ctrl.target = ctrl.exact_target()
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        data, _physical_id = provider._handle_managed_web_search_delete(event, _Context())

        assert data == {"DeletedTargetId": TARGET_ID}
        assert [call for call, _kwargs in ctrl.calls][:2] == ["list", "get"]
        assert any(call == "delete" for call, _kwargs in ctrl.calls)

    def test_delete_does_not_remove_a_same_name_foreign_target(self, monkeypatch):
        event = _event("Delete", PhysicalResourceId="ManagedWebSearchTarget")
        ctrl = FakeGatewayControl(event)
        ctrl.target = ctrl.exact_target(description="foreign")
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        data, _physical_id = provider._handle_managed_web_search_delete(event, _Context())

        assert data == {}
        assert ctrl.target is not None
        assert not any(call == "delete" for call, _kwargs in ctrl.calls)

    def test_delete_still_removes_owned_target_after_configuration_drift(self, monkeypatch):
        event = _event("Delete")
        ctrl = FakeGatewayControl(event)
        ctrl.target = ctrl.exact_target()
        ctrl.target["targetConfiguration"] = {"mcp": {"connector": {"source": {"connectorId": "wrong"}}}}
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        provider._handle_managed_web_search_delete(event, _Context())

        assert ctrl.target is None

    def test_pagination_cycle_fails_instead_of_spinning(self, monkeypatch):
        event = _event("Delete", PhysicalResourceId="ManagedWebSearchTarget")
        ctrl = FakeGatewayControl(event)
        ctrl.list_pages = [
            {"items": [], "nextToken": "same"},
            {"items": [], "nextToken": "same"},
        ]
        monkeypatch.setattr(provider.boto3, "client", lambda *_a, **_k: ctrl)

        with pytest.raises(provider.ProviderError, match="repeated a pagination token"):
            provider._handle_managed_web_search_delete(event, _Context())
