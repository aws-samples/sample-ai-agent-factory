"""The delete path must ask IAM for the role name the create path actually made.

THE DEFECT THIS PINS. ``iam_step`` mints a per-agent least-privilege role via
``per_agent_identity.build_per_agent_role_name``, which truncates to IAM's 64-character
role-name limit. ``runtime_deployer.destroy_runtime`` used to rebuild that name inline as
``f"AgentCoreRuntime-{name_for_role}"`` with no truncation. The prefix is 17 characters
and ``sanitize_runtime_name`` caps a runtime name at 48, so 17 + 48 = 65: for any agent
whose sanitized name reaches 48 characters, create wrote a 64-character role and delete
asked IAM for a 65-character one.

IAM answers ``NoSuchEntityException``. The cleanup loop treats that as "this candidate
isn't one of ours" and moves on, so DELETE still reports success while the role and its
inline ``AgentCoreRuntimePolicy`` stay behind -- a policy that grants reads against
targets that are still live (the platform artifacts bucket, the gateway, memory, the
knowledge base, and the shared Cognito pool that authenticates every deployed agent).
Nothing else cleans it up: ``iam_step`` records nothing in ``created_resources``, and
there is no tag-based IAM sweep on the delete path.

WHY IT LOOKED SAFE. ``build_per_agent_role_name``'s docstring asserted that
``destroy_runtime`` "derives the SAME convention", and ``destroy_runtime``'s own comment
(Gap P3.3B) asserted per-agent roles "are cleaned up here too". Both were written from
the prefix and were true for short names. Neither side mentioned truncation.

The fix makes ``destroy_runtime`` call the create-side function, so these tests assert
AGREEMENT between the two paths rather than re-spelling the expected name -- a test that
hardcoded ``f"AgentCoreRuntime-{name}"`` would have encoded the bug.
"""

from __future__ import annotations

import pytest
from app.services.per_agent_identity import _ROLE_NAME_PREFIX, build_per_agent_role_name
from app.services.resource_ownership import owner_tag_list
from app.services.runtime_deployer import sanitize_runtime_name

#: Long enough to hit sanitize_runtime_name's 48-char cap, and a name a real user could
#: plausibly type. This is the case that leaked.
LONG_AGENT_NAME = "customer_support_escalation_triage_assistant_v2_prod"

#: The 10-char suffix AgentCore appends to a runtime name to form the runtime id.
FAKE_SUFFIX = "AbCdEfGhIj"


# --------------------------------------------------------------------------------
# Fakes. destroy_runtime talks to two clients; we capture what it asks IAM for.
# --------------------------------------------------------------------------------


class _FakeNoSuchEntity(Exception):
    pass


class _FakeIamExceptions:
    NoSuchEntityException = _FakeNoSuchEntity


class _FakeIam:
    """Records every RoleName asked about, and only 'has' the roles it was given."""

    exceptions = _FakeIamExceptions()

    def __init__(self, existing: set[str]):
        self._existing = set(existing)
        self.asked: list[str] = []
        self.deleted: list[str] = []

    def _check(self, role_name: str):
        if role_name not in self.asked:
            self.asked.append(role_name)
        if role_name not in self._existing:
            raise _FakeNoSuchEntity(f"Role not found: {role_name}")

    def get_role(self, RoleName: str):  # noqa: N803 - boto3 casing
        self._check(RoleName)
        return {
            "Role": {
                "RoleName": RoleName,
                "Arn": f"arn:aws:iam::123456789012:role/{RoleName}",
                "Tags": owner_tag_list("us-east-1"),
            }
        }

    def list_attached_role_policies(self, RoleName: str):  # noqa: N803 - boto3 casing
        self._check(RoleName)
        return {"AttachedPolicies": []}

    def list_role_policies(self, RoleName: str):  # noqa: N803 - boto3 casing
        self._check(RoleName)
        return {"PolicyNames": ["AgentCoreRuntimePolicy"]}

    def delete_role_policy(self, RoleName: str, PolicyName: str):  # noqa: N803 - boto3 casing
        return {}

    def delete_role(self, RoleName: str):  # noqa: N803 - boto3 casing
        self._check(RoleName)
        self.deleted.append(RoleName)
        return {}


class _FakeAgentCoreControl:
    """A runtime that is already gone: get/delete both raise ResourceNotFound.

    destroy_runtime is documented as idempotent and proceeds to role cleanup in that
    case, which is exactly the path under test -- and it means no roleArn is captured,
    so cleanup must fall back to the name convention. That fallback is where the bug was.
    """

    def get_agent_runtime(self, **_kw):
        raise _mk_client_error("ResourceNotFoundException")

    def delete_agent_runtime(self, **_kw):
        raise _mk_client_error("ResourceNotFoundException")


class _ExistingRuntimeControl:
    def __init__(self, role_arn: str, region: str = "us-east-1"):
        self.role_arn = role_arn
        self.region = region
        self.deleted: list[str] = []

    def get_agent_runtime(self, *, agentRuntimeId: str):
        if agentRuntimeId in self.deleted:
            # F-08: destroy_runtime now confirms the delete by re-reading; a deleted runtime is gone.
            raise _mk_client_error("ResourceNotFoundException")
        return {
            "agentRuntimeId": agentRuntimeId,
            "agentRuntimeArn": (f"arn:aws:bedrock-agentcore:{self.region}:123456789012:runtime/{agentRuntimeId}"),
            "roleArn": self.role_arn,
        }

    def list_tags_for_resource(self, *, resourceArn: str):  # noqa: N803
        return {"tags": {tag["Key"]: tag["Value"] for tag in owner_tag_list(self.region)}}

    def delete_agent_runtime(self, *, agentRuntimeId: str):
        self.deleted.append(agentRuntimeId)
        return {}

    def list_online_evaluation_configs(self, **_kwargs):
        return {"onlineEvaluationConfigs": []}


def _mk_client_error(code: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


@pytest.fixture
def run_destroy(monkeypatch):
    """Drive the real ``destroy_runtime`` and return the ``_FakeIam`` it talked to."""

    def _run(runtime_id: str, existing_roles: set[str]) -> _FakeIam:
        from app.services import runtime_deployer as mod

        fake_iam = _FakeIam(existing_roles)

        def _fake_client(service: str, **_kw):
            if service == "iam":
                return fake_iam
            if service == "bedrock-agentcore-control":
                return _FakeAgentCoreControl()
            raise AssertionError(f"unexpected boto3 client: {service}")

        monkeypatch.setattr(mod.boto3, "client", _fake_client)
        # Keep the Bug-62 shared-role guard out of the way; it is tested elsewhere.
        monkeypatch.delenv("SHARED_RUNTIME_ROLE_ARN", raising=False)
        mod.destroy_runtime(runtime_id, "us-east-1")
        return fake_iam

    return _run


# --------------------------------------------------------------------------------
# The arithmetic that makes the bug reachable at all.
# --------------------------------------------------------------------------------


def test_truncation_is_reachable_so_the_rest_of_this_file_matters():
    """Vacuity guard. If the prefix plus the max runtime name fits in 64 characters,
    truncation never fires, create and delete agree trivially, and every test below
    passes for reasons unrelated to what it claims to check. Should that become true
    (a shorter prefix, a tighter name cap), this fails so the change is noticed rather
    than silently hollowing out the suite."""
    longest_runtime_name = sanitize_runtime_name("x" * 200)
    assert len(longest_runtime_name) == 48, (
        f"expected sanitize_runtime_name to cap at 48; got {len(longest_runtime_name)}"
    )
    assert len(_ROLE_NAME_PREFIX) + len(longest_runtime_name) > 64, (
        "prefix + max runtime name now fits inside IAM's 64-char limit, so the "
        "create/delete truncation mismatch is no longer reachable and these tests are "
        "no longer guarding anything real"
    )


def test_the_created_role_name_respects_the_iam_limit():
    """The other direction: the mismatch must not be 'fixed' by dropping the truncation,
    because IAM would then reject the create with ValidationError at deploy time."""
    created = build_per_agent_role_name(sanitize_runtime_name(LONG_AGENT_NAME))
    assert len(created) <= 64, f"role name is {len(created)} chars, IAM's limit is 64"


# --------------------------------------------------------------------------------
# The regression.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_name",
    [
        pytest.param("short_agent", id="short-name-already-worked"),
        pytest.param(LONG_AGENT_NAME, id="long-name-the-leak"),
        pytest.param("a" * 60, id="over-long-name"),
        pytest.param("x" * 47, id="one-under-the-boundary"),
        pytest.param("x" * 48, id="exactly-at-the-boundary"),
    ],
)
def test_delete_deletes_the_role_create_would_have_made(run_destroy, raw_name):
    """The invariant: whatever name the create path produces, the delete path deletes it.

    The expected name comes from the create-side function, never from a re-spelling of
    the convention, so this cannot drift into agreeing with a buggy delete path.
    """
    runtime_name = sanitize_runtime_name(raw_name)
    created_role = build_per_agent_role_name(runtime_name)
    runtime_id = f"{runtime_name}-{FAKE_SUFFIX}"

    fake_iam = run_destroy(runtime_id, existing_roles={created_role})

    assert created_role in fake_iam.deleted, (
        f"delete never removed the role create made.\n"
        f"  runtime name  : {runtime_name!r} ({len(runtime_name)} chars)\n"
        f"  created role  : {created_role!r} ({len(created_role)} chars)\n"
        f"  delete asked  : {fake_iam.asked}\n"
        "The role and its inline AgentCoreRuntimePolicy are left behind, and because "
        "IAM answers NoSuchEntityException the cleanup loop reports no failure -- "
        "DELETE still returns success."
    )


def test_delete_leaves_a_role_it_did_not_create_alone(run_destroy):
    """The counterpart: cleanup is name-scoped, so it must not delete a same-prefixed
    role belonging to a different agent. Without this, the test above could be satisfied
    by deleting everything."""
    runtime_name = sanitize_runtime_name(LONG_AGENT_NAME)
    mine = build_per_agent_role_name(runtime_name)
    someone_elses = build_per_agent_role_name(sanitize_runtime_name("a_totally_different_agent"))

    fake_iam = run_destroy(f"{runtime_name}-{FAKE_SUFFIX}", existing_roles={mine, someone_elses})

    assert mine in fake_iam.deleted
    assert someone_elses not in fake_iam.deleted, f"cleanup deleted another agent's role: {someone_elses}"


def test_the_two_paths_agree_across_every_name_length(run_destroy):
    """Swept rather than spot-checked, because the failure was one character wide and a
    hand-picked example set is exactly what missed it."""
    disagreements = []
    for length in range(1, 61):
        runtime_name = sanitize_runtime_name("n" * length)
        created_role = build_per_agent_role_name(runtime_name)
        fake_iam = run_destroy(f"{runtime_name}-{FAKE_SUFFIX}", existing_roles={created_role})
        if created_role not in fake_iam.deleted:
            disagreements.append((length, len(created_role), created_role, fake_iam.asked))

    assert not disagreements, (
        "create and delete disagree on the role name at these raw-name lengths "
        f"(length, role len, created, asked): {disagreements}"
    )


def test_cross_account_runtime_delete_preserves_the_stable_execution_role(
    monkeypatch,
):
    """One target runtime must not delete the role shared by every target runtime."""
    from app.services import observability_dashboard
    from app.services import runtime_deployer as mod

    stable_role = "CustomerStableAgentCoreRuntimeRole"
    iam = _FakeIam({stable_role})
    control = _ExistingRuntimeControl(
        f"arn:aws:iam::123456789012:role/{stable_role}",
        region="eu-west-1",
    )

    def _client(service: str, **_kwargs):
        if service == "bedrock-agentcore-control":
            return control
        if service == "iam":
            return iam
        return type("_NoopClient", (), {})()

    monkeypatch.setattr(
        observability_dashboard,
        "delete_dashboard_for_runtime",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        mod,
        "_resolve_runtime_name_for_cleanup",
        lambda *_args, **_kwargs: None,
    )

    result = mod.destroy_runtime(
        "target-runtime-AbCdEfGhIj",
        "eu-west-1",
        client_factory=_client,
        delete_execution_role=False,
    )

    assert result["success"] is True
    assert control.deleted == ["target-runtime-AbCdEfGhIj"]
    assert stable_role not in iam.asked
    assert stable_role not in iam.deleted


def test_exact_captured_role_does_not_trigger_deletion_of_derived_candidates(
    monkeypatch,
):
    """A known role ARN is authoritative; name guesses must not delete a second role."""
    from app.services import observability_dashboard
    from app.services import runtime_deployer as mod

    runtime_id = "target_runtime-AbCdEfGhIj"
    exact_role = "ExactRuntimeRole"
    derived_role = build_per_agent_role_name("target_runtime")
    iam = _FakeIam({exact_role, derived_role})
    control = _ExistingRuntimeControl(f"arn:aws:iam::123456789012:role/{exact_role}")

    def _client(service: str, **_kwargs):
        if service == "bedrock-agentcore-control":
            return control
        if service == "iam":
            return iam
        return type("_NoopClient", (), {})()

    monkeypatch.setattr(
        observability_dashboard,
        "delete_dashboard_for_runtime",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        mod,
        "_resolve_runtime_name_for_cleanup",
        lambda *_args, **_kwargs: None,
    )

    mod.destroy_runtime(runtime_id, "us-east-1", client_factory=_client)

    assert exact_role in iam.deleted
    assert derived_role not in iam.asked
    assert derived_role not in iam.deleted


def test_cross_account_runtime_trigger_cleanup_uses_platform_secret_client(
    monkeypatch,
):
    """Runtime AWS resources are target-owned; trigger metadata/secrets are not.

    The assertion this test exists for is the client split: the webhook secret lives in the PLATFORM
    account in the platform region, so deleting it through the target account's client in
    ``eu-west-1`` would either fail or, worse, hit a same-named secret belonging to the customer.

    The setup around that assertion was rewritten for F-81/F-81f, which changed three things it had
    baked in. The caller-supplied ``runtime_name`` is now a VETO rather than a short-circuit, so the
    resolver runs and has to agree; the teardown's enumeration is a strongly consistent read, because
    a missed row means a released name plus a schedule still firing; and a row is authorized by its
    own server-derived ``target_runtime_arn``, not by the partition it sits in.
    """
    from app.services import observability_dashboard, trigger_store
    from app.services import runtime_deployer as mod
    from app.services.trigger_store import Trigger

    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    control = _ExistingRuntimeControl(
        "arn:aws:iam::123456789012:role/AgentCoreFlowsRuntimeRole",
        region="eu-west-1",
    )
    iam = _FakeIam({"AgentCoreFlowsRuntimeRole"})
    target_services: list[tuple[str, str | None]] = []

    class _NoopClient:
        pass

    def _target_client(service: str, **kwargs):
        target_services.append((service, kwargs.get("region_name")))
        if service == "bedrock-agentcore-control":
            return control
        if service == "iam":
            return iam
        return _NoopClient()

    secret_arn = "arn:aws:secretsmanager:us-east-1:999999999999:secret:agentcore-trigger/alice-abc"

    class _PlatformSecrets:
        def __init__(self):
            self.deleted = []

        def describe_secret(self, *, SecretId: str):  # noqa: N803
            assert SecretId == secret_arn
            return {
                "Name": "agentcore-trigger/alice-abc",
                "Tags": [
                    {"Key": "ManagedBy", "Value": "agentcore-flows"},
                    {"Key": "Purpose", "Value": "trigger-webhook-hmac"},
                    {"Key": "owner_sub", "Value": "alice"},
                ],
            }

        def delete_secret(self, **kwargs):
            self.deleted.append(kwargs)

    platform_sm = _PlatformSecrets()
    platform_calls = []

    def _platform_client(service: str, **kwargs):
        # The unified trigger cleanup mints the EventBridge client first (a
        # no-op for a webhook with no rule) and then the Secrets Manager client.
        # Both are platform control-plane clients even when the runtime is
        # cross-account; what must never happen is a target-account secret
        # client (asserted separately against target_services).
        platform_calls.append((service, kwargs.get("region_name")))
        assert service in ("events", "secretsmanager")
        return platform_sm if service == "secretsmanager" else _NoopClient()

    canonical = "target_runtime-AbCdEfGhIj"
    trig = Trigger(
        runtime_name="friendly_agent",
        trigger_id="trigger-1",
        owner_sub="alice",
        type="webhook",
        # The row's own statement of which runtime it fires at, derived server-side at create time.
        # It has to name THIS runtime or the cleanup is not authorized to touch the row -- the
        # partition is keyed by a friendly name two tenants can collide on.
        target_runtime_arn=f"arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/{canonical}",
        webhook_secret_ref=secret_arn,
    )

    class _Store:
        def __init__(self):
            self.deleted = []
            self.consistency = []

        def list_for_runtime(self, runtime_name, *, consistent=False):
            assert runtime_name == "friendly_agent"
            self.consistency.append(consistent)
            return [trig]

        def claim_delete(
            self,
            *,
            runtime_name,
            trigger_id,
            owner_sub,
            delete_token,
            now=None,
        ):
            # Mirror the real store's dispatch fence: a successful claim returns
            # the trigger so the caller proceeds to resource + secret cleanup.
            # Without this method the deployer's claim call raised AttributeError,
            # was swallowed, and the cross-account secret cleanup never ran.
            return trig

        def get(self, runtime_name, trigger_id, *, consistent=False):
            return trig

        def delete_claimed(self, *, runtime_name, trigger_id, delete_token):
            # The deployer removes the row through the token-fenced delete, not
            # the unconditional delete(); only the latest claimant may remove it.
            self.deleted.append((runtime_name, trigger_id))
            return True

        def delete(self, runtime_name, trigger_id):
            self.deleted.append((runtime_name, trigger_id))

    store = _Store()
    resolver_calls = []
    monkeypatch.setattr(trigger_store, "get_trigger_store", lambda: store)
    monkeypatch.setattr(
        observability_dashboard,
        "delete_dashboard_for_runtime",
        lambda *_args, **_kwargs: None,
    )
    # The metadata resolver is the only source of the partition key; the caller's hint may agree with
    # it or veto it, never stand in for it. Stubbed here because this test is about the secret client,
    # and the resolver's own proof rules have their own suite.
    monkeypatch.setattr(
        mod,
        "_resolve_runtime_name_for_cleanup",
        lambda *args, **kwargs: resolver_calls.append(args) or "friendly_agent",
    )

    result = mod.destroy_runtime(
        canonical,
        "eu-west-1",
        client_factory=_target_client,
        delete_execution_role=False,
        runtime_name="friendly_agent",
        platform_client_factory=_platform_client,
    )

    assert result["success"] is True
    assert ("secretsmanager", "eu-west-1") not in target_services
    # The secret client is minted in the platform account/region, never target-side.
    # A webhook-only row has no EventBridge rule, so cleanup builds no events client.
    assert platform_calls == [("secretsmanager", "us-east-1")]
    assert platform_sm.deleted == [
        {
            "SecretId": secret_arn,
            "ForceDeleteWithoutRecovery": True,
        }
    ]
    assert store.deleted == [("friendly_agent", "trigger-1")]
    assert resolver_calls, "the caller's runtime_name hint must not short-circuit the resolver"
    assert store.consistency == [True], "an eventually consistent enumeration can miss a live row"
    assert result["triggers"] == {
        "outcome": "confirmed",
        "rows": 1,
        "deleted": 1,
        "kept": 0,
        "foreign": 0,
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
