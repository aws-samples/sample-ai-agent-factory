"""A secret whose ARN never reached anyone is still named, found, and deleted.

Two ways a live secret escapes the manifest. The CreateSecret response is lost (a read
timeout after the service committed), so the caller never learns the ARN. Or the step
is killed between the create and its manifest row (a Lambda timeout, an OOM), and no
handler runs at all. Before this, both left a live credential no row named, and the
teardown reported the deployment clean.

Tag discovery (ListSecrets) is the backstop, not the proof: the service documents that
ListSecrets may not reflect changes from the last few minutes, so an immediate empty
result says nothing. The proof is the pre-create journal: the exact name is a durable
manifest row BEFORE the create, and teardown's DescribeSecret on that exact name either
finds it (and deletes it, after re-proving its tags) or proves it absent.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import app.deployment_handler as dh
import pytest
from app.services import gateway_deployer as gd
from app.services import step_clients
from app.services.deployment_state_store import collapse_secret_intent_rows, manifest_resource_key
from app.step_handlers import status_update_step as sus
from botocore.exceptions import ClientError

from tests.test_deployment_bound_secrets import (
    ACCOUNT,
    DEPLOYMENT,
    OWNER,
    REGION,
    _Secrets,
    _Session,
    _Store,
    _tags,
)

RAW = "notarealconnectorkey-0000"  # pragma: allowlist secret

# Every test here runs the real tag discovery against its own fake (see conftest).
pytestmark = pytest.mark.real_secret_discovery


class _Killed(BaseException):
    """What a hard kill looks like to the code under test: nothing after it runs,
    and no ``except Exception`` handler sees it."""


class _SM(_Secrets):
    """Adds the two faults and a ListSecrets with the service's real semantics."""

    def __init__(self, *, after_create: BaseException | None = None, list_lag: int = 0):
        super().__init__()
        self.after_create = after_create
        # ListSecrets omits a secret for this many calls after its create.
        self.list_lag = list_lag
        self._unlisted: dict[str, int] = {}
        self.list_calls = 0

    def _find(self, secret_id: str) -> dict:
        try:
            return super()._find(secret_id)
        except Exception:
            # What the service raises, so the product's own is_error check is what runs.
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "Secrets Manager can't find it"}},
                "DescribeSecret",
            ) from None

    def create_secret(self, **kwargs):
        out = super().create_secret(**kwargs)
        self._unlisted[out["ARN"]] = self.list_lag
        if self.after_create is not None:
            raise self.after_create
        return out

    def delete_secret(self, **kwargs):
        super().delete_secret(**kwargs)
        item = self._find(kwargs["SecretId"])
        self.secrets.pop(item["ARN"])

    def list_secrets(self, *, Filters, MaxResults, NextToken=""):  # noqa: N803
        self.list_calls += 1
        f = {x["Key"]: x["Values"] for x in Filters}

        def _match(item) -> bool:
            tags = item["Tags"]
            return (
                any(item["Name"].startswith(v) for v in f.get("name", [""]))
                and any(t["Key"].startswith(v) for t in tags for v in f.get("tag-key", [""]))
                # tag-value matches the value under ANY key, by prefix.
                and any(t["Value"].startswith(v) for t in tags for v in f.get("tag-value", [""]))
            )

        visible = []
        for arn, item in self.secrets.items():
            if self._unlisted.get(arn, 0) > 0:
                self._unlisted[arn] -= 1
                continue
            if _match(item):
                visible.append({"ARN": arn, "Name": item["Name"], "Tags": item["Tags"]})
        start = int(NextToken or 0)
        page = visible[start : start + 1]  # page size 1 exercises NextToken
        out = {"SecretList": page}
        if start + 1 < len(visible):
            out["NextToken"] = str(start + 1)
        return out

    def exposed(self) -> str:
        """Every string the fake handed back or recorded, minus the stored values."""
        return json.dumps([self.described, self.deleted, list(self.secrets)], default=str)


@pytest.fixture(autouse=True)
def _stack_identity(monkeypatch):
    monkeypatch.setenv("PROJECT_NAME", "secret-tests")
    monkeypatch.setenv("ENVIRONMENT", "unit")


def _connector() -> list[dict]:
    return [{"connector_id": "github", "auth_method": "api_key", "secret_value": RAW}]


def _stage(monkeypatch, sm, store):
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: _Session(sm))
    return dh._prepare_deployment_credentials(
        gateway_config=None,
        connectors=_connector(),
        external_mcp_servers=None,
        deployment_id=DEPLOYMENT,
        owner_sub=OWNER,
        target_account_id=None,
        target_region=REGION,
        target_role_arn=None,
        store=store,
    )


def _auto_cleanup(monkeypatch, sm, rows: list[dict], *, unrelated: bool = True, messages=None) -> list[str]:
    """The real status_update cleanup over *rows*, plus one unrelated resource so the
    manifest is never empty (an empty one is retained for its own reasons)."""
    store = MagicMock()
    extra = [{"type": "lambda", "name": "Unrelated", "region": REGION, "created_by_deployment": True}]
    store.get.return_value.model_dump.return_value = {
        "deployment_id": DEPLOYMENT,
        "created_resources": [*(extra if unrelated else []), *rows],
    }
    store.has_other_live_resource_reference.return_value = False
    monkeypatch.setattr(step_clients, "client", lambda event, service, **kw: sm)
    real = sus._cleanup_resource

    def _only_secrets_are_real(res, region, event):
        if res.get("type") == "secret":
            return real(res, region, event)
        return None

    with patch.object(sus, "_cleanup_resource", side_effect=_only_secrets_are_real):
        sus._auto_cleanup_on_failure(store, DEPLOYMENT, {"deployment_id": DEPLOYMENT})
    if messages is not None:
        messages.extend(c.args[2] for c in store.update_delete_status.call_args_list if len(c.args) > 2)
    return [c.args[1] for c in store.update_delete_status.call_args_list]


def _no_plaintext(*surfaces) -> None:
    for s in surfaces:
        assert RAW not in json.dumps(s, default=str)


# --- the journal ---------------------------------------------------------------------


def test_every_secret_is_named_in_the_manifest_before_it_exists(monkeypatch):
    # The happy path: the create returns, and the manifest holds the journal row AND
    # the ARN row, which canonicalize to one resource.
    sm, store = _SM(), _Store()
    _gateway, _connectors, _mcp, recorded = _stage(monkeypatch, sm, store)
    (arn,) = recorded
    name = arn.partition(":secret:")[2][:-7]
    assert [r["id"] for r in store.rows] == [name, arn]
    assert all(r["type"] == "secret" and r["created_by_deployment"] is True for r in store.rows)
    _no_plaintext(store.rows, recorded)


@pytest.mark.parametrize("target_account", [None, "999999999999"])
def test_a_journal_row_and_its_arn_row_are_one_resource(monkeypatch, target_account):
    """Two rows, one secret: one manifest key, one delete attempt, a 1/1 count. Same-
    account deployments matter most here: the name row carries no account, and the
    ARN row's account comes out of the ARN."""
    sm, store = _SM(), _Store()
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: _Session(sm))
    *_, recorded = dh._prepare_deployment_credentials(
        gateway_config=None,
        connectors=_connector(),
        external_mcp_servers=None,
        deployment_id=DEPLOYMENT,
        owner_sub=OWNER,
        target_account_id=target_account,
        target_region=REGION,
        target_role_arn=f"arn:aws:iam::{target_account}:role/Deploy" if target_account else None,
        store=store,
    )
    (arn,) = recorded
    name, arn_row = store.rows
    assert arn_row["id"] == arn and not name["id"].startswith("arn:")
    default = {"default_account": target_account, "default_region": REGION}
    assert collapse_secret_intent_rows(store.rows, **default) == [arn_row]
    assert len({manifest_resource_key(r, **default) for r in collapse_secret_intent_rows(store.rows, **default)}) == 1

    messages: list[str] = []
    statuses = _auto_cleanup(monkeypatch, sm, store.rows, unrelated=False, messages=messages)
    assert [d["SecretId"] for d in sm.deleted] == [arn]
    assert sm.secrets == {}
    assert statuses[-1] == "deleted"
    assert "handled 1/1 resources" in messages[-1]


def test_a_journal_row_without_its_arn_row_is_kept():
    """The lost-response case: the name row is the ONLY record, so it must survive."""
    row = {"type": "secret", "id": "agentcore-connector/o/lost000001", "region": REGION, "created_by_deployment": True}
    other = {**row, "id": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/o/other00001-AbCdEf"}
    assert collapse_secret_intent_rows([row, other]) == [row, other]
    # Same name, another region: not its partner.
    far = {**row, "id": f"arn:aws:secretsmanager:us-west-2:{ACCOUNT}:secret:{row['id']}-AbCdEf", "region": "us-west-2"}
    assert collapse_secret_intent_rows([row, far]) == [row, far]


def test_the_delete_path_makes_one_attempt_for_a_journaled_secret(monkeypatch):
    """deployment_handler's teardown, over the same [name, ARN] pair, same-account."""
    sm, store = _SM(), _Store()
    _stage(monkeypatch, sm, store)
    name_row, arn_row = store.rows
    record = {"deployment_id": DEPLOYMENT, "user_id": OWNER, "created_resources": [name_row, arn_row]}

    class _StateStore:
        _table = object()

        def get(self, _deployment_id):
            return None

        def has_other_live_resource_reference(self, *_a, **_k):
            return False

        def reset_manifest_reference_cache(self, _deployment_id):
            return None

    attempts: list[dict] = []
    monkeypatch.setattr(dh, "_get_state_store", lambda: _StateStore())
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, runtime_id: record)
    monkeypatch.setattr(
        dh, "_delete_managed_resource", lambda res, *_a, **_k: attempts.append(res) or f"{res['type']} deleted"
    )
    monkeypatch.setattr(dh, "destroy_runtime", lambda *_a, **_k: {"success": True})
    dh._run_delete_cleanup(DEPLOYMENT, OWNER)
    assert [r["id"] for r in attempts if r["type"] == "secret"] == [arn_row["id"]]


def test_a_journal_that_cannot_write_stops_the_create(monkeypatch):
    sm = _SM()
    with pytest.raises(RuntimeError, match="durability unavailable"):
        _stage(monkeypatch, sm, _Store(fail_after=0))
    assert sm.created == [], "a secret must never exist before a durable record names it"


# --- a lost CreateSecret response -----------------------------------------------------


def test_a_lost_create_response_is_torn_down_by_the_name_journaled_before_it(monkeypatch):
    sm, store = _SM(after_create=TimeoutError("read timeout after the create committed")), _Store()
    with pytest.raises(TimeoutError) as exc:
        with gd.secret_intent_journal(gd.manifest_secret_journal(store, DEPLOYMENT)):
            gd._put_connector_secret(REGION, OWNER, {"apiKey": RAW}, DEPLOYMENT, secrets_client=sm)
    (row,) = store.rows
    (live,) = sm.secrets.values()
    assert row["id"] == live["Name"] == getattr(exc.value, gd.SECRET_CANDIDATE_ATTR)
    _no_plaintext(store.rows, str(exc.value), getattr(exc.value, "__notes__", []))

    # Nothing but the journal row survives the lost response, and it is enough.
    assert "deleted" == _auto_cleanup(monkeypatch, sm, store.rows)[-1]
    assert sm.secrets == {}
    _no_plaintext(sm.exposed())


# --- a hard kill between the create and the row ------------------------------------


def test_a_hard_kill_after_the_create_is_cleaned_even_while_listsecrets_lags(monkeypatch):
    """ListSecrets never shows the secret during this teardown. Only the journal
    can name it; before the journal, this teardown reported the deployment deleted
    while the credential was still live."""
    sm, store = _SM(after_create=_Killed(), list_lag=10**6), _Store()
    with pytest.raises(_Killed):
        _stage(monkeypatch, sm, store)
    assert len(sm.secrets) == 1, "precondition: the create committed"
    assert all(r["id"].startswith("agentcore-connector/") and ":secret:" not in r["id"] for r in store.rows)

    statuses = _auto_cleanup(monkeypatch, sm, store.rows)
    assert sm.secrets == {}
    assert statuses[-1] == "deleted"
    _no_plaintext(store.rows, sm.exposed())


def test_a_journaled_name_that_was_never_created_is_proven_absent_not_deleted(monkeypatch):
    # The kill came before the create: the journal row names nothing. The exact-name
    # read proves absence, so this is clean without a single delete.
    sm = _SM()
    name = "agentcore-connector/someone/never-created0"
    statuses = _auto_cleanup(
        monkeypatch, sm, [{"type": "secret", "id": name, "region": REGION, "created_by_deployment": True}]
    )
    assert sm.deleted == []
    assert name in sm.described
    assert statuses[-1] == "deleted"


# --- discovery: the backstop -----------------------------------------------------------


def test_discovery_finds_an_unjournaled_secret_and_only_this_deployments(monkeypatch):
    """A record written before the journal existed: no row, the secret already listed."""
    sm = _SM()
    ours = sm.add("agentcore-connector/o/ours000001", {"apiKey": RAW}, _tags(OWNER, DEPLOYMENT))
    # tag-value is a prefix match under any key: both of these match the filter and
    # must still be left alone.
    other_dep = sm.add("agentcore-connector/o/other00001", {"apiKey": RAW}, _tags(OWNER, f"{DEPLOYMENT}-2"))
    tagged_elsewhere = sm.add(
        "agentcore-connector/o/elsewhere1",
        {"apiKey": RAW},
        _tags(OWNER, "dep-x") + [{"Key": "Note", "Value": DEPLOYMENT}],
    )
    statuses = _auto_cleanup(monkeypatch, sm, [])
    assert set(sm.secrets) == {other_dep, tagged_elsewhere}
    assert [d["SecretId"] for d in sm.deleted] == [ours]
    assert statuses[-1] == "deleted"
    _no_plaintext(sm.exposed())


def test_discovery_skips_what_the_manifest_already_names(monkeypatch):
    sm = _SM()
    arn = sm.add("agentcore-connector/o/named00001", {"apiKey": RAW}, _tags(OWNER, DEPLOYMENT))
    rows, failures = gd.unrecorded_deployment_secret_rows(
        deployment_id=DEPLOYMENT,
        recorded_rows=[{"type": "secret", "id": "agentcore-connector/o/named00001", "region": REGION}],
        region=REGION,
        secrets_client_for=lambda _r: sm,
    )
    assert (rows, failures) == ([], [])
    assert arn in sm.secrets


def test_a_discovery_failure_is_never_reported_clean(monkeypatch):
    class _Denied(_SM):
        def list_secrets(self, **_kw):
            raise RuntimeError("AccessDeniedException: not authorized to perform ListSecrets")

    statuses = _auto_cleanup(monkeypatch, _Denied(), [])
    assert statuses[-1] == "delete_retained"


def test_the_delete_path_discovers_too(monkeypatch):
    """deployment_handler's teardown uses the same helper, with the same rows."""
    sm = _SM()
    ours = sm.add("agentcore-connector/o/ours000002", {"apiKey": RAW}, _tags(OWNER, DEPLOYMENT))
    rows, failures = gd.unrecorded_deployment_secret_rows(
        deployment_id=DEPLOYMENT, recorded_rows=[], region=REGION, secrets_client_for=lambda _r: sm
    )
    assert failures == []
    assert [r["id"] for r in rows] == [ours]
    msg = dh._delete_managed_resource(rows[0], REGION, deployment_id=DEPLOYMENT, target_session=_Session(sm))
    assert "deleted" in msg and ours not in sm.secrets
    assert ACCOUNT in ours  # the row carries the ARN discovery returned, not a guess


# --- discovery pagination fails closed ---------------------------------------------------


def _paged(*pages):
    sm = MagicMock()
    sm.list_secrets.side_effect = list(pages)
    return sm


def _entry(n: int, dep: str = DEPLOYMENT) -> dict:
    arn = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/o/s{n:010d}-AbCdEf"
    return {"ARN": arn, "Tags": [{"Key": "DeploymentId", "Value": dep}]}


def _discover(sm):
    return gd.discover_deployment_bound_secrets(deployment_id=DEPLOYMENT, secrets_client=sm)


def test_discovery_with_no_token_reads_one_page():
    sm = _paged({"SecretList": [_entry(1)]})
    assert _discover(sm) == [_entry(1)["ARN"]]
    assert sm.list_secrets.call_count == 1


def test_discovery_follows_every_page_and_passes_the_token():
    sm = _paged(
        {"SecretList": [_entry(1)], "NextToken": "t1"},
        {"SecretList": [_entry(2, "dep-other")], "NextToken": "t2"},
        {"SecretList": [_entry(3)]},
    )
    assert _discover(sm) == [_entry(1)["ARN"], _entry(3)["ARN"]]
    assert [c.kwargs.get("NextToken") for c in sm.list_secrets.call_args_list] == [None, "t1", "t2"]


@pytest.mark.parametrize("token", [MagicMock(), object(), 7, ["t"]])
def test_a_non_string_token_fails_closed_after_one_call(token):
    sm = _paged({"SecretList": [], "NextToken": token})
    with pytest.raises(RuntimeError, match="non-string NextToken"):
        _discover(sm)
    assert sm.list_secrets.call_count == 1


def test_an_unconfigured_mock_client_fails_closed_after_one_call():
    """The incident: a bare MagicMock returns a MagicMock page with a truthy token."""
    sm = MagicMock()
    with pytest.raises(RuntimeError, match="unexpected response shape"):
        _discover(sm)
    assert sm.list_secrets.call_count == 1


def test_a_repeated_token_fails_closed():
    sm = _paged(
        {"SecretList": [], "NextToken": "same"},
        {"SecretList": [], "NextToken": "same"},
    )
    with pytest.raises(RuntimeError, match="repeated a NextToken"):
        _discover(sm)
    assert sm.list_secrets.call_count == 2


def test_a_token_that_never_ends_stops_at_the_page_budget():
    sm = MagicMock()
    sm.list_secrets.side_effect = lambda **kw: {"SecretList": [], "NextToken": f"t{sm.list_secrets.call_count}"}
    with pytest.raises(RuntimeError, match="did not finish"):
        _discover(sm)
    assert sm.list_secrets.call_count == gd._DISCOVERY_PAGE_BUDGET


@pytest.mark.parametrize("bad", [MagicMock(), {"SecretList": [], "NextToken": 7}])
def test_a_malformed_listing_is_never_reported_clean(monkeypatch, bad):
    class _Bad(_SM):
        def list_secrets(self, **_kw):
            return bad

    assert _auto_cleanup(monkeypatch, _Bad(), [])[-1] == "delete_retained"
