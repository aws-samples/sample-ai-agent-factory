"""F-02: a legacy inline ``gateway_result.client_info.client_secret`` is never served.

``resolve_client_secret`` still honours an inline ``client_info["client_secret"]`` (rows written
before ``_mint_client_secret_ref``), so that row shape is supported storage. ``DeploymentState.
gateway_result`` is the whole gateway step result, ``GET /api/deploy/{id}`` and
``GET /api/deployments`` serve ``model_dump(exclude=INTERNAL_ONLY_STATE_FIELDS)``, and that
exclusion names no ``gateway_result`` leaf. So a legacy row served ``client_secret`` +
``token_endpoint`` + ``client_id`` + ``scope`` -- a complete client-credentials grant -- to any
``agent:read`` caller who knew the id (pre-tenancy rows are readable by design).

ARCC cnt_dwzZ05hLnqhYXQ: a secret carried in an API response is the anti-pattern; the fix is a
read-side scrub at the response boundary, so it no longer depends on whether such rows exist.
cnt_n8LpZcqYi2t3I2: the value belongs in Secrets Manager and is reached by reference, which is
exactly what the modern ``client_secret_ref`` row shape does -- and it must keep being served.

The storage is deliberately untouched: the legacy fallback inside ``resolve_client_secret`` still
reads the inline value, so existing gateways keep working.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from app.models.deployment_models import DeploymentState, DeploymentStatusEnum
from app.services.credential_scrub import is_write_only_credential_key, strip_credential_leaves

#: Obviously fake. The point is that this exact string never reaches a response body.
LEGACY_SECRET = "FAKE-legacy-inline-client-secret-NEVER-SERVE-0000"
TOKEN_ENDPOINT = "https://fake-pool.auth.us-east-1.amazoncognito.com/oauth2/token"
SECRET_REF = "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-gw-client-AbCdEf"


def _legacy_gateway_result() -> dict:
    """The pre-``_mint_client_secret_ref`` shape: the secret inline, next to everything a
    client-credentials grant needs."""
    return {
        "gateway_id": "legacygw-abc123",
        "gateway_url": "https://legacygw-abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        "client_info": {
            "client_id": "7abcdefghijklmnopqrstuvwxy",
            "client_secret": LEGACY_SECRET,
            "token_endpoint": TOKEN_ENDPOINT,
            "scope": "legacygw/invoke",
            "user_pool_id": "us-east-1_FAKEPOOL",
        },
        # Other leaves a step result can carry; the redaction is key-based and deep.
        "targets": [{"name": "github", "headers": {"X-Api-Key": "FAKE-target-key-1111", "Accept": "*/*"}}],
        "outbound": {"GITHUB_TOKEN": "ghp_FAKE2222", "github_token_ref": SECRET_REF},
    }


def _modern_gateway_result() -> dict:
    return {
        "gateway_id": "moderngw-def456",
        "gateway_url": "https://moderngw-def456.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        "client_info": {
            "client_id": "8abcdefghijklmnopqrstuvwxy",
            "client_secret_ref": SECRET_REF,
            "token_endpoint": TOKEN_ENDPOINT,
            "scope": "moderngw/invoke",
            "user_pool_id": "us-east-1_FAKEPOOL",
        },
    }


def _state(gateway_result: dict, *, user_id: str | None) -> DeploymentState:
    return DeploymentState(
        deployment_id="70e488d0-fb48-47bf-b25f-b01769b6c85f",
        workflow_id="93152cb2-9578-493e-a16a-c0b4438c4acb",
        user_id=user_id,
        status=DeploymentStatusEnum.SUCCEEDED,
        started_at=datetime(2026, 9, 20, 19, 12, tzinfo=UTC),
        gateway_result=gateway_result,
        gateway_url=gateway_result["gateway_url"],
    )


class _StubStore:
    def __init__(self, state: DeploymentState) -> None:
        self._state = state

    def get(self, deployment_id: str) -> DeploymentState | None:
        return self._state if deployment_id == self._state.deployment_id else None

    def query_by_user(self, user_id: str, status_filter=None) -> list[DeploymentState]:
        return [self._state] if user_id == self._state.user_id else []

    def query_by_workflow(self, workflow_id: str, status_filter=None) -> list[DeploymentState]:
        return [self._state] if workflow_id == self._state.workflow_id else []


class _Request:
    def __init__(self, sub: str | None) -> None:
        claims = {"sub": sub} if sub else {}
        self.scope = {"aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}}


def _serve(monkeypatch, state: DeploymentState, caller: str) -> tuple[dict, list[dict]]:
    from app import deployment_handler as dh

    monkeypatch.setattr(dh, "_get_state_store", lambda: _StubStore(state))
    monkeypatch.setattr(dh, "_maybe_promote_policy", lambda *a, **k: None, raising=False)
    one = asyncio.run(dh.handle_deploy_status(state.deployment_id, _Request(caller)))
    many = asyncio.run(dh.handle_list_deployments(_Request(caller)))
    return one, many


def _credential_leaves(value, path="") -> list[str]:
    """Every path whose LAST key names a credential value and holds a string."""
    found: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}/{k}"
            if is_write_only_credential_key(k) and isinstance(v, str):
                found.append(here)
            found.extend(_credential_leaves(v, here))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found.extend(_credential_leaves(item, f"{path}[{i}]"))
    return found


OWNER = "54381418-7021-708e-4f3b-30505a2b82ec"


class TestTheLegacyRowIsServedWithoutItsSecret:
    @pytest.mark.parametrize("user_id", [OWNER, None], ids=["owned", "pre-tenancy"])
    def test_the_status_route_never_carries_the_inline_secret(self, monkeypatch, user_id):
        one, _ = _serve(monkeypatch, _state(_legacy_gateway_result(), user_id=user_id), OWNER)
        body = json.dumps(one, default=str)
        assert LEGACY_SECRET not in body, body
        assert "FAKE-target-key-1111" not in body, body
        assert "ghp_FAKE2222" not in body, body
        assert _credential_leaves(one) == [], _credential_leaves(one)
        assert "client_secret" not in one["gateway_result"]["client_info"]

    def test_the_list_route_never_carries_the_inline_secret(self, monkeypatch):
        _, many = _serve(monkeypatch, _state(_legacy_gateway_result(), user_id=OWNER), OWNER)
        assert len(many) == 1
        body = json.dumps(many, default=str)
        assert LEGACY_SECRET not in body, body
        assert _credential_leaves(many) == []

    def test_everything_that_is_not_a_secret_survives(self, monkeypatch):
        """Vacuity guard: an empty ``gateway_result`` would pass the assertions above. The
        client id, token endpoint, scope, pool, gateway url and the REFERENCE all stay."""
        one, _ = _serve(monkeypatch, _state(_legacy_gateway_result(), user_id=OWNER), OWNER)
        info = one["gateway_result"]["client_info"]
        assert info["client_id"] == "7abcdefghijklmnopqrstuvwxy"
        assert info["token_endpoint"] == TOKEN_ENDPOINT
        assert info["scope"] == "legacygw/invoke"
        assert info["user_pool_id"] == "us-east-1_FAKEPOOL"
        assert one["gateway_result"]["gateway_id"] == "legacygw-abc123"
        assert one["gateway_url"] == _legacy_gateway_result()["gateway_url"]
        assert one["gateway_result"]["targets"] == [{"name": "github", "headers": {"Accept": "*/*"}}]
        assert one["gateway_result"]["outbound"] == {"github_token_ref": SECRET_REF}

    def test_the_stored_row_still_holds_the_secret_for_the_legacy_fallback(self, monkeypatch):
        """The other direction. ``resolve_client_secret``'s source 1 is the inline value, and
        gateways deployed before the reference existed only work because of it. The fix is a
        read-side scrub of the RESPONSE, not a migration of the row."""
        from app.services.gateway_deployer import resolve_client_secret

        state = _state(_legacy_gateway_result(), user_id=OWNER)
        _serve(monkeypatch, state, OWNER)
        assert state.gateway_result["client_info"]["client_secret"] == LEGACY_SECRET
        assert resolve_client_secret(state.gateway_result["client_info"]) == LEGACY_SECRET


class TestTheModernRowIsServedUnchanged:
    def test_a_reference_shaped_row_is_byte_for_byte_what_the_model_dumps(self, monkeypatch):
        from app.models.deployment_models import INTERNAL_ONLY_STATE_FIELDS

        state = _state(_modern_gateway_result(), user_id=OWNER)
        one, many = _serve(monkeypatch, state, OWNER)
        expected = state.model_dump(mode="json", exclude=INTERNAL_ONLY_STATE_FIELDS)
        assert one == expected
        assert many == [expected]
        assert one["gateway_result"]["client_info"]["client_secret_ref"] == SECRET_REF


class TestTheRedactionHelperItself:
    def test_it_is_a_deep_copy_that_removes_only_credential_leaves(self):
        src = _legacy_gateway_result()
        out = strip_credential_leaves(src)
        assert out is not src
        assert src["client_info"]["client_secret"] == LEGACY_SECRET, "the input is never mutated"
        assert "client_secret" not in out["client_info"]
        assert out["client_info"]["client_secret_ref"] if "client_secret_ref" in out["client_info"] else True
        assert out["targets"][0]["headers"] == {"Accept": "*/*"}

    def test_it_has_no_container_semantics(self):
        """Unlike the persistence scrub, a served record's STRUCTURE is the caller's: a
        ``credentials`` object keeps its non-secret leaves whatever they are called."""
        out = strip_credential_leaves(
            {"credentials": {"status": "READY", "provider_name": "gh", "client_secret": LEGACY_SECRET}}
        )
        assert out == {"credentials": {"status": "READY", "provider_name": "gh"}}

    def test_non_dict_values_pass_through(self):
        assert strip_credential_leaves(None) is None
        assert strip_credential_leaves("x") == "x"
        assert strip_credential_leaves([1, {"token": "FAKE"}]) == [1, {}]
