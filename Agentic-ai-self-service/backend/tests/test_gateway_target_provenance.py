"""A gateway target is repointed by its own deployment, and never adopted from another.

F-74b. Three defects, all of which produced a green deployment:

1. **A target that already held the name was reused unchanged** at five of the ten call
   sites, so an edited canvas never reached the gateway: the tool kept invoking the
   previous deploy's function with the previous schema, and broke outright once that
   deployment was deleted. F-74 fixed five sites; the other five are fixed here, and the
   test that pins it enumerates the call sites out of the source rather than exercising
   the families one at a time -- a per-family test can only cover the families that exist
   today, and the defect WAS a site nobody remembered.

2. **Every conflict path could return ``None``.** Four of the five callers discarded the
   return entirely and the fifth read it as "skip the readiness wait", so "the target is
   there and serving this canvas" and "we have no idea what is under this name" were the
   same outcome. There is no ``None`` exit left.

3. **Presence was treated as provenance.** A same-name target was reused, or with
   ``update_existing`` overwritten outright, with no check that it was ours. The floor
   available without a manifest row is the target's FAMILY, which our own code never
   changes for a canvas node, so a mismatch is positive evidence someone else configured
   it. Read failures are deliberately NOT mismatches: turning a throttle into "someone
   else owns your target" is both wrong and unactionable.

What is *not* claimed here: the family check does not tell two deployments apart when both
want the same family under the same name. That needs durable per-target ownership, which is
what the ``gateway_target`` manifest rows this module also pins are for -- and they have to
exist in the population before a check can require them, or the first teardown after the
change runs against a population where nothing has a row.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

sys.path.insert(0, "src")

from app.services import gateway_deployer as gd  # noqa: E402
from app.services.deployment_state_store import GATEWAY_GRAPH_FIELD  # noqa: E402
from app.services.gateway_deployer import (  # noqa: E402
    GatewayTargetFamilyConflict,
    GatewayTargetUnproven,
    collecting_target_records,
    target_family,
    target_replace_digest,
)
from app.step_handlers.gateway_step import _gateway_manifest_resources  # noqa: E402

GW = "agent-gateway-fake0000"
SOURCE = pathlib.Path(gd.__file__)


def _lambda_params(name: str = "Tools", arn: str = "arn:aws:lambda:us-east-1:1:function:f") -> dict:
    return {
        "gatewayIdentifier": GW,
        "name": name,
        "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": arn, "toolSchema": {"inlinePayload": []}}}},
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    }


class _Conflict(Exception):
    """What the control plane raises when the name is taken."""

    def __init__(self):
        super().__init__("ConflictException: Target with name Tools already exists")


class _Ctrl:
    """A control plane whose target name is already taken, by a target we describe.

    ``existing_family`` is what ``get_gateway_target`` reports, which is the only place the
    family is readable -- ``list_gateway_targets`` returns summaries with no
    ``targetConfiguration``, and a test that put one there would be testing a shape the
    service does not return.
    """

    def __init__(self, *, existing_family="lambda", list_raises=None, get_raises=None, listed_name="Tools"):
        self.existing_family = existing_family
        self.list_raises = list_raises
        self.get_raises = get_raises
        self.listed_name = listed_name
        self.applied: dict | None = None
        self.updated: list[dict] = []
        self.deleted: list[str] = []
        self.created: list[dict] = []

    def create_gateway_target(self, **kw):
        raise _Conflict()

    def list_gateway_targets(self, **kw):
        if self.list_raises is not None:
            raise self.list_raises
        return {"items": [{"name": self.listed_name, "targetId": "t-existing"}]}

    def get_gateway_target(self, **kw):
        if self.get_raises is not None:
            raise self.get_raises
        cfg = {"mcp": {self.existing_family: {}}} if self.existing_family else {}
        return {
            "status": "READY",
            "targetConfiguration": cfg,
            **(self.applied or {}),
        }

    def update_gateway_target(self, **kw):
        self.updated.append(kw)
        self.applied = {key: kw[key] for key in gd._TARGET_REPLACE_KEYS if key in kw}
        return {}

    def delete_gateway_target(self, **kw):
        self.deleted.append(kw.get("targetId"))
        return {}


# ---------------------------------------------------------------------------
# 1. Every call site asks for the update
# ---------------------------------------------------------------------------


def _target_call_sites() -> list[tuple[int, dict]]:
    """Every ``_create_gateway_target_with_retry(...)`` call in the real module.

    Enumerated from the AST rather than grepped for a flag, because the defect is a site
    that does NOT mention the flag: a grep for ``update_existing`` finds the sites that
    already comply and is blind to exactly the ones that do not.
    """
    tree = ast.parse(SOURCE.read_text())
    sites: list[tuple[int, dict]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "_create_gateway_target_with_retry":
            continue
        sites.append((node.lineno, {kw.arg: kw.value for kw in node.keywords if kw.arg}))
    return sites


def test_every_target_call_site_repoints_an_existing_target():
    """A new target family added without the flag reuses a stale target silently."""
    sites = _target_call_sites()
    # The five F-74 sites (dynamic tools x2, runtime MCP, custom tools x2) plus the five
    # F-74b ones (connector OpenAPI, external MCP, config lambda/openapi/smithy).
    assert len(sites) >= 10, f"expected at least the 10 known call sites, found {len(sites)}"

    missing = [
        lineno
        for lineno, kwargs in sites
        if not (isinstance(kwargs.get("update_existing"), ast.Constant) and kwargs["update_existing"].value is True)
    ]
    assert missing == [], (
        f"{SOURCE.name} lines {missing} create a gateway target without update_existing=True. "
        "A target that already holds the name would be REUSED as it is, so this deploy's "
        "schema, ARN or endpoint never reaches the gateway and the tool keeps serving the "
        "previous deployment's."
    )


# ---------------------------------------------------------------------------
# 2. No unproven outcome is reported as a deployed tool
# ---------------------------------------------------------------------------


def test_a_conflict_that_cannot_be_listed_is_fatal():
    ctrl = _Ctrl(list_raises=RuntimeError("ThrottlingException"))
    with pytest.raises(GatewayTargetUnproven, match="could not be listed"):
        gd._create_gateway_target_with_retry(ctrl, GW, "Tools", _lambda_params())


def test_a_conflict_whose_name_is_not_in_the_listing_is_fatal():
    """The create says the name is taken and the listing does not show it.

    Either the listing lagged the create or something removed it in between; both mean
    nothing can be said about what serves this tool. It used to return None silently.
    """
    ctrl = _Ctrl(listed_name="SomethingElse")
    with pytest.raises(GatewayTargetUnproven, match="no target with that name"):
        gd._create_gateway_target_with_retry(ctrl, GW, "Tools", _lambda_params())


def test_an_exhausted_not_ready_budget_is_fatal(monkeypatch):
    monkeypatch.setattr(gd.time, "sleep", lambda *_a: None)

    class _NeverReady(_Ctrl):
        def create_gateway_target(self, **kw):
            raise RuntimeError("Gateway is not ready yet")

    with pytest.raises(GatewayTargetUnproven, match="never became ready"):
        gd._create_gateway_target_with_retry(_NeverReady(), GW, "Tools", _lambda_params(), max_retries=2)


def test_an_external_mcp_target_with_no_id_does_not_skip_the_readiness_proof(monkeypatch):
    """The call site's own half of the fix, which nothing covered until a mutant said so.

    This was ``if target_id:`` guarding ``_wait_for_mcp_target_ready``, so the ONE outcome
    we could not identify was the one routed past the check that exists to prove the target
    connected: a wrong endpoint, key or prefix then deployed "successfully" and the agent
    simply had no tools. The raise replaced the guard, but no test drove it -- planning the
    mutation run is what surfaced that, which is the point of planning one.
    """
    waited = []
    monkeypatch.setattr(gd, "_wait_for_mcp_target_ready", lambda *a, **k: waited.append(a))
    monkeypatch.setattr(
        gd,
        "_create_gateway_target_with_retry",
        # A response shape we do not understand: named, but with no id to check.
        lambda ctrl, gw, name, params, max_retries=5, *, update_existing=False: {"name": name},
    )

    with pytest.raises(GatewayTargetUnproven, match="no targetId came back"):
        gd._deploy_external_mcp_targets(object(), GW, "us-east-1", [{"server_id": "aws-knowledge"}], owner_sub="alice")
    assert waited == [], "an unidentifiable target must not be waited on, and must not be reported as served"


def test_the_helper_has_no_none_return_left():
    """Read off the signature, so the guarantee the callers rely on is stated somewhere.

    Four callers discard this return and one branches on it. Re-widening the annotation to
    ``dict | None`` is the change that would quietly restore the old behaviour.
    """
    import inspect

    # The module has no ``from __future__ import annotations``, so these are the real
    # objects rather than strings -- asserted against ``dict`` for that reason, and it is
    # worth knowing: ``== "dict"`` passes vacuously nowhere and fails loudly here.
    assert inspect.signature(gd._create_gateway_target_with_retry).return_annotation is dict
    assert inspect.signature(gd.deploy_external_mcp_target).return_annotation is dict


# ---------------------------------------------------------------------------
# 3. Presence is not provenance
# ---------------------------------------------------------------------------


def test_a_target_of_another_family_is_not_reused():
    ctrl = _Ctrl(existing_family="openApiSchema")
    with pytest.raises(GatewayTargetFamilyConflict, match="is a openApiSchema target"):
        gd._create_gateway_target_with_retry(ctrl, GW, "Tools", _lambda_params())
    assert ctrl.updated == [] and ctrl.deleted == [], "the foreign target must be left exactly as it is"


def test_a_target_of_another_family_is_not_overwritten_by_an_update():
    """The worse half. ``UpdateGatewayTarget`` is a full replace, so adopting here does not
    serve the wrong tools -- it destroys someone else's configuration irreversibly."""
    ctrl = _Ctrl(existing_family="mcpServer")
    with pytest.raises(GatewayTargetFamilyConflict):
        gd._create_gateway_target_with_retry(ctrl, GW, "Tools", _lambda_params(), update_existing=True)
    assert ctrl.updated == [], "an update is a full replace; refusing must mean not sending it"


def test_a_read_failure_is_not_reported_as_someone_elses_target():
    """A throttle on the describe must not read as a conflict.

    Two different operator actions: a real family conflict is resolved by renaming the tool,
    an unread target by retrying. Collapsing them sends the operator to rename something
    that was never the problem.
    """
    ctrl = _Ctrl(get_raises=RuntimeError("ThrottlingException: Rate exceeded"))
    with pytest.raises(GatewayTargetUnproven, match="could not be read"):
        gd._create_gateway_target_with_retry(ctrl, GW, "Tools", _lambda_params(), update_existing=True)
    assert ctrl.updated == []


def test_the_same_family_is_updated_in_place():
    """The happy path this module's refusals must not have removed.

    A suite of refusals is compatible with refusing everything, and has been before now.
    """
    ctrl = _Ctrl(existing_family="lambda")
    # UpdateGatewayTarget's input shape has no clientToken, and gatewayIdentifier is passed
    # as its own keyword by the caller, so neither may be forwarded inside the replace
    # payload. The clientToken has to be PUT HERE to be worth asserting about: the first
    # version of this test asserted ``"clientToken" not in sent`` over params that never
    # had one, so dropping the strip left it passing. A mutant found that, not review.
    params = {**_lambda_params(), "clientToken": "idem-0001"}
    out = gd._create_gateway_target_with_retry(ctrl, GW, "Tools", params, update_existing=True)
    assert out["targetId"] == "t-existing"
    assert len(ctrl.updated) == 1
    sent = ctrl.updated[0]
    assert sent["targetId"] == "t-existing"
    assert sent["targetConfiguration"] == _lambda_params()["targetConfiguration"]
    assert "clientToken" not in sent, "UpdateGatewayTarget has no clientToken; sending one is a ValidationException"
    # gatewayIdentifier is passed separately; leaving it in the payload is a duplicate
    # keyword, which is a TypeError rather than a wrong deploy -- asserted so the reason
    # the strip covers two keys is recorded, not just the one that fails quietly.
    assert sent["gatewayIdentifier"] == GW


def test_a_target_with_no_declared_family_does_not_manufacture_a_conflict():
    """``target_family`` returning "" means "we cannot tell", never "mismatch"."""
    ctrl = _Ctrl(existing_family="")
    out = gd._create_gateway_target_with_retry(ctrl, GW, "Tools", _lambda_params())
    assert out["targetId"] == "t-existing"


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


def test_the_digest_is_independent_of_key_insertion_order():
    a = _lambda_params()
    b = {k: a[k] for k in reversed(list(a))}
    assert target_replace_digest(a) == target_replace_digest(b)


def test_the_digest_changes_when_the_target_would_be_repointed():
    a = _lambda_params(arn="arn:aws:lambda:us-east-1:1:function:one")
    b = _lambda_params(arn="arn:aws:lambda:us-east-1:1:function:two")
    assert target_replace_digest(a) != target_replace_digest(b)


def test_the_digest_ignores_the_name_and_the_gateway():
    """The name is the sharing KEY, so including it would make the digest tautological:
    every comparison is already between two requests for the same name."""
    assert target_replace_digest(_lambda_params(name="A")) == target_replace_digest(_lambda_params(name="B"))


def test_the_digest_covers_the_credential_configuration():
    """An OpenAPI target that moves from public to api_key is a different tool plane."""
    public = {"gatewayIdentifier": GW, "name": "X", "targetConfiguration": {"mcp": {"openApiSchema": {}}}}
    keyed = {**public, "credentialProviderConfigurations": [{"credentialProviderType": "API_KEY"}]}
    assert target_replace_digest(public) != target_replace_digest(keyed)


def test_the_digest_survives_a_value_json_cannot_encode():
    """A spec loader handing back a datetime must not turn a governance check into a
    TypeError raised from inside a conflict handler."""
    import datetime

    params = _lambda_params()
    params["targetConfiguration"]["mcp"]["lambda"]["loadedAt"] = datetime.datetime(2026, 9, 23)
    assert target_replace_digest(params).startswith("sha256:")


def test_the_family_is_read_from_the_configuration_not_guessed():
    assert target_family({"mcp": {"smithyModel": {"inlinePayload": "{}"}}}) == "smithyModel"
    assert target_family({"mcp": {}}) == ""
    assert target_family(None) == ""


# ---------------------------------------------------------------------------
# The records, and the manifest rows built from them
# ---------------------------------------------------------------------------


def test_an_updated_target_is_recorded_with_its_family_and_digest():
    params = _lambda_params()
    with collecting_target_records() as records:
        gd._create_gateway_target_with_retry(_Ctrl(), GW, "Tools", params, update_existing=True)
    assert records == [
        {
            "target_id": "t-existing",
            "name": "Tools",
            "family": "lambda",
            "digest": target_replace_digest(params),
            "arm": "updated",
            "source_runtime_arn": "",
            "source_runtime_id": "",
        }
    ]


def test_a_reused_target_is_recorded_too():
    """A reuse is still this deployment declaring a dependency on that target."""
    with collecting_target_records() as records:
        gd._create_gateway_target_with_retry(_Ctrl(), GW, "Tools", _lambda_params())
    assert [r["target_id"] for r in records] == ["t-existing"]


def test_a_created_target_is_recorded_only_once_it_is_ready():
    """Recording a target that then reaches FAILED would claim ownership of a broken
    target and, worse, would be the only row justifying a future delete."""

    class _Creates(_Ctrl):
        def __init__(self, status):
            super().__init__()
            self.status = status

        def create_gateway_target(self, **kw):
            self.created.append(kw)
            return {"targetId": "t-new"}

        def get_gateway_target(self, **kw):
            return {"status": self.status, "statusReasons": ["because"]}

    with collecting_target_records() as ready:
        gd._create_gateway_target_with_retry(_Creates("READY"), GW, "Tools", _lambda_params())
    assert [r["target_id"] for r in ready] == ["t-new"]

    with collecting_target_records() as failed:
        with pytest.raises(RuntimeError):
            gd._create_gateway_target_with_retry(_Creates("FAILED"), GW, "Tools", _lambda_params())
    assert failed == []


def test_recording_is_tolerant_of_no_collector():
    """The helper is called from teardown-adjacent paths and from tests. A deploy that
    cannot record its target must still deploy it: the row makes the NEXT deploy smarter,
    so a missing one degrades to today's behaviour rather than refusing anything."""
    assert gd._TARGET_RECORD_SINK.get() is None
    out = gd._create_gateway_target_with_retry(_Ctrl(), GW, "Tools", _lambda_params())
    assert out["targetId"] == "t-existing"


def test_the_sink_is_reset_even_when_the_body_raises():
    with pytest.raises(ValueError):
        with collecting_target_records():
            raise ValueError("boom")
    assert gd._TARGET_RECORD_SINK.get() is None


def test_the_manifest_row_carries_the_target_provenance():
    rows = _gateway_manifest_resources(
        "us-east-1",
        {
            "gateway_id": GW,
            "gateway_name": "agent-gateway",
            "gateway_targets": [
                {"target_id": "t-1", "name": "Tools", "family": "lambda", "digest": "sha256:abc"},
                # No target_id: nothing to own, nothing to record.
                {"target_id": "", "name": "Ghost", "family": "lambda", "digest": "sha256:def"},
            ],
        },
    )
    targets = [r for r in rows if r.get("type") == "gateway_target"]
    assert len(targets) == 1, "a record with no targetId names no resource and must not become a row"
    row = targets[0]
    assert row["id"] == "t-1"
    assert row["name"] == "Tools"
    assert row["gateway_id"] == GW
    assert row["target_family"] == "lambda"
    assert row["target_digest"] == "sha256:abc"
    assert row["region"] == "us-east-1"
    assert row["created_by_deployment"] is True
    # Part of the gateway graph: teardown leaves the whole graph while its gateway stands.
    assert row[GATEWAY_GRAPH_FIELD] is True


def test_a_gateway_target_row_deletes_nothing_yet_and_says_so():
    """Pinned deliberately, because it is a decision and not an oversight.

    The rows land one release before the refcounted delete arm that reads them. A delete
    arm shipped in the same change would run its first teardown against a population where
    almost no target has a row -- and a delete that cannot see a co-resident deployment's
    reference deletes that deployment's tool. Today the "gateway" arm removes every target
    on a gateway it is about to delete, and a gateway retained for a co-resident keeps its
    targets; this row must not change either behaviour.

    Written first as ``assert msg == ""`` on the assumption that an unrecognized type is a
    silent no-op. It is not: the dispatcher logs an ERROR and returns "still in the account
    ... delete it by hand". Every gateway-bearing teardown would have reported a leak for
    targets it had in fact just deleted, and a false leak on every teardown teaches an
    operator to ignore the true ones. Hence an explicit arm, and hence this assertion is on
    the message rather than on its absence.
    """
    from app.deployment_handler import _delete_managed_resource

    msg = _delete_managed_resource(
        {"type": "gateway_target", "id": "t-1", "gateway_id": GW, "name": "Tools"},
        "us-east-1",
        deployment_id="d-1",
    )
    assert "nothing to delete here" in msg
    assert "delete it by hand" not in msg, (
        "a recognized provenance row must not be reported to the operator as an abandoned "
        "resource; the gateway arm deletes these targets"
    )
