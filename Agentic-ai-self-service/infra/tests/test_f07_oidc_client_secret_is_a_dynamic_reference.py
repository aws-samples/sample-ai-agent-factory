"""F-07: the opt-in OIDC client secret must reach Cognito as a Secrets Manager dynamic reference.

``cognito_auth.py`` read ``-c oidc_client_secret=<plaintext>`` and placed it verbatim in
``CfnUserPoolIdentityProvider.provider_details``, so the secret landed in
``cdk.out/*.template.json``, ``GetTemplate``, ``cdk diff`` output and stack events. ARCC
cnt_9OT33u5q3kyAPq: sensitive values in a CloudFormation template must be Secrets Manager
dynamic references, whose value CloudFormation never retains, logs or passes on. The context
value is now a secret ARN or name (``oidc_client_secret_arn``, optionally
``oidc_client_secret_json_key``), and the legacy plaintext key is refused loudly rather than
ignored -- an operator who keeps passing it would otherwise believe federation was configured.

Pinned on the template, not the source: the resolved ``client_secret`` property must be a
``{{resolve:secretsmanager:...}}`` reference naming the configured secret, and no plaintext
context value can reach the template because the code no longer reads one.
"""

from __future__ import annotations

import json

import pytest

from tests.p1_synth import synth

IDP_TYPE = "AWS::Cognito::UserPoolIdentityProvider"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:oidc/okta-client-AbCdEf"  # pragma: allowlist secret
BASE_CTX = {
    "oidc_provider_name": "Okta",
    "oidc_issuer": "https://example.okta.com/oauth2/default",
    "oidc_client_id": "0oa-fake-client-id",
}


def _idp(tpl: dict) -> dict:
    found = [r for r in tpl["Resources"].values() if r["Type"] == IDP_TYPE]
    assert len(found) == 1, f"expected one {IDP_TYPE}, found {len(found)}"
    return found[0]


def test_without_oidc_context_no_identity_provider_is_created():
    tpl = synth("F07NoOidcStack")
    assert not [r for r in tpl["Resources"].values() if r["Type"] == IDP_TYPE]


def test_the_client_secret_is_a_secrets_manager_dynamic_reference_to_the_configured_secret():
    """The assertion that fails on the pre-fix tree (the key is not read; no provider appears)."""
    tpl = synth("F07OidcRefStack", context={**BASE_CTX, "oidc_client_secret_arn": SECRET_ARN})
    details = _idp(tpl)["Properties"]["ProviderDetails"]
    secret = details["client_secret"]
    assert isinstance(secret, str), secret
    assert secret.startswith("{{resolve:secretsmanager:" + SECRET_ARN), secret
    assert secret.endswith("}}"), secret
    assert details["client_id"] == BASE_CTX["oidc_client_id"]


def test_a_json_key_selects_one_field_of_the_secret():
    tpl = synth(
        "F07OidcJsonKeyStack",
        context={**BASE_CTX, "oidc_client_secret_arn": SECRET_ARN, "oidc_client_secret_json_key": "client_secret"},
    )
    secret = _idp(tpl)["Properties"]["ProviderDetails"]["client_secret"]
    # CDK renders the canonical six-segment form with empty version-stage and version-id:
    # {{resolve:secretsmanager:<arn>:SecretString:<key>::}}
    assert secret == "{{resolve:secretsmanager:" + SECRET_ARN + ":SecretString:client_secret::}}", secret


def test_the_legacy_plaintext_context_key_is_refused_not_ignored():
    with pytest.raises(ValueError, match="oidc_client_secret_arn"):
        synth("F07OidcLegacyStack", context={**BASE_CTX, "oidc_client_secret": "not-a-real-secret-value"})


def test_a_plaintext_secret_never_appears_in_the_template_even_when_both_keys_are_passed():
    """Belt and braces on the refusal: nothing synthesizes, so nothing can leak."""
    with pytest.raises(ValueError):
        synth(
            "F07OidcBothStack",
            context={
                **BASE_CTX,
                "oidc_client_secret_arn": SECRET_ARN,
                "oidc_client_secret": "plaintext-should-not-land",
            },
        )


def test_the_dynamic_reference_is_the_only_place_the_secret_id_appears_outside_outputs():
    """The ARN is not a secret, but it should not be sprayed across the template either."""
    tpl = synth("F07OidcSprayStack", context={**BASE_CTX, "oidc_client_secret_arn": SECRET_ARN})
    body = json.dumps(tpl["Resources"])
    assert body.count(SECRET_ARN) == 1, body.count(SECRET_ARN)
