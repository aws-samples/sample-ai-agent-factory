"""The provider enforces the FULL Secrets Manager ARN grammar on LiteLLMApiKeySecretArn before any AWS call.

The generator's AllowedPattern protects the template parameter, but a hand-edited or Terraform-embedded template bypasses
parameter constraints, and a custom resource that reached CREATE_COMPLETE on a pseudo-ARN would fail hours later at the
first secret read. A peer proved that same-region pseudo-ARNs (S3, KMS, a foreign partition, a non-numeric account, a
non-secret resource kind, `notarn:...`) all passed the previous colon-split check.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_PROVIDER_DIR = Path(__file__).resolve().parents[1] / "src" / "app" / "services" / "cfn_provider"
if str(_PROVIDER_DIR) not in sys.path:  # the handler is a flat Lambda module: it imports cfn_response absolutely
    sys.path.insert(0, str(_PROVIDER_DIR))
provider = importlib.import_module("handler")

HOME = "us-east-1"
GOOD = f"arn:aws:secretsmanager:{HOME}:123456789012:secret:agentcore-litellm-key-AbCdEf"


@pytest.fixture(autouse=True)
def _home_region_and_no_aws(monkeypatch):
    monkeypatch.setenv("AWS_REGION", HOME)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    def _no_aws(*_a, **_k):  # pragma: no cover - reached only when the guard fails
        raise AssertionError("the ARN grammar check must make no AWS call")

    import boto3

    monkeypatch.setattr(boto3, "client", _no_aws)
    monkeypatch.setattr(boto3, "resource", _no_aws)


@pytest.mark.parametrize(
    "pseudo",
    [
        pytest.param(f"a:b:c:{HOME}:e", id="five-colon-fields-that-are-not-an-arn"),
        pytest.param(f"notarn:foo:bar:{HOME}:x", id="not-arn-scheme"),
        pytest.param(f"arn:aws:s3:{HOME}:123456789012:secret:x", id="wrong-service-s3"),
        pytest.param(f"arn:aws:kms:{HOME}:123456789012:key/abc", id="wrong-service-kms"),
        pytest.param(f"arn:evil:secretsmanager:{HOME}:123456789012:secret:x", id="wrong-partition"),
        pytest.param(f"arn:aws:secretsmanager:{HOME}:not-an-account:secret:x", id="non-numeric-account"),
        pytest.param(f"arn:aws:secretsmanager:{HOME}:12345678901:secret:x", id="eleven-digit-account"),
        pytest.param(f"arn:aws:secretsmanager:{HOME}:1234567890123:secret:x", id="thirteen-digit-account"),
        pytest.param(f"arn:aws:secretsmanager:{HOME}:123456789012:not-secret:x", id="wrong-resource-kind"),
        pytest.param(f"arn:aws:secretsmanager:{HOME}:123456789012:secret:", id="empty-secret-name"),
        pytest.param("arn:aws:secretsmanager::123456789012:secret:x", id="empty-region"),
        pytest.param(f"arn:aws:secretsmanager:{HOME}:123456789012", id="truncated"),
        pytest.param(f"arn:aws:secretsmanager:{HOME}:123456789012:secret:x y", id="embedded-space"),
        pytest.param(f"arn:aws:secretsmanager:{HOME.upper()}:123456789012:secret:x", id="uppercase-region"),
        pytest.param(f" {GOOD}", id="leading-space-padded-value"),
        pytest.param(f"{GOOD}\n", id="trailing-newline-padded-value"),
        pytest.param(
            f"arn:aws:secretsmanager:{HOME}:\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u0660\u0661\u0662:secret:x",
            id="twelve-unicode-digits",
        ),
        pytest.param("sk-a-virtual-key-pasted-here", id="a-key-not-an-arn"),
    ],
)
def test_a_pseudo_arn_is_refused_before_any_aws_call_and_never_echoed(pseudo):
    with pytest.raises(provider.ProviderError) as info:
        provider._verify_litellm_secret_region({"LiteLLMApiKeySecretArn": pseudo})
    message = str(info.value)
    assert "not a Secrets Manager ARN" in message, message
    assert pseudo.strip() not in message and "123456789012" not in message, "the supplied value must never be echoed"


@pytest.mark.parametrize("partition", ["aws", "aws-us-gov", "aws-cn", "aws-iso"])
def test_every_aws_partition_is_accepted_when_the_region_matches(partition):
    arn = f"arn:{partition}:secretsmanager:{HOME}:123456789012:secret:agentcore-litellm-key-AbCdEf"
    assert provider._verify_litellm_secret_region({"LiteLLMApiKeySecretArn": arn}) is None


def test_the_region_check_still_runs_on_a_grammatical_arn():
    away = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:agentcore-litellm-key-AbCdEf"
    with pytest.raises(provider.ProviderError) as info:
        provider._verify_litellm_secret_region({"LiteLLMApiKeySecretArn": away})
    assert "eu-central-1" in str(info.value) and HOME in str(info.value)


def test_the_handler_grammar_is_the_generators_allowed_pattern():
    """One grammar, two enforcement points: the handler's compiled pattern IS the generator's AllowedPattern constant
    (anchored), compared as constants rather than searched for in source text."""
    from app.services import cfn_template_generator as gen

    assert f"^{provider.LITELLM_SECRET_ARN_RE.pattern}$" == gen.SECRETSMANAGER_ARN_PATTERN
    assert "[0-9]{12}" in gen.SECRETSMANAGER_ARN_PATTERN and "\\d" not in gen.SECRETSMANAGER_ARN_PATTERN, (
        "account ids are ASCII digits; a Unicode-aware digit class would admit Arabic-Indic digits"
    )


def test_a_whitespace_only_value_is_absent_not_malformed():
    assert provider._verify_litellm_secret_region({"LiteLLMApiKeySecretArn": "   "}) is None
