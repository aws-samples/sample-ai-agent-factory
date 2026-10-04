"""P0-B governance tags must reach the AWS resources, not only the response and the export.

Measured live on ``acfe2e-p0920`` before any of this existed: a governed deploy resolved three
platform tags, wrote them into the deployment record, and carried them in EVERY Step Functions
state input -- and produced a runtime tagged ``{ManagedBy, AgentCoreStack}`` and nothing else.
Of the ~40 ``owner_tags``/``owner_tag_list`` call sites, exactly one forwarded the governance
set, and it was the runtime EXEC ROLE: the single resource in the deployment that cannot appear
in a cost report. ``runtime_deployer`` states the feature's purpose as "cost attribution + ABAC
work off real AWS resource tags".

The reason no test caught it is worth keeping in view: ``create_agent_runtime`` had no
``resource_tags`` PARAMETER, so there was no argument for a test to assert on and no call site
that looked wrong. The tests here therefore assert on what the AWS API actually receives.
"""

import ast
import pathlib
import uuid
from unittest.mock import MagicMock

import pytest
from app.services import resource_tagging as rt
from app.services.resource_tagging import GovernanceTagError

REGION = "us-east-1"
GOV = {"platform:application": "payments", "platform:owner": "team-a"}


@pytest.fixture(autouse=True)
def _ownership_env(monkeypatch):
    """``stack_id`` refuses to invent an identity, so every test must supply one.

    Deliberately NOT the live stack's values: a test that impersonated a real deployment would
    be asserting about deletion authority it does not have.
    """
    monkeypatch.setenv("PROJECT_NAME", "unit-proj")
    monkeypatch.setenv("ENVIRONMENT", "unit-env")
    monkeypatch.delenv("ENVIRONMENT_NAME", raising=False)


# --------------------------------------------------------------------------------------
# The merge
# --------------------------------------------------------------------------------------


def test_the_governance_set_and_the_ownership_set_both_survive_the_merge():
    tags = rt.governed_tags(REGION, GOV)
    assert tags["platform:application"] == "payments"
    assert tags["platform:owner"] == "team-a"
    # Both ownership keys, or teardown cannot identify the resource at all.
    assert tags["ManagedBy"] == "agentcore-flows"
    assert tags["AgentCoreStack"] == "unit-proj-unit-env-us-east-1"


def test_a_governance_tag_named_after_an_ownership_key_is_refused_outright():
    """The namespace gate answers this before the merge does, and the refusal is the better
    answer: an operator who typed ``AgentCoreStack`` into the deploy panel reads a sentence
    instead of silently getting a deployment whose tag was dropped."""
    for hostile_key in ("ManagedBy", "AgentCoreStack"):
        with pytest.raises(GovernanceTagError) as excinfo:
            rt.governed_tags(REGION, {hostile_key: "somebody-else"})
        assert "outside the tag namespaces" in str(excinfo.value)


def test_a_governance_tag_cannot_displace_either_ownership_key(monkeypatch):
    """The two keys every teardown gate reads must be unforgeable from the deploy panel.

    A caller who could set ``AgentCoreStack`` could make a resource read as another stack's --
    which is either a leak (ours looks foreign, so teardown refuses it forever) or the reverse
    (another stack's looks like ours). ``owner_tags`` applies them last; this pins that it stays
    true through ``governed_tags``, which is the function the call sites now use.

    The namespaces are widened here ON PURPOSE, to a value no stack deploys. The point is that
    the ordering guarantee is what protects these two keys, NOT the namespace gate: the gate is
    an env-configurable bound (``GOVERNANCE_TAG_KEY_PREFIXES``, set by the platform's CDK) and
    performs no shape check on what it is given, so an operator widening it must not be able to
    turn a tag field into an ownership rewrite. Testing this through the default namespaces
    would only re-measure the refusal above.
    """
    monkeypatch.setenv(rt.GOVERNANCE_TAG_KEY_PREFIXES_ENV, "platform:,ManagedBy,AgentCoreStack")
    hostile = {"ManagedBy": "somebody-else", "AgentCoreStack": "other-proj-prod-us-east-1"}
    tags = rt.governed_tags(REGION, hostile)
    assert tags["ManagedBy"] == "agentcore-flows"
    assert tags["AgentCoreStack"] == "unit-proj-unit-env-us-east-1"


def test_product_internal_tags_win_over_a_governance_tag_of_the_same_name(monkeypatch):
    """``extra`` carries keys read back as ownership evidence (DeploymentId, OwnerSubHash).

    A governance tag able to displace one would let the deploy panel rewrite the provenance a
    later teardown reads. Same reasoning as above for the widened namespaces: with the deployed
    value these keys never reach the merge at all, and the precedence rule still has to hold for
    the case where they do.
    """
    monkeypatch.setenv(rt.GOVERNANCE_TAG_KEY_PREFIXES_ENV, "platform:,DeploymentId")
    tags = rt.governed_tags(REGION, {"DeploymentId": "spoofed", **GOV}, {"DeploymentId": "real-dep-id"})
    assert tags["DeploymentId"] == "real-dep-id"
    assert tags["platform:application"] == "payments"


def test_no_governance_tags_is_exactly_the_old_owner_tags_behaviour():
    """The control the "refusal-only suite hides a dead happy path" lesson demands, inverted:
    prove the new function is a no-op when there is nothing to govern, so it can be dropped in
    at a call site without changing an ungoverned deploy."""
    from app.services.resource_ownership import owner_tags

    assert rt.governed_tags(REGION, None) == owner_tags(REGION)
    assert rt.governed_tags(REGION, {}) == owner_tags(REGION)


# --------------------------------------------------------------------------------------
# The validation, which the live path did not have at all
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "because"),
    [
        ({"aws:cost-center": "x"}, "reserved 'aws:' prefix"),
        ({"api_key": "AKIAsomething"}, "designates credential material"),
        ({"platform:apiKey": "x"}, "designates credential material"),
        ({"platform:api-key": "x"}, "designates credential material"),
        ({"k" * 129: "x"}, "AWS accepts 1 to 128"),
        ({"platform:app": "v" * 257}, "AWS accepts"),
        ({"platform:app!": "x"}, "character AgentCore rejects"),
        ({"platform:app": "cost,centre"}, "character AgentCore rejects"),
        ({"platform:app": "\x00"}, "character AgentCore rejects"),
        ({f"k{n}": "v" for n in range(51)}, "at most 50"),
    ],
)
def test_the_live_path_now_refuses_the_tags_the_export_path_always_refused(bad, because):
    """Before this, these reached ``iam_client.create_role(Tags=...)`` unvalidated.

    The credential rows are the ones that mattered: a key named ``api_key`` with a secret pasted
    into its value was written verbatim onto a live IAM role, while the CloudFormation export
    refused the identical tag set with a message explaining why. Same input, two answers.
    """
    with pytest.raises(GovernanceTagError, match=because):
        rt.governed_tags(REGION, bad)


def test_whitespace_including_a_newline_is_accepted_because_aws_accepts_it():
    """A control on the boundary, written after a test of mine asserted the opposite.

    The character class is AWS's own ``TagsMap`` pattern and ``\\s`` is part of it, so a tab or a
    newline inside a tag value is legal to the service. This validator deliberately does not
    invent a stricter rule than the API it is protecting: a refusal AWS would not have made is
    an outage we caused, and the export path has always accepted these. Pinning it means a
    future tightening has to be a deliberate edit to this assertion rather than a silent
    behaviour change on both paths at once.
    """
    tags = rt.validated_governance_tags({"platform:app": "two\nlines", "platform:owner": "a\tb"})
    assert tags["platform:app"] == "two\nlines"
    assert tags["platform:owner"] == "a\tb"


def test_a_malformed_value_is_never_echoed_in_the_refusal():
    """A bad value is exactly where a caller may have pasted a credential, and this error text
    can reach a UI and a log. The KEY is named so the operator can find it; the value is not."""
    secretish = "hunter2\x00-not-a-real-secret"
    with pytest.raises(GovernanceTagError) as err:
        rt.governed_tags(REGION, {"platform:app": secretish})
    assert "platform:app" in str(err.value)
    assert secretish not in str(err.value)
    assert "hunter2" not in str(err.value)


def test_fifty_governance_tags_plus_the_two_ownership_tags_is_refused_as_fifty_two():
    """The ceiling is per RESOURCE, so it has to be checked on the merged set. Validating only
    the caller's half accepts 50 and then sends 52, which AWS rejects at create time -- mid
    deploy, with some resources already made."""
    fifty = {f"platform:k{n}": "v" for n in range(50)}
    assert len(rt.validated_governance_tags(fifty)) == 50  # the caller's half is legal
    with pytest.raises(GovernanceTagError, match="52 tags would be applied"):
        rt.governed_tags(REGION, fifty)


def test_the_export_path_still_raises_its_own_error_type_with_the_same_text():
    """``_validated_tags`` now delegates here. Existing callers catch
    ``CfnExportUnsupportedError`` and existing tests assert on the message, so the translation
    has to preserve both."""
    from app.services.cfn_template_generator import CfnExportUnsupportedError, _validated_tags

    with pytest.raises(CfnExportUnsupportedError, match="reserved 'aws:' prefix"):
        _validated_tags({"aws:x": "y"})
    assert _validated_tags({"platform:app": "ok"}) == {"platform:app": "ok"}


# --------------------------------------------------------------------------------------
# The namespace gate: the second half of a live regression
#
# Adding ``tags=`` to CreateAgentRuntime denied every governed deploy on ``acfe2e-p0920``:
#
#     AccessDeniedException ... not authorized to perform: bedrock-agentcore:TagResource on
#     resource: arn:aws:bedrock-agentcore:us-east-1:...:runtime/*
#
# with the action granted and the resource matching. The step roles carry
# ``ForAllValues:StringEquals aws:TagKeys: [ManagedBy, AgentCoreStack]`` as a deliberate
# tripwire, and a governance key is another key. The IAM half now admits the governance
# NAMESPACES (``infra/stacks/platform/config.py::GOVERNANCE_TAG_KEY_PREFIXES``, asserted against
# every grant in ``infra/tests/test_governance_tag_namespace_grant.py``); these are the backend
# half, which refuses an out-of-namespace key at the API boundary so a key IAM will reject is
# never first discovered by a half-built deployment.
#
# The bound was kept rather than dropped because ARCC ``cnt_L4ZLZgjrCctfxl`` lists create/update
# tags among the operations leveraged for privilege escalation and ``cnt_6gBImtb08AJqCB`` gives
# the mechanism: tags carry ABAC decisions. ``cnt_SaTYaDCgBBJTcv`` describes the failure we hit.
# --------------------------------------------------------------------------------------


def test_an_out_of_namespace_key_is_refused_on_the_live_path_but_not_on_the_export_path():
    """The asymmetry is deliberate and is the thing most likely to be "fixed" by mistake.

    A live deploy is made by THIS platform's step roles, whose ``aws:TagKeys`` allowlist names
    the namespaces, so an out-of-namespace key is an AccessDenied waiting to happen. An exported
    template is deployed by the RECIPIENT under their own role, so refusing their tag namespace
    would be inventing a constraint their account does not have. Same validator underneath, one
    extra rule on the live path only.
    """
    out_of_namespace = {"CostCenter": "cc-1234"}
    with pytest.raises(GovernanceTagError, match="outside the tag namespaces"):
        rt.stampable_governance_tags(out_of_namespace)
    with pytest.raises(GovernanceTagError, match="outside the tag namespaces"):
        rt.governed_tags(REGION, out_of_namespace)
    # The export path's validator accepts it, and that is not an oversight.
    assert rt.validated_governance_tags(out_of_namespace) == {"CostCenter": "cc-1234"}


def test_the_refusal_names_the_key_the_namespaces_and_the_knob_but_never_the_value():
    """An operator has to be able to act on this without reading the source, and a tag VALUE can
    be pasted credential material, so it is never repeated back (the same rule the character-class
    refusal follows)."""
    with pytest.raises(GovernanceTagError) as err:
        rt.governed_tags(REGION, {"CostCenter": "hunter2-not-a-real-secret"})
    message = str(err.value)
    assert "'CostCenter'" in message
    assert "platform:" in message, "the operator cannot rename the key without being told the namespace"
    assert rt.GOVERNANCE_TAG_KEY_PREFIXES_ENV in message, "the widening knob is not discoverable from the message"
    assert "hunter2" not in message


def test_the_deployed_namespaces_come_from_the_environment_not_from_this_source_file(monkeypatch):
    """The value is set by the platform's CDK from the SAME constant that builds the IAM
    allowlist, so the backend must read it rather than hold its own copy: a hardcoded list would
    accept keys the deployed policy denies the moment the two differ."""
    monkeypatch.setenv(rt.GOVERNANCE_TAG_KEY_PREFIXES_ENV, "platform:,acme:")
    assert rt.governance_tag_key_prefixes() == ("platform:", "acme:")
    assert rt.stampable_governance_tags({"acme:cost-center": "cc-1"}) == {"acme:cost-center": "cc-1"}
    # Widened, not replaced-by-accident: the platform namespace still works.
    assert rt.stampable_governance_tags({"platform:owner": "team-a"}) == {"platform:owner": "team-a"}
    # And a key outside the widened set is still refused, so the parse did not degrade to "any".
    with pytest.raises(GovernanceTagError, match="outside the tag namespaces"):
        rt.stampable_governance_tags({"other:cost-center": "cc-1"})


@pytest.mark.parametrize("value", [None, "", "   ", ",", " , "])
def test_an_absent_or_empty_value_falls_back_to_the_two_default_namespaces(monkeypatch, value):
    """A step Lambda from a stack that predates the variable must behave like a new one.

    Both ways of getting this wrong are outages of the opposite sign: an empty tuple refuses
    every governance tag, and treating empty as "unbounded" accepts keys the deployed IAM policy
    denies.

    BOTH namespaces are the default, and ``org:`` is the one that matters here. ``platform:`` is
    reserved -- ``POST /api/settings/tags`` refuses to create a new key in it -- so a fallback of
    ``platform:`` alone would refuse every key an admin is able to create, which is a governance
    feature that cannot govern.
    """
    if value is None:
        monkeypatch.delenv(rt.GOVERNANCE_TAG_KEY_PREFIXES_ENV, raising=False)
    else:
        monkeypatch.setenv(rt.GOVERNANCE_TAG_KEY_PREFIXES_ENV, value)
    assert rt.governance_tag_key_prefixes() == ("platform:", "org:")
    assert rt.stampable_governance_tags({"platform:owner": "team-a"}) == {"platform:owner": "team-a"}
    assert rt.stampable_governance_tags({"org:cost-center": "cc-1"}) == {"org:cost-center": "cc-1"}
    with pytest.raises(GovernanceTagError, match="outside the tag namespaces"):
        rt.stampable_governance_tags({"CostCenter": "cc-1"})


def test_the_namespaces_are_re_read_per_call_not_captured_at_import(monkeypatch):
    """The step Lambdas are long-lived and a platform redeploy changes the env var without
    replacing the function, so a value cached at import would keep refusing a namespace the
    running IAM policy already allows -- for as long as the execution environment survives."""
    monkeypatch.setenv(rt.GOVERNANCE_TAG_KEY_PREFIXES_ENV, "platform:")
    with pytest.raises(GovernanceTagError):
        rt.stampable_governance_tags({"acme:x": "y"})
    monkeypatch.setenv(rt.GOVERNANCE_TAG_KEY_PREFIXES_ENV, "platform:,acme:")
    assert rt.stampable_governance_tags({"acme:x": "y"}) == {"acme:x": "y"}


def test_the_aws_legality_rules_still_run_under_the_namespace_gate():
    """``stampable_`` is the wider check, not a different one. A namespaced key that is illegal
    per AWS -- or that designates credential material -- must still be refused, or the live path
    would have traded one validator for the other rather than adding to it."""
    with pytest.raises(GovernanceTagError, match="reserved 'aws:' prefix"):
        rt.stampable_governance_tags({"aws:cost-center": "x"})
    with pytest.raises(GovernanceTagError, match="designates credential material"):
        rt.stampable_governance_tags({"platform:apiKey": "x"})
    with pytest.raises(GovernanceTagError, match="1 to 128"):
        rt.stampable_governance_tags({"platform:" + "k" * 200: "x"})
    assert rt.stampable_governance_tags(None) == {}
    assert rt.stampable_governance_tags({}) == {}


# --------------------------------------------------------------------------------------
# The call sites: what the AWS API actually receives
# --------------------------------------------------------------------------------------


class _Ctrl:
    """Captures the create_agent_runtime kwargs. The assertion has to be on the wire call:
    the defect was an absent parameter, which no assertion about return values can see."""

    def __init__(self):
        self.kwargs = None

    def create_agent_runtime(self, **kwargs):
        self.kwargs = kwargs
        return {"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:aws:bedrock-agentcore:::runtime/rt-1"}


def test_the_runtime_itself_is_created_with_the_governance_tags():
    from app.services.runtime_deployer import create_agent_runtime

    ctrl = _Ctrl()
    create_agent_runtime(
        agentcore_ctrl=ctrl,
        runtime_name="r",
        role_arn="arn:aws:iam::1:role/r",
        s3_bucket="b",
        s3_key="k",
        region=REGION,
        resource_tags=GOV,
    )
    tags = ctrl.kwargs["tags"]
    assert tags["platform:application"] == "payments"
    assert tags["platform:owner"] == "team-a"
    assert tags["ManagedBy"] == "agentcore-flows"


def test_a_runtime_create_refuses_before_the_api_call_when_a_tag_is_illegal():
    """Fail BEFORE create_agent_runtime, not after. A runtime created and then found to be
    untaggable is a resource to clean up; a refusal is not."""
    from app.services.runtime_deployer import create_agent_runtime

    ctrl = _Ctrl()
    with pytest.raises(GovernanceTagError):
        create_agent_runtime(
            agentcore_ctrl=ctrl,
            runtime_name="r",
            role_arn="arn:aws:iam::1:role/r",
            s3_bucket="b",
            s3_key="k",
            region=REGION,
            resource_tags={"aws:nope": "x"},
        )
    assert ctrl.kwargs is None, "the runtime was created before the tag set was validated"


def test_a_runtime_create_refuses_an_out_of_namespace_tag_before_the_api_call():
    """The live regression, at the call site that produced it.

    ``CreateAgentRuntime`` with an out-of-namespace key returns AccessDeniedException on
    ``runtime/*``, and it does so AFTER the deploy has built an exec role, staged code and a
    workload identity. So the refusal has to happen before the API call rather than be left to
    IAM -- ``ctrl.kwargs is None`` is the whole assertion, and it is the one an assertion about
    the raised error cannot make.
    """
    from app.services.runtime_deployer import create_agent_runtime

    ctrl = _Ctrl()
    with pytest.raises(GovernanceTagError, match="outside the tag namespaces"):
        create_agent_runtime(
            agentcore_ctrl=ctrl,
            runtime_name="r",
            role_arn="arn:aws:iam::1:role/r",
            s3_bucket="b",
            s3_key="k",
            region=REGION,
            resource_tags={"CostCenter": "cc-1234"},
        )
    assert ctrl.kwargs is None, "CreateAgentRuntime was called with a tag key IAM denies"


def test_the_runtime_exec_role_gets_the_governance_tags_validated():
    """This call site DID forward the tags, via ``owner_tag_list(extra=...)``, with no
    validation. It now shares the one validator, so the role and the runtime cannot disagree
    about whether a tag set is legal."""
    from app.services.runtime_deployer import create_runtime_iam_role

    class _Iam:
        exceptions = type("E", (), {"EntityAlreadyExistsException": type("X", (Exception,), {})})

        def __init__(self):
            self.tags = None

        def create_role(self, **kw):
            self.tags = kw.get("Tags")
            return {"Role": {"Arn": "arn:aws:iam::1:role/" + kw["RoleName"]}}

        def put_role_policy(self, **kw):
            return {}

        def attach_role_policy(self, **kw):
            return {}

        def get_role(self, **kw):
            raise self.exceptions.EntityAlreadyExistsException()

    iam = _Iam()
    with pytest.raises(GovernanceTagError, match="designates credential material"):
        create_runtime_iam_role(
            iam_client=iam,
            role_name="AgentCoreRuntime-x",
            account_id="123456789012",
            region=REGION,
            resource_tags={"secret_token": "shhh"},
        )
    assert iam.tags is None, "the role was created before the tag set was validated"


def _assert_the_retired_in_process_deploy_path_is_still_refused():
    """The premise of the census's one exclusion: the legacy executor is unreachable."""
    import asyncio

    from app.routers.workflows import deploy_workflow
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        asyncio.run(deploy_workflow("wf-1", MagicMock()))
    assert err.value.status_code == 501


def test_every_runtime_call_site_forwards_the_governance_tags():
    """The census the original defect needed and nobody had.

    ``create_agent_runtime`` gained a ``resource_tags`` parameter; a call site that omits it
    compiles, deploys, passes every existing test and silently produces an ungoverned runtime.
    That is the whole failure mode -- an absent argument is invisible at the call site -- so the
    control has to be over the call sites themselves rather than over one of them. A new node
    type that creates a runtime will fail here until it forwards the tags.

    Client calls (``agentcore_ctrl.create_agent_runtime(**params)``) are attribute calls on a
    boto3 client and are deliberately not in scope; only calls to OUR function are.

    ``services/deployment.py`` is excluded, and the exclusion is load-bearing rather than
    convenient: it holds the retired in-process ``WorkflowExecutor``, whose only entry point --
    ``POST /api/workflows/{id}/deploy`` -- raises 501 without calling it. Its two ungoverned
    runtime calls cannot tag anything because nothing can reach them, and governing dead code
    would make this census pass for a path that must never come back. The exclusion asserts its
    own premise below rather than trusting it, because an exclusion whose reason quietly stops
    being true is how a census starts lying.
    """
    _assert_the_retired_in_process_deploy_path_is_still_refused()
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "app"
    retired = src / "services" / "deployment.py"
    sites: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if path == retired:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "create_agent_runtime"
            ):
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                marker = f"{path.relative_to(src)}:{node.lineno}"
                sites.append(marker if "resource_tags" in kwargs else f"{marker} MISSING resource_tags")

    # Reach before verdict: an AST walk that found nothing would report every call site as
    # compliant. The two known sites are the agent runtime and the MCP server runtime.
    assert len(sites) >= 2, f"the census found no call sites to check: {sites}"
    assert [s for s in sites if "MISSING" in s] == [], sites


# --------------------------------------------------------------------------------------
# Memory: the resource that bills per event stored
# --------------------------------------------------------------------------------------


def test_the_memory_and_its_role_both_carry_the_governance_tags(monkeypatch):
    """AgentCore Memory bills per event stored, so it is exactly what a cost-allocation tag is
    for, and its ownership binding (``OwnerSubHash``/``DeploymentId``) must still win."""
    from app.services.resource_ownership import DEPLOYMENT_ID_TAG_KEY, OWNER_SUB_HASH_TAG_KEY
    from app.step_handlers import memory_step

    monkeypatch.setenv("APP_AWS_REGION", REGION)
    monkeypatch.setattr(memory_step.time, "sleep", lambda _s: None)
    monkeypatch.setattr(memory_step, "_get_deployment_store", MagicMock())

    control, iam = MagicMock(), MagicMock()
    control.list_memories.return_value = {"memories": []}
    control.create_memory.return_value = {
        "memory": {
            "id": "orders-AbCdEf1234",
            "arn": f"arn:aws:bedrock-agentcore:{REGION}:1:memory/orders-AbCdEf1234",
            "name": "orders",
            "status": "CREATING",
            "memoryExecutionRoleArn": "arn:aws:iam::1:role/AgentCoreMemory-orders",
        }
    }
    iam.exceptions = type("E", (), {"EntityAlreadyExistsException": type("X", (Exception,), {})})()
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/AgentCoreMemory-orders"}}
    monkeypatch.setattr(
        memory_step.step_clients,
        "client",
        lambda _e, service, **_k: {"bedrock-agentcore-control": control, "iam": iam}[service],
    )

    memory_step.handler(
        {
            "deployment_id": f"dep-{uuid.uuid4().hex[:8]}",
            "owner_sub": "owner-gov",
            "target_region": REGION,
            "memory_config": {"name": "orders"},
            "resource_tags": GOV,
        },
        None,
    )

    memory_tags = control.create_memory.call_args.kwargs["tags"]
    assert memory_tags["platform:application"] == "payments"
    assert memory_tags["platform:owner"] == "team-a"
    # The ownership binding is what teardown reads; a governance tag must not have cost it.
    assert memory_tags[OWNER_SUB_HASH_TAG_KEY] and memory_tags[DEPLOYMENT_ID_TAG_KEY]

    role_tags = {t["Key"]: t["Value"] for t in iam.create_role.call_args.kwargs["Tags"]}
    assert role_tags["platform:application"] == "payments"
    assert role_tags[DEPLOYMENT_ID_TAG_KEY] == memory_tags[DEPLOYMENT_ID_TAG_KEY]
