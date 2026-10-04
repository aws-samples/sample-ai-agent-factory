"""A harness deploy that dies after minting the gateway OAuth provider must
still be tearable down.

``harness_step`` creates a harness->gateway outbound OAuth2 credential provider
BEFORE it creates the harness. That provider is a real AWS resource holding the
gateway's Cognito client credentials, and it is reachable for teardown by exactly
two routes:

  1. an ``oauth2_credential_provider`` row in the deployment manifest, or
  2. ``destroy_harness`` reconstructing the deterministic name
     ``harness-gw-<harness_name>`` from a recorded ``harness_id``.

Route 2 needs a harness. So when ``create_harness`` raises, route 2 does not
exist -- and the row used to be written *after* ``create_harness``, so route 1
did not exist either. The provider was orphaned with nothing pointing at it.

This is the same orphan-guard rule the handler already states for the harness and
its exec role ("record ... right after create succeeds"); the provider is simply
created earlier, so it has to be recorded earlier.

ARCC guidance ``cnt_ua0cTwldOsODs8``: tap into the related system's resource
deletion flow so trust relationships are also deleted upon resource deletion. The
provider IS a trust relationship -- it exists only to let the harness authenticate
to the gateway -- so it has to die with the deployment that created it.
"""

from unittest.mock import MagicMock

import pytest
from app.services.resource_ownership import owner_tags


class _Store:
    """Records manifest rows; the step-status writers are no-ops like the real ones."""

    def __init__(self):
        self.resources: list[dict] = []

    def update_step(self, *a, **kw):
        pass

    def update_status(self, *a, **kw):
        pass

    def record_resource(self, _deployment_id, resource):
        self.resources.append(resource)


PROVIDER_NAME = "harness-gw-hstepfail"

# RuntimeConfig requires both `name` and `model`, and a validator rejects model
# IDs outside the supported window (Bug 113) -- so this cannot be trimmed further.
_CONFIG = {"name": "hstepfail", "model": {"modelId": "us.anthropic.claude-sonnet-5"}}


def _run(monkeypatch, *, create_harness, store=None):
    """Drive harness_step.handler with a connected gateway, stubbing every AWS call.

    ``create_harness`` is injected so one test can let it succeed and the other
    can make it raise, with nothing else differing between them. ``store`` can be
    passed in so a caller that expects the handler to raise still holds the
    manifest the handler wrote before it did.
    """
    from app.services import harness_deployer
    from app.step_handlers import harness_step

    # Pin the region. The step reads APP_AWS_REGION and this dev shell exports
    # us-west-2, which would make the row assertions pass or fail by accident.
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    # A shared role would suppress the iam_role row; keep the per-harness path.
    monkeypatch.delenv("SHARED_HARNESS_ROLE_ARN", raising=False)

    store = store if store is not None else _Store()
    monkeypatch.setattr(harness_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(harness_step.step_clients, "client", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(harness_deployer, "sanitize_harness_name", lambda n: "hstepfail")
    monkeypatch.setattr(
        harness_deployer,
        "get_shared_or_new_harness_role",
        lambda *a, **kw: "arn:aws:iam::1:role/AgentCoreHarness-hstepfail",
    )
    # The provider really was created: a non-empty ARN is the signal the handler
    # uses to decide the resource exists.
    monkeypatch.setattr(
        harness_deployer,
        "ensure_gateway_outbound_provider",
        lambda *a, **kw: (
            "arn:aws:bedrock-agentcore:us-east-1:1:token-vault/default/oauth2credentialprovider/x",
            ["s"],
            True,
        ),
    )
    monkeypatch.setattr(harness_deployer, "create_harness", create_harness)
    monkeypatch.setattr(
        harness_deployer,
        "wait_for_harness_ready",
        lambda *a, **kw: {"success": True, "status": "READY", "backing_runtime_id": "harness_hstepfail-AbCdEf1234"},
    )

    event = {
        "deployment_id": "d-hstepfail",
        "config": _CONFIG,
        "gateway_result": {
            "gateway_arn": "arn:aws:bedrock-agentcore:us-east-1:1:gateway/gw-abc",
            "client_info": {"client_id": "cid", "user_pool_id": "us-east-1_p"},
        },
    }
    out = harness_step.handler(event, None)
    return store, out


def _provider_rows(store):
    return [r for r in store.resources if r.get("type") == "oauth2_credential_provider"]


def test_provider_is_recorded_even_when_create_harness_raises(monkeypatch):
    """The regression. Without the fix, zero rows are recorded at all: the harness
    row, the role row and the provider row all sit after the call that raised."""

    def _boom(*a, **kw):
        raise RuntimeError("ValidationException: harness quota exceeded")

    # The handler logs and re-raises so Step Functions marks the step FAILED --
    # that part is correct and must stay. The bug was what the manifest held
    # afterwards, so capture the store and assert on it past the raise.
    store = _Store()
    with pytest.raises(RuntimeError, match="harness quota exceeded"):
        store, _ = _run(monkeypatch, create_harness=_boom, store=store)

    rows = _provider_rows(store)
    assert rows == [
        {
            "type": "oauth2_credential_provider",
            "name": PROVIDER_NAME,
            "region": "us-east-1",
            "created_by_deployment": True,
        }
    ], (
        "create_harness failed after the OAuth provider was created, so no harness_id "
        "exists and destroy_harness can never reconstruct the provider name. The "
        f"manifest is the only route left, and it holds: {store.resources}"
    )


def test_the_same_row_is_recorded_exactly_once_on_the_success_path(monkeypatch):
    """Moving the record earlier must not double-record it. A duplicate is not
    fatal (both delete paths treat a missing provider as success) but it would
    put two identical rows in the customer-visible manifest."""
    store, out = _run(
        monkeypatch, create_harness=lambda *a, **kw: {"harness_id": "hstepfail-abc1234567", "arn": "arn:h"}
    )

    assert out.get("harness_id") == "hstepfail-abc1234567"
    assert _provider_rows(store) == [
        {
            "type": "oauth2_credential_provider",
            "name": PROVIDER_NAME,
            "region": "us-east-1",
            "created_by_deployment": True,
        }
    ]
    # The pre-existing orphan guards must be intact, not displaced by the move.
    types = [r.get("type") for r in store.resources]
    assert "harness" in types
    assert "iam_role" in types


def test_no_provider_row_when_there_is_no_gateway(monkeypatch):
    """Pins that the new record is inside the gateway branch. A harness with no
    gateway creates no provider, so a row would name a resource that does not
    exist -- and teardown would log a spurious failure for every such deploy."""
    from app.services import harness_deployer
    from app.step_handlers import harness_step

    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    store = _Store()
    monkeypatch.setattr(harness_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(harness_step.step_clients, "client", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(harness_deployer, "sanitize_harness_name", lambda n: "hstepfail")
    monkeypatch.setattr(harness_deployer, "get_shared_or_new_harness_role", lambda *a, **kw: "arn:aws:iam::1:role/r")

    def _must_not_run(*a, **kw):
        raise AssertionError("ensure_gateway_outbound_provider ran with no gateway attached")

    monkeypatch.setattr(harness_deployer, "ensure_gateway_outbound_provider", _must_not_run)
    monkeypatch.setattr(harness_deployer, "create_harness", lambda *a, **kw: {"harness_id": "h-1", "arn": "arn:h"})
    monkeypatch.setattr(
        harness_deployer,
        "wait_for_harness_ready",
        lambda *a, **kw: {"success": True, "status": "READY", "backing_runtime_id": "harness_hstepfail-AbCdEf1234"},
    )

    harness_step.handler({"deployment_id": "d2", "config": _CONFIG}, None)
    assert _provider_rows(store) == []


def test_destroy_harness_still_deletes_the_provider_by_reconstructed_name(monkeypatch):
    """Route 2 must keep working. The manifest row is a second route, not a
    replacement -- deployments recorded before this change have no provider row,
    so destroy_harness remains their only cleanup."""
    from app.services import harness_deployer

    deleted: list[str] = []

    class _Ctrl:
        harness_reads = 0

        def get_harness(self, harnessId):  # noqa: N803 - boto3 kwarg name
            self.harness_reads += 1
            if self.harness_reads > 1:
                raise RuntimeError("ResourceNotFoundException: harness is gone")
            return {
                "harness": {
                    "harnessId": harnessId,
                    "arn": (f"arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/{harnessId}"),
                }
            }

        def get_oauth2_credential_provider(self, name):
            return {
                "credentialProviderArn": (
                    f"arn:aws:bedrock-agentcore:us-east-1:123456789012:credential-provider/{name}"
                )
            }

        def list_tags_for_resource(self, resourceArn):  # noqa: N803 - boto3 kwarg name
            return {"tags": owner_tags("us-east-1")}

        def delete_harness(self, harnessId):  # noqa: N803 - boto3 kwarg name
            return {}

        def delete_oauth2_credential_provider(self, name):
            deleted.append(name)
            return {}

    monkeypatch.setattr(harness_deployer, "_create_agentcore_control_client", lambda region: _Ctrl())
    monkeypatch.setattr(harness_deployer, "_resolve_harness_identifier", lambda c, h: h)

    result = harness_deployer.destroy_harness("hstepfail-abc1234567", "us-east-1")
    assert result["success"] is True
    assert deleted == [PROVIDER_NAME]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
