"""F-55: a refused deploy must cost NOTHING — no rows, no secrets, no execution.

Why this file exists rather than more validator tests. ``test_deployment_payload_validation.py``
proves the validator decides correctly; it cannot prove the endpoint CALLS it, calls it with the
right phase, calls it on the right bytes, or calls it before the side effects. Those are four
separate wiring facts and every one of them has a failure mode that leaves the validator suite
entirely green:

* not wired at all -> every payload deploys, the validator is dead code;
* wired after ``store.create`` -> a refusal leaves a DeploymentState row in PENDING and an
  AgentVersion row in "pending" that nothing will ever move, because the only thing that moves
  them is a step handler and no execution was started;
* wired on ``request.model_dump()`` -> ``ConnectorConfig.secret_value`` is
  ``Field(..., exclude=True)``, so the dump drops the one field that carries a raw connector
  credential and the scan reads clean while never having seen those bytes;
* wired with ``PayloadPhase.PREPARED`` at the API boundary -> every legitimate request is
  refused, because raw secret material at the approved write-only paths is exactly what a
  REQUEST is allowed to contain.

So every test here asserts an ORDERING or a CALL COUNT, through the real ASGI app, not a return
value. And the suite is two-sided on purpose: a file that only proved refusals would pass against
an endpoint that refuses everything, which is the failure mode that has actually shipped in this
repo before (see ``test_no_resources_created_marker.py``).

ARCC cnt_jljdNeOwgPnFx2 is the governing guidance: an authorization decision must be enforced
outside and ahead of the side-effecting path, and its Common Pitfalls section names post-hoc
filtering as non-compliant. "Rejected at PREPARED, after the rows and the secrets" is that
pitfall, which is why the ordering is the thing under test.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from app import deployment_handler as dh
from fastapi.testclient import TestClient

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
MODEL_ID = "us.anthropic.claude-sonnet-5"
ACCOUNT = "166827918465"
REGION = "us-east-1"
STATE_MACHINE = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:acfe2e-p0920-deployment"

# An obviously fake credential. It must never appear in a response body or a log line, which is
# itself asserted below -- a refusal that echoes the value it refused has leaked it to whatever
# collects the response.
FAKE_SECRET = "sk-fake-not-a-real-key-0000"


def _client(sub: str | None = OWNER) -> TestClient:
    """A TestClient carrying ``sub`` the way API Gateway's JWT authorizer delivers it.

    Injected at the ASGI layer rather than by calling the route function, so FastAPI's own
    routing and parameter binding stay in the loop. That matters specifically here: the REQUEST
    gate re-reads the body from the same ``Request`` FastAPI used to build ``DeployRequest``, and
    a harness that bypassed FastAPI would not exercise that caching at all.
    """
    # A standard user's groups even with no sub, so a missing identity is refused by the
    # ownership check under test rather than by the scope check in front of it.
    claims = {"cognito:groups": ["g-users-default"], **({"sub": sub} if sub else {})}
    event = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}

    async def _inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await dh.deployment_app(scope, receive, send)

    return TestClient(_inject)


def _connector(**extra) -> dict:
    """A connector the API genuinely accepts.

    Both required fields and a catalog-valid ``connectorId`` matter more than they look. With a
    placeholder id or a missing ``authMethod``, pydantic 422s the request before the gate under
    test ever runs -- so a refusal test would pass for the wrong reason and would keep passing
    with the gate deleted. ``ConnectorConfig`` does NOT set ``extra="forbid"``, so an unknown key
    like ``password`` reaches the handler and the raw-body scan is the only thing that sees it.
    """
    return {"connectorId": "github", "authMethod": "api_key", **extra}


def _body(**extra) -> dict:
    """A minimal request the platform genuinely accepts, as the deploy panel would send it."""
    return {
        "nodeId": "node-1",
        "config": {"name": "gatetest", "model": {"modelId": MODEL_ID}},
        **extra,
    }


class _Spy:
    """Records every side-effecting call the endpoint can make, in order.

    One shared ordered log rather than per-mock counters, because the question is not "was
    ``store.create`` called" but "was it called BEFORE the refusal". A set of counters cannot
    express that; a sequence can.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def record(self, name: str):
        def _fn(*args, **kwargs):
            self.calls.append(name)
            return MagicMock()

        return _fn

    @property
    def side_effects(self) -> list[str]:
        """Only the calls that create or persist something. Reads are not side effects."""
        return [c for c in self.calls if c != "read"]


@pytest.fixture
def spy(monkeypatch):
    """Wire every persistence and AWS boundary of ``handle_deploy`` to the ordered log."""
    s = _Spy()

    monkeypatch.setattr(dh, "STATE_MACHINE_ARN", STATE_MACHINE)
    # Pin the home region. It is otherwise whatever the developer's environment resolved (here,
    # the CLI default us-west-2), and the validator legitimately refuses a staged ARN in a region
    # other than the deployment's -- so an unpinned region makes this suite pass or fail based on
    # a shell variable rather than on the code.
    # ``AppConfig`` is a frozen dataclass, so setting the attribute raises FrozenInstanceError
    # regardless of ``raising=False`` -- replace the module global with a new instance instead.
    monkeypatch.setattr(dh, "config", replace(dh.config, aws_region=REGION))

    store = MagicMock()
    store.create.side_effect = s.record("store.create")
    store.update_status.side_effect = s.record("store.update_status")
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    s.store = store

    versions = MagicMock()
    versions.put.side_effect = s.record("versions.put")
    versions.update_status.side_effect = s.record("versions.update_status")
    versions.list_for_runtime.return_value = []
    slots = MagicMock()
    slots.get.side_effect = lambda *a, **k: (s.calls.append("read"), None)[1]

    import app.services.agent_versions_store as avs

    monkeypatch.setattr(avs, "get_versions_store", lambda: versions)
    monkeypatch.setattr(avs, "get_slots_store", lambda: slots)
    s.versions = versions
    s.slots = slots

    # Credential staging. The real function returns
    # ``(prepared_gateway, connectors, external_mcp, staged_arns)`` with every raw credential
    # replaced by a deployment-bound ARN.
    #
    # The stub MUST scrub too. A pass-through stub looks harmless and is not: the raw
    # ``secret_value`` survives into the prepared payload, PREPARED correctly refuses it, and the
    # admitting test fails with a 500 that looks like a product defect but is entirely an artefact
    # of the stub. Matching the real contract is what keeps a failure here attributable.
    def _stage(**kwargs):
        s.calls.append("stage")
        _stage.seen = kwargs
        staged: list[str] = []
        connectors = []
        for index, connector in enumerate(kwargs.get("connectors") or []):
            scrubbed = {k: v for k, v in connector.items() if k != "secret_value"}
            if connector.get("secret_value"):
                arn = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/owner/staged-{index}"
                scrubbed["secret_arn"] = arn
                staged.append(arn)
            connectors.append(scrubbed)
        return kwargs.get("gateway_config"), connectors, [], staged

    _stage.seen = {}
    monkeypatch.setattr(dh, "_prepare_deployment_credentials", _stage)
    s.stage = _stage

    monkeypatch.setattr(dh, "_cleanup_staged_credentials", s.record("compensate"))
    monkeypatch.setattr(dh, "_create_sfn_client", lambda *a, **k: MagicMock())

    def _start(*args, **kwargs):
        s.calls.append("StartExecution")
        _start.input_json = kwargs.get("input_json")
        return {"executionArn": f"arn:aws:states:{REGION}:{ACCOUNT}:execution:x:y"}

    monkeypatch.setattr(dh, "_start_sfn_execution", _start)
    monkeypatch.setattr(dh, "_update_execution_arn", lambda *a, **k: None)
    s.start = _start

    # Tag policy resolution: a table read in production. Neutral by default.
    #
    # ``resolve_governance`` is the seam, not ``resolve_tags``: P0-B moved the route onto the
    # richer call so the staleness check and the values it returns come from ONE read. A
    # MagicMock left to auto-spec here is worse than useless -- ``dict(mock.tags)`` raises,
    # the route's fail-closed ``except Exception`` turns it into a 503, and every test in this
    # file fails for a reason that has nothing to do with what it is testing. So the return
    # value is a real ``ResolvedGovernance``.
    tag_store = MagicMock()
    import app.services.tag_policy_store as tps

    #
    # The key is NAMESPACED, and that is load-bearing rather than cosmetic. This stub used to
    # return ``{"ManagedBy": "AgentCore"}``, which is now refused at the route with a 400 --
    # every test in this file failed on a tag error while testing credential gating. Two
    # separate reasons it was the wrong stub, both worth stating so it is not "fixed" back:
    # governance keys the platform will stamp on live resources must fall inside
    # ``GOVERNANCE_TAG_KEY_PREFIXES`` (the step roles' ``aws:TagKeys`` allowlists enumerate
    # exactly those namespaces), and ``ManagedBy`` is specifically the OWNERSHIP key, so a
    # governance policy able to set it is the cross-deployment ownership forgery the namespace
    # rule exists to prevent. A stub may be simple; it may not model a state the product
    # refuses to produce.
    tag_store.resolve_governance.return_value = tps.ResolvedGovernance(
        tags={"platform:application": "AgentCore"},
        policy_revision="sha256:" + "0" * 64,
    )

    monkeypatch.setattr(tps, "get_tag_policy_store", lambda: tag_store)
    s.tag_store = tag_store

    # Governance gating queries an external registry; not what this file is about.
    import app.services.aws_agent_registry as reg

    monkeypatch.setattr(reg, "unapproved_integrations", lambda idents: [])
    monkeypatch.setattr(dh, "resolve_system_prompt", lambda *a, **k: None, raising=False)

    return s


# --------------------------------------------------------------------------------------
# ADMITTING. Without these, every refusal test below is satisfied by "refuse everything".
# --------------------------------------------------------------------------------------


def test_a_legitimate_request_still_deploys(spy):
    """The happy path: 202 and exactly one execution started."""
    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 202, response.text
    assert spy.calls.count("StartExecution") == 1, (
        f"a valid request must start exactly one execution; call log was {spy.calls}"
    )
    assert "store.create" in spy.calls and "versions.put" in spy.calls


def test_the_request_gate_runs_before_every_side_effect_even_when_it_admits(spy):
    """Ordering holds on the admitting path too, which is where it is load-bearing.

    An endpoint that validated after ``store.create`` would pass the refusal tests only if it
    also compensated; asserting the order on a SUCCESSFUL deploy pins the sequence itself.
    """
    _client().post("/api/deploy", json=_body())

    order = spy.side_effects
    assert order.index("store.create") < order.index("StartExecution")
    assert order.index("versions.put") < order.index("StartExecution")
    assert order.index("stage") < order.index("StartExecution")


def test_runtime_name_admission_reads_both_ownership_oracles_strongly(spy):
    """A stale missing row is permission to collide, so both reads are strong."""
    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 202, response.text
    spy.slots.get.assert_called_once_with("gatetest", consistent=True)
    spy.versions.list_for_runtime.assert_called_once_with("gatetest", consistent=True)


@pytest.mark.parametrize("oracle", ["slots", "versions"])
def test_an_unreadable_name_ownership_oracle_fails_closed_before_persistence(spy, oracle):
    """Unknown ownership is a 503, never the old 'treating as first deploy' path."""
    raw_error = "AccessDeniedException request carried tenant-sensitive-values"
    if oracle == "slots":
        spy.slots.get.side_effect = RuntimeError(raw_error)
    else:
        spy.versions.list_for_runtime.side_effect = RuntimeError(raw_error)

    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 503, response.text
    assert spy.side_effects == [], spy.calls
    assert raw_error not in response.text
    assert "StartExecution" not in spy.calls and "stage" not in spy.calls


def _refuse_version_put(spy, exc: Exception) -> None:
    spy.calls.append("versions.put")
    raise exc


def _assert_admission_refusal_costs_no_aws_work(spy) -> None:
    assert "store.create" in spy.calls, "the already-created state is the record that gets settled"
    assert "store.update_status" in spy.calls, "the pending state must not be left pending forever"
    assert "stage" not in spy.calls
    assert "StartExecution" not in spy.calls
    assert "compensate" not in spy.calls, "nothing was staged, so compensation must have nothing to do"


def test_the_atomic_name_claim_race_is_a_sanitized_409_before_staging(spy):
    """The transaction, not the preflight read, is the authoritative ownership decision."""
    from app.services.agent_versions_store import NameClaimConflict

    raw = "TransactionCanceledException owner_sub=sub-other request=secret-payload"
    spy.versions.put.side_effect = lambda *a, **k: _refuse_version_put(spy, NameClaimConflict(raw))

    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 409, response.text
    _assert_admission_refusal_costs_no_aws_work(spy)
    assert raw not in response.text and "sub-other" not in response.text
    args = spy.store.update_status.call_args.args
    assert args[1] is dh.DeploymentStatusEnum.FAILED


def test_a_version_store_outage_is_a_sanitized_503_before_staging(spy):
    """Version persistence is the claim used by invoke/promote/delete, not optional metadata."""
    raw = "ProvisionedThroughputExceededException request=tenant-sensitive-values"
    spy.versions.put.side_effect = lambda *a, **k: _refuse_version_put(spy, RuntimeError(raw))

    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 503, response.text
    _assert_admission_refusal_costs_no_aws_work(spy)
    assert raw not in response.text
    assert spy.store.update_status.call_args.kwargs["error_details"].endswith("RuntimeError")


def test_a_failed_state_settlement_cannot_reopen_the_name_claim_refusal(spy):
    """The secondary write is best-effort; the primary 409 and zero-AWS posture survive."""
    from app.services.agent_versions_store import NameClaimConflict

    spy.versions.put.side_effect = lambda *a, **k: _refuse_version_put(
        spy,
        NameClaimConflict("transaction request must never be returned"),
    )

    def _settle_then_fail(*_a, **_k):
        spy.calls.append("store.update_status")
        raise RuntimeError("state table unavailable")

    spy.store.update_status.side_effect = _settle_then_fail

    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 409, response.text
    _assert_admission_refusal_costs_no_aws_work(spy)
    assert "state table unavailable" not in response.text


def test_a_raw_connector_credential_is_admitted_and_reaches_staging(spy):
    """The field ``model_dump`` drops must survive to staging, and must be SEEN by the gate.

    ``ConnectorConfig.secret_value`` is ``Field(..., exclude=True)``. If the REQUEST gate had
    been built on the dump it would not see this value -- and the give-away is not that the
    request is refused (it is not, this is an approved write-only path) but that the gate could
    not possibly have refused a bad one. So this test pairs with
    ``test_a_credential_at_an_unapproved_path_is_refused_before_anything_is_created``: the same
    bytes, admitted at the approved path and refused one key over.
    """
    body = _body(connectors=[_connector(secretValue=FAKE_SECRET)])
    response = _client().post("/api/deploy", json=body)

    assert response.status_code == 202, response.text
    staged = spy.stage.seen.get("connectors") or []
    assert staged and staged[0].get("secret_value") == FAKE_SECRET, (
        "the approved write-only credential must reach staging unaltered; if it is missing, the "
        "handler is passing a model_dump that excluded it"
    )


# --------------------------------------------------------------------------------------
# REFUSALS. Each asserts the COST of the refusal, not just its status code.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,why",
    [
        (
            _body(config={"name": "gatetest", "model": {"modelId": MODEL_ID}, "apiKey": FAKE_SECRET}),
            "a raw credential on config, which is not an approved write-only path",
        ),
        (
            _body(connectors=[_connector(password=FAKE_SECRET)]),
            "a raw credential under a non-approved connector key",
        ),
    ],
)
def test_a_credential_at_an_unapproved_path_is_refused_before_anything_is_created(spy, body, why):
    """The substantive assertion is the EMPTY side-effect log, not the 4xx."""
    response = _client().post("/api/deploy", json=body)

    assert response.status_code in (400, 422), f"{why}: {response.status_code} {response.text}"
    assert spy.side_effects == [], (
        f"a request refused for {why} must create nothing; these calls happened anyway: {spy.side_effects}"
    )


def test_the_refusal_does_not_echo_the_credential_it_refused(spy):
    """A refusal that quotes the value has published it to whatever reads the response."""
    body = _body(config={"name": "gatetest", "model": {"modelId": MODEL_ID}, "apiKey": FAKE_SECRET})
    response = _client().post("/api/deploy", json=body)

    assert response.status_code in (400, 422)
    assert FAKE_SECRET not in response.text, (
        "the refusal echoed the credential back to the caller; the response body is logged by "
        "API Gateway access logs and by the browser, so this is a disclosure"
    )
    # The field path IS named -- a refusal that will not say which key was wrong is unactionable.
    assert "apiKey" in response.text or "api_key" in response.text, response.text


def test_an_unauthenticated_request_creates_nothing(spy):
    """A route-authorizer miswire must cost zero rows, zero secrets and zero executions.

    Without the identity gate this request reached ``store.create`` and wrote rows whose
    ``owner_sub`` was the empty string: unowned by any tenant, invisible to every owner filter,
    and unreachable by the owner-checked delete paths.
    """
    response = _client(sub=None).post("/api/deploy", json=_body())

    assert response.status_code == 401, response.text
    assert spy.side_effects == [], f"an unauthenticated deploy must create nothing; got {spy.side_effects}"


def test_a_blank_subject_is_refused_like_a_missing_one(spy):
    """Whitespace is not an identity. ``owner_sub=" "`` is as unowned as ``owner_sub=""``."""
    response = _client(sub="   ").post("/api/deploy", json=_body())

    assert response.status_code == 401, response.text
    assert spy.side_effects == []


def test_a_required_tag_violation_no_longer_strands_two_rows(spy):
    """The reorder: tag resolution ran BELOW both writes, so its 400 left them forever.

    Nothing ever moves a PENDING DeploymentState or a "pending" AgentVersion except a step
    handler, and no execution is started on this path -- so before the reorder every missing
    required tag permanently added two rows the operator had to reconcile by hand.
    """
    from app.services.tag_policy_store import TagResolutionError

    spy.tag_store.resolve_governance.side_effect = TagResolutionError("missing required tag: CostCenter")

    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 400, response.text
    assert "CostCenter" in response.text
    assert spy.side_effects == [], f"a required-tag refusal must not persist anything; got {spy.side_effects}"


def test_a_non_tag_resolution_failure_now_refuses_instead_of_deploying_untagged(spy):
    """P0-B REVERSED this one. It used to assert 202, and that was the defect.

    The old control read "a missing table must NOT block deploys", and it was satisfied by a
    handler that caught every non-``TagResolutionError`` from the store, logged "Tag
    resolution skipped (non-fatal)" and deployed with ``resource_tags`` empty. Which means a
    throttle, a missing IAM grant or an absent table produced UNTAGGED resources and an HTTP
    202 -- required tags were only required when the store happened to answer, and a control
    that opens on error is not a control.

    So the tolerance is gone deliberately and this test now pins the opposite: 503, and
    nothing persisted. The concern the old test encoded (a fresh stack with no tag-policy
    table must still be able to deploy) is real, and it is handled by the table existing in
    every emitted stack rather than by opening the gate when it does not.
    """
    spy.tag_store.resolve_governance.side_effect = RuntimeError("ResourceNotFoundException: no table")

    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 503, response.text
    assert spy.side_effects == [], f"a store outage must persist nothing; got {spy.side_effects}"
    assert "Nothing was created" in response.text
    # The refusal must not republish the store's own error text: a botocore message echoes the
    # request parameters, and those carry the caller's tag VALUES.
    assert "ResourceNotFoundException" not in response.text


# --------------------------------------------------------------------------------------
# The PREPARED gate: OUR bug, not the caller's, and the last point at which it is free.
# --------------------------------------------------------------------------------------


def test_a_prepared_payload_we_built_wrongly_starts_no_execution(spy):
    """Simulates staging failing to replace a raw value, and asserts full compensation.

    Patched at ``_prepare_deployment_credentials`` because that is the real seam: its whole job
    is to replace raw material with staged ARNs, and "it silently did not" is the defect the
    PREPARED phase exists to catch. A staged ARN in another account would do equally well.
    """
    staged_arn = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/me/s-1"

    def _bad_stage(**kwargs):
        spy.calls.append("stage")
        # A gateway config still carrying a raw credential after staging "succeeded".
        return {"name": "gw", "apiKey": FAKE_SECRET}, [], [], [staged_arn]

    with patch.object(dh, "_prepare_deployment_credentials", _bad_stage):
        response = _client().post("/api/deploy", json=_body(gatewayConfig={"name": "gw"}))

    assert response.status_code == 500, response.text
    assert "StartExecution" not in spy.calls, (
        "a payload that fails PREPARED validation must not reach Step Functions; the in-SFN "
        f"gate would then refuse it with the secrets already staged. Log: {spy.calls}"
    )
    assert "compensate" in spy.calls, (
        "the staged secrets must be deleted; nothing else is ever triggered to clean them up, "
        "because no execution ran and so no manifest-driven cleanup exists"
    )
    assert "versions.update_status" in spy.calls, (
        "the pending AgentVersion row must be marked failed, or the version history shows a "
        "version that is pending forever"
    )
    assert FAKE_SECRET not in response.text


def test_the_prepared_gate_validates_the_exact_dict_that_is_sent(spy):
    """Pins the coupling that makes a PREPARED pass here a guarantee for the in-SFN check.

    If this endpoint validated a reconstruction rather than ``sfn_input`` itself, the two could
    disagree -- and the disagreement surfaces as a deployment that dies at ValidateWorkflow with
    the execution already running and the secrets already staged. Asserting that the validated
    object IS the serialized one is the only way to rule that out from the outside.
    """
    import json

    seen: list[dict] = []
    real = dh.validate_deployment_payload

    def _spy_validate(payload, *, phase, context):
        if phase is dh.PayloadPhase.PREPARED:
            seen.append(payload)
        return real(payload, phase=phase, context=context)

    with patch.object(dh, "validate_deployment_payload", _spy_validate):
        response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 202, response.text
    assert len(seen) == 1, f"PREPARED must be validated exactly once, saw {len(seen)}"
    sent = json.loads(spy.start.input_json)
    assert seen[0] == sent, (
        "the dict validated at PREPARED is not the dict sent to StartExecution; a pass here "
        "therefore does not guarantee the state machine's own gate passes"
    )


def test_both_phases_run_and_in_the_right_order(spy):
    """REQUEST then PREPARED, exactly once each, and REQUEST before any side effect.

    Kills the mutant that wires ONE call doing double duty: PREPARED at the boundary refuses
    every legitimate request (raw material at an approved path is allowed pre-staging), and
    REQUEST on the prepared payload cannot see a staging failure at all.
    """
    phases: list[str] = []
    real = dh.validate_deployment_payload

    def _spy_validate(payload, *, phase, context):
        phases.append(phase.value if hasattr(phase, "value") else str(phase))
        spy.calls.append(f"validate:{phases[-1]}")
        return real(payload, phase=phase, context=context)

    with patch.object(dh, "validate_deployment_payload", _spy_validate):
        response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 202, response.text
    assert len(phases) == 2 and phases[0] != phases[1], (
        f"expected one REQUEST and one PREPARED validation, got {phases}"
    )
    order = spy.calls
    assert order.index(f"validate:{phases[0]}") < order.index("store.create"), (
        "the REQUEST validation must precede the first persisted row"
    )
    assert order.index(f"validate:{phases[1]}") > order.index("stage"), (
        "the PREPARED validation must run after staging, or it cannot see a staging failure"
    )
    assert order.index(f"validate:{phases[1]}") < order.index("StartExecution")


def test_the_validation_context_is_not_taken_from_the_payload(spy):
    """The account a staged ARN is checked against must not be caller-influenced.

    A request naming its own account would otherwise authorize its own cross-account secret --
    cnt_QAWqFk4LdKNGAO's named threat. The context comes from ``STATE_MACHINE_ARN``, which the
    platform's CDK sets and no request can reach.
    """
    contexts: list[object] = []
    real = dh.validate_deployment_payload

    def _spy_validate(payload, *, phase, context):
        contexts.append(context)
        return real(payload, phase=phase, context=context)

    with patch.object(dh, "validate_deployment_payload", _spy_validate):
        _client().post("/api/deploy", json=_body())

    assert contexts, "no validation ran at all"
    for ctx in contexts:
        assert ctx.home_account_id == ACCOUNT, (
            f"expected the account from STATE_MACHINE_ARN, got {ctx.home_account_id!r}"
        )


def test_a_malformed_state_machine_arn_weakens_one_check_and_breaks_nothing(spy, monkeypatch):
    """Absent context must not be an outage. It weakens the account pin only."""
    monkeypatch.setattr(dh, "STATE_MACHINE_ARN", "")

    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 202, response.text
    assert spy.calls.count("StartExecution") == 1


# --------------------------------------------------------------------------------------
# Flow identity (F-55 finding A). ``flowId`` and ``nodeId`` are DIFFERENT objects, so every
# admitting test here uses UNEQUAL values: with equal ones, a handler that still wrote the node
# id into ``workflow_id`` would pass them all, and nothing would distinguish the two fields.
# --------------------------------------------------------------------------------------

import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

FLOW = "7d1e0c2a-5b9f-4f7e-8a31-2c6d9e0b4a17"
NODE = "agentcore-node-42"
OTHER = "0c0ffee0-0000-4000-8000-000000000000"


class _FlowStore:
    """A flows table with fixed contents, recording every read."""

    def __init__(self, flows: dict | None = None, error: Exception | None = None) -> None:
        self.flows = flows or {}
        self.error = error
        self.reads: list[str] = []

    def get(self, flow_id):
        self.reads.append(flow_id)
        if self.error is not None:
            raise self.error
        return self.flows.get(flow_id)


@pytest.fixture
def flows(monkeypatch):
    """The caller owns FLOW; nothing else exists unless a test adds it."""
    store = _FlowStore({FLOW: SimpleNamespace(owner_sub=OWNER)})
    monkeypatch.setattr(dh, "_flow_store_for_ownership", lambda: store)
    return store


def _persisted_state(spy):
    assert spy.store.create.call_count == 1, spy.calls
    return spy.store.create.call_args.args[0]


def _sfn_input(spy) -> dict:
    return json.loads(spy.start.input_json)


def test_unequal_flow_and_node_ids_survive_to_the_state_row_and_the_execution(spy, flows):
    """API -> DeploymentState -> SFN input, each id in its own field, neither substituted."""
    response = _client().post("/api/deploy", json=_body(nodeId=NODE, flowId=FLOW))

    assert response.status_code == 202, response.text
    state = _persisted_state(spy)
    assert (state.workflow_id, state.node_id) == (FLOW, NODE), (
        f"the row must file the deployment under the FLOW and keep the node separately; got "
        f"workflow_id={state.workflow_id!r}, node_id={state.node_id!r}"
    )
    sent = _sfn_input(spy)
    assert (sent["workflow_id"], sent["node_id"]) == (FLOW, NODE), sent
    assert flows.reads == [FLOW], "the named flow must be owner-checked exactly once"


def test_the_flow_owner_check_runs_before_every_side_effect(spy, flows, monkeypatch):
    """Ordering on the ADMITTING path: the read precedes the first write."""
    order: list[str] = []
    real_get = flows.get

    def _get(flow_id):
        order.append("flow.read")
        return real_get(flow_id)

    monkeypatch.setattr(flows, "get", _get)
    spy.store.create.side_effect = lambda *a, **k: order.append("store.create")

    _client().post("/api/deploy", json=_body(nodeId=NODE, flowId=FLOW))

    assert order[:2] == ["flow.read", "store.create"], order


def test_a_deploy_that_names_no_flow_still_deploys_and_reads_no_flow(spy, flows):
    """The harness / unsaved-canvas branch: absence is representable, and is not the node id."""
    response = _client().post("/api/deploy", json=_body(nodeId=NODE))

    assert response.status_code == 202, response.text
    assert flows.reads == [], "no flow was named, so no ownership lookup is warranted"
    state = _persisted_state(spy)
    assert state.workflow_id is None and state.node_id == NODE, (
        f"an absent flow must stay absent, never be backfilled with the node id; got workflow_id={state.workflow_id!r}"
    )
    sent = _sfn_input(spy)
    assert sent["workflow_id"] is None and sent["node_id"] == NODE, sent


@pytest.mark.parametrize(
    "flow_id,contents,why",
    [
        (FLOW, {FLOW: SimpleNamespace(owner_sub=OTHER)}, "another tenant's flow"),
        (FLOW, {}, "a flow that does not exist"),
        (FLOW, {FLOW: SimpleNamespace(owner_sub=None)}, "a legacy flow with no owner"),
    ],
)
def test_a_flow_the_caller_does_not_own_is_refused_before_anything_is_created(spy, monkeypatch, flow_id, contents, why):
    """404 for all three, so the response cannot tell a foreign flow from an absent one."""
    monkeypatch.setattr(dh, "_flow_store_for_ownership", lambda: _FlowStore(contents))

    response = _client().post("/api/deploy", json=_body(nodeId=NODE, flowId=flow_id))

    assert response.status_code == 404, f"{why}: {response.status_code} {response.text}"
    assert spy.side_effects == [], f"{why} must create nothing; got {spy.side_effects}"
    assert OTHER not in response.text


@pytest.mark.parametrize(
    "store,why",
    [
        (_FlowStore(error=RuntimeError("AccessDeniedException")), "an unreadable flows table"),
        (None, "no flows table configured in Lambda"),
    ],
)
def test_unprovable_ownership_fails_closed(spy, monkeypatch, store, why):
    """A read failure is NOT permission. 503, and nothing created."""
    monkeypatch.setattr(dh, "_flow_store_for_ownership", lambda: store)

    response = _client().post("/api/deploy", json=_body(nodeId=NODE, flowId=FLOW))

    assert response.status_code == 503, f"{why}: {response.status_code} {response.text}"
    assert spy.side_effects == [], f"{why}: {spy.side_effects}"
    assert "AccessDeniedException" not in response.text


@pytest.mark.parametrize("bad", ["../etc", "a" * 129, "has space", "x.y"])
def test_a_malformed_flow_id_is_refused_at_the_model(spy, flows, bad):
    """The model's grammar matches ``routers/flows.py::_validate_flow_id``; nothing is read."""
    response = _client().post("/api/deploy", json=_body(nodeId=NODE, flowId=bad))

    assert response.status_code == 422, response.text
    assert flows.reads == [] and spy.side_effects == []


# --------------------------------------------------------------------------------------
# One identity resolution. ``_get_user_id`` swallows every exception and returns None, so two
# resolutions could authorize one subject and persist another.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("second", [None, OTHER], ids=["then-none", "then-other"])
def test_the_authorized_subject_is_the_one_that_is_persisted(spy, monkeypatch, second):
    """A second answer from the resolver must be unreachable, whatever it would have been."""
    resolver = MagicMock(side_effect=[OWNER, second])
    monkeypatch.setattr(dh, "_get_user_id", resolver)

    response = _client().post("/api/deploy", json=_body(nodeId=NODE))

    assert response.status_code == 202, response.text
    assert resolver.call_count == 1, f"identity resolved {resolver.call_count} times"
    assert _persisted_state(spy).user_id == OWNER
    assert _sfn_input(spy)["owner_sub"] == OWNER


# --------------------------------------------------------------------------------------
# The store behind the owner check. The deployment Lambda never imports ``main.py``, so the
# process-wide flow storage there is the empty in-memory store; the check must not use it.
# --------------------------------------------------------------------------------------


def test_the_owner_check_opens_the_configured_flows_table(monkeypatch):
    opened = []

    # ``app.services`` re-exports an INSTANCE named ``flow_storage`` that shadows the submodule,
    # so ``import app.services.flow_storage as fs`` binds the instance. Patch the real module.
    import app.services.flow_storage  # noqa: F401

    fs = sys.modules["app.services.flow_storage"]
    monkeypatch.setattr(fs, "DynamoDBFlowStorage", lambda **kw: opened.append(kw) or "ddb")
    monkeypatch.setattr(dh, "config", replace(dh.config, dynamodb_flows_table_name="t-flows", aws_region=REGION))

    assert dh._flow_store_for_ownership() == "ddb"
    assert opened == [{"table_name": "t-flows", "region": REGION}]


def test_in_lambda_without_a_flows_table_there_is_no_store_to_trust(monkeypatch):
    monkeypatch.setattr(dh, "config", replace(dh.config, dynamodb_flows_table_name=None))
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "acfe2e-p0920-deployment")

    assert dh._flow_store_for_ownership() is None


# --------------------------------------------------------------------------------------
# GET /api/deployments: the flow index narrows the caller's rows; it never replaces the caller.
# --------------------------------------------------------------------------------------


def test_listing_by_flow_without_an_identity_is_refused(monkeypatch):
    store = MagicMock()
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)

    response = _client(sub=None).get(f"/api/deployments?workflow_id={FLOW}")

    assert response.status_code == 401, response.text
    store.query_by_workflow.assert_not_called()


def test_listing_by_flow_returns_only_the_callers_rows(monkeypatch):
    from datetime import datetime, timezone

    from app.models.deployment_models import DeploymentState, DeploymentStatusEnum

    def _row(dep, owner):
        return DeploymentState(
            deployment_id=dep,
            workflow_id=FLOW,
            node_id=NODE,
            user_id=owner,
            status=DeploymentStatusEnum.SUCCEEDED,
            started_at=datetime.now(timezone.utc),
        )

    store = MagicMock()
    store.query_by_workflow.return_value = [_row("mine", OWNER), _row("theirs", OTHER)]
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)

    response = _client().get(f"/api/deployments?workflow_id={FLOW}")

    assert response.status_code == 200, response.text
    assert [r["deployment_id"] for r in response.json()] == ["mine"]
