"""P0-B: the tag governance gate has exactly two outcomes -- resolved, or refused.

THE DEFECT. ``/api/deploy`` used to treat tag resolution as a sidecar. Anything the store
raised that was not a ``TagResolutionError`` was caught, logged "Tag resolution skipped
(non-fatal)", and the deploy carried on to ``store.create`` and the Step Functions start with
``resource_tags`` empty. So a throttle, a missing IAM grant or an absent table produced
UNTAGGED resources and an HTTP 202: required tags were required only when the store happened
to answer. A control that opens on error is not a control. Worse, the values applied were the
ones the BROWSER resolved against whatever policy set it had loaded, so an admin who added a
required tag, changed a default or edited a profile was bypassed by every deploy panel left
open.

WHAT IS UNDER TEST HERE, and why each case is separate:

  1. the happy path -- without it, every refusal below is satisfied by "refuse everything",
     which is a failure mode this repo has actually shipped (see
     ``test_a_refusal_only_test_suite_hides_a_dead_happy_path``'s findings);
  2. a store outage -> 503 and an EMPTY side-effect log. The status code alone proves nothing:
     the old code also ended in a response, it just created things first;
  3. a stale ``policyRevision`` -> 409, nothing created;
  4. a stale profile ``updated_at`` -> 409, nothing created;
  5. a profile DELETED after capture -> 409, not the 400 that ``get_profile`` returning None
     would otherwise produce. The remedy is "reload", not "supply a value", and a 400 sends
     the caller looking for a field to fix;
  6. omitted tokens. Silence must not be consent: tags or a profile with no revision, and a
     profile with no timestamp, are both 400. Without the second one, stripping a single
     optional-looking field downgrades profile freshness to unchecked while the revision still
     reads current -- the check cannot see a value it is not given;
  7. an explicitly EMPTY tag request is still re-resolved, so policy DEFAULTS still apply and a
     required tag with no default still refuses. "I sent no tags" is not "governance off";
  8. the same posture on ``/api/generate-cfn-template``, because that artifact is a file the
     customer keeps and deploys after we are out of the loop.

Every refusal test asserts the COST of the refusal, not just its status: ``spy.side_effects ==
[]``, which is the ordered log of every persistence and AWS boundary the route can cross.

ARCC cnt_jljdNeOwgPnFx2 governs: an authorization decision must be enforced outside and ahead
of the side-effecting path, and its Common Pitfalls name post-hoc filtering as non-compliant.
"Refused after the rows and the execution" is that pitfall.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from app import deployment_handler as dh
from app.models.deployment_models import DeployRequest
from app.services.tag_policy_store import (
    ResolvedGovernance,
    TagGovernanceStaleError,
    TagPolicy,
    TagPolicyStore,
    TagProfile,
    TagResolutionError,
    compute_policy_revision,
)
from fastapi.testclient import TestClient

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
MODEL_ID = "us.anthropic.claude-sonnet-5"
ACCOUNT = "123456789012"
REGION = "us-east-1"
STATE_MACHINE = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:acfe2e-p0920-deployment"

PROFILE_AT = "2026-09-23T09:00:00Z"

#: The policy set the "browser" resolved against. The revision is COMPUTED from it rather than
#: hardcoded, so this suite keeps testing staleness detection and not a frozen string: if the
#: projection ever changes, the parity suite is what fails, not every case here.
POLICIES = [
    TagPolicy(key="org:cost-center", default_value="cc-000", required=True, updated_at=PROFILE_AT),
    TagPolicy(key="org:owner", default_value=None, required=False, updated_at=PROFILE_AT),
]
REVISION = compute_policy_revision(POLICIES)

#: A revision that is syntactically fine and simply is not the current one. Using a
#: well-formed value matters: a garbage string could be refused by a format check instead of
#: by the comparison under test, and the test would pass with the comparison deleted.
STALE_REVISION = compute_policy_revision([*POLICIES, TagPolicy(key="Extra", updated_at=PROFILE_AT)])


class _FakeStore(TagPolicyStore):
    """The REAL store with only its two table reads substituted.

    Deliberately a subclass, not a MagicMock, for two reasons. The decision logic -- staleness,
    precedence, required-tag enforcement -- is the shipping ``resolve_governance``, so these
    tests cannot pass against a re-implementation of it that drifted. And the route's
    fail-closed ``except Exception`` turns any accidental ``TypeError`` from a loose mock into
    the very 503 that half of this file asserts, so a mock would let the outage cases pass for
    entirely the wrong reason.

    ``__init__`` is overridden because the real one opens a boto3 DynamoDB resource.
    """

    def __init__(self, policies=None, profiles=None, raises: Exception | None = None) -> None:  # noqa: D107
        self._policies = list(POLICIES if policies is None else policies)
        self._profiles = dict(profiles or {})
        self._raises = raises
        self.reads = 0

    # -- the substituted reads -------------------------------------------
    def list_policies(self, _org_id):
        self.reads += 1
        if self._raises is not None:
            raise self._raises
        return list(self._policies)

    def get_profile(self, _org_id, name):
        return self._profiles.get(name)

    def ensure_platform_policies(self, _org_id):
        # The real one seeds through put_policy. Seeding is not what this file is about, and a
        # store that is DOWN must fail on the first read either way -- which the outage case
        # gets from list_policies.
        if self._raises is not None:
            raise self._raises

    # resolve_governance / resolve_tags are INHERITED. That is the point of the subclass.


def _client(sub: str | None = OWNER) -> TestClient:
    claims = {"cognito:groups": ["g-users-default"], **({"sub": sub} if sub else {})}
    event = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}

    async def _inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await dh.deployment_app(scope, receive, send)

    return TestClient(_inject, raise_server_exceptions=False)


def _body(**extra) -> dict:
    return {
        "nodeId": "node-1",
        "config": {"name": "govtest", "model": {"modelId": MODEL_ID}},
        **extra,
    }


class _Spy:
    """The ordered log of every persistence and AWS boundary ``handle_deploy`` can cross."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def record(self, name: str):
        def _fn(*_a, **_k):
            self.calls.append(name)
            return MagicMock()

        return _fn

    @property
    def side_effects(self) -> list[str]:
        return [c for c in self.calls if c != "read"]


@pytest.fixture
def spy(monkeypatch):
    s = _Spy()

    monkeypatch.setattr(dh, "STATE_MACHINE_ARN", STATE_MACHINE)
    # config is a frozen dataclass; replace the global rather than set the attribute.
    monkeypatch.setattr(dh, "config", replace(dh.config, aws_region=REGION))

    store = MagicMock()
    store.create.side_effect = s.record("store.create")
    store.update_status.side_effect = s.record("store.update_status")
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)

    versions = MagicMock()
    versions.put.side_effect = s.record("versions.put")
    versions.update_status.side_effect = s.record("versions.update_status")
    slots = MagicMock()
    slots.get.side_effect = lambda *_a, **_k: (s.calls.append("read"), None)[1]

    import app.services.agent_versions_store as avs

    monkeypatch.setattr(avs, "get_versions_store", lambda: versions)
    monkeypatch.setattr(avs, "get_slots_store", lambda: slots)

    monkeypatch.setattr(
        dh,
        "_prepare_deployment_credentials",
        lambda **kw: (s.calls.append("stage"), (kw.get("gateway_config"), [], [], []))[1],
    )
    monkeypatch.setattr(dh, "_cleanup_staged_credentials", s.record("compensate"))
    monkeypatch.setattr(dh, "_create_sfn_client", lambda *_a, **_k: MagicMock())

    def _start(*_a, **kwargs):
        s.calls.append("StartExecution")
        _start.input_json = kwargs.get("input_json")
        return {"executionArn": f"arn:aws:states:{REGION}:{ACCOUNT}:execution:x:y"}

    monkeypatch.setattr(dh, "_start_sfn_execution", _start)
    monkeypatch.setattr(dh, "_update_execution_arn", lambda *_a, **_k: None)
    s.start = _start

    import app.services.aws_agent_registry as reg

    monkeypatch.setattr(reg, "unapproved_integrations", lambda _idents: [])
    monkeypatch.setattr(dh, "resolve_system_prompt", lambda *_a, **_k: None, raising=False)
    return s


@pytest.fixture
def use_store(monkeypatch):
    """Install a ``_FakeStore`` and hand it back so a test can inspect its read count."""
    import app.services.tag_policy_store as tps

    def _install(**kwargs) -> _FakeStore:
        store = _FakeStore(**kwargs)
        monkeypatch.setattr(tps, "get_tag_policy_store", lambda: store)
        return store

    return _install


# ======================================================================================
# 1. ADMITTING. Without this, every case below is satisfied by "refuse everything".
# ======================================================================================


def test_a_current_revision_and_profile_timestamp_deploys_and_applies_the_servers_resolution(spy, use_store):
    """The happy path, and it pins WHOSE resolution gets applied.

    The caller sends ``org:owner=browser``, and the profile the server reads says
    ``org:owner=from-profile``. Supplied wins by design -- but ``org:cost-center`` is the interesting
    one: the caller never sent it, and it must appear from the POLICY DEFAULT, which can only
    happen if the server re-resolved rather than taking the caller's dict at face value.
    """
    use_store(
        profiles={
            "regulated": TagProfile(name="regulated", values={"org:owner": "from-profile"}, updated_at=PROFILE_AT)
        }
    )

    response = _client().post(
        "/api/deploy",
        json=_body(
            resourceTags={"org:owner": "browser"},
            tagProfile="regulated",
            policyRevision=REVISION,
            tagProfileUpdatedAt=PROFILE_AT,
        ),
    )

    assert response.status_code == 202, response.text
    assert spy.calls.count("StartExecution") == 1, spy.calls
    payload = spy.start.input_json
    assert '"org:cost-center":"cc-000"' in payload.replace(", ", ",").replace('": "', '":"'), payload[:600]
    assert "browser" in payload and "from-profile" not in payload


def test_a_deploy_carrying_no_tag_state_at_all_is_still_resolved_and_still_deploys(spy, use_store):
    """Governance is opt-in, so an ordinary deploy must not need a revision to proceed...

    ...but it must still be RESOLVED: the policy defaults apply, and the store is actually
    read. If this had been made an early return instead, every deploy without tags would skip
    governance entirely and the required-tag case below would be one payload edit away from
    bypass.
    """
    store = use_store()

    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 202, response.text
    assert store.reads == 1, "the store must be read even when the request carries no tag state"
    assert '"org:cost-center"' in spy.start.input_json


# ======================================================================================
# 2. OUTAGE. The case the old code answered with an untagged 202.
# ======================================================================================


def test_a_store_outage_refuses_with_503_and_creates_absolutely_nothing(spy, use_store):
    """This is the regression that P0-B exists for.

    Before: caught, logged non-fatal, deployed with empty tags, HTTP 202. The assertion that
    matters is ``side_effects == []`` -- the status code alone was never the problem.
    """
    use_store(raises=RuntimeError("ProvisionedThroughputExceededException: tag table"))

    response = _client().post("/api/deploy", json=_body(resourceTags={"org:owner": "x"}, policyRevision=REVISION))

    assert response.status_code == 503, response.text
    assert spy.side_effects == [], f"a store outage must create nothing; got {spy.side_effects}"
    assert "Nothing was created" in response.text
    # The store's own error text stays out of the response: a botocore message echoes the
    # request parameters, and on this path those are the caller's tag values.
    assert "ProvisionedThroughputExceededException" not in response.text


def test_the_outage_refusal_names_no_aws_detail_but_is_still_actionable(spy, use_store):
    """A 503 a caller cannot act on gets retried forever or escalated wrongly."""
    use_store(raises=PermissionError("AccessDeniedException: dynamodb:Query"))

    detail = _client().post("/api/deploy", json=_body()).json()["detail"]

    assert "AccessDenied" not in detail and "dynamodb" not in detail, detail
    assert "Retry" in detail and "platform fault" in detail, detail


# ======================================================================================
# 3-5. STALENESS. A 409 whose remedy is "reload", never a silent apply.
# ======================================================================================


def test_a_stale_policy_revision_is_refused_with_409_and_creates_nothing(spy, use_store):
    """The admin changed the policies after the panel loaded.

    Note the request is otherwise entirely valid, and its tags satisfy every required policy.
    Without the revision check it would deploy, applying values the caller approved under
    policies that no longer exist.
    """
    use_store()

    response = _client().post(
        "/api/deploy",
        json=_body(resourceTags={"org:cost-center": "cc-1"}, policyRevision=STALE_REVISION),
    )

    assert response.status_code == 409, response.text
    assert spy.side_effects == [], f"a stale refusal must create nothing; got {spy.side_effects}"
    assert "Reload" in response.text


def test_a_stale_profile_timestamp_is_refused_with_409_and_creates_nothing(spy, use_store):
    """The policy set is untouched, so the revision is CURRENT -- only the profile moved.

    This is why the two tokens are separate. A profile is its own record: an admin who edits
    the values inside it changes what gets applied without changing the policy set at all, so
    the revision cannot detect it.
    """
    use_store(
        profiles={
            "regulated": TagProfile(name="regulated", values={"org:owner": "new"}, updated_at="2026-09-23T11:00:00Z")
        }
    )

    response = _client().post(
        "/api/deploy",
        json=_body(tagProfile="regulated", policyRevision=REVISION, tagProfileUpdatedAt=PROFILE_AT),
    )

    assert response.status_code == 409, response.text
    assert spy.side_effects == []
    assert "regulated" in response.text and "Reload" in response.text


@pytest.mark.parametrize(
    ("stored", "sent"),
    [
        ("2026-09-23T09:00:00+00:00", "2026-09-23T09:00:00Z"),
        ("2026-09-23T09:00:00Z", "2026-09-23T09:00:00+00:00"),
        ("2026-09-23T09:00:00.055194+00:00", "2026-09-23T09:00:00.055194Z"),
    ],
)
def test_the_same_profile_instant_in_another_spelling_is_current_not_stale(spy, use_store, stored, sent):
    """The catalog spells ``+00:00``; a workflow document round-trips the captured timestamp
    through a ``datetime`` and comes back spelled ``Z``. Live (2026-09-27) that spelling
    difference was read as "the profile changed" and every governed export of a reloaded
    workflow was refused with 409. The instant is what governs, not its spelling."""
    use_store(profiles={"regulated": TagProfile(name="regulated", values={"org:owner": "new"}, updated_at=stored)})

    response = _client().post(
        "/api/deploy",
        json=_body(tagProfile="regulated", policyRevision=REVISION, tagProfileUpdatedAt=sent),
    )

    assert response.status_code != 409, response.text
    assert response.status_code < 400, response.text
    assert spy.side_effects != []


def test_a_genuinely_different_profile_instant_is_still_stale_whatever_its_spelling(spy, use_store):
    """The instant comparison must not widen into "any parseable timestamp passes"."""
    use_store(
        profiles={
            "regulated": TagProfile(
                name="regulated", values={"org:owner": "new"}, updated_at="2026-09-23T09:00:01+00:00"
            )
        }
    )

    response = _client().post(
        "/api/deploy",
        json=_body(tagProfile="regulated", policyRevision=REVISION, tagProfileUpdatedAt=PROFILE_AT),
    )

    assert response.status_code == 409, response.text
    assert spy.side_effects == []


def test_a_profile_deleted_after_capture_is_stale_governance_not_a_bad_request(spy, use_store):
    """Deletion is the profile change that used to be reported as the caller's mistake.

    ``get_profile`` returns None, and the unguarded path raised ``TagResolutionError`` -> 400
    "Unknown tag profile", which tells the caller to fix a field. Nothing is wrong with their
    field; the profile they legitimately selected was deleted underneath them, and the remedy
    is to reload. So when a timestamp was captured, a missing profile is a 409.
    """
    use_store(profiles={})

    response = _client().post(
        "/api/deploy",
        json=_body(tagProfile="regulated", policyRevision=REVISION, tagProfileUpdatedAt=PROFILE_AT),
    )

    assert response.status_code == 409, response.text
    assert spy.side_effects == []
    assert "no longer exists" in response.text and "regulated" in response.text


def test_a_profile_that_never_existed_is_still_a_400_when_nothing_was_captured(spy, use_store):
    """The control for the case above: without a captured timestamp there is no staleness
    claim to make, and a genuinely unknown profile name IS the caller's error. If this
    returned 409 too, the 409 would have stopped meaning "reload"."""
    use_store(profiles={})

    response = _client().post("/api/deploy", json=_body(tagProfile="typo", policyRevision=REVISION))

    assert response.status_code == 400, response.text
    assert spy.side_effects == []


# ======================================================================================
# 6. OMITTED TOKENS. Silence is not consent.
# ======================================================================================


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"resourceTags": {"org:owner": "x"}}, "tags with no revision"),
        ({"tagProfile": "regulated"}, "a profile with no revision"),
    ],
)
def test_tag_state_without_a_policy_revision_is_refused_before_the_store_is_read(spy, use_store, payload, why):
    """Refused BEFORE the read, which is what ``store.reads == 0`` pins.

    If this check sat after resolution it would still 400, but the request would already have
    been resolved -- and the day someone reorders the function, the only thing that catches it
    is a test that knows the read never happened.
    """
    store = use_store()

    response = _client().post("/api/deploy", json=_body(**payload))

    assert response.status_code == 400, f"{why}: {response.text}"
    assert store.reads == 0, f"{why}: the request must be refused before the store is read"
    assert spy.side_effects == []
    assert "policyRevision" in response.text


def test_a_profile_without_its_updated_at_is_refused_even_when_the_revision_is_current(spy, use_store):
    """The bypass that a current revision would otherwise hide.

    ``resolve_governance`` can only compare a timestamp it was given, so an old or hostile
    client that sends ``policyRevision`` and omits ``tagProfileUpdatedAt`` gets profile
    freshness switched off while every other signal reads healthy. The revision covers the
    POLICY set; it says nothing about the values inside a profile.
    """
    store = use_store(
        profiles={"regulated": TagProfile(name="regulated", values={"org:owner": "o"}, updated_at=PROFILE_AT)}
    )

    response = _client().post(
        "/api/deploy",
        json=_body(tagProfile="regulated", policyRevision=REVISION),
    )

    assert response.status_code == 400, response.text
    assert store.reads == 0
    assert spy.side_effects == []
    assert "tagProfileUpdatedAt" in response.text and "regulated" in response.text


def test_a_profile_timestamp_with_no_profile_name_is_refused(spy, use_store):
    """A token with nothing to check it against must not read as a successful check."""
    use_store()

    response = _client().post(
        "/api/deploy",
        json=_body(policyRevision=REVISION, tagProfileUpdatedAt=PROFILE_AT),
    )

    assert response.status_code == 409, response.text
    assert spy.side_effects == []


# ======================================================================================
# 7. EXPLICITLY EMPTY. "I sent no tags" is not "governance off".
# ======================================================================================


def test_an_explicitly_empty_tag_set_still_gets_the_policy_defaults(spy, use_store):
    """``resourceTags: {}`` is falsy, so it takes the no-revision-needed path -- and it must
    still be RESOLVED. The default-valued required policy has to land on the deployment."""
    store = use_store()

    response = _client().post("/api/deploy", json=_body(resourceTags={}))

    assert response.status_code == 202, response.text
    assert store.reads == 1
    assert '"org:cost-center"' in spy.start.input_json


def test_an_explicitly_empty_tag_set_still_hits_a_required_policy_with_no_default(spy, use_store):
    """The teeth of case 7. An empty dict must not be a way to skip a required tag.

    A required policy with no default and no supplied value is a 400 from the store, and
    nothing is created -- exactly as if the caller had sent the tag wrongly.
    """
    use_store(policies=[TagPolicy(key="MustSet", default_value=None, required=True, updated_at=PROFILE_AT)])

    response = _client().post("/api/deploy", json=_body(resourceTags={}))

    assert response.status_code == 400, response.text
    assert "MustSet" in response.text
    assert spy.side_effects == [], f"a required-tag refusal must create nothing; got {spy.side_effects}"


# ======================================================================================
# 8. THE EXPORT PATH. Same resolver, same posture, on an artifact we do not control later.
# ======================================================================================


def _export(**extra):
    return _client().post("/api/generate-cfn-template", json=_body(**extra))


def test_the_export_refuses_a_stale_revision_with_409(use_store):
    use_store()

    response = _export(resourceTags={"org:cost-center": "cc-1"}, policyRevision=STALE_REVISION)

    assert response.status_code == 409, response.text[:400]
    assert "Reload" in response.text


def test_the_export_refuses_tags_with_no_revision_before_reading_the_store(use_store):
    store = use_store()

    response = _export(resourceTags={"org:cost-center": "cc-1"})

    assert response.status_code == 400, response.text[:400]
    assert "policyRevision" in response.text
    assert store.reads == 0


def test_the_export_refuses_a_profile_with_no_timestamp(use_store):
    use_store(profiles={"regulated": TagProfile(name="regulated", values={"org:owner": "o"}, updated_at=PROFILE_AT)})

    response = _export(tagProfile="regulated", policyRevision=REVISION)

    assert response.status_code == 400, response.text[:400]
    assert "tagProfileUpdatedAt" in response.text


def test_the_export_refuses_a_store_outage_with_503(use_store):
    use_store(raises=RuntimeError("ResourceNotFoundException: tag table"))

    response = _export(resourceTags={"org:cost-center": "cc-1"}, policyRevision=REVISION)

    assert response.status_code == 503, response.text[:400]
    assert "Nothing was created" in response.text
    assert "ResourceNotFoundException" not in response.text


def test_a_governed_export_that_is_current_succeeds_and_carries_the_resolved_tags(use_store, monkeypatch):
    """The admitting control for the export half, and it pins the consumption too.

    ``policy_revision`` and ``tag_profile_updated_at`` are concurrency tokens, not stack
    state, so they must be verified and then cleared: the generator's allow-list guard refuses
    any field it cannot express, and before they were declared as non-template state a caller
    who correctly sent the revision the deploy path REQUIRES got "this export cannot express
    policy_revision" -- the fail-closed check making the export unusable by exactly the
    callers who complied with it.
    """
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    use_store(profiles={"regulated": TagProfile(name="regulated", values={"org:owner": "ops"}, updated_at=PROFILE_AT)})

    response = _export(
        tagProfile="regulated",
        policyRevision=REVISION,
        tagProfileUpdatedAt=PROFILE_AT,
    )

    assert response.status_code == 200, response.text[:600]
    import base64
    import io
    import zipfile

    archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(response.json()["zip_base64"])))
    template = archive.read(next(n for n in archive.namelist() if n.endswith("template.yaml"))).decode()
    # Both the profile's value and the policy default reached the emitted template.
    assert "ops" in template and "cc-000" in template


# ======================================================================================
# 9. THE TAG-KEY NAMESPACE, which the two artifacts must NOT agree about.
#
# ``resolve_governance`` merges any supplied key that matches no policy into the resolved set as
# an ad-hoc tag, so a caller reaches the live tag-on-create path with a key no admin ever
# declared. The step roles' IAM grants bound ``aws:TagKeys`` to the governance namespaces, so
# such a key is an AccessDeniedException on CreateAgentRuntime -- measured live on
# ``acfe2e-p0920`` -- and it lands after the exec role, the code upload and the workload identity
# already exist. The export is the opposite case: that template is deployed by its recipient
# under their own role, so this platform's allowlist is none of its business.
# ======================================================================================


def test_an_out_of_namespace_tag_key_refuses_the_deploy_and_creates_nothing(spy, use_store):
    """400, and the side-effect log is empty: the refusal has to precede the row and the
    execution, not follow them. A 400 after ``store.create`` would leave a deployment record for
    a deploy that never ran, which is the shape ARCC cnt_jljdNeOwgPnFx2 names as non-compliant
    and the shape this whole file exists to rule out."""
    use_store()

    response = _client().post(
        "/api/deploy",
        json=_body(resourceTags={"CostCenter": "cc-1"}, policyRevision=REVISION),
    )

    assert response.status_code == 400, response.text[:400]
    assert "outside the tag namespaces" in response.text
    # Actionable without reading the source: the namespaces, and the knob that widens them.
    assert "org:" in response.text and "GOVERNANCE_TAG_KEY_PREFIXES" in response.text
    assert spy.side_effects == [], spy.calls


def test_the_same_key_exports_without_complaint(use_store, monkeypatch):
    """The asymmetry, asserted as a pair with the test above so neither can be "fixed" alone.

    An export refused on this key would be this platform imposing its own IAM bound on a stack
    it does not deploy. A deploy allowed on it is the live regression. One validator, one extra
    rule on one path -- and the pair is the only thing that pins WHICH path.
    """
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    use_store()

    response = _export(resourceTags={"CostCenter": "cc-1"}, policyRevision=REVISION)

    assert response.status_code == 200, response.text[:600]
    import base64
    import io
    import zipfile

    archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(response.json()["zip_base64"])))
    template = archive.read(next(n for n in archive.namelist() if n.endswith("template.yaml"))).decode()
    assert "CostCenter" in template, "the recipient's own tag key was dropped from their template"


def test_the_consumed_governance_tokens_are_stripped_before_generation(use_store):
    """The tokens are verified and then removed from the request the generator receives.

    Asserted on ``_resolve_export_tags`` DIRECTLY, because the two defences that make a
    governed export work are indistinguishable through the route: the generator's allow-list
    declares both tokens as non-template state, AND this function clears them once they have
    been checked. Delete either one and a governed export still succeeds, so the route-level
    test above cannot tell which one is carrying it -- a measured gap, not a hypothetical: a
    mutation run that removed the allow-list entry left the whole suite green.

    Clearing them is not cosmetic. A revision or timestamp left on the request is a
    governance token that has already been spent; if the resulting object were ever resolved
    a second time (a retry, a caller reusing the copy) the orphan-timestamp and
    missing-profile branches would fire on state the caller never sent.
    """
    use_store()

    consumed = dh._resolve_export_tags(
        DeployRequest.model_validate(_body(resourceTags={"org:owner": "o"}, policyRevision=REVISION))
    )

    assert consumed.resource_tags == {"org:cost-center": "cc-000", "org:owner": "o"}, (
        "the SERVER's resolution must replace what the caller sent, defaults included"
    )
    assert consumed.policy_revision is None
    assert consumed.tag_profile is None
    assert consumed.tag_profile_updated_at is None


# ======================================================================================
# The exception types themselves. A shared base would collapse two different remedies.
# ======================================================================================


def test_the_two_error_types_are_not_interchangeable():
    """The route maps one to 400 and the other to 409. If ``TagGovernanceStaleError`` were a
    subclass of ``TagResolutionError`` the ``except`` order would decide the status code, and
    a reordering would silently turn every staleness refusal into "fix your field"."""
    assert not issubclass(TagGovernanceStaleError, TagResolutionError)
    assert not issubclass(TagResolutionError, TagGovernanceStaleError)
    assert issubclass(TagGovernanceStaleError, ValueError) and issubclass(TagResolutionError, ValueError)


def test_resolved_governance_reports_the_revision_it_resolved_against():
    """The route hands back only ``tags`` today, but the revision has to travel with them:
    the next consumer (recording what a deployment was governed by) must not have to re-read
    the table, because a second read can disagree with the one that produced the values."""
    resolved = _FakeStore().resolve_governance("default", supplied={"org:owner": "o"})

    assert isinstance(resolved, ResolvedGovernance)
    assert resolved.policy_revision == REVISION
    assert resolved.tags == {"org:cost-center": "cc-000", "org:owner": "o"}
    assert resolved.profile_updated_at is None
