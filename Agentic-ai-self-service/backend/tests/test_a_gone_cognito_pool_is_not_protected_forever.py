"""A Cognito pool that is already gone is deleted, not protected forever.

Measured live 2026-10-01: nine older mcp-server-gateway-target versions stayed
``delete_retained`` on every retry with "Resources retained by deletion-authority policy:
cognito_user_pool". An earlier attempt had deleted each version's own pool; the retry could not
prove ownership of a pool that no longer exists, reported "skipped (protected)", and the
teardown counts that as a retention. A pool that does not exist takes its app clients and
resource servers with it, so those rows now end "already gone". Any other describe failure
still raises, so the teardown is retried instead of guessed, and an existing pool whose
ownership cannot be proven is still protected.
"""

from __future__ import annotations

import app.deployment_handler as dh
import pytest
from botocore.exceptions import ClientError

POOL_ID = "us-east-1_GonePool1"


def _error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} raised by the test"}}, "DescribeUserPool")


class _Cognito:
    def __init__(self, describe_error: Exception | None = None) -> None:
        self.describe_error = describe_error
        self.calls: list[tuple[str, str]] = []

    def describe_user_pool(self, **kwargs):
        self.calls.append(("describe_user_pool", kwargs["UserPoolId"]))
        if self.describe_error is not None:
            raise self.describe_error
        return {"UserPool": {"Id": kwargs["UserPoolId"], "UserPoolTags": {}}}

    def delete_user_pool(self, **kwargs):
        self.calls.append(("delete_user_pool", kwargs["UserPoolId"]))

    def delete_user_pool_client(self, **kwargs):
        self.calls.append(("delete_user_pool_client", kwargs["ClientId"]))

    def delete_resource_server(self, **kwargs):
        self.calls.append(("delete_resource_server", kwargs["Identifier"]))

    def list_user_pool_clients(self, **_kwargs):
        return {"UserPoolClients": []}


class _Session:
    def __init__(self, cognito: _Cognito) -> None:
        self._cognito = cognito

    def client(self, service: str, **_kwargs):
        assert service == "cognito-idp"
        return self._cognito


@pytest.fixture(autouse=True)
def _not_the_shared_pool(monkeypatch):
    monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_ID", raising=False)
    monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_DOMAIN", raising=False)


def _delete(row: dict, cognito: _Cognito, monkeypatch) -> str:
    monkeypatch.setattr(dh, "_shared_pool_children_client", lambda *_args: cognito)
    return dh._delete_managed_resource(row, "us-east-1", deployment_id="dep-1", target_session=_Session(cognito))


def _counts_as_deleted(message: str) -> bool:
    # The manifest loop reads these markers as a retention; anything else is a delete.
    return not any(marker in message.lower() for marker in ("skipped", "protected", "left in place"))


ROWS = {
    "pool": {"type": "cognito_user_pool", "id": POOL_ID, "region": "us-east-1", "created_by_deployment": True},
    "client": {
        "type": "cognito_app_client",
        "id": "client-1",
        "pool_id": POOL_ID,
        "region": "us-east-1",
        "created_by_deployment": True,
    },
    "resource_server": {
        "type": "cognito_resource_server",
        "id": "agentcore-mcp-server-gateway",
        "pool_id": POOL_ID,
        "region": "us-east-1",
        "created_by_deployment": True,
    },
}


@pytest.mark.parametrize("row_kind", sorted(ROWS))
def test_a_row_whose_pool_is_gone_is_already_gone(row_kind, monkeypatch):
    cognito = _Cognito(describe_error=_error("ResourceNotFoundException"))

    message = _delete(ROWS[row_kind], cognito, monkeypatch)

    assert "already gone" in message
    assert _counts_as_deleted(message), message
    assert [call for call in cognito.calls if call[0].startswith("delete_")] == []


@pytest.mark.parametrize("row_kind", sorted(ROWS))
@pytest.mark.parametrize("code", ["AccessDeniedException", "ThrottlingException", "InternalErrorException"])
def test_any_other_describe_failure_is_raised_for_a_retry(row_kind, code, monkeypatch):
    cognito = _Cognito(describe_error=_error(code))

    with pytest.raises(ClientError):
        _delete(ROWS[row_kind], cognito, monkeypatch)


@pytest.mark.parametrize("row_kind", sorted(ROWS))
def test_an_existing_pool_we_cannot_prove_is_ours_is_still_protected(row_kind, monkeypatch):
    cognito = _Cognito()  # exists, but carries none of this stack's owner tags

    message = _delete(ROWS[row_kind], cognito, monkeypatch)

    assert "could not be proven" in message
    assert not _counts_as_deleted(message)
    assert [call for call in cognito.calls if call[0].startswith("delete_")] == []


# The Step Functions abort path (status_update_step._cleanup_resource) had the same three arms.


def _abort_cleanup(row: dict, cognito: _Cognito, monkeypatch):
    from app.step_handlers import status_update_step as sus

    monkeypatch.setattr(sus, "_shared_pool_children_client", lambda *_args: cognito)
    monkeypatch.setattr(sus.step_clients, "client", lambda *_args, **_kwargs: cognito)
    return sus._cleanup_resource(row, "us-east-1", {"deployment_id": "dep-1"})


@pytest.mark.parametrize("row_kind", sorted(ROWS))
def test_the_abort_path_counts_a_row_whose_pool_is_gone_as_cleaned(row_kind, monkeypatch):
    cognito = _Cognito(describe_error=_error("ResourceNotFoundException"))

    assert _abort_cleanup(ROWS[row_kind], cognito, monkeypatch) is None
    assert [call for call in cognito.calls if call[0].startswith("delete_")] == []


@pytest.mark.parametrize("row_kind", sorted(ROWS))
def test_the_abort_path_raises_any_other_describe_failure(row_kind, monkeypatch):
    cognito = _Cognito(describe_error=_error("AccessDeniedException"))

    with pytest.raises(ClientError):
        _abort_cleanup(ROWS[row_kind], cognito, monkeypatch)


@pytest.mark.parametrize("row_kind", sorted(ROWS))
def test_the_abort_path_still_retains_an_existing_pool_we_cannot_prove(row_kind, monkeypatch):
    from app.step_handlers import status_update_step as sus

    cognito = _Cognito()

    with pytest.raises(sus._ResourceRetained):
        _abort_cleanup(ROWS[row_kind], cognito, monkeypatch)
    assert [call for call in cognito.calls if call[0].startswith("delete_")] == []
