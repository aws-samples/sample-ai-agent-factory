"""F-81c — a wrong runtime name must cause ZERO trigger mutations.

WHAT THIS PATH DOES. ``destroy_runtime`` has only the canonical AgentCore id, but the TriggersTable
is keyed by the tenant's friendly ``runtime_name`` and that partition is NOT owner-scoped. Every row
it finds under that key costs an EventBridge Scheduler ``delete_schedule``, an EventBridge
``remove_targets``/``delete_rule``, a Lambda ``delete_function_url_config``, a Secrets Manager
delete of the webhook HMAC secret, and finally the DDB row itself
(services/runtime_deployer.py:1494-1560). So the name is a destructive selector, and a name that is
merely PLAUSIBLE is a cross-tenant delete.

WHAT USED TO PRODUCE THE NAME. ``_resolve_runtime_name_for_cleanup`` ended in
``re.sub(r"-[A-Za-z0-9]{10}$", "", canonical_id)`` -- reached on a scan miss AND from inside the
``except``, so a throttle or an expired credential produced a name too. The value was also usually
wrong, because the strip leaves the version suffix on (``myagent_1a2b3c4d``): the realistic outcomes
were "delete nothing" or "delete the triggers of the tenant whose friendly name happens to be that
string". The manifest teardown fed the same class of value in from the other side --
``_delete_managed_resource`` passed ``res["name"]`` straight through as ``runtime_name``, and the
writers record ``friendly_runtime_name or runtime_id`` / ``or runtime_name``, so a canvas with no
friendly name puts a canonical id or an ``<friendly>_<8hex>`` string in that field.

The tests below are therefore about ABSENCE OF SIDE EFFECTS, which is the only thing that
distinguishes "refused to guess" from "guessed something that happened to match nothing". The
client factory raises on the first scheduler/events/lambda/secretsmanager client, and the trigger
store raises if it is enumerated at all, so a guess cannot pass quietly. Two controls keep the
suite from passing vacuously: an exact mapping still resolves, and a proven name still deletes its
triggers.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import boto3
import pytest

sys.path.insert(0, "src")

moto = pytest.importorskip("moto")
from app.services import runtime_deployer as mod  # noqa: E402
from app.services.resource_ownership import owner_tag_list  # noqa: E402
from moto import mock_aws  # noqa: E402

REGION = "us-east-1"
FRIENDLY = "orders_bot"
#: The live AgentCore runtime NAME (``friendly[:39]_<8hex>``) and the canonical ID built from it.
#: They are deliberately spelled as name + suffix, because the whole point of the second matching
#: tier is that the id is NOT the name.
LIVE_NAME = "orders_bot_1a2b3c4d"
CANONICAL = f"{LIVE_NAME}-AbCdEfGhIj"

#: Every service whose client existing only to DELETE a trigger-provisioned resource. A request for
#: one of these is itself the failure: the assertion has to fire before the API call, because a
#: fake that answered would make the test pass for a reason unrelated to what it claims.
DESTRUCTIVE_SERVICES = ("scheduler", "events", "lambda", "secretsmanager")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Permissive:
    """Answers any AWS call with an empty dict.

    destroy_runtime does a lot of unrelated best-effort cleanup (log groups, dashboards, eval
    configs) on the way to the trigger block. None of it is what these tests measure, and all of it
    must stay reachable -- an exception here would short-circuit the function BEFORE the trigger
    block and make every assertion below vacuously true.
    """

    def __getattr__(self, _name: str):
        def _call(**_kwargs):
            return {}

        return _call


class _OwnedEvents(_Permissive):
    """An EventBridge rule carrying the trigger row's exact ownership tags."""

    def describe_rule(self, *, Name: str, EventBusName: str):  # noqa: N803
        assert EventBusName == "default"
        return {"Arn": (f"arn:aws:events:{REGION}:123456789012:rule/{Name}")}

    def list_tags_for_resource(self, *, ResourceARN: str):  # noqa: N803
        rule_name = ResourceARN.rsplit("/", 1)[-1]
        trigger_id = rule_name.removeprefix("agentcore-trigger-")
        return {
            "Tags": [
                {"Key": "ManagedBy", "Value": "agentcore-flows"},
                {"Key": "Purpose", "Value": "runtime-trigger"},
                {
                    "Key": "AgentCoreStack",
                    "Value": "unit-tests-local-us-east-1",
                },
                {"Key": "TriggerId", "Value": trigger_id},
                {"Key": "RuntimeName", "Value": FRIENDLY},
            ]
        }


class _NoSuchEntity(Exception):
    pass


class _IamExceptions:
    NoSuchEntityException = _NoSuchEntity


class _Iam(_Permissive):
    exceptions = _IamExceptions()

    def get_role(self, **_kwargs):
        raise _NoSuchEntity("no role here")

    def delete_role(self, **_kwargs):
        raise _NoSuchEntity("no role here")


class _LiveRuntime:
    """A runtime that exists and carries THIS deployment's owner tags.

    Ownership matters: the trigger block is skipped outright for an unowned or already-absent
    runtime (``raise ResourceDeletionRefused`` at runtime_deployer.py:1511), which is another way to
    reach "zero mutations" for the wrong reason. These tests must get past that gate.
    """

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def get_agent_runtime(self, *, agentRuntimeId: str):  # noqa: N803 - boto3 casing
        if agentRuntimeId in self.deleted:
            # F-08: destroy_runtime now confirms the delete by re-reading; a deleted runtime is gone.
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetAgentRuntime")
        return {
            "agentRuntimeId": agentRuntimeId,
            "agentRuntimeName": LIVE_NAME,
            "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{agentRuntimeId}",
            "roleArn": "",
        }

    def list_tags_for_resource(self, *, resourceArn: str):  # noqa: N803 - boto3 casing
        return {"tags": {t["Key"]: t["Value"] for t in owner_tag_list(REGION)}}

    def delete_agent_runtime(self, *, agentRuntimeId: str):  # noqa: N803 - boto3 casing
        self.deleted.append(agentRuntimeId)
        return {}

    def list_online_evaluation_configs(self, **_kwargs):
        return {"onlineEvaluationConfigs": []}


class _Trigger:
    """One trigger row with every provisioned side resource set.

    ``target_runtime_arn`` defaults to the ARN of ``CANONICAL`` because that is what
    ``routers/triggers._resolve_owned_runtime`` derives server-side, and F-81f authorizes each row
    against it individually. A row for a different runtime is built by passing ``target``.
    """

    def __init__(self, runtime_name: str, *, target: str | None = None) -> None:
        self.runtime_name = runtime_name
        self.trigger_id = "trg-1"
        self.owner_sub = "sub-alice"
        self.type = "cron"
        self.status = "active"
        self.scheduler_name = "sched-1"
        self.eventbridge_rule_arn = f"arn:aws:events:{REGION}:123456789012:rule/agentcore-trigger-{self.trigger_id}"
        self.function_name = "fn-1"
        self.function_url = None
        self.webhook_secret_ref = None
        self.delete_token = None
        self.target_runtime_arn = (
            target if target is not None else f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{CANONICAL}"
        )


class _SpyTriggerStore:
    def __init__(self, rows: list[_Trigger] | None = None) -> None:
        self.enumerated: list[str] = []
        self.consistency: list[bool] = []
        self.get_consistency: list[bool] = []
        self.deleted: list[tuple[str, str]] = []
        self.claimed: list[tuple[str, str]] = []
        self._rows = rows or []

    def list_for_runtime(self, runtime_name: str, *, consistent: bool = False):
        self.enumerated.append(runtime_name)
        self.consistency.append(consistent)
        return [r for r in self._rows if r.runtime_name == runtime_name]

    def delete(self, runtime_name: str, trigger_id: str):
        self.deleted.append((runtime_name, trigger_id))

    def get(
        self,
        runtime_name: str,
        trigger_id: str,
        *,
        consistent: bool = False,
    ):
        self.get_consistency.append(consistent)
        return next(
            (row for row in self._rows if row.runtime_name == runtime_name and row.trigger_id == trigger_id),
            None,
        )

    def claim_delete(
        self,
        *,
        runtime_name: str,
        trigger_id: str,
        owner_sub: str,
        delete_token: str,
        now: int | None = None,
    ):
        del now
        row = self.get(runtime_name, trigger_id, consistent=True)
        if row is None or row.owner_sub != owner_sub:
            return None
        row.status = "deleting"
        row.delete_token = delete_token
        self.claimed.append((runtime_name, trigger_id))
        return row

    def delete_claimed(
        self,
        *,
        runtime_name: str,
        trigger_id: str,
        delete_token: str,
    ) -> bool:
        row = self.get(runtime_name, trigger_id, consistent=True)
        if row is None or row.status != "deleting" or row.delete_token != delete_token:
            return False
        self.deleted.append((runtime_name, trigger_id))
        self._rows.remove(row)
        return True


@pytest.fixture
def versions_table(monkeypatch: pytest.MonkeyPatch) -> Iterator:
    """The real AgentVersions table the resolver scans, empty by default."""
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName="AgentVersions",
            KeySchema=[
                {"AttributeName": "runtime_name", "KeyType": "HASH"},
                {"AttributeName": "version_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "runtime_name", "AttributeType": "S"},
                {"AttributeName": "version_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("AGENT_VERSIONS_TABLE_NAME", "AgentVersions")
        monkeypatch.setenv("APP_AWS_REGION", REGION)
        yield boto3.resource("dynamodb", region_name=REGION).Table("AgentVersions")


def seed_row(table, *, runtime_name: str, version_id: str, runtime_id: str = "", acn: str = "") -> None:
    item = {"runtime_name": runtime_name, "version_id": version_id, "owner_sub": "sub-alice"}
    if runtime_id:
        item["runtime_id"] = runtime_id
    if acn:
        item["agentcore_runtime_name"] = acn
    table.put_item(Item=item)


# ---------------------------------------------------------------------------
# The resolver, directly. Four ways to have no proof.
# ---------------------------------------------------------------------------


def test_a_scan_miss_resolves_to_nothing(versions_table):
    """And specifically NOT the hash-stripped guess, which is what it used to return."""
    assert mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION) is None
    assert mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION) != "orders_bot_1a2b3c4d"


def test_a_read_error_resolves_to_nothing(versions_table, monkeypatch):
    """The ``except`` used to produce a name too, so a throttled scan deleted triggers."""

    def _boom(*_a, **_kw):
        raise RuntimeError("throttled")

    monkeypatch.setattr(mod.boto3, "resource", _boom)
    assert mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION) is None


def test_two_names_for_one_id_resolve_to_nothing(versions_table):
    """Corrupt data is not a tie to break.

    The old loop returned on the first match, so which tenant's triggers got deleted depended on
    DynamoDB's scan order.
    """
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    seed_row(versions_table, runtime_name="someone_elses_bot", version_id="v1", runtime_id=CANONICAL)

    assert mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION) is None


def test_a_blank_friendly_name_cannot_even_be_stored(versions_table):
    """Why the ``if not name: continue`` guard is defensive, not a case.

    A row with a blank ``runtime_name`` would be a second, un-attributable mapping for the same id
    and so would make every lookup ambiguous. It is unrepresentable: ``runtime_name`` is the
    partition key and DynamoDB refuses an empty string for a key attribute. Pinned rather than
    assumed, because the OTHER guard in this area -- ``owner_sub`` on the same table -- turned out to
    accept an absent value on a sparse GSI, so "obviously impossible" has already been wrong once.
    """
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError) as excinfo:
        versions_table.put_item(Item={"runtime_name": "", "version_id": "v2", "runtime_id": CANONICAL})

    assert "ValidationException" in str(excinfo.value)


def test_an_exact_id_mapping_resolves(versions_table):
    """Control. Without this, every test in this file could pass by always returning None."""
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    assert mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION) == FRIENDLY


def test_an_agentcore_name_mapping_resolves_when_no_id_matched(versions_table):
    """The second tier, keyed by the LIVE name off the ownership-proven describe."""
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", acn=LIVE_NAME)
    assert mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION, LIVE_NAME) == FRIENDLY


def test_the_canonical_id_is_never_compared_against_the_stored_name(versions_table):
    """The dead tier, pinned so it cannot come back.

    This function used to compare ``agentcore_runtime_name == canonical_id``. The id is
    ``<agentcore_runtime_name>-<10hash>``, so that equality could not hold for any real runtime and
    the entire fallback was unreachable -- it read as coverage while resolving nothing. A row whose
    stored name equals the ID (the shape the old code looked for) must NOT match, and with no live
    name supplied there is no second tier at all.
    """
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", acn=CANONICAL)

    assert mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION) is None
    assert mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION, LIVE_NAME) is None


def test_an_id_match_beats_an_agentcore_name_match(versions_table):
    """``agentcore_runtime_name`` is a NAME (``friendly[:39]_<8hex>``), so two tenants can collide
    on it after truncation. The id is unique, so it decides and the weaker match is not even
    counted towards ambiguity."""
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    seed_row(versions_table, runtime_name="collided_bot", version_id="v1", acn=LIVE_NAME)

    assert mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION, LIVE_NAME) == FRIENDLY


def test_two_names_for_one_live_name_resolve_to_nothing(versions_table):
    """Ambiguity in the second tier is still ambiguity -- this is the truncation collision."""
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", acn=LIVE_NAME)
    seed_row(versions_table, runtime_name="collided_bot", version_id="v1", acn=LIVE_NAME)

    assert mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION, LIVE_NAME) is None


def test_every_scan_page_is_strongly_consistent(versions_table, monkeypatch):
    """An eventually consistent scan cannot prove uniqueness.

    Two directions, both bad: it can miss the row that maps this id (a leak), and it can miss the
    SECOND, conflicting row and so report a unique mapping that is not one -- which authorizes a
    non-owner-scoped delete. The oracle is per page, because ``ExclusiveStartKey`` pagination
    rebuilds the kwargs and dropping the flag on page 2 would be invisible.
    """
    for i in range(3):
        seed_row(versions_table, runtime_name=f"bot{i}", version_id="v1", runtime_id=f"other-{i}")
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    real_resource = mod.boto3.resource
    seen: list[object] = []

    class _RecordingTable:
        def __init__(self, inner):
            self._inner = inner

        def scan(self, **kwargs):
            seen.append(kwargs.get("ConsistentRead"))
            # One row per page, so pagination actually happens.
            return self._inner.scan(**{**kwargs, "Limit": 1})

    class _Res:
        def __init__(self, inner):
            self._inner = inner

        def Table(self, name):  # noqa: N802 - boto3 casing
            return _RecordingTable(self._inner.Table(name))

    monkeypatch.setattr(mod.boto3, "resource", lambda *a, **kw: _Res(real_resource(*a, **kw)))

    mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION)

    assert len(seen) > 1, "pagination never happened, so a per-page assertion proves nothing"
    assert set(seen) == {True}, f"a scan page was eventually consistent: {seen}"


def test_a_truncated_scan_resolves_to_nothing(versions_table, monkeypatch):
    """A page cap with pages left is INCONCLUSIVE, not a miss.

    Uniqueness cannot be claimed over rows nobody read, and this table is scanned, not queried, so
    on a busy platform the match on page 1 says nothing about page 21.
    """
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    real_resource = mod.boto3.resource

    class _AlwaysMorePages:
        def __init__(self, inner):
            self._inner = inner

        def scan(self, **kwargs):
            resp = self._inner.scan(**kwargs)
            resp["LastEvaluatedKey"] = {"runtime_name": FRIENDLY, "version_id": "v1"}
            return resp

    class _Res:
        def __init__(self, inner):
            self._inner = inner

        def Table(self, name):  # noqa: N802 - boto3 casing
            return _AlwaysMorePages(self._inner.Table(name))

    monkeypatch.setattr(mod.boto3, "resource", lambda *a, **kw: _Res(real_resource(*a, **kw)))
    assert mod._resolve_runtime_name_for_cleanup(CANONICAL, REGION) is None


# ---------------------------------------------------------------------------
# destroy_runtime end to end: zero side effects, and the control that proves
# the side effects are reachable.
# ---------------------------------------------------------------------------


@pytest.fixture
def run_destroy(monkeypatch: pytest.MonkeyPatch):
    def _run(*, store: _SpyTriggerStore, runtime_name: str | None = None, strict: bool = True):
        import app.services.trigger_store as ts

        requested: list[str] = []
        runtime = _LiveRuntime()

        def _client(service: str, **_kwargs):
            requested.append(service)
            if strict and service in DESTRUCTIVE_SERVICES:
                raise AssertionError(
                    f"destroy_runtime built a {service!r} client for an unproven runtime name; "
                    "that is a cross-tenant trigger delete"
                )
            if service == "bedrock-agentcore-control":
                return runtime
            if service == "iam":
                return _Iam()
            if service == "events":
                return _OwnedEvents()
            return _Permissive()

        monkeypatch.setattr(mod.boto3, "client", _client)
        monkeypatch.setattr(ts, "get_trigger_store", lambda: store)
        monkeypatch.delenv("SHARED_RUNTIME_ROLE_ARN", raising=False)
        result = mod.destroy_runtime(
            CANONICAL,
            REGION,
            delete_execution_role=False,
            runtime_name=runtime_name,
        )
        return result, requested, runtime

    return _run


def test_an_unresolvable_name_makes_zero_trigger_mutations(versions_table, run_destroy):
    """The empty AgentVersions table is the scan miss; nothing downstream may run.

    Note what is NOT asserted: that destroy_runtime failed. It must still delete the runtime -- the
    refusal is scoped to the name-keyed cleanup, because deleting another tenant's triggers is
    unrecoverable while a leaked schedule is merely expensive.

    F-81f: "merely expensive" was NOT true when this test was first written, and the claim is now
    carried by the returned outcome rather than by this comment. A peer session measured the old
    end-to-end behaviour: the refusal skipped the cleanup, the name release then deleted the slot
    and version rows anyway, and the owner's own trigger DELETE answered 404 afterwards, because
    ``routers/triggers._resolve_owned_runtime`` resolves ownership THROUGH the production slot. The
    residue was neither visible nor fixable. So the refusal has to be reported -- ``unresolved``,
    never ``confirmed`` -- and ``_release_runtime_name_claim`` keeps the name locked on anything but
    ``confirmed``. That is asserted here and again, against the real stores, in
    tests/test_teardown_keeps_the_owners_trigger_handle.py.
    """
    store = _SpyTriggerStore([_Trigger(FRIENDLY)])

    result, requested, runtime = run_destroy(store=store)

    assert store.enumerated == [], f"the TriggersTable was enumerated by an unproven name: {store.enumerated}"
    assert store.deleted == []
    assert [s for s in requested if s in DESTRUCTIVE_SERVICES] == []
    assert runtime.deleted == [CANONICAL], "the runtime itself must still be destroyed"
    assert result.get("success") is True
    assert result["triggers"]["outcome"] == "unresolved", (
        "a skipped cleanup reported as anything else lets the caller release the name"
    )


def test_a_read_error_makes_zero_trigger_mutations(versions_table, run_destroy, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("throttled")

    monkeypatch.setattr(mod.boto3, "resource", _boom)
    store = _SpyTriggerStore([_Trigger(FRIENDLY)])

    _result, requested, _runtime = run_destroy(store=store)

    assert store.enumerated == []
    assert [s for s in requested if s in DESTRUCTIVE_SERVICES] == []


def test_an_ambiguous_mapping_makes_zero_trigger_mutations(versions_table, run_destroy):
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    seed_row(versions_table, runtime_name="someone_elses_bot", version_id="v1", runtime_id=CANONICAL)
    store = _SpyTriggerStore([_Trigger(FRIENDLY), _Trigger("someone_elses_bot")])

    _result, requested, _runtime = run_destroy(store=store)

    assert store.enumerated == []
    assert store.deleted == []
    assert [s for s in requested if s in DESTRUCTIVE_SERVICES] == []


def test_a_proven_name_does_delete_its_triggers(versions_table, run_destroy):
    """THE CONTROL, and the reason the others mean anything.

    A refusal-only suite is compatible with the trigger cleanup being dead code -- which is exactly
    what a test file full of "asserts nothing happened" cannot tell you. Here the mapping is proven
    by a real version row, so every delete must fire.
    """
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    store = _SpyTriggerStore([_Trigger(FRIENDLY)])

    result, requested, _runtime = run_destroy(store=store, strict=False)

    assert store.enumerated == [FRIENDLY]
    assert store.deleted == [(FRIENDLY, "trg-1")]
    for service in ("scheduler", "events", "lambda"):
        assert service in requested, f"the proven path never built a {service} client"
    assert result["triggers"] == {
        "outcome": "confirmed",
        "rows": 1,
        "deleted": 1,
        "kept": 0,
        "foreign": 0,
    }
    # The enumeration is the evidence for "nothing is left"; a stale page is a released name plus a
    # schedule still firing.
    assert store.consistency == [True]


def test_a_live_delivery_lease_keeps_the_trigger_and_name_retryable(
    versions_table,
    monkeypatch,
):
    """Exercise the real DynamoDB fence across dispatch and runtime teardown."""

    import app.services.trigger_store as ts

    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName="Triggers",
        KeySchema=[
            {"AttributeName": "runtime_name", "KeyType": "HASH"},
            {"AttributeName": "trigger_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "runtime_name", "AttributeType": "S"},
            {"AttributeName": "trigger_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    store = ts.TriggerStore(table_name="Triggers", region=REGION)
    trigger = store.put_trigger_unfenced(
        runtime_name=FRIENDLY,
        trigger_id="trg-live",
        owner_sub="sub-alice",
        type=ts.TYPE_CRON,
        target_runtime_arn=(f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{CANONICAL}"),
        version_id="v1",
        deployment_id="dep-1",
        status=ts.STATUS_ACTIVE,
        schedule="cron(0 12 * * ? *)",
        eventbridge_rule_arn=(f"arn:aws:events:{REGION}:123456789012:rule/agentcore-trigger-trg-live"),
    )
    store.acquire_delivery(
        trigger=trigger,
        delivery_id="evt-live",
        lease_seconds=630,
        retention_seconds=86_400,
    )
    seed_row(
        versions_table,
        runtime_name=FRIENDLY,
        version_id="v1",
        runtime_id=CANONICAL,
    )
    monkeypatch.setattr(ts, "get_trigger_store", lambda: store)
    runtime = _LiveRuntime()

    def _target_client(service: str, **_kwargs):
        if service == "bedrock-agentcore-control":
            return runtime
        if service == "iam":
            return _Iam()
        return _Permissive()

    def _platform_client(service: str, **_kwargs):
        raise AssertionError(f"teardown built a platform {service} client while dispatch held the fence")

    result = mod.destroy_runtime(
        CANONICAL,
        REGION,
        client_factory=_target_client,
        platform_client_factory=_platform_client,
        delete_execution_role=False,
    )

    current = store.get(FRIENDLY, "trg-live", consistent=True)
    assert current is not None
    assert current.status == ts.STATUS_ACTIVE
    assert result["triggers"]["outcome"] == "partial"
    assert result["triggers"]["kept"] == 1
    assert result["triggers"]["deleted"] == 0


def test_trigger_resources_always_use_the_platform_client(
    versions_table,
    monkeypatch,
):
    """A target-account runtime must not redirect trigger cleanup there."""

    import app.services.trigger_store as ts

    seed_row(
        versions_table,
        runtime_name=FRIENDLY,
        version_id="v1",
        runtime_id=CANONICAL,
    )
    store = _SpyTriggerStore([_Trigger(FRIENDLY)])
    monkeypatch.setattr(ts, "get_trigger_store", lambda: store)
    runtime = _LiveRuntime()
    target_requests: list[str] = []
    platform_requests: list[str] = []

    def _target_client(service: str, **_kwargs):
        target_requests.append(service)
        if service in DESTRUCTIVE_SERVICES:
            raise AssertionError(f"trigger cleanup escaped to the runtime account via {service}")
        if service == "bedrock-agentcore-control":
            return runtime
        if service == "iam":
            return _Iam()
        return _Permissive()

    def _platform_client(service: str, **kwargs):
        assert kwargs.get("region_name") == REGION
        platform_requests.append(service)
        if service == "events":
            return _OwnedEvents()
        return _Permissive()

    result = mod.destroy_runtime(
        CANONICAL,
        REGION,
        client_factory=_target_client,
        platform_client_factory=_platform_client,
        delete_execution_role=False,
    )

    assert result["triggers"]["outcome"] == "confirmed"
    assert store.deleted == [(FRIENDLY, "trg-1")]
    assert {"scheduler", "events", "lambda"} <= set(platform_requests)
    assert not (set(target_requests) & set(DESTRUCTIVE_SERVICES))


def test_a_target_change_after_enumeration_authorizes_no_resource_delete(
    versions_table,
    run_destroy,
):
    """The claim's returned image, not the stale scan row, decides authority."""

    class _MovedTargetStore(_SpyTriggerStore):
        def claim_delete(self, **kwargs):
            row = super().claim_delete(**kwargs)
            assert row is not None
            row.target_runtime_arn = f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/someone_else-ZyXwVuTsRq"
            return row

    seed_row(
        versions_table,
        runtime_name=FRIENDLY,
        version_id="v1",
        runtime_id=CANONICAL,
    )
    store = _MovedTargetStore([_Trigger(FRIENDLY)])

    result, requested, _runtime = run_destroy(store=store)

    assert store.deleted == []
    assert [s for s in requested if s in DESTRUCTIVE_SERVICES] == []
    assert result["triggers"]["outcome"] == "partial"
    assert result["triggers"]["kept"] == 1


def test_lost_row_delete_fence_keeps_the_name_locked(
    versions_table,
    run_destroy,
):
    """Resource cleanup is not permission to erase a newer claimant's row."""

    class _LostDeleteFenceStore(_SpyTriggerStore):
        def delete_claimed(self, **_kwargs) -> bool:
            return False

    seed_row(
        versions_table,
        runtime_name=FRIENDLY,
        version_id="v1",
        runtime_id=CANONICAL,
    )
    store = _LostDeleteFenceStore([_Trigger(FRIENDLY)])

    result, _requested, _runtime = run_destroy(
        store=store,
        strict=False,
    )

    assert store.deleted == []
    assert result["triggers"]["outcome"] == "partial"
    assert result["triggers"]["kept"] == 1


def test_a_row_that_targets_another_runtime_is_never_touched(versions_table, run_destroy):
    """F-81f — a proven NAME is not a proven ROW.

    The friendly name selects a TriggersTable partition that is not owner-scoped, so two tenants
    that collide on a name share it. The old code deleted every row in the partition: the schedule,
    the rule, the function URL, the webhook secret and the row itself, for a trigger that fires at
    somebody else's runtime. ``target_runtime_arn`` is derived server-side at create time, so the
    row itself says which runtime it belongs to.

    The outcome is still ``confirmed``: every row that targets THIS runtime is gone (there were
    none). The foreign row is reported, not counted as our residue -- otherwise a name shared with
    another tenant could never be released.
    """
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    foreign = _Trigger(FRIENDLY, target="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/other_bot-ZyXwVuTsRq")
    store = _SpyTriggerStore([foreign])

    result, requested, _runtime = run_destroy(store=store)

    assert store.enumerated == [FRIENDLY], "the partition must still be read; the row is authorized, not the name"
    assert store.deleted == [], "a row targeting another runtime was deleted"
    assert [s for s in requested if s in DESTRUCTIVE_SERVICES] == []
    assert result["triggers"]["foreign"] == 1
    assert result["triggers"]["outcome"] == "confirmed"


def test_a_row_with_no_recorded_target_is_kept_not_guessed(versions_table, run_destroy):
    """A legacy row with no ``target_runtime_arn`` authorizes nothing, and that is reported.

    Absence is not agreement -- the same rule the version-row identity check learned. The row
    survives as the operator's handle and the outcome is ``partial``, which keeps the name locked
    rather than releasing it over a row nobody can attribute.
    """
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    store = _SpyTriggerStore([_Trigger(FRIENDLY, target="")])

    result, requested, _runtime = run_destroy(store=store)

    assert store.deleted == []
    assert [s for s in requested if s in DESTRUCTIVE_SERVICES] == []
    assert result["triggers"]["foreign"] == 0, "an absent target is not proof the row is somebody else's"
    assert result["triggers"]["kept"] == 1
    assert result["triggers"]["outcome"] == "partial", (
        "an unattributable row must keep the name locked; it may be a legacy row of ours"
    )


# ---------------------------------------------------------------------------
# The target parser itself. Three outcomes, and the ARN shape has to be checked.
# ---------------------------------------------------------------------------

_OTHER = "other_bot-ZyXwVuTsRq"

CLASSIFICATIONS = [
    # (target, expected classification, why this case is in the table)
    (
        f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{CANONICAL}",
        "ours",
        "the shape the version row records",
    ),
    (
        f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{CANONICAL}/runtime-endpoint/DEFAULT",
        "ours",
        "the endpoint ARN an invoke path records for the same runtime",
    ),
    (CANONICAL, "ours", "the bare-id fallback when no ARN was recorded yet"),
    (
        f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{_OTHER}",
        "foreign",
        "a well-formed ARN for another runtime",
    ),
    (_OTHER, "foreign", "a well-formed bare id for another runtime"),
    # The measured defect. Checking only the resource segment let an unrelated service's ARN that
    # happens to contain "runtime/<our-id>" -- an S3 object key, say -- read as PROOF of ownership,
    # which authorized deleting that row's schedule, rule, function URL and webhook secret.
    (f"arn:aws:s3:{REGION}:123456789012:runtime/{CANONICAL}", "unknown", "wrong service, our id"),
    (f"arn:aws:s3:{REGION}:123456789012:runtime/{_OTHER}", "unknown", "wrong service, another id"),
    (f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime", "unknown", "truncated: no id at all"),
    (f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/", "unknown", "truncated: empty id"),
    (f"arn:aws:bedrock-agentcore:{REGION}:123456789012:memory/{CANONICAL}", "unknown", "another resource type"),
    (
        f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{CANONICAL}/alias/live",
        "unknown",
        "a resource nested under the runtime is not the runtime",
    ),
    (f"arn::bedrock-agentcore:{REGION}:123456789012:runtime/{CANONICAL}", "unknown", "no partition"),
    # Peer counterexamples against the service model in the installed bedrock-agentcore-control
    # model: AgentRuntimeArn requires a region and a 12-digit account, and AgentRuntimeId's name
    # part is [a-zA-Z][a-zA-Z0-9_]* -- no dots and no leading digit. A pattern loose enough to admit
    # these turns a string somebody made up into a positive classification.
    (f"arn:aws:bedrock-agentcore:::runtime/{CANONICAL}", "unknown", "no region and no account"),
    (f"arn:aws:bedrock-agentcore:{REGION}::runtime/{CANONICAL}", "unknown", "no account"),
    (f"arn:aws:bedrock-agentcore::123456789012:runtime/{CANONICAL}", "unknown", "no region"),
    (f"arn:aws:bedrock-agentcore:{REGION}:12345:runtime/{CANONICAL}", "unknown", "account is not 12 digits"),
    ("bad.name-ZyXwVu9876", "unknown", "a dot is not in the id grammar"),
    ("9lead-ZyXwVu9876", "unknown", "an id cannot start with a digit"),
    ("other-bot-ZyXwVuTsRq", "unknown", "a hyphen is not in the name grammar"),
    (f"{CANONICAL}-ZyXwVuTsRq", "unknown", "our id plus another suffix is not our id"),
    (
        f"arn:aws-cn:bedrock-agentcore:cn-north-1:123456789012:runtime/{_OTHER}",
        "foreign",
        "a non-aws partition is valid",
    ),
    ("not-an-arn", "unknown", "not an ARN and not an id"),
    ("arn:aws:bedrock-agentcore", "unknown", "too few segments to have a resource"),
    ("", "unknown", "a row that recorded no target"),
    ("   ", "unknown", "whitespace is not a target"),
]


@pytest.mark.parametrize("target,expected,why", CLASSIFICATIONS, ids=[c[2] for c in CLASSIFICATIONS])
def test_the_target_classifier_is_tri_state(target, expected, why):
    """Every shape lands in exactly one of ours / foreign / unknown, and unknown is the default.

    A peer session probed the earlier two-state helper directly and found the hole this table
    closes: with a bool, "not ours" had to serve as both "provably somebody else's" and "I cannot
    read this", and the caller spent it as the former. So a malformed or wrong-service target was a
    positive finding -- either authorizing a delete (same id, wrong service) or authorizing the name
    release over a row nobody could attribute.

    ``foreign`` is only ever returned for a target that is structurally a runtime identifier and
    names a DIFFERENT runtime. Everything unreadable is ``unknown``, which keeps the row and the
    name lock.
    """
    assert mod._classify_trigger_target(target, CANONICAL) == expected, why  # noqa: SLF001
    # The delete authorization is the classifier's "ours" and nothing else, so the two cannot drift.
    assert mod._trigger_targets_runtime(target, CANONICAL) is (expected == "ours")  # noqa: SLF001


def test_no_canonical_id_attributes_nothing_in_either_direction():
    """With no id to compare against, a well-formed ARN is not evidence of anything.

    Read as ``foreign`` it would let the teardown release the friendly name over live rows; read as
    ``ours`` it would delete them. It has to be ``unknown``.
    """
    valid = f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{CANONICAL}"
    for empty in ("", None, "   "):
        assert mod._classify_trigger_target(valid, empty) == "unknown"  # noqa: SLF001
        assert mod._trigger_targets_runtime(valid, empty) is False  # noqa: SLF001


def test_a_wrong_service_arn_with_our_id_deletes_nothing(versions_table, run_destroy):
    """The end-to-end half of the defect above: it must reach the caller as a KEPT row.

    Asserting the classifier in isolation is compatible with the loop ignoring it, and the loop is
    where the deletes happen. ``unknown`` has to mean no mutation, the row preserved as the handle,
    and ``partial`` so the name stays locked.
    """
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    store = _SpyTriggerStore([_Trigger(FRIENDLY, target=f"arn:aws:s3:{REGION}:123456789012:runtime/{CANONICAL}")])

    result, requested, _runtime = run_destroy(store=store)

    assert store.deleted == [], "an ARN from another service was accepted as proof of ownership"
    assert [s for s in requested if s in DESTRUCTIVE_SERVICES] == []
    assert result["triggers"]["kept"] == 1
    assert result["triggers"]["foreign"] == 0
    assert result["triggers"]["outcome"] == "partial"


# ---------------------------------------------------------------------------
# The caller-supplied hint. It may VETO the resolved name; it may never be one.
# ---------------------------------------------------------------------------


def test_a_hint_that_disagrees_with_metadata_makes_zero_trigger_mutations(versions_table, run_destroy):
    """``runtime_name=...`` used to short-circuit the resolver entirely.

    Both production callers had a name to offer and neither had proof: the manifest teardown passed
    ``res["name"]``, recorded as ``friendly_runtime_name or runtime_id``, and the legacy
    cross-account path passed ``_proven_runtime_name_for_destroy``, whose "proven" means "this key is
    the one the deployment record names" -- not "this key maps to this runtime id". So a stale record
    selected a live partition belonging to whoever holds that name now.

    Here metadata proves the name is ``orders_bot`` and the caller says ``someone_elses_bot``.
    Neither is trusted: a disagreement means one of the two is stale.
    """
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    store = _SpyTriggerStore([_Trigger(FRIENDLY), _Trigger("someone_elses_bot")])

    _result, requested, _runtime = run_destroy(store=store, runtime_name="someone_elses_bot")

    assert store.enumerated == [], f"a caller-supplied name selected a partition: {store.enumerated}"
    assert store.deleted == []
    assert [s for s in requested if s in DESTRUCTIVE_SERVICES] == []


def test_a_hint_with_no_metadata_to_confirm_it_makes_zero_trigger_mutations(versions_table, run_destroy):
    """The version row is already gone -- so there is nothing to confirm the hint against.

    This is the deliberate cost of the fix, pinned so nobody 'fixes' it back: the triggers leak.
    Trusting the hint here is the exact shape that deletes another tenant's schedules.

    What makes the leak survivable is the REPORT, not the leak's nature: the outcome is
    ``unresolved``, so the name stays locked and the tenant keeps the slot their own trigger API
    resolves ownership through. Before F-81f this refusal was silent and the subsequent release
    stripped that handle.
    """
    store = _SpyTriggerStore([_Trigger(FRIENDLY)])

    result, requested, _runtime = run_destroy(store=store, runtime_name=FRIENDLY)

    assert store.enumerated == []
    assert [s for s in requested if s in DESTRUCTIVE_SERVICES] == []
    assert result["triggers"]["outcome"] == "unresolved"


def test_a_hint_that_agrees_with_metadata_still_cleans_up(versions_table, run_destroy):
    """The control for the veto: agreement must not be treated as a conflict.

    Without this, the fix could be 'any hint at all cancels the cleanup', which passes every refusal
    test above and silently removes the feature for the manifest path.
    """
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    store = _SpyTriggerStore([_Trigger(FRIENDLY)])

    _result, _requested, _runtime = run_destroy(store=store, runtime_name=FRIENDLY, strict=False)

    assert store.enumerated == [FRIENDLY]
    assert store.deleted == [(FRIENDLY, "trg-1")]


def test_the_manifest_teardown_hands_over_no_name_at_all(monkeypatch):
    """The other end of the same boundary, at its own call site.

    ``_delete_managed_resource`` used to do ``if rname: destroy_kwargs["runtime_name"] = rname``.
    The veto above makes a wrong value harmless, but the value should never travel: the manifest
    records ``friendly_runtime_name or runtime_id``, so for a canvas with no friendly name this was
    a canonical id or an ``<friendly>_<8hex>`` AgentCore name -- precisely the unproven shape the
    resolver refuses to synthesise, arriving from the other direction.
    """
    from app import deployment_handler as dh

    captured: dict = {}

    def _fake_destroy(runtime_id, region, **kwargs):
        captured["runtime_id"] = runtime_id
        captured["kwargs"] = kwargs
        return {"success": True, "message": "deleted"}

    monkeypatch.setattr(dh, "destroy_runtime", _fake_destroy)
    monkeypatch.setattr(dh, "assert_agentcore_resource_owned", lambda *a, **kw: {})
    monkeypatch.setattr(dh.boto3, "client", lambda *a, **kw: _Permissive())

    msg = dh._delete_managed_resource(
        {
            "type": "agent_runtime",
            "id": CANONICAL,
            # The wrong-but-plausible value the writers actually record.
            "name": LIVE_NAME,
            "region": REGION,
        },
        REGION,
    )

    assert captured["runtime_id"] == CANONICAL
    assert "runtime_name" not in captured["kwargs"], (
        f"the manifest's unproven name reached destroy_runtime: {captured['kwargs']}"
    )
    assert CANONICAL in msg


# --------------------------------------------------------------------------- every trigger type, and the sidecars


@pytest.mark.parametrize("trigger_type", ["cron", "eventbridge", "s3", "webhook"])
def test_every_trigger_type_is_torn_down_by_the_runtime_destroy(versions_table, run_destroy, trigger_type):
    """All four types provision the same shapes (an EventBridge rule; a webhook adds an HMAC secret),
    so one path must delete all four. Proven per type, because a suite that only tried ``cron`` is
    compatible with a type check somewhere skipping the rest."""
    import app.services.trigger_store as ts

    assert trigger_type in ts.TRIGGER_TYPES
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    trigger = _Trigger(FRIENDLY)
    trigger.type = trigger_type
    store = _SpyTriggerStore([trigger])
    result, requested, _runtime = run_destroy(store=store, strict=False)
    assert store.deleted == [(FRIENDLY, "trg-1")], trigger_type
    assert "events" in requested, f"{trigger_type}: the EventBridge rule was never touched"
    assert result["triggers"]["outcome"] == "confirmed" and result["triggers"]["deleted"] == 1


def _expected_default_eval_name() -> str:
    import re as _re

    return _re.sub(r"[^a-zA-Z0-9_]", "_", f"eval_{CANONICAL}")[:48]


def test_sidecars_the_destroy_could_not_remove_are_returned_not_logged_away(versions_table, run_destroy, monkeypatch):
    import app.services.observability_dashboard as dash

    monkeypatch.setattr(dash, "delete_dashboard_for_runtime", lambda *a, **k: False)
    monkeypatch.setattr(
        mod,
        "_list_all_online_evaluation_configs",
        lambda ctrl: [
            {"onlineEvaluationConfigName": _expected_default_eval_name(), "onlineEvaluationConfigId": "cfg-ours"},
            # Contains our id as a substring but is not our default name: another runtime's config.
            {
                "onlineEvaluationConfigName": (_expected_default_eval_name() + "_2")[:48],
                "onlineEvaluationConfigId": "cfg-theirs",
            },
        ],
    )
    deleted: list[str] = []

    def _delete(self, *, onlineEvaluationConfigId):  # noqa: N803
        deleted.append(onlineEvaluationConfigId)
        raise RuntimeError("ThrottlingException")

    monkeypatch.setattr(_LiveRuntime, "delete_online_evaluation_config", _delete, raising=False)
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    result, _requested, _runtime = run_destroy(store=_SpyTriggerStore([]), strict=False)
    assert deleted == ["cfg-ours"], "only the exact default-named config may be touched"
    assert "dashboard" in result["sidecar_failures"]
    assert "evaluation_config:cfg-ours" in result["sidecar_failures"]
    assert result["success"] is True, "the runtime delete itself is not blocked by a sidecar"


def test_a_clean_destroy_reports_no_sidecar_failures(versions_table, run_destroy, monkeypatch):
    import app.services.observability_dashboard as dash

    monkeypatch.setattr(dash, "delete_dashboard_for_runtime", lambda *a, **k: True)
    monkeypatch.setattr(mod, "_list_all_online_evaluation_configs", lambda ctrl: [])
    seed_row(versions_table, runtime_name=FRIENDLY, version_id="v1", runtime_id=CANONICAL)
    result, _requested, _runtime = run_destroy(store=_SpyTriggerStore([]), strict=False)
    assert result["sidecar_failures"] == []
