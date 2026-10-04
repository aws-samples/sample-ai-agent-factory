"""The read half of the AgentCore tagging model, and the container ARNs a nested shape needs.

Four live AccessDeniedExceptions on acfe2e-p0920 (us-east-1, account 166827918465) on
2026-09-22, across five deployments. Three are one defect and one is another:

1. CONTAINER vs CHILD. AgentCore authorizes one logical call against BOTH the container ARN
   and the per-resource child ARN, and the 403 names whichever check it reached first. The
   grants named only the child.

     StepRuntimeConfigureRole, CreateAgentRuntime:
       not authorized to perform: bedrock-agentcore:TagResource on resource:
       arn:aws:bedrock-agentcore:us-east-1:166827918465:workload-identity-directory/default
     StepHarnessRole, CreateOauth2CredentialProvider:
       not authorized to perform: bedrock-agentcore:TagResource on resource:
       arn:aws:bedrock-agentcore:us-east-1:166827918465:token-vault/default

   The first blocked every AgentCore runtime deploy outright. This exact mechanism, on these
   exact two shapes, was already measured with five throwaway roles for the DELETE verbs
   (step_lambdas.py, "BOTH resource ARNs are required") and fixed there -- and left unfixed
   for TagResource, which is why a fix landing in one place is not a fix.

2. THE READ HALF WAS NEVER GRANTED. Every role holding bedrock-agentcore:TagResource held
   ZERO bedrock-agentcore:ListTagsForResource, so the platform could stamp ownership and
   never read it back. The ownership, adoption and teardown gates therefore could not work
   at all:

     StepMemoryRole:
       not authorized to perform: bedrock-agentcore:ListTagsForResource on resource:
       arn:aws:bedrock-agentcore:us-east-1:166827918465:memory/mem_f46d1ded29-I80I66817h
     -> ResourceDeletionRefused: Deletion refused for memory mem_f46d1ded29-I80I66817h:
        live ownership could not be read (AccessDeniedException). The resource was left in
        place.

   Refusing to delete on an unreadable owner is CORRECT. The denial is not: it left a
   memory ACTIVE and billing in the customer's account.

   The read grant spans THREE surfaces, and the first two attempts at it were both too
   narrow. (a) The step roles: a grep scoped to step_handlers/ finds three steps; the real
   answer is six, because nine more call sites live in runtime_deployer, gateway_deployer
   and harness_deployer, reached from the steps that import them. (b) DeploymentLambdaRole
   in platform/lambdas.py, which owns DELETE /api/runtime/{id} and the direct deploy -- the
   path an ordinary user takes, so it fails for users even with every step role fixed.
   (c) docs/cross-account-deploy-role.json, where the same reads run against the assumed
   target session (asserted in backend/tests/test_cross_account_role_contract.py).
   The table is now DERIVED by tests/ownership_read_graph.py rather than transcribed.

3. THE HARNESS TAGS A RUNTIME. CreateHarness is built on CreateAgentRuntime and tags that
   backing runtime asynchronously, under the caller's role, after its own 200. The denial
   appeared in NO log group -- only in the delete_harness response body.

These tests assert the shape of the policy, not the text of the fix. The live re-run is a
separate gate and is NOT claimed by this file.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.lambdas import DEPLOYMENT_LAMBDA_OWNERSHIP_READ_TYPES
from stacks.platform.step_lambdas import (
    AGENTCORE_OWNERSHIP_READ_TYPES,
    AGENTCORE_TAG_ON_CREATE_TYPES,
    AGENTCORE_TYPE_ARN_TAIL,
)
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_for_role
from tests.ownership_read_graph import reachable_ownership_read_types

REGION = "us-east-1"
ACCOUNT = "123456789012"

#: Every step handler, so the derivation test can prove the ABSENT steps are absent because
#: they reach no ownership read -- not because nobody looked at them.
_ALL_STEPS = (
    "auth",
    "codegen",
    "evaluation",
    "gateway",
    "guardrails",
    "harness",
    "iam",
    "knowledge_base",
    "mcp_server",
    "memory",
    "policy",
    "runtime_configure",
    "runtime_launch",
    "status_update",
    "validate",
)

READ_ACTION = "bedrock-agentcore:ListTagsForResource"
TAG_ACTION = "bedrock-agentcore:TagResource"

#: The nested shapes, and the container each one's grant must ALSO name.
CONTAINERS = {
    "workload-identity": "workload-identity-directory/default",
    "oauth2credentialprovider": "token-vault/default",
    "apikeycredentialprovider": "token-vault/default",
}


@pytest.fixture(scope="module")
def template_json():
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _statements(template_json: dict, action: str) -> list[tuple[str, dict]]:
    """Every policy statement in the template granting *action*, with its logical id."""
    out = []
    for lid, res in template_json.get("Resources", {}).items():
        if res.get("Type") not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            continue
        doc = (res.get("Properties") or {}).get("PolicyDocument") or {}
        for st in doc.get("Statement") or []:
            actions = st.get("Action")
            actions = [actions] if isinstance(actions, str) else actions or []
            if action in actions:
                out.append((lid, st))
    return out


def _resources(st: dict) -> list[str]:
    raw = st.get("Resource")
    return [raw] if isinstance(raw, str) else list(raw or [])


def _actions(st: dict) -> list[str]:
    raw = st.get("Action")
    return [raw] if isinstance(raw, str) else list(raw or [])


def _role_logical_id(template_json: dict, step_name: str) -> str:
    """``status_update`` -> the logical id of ``StepStatusUpdateRole``.

    Asserts the match is unique. An ambiguous fragment would make the union below span two
    roles, which is the cross-role leniency this file's read test was rewritten to remove.
    """
    fragment = "Step" + "".join(part.capitalize() for part in step_name.split("_")) + "Role"
    lids = [
        lid
        for lid, res in template_json.get("Resources", {}).items()
        if res.get("Type") == "AWS::IAM::Role" and fragment in lid
    ]
    assert lids, (
        f"no IAM role logical id contains {fragment!r}. The step-name -> role-name convention "
        "changed, so this assertion would be scoped to nothing and pass on any policy."
    )
    assert len(lids) == 1, f"{fragment!r} matches {lids}; an ambiguous role makes the result unattributable"
    return lids[0]


class TestANestedShapeGrantsBothArns:
    """The container-vs-child defect, asserted on the synthesized template."""

    def test_every_tagresource_grant_naming_a_child_also_names_its_container(self, template_json) -> None:
        """The defect itself: a child glob with no container is the live outage.

        Asserted over the TEMPLATE rather than over the table, because the table is what the
        table-consistency test already checks. This one fails if the statement builder stops
        expanding the tuple -- which is the mutation that would quietly restore the outage
        while every table assertion still passed.
        """
        offenders = []
        for lid, st in _statements(template_json, TAG_ACTION):
            res = _resources(st)
            for type_name, container in CONTAINERS.items():
                child_tail = f"{container}/{type_name}/"
                if not any(child_tail in r for r in res):
                    continue
                if not any(r.endswith(f":{container}") for r in res):
                    offenders.append((lid, container, sorted(res)))
        assert not offenders, (
            f"these roles grant {TAG_ACTION} on a nested CHILD ARN without its CONTAINER "
            f"ARN: {offenders}. AgentCore authorizes one call against both and the 403 "
            f"names whichever it checked first, so this is a live AccessDeniedException on "
            f"that create path, not a tighter policy. Measured 2026-09-22: it blocked every "
            f"AgentCore runtime deploy at CreateAgentRuntime."
        )

    def test_a_container_arn_is_never_granted_alone(self, template_json) -> None:
        """The converse, so the fix cannot be 'swap child for container'.

        Both authorizations happen. Granting exactly what the 403 asked for still leaks --
        that is precisely the mistake the DeleteWorkloadIdentity comment records from five
        throwaway roles, where the directory-only role failed with a 403 naming the child.
        """
        offenders = []
        for lid, st in _statements(template_json, TAG_ACTION):
            res = _resources(st)
            for type_name, container in CONTAINERS.items():
                if not any(r.endswith(f":{container}") for r in res):
                    continue
                if not any(f"{container}/{type_name}/" in r for r in res):
                    # Another nested type under the same container may own this grant.
                    siblings = [t for t, c in CONTAINERS.items() if c == container and t != type_name]
                    if any(f"{container}/{s}/" in r for r in res for s in siblings):
                        continue
                    offenders.append((lid, container, sorted(res)))
        assert not offenders, f"container ARN granted with no child glob under it: {offenders}"

    def test_no_grant_wildcards_the_container_segment(self, template_json) -> None:
        """``token-vault/*`` would extend the grant to vaults nobody has.

        There is exactly one token vault and one workload-identity directory per
        account+region, so ``default`` is a literal on purpose. A wildcard there is a silent
        widening that no denial would ever reveal.
        """
        bad = [
            (lid, r)
            for lid, st in _statements(template_json, TAG_ACTION) + _statements(template_json, READ_ACTION)
            for r in _resources(st)
            if "token-vault/*" in r or "workload-identity-directory/*" in r
        ]
        assert not bad, f"container segment wildcarded instead of the literal 'default': {bad}"


class TestTheOwnershipReadIsGranted:
    """The read half, and the reason its step set is NARROWER than the tagging one."""

    def test_every_step_that_reads_ownership_holds_the_read_action(self, template_json) -> None:
        """Each step role's read grant must cover its table row exactly, UNIONED.

        This oracle was rewritten on 2026-09-22 after it failed on a policy that is strictly
        safer than the one it was written against. Two defects, and the second is the worse one:

        1. IT REQUIRED ONE STATEMENT PER ROLE. It searched for a single ``ListTagsForResource``
           statement whose resource set equalled the step's entire type union. An ownership read
           is two calls -- the type's getter resolves an ARN, then the tags are read off it --
           so the grant is now emitted as one statement PER TYPE carrying both actions scoped
           to that type's ARNs. A merged statement cannot express that: it would have to union
           the getters across types and so grant e.g. ``GetMemory`` on a gateway. The union
           across a role's statements is the property that actually matters; the number of
           statements it is split across is not.

        2. IT DISCARDED THE ROLE. It iterated ``by_role.items()`` and never used ``lid``, so a
           statement on ANY role satisfied the assertion for EVERY step. The whole live defect
           was one surface holding a grant another surface lacked -- ``DeploymentLambdaRole``
           held all seven getters while ``StepStatusUpdateRole`` held three -- which is exactly
           the shape a cross-role search reports as clean. The union is therefore taken per
           role and deliberately NOT template-wide.

        Resolved through ``statements_for_role`` so the role's CDK ``OverflowPolicy`` managed
        policy counts too; scanning only ``AWS::IAM::Policy`` would report a grant as absent
        as soon as an unrelated statement pushed the role past the inline size limit.
        """
        assert _statements(template_json, READ_ACTION), (
            f"NO role in the template grants {READ_ACTION}. That is the shipped defect: the "
            "platform tagged resources it could never read back, so every ownership, "
            "adoption and teardown gate failed closed and left resources running."
        )
        for step_name, types in AGENTCORE_OWNERSHIP_READ_TYPES.items():
            want = {tail for t in types for tail in AGENTCORE_TYPE_ARN_TAIL[t]}
            assert want, f"step {step_name} derived an empty resource set, so this row is vacuous"

            role_lid = _role_logical_id(template_json, step_name)
            got = {
                r.split(":", 5)[-1]
                for _src, st in statements_for_role(template_json, role_lid)
                if READ_ACTION in _actions(st) and st.get("Effect", "Allow") == "Allow"
                for r in _resources(st)
                if isinstance(r, str)
            }
            assert got, (
                f"{role_lid} grants {READ_ACTION} on nothing, but step {step_name} reads "
                f"ownership for {sorted(types)}. Every assertion below a missing grant passes "
                "vacuously, so absence is a failure here rather than a silent skip."
            )
            assert got == want, (
                f"{role_lid} grants {READ_ACTION} on {sorted(got)}; step {step_name} reads "
                f"ownership for {sorted(types)}, which needs exactly {sorted(want)}.\n"
                f"  missing (fail-closed outage): {sorted(want - got)}\n"
                f"  extra (unused grant)        : {sorted(got - want)}\n"
                "A missing tail leaves the ownership read denied, which refuses the delete and "
                "leaves the resource running and billing; an extra tail is reach the step "
                "cannot use. The statements may be split per type -- only the union is pinned."
            )

    def test_the_read_map_equals_what_the_call_graph_derives(self) -> None:
        """The anchor test. Hand-transcribing this table got it WRONG, twice.

        First attempt: three steps, from a ``grep`` scoped to ``step_handlers/``. The real
        answer is six, because nine more call sites live in the deployer modules the steps
        import. Second attempt: still missing ``apikeycredentialprovider`` on the teardown
        row, because no call site names that type literally -- it is a loop variable.

        So the table is checked against a derivation rather than against a reading. This is
        the test that kills the omission of either provider namespace, and it is the reason
        a self-consistent table test is not enough: the previous grant test built its
        expectation from this same table, so it passed while the table was wrong.
        """
        derived = {}
        for step_name in _ALL_STEPS:
            keys, reached, _labels, _unres = reachable_ownership_read_types(
                f"src/app/step_handlers/{step_name}_step.handler"
            )
            assert reached > 0, (
                f"the walker reached no functions from {step_name}_step.handler, so its "
                "empty read set is a broken handler reference, not a step that reads "
                "nothing -- which is the failure shape this whole table exists to prevent"
            )
            if keys:
                derived[step_name] = tuple(sorted(keys))
        actual = {k: tuple(sorted(v)) for k, v in AGENTCORE_OWNERSHIP_READ_TYPES.items()}
        assert actual == derived, (
            "AGENTCORE_OWNERSHIP_READ_TYPES disagrees with the call graph.\n"
            f"  table  : {sorted(actual.items())}\n"
            f"  derived: {sorted(derived.items())}\n"
            "Do not edit the table to match -- change the code, or fix the walker if it "
            "cannot see a real path. A type in the table that the step cannot reach is an "
            "unused grant; a type missing from the table is a fail-closed outage."
        )

    def test_codegen_reaches_no_ownership_read_and_so_holds_no_tag_action(self) -> None:
        """The step that must never hold a tagging primitive, checked not assumed.

        ``codegen_step`` imports ``runtime_deployer``, so an import-level analysis would
        grant it the read. The walker resolves it at function level: codegen reaches 85
        functions and not one of them is an ownership read. That distinction matters here
        more than anywhere else, because codegen runs model-authored code.
        """
        for step_name in ("codegen", "iam", "runtime_launch"):
            keys, reached, _labels, _unres = reachable_ownership_read_types(
                f"src/app/step_handlers/{step_name}_step.handler"
            )
            assert reached > 10, f"{step_name}: walker reached only {reached} functions"
            assert not keys, (
                f"{step_name} now reaches an ownership read for {sorted(keys)}. If that is "
                "intended, add it to AGENTCORE_OWNERSHIP_READ_TYPES -- but for codegen in "
                "particular, prefer moving the read out of the path it runs generated code from"
            )
            assert step_name not in AGENTCORE_OWNERSHIP_READ_TYPES

    def test_the_read_set_is_not_a_copy_of_the_write_set(self, template_json) -> None:
        """Least privilege, asserted rather than hoped for.

        Guards the easy uniform fix -- "add ListTagsForResource everywhere TagResource
        already is" -- which would be wrong in BOTH directions:

          - ``status_update`` reads seven types and tags none. It is teardown; it decides
            what it may delete and creates nothing.
          - ``workload-identity`` is tagged by three steps and read by NOBODY. Nothing in
            the codebase reads a workload identity's owner, so a read grant on it would be
            a pure unused capability.
        """
        readers = set(AGENTCORE_OWNERSHIP_READ_TYPES)
        taggers = set(AGENTCORE_TAG_ON_CREATE_TYPES)
        assert readers != taggers, (
            "the read and write step sets are identical, which is what granting the read "
            "uniformly to every tagging role looks like"
        )
        assert "status_update" in readers and "status_update" not in taggers, (
            "status_update is teardown: it must read owners and create nothing"
        )

        tagged_types = {t for types in AGENTCORE_TAG_ON_CREATE_TYPES.values() for t in types}
        read_types = {t for types in AGENTCORE_OWNERSHIP_READ_TYPES.values() for t in types}
        assert "workload-identity" in tagged_types and "workload-identity" not in read_types, (
            "workload-identity is read somewhere it was not before. Nothing reads a "
            "workload identity's owner, so this is the signature of the read having been "
            "copied from the write table instead of derived from the call sites"
        )

        # Per step, the read types must never exceed that step's tagged types PLUS the
        # teardown-only reader. This is the template-level check that a role did not simply
        # inherit the tagging row.
        for step_name, types in AGENTCORE_OWNERSHIP_READ_TYPES.items():
            if step_name == "status_update":
                continue
            assert set(types) <= set(AGENTCORE_TAG_ON_CREATE_TYPES.get(step_name, ())), (
                f"{step_name} reads {sorted(types)} but only tags "
                f"{sorted(AGENTCORE_TAG_ON_CREATE_TYPES.get(step_name, ()))}; a create step "
                "reading an owner it never creates wants review"
            )

    def test_untag_is_granted_to_nobody(self, template_json) -> None:
        """No caller, no grant.

        ``bedrock-agentcore:UntagResource`` has zero callers in backend/src/app. Teardown
        DELETES resources, it does not un-tag them. It was recommended alongside the read
        action and is deliberately excluded: a tagging primitive with no caller is what the
        next thing that wants one reaches for.
        """
        granted = _statements(template_json, "bedrock-agentcore:UntagResource")
        assert not granted, (
            f"bedrock-agentcore:UntagResource is granted to {[lid for lid, _ in granted]} but "
            "nothing calls it. Add it in the same commit as its first caller or not at all."
        )

    def test_the_read_action_carries_no_requesttag_condition(self, template_json) -> None:
        """A read sends no tags, so a RequestTag condition would deny every call.

        This is the copy-paste failure mode for this statement: the TagResource grant
        directly above it is heavily conditioned, and carrying those conditions down would
        be unsatisfiable -- and would fail CLOSED into the very outage being fixed.
        """
        for lid, st in _statements(template_json, READ_ACTION):
            cond = st.get("Condition") or {}
            flat = {k for block in cond.values() if isinstance(block, dict) for k in block}
            assert not any(k.startswith("aws:RequestTag") for k in flat), (
                f"{lid} conditions {READ_ACTION} on a request tag, but a ListTagsForResource "
                f"call sends no tags, so this denies every read: {cond}"
            )
            assert "aws:TagKeys" not in flat, (lid, cond)


class TestTheDeploymentLambdaReadsOwnershipToo:
    """The THIRD surface, and the one a real user hits first.

    The step roles are the Step Functions deploy path. ``DELETE /api/runtime/{id}`` and the
    direct (non-SFN) deploy both run in the deployment Lambda, under
    ``DeploymentLambdaRole`` -- a completely separate role whose explicit AgentCore action
    list held every Create/Get/Delete verb and no ``ListTagsForResource``. So even with
    every step role fixed, an ordinary user deleting their deployment from the UI still hit
    ``ResourceDeletionRefused`` and still left resources running and billing.

    Recorded because it is the third time this session that one defect had three homes: the
    container-ARN fix existed for the delete verbs and not the tag verb, the tagging model
    shipped with no read half on any surface, and this role was missed by a fix that only
    looked at ``step_lambdas.py``. A fix landing in one place is not a fix.
    """

    def test_the_deployment_role_holds_the_ownership_read(self, template_json) -> None:
        derived, reached, _labels, _unres = reachable_ownership_read_types("src/app/deployment_handler.handler")
        assert reached > 50, f"walker reached only {reached} functions from deployment_handler"
        assert derived == set(DEPLOYMENT_LAMBDA_OWNERSHIP_READ_TYPES), (
            "DEPLOYMENT_LAMBDA_OWNERSHIP_READ_TYPES disagrees with the call graph.\n"
            f"  table  : {sorted(DEPLOYMENT_LAMBDA_OWNERSHIP_READ_TYPES)}\n"
            f"  derived: {sorted(derived)}"
        )
        want = {tail for t in DEPLOYMENT_LAMBDA_OWNERSHIP_READ_TYPES for tail in AGENTCORE_TYPE_ARN_TAIL[t]}
        matched = [
            st
            for _lid, st in _statements(template_json, READ_ACTION)
            if {r.split(":", 5)[-1] for r in _resources(st)} == want
        ]
        assert matched, (
            f"no {READ_ACTION} statement grants exactly {sorted(want)}. The deployment "
            "Lambda owns DELETE /api/runtime/{id}; without this every user-initiated "
            "teardown refuses and leaves the resources running"
        )

    def test_the_deployment_role_read_is_not_a_resource_wildcard(self, template_json) -> None:
        """It sits beside a deliberate ``resources=["*"]`` statement, so this is the trap.

        That wildcard exists because AgentCore mints ids at create time and does not honor
        ``bedrock-agentcore:*``. A tag READ needs no unknowable id, so folding the action in
        there would have been the path of least resistance and would have widened the grant
        for no reason. ARCC cnt_L4ZLZgjrCctfxl: never widen to make a check pass.
        """
        for lid, st in _statements(template_json, READ_ACTION):
            assert "*" not in _resources(st), f"{lid} grants {READ_ACTION} on a resource wildcard: {_resources(st)}"

    def test_both_provider_namespaces_are_read_on_every_teardown_surface(self, template_json) -> None:
        """The mutation target peer review specifically asked to be killed.

        ``delete_owned_credential_provider`` probes OAuth then API-key on ONE call and
        deletes the OAuth provider BEFORE probing the API-key one. It re-raises on
        AccessDenied (only a MISSING resource is skipped). So a surface granted the read for
        one namespace does not fail closed -- it half-tears-down and then errors.

        Both teardown surfaces are checked: the ``status_update`` step role and the
        deployment Lambda role.
        """
        for label, types in (
            ("status_update step role", AGENTCORE_OWNERSHIP_READ_TYPES["status_update"]),
            ("deployment Lambda role", DEPLOYMENT_LAMBDA_OWNERSHIP_READ_TYPES),
        ):
            for provider in ("oauth2credentialprovider", "apikeycredentialprovider"):
                assert provider in types, (
                    f"{label} does not read {provider}. delete_owned_credential_provider "
                    "reads both namespaces on one call and deletes the OAuth one first, so "
                    "omitting either leaves a partially torn-down deployment rather than a "
                    "clean refusal"
                )
        # And the synthesized template must carry the child ARNs for both, under the shared
        # token-vault container.
        vault_children = {
            "token-vault/default/oauth2credentialprovider/*",
            "token-vault/default/apikeycredentialprovider/*",
        }
        granted = {r.split(":", 5)[-1] for _lid, st in _statements(template_json, READ_ACTION) for r in _resources(st)}
        missing = vault_children - granted
        assert not missing, f"no role reads these credential-provider ARNs at all: {sorted(missing)}"


class TestTheHarnessTagsABackingRuntime:
    def test_the_harness_step_may_tag_a_runtime(self) -> None:
        """Measured live, and invisible to every oracle except a delete.

        CreateHarness creates and tags a backing agent runtime asynchronously under the
        caller's role. The 403 reached no log group at all.
        """
        assert "runtime" in AGENTCORE_TAG_ON_CREATE_TYPES["harness"], (
            "the harness step cannot tag runtime/*, so CreateHarness reaches CREATE_FAILED "
            "with the denial visible only in the delete_harness response body"
        )

    def test_the_harness_step_may_tag_the_workload_identity_that_runtime_mints(self) -> None:
        """Derived, not measured, and included on purpose.

        create_agent_runtime mints and tags a workload identity, so a backing runtime mints
        one too. It could not have been measured yet: the runtime/* denial is reached first
        and masks it, so granting only what was observed just buys the next invisible async
        denial.
        """
        assert "workload-identity" in AGENTCORE_TAG_ON_CREATE_TYPES["harness"]

    def test_the_harness_step_may_tag_the_default_memory_createharness_provisions(self) -> None:
        """Measured live, and the entry the two tests above stopped one resource short of.

        CreateHarness auto-provisions a DEFAULT memory as well as the backing runtime, and
        tags it asynchronously under the same role. harness_deployer.py:276 already documents
        that memory in prose -- for the EXEC role that reads it at invoke time -- so the fact
        was in the repo and simply never reached the tag-on-create table. The consequence was
        not an untagged memory: the harness went CREATE_FAILED and the whole
        deploymentMode="harness" path was dead from the moment create_harness started passing
        `tags=`. Measured 2026-09-24 on acfe2e-p0920 (deployment d22088ec), GenesisMemoryControlPlane
        403 on memory/p0bharn1790231300_302f262f-*.
        """
        assert "memory" in AGENTCORE_TAG_ON_CREATE_TYPES["harness"], (
            "the harness step cannot tag the default memory CreateHarness provisions, so "
            "every harness deploy reaches CREATE_FAILED after CreateHarness has already "
            "returned 200"
        )

    def test_the_default_memory_name_is_not_under_a_harness_prefix(self) -> None:
        """Why the tail has to be ``memory/*`` and not ``memory/harness_*``.

        The live memory was named ``p0bharn1790231300_302f262f`` -- ``<harnessName>_<hex>``,
        with no ``harness_`` prefix, because sanitize_harness_name does not add one (its
        ``prefix="h"`` is a fallback for names not starting with a letter). A grant scoped to
        ``memory/harness_*`` would match only a harness a user happened to name ``harness_*``,
        which is the shape of the SEPARATE defect recorded against the exec role's
        AgentCoreHarnessOwnedMemory statement.
        """
        assert AGENTCORE_TYPE_ARN_TAIL["memory"] == ("memory/*",), (
            "the default memory's name is derived from the harness name, so any narrower "
            "prefix here denies every harness not named harness_*"
        )
