"""Regression: non-Bedrock model providers must receive their API key.

Loom-study Phase-0 defect 0.2 — selecting openai/anthropic/gemini/litellm/mistral
generated a model with NO credential (provider_api_key_ref was consumed nowhere),
so every model call 401'd. Fix: generated model init obtains the key from
``_provider_api_key()``, which dereferences ``PROVIDER_API_KEY_SECRET_ARN`` inside the
container; the runtime_configure step injects that ARN (never the key) and iam_step grants
the read on the same namespace the API boundary locks the ref into.

The first version of the fix injected the RESOLVED key as a ``PROVIDER_API_KEY``
environment variable, and these tests pinned it there. That is not a place a secret can
live: ``GetAgentRuntime`` returns a runtime's environment variables verbatim to any
principal holding that one describe call, and every Task in the deployment state machine
re-emits the whole event into the execution history. ARCC ``cnt_n8LpZcqYi2t3I2``,
``cnt_dAiE0OyXKvfeow``.
"""

from __future__ import annotations

import ast
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, "src")

from app.services.code_generator import _get_model_init_code  # noqa: E402


def test_all_provider_init_lines_are_valid_python():
    # Enumerated from the enum, not hand-listed: llamaapi was in the enum, selectable in
    # the UI, and had no branch here at all — it fell through to the Bedrock fallback, so a
    # Llama API canvas silently deployed a Bedrock agent. A hand-written list cannot fail
    # for a provider it forgot to mention.
    from app.models.enums import StrandsModelProvider

    for prov in [p.value for p in StrandsModelProvider]:
        _imp, init = _get_model_init_code(prov, "m", "us-east-1")
        ast.parse(init)  # malformed f-string would raise


def test_non_bedrock_providers_read_a_provider_key():
    # Every credentialed non-Bedrock provider must obtain a key — including the
    # OpenAI-compatible shims (groq/deepseek/writer), the LiteLLM-backed together, and
    # llamaapi, which previously read ONLY a provider-specific env var that the deploy
    # path never sets, so they deployed keyless and 401'd (Loom-study 5.4).
    #
    # It is now obtained through _provider_api_key(), not from os.environ. This assertion
    # read `"PROVIDER_API_KEY" in init` while the key was injected as a plaintext runtime
    # environment variable — a place a secret cannot live, because GetAgentRuntime returns
    # runtime environment variables verbatim to any principal holding that one describe
    # call. The env var survives only as the resolver's FALLBACK, which is why the check
    # has to move to the call and not merely be spelled differently.
    for prov in [
        "openai",
        "anthropic",
        "gemini",
        "litellm",
        "mistral",
        "groq",
        "deepseek",
        "together",
        "writer",
        "llamaapi",
    ]:
        _imp, init = _get_model_init_code(prov, "m", "us-east-1")
        assert "_provider_api_key(" in init, f"{prov} obtains no key at all: {init}"
        assert 'os.environ.get("PROVIDER_API_KEY")' not in init, (
            f"{prov} reads the plaintext env var directly instead of the resolver: {init}"
        )


def test_the_resolver_prefers_the_reference_over_the_plaintext_env_var():
    """The precedence that used to live in each init line now lives in one function.

    Asserted against the emitted resolver SOURCE, because that is what runs in the
    container: the ARN is read first, the plaintext PROVIDER_API_KEY is a fallback for a
    local run, and a provider-specific variable is the last resort.
    """
    from app.services.code_generator import _PROVIDER_KEY_HELPER

    arn_at = _PROVIDER_KEY_HELPER.index("PROVIDER_API_KEY_SECRET_ARN:")
    plain_at = _PROVIDER_KEY_HELPER.index('os.environ.get("PROVIDER_API_KEY", "")')
    assert arn_at < plain_at, "the plaintext env var must not be consulted before the reference"
    assert "get_secret_value(SecretId=PROVIDER_API_KEY_SECRET_ARN)" in _PROVIDER_KEY_HELPER


def test_openai_compat_shims_keep_their_provider_specific_fallback():
    # The fallback is passed INTO the resolver so the precedence lives in one place.
    # Without it a deployed groq agent read an unset GROQ_API_KEY and 401'd.
    for prov, fallback in [
        ("groq", "GROQ_API_KEY"),
        ("deepseek", "DEEPSEEK_API_KEY"),
        ("writer", "WRITER_API_KEY"),
        ("together", "TOGETHER_API_KEY"),
        ("llamaapi", "LLAMA_API_KEY"),
    ]:
        _imp, init = _get_model_init_code(prov, "m", "us-east-1")
        assert f'_provider_api_key("{fallback}")' in init, (
            f"{prov} must pass {fallback} to the resolver as its fallback, not read it directly: {init}"
        )


def test_openai_and_litellm_support_base_url():
    for prov in ["openai", "litellm"]:
        _imp, init = _get_model_init_code(prov, "m", "us-east-1")
        assert "PROVIDER_BASE_URL" in init


def test_bedrock_unchanged_no_provider_key():
    _imp, init = _get_model_init_code("bedrock", "m", "us-east-1")
    assert "BedrockModel" in init
    assert "PROVIDER_API_KEY" not in init


def test_provider_secret_arn_namespace_validation():
    # The API-boundary guard rejects a foreign ARN and accepts an in-namespace one.
    good = "arn:aws:secretsmanager:us-east-1:111122223333:secret:agentcore-provider/openai/abc-123"
    bad = "arn:aws:secretsmanager:us-east-1:111122223333:secret:someone-elses-secret"
    assert ":secret:agentcore-provider/" in good
    assert ":secret:agentcore-provider/" not in bad


# ---------------------------------------------------------------------------
# providerBaseUrl validation
# ---------------------------------------------------------------------------
# The sibling of the ARN namespace lock above. `provider_api_key_ref` was
# namespace-locked at the API boundary; `provider_base_url` — the URL that same
# key is *sent to* by the OpenAI/LiteLLM model init — had no validation at all
# beyond a length cap. These pin the boundary checks.


def _runtime_config(**kw):
    from app.models.deployment_models import RuntimeConfig

    return RuntimeConfig(
        name="agent",
        model={"modelId": "us.anthropic.claude-sonnet-5"},
        modelProvider="litellm",
        **kw,
    )


def test_provider_base_url_accepts_a_private_https_proxy():
    """The point of the field: a self-hosted LiteLLM behind VPC egress. The
    AgentCore Runtime is the dialer, not the control-plane Lambda, so a private
    address must NOT be rejected the way gateway_deployer._validate_outbound_url
    rejects one. If this ever fails, the customer's own proxy stopped working."""
    cfg = _runtime_config(providerBaseUrl="https://litellm.internal.corp:4000/v1")
    assert cfg.provider_base_url == "https://litellm.internal.corp:4000/v1"
    assert _runtime_config(providerBaseUrl="https://10.0.4.17:4000/v1").provider_base_url


def test_provider_base_url_rejects_plaintext_http():
    # The provider API key is sent to this host as a bearer credential.
    with pytest.raises(ValidationError) as ei:
        _runtime_config(providerBaseUrl="http://litellm.internal.corp:4000/v1")
    assert "https" in str(ei.value)


@pytest.mark.parametrize(
    "bad,because",
    [
        ("file:///etc/passwd", "non-http scheme"),
        ("litellm.internal.corp:4000", "no scheme"),
        ("https://", "no host"),
        ("https://user:pw@litellm.corp/v1", "credentials in the URL reach logs"),
        ("https://169.254.169.254/latest/meta-data", "instance metadata endpoint"),
        ("https://litellm.corp/v1\nAWS_SECRET=x", "newline forges a second env var"),
        ("   ", "empty after stripping"),
    ],
)
def test_provider_base_url_rejects(bad, because):
    with pytest.raises(ValidationError, match=r".") as ei:
        _runtime_config(providerBaseUrl=bad)
    assert "providerBaseUrl" in str(ei.value), f"rejection for {because!r} must name the field"


def test_provider_base_url_stays_optional():
    # Every existing deploy omits it; validation must not make it required.
    assert _runtime_config().provider_base_url is None
    assert _runtime_config(providerBaseUrl=None).provider_base_url is None
