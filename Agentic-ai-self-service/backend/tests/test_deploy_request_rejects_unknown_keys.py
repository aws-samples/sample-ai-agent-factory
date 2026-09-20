"""A misspelled key on a deploy or export request must not be silently discarded.

Found live, not by reading. An export requested with ``deletionPolicy: "Delete"`` — the
wrong spelling of ``dataRetentionPolicy`` — returned HTTP 200 and a bundle whose
data-bearing resources were every one of them ``Retain``. Nothing in the response, the
bundle or the logs said the request had been ignored.

The consequence is not cosmetic and it is not recoverable by retrying: the caller believes
they have a throwaway stack, deletes it, and the Knowledge Base, the Cognito user pool and
the conversation Memory are all still there and still billing. ``DeletionPolicy`` is a
CloudFormation *attribute* baked into the YAML at generation time, so there is no parameter
to correct at deploy time either — the only fix is a re-export, which is the one thing the
caller has no reason to think they need.

The same silence covered every other field on the model, so this is pinned as the general
property (unknown key → 422) with the specific key that found it as one case.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import pytest
from app.models.deployment_models import DeployRequest
from pydantic import ValidationError

MODEL_ID = "us.anthropic.claude-sonnet-5"


def _payload(**extra):
    return {"nodeId": "node-1", "config": {"name": "exporttest", "model": {"modelId": MODEL_ID}}, **extra}


@pytest.mark.parametrize(
    "key",
    [
        # The one that found this. Plausible enough that the author of the live probe wrote
        # it, and near enough the real name that nothing looked wrong.
        "deletionPolicy",
        "deletion_policy",
        "retentionPolicy",
        # A real field of the emitted template, but not of this request.
        "dataRetentionPolicyy",
        "somethingNobodyHasEverSent",
    ],
)
def test_an_unknown_key_is_refused_rather_than_dropped(key):
    with pytest.raises(ValidationError) as err:
        DeployRequest(**_payload(**{key: "Delete"}))

    # The key has to be NAMED. "Extra inputs are not permitted" with no location is the
    # same dead end as the silent drop, just louder.
    assert key in str(err.value), str(err.value)


def test_the_misspelling_would_otherwise_have_meant_retain():
    """The negative half: what the caller asked for is reachable under the right name.

    Without this, "unknown keys are refused" is compatible with the field being broken —
    and a test suite in which every case asserts a refusal proves nothing about the path
    that matters. ``Delete`` under the correct spelling is the admitting case.
    """
    assert DeployRequest(**_payload(dataRetentionPolicy="Delete")).data_retention_policy == "Delete"
    # And the default, which is what the misspelled request silently produced.
    assert DeployRequest(**_payload()).data_retention_policy == "Retain"


def test_the_deploy_panels_duplicate_spelling_of_deployment_mode_still_works():
    """The compatibility case that ``extra="forbid"`` would otherwise have broken.

    ``frontend/src/components/deploy/useDeployment.ts`` sends ``deployment_mode`` AND
    ``deploymentMode`` in the same body. ``populate_by_name`` accepts either, but under
    ``forbid`` the second is "extra", so this exact payload 422'd until a ``mode="before"``
    validator collapsed the pair. Asserted on the real shape rather than on the rule,
    because the rule is what I had wrong: I expected pydantic to treat them as one field
    and it does not.
    """
    request = DeployRequest(**_payload(deployment_mode="harness", deploymentMode="harness"))
    assert request.deployment_mode == "harness"


@pytest.mark.parametrize("spelling", ["deployment_mode", "deploymentMode"])
def test_either_spelling_alone_is_still_accepted(spelling):
    assert DeployRequest(**_payload(**{spelling: "harness"})).deployment_mode == "harness"


def test_two_spellings_carrying_different_values_is_an_error_not_a_coin_toss():
    """Hedging is one thing; contradicting yourself is another.

    Collapsing the pair silently would mean picking a winner by dict order, and the losing
    value here is a deployment *mode* — the difference between code-generated runtime and a
    managed harness. A caller who sent both deserves to be told, not resolved.
    """
    with pytest.raises(ValidationError) as err:
        DeployRequest(**_payload(deployment_mode="harness", deploymentMode="runtime"))

    message = str(err.value)
    assert "deployment_mode" in message
    assert "deploymentMode" in message
    assert "harness" in message and "runtime" in message, "the caller has to see which values clashed"


# Every key the three real callers send, transcribed from the request bodies they build.
# ``DeployRequest`` is constructed nowhere but by FastAPI at ``/api/deploy``,
# ``/api/generate-cfn-template`` and ``/api/export-python``, so this is the whole caller set.
# (``routers/workflows.py`` declares its own unrelated class of the same name.)
_DEPLOY_PANEL_KEYS = [
    "nodeId",
    "config",
    "connectedTools",
    "gatewayConfig",
    "gatewayTools",
    "templateId",
    "identityConfig",
    "customTools",
    "connectors",
    "externalMcpServers",
    "memoryConfig",
    "evaluationConfig",
    "policyConfig",
    "guardrailsConfig",
    "mcpServerConfig",
    "knowledgeBaseConfig",
    "observabilityConfig",
    "a2aConfig",
    "resourceTags",
    "tagProfile",
]

CALLER_PAYLOAD_KEYS = {
    # frontend/src/components/deploy/DeployPanel.tsx — the two export buttons send
    # identical bodies and neither carries deploymentMode.
    "/api/export-python": _DEPLOY_PANEL_KEYS,
    "/api/generate-cfn-template": _DEPLOY_PANEL_KEYS,
    # frontend/src/components/deploy/useDeployment.ts
    "/api/deploy": ["deploymentMode", *_DEPLOY_PANEL_KEYS],
}


@pytest.mark.parametrize("endpoint", sorted(CALLER_PAYLOAD_KEYS))
def test_every_key_a_real_caller_sends_is_a_declared_field(endpoint):
    """``extra="forbid"`` turns a stray key into a 422, so the caller set has to be checked.

    The first version of this change shipped with a docstring asserting the deploy panel's
    payload was fine because ``populate_by_name`` made a duplicate spelling one field.
    Pydantic does not: the second spelling is "extra". That was caught, but only for the one
    endpoint I happened to look at — two more endpoints bind this same model, and a single
    unlisted key on either is a button in the UI that returns 422 for every canvas.

    Asserted against the model's declared names and aliases rather than by constructing a
    payload, so a key that is accepted for the wrong reason still shows up here.
    """
    accepted = set()
    for name, field in DeployRequest.model_fields.items():
        accepted.add(name)
        if field.alias:
            accepted.add(field.alias)

    unknown = [key for key in CALLER_PAYLOAD_KEYS[endpoint] if key not in accepted]
    assert unknown == [], f"{endpoint} would 422 on {unknown}"


def test_a_nested_config_is_unaffected():
    """Scoped deliberately to the request envelope.

    ``RuntimeConfig`` and the component config dicts are shaped by the canvas and carry
    fields this backend version may not know yet; forbidding extras there would make a
    newer frontend unable to deploy against an older backend at all. The reported defect
    was on the envelope, and that is where the fix is.
    """
    request = DeployRequest(
        **_payload(config={"name": "exporttest", "model": {"modelId": MODEL_ID}, "someFutureKnob": True})
    )
    assert request.config.name == "exporttest"
