"""Root conftest — fix sys.path so vendored pydantic stubs don't shadow the real package."""

import copy
import functools
import os
import sys
import threading
import types

# The backend/ directory contains vendored pydantic/pydantic_core stubs for Lambda
# packaging. These are pure-Python stubs without the compiled _pydantic_core extension.
# When pytest runs from backend/, '' (cwd) in sys.path picks up these stubs instead
# of the real pydantic from site-packages. Fix by removing the backend dir from path.
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src_dir = os.path.join(_backend_dir, "src")

# Remove backend dir entries that would shadow site-packages
sys.path = [
    p
    for p in sys.path
    if p == _src_dir  # keep src/
    or "site-packages" in p  # keep venv packages
    or (p and not os.path.samefile(p, _backend_dir) if os.path.isdir(p) and p else True)
]

# Ensure src/ is on the path
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


# ---------------------------------------------------------------------------
# moto cross-file isolation
# ---------------------------------------------------------------------------
# When many test files each open their own `mock_aws()` context across one
# pytest process, boto3's module-level DEFAULT_SESSION gets cleared on teardown
# and a LATER file's `mock_aws()` setup raises
#   AttributeError: <module 'boto3'> does not have the attribute 'DEFAULT_SESSION'
# (moto patches boto3.DEFAULT_SESSION, but if a prior context deleted the attr
# the patch target is gone). Each file passes alone / pairwise; the failure only
# appears in the full Phase-3 suite. This is a test-runner ordering artifact,
# NOT a product defect. Defensively ensure the attribute exists before each test
# so moto always has a patch target. See tasks/lessons.md (Phase 3 integration).
import pytest  # noqa: E402

# ---------------------------------------------------------------------------
# Hypothesis determinism on CI
# ---------------------------------------------------------------------------
# 20 property-test files explore RANDOM inputs each run, so a latent edge case
# can pass locally yet fail on a CI runner that happened to draw the bad input
# (exactly how the NoCredentialsError surfaced). Register a derandomized "ci"
# profile (fixed example database off, stable derandomize seed, generous
# deadline for slow runners) and load it when CI is set, so a green run stays
# green on re-runs. Locally the default profile still explores freely.
try:
    from hypothesis import HealthCheck, settings

    settings.register_profile(
        "ci",
        derandomize=True,
        deadline=None,
        max_examples=50,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    settings.register_profile(
        "dev",
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    settings.load_profile("ci" if os.environ.get("CI") else "dev")
except Exception:  # noqa: BLE001 — hypothesis always present in dev deps; be defensive
    pass


@pytest.fixture(autouse=True)
def _no_real_aws_credentials(request, monkeypatch):
    """Unit tests must never reach real AWS. Neutralize ambient credentials so a
    stray un-mocked boto3 call fails loudly (NoCredentialsError) HERE instead of
    silently succeeding on a developer's machine and then failing on CI. moto's
    @mock_aws and explicit fake-cred patches set their own values and win over
    this (fixtures run before the test body; moto/patch.dict apply inside it).

    Real-AWS integration tests are the one explicit exception. They are marked
    ``integration`` and require the caller's ambient AWS session for direct
    verification of durable DynamoDB state and resource teardown. Keeping the
    exemption marker-scoped prevents an ordinary unit test from reaching AWS
    merely because the developer happens to be authenticated.
    """
    if request.node.get_closest_marker("integration") is not None:
        yield
        return

    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    # Runtime-created resources now refuse to be created without an explicit
    # stack identity, because an untagged resource can never be deleted safely.
    # Individual ownership/config tests clear these values when exercising the
    # fail-closed path.
    monkeypatch.setenv("PROJECT_NAME", "unit-tests")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    yield


@pytest.fixture(autouse=True)
def _no_unrecorded_secrets(request, monkeypatch):
    """Both teardown paths also look for secrets by tag (ListSecrets), and treat a
    discovery failure as "cleanup incomplete". A suite that never models Secrets
    Manager would otherwise read NoCredentialsError as that, and every teardown in it
    would retain. Default: the account holds no unrecorded secret. Tests of the
    discovery itself opt out with ``@pytest.mark.real_secret_discovery``.
    """
    if request.node.get_closest_marker("real_secret_discovery") is None:
        from app.services import gateway_deployer

        monkeypatch.setattr(gateway_deployer, "discover_deployment_bound_secrets", lambda **_kw: [])
    yield


def _atomic(op):
    @functools.wraps(op)
    def _call(self, *a, **kw):
        with self._mutex:
            return op(self, *a, **kw)

    return _call


class InMemoryLockTable:
    """The conditional writes the claims table sees, and nothing else: the gateway
    mutation lock's PutItem/DeleteItem (F-66e) and the name claim's acquire, release
    and erase (F-66f), each under the condition the real call sends.

    Each write is atomic, as DynamoDB's conditional writes are, so a threaded race
    against it measures the caller. moto's conditional UpdateItem is not atomic under
    threads, and a race run against it failed on moto, not on the code under test.
    """

    name = "claims"

    def __init__(self):
        self.items = {}
        self.puts = 0
        self.calls = []
        self._mutex = threading.Lock()
        self.meta = types.SimpleNamespace(client=types.SimpleNamespace(transact_write_items=self._transact))

    def _transact(self, TransactItems):  # noqa: N803
        """TransactWriteItems of Updates, with the plain values the resource's client takes:
        all land, or none, cancelled with one reason each."""
        from botocore.exceptions import ClientError

        with self._mutex:
            self.calls.append(("transact_write_items", len(TransactItems)))
            before = copy.deepcopy(self.items)
            reasons, failed = [], False
            for op in TransactItems:
                u = op["Update"]
                assert u["TableName"] == self.name, u["TableName"]
                # The resource's client serializes: a typed value would be wrapped again,
                # which DynamoDB (and moto) reject, so the fake must reject it too.
                for value in (*u["Key"].values(), *u["ExpressionAttributeValues"].values()):
                    assert not isinstance(value, dict), f"a typed value reached the resource client: {value}"
                try:
                    self._update(
                        Key=u["Key"],
                        UpdateExpression=u["UpdateExpression"],
                        ConditionExpression=u.get("ConditionExpression"),
                        ExpressionAttributeValues=u["ExpressionAttributeValues"],
                    )
                    reasons.append({"Code": "None"})
                except ClientError:
                    failed = True
                    reasons.append({"Code": "ConditionalCheckFailed"})
            if failed:
                self.items = before
                raise ClientError(
                    {"Error": {"Code": "TransactionCanceledException"}, "CancellationReasons": reasons},
                    "TransactWriteItems",
                )
            return {}

    @staticmethod
    def _refuse(op):
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, op)

    @_atomic
    def put_item(self, Item, ConditionExpression, ExpressionAttributeValues):  # noqa: N803
        self.calls.append(("put_item", Item["claim_key"]))
        self.puts += 1
        held = self.items.get(Item["claim_key"])
        if held is not None and not held["lock_expires_at"] < ExpressionAttributeValues[":now"]:
            self._refuse("PutItem")
        self.items[Item["claim_key"]] = dict(Item)

    @_atomic
    def get_item(self, Key, ConsistentRead, ProjectionExpression, ExpressionAttributeNames):  # noqa: N803
        """Only ever the recovery reader's: strongly consistent, and projected to exactly
        the attributes its grant allows (the dynamodb:Attributes condition)."""
        from app.services.gateway_name_claim import RECOVERY_READ_ATTRIBUTES

        assert ConsistentRead is True
        names = [ExpressionAttributeNames[ref] for ref in ProjectionExpression.split(", ")]
        assert sorted(names) == sorted(RECOVERY_READ_ATTRIBUTES), names
        self.calls.append(("get_item", Key["claim_key"]))
        held = self.items.get(Key["claim_key"])
        if not held:
            return {}
        return {"Item": {k: (set(v) if isinstance(v, set) else v) for k, v in held.items() if k in names}}

    @_atomic
    def update_item(self, **kw):
        self.calls.append(("update_item", kw["Key"]["claim_key"]))
        return self._update(**kw)

    def _update(self, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression=None, **_kw):  # noqa: N803
        """The claim writes, told apart by their update expression; each under the
        condition the real call sends (test_the_fake_agrees_with_dynamodb pins it)."""
        key, v, expr = Key["claim_key"], ExpressionAttributeValues, UpdateExpression
        held = self.items.get(key)
        if expr == "ADD claim_keys :k, pointer_generation :one REMOVE gc_after":  # a recovery pointer
            item = self.items.setdefault(key, {"claim_key": key})
            item["claim_keys"] = set(item.get("claim_keys") or set()) | set(v[":k"])
            item["pointer_generation"] = item.get("pointer_generation", 0) + v[":one"]
            item.pop("gc_after", None)
            return {}
        if expr == "SET gc_after = :exp":  # reclaim a recovery pointer, at an unchanged generation
            if ConditionExpression == "pointer_generation = :g":
                unchanged = held is not None and held.get("pointer_generation") == v[":g"]
            else:
                assert (
                    ConditionExpression == "attribute_exists(claim_keys) AND attribute_not_exists(pointer_generation)"
                )
                unchanged = held is not None and "claim_keys" in held and "pointer_generation" not in held
            if not unchanged:
                self._refuse("UpdateItem")
            held["gc_after"] = v[":exp"]
            return {}

        def free():
            return (
                "holder_expires_at" not in held
                or held["holder_expires_at"] < v[":now"]
                or (held.get("holder_token") == v[":tok"] and held.get("owner_sub") == v[":owner"])
            )

        def refuse():
            from botocore.exceptions import ClientError

            item = {"owner_sub": {"S": held["owner_sub"]}} if held else {}
            if held and held.get("provisional"):
                item["provisional"] = {"BOOL": True}
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}, "Item": item}, "UpdateItem")

        lease = ("holder_deployment_id", "holder_token", "holder_expires_at")
        if expr.startswith("REMOVE holder"):  # release, maybe clearing recovery evidence
            if held is None or held.get("holder_token") != v[":tok"] or "provisional" in held:
                refuse()
            clears = "recovery_gateway_id" in expr
            if clears and held.get("recovery_gateway_id") not in {g for r, g in v.items() if r.startswith(":g")}:
                refuse()
            for attr in (*lease, "recovery_gateway_id", "recovery_deployment_id") if clears else lease:
                held.pop(attr, None)
            return {}
        if "REMOVE provisional" in expr or expr.startswith("SET holder_expires_at = :gone"):
            if held is None or not (
                held.get("provisional") is True
                and held.get("owner_sub") == v[":owner"]
                and held.get("holder_token") == v[":tok"]
                and ("REMOVE provisional" not in expr or held["holder_expires_at"] >= v[":now"])
            ):
                refuse()
            if "REMOVE provisional" in expr:  # promote, maybe writing recovery evidence
                for assignment in expr.split(" REMOVE ")[0].removeprefix("SET ").split(", "):
                    if " = " in assignment:
                        attr, ref = assignment.split(" = ")
                        held[attr] = v[ref]
                for attr in ("provisional", "gc_after", *lease):
                    held.pop(attr, None)
            else:  # abandon
                held.update(holder_expires_at=v[":gone"], gc_after=v[":now"])
            return {}
        if expr.startswith("SET owner_sub"):  # acquire: new, or a provisional claim with a free lease
            if held is not None and not (held.get("provisional") is True and free()):
                refuse()
            self.items[key] = {
                "claim_key": key,
                "owner_sub": v[":owner"],
                "holder_deployment_id": v[":dep"],
                "holder_token": v[":tok"],
                "holder_expires_at": v[":exp"],
                "provisional": True,
                "gc_after": v[":gc"],
                "created_at": (held or {}).get("created_at", v[":now"]),
            }
            return {}
        # acquire: re-take a durable claim of this owner
        if held is None or "provisional" in held or held.get("owner_sub") != v[":owner"] or not free():
            refuse()
        held.update(holder_deployment_id=v[":dep"], holder_token=v[":tok"], holder_expires_at=v[":exp"])
        return {}

    @_atomic
    def delete_item(self, Key, ConditionExpression, ExpressionAttributeValues):  # noqa: N803
        key, v = Key["claim_key"], ExpressionAttributeValues
        self.calls.append(("delete_item", key))
        held = self.items.get(key)
        if ":t" in v:
            if held is None or held.get("lock_holder") != v[":t"]:
                self._refuse("DeleteItem")
        elif held is None or not (
            held.get("owner_sub") == v[":owner"]
            and (
                "holder_expires_at" not in held
                or held["holder_expires_at"] < v[":now"]
                or (":tok" in v and held.get("holder_token") == v[":tok"])
            )
        ):
            self._refuse("DeleteItem")
        del self.items[key]


@pytest.fixture(autouse=True)
def gateway_lock_table(request, monkeypatch):
    """Every gateway writer takes a DynamoDB lock first (F-66e), and every teardown
    holds the gateway name (F-66f). A suite that never models the claims table would
    otherwise fail both on a missing env var. Default: one in-memory table with the
    real conditions, returned so a test can assert what was taken and released. Tests
    against their own table opt out with ``@pytest.mark.real_gateway_lock``.
    """
    table = InMemoryLockTable()
    if request.node.get_closest_marker("real_gateway_lock") is None:
        from app.services import gateway_mutation_lock, gateway_name_claim

        monkeypatch.setattr(gateway_mutation_lock, "_lock_table", lambda: table)
        monkeypatch.setattr(gateway_name_claim, "_teardown_claims", lambda: gateway_name_claim.GatewayNameClaims(table))
    yield table


class InMemoryLogGroups:
    """The two CloudWatch Logs calls a gateway deploy makes for each tool function's log
    group (``gateway_deployer.govern_tool_function_log_group``), with the service's
    answers: creating a name that exists is ResourceAlreadyExistsException, and retention
    on a group that does not exist is ResourceNotFoundException.
    """

    def __init__(self):
        self.groups: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.regions: list[str] = []

    def client(self, region):
        self.regions.append(region)
        return self

    @staticmethod
    def _error(code: str, operation: str):
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": code, "Message": f"{code} (in-memory)"}}, operation)

    def create_log_group(self, **kwargs):
        self.calls.append(("create_log_group", dict(kwargs)))
        if kwargs["logGroupName"] in self.groups:
            raise self._error("ResourceAlreadyExistsException", "CreateLogGroup")
        self.groups[kwargs["logGroupName"]] = {}

    def put_retention_policy(self, **kwargs):
        self.calls.append(("put_retention_policy", dict(kwargs)))
        if kwargs["logGroupName"] not in self.groups:
            raise self._error("ResourceNotFoundException", "PutRetentionPolicy")
        self.groups[kwargs["logGroupName"]]["retentionInDays"] = kwargs["retentionInDays"]


@pytest.fixture(autouse=True)
def tool_log_groups(monkeypatch):
    """Every gateway deploy that creates or adopts a tool function governs its log group
    first. A suite that never models CloudWatch Logs would otherwise fail every such
    deploy on NoCredentialsError. Default: an empty in-memory account, returned so a test
    can assert which groups were governed. A test of a failing call patches
    ``gateway_deployer._create_logs_client`` itself.
    """
    from app.services import gateway_deployer

    groups = InMemoryLogGroups()
    monkeypatch.setattr(gateway_deployer, "_create_logs_client", groups.client)
    yield groups


@pytest.fixture(autouse=True)
def _ensure_boto3_default_session():
    try:
        import boto3

        if not hasattr(boto3, "DEFAULT_SESSION"):
            boto3.DEFAULT_SESSION = None
    except Exception:
        pass
    yield
    try:
        import boto3

        if not hasattr(boto3, "DEFAULT_SESSION"):
            boto3.DEFAULT_SESSION = None
    except Exception:
        pass
