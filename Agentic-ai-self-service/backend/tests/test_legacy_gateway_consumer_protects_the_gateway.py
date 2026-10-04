"""F-09: a legacy deployment that names its gateway only in ``gateway_result`` is a live consumer.

``has_other_live_resource_reference`` projected only ``created_resources``. ``live_gateway_consumers``
(F-62/F-63) also reads ``gateway_result.gateway_id`` precisely because rows written before the
manifest existed name the gateway nowhere else. ``manifest_delete_refusal`` used only the first, so
with legacy deployment L (no manifest, ``gateway_result.gateway_id = G``, live) and manifest
deployment A that created or adopted G, ``DELETE A`` saw no co-resident, deleted G's targets and G,
and L's runtime lost its tool plane.

The projection now mirrors ``live_gateway_consumers`` for the gateway: a legacy row protects the
gateway it names, and account/region exclude it only when the row RECORDS a different one (an old
row that recorded neither still protects).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from app.services.deployment_state_store import (
    CO_RESIDENT_REFUSAL,
    DeploymentStateStore,
    manifest_delete_refusal,
)

ACCOUNT = "111122223333"
REGION = "us-east-1"
GATEWAY = "legacygw-abc123"


def _store(items: list[dict]) -> tuple[DeploymentStateStore, MagicMock]:
    store = object.__new__(DeploymentStateStore)
    table = MagicMock()
    table.scan.return_value = {"Items": items}
    store._table = table
    return store, table


def _legacy_row(**overrides) -> dict:
    """A pre-manifest row: no ``created_resources``, the gateway named only in the step result."""
    row = {
        "deployment_id": "legacy-L",
        "status": "succeeded",
        "gateway_result": {
            "gateway_id": GATEWAY,
            "gateway_url": f"https://{GATEWAY}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp",
            "client_info": {"client_id": "abc"},
        },
    }
    row.update(overrides)
    return row


def _gateway_row_of_a() -> dict:
    return {"type": "gateway", "id": GATEWAY, "region": REGION, "account": ACCOUNT, "created_by_deployment": True}


def _referenced(store: DeploymentStateStore, resource: dict | None = None) -> bool:
    return store.has_other_live_resource_reference(
        "manifest-A",
        resource or _gateway_row_of_a(),
        target_account_id=ACCOUNT,
        target_region=REGION,
    )


class TestALegacyRowProtectsItsGateway:
    def test_a_live_legacy_row_with_no_recorded_target_protects_the_gateway(self):
        store, _ = _store([_legacy_row()])
        assert _referenced(store) is True

    def test_a_live_legacy_row_recording_the_same_target_protects_the_gateway(self):
        store, _ = _store([_legacy_row(target_account_id=ACCOUNT, target_region=REGION)])
        assert _referenced(store) is True

    @pytest.mark.parametrize("key", ["gatewayId", "gateway_id"])
    def test_both_spellings_of_the_id_count(self, key):
        store, _ = _store([_legacy_row(gateway_result={key: GATEWAY})])
        assert _referenced(store) is True

    def test_a_json_string_gateway_result_is_read(self):
        store, _ = _store([_legacy_row(gateway_result=json.dumps({"gateway_id": GATEWAY}))])
        assert _referenced(store) is True

    def test_manifest_delete_refusal_refuses_with_the_hand_off_sentinel(self):
        """The teardown compares against this exact string to treat the refusal as a hand-off."""
        store, _ = _store([_legacy_row()])
        assert (
            manifest_delete_refusal(
                store, "manifest-A", _gateway_row_of_a(), target_account_id=ACCOUNT, target_region=REGION
            )
            == CO_RESIDENT_REFUSAL
        )

    def test_the_scan_projects_the_gateway_result_with_a_strong_read(self):
        store, table = _store([_legacy_row()])
        _referenced(store)
        table.scan.assert_called_once()
        kwargs = table.scan.call_args.kwargs
        assert kwargs["ConsistentRead"] is True
        assert "created_resources" in kwargs["ProjectionExpression"]
        assert "gateway_result" in kwargs["ProjectionExpression"], kwargs


class TestWhatDoesNotProtect:
    @pytest.mark.parametrize("delete_status", ["deleted", "deleting"])
    def test_a_legacy_row_already_being_torn_down_does_not(self, delete_status):
        store, _ = _store([_legacy_row(delete_status=delete_status)])
        assert _referenced(store) is False

    @pytest.mark.parametrize("delete_status", ["delete_retained", "delete_failed"])
    def test_a_retained_or_failed_legacy_row_still_does(self, delete_status):
        store, _ = _store([_legacy_row(delete_status=delete_status)])
        assert _referenced(store) is True

    def test_a_legacy_row_naming_another_gateway_does_not(self):
        store, _ = _store([_legacy_row(gateway_result={"gateway_id": "othergw-zzz999"})])
        assert _referenced(store) is False

    def test_a_legacy_row_that_records_a_different_region_does_not(self):
        store, _ = _store([_legacy_row(target_region="eu-west-1")])
        assert _referenced(store) is False

    def test_a_legacy_row_that_records_a_different_account_does_not(self):
        store, _ = _store([_legacy_row(target_account_id="999988887777")])
        assert _referenced(store) is False

    def test_the_deployment_under_teardown_does_not_protect_itself(self):
        store, _ = _store([_legacy_row(deployment_id="manifest-A")])
        assert _referenced(store) is False

    def test_a_legacy_gateway_does_not_protect_an_unrelated_resource_type(self):
        """The projection is for the gateway only; a memory with the same id string is not it."""
        store, _ = _store([_legacy_row()])
        assert _referenced(store, {"type": "memory", "id": GATEWAY, "region": REGION, "account": ACCOUNT}) is False

    def test_an_empty_gateway_result_is_not_a_reference(self):
        store, _ = _store(
            [
                _legacy_row(deployment_id="l1", gateway_result={}),
                _legacy_row(deployment_id="l2", gateway_result=None),
                _legacy_row(deployment_id="l3", gateway_result=""),
                _legacy_row(deployment_id="l4", gateway_result={"gateway_url": "https://x.example/mcp"}),
            ]
        )
        assert _referenced(store) is False

    @pytest.mark.parametrize("bad", ["not json", [GATEWAY], 42], ids=["unreadable-json", "list", "number"])
    def test_an_unreadable_gateway_result_raises_and_the_refusal_says_so(self, bad):
        """Same rule as ``live_gateway_consumers``: a row that cannot be read is not proof that no
        legacy deployment is on the gateway. ``manifest_delete_refusal`` turns that into a
        retention, never a delete."""
        store, _ = _store([_legacy_row(gateway_result=bad)])
        with pytest.raises(ValueError):
            _referenced(store)
        refusal = manifest_delete_refusal(
            store, "manifest-A", _gateway_row_of_a(), target_account_id=ACCOUNT, target_region=REGION
        )
        assert refusal and "could not prove" in refusal


class TestTheSnapshotIsStillOnePass:
    def test_one_scan_serves_a_manifest_row_and_the_legacy_projection(self):
        store, table = _store(
            [
                _legacy_row(),
                {
                    "deployment_id": "manifest-B",
                    "status": "succeeded",
                    "target_account_id": ACCOUNT,
                    "target_region": REGION,
                    "created_resources": [{"type": "memory", "id": "mem-1", "created_by_deployment": False}],
                },
            ]
        )
        assert _referenced(store) is True
        assert _referenced(store, {"type": "memory", "id": "mem-1", "region": REGION, "account": ACCOUNT}) is True
        assert _referenced(store, {"type": "memory", "id": "mem-2", "region": REGION, "account": ACCOUNT}) is False
        table.scan.assert_called_once()
