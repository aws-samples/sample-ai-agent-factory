"""A CFN export must not accept a configuration field and then silently discard it.

This is the same failure class as Alvaro's Q3. There, a canvas whose gateway provider was
LiteLLM exported the AgentCore path anyway -- creating an AgentCore gateway plus a Cognito
pool, resource server, client and domain, and dropping the proxy base URL and virtual key
-- with no error raised. The generator does the same thing to ten more fields.

MEASURED, not read. Generate a bundle, vary exactly one ``DeployRequest`` field, and
compare the emitted artifacts byte for byte:

    identity_config         all 7 text artifacts byte-identical
    connectors              all 7 text artifacts byte-identical
    external_mcp_servers    all 7 text artifacts byte-identical
    guardrails_config       all 7 text artifacts byte-identical
    observability_config    all 7 text artifacts byte-identical
    resource_tags           all 7 text artifacts byte-identical   <- SINCE FIXED, emitted
    tag_profile             all 7 text artifacts byte-identical   <- SINCE FIXED, resolved
    target_account_id       all 7 text artifacts byte-identical
    target_region           all 7 text artifacts byte-identical
    version_description     all 7 text artifacts byte-identical

Each of those is a declared field on ``DeployRequest``, whose ``model_config`` is
``extra="forbid"`` -- so these are not stray keys being ignored, they are part of the
accepted API contract. ``resource_tags`` and ``tag_profile`` dropping meant a regulated
account's cost-allocation and ownership tags were absent from every emitted resource;
``target_account_id`` and ``target_region`` dropping means an export aimed at one account
is byte-identical to one aimed at another.

THE TWO TAG FIELDS NOW GET THE REAL FIX rather than a refusal, because refusing them
would have broken the export outright for their intended users -- see
``test_the_export_tags_every_resource_it_can.py`` for the measurement. They stay in the
table below on purpose: the assertion accepts "the output changed" OR "the export
refused", so those two rows now pass by the first branch, and the day the tagging pass
stops working they fail here as well as there.

TWO CONTROLS, because the first version of this measurement reached the OPPOSITE
conclusion and was wrong:

1. ``cfn_provider_code`` is a ZIP with an embedded mtime, so it differs between two
   generates of identical input. Including it made all ten fields look HONOURED. Only the
   seven text artifacts are compared here, and ``test_the_text_artifacts_are_deterministic``
   pins that they are stable run to run -- without it, a nondeterministic artifact would
   make every field below look honoured again.
2. ``test_the_generator_can_change_its_output`` is the positive control:
   ``data_retention_policy="Delete"`` really does change ``template_yaml``, ``teardown_sh``
   and ``readme``. Without it, "byte-identical" would be equally consistent with a
   generator that ignores its whole request.

THE FIX IS A REFUSAL, NOT A FEATURE. Faithfully emitting ten more config dimensions is a
large piece of work; refusing to emit a template that a caller would reasonably believe
carries their guardrails, tags and target account is a small one, and it is the half that
has to land first. A guard that says "this export cannot express X" is honest. An export
that quietly discards X is worse than an error, because the customer deploys it and
believes they got what they configured.
"""

from __future__ import annotations

import hashlib

import pytest
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import CfnTemplateGenerator

#: The artifacts that are byte-stable for a given input. The ZIP-valued members of
#: CfnBundle (cfn_provider_code and friends) are deliberately excluded -- see control 1.
_TEXT_ARTIFACTS = (
    "template_yaml",
    "agent_code",
    "deploy_sh",
    "teardown_sh",
    "readme",
    "build_bundle_sh",
    "deployment_name",
)


#: A syntactically valid policy revision. P0-B refuses an export that carries tags or a
#: profile without one, before the tag store is touched, so any test that means to reach the
#: store has to send it. The VALUE is never compared here -- the doubles below accept any
#: revision; the staleness comparison itself is pinned in
#: ``test_tag_governance_is_fail_closed.py``.
_REVISION = "sha256:" + "a" * 64


def _base() -> dict:
    return {
        "node_id": "n1",
        "config": RuntimeConfig(
            name="agent",
            model={"model_id": "us.anthropic.claude-sonnet-5"},
            system_prompt="hi",
            entrypoint="agent.py",
        ),
    }


def _snapshot(**overrides) -> dict[str, str]:
    bundle = CfnTemplateGenerator().generate(DeployRequest(**{**_base(), **overrides}))
    return {part: hashlib.sha256((getattr(bundle, part) or "").encode()).hexdigest() for part in _TEXT_ARTIFACTS}


#: Each entry is a field that was measured as dropped, with a value a real caller would
#: plausibly send. Values must be valid for the field's own validators, or the test
#: measures a ValidationError instead of the drop. ``resource_tags`` and ``tag_profile``
#: are now expressed rather than refused and are kept here as regression cover -- see the
#: module docstring.
_MEASURED_AS_DROPPED = {
    # A SUBSTANTIVE identity: a client id and scopes. The UI's inert shape (provider selected,
    # every credential field empty) is expressible by omission since F-G04-003 and is pinned in
    # test_the_export_expresses_inert_ui_blocks.py; only a value that would change the deployed
    # graph is refused here.
    "identity_config": {
        "clientId": "3fj9s8d7f6g5h4j3k2l1",
        "clientSecretRef": "arn:aws:secretsmanager:us-east-1:123456789012:secret:idp-AbCdEf",
        "scopes": ["agent/invoke"],
    },
    "connectors": [{"connector_id": "slack", "name": "slack", "authMethod": "api_key"}],
    "external_mcp_servers": [{"name": "ext", "url": "https://x.invalid/mcp"}],
    "guardrails_config": {"enabled": True, "blocked_topics": ["z"]},
    "observability_config": {"name": "obs", "enableOtel": True, "otlp_endpoint": "https://o.invalid"},
    "resource_tags": {"CostCentre": "ECB-42", "Owner": "a"},
    "tag_profile": "regulated",
    "target_account_id": "999988887777",
    "target_region": "eu-central-1",
    "version_description": "v2 for ECB",
}


class TestTheMeasurementItselfIsTrustworthy:
    def test_the_text_artifacts_are_deterministic(self):
        """Control 1. If these drift run to run, every drop assertion below is vacuous."""
        assert _snapshot() == _snapshot() == _snapshot(), (
            "a text artifact is nondeterministic, so 'byte-identical' can no longer "
            "distinguish a dropped field from a honoured one"
        )

    def test_the_generator_can_change_its_output(self):
        """Control 2. The generator demonstrably DOES vary its output for a field it
        honours, so 'byte-identical' means dropped rather than 'ignores everything'."""
        ref = _snapshot()
        changed = [p for p, h in _snapshot(data_retention_policy="Delete").items() if h != ref[p]]
        assert "template_yaml" in changed, (
            f"data_retention_policy='Delete' should change the template; changed={changed}"
        )

    @pytest.mark.parametrize("field,value", sorted(_MEASURED_AS_DROPPED.items()))
    def test_the_value_is_accepted_by_the_request_model(self, field, value):
        """These are real fields on the accepted API contract, not stray keys. If this
        fails, the field was renamed and the drop test below is measuring nothing."""
        assert field in DeployRequest.model_fields
        DeployRequest(**{**_base(), field: value})


class TestNoFieldIsAcceptedAndSilentlyDiscarded:
    """The guard. Either the generator expresses the field, or it refuses the export."""

    @pytest.mark.parametrize("field,value", sorted(_MEASURED_AS_DROPPED.items()))
    def test_setting_it_either_changes_the_output_or_raises(self, field, value):
        ref = _snapshot()
        try:
            after = _snapshot(**{field: value})
        except Exception as exc:  # noqa: BLE001 - any explicit refusal is acceptable here
            assert field in str(exc) or "cannot" in str(exc).lower(), (
                f"the export refused, but the message does not tell the caller that {field!r} is the reason: {exc}"
            )
            return
        changed = [p for p in _TEXT_ARTIFACTS if after[p] != ref[p]]
        assert changed, (
            f"{field}={value!r} was accepted by DeployRequest and then discarded: all "
            f"{len(_TEXT_ARTIFACTS)} text artifacts are byte-identical to an export that "
            f"never set it. The customer deploys this template believing it carries "
            f"{field}. Either express it in the template or refuse the export."
        )


class TestTheCallerActuallySeesTheReason:
    """A refusal nobody can read is not better than a silent drop.

    ``handle_generate_cfn_template`` passes ONE exception type through with its message
    intact -- ``CfnExportUnsupportedError`` becomes a 400 whose detail is the message --
    and collapses everything else into ``HTTPException(500, "Internal server error")``.
    The guard above originally raised ``ValueError``, so every carefully worded sentence
    it produces would have been replaced by an opaque server error at the seam, and the
    caller would have had no way to learn which setting to remove.
    """

    def _client(self):
        import app.deployment_handler as dh
        from fastapi.testclient import TestClient

        return TestClient(dh.deployment_app, raise_server_exceptions=False)

    def _body(self, **overrides) -> dict:
        base = _base()
        return {
            "nodeId": base["node_id"],
            "config": base["config"].model_dump(mode="json", by_alias=True),
            **overrides,
        }

    def test_the_route_returns_the_reason_not_a_500(self):
        response = self._client().post(
            "/api/generate-cfn-template",
            json=self._body(guardrailsConfig={"enabled": True, "blocked_topics": ["z"]}),
        )
        assert response.status_code == 400, (
            f"expected the refusal to reach the caller as a 400; got {response.status_code}. "
            f"A 500 here means the guard's message was swallowed at the route seam. "
            f"Body: {response.text[:400]}"
        )
        detail = str(response.json().get("detail", ""))
        assert "guardrails_config" in detail, f"the caller cannot tell what to remove: {detail}"
        assert "Internal server error" not in detail

    def test_the_governance_concurrency_tokens_are_declared_non_template_state(self):
        """The guard must not refuse the very fields the deploy path REQUIRES.

        ``_reject_what_the_export_cannot_express`` is an allow-list, deliberately: a new
        ``DeployRequest`` field is refused until someone declares it. P0-B added two, and
        before they were declared a caller who correctly sent ``policyRevision`` got "This
        CloudFormation export cannot express policy_revision" -- the fail-closed governance
        check making the export unusable by exactly the callers who complied with it.

        Asserted on the guard DIRECTLY rather than through the route, because the route
        consumes and clears both tokens before the generator sees them. That clear is a second,
        independent defence; with only the route-level test, deleting either one is invisible.
        """
        from app.services.cfn_template_generator import (
            _NOT_TEMPLATE_STATE,
            _reject_what_the_export_cannot_express,
        )

        request = DeployRequest(
            **_base(),
            policy_revision="sha256:" + "a" * 64,
            tag_profile_updated_at="2026-09-23T09:00:00Z",
        )

        _reject_what_the_export_cannot_express(request)  # must not raise

        # And each is declared with a REASON, not silently whitelisted: the dict's whole
        # purpose is that "we express this" and "this is none of the template's business"
        # cannot be confused for one another.
        for field in ("policy_revision", "tag_profile_updated_at"):
            assert _NOT_TEMPLATE_STATE.get(field), field

    def test_an_unusable_tag_also_reaches_the_caller(self, monkeypatch):
        """The tag validator raises the same type, so it must land the same way.

        The tag store is stubbed to pass the supplied tags straight through. Without it
        the route never reaches the generator: ``_resolve_export_tags`` hits DynamoDB
        first and, with no credentials, returns the 503 pinned below instead.
        """
        import app.services.tag_policy_store as tps

        class _Echo:
            def ensure_platform_policies(self, _tenant):
                return None

            def resolve_governance(self, _tenant, supplied=None, profile_name=None, **_kw):
                return tps.ResolvedGovernance(tags=dict(supplied or {}), policy_revision=_REVISION)

        monkeypatch.setattr(tps, "get_tag_policy_store", lambda: _Echo())

        # P0-B: an export that carries tags must also carry the policy revision they were
        # resolved against, or it is refused before the store is reached. Without it this test
        # would pass on a 400 about the missing revision and stop proving anything about the
        # reserved-prefix guard it exists for.
        response = self._client().post(
            "/api/generate-cfn-template",
            json=self._body(resourceTags={"aws:reserved": "x"}, policyRevision=_REVISION),
        )
        assert response.status_code == 400, response.text[:400]
        assert "aws:" in str(response.json().get("detail", ""))

    def test_an_unreachable_tag_store_stops_the_export_rather_than_dropping_the_tags(self):
        """One posture, shared with ``/api/deploy`` since P0-B.

        This used to be the deliberate DIFFERENCE from the deploy path: there a tag-store
        failure was caught and logged as non-fatal. That tolerance is gone -- it produced
        untagged resources and an HTTP 202 -- so both routes now fail closed through the same
        resolver. The reason the export always did is unchanged: the artifact is a file the
        customer keeps and deploys later, possibly long after we are out of the loop, so
        handing them a stack silently missing the governance tags they asked for, with the
        warning only in OUR log, is the exact failure this guard exists to remove.

        No credentials are configured in this test, so the store genuinely fails. The revision
        is supplied so the request reaches the store at all rather than stopping at the 400
        for a missing one -- the outage is the thing under test.
        """
        response = self._client().post(
            "/api/generate-cfn-template",
            json=self._body(resourceTags={"CostCentre": "x"}, policyRevision=_REVISION),
        )
        assert response.status_code == 503, (
            f"a tag store failure must stop the export, not produce an untagged template; got {response.status_code}"
        )
        detail = str(response.json().get("detail", ""))
        # The caller has to learn two things: that the TAGS are why, and that the refusal cost
        # them nothing -- otherwise the safe response to a 503 looks like "check what got
        # created and clean it up".
        assert "tags" in detail.lower(), detail
        assert "nothing was created" in detail.lower(), detail
        # And not the store's own error text: a botocore message echoes the request
        # parameters, which on this path carry the caller's tag values.
        assert "NoCredentials" not in detail and "botocore" not in detail, detail

    def test_an_export_with_no_tags_never_touches_the_tag_store(self):
        """The common case must not acquire a new dependency. If the store were consulted
        unconditionally, every existing export would start failing wherever the tagging
        table is absent -- which is every fresh stack."""
        import app.services.tag_policy_store as tps

        def _explode():
            raise AssertionError("the tag store was consulted for an export that supplied no tags")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(tps, "get_tag_policy_store", _explode)
        try:
            response = self._client().post("/api/generate-cfn-template", json=self._body())
        finally:
            monkeypatch.undo()
        assert response.status_code != 500, response.text[:300]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
