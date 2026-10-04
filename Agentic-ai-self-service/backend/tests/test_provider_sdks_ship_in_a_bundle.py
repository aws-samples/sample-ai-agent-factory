"""A non-Bedrock agent's model-provider SDK ships INSIDE a dependency bundle.

Measured, not reasoned about. An OpenAI canvas deployed through the UI reported
``succeeded``, and the container then died at
``from strands.models.openai import OpenAIModel`` with
``ModuleNotFoundError: No module named 'openai'``. The only thing the user ever saw was
AgentCore's ``RuntimeClientError: Runtime initialization time exceeded. Please make sure
that initialization completes in 30s`` — which reads like a cold-start budget problem and
is not one. None of the three dependency bundles carried a model-provider SDK, and
nothing pip-installs at container start (``requirements_txt`` is ``""`` on the AgentCore
path), so all twelve non-Bedrock providers deployed green and never started.

It has to be in the bundle rather than installed on the fly: ARCC ``cnt_Vsqr5LAdJVd1Il``
requires third-party software to be served from infrastructure we control, and
``cnt_mYvaeqAKMTfIlZ`` forbids pulling dependencies from uncontrolled sources during the
deployment lifecycle. So the fix is a pre-built ``provider-<extra>.zip`` per
``strands-agents`` extra, and these tests hold the four places that have to agree:

1. ``_get_model_init_code``'s emitted IMPORT LINE — the only thing that actually decides
   which distribution the container needs. groq, deepseek and writer all emit
   ``OpenAIModel``; ``together`` emits ``LiteLLMModel``. A per-provider-name guess gets
   those four wrong.
2. ``PROVIDER_STRANDS_EXTRA`` — provider → extra, what the deploy downloads.
3. ``scripts/install-agentcore-deps.sh`` — what is actually BUILT and uploaded. A provider
   present in (2) and absent here is exactly the original defect again: a green deploy
   over a container that cannot import.
4. ``PROVIDER_PACKAGES`` — the standalone Python/Docker export's ``requirements.txt``.
   Not the AgentCore path, but the same "does this satisfy the emitted import" question,
   and four of its entries did not.

Every failure mode in here is silent at deploy time. That is the whole reason the file
exists: there is no red state to notice, only an agent that never answers.
"""

import io
import re
import zipfile
from pathlib import Path

import pytest
from app.models.enums import StrandsModelProvider
from app.services.code_generator import (
    PROVIDER_PACKAGES,
    PROVIDER_STRANDS_EXTRA,
    STRANDS_MODULE_EXTRA,
    _generate_strands_default,
    _get_model_init_code,
    provider_bundle_key,
    provider_bundle_keys_for,
)
from app.services.runtime_deployer import canvas_model_providers

REGION = "us-east-1"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUILD_SCRIPT = _REPO_ROOT / "scripts" / "install-agentcore-deps.sh"

ALL_PROVIDERS = [p.value for p in StrandsModelProvider]
NON_BEDROCK = [p for p in ALL_PROVIDERS if p != "bedrock"]

# What the module named in the emitted import line needs installed, by DISTRIBUTION name.
# Read off ``strands-agents`` 1.56.0 (its ``Provides-Extra`` metadata plus the module
# source), not guessed from the provider's brand:
#   strands.models.gemini    imports ``google.genai``           → google-genai
#   strands.models.mistral   imports ``mistralai``              → mistralai
#   strands.models.sagemaker imports mypy_boto3_sagemaker_runtime at MODULE scope,
#                            not under TYPE_CHECKING            → mypy-boto3-sagemaker-runtime
#   strands.models.llamaapi  imports ``llamaapi``               → llama-api-client
_MODULE_REQUIRES: dict[str, tuple[str, ...]] = {
    "": (),  # BedrockModel — boto3, already in every bundle
    "openai": ("openai",),
    "anthropic": ("anthropic",),
    "gemini": ("google-genai",),
    "litellm": ("litellm",),
    "mistral": ("mistralai",),
    "ollama": ("ollama",),
    "sagemaker": ("mypy-boto3-sagemaker-runtime",),
    "llamaapi": ("llama-api-client",),
}

_IMPORT_RE = re.compile(r"^from strands\.models(?:\.(?P<module>[a-z_]+))? import ")


def _emitted_strands_module(provider: str) -> str:
    """The ``strands.models`` submodule the generator imports for *provider*.

    Parsed from the emitted text rather than taken from a table, because the emitted text
    is what the container runs. ``""`` means ``from strands.models import BedrockModel``.
    """
    import_line, _ = _get_model_init_code(provider, "some-model", REGION)
    match = _IMPORT_RE.match(import_line)
    assert match, f"{provider}: unrecognised import line {import_line!r}"
    return match.group("module") or ""


def _script_provider_extras() -> list[str]:
    """The extras ``install-agentcore-deps.sh`` actually builds.

    Parsed out of the shell array. The script is the only thing that produces the zips, so
    reading it is the only way this test can fail for the real reason instead of passing
    because both sides read the same Python constant.
    """
    text = _BUILD_SCRIPT.read_text()
    match = re.search(r"local provider_extras=\((?P<body>[^)]*)\)", text)
    assert match, "install-agentcore-deps.sh no longer declares a provider_extras array"
    return [tok for tok in match.group("body").split() if tok and not tok.startswith("#")]


# ---------------------------------------------------------------------------
# Every provider the API accepts has a bundle decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_every_published_provider_has_a_bundle_decision(provider):
    """``StrandsModelProvider`` is what ``RuntimeConfig.model_provider`` validates against,
    so every member is reachable from the UI. A member with no entry here is a provider
    whose SDK nobody decided about — which defaults to "not shipped"."""
    assert provider in PROVIDER_STRANDS_EXTRA, (
        f"{provider} is selectable in the UI and has no PROVIDER_STRANDS_EXTRA entry, so "
        "no provider bundle is downloaded for it and the container cannot import."
    )
    assert provider in PROVIDER_PACKAGES, f"{provider} has no PROVIDER_PACKAGES entry for the standalone export"


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_the_extra_matches_the_module_the_generator_imports(provider):
    """The mapping is derived from the emitted import, not from the provider's name.

    This is the assertion that catches the four providers a name-based table gets wrong:
    groq, deepseek and writer emit ``OpenAIModel`` (so they need the ``openai`` extra, not
    a ``groq``/``writerai`` one), and ``together`` emits ``LiteLLMModel``.
    """
    module = _emitted_strands_module(provider)
    assert module in STRANDS_MODULE_EXTRA, (
        f"{provider} imports strands.models.{module}, which no STRANDS_MODULE_EXTRA entry covers"
    )
    assert PROVIDER_STRANDS_EXTRA[provider] == STRANDS_MODULE_EXTRA[module], (
        f"{provider} emits `from strands.models{'.' + module if module else ''} import ...`, "
        f"which needs the {STRANDS_MODULE_EXTRA[module]!r} extra, but PROVIDER_STRANDS_EXTRA "
        f"downloads {PROVIDER_STRANDS_EXTRA[provider]!r}. The deploy succeeds and the "
        "container dies at import."
    )


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_the_requirements_export_satisfies_the_emitted_import(provider):
    """``PROVIDER_PACKAGES`` feeds the standalone export's ``requirements.txt``.

    Four entries did not satisfy the import the same generator emits — ``gemini`` named
    ``google-generativeai`` where ``strands.models.gemini`` needs ``google-genai``, and
    groq/writer named their own SDK where the generated code is ``OpenAIModel``. A
    substring check would let ``google-generativeai`` pass for ``google-genai``, so match
    whole tokens.
    """
    module = _emitted_strands_module(provider)
    tokens = PROVIDER_PACKAGES[provider].split()
    for dist in _MODULE_REQUIRES[module]:
        assert dist in tokens, (
            f"{provider} emits `import strands.models.{module}`, which needs {dist!r}, but "
            f"its requirements.txt line is {PROVIDER_PACKAGES[provider]!r}. `pip install -r` "
            "then succeeds and the agent dies at import."
        )
    assert "strands-agents" in tokens, f"{provider} omits strands-agents itself"


# ---------------------------------------------------------------------------
# What the code selects is what the script builds
# ---------------------------------------------------------------------------


def test_the_build_script_builds_exactly_the_extras_the_code_selects():
    """Equality, not containment, in both directions and for different reasons.

    An extra selected and not built is the original defect. An extra built and never
    selected is a bundle nobody downloads, which rots silently: it keeps passing a
    "is it there" check while being wrong, and it costs build time and S3 on every
    platform deploy. ``writer`` was in the script for exactly that reason — the provider
    emits ``OpenAIModel``, so ``provider-writer.zip`` could never be chosen.
    """
    selected = {extra for extra in PROVIDER_STRANDS_EXTRA.values() if extra}
    built = set(_script_provider_extras())
    assert built == selected, (
        f"install-agentcore-deps.sh builds {sorted(built)}; the code selects {sorted(selected)}. "
        f"Never built: {sorted(selected - built)} (each is a green deploy over a container "
        f"that cannot import). Never downloaded: {sorted(built - selected)}."
    )


def test_the_bundle_key_matches_the_filename_the_script_writes():
    """The S3 key and the file on disk are produced by two different languages."""
    assert provider_bundle_key("openai") == "agentcore-deps/provider-openai.zip"
    text = _BUILD_SCRIPT.read_text()
    assert 'create_bundle_zip "${prov_dir}" "${OUTPUT_DIR}/provider-${extra}.zip"' in text
    assert 'OUTPUT_DIR="${PROJECT_ROOT}/backend/agentcore-deps"' in text, (
        "the CDK BucketDeployment syncs backend/agentcore-deps/ to the agentcore-deps/ "
        "prefix; if the script writes elsewhere the zips are never uploaded"
    )


def test_the_script_refuses_to_write_an_empty_provider_bundle():
    """An empty ``provider-openai.zip`` merges cleanly and reproduces the defect exactly.

    The delta subtraction makes that a live possibility: get the extra name wrong, or have
    pip silently skip a wheel for ``manylinux2014_aarch64``, and everything that is left
    is what the base bundle already had. So emptiness has to be fatal at BUILD time — the
    only point where anyone is looking.
    """
    text = _BUILD_SCRIPT.read_text()
    assert 'if [ -z "$(ls -A "${prov_dir}" 2>/dev/null)" ]; then' in text
    assert "return 1" in text.split("build_provider_bundle()")[1].split("\n# ──")[0]


# ---------------------------------------------------------------------------
# Which bundles a canvas asks for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["bedrock", "", None, "  BEDROCK  "])
def test_bedrock_asks_for_no_provider_bundle(provider):
    """Bedrock's SDK is boto3, which every bundle already carries. Downloading a provider
    zip for it would add megabytes to a cold start that is budgeted at 30 seconds."""
    assert provider_bundle_keys_for([provider]) == []


def test_an_unknown_provider_asks_for_no_bundle():
    """``_get_model_init_code`` falls through to ``BedrockModel`` for a provider string it
    does not recognise. If this asked for a bundle instead, the deploy would fail hard on a
    missing ``provider-<typo>.zip`` for an agent that is in fact a working Bedrock agent."""
    assert provider_bundle_keys_for(["not-a-real-provider"]) == []


@pytest.mark.parametrize("provider", NON_BEDROCK)
def test_every_non_bedrock_provider_asks_for_exactly_one_bundle(provider):
    keys = provider_bundle_keys_for([provider])
    assert len(keys) == 1, f"{provider} resolved to {keys}"
    assert keys[0] == provider_bundle_key(PROVIDER_STRANDS_EXTRA[provider])


def test_providers_sharing_an_extra_share_one_bundle():
    """openai, groq, deepseek and writer all emit ``OpenAIModel``. Four downloads of the
    same zip merged four times is four chances for the duplicate-entry behaviour below."""
    keys = provider_bundle_keys_for(["openai", "groq", "deepseek", "writer"])
    assert keys == ["agentcore-deps/provider-openai.zip"]


def test_a_sub_agent_provider_pulls_its_own_bundle():
    """The case a parent-only gate gets wrong. ``code_generator`` builds one model per
    sub-agent IN THE SAME MODULE, so a Bedrock parent with one OpenAI sub-agent needs the
    OpenAI SDK at import — a missing bundle kills the whole agent, not that sub-agent."""
    config = {
        "model_provider": "bedrock",
        "multi_agent_config": {
            "agents": [
                {"agentId": "a", "modelProvider": "openai", "modelId": "gpt-4o-mini"},
                {"agentId": "b", "modelId": "some-model"},
            ],
            "edges": [{"source": "a", "target": "b"}],
            "entryPoint": "a",
        },
    }
    assert provider_bundle_keys_for(canvas_model_providers(config)) == ["agentcore-deps/provider-openai.zip"]


def test_two_different_sub_agent_providers_pull_both_bundles():
    config = {
        "model_provider": "anthropic",
        "multi_agent_config": {
            "agents": [
                {"agentId": "a", "modelProvider": "openai", "modelId": "gpt-4o-mini"},
                {"agentId": "b", "modelProvider": "mistral", "modelId": "mistral-large-latest"},
            ],
            "edges": [{"source": "a", "target": "b"}],
            "entryPoint": "a",
        },
    }
    assert provider_bundle_keys_for(canvas_model_providers(config)) == [
        "agentcore-deps/provider-anthropic.zip",
        "agentcore-deps/provider-openai.zip",
        "agentcore-deps/provider-mistral.zip",
    ]


# ---------------------------------------------------------------------------
# A provider bundle is a DELTA. It is only importable merged onto strands-mcp.zip.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", NON_BEDROCK)
def test_every_non_bedrock_agent_also_selects_the_strands_bundle(provider):
    """The interlock that makes the delta safe.

    ``build_provider_bundle`` subtracts every path ``strands-mcp.zip`` already has — which
    is what keeps these a few MB instead of ~50 — so a provider bundle merged onto
    ``base.zip`` would be missing shared transitive dependencies. It is safe only because
    every generated non-Bedrock agent imports strands, which is exactly what
    ``_needs_strands_bundle`` matches. If a generator ever emits a non-Bedrock agent that
    does not, the delta assumption breaks silently.
    """
    from app.step_handlers.codegen_step import _needs_strands_bundle

    source = _generate_strands_default("You are helpful.", "some-model", REGION, provider)
    assert _needs_strands_bundle(source), (
        f"a {provider} agent would be deployed with base.zip, onto which a provider DELTA bundle is not importable"
    )


# ---------------------------------------------------------------------------
# The merge itself
# ---------------------------------------------------------------------------


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    buf.seek(0)
    return buf.read()


def test_merging_two_bundles_writes_each_path_once():
    """``zipfile`` does NOT reject a duplicate entry name.

    It appends a second member with the same path, emits a UserWarning, and leaves the
    reader to get whichever one it happens to find. Two provider bundles necessarily share
    paths (both contain ``strands-agents``' own metadata and any shared transitive), so
    without an explicit seen-set the merged code.zip carries two copies of some modules
    and an import resolves nondeterministically.
    """
    from app.services.runtime_deployer import _create_code_zip

    first = _zip_bytes({"shared/mod.py": "FIRST", "only_a.py": "A"})
    second = _zip_bytes({"shared/mod.py": "SECOND", "only_b.py": "B"})

    merged = _create_code_zip("print('agent')", "", "agent.py", deps_bundle=first, extra_bundles=[second])

    with zipfile.ZipFile(io.BytesIO(merged)) as zf:
        names = zf.namelist()
        assert len(names) == len(set(names)), f"duplicate entries in the merged zip: {names}"
        assert zf.read("shared/mod.py") == b"FIRST", "the base bundle must win a path collision"
        assert set(names) == {"agent.py", "shared/mod.py", "only_a.py", "only_b.py"}


def test_the_entrypoint_survives_a_bundle_that_contains_the_same_path():
    """The entrypoint is written first and claimed in the seen-set, so a bundle carrying
    an ``agent.py`` of its own cannot shadow the generated agent."""
    from app.services.runtime_deployer import _create_code_zip

    merged = _create_code_zip(
        "print('the real agent')",
        "",
        "agent.py",
        deps_bundle=_zip_bytes({"agent.py": "print('impostor')"}),
    )
    with zipfile.ZipFile(io.BytesIO(merged)) as zf:
        assert zf.read("agent.py") == b"print('the real agent')"


def test_pycache_is_never_merged():
    from app.services.runtime_deployer import _create_code_zip

    merged = _create_code_zip(
        "print('agent')",
        "",
        "agent.py",
        deps_bundle=_zip_bytes({"pkg/__pycache__/mod.cpython-313.pyc": "x", "pkg/mod.py": "y"}),
    )
    with zipfile.ZipFile(io.BytesIO(merged)) as zf:
        assert zf.namelist() == ["agent.py", "pkg/mod.py"]


# ---------------------------------------------------------------------------
# The deploy step fails where the cause is
# ---------------------------------------------------------------------------


class _S3WithoutBundles:
    def __init__(self):
        self.asked: list[str] = []

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's own signature
        self.asked.append(Key)
        raise RuntimeError("NoSuchKey")


class _S3WithBundles:
    def __init__(self):
        self.asked: list[str] = []

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's own signature
        self.asked.append(Key)
        return {"Body": io.BytesIO(_zip_bytes({f"{Key}/mod.py": "x"}))}


def _config(provider: str) -> dict:
    return {"model_provider": provider, "model": {"modelId": "gpt-4o-mini"}}


def test_a_missing_provider_bundle_fails_the_step():
    """Fail-closed, deliberately.

    ``_download_bundle`` returns ``None`` and warns, which for the base bundle predates
    this work. For a provider bundle, continuing is the worse of the two available bugs:
    the deploy goes green and the failure resurfaces as a 30-second init timeout with no
    mention of a module. Stopping here names the S3 key that is missing.
    """
    from app.step_handlers.codegen_step import _provider_bundles

    s3 = _S3WithoutBundles()
    with pytest.raises(RuntimeError) as err:
        _provider_bundles(s3, "platform-bucket", _config("openai"))

    message = str(err.value)
    assert "agentcore-deps/provider-openai.zip" in message, "the error must name the key that is missing"
    assert "platform-bucket" in message
    assert "install-agentcore-deps.sh" in message, "the error must name what fixes it"
    assert s3.asked == ["agentcore-deps/provider-openai.zip"]


def test_no_artifacts_bucket_also_fails_the_step():
    from app.step_handlers.codegen_step import _provider_bundles

    with pytest.raises(RuntimeError) as err:
        _provider_bundles(_S3WithBundles(), "", _config("anthropic"))
    assert "agentcore-deps/provider-anthropic.zip" in str(err.value)


def test_a_bedrock_canvas_asks_s3_for_nothing():
    """No bundle, no download, and above all no failure when the platform bucket is absent
    — a Bedrock deploy must not start depending on artifacts it never reads."""
    from app.step_handlers.codegen_step import _provider_bundles

    s3 = _S3WithoutBundles()
    assert _provider_bundles(s3, "", _config("bedrock")) == []
    assert s3.asked == []


def test_a_non_bedrock_canvas_gets_its_bundle_bytes():
    from app.step_handlers.codegen_step import _provider_bundles

    s3 = _S3WithBundles()
    bundles = _provider_bundles(s3, "platform-bucket", _config("groq"))
    assert len(bundles) == 1
    assert s3.asked == ["agentcore-deps/provider-openai.zip"]
    with zipfile.ZipFile(io.BytesIO(bundles[0])) as zf:
        assert zf.namelist() == ["agentcore-deps/provider-openai.zip/mod.py"]


# ---------------------------------------------------------------------------
# The provider NAME, whichever config model it arrived in
# ---------------------------------------------------------------------------
# Two different pydantic models carry `model_provider` into the same helper, and they
# declare it differently: deployment_models.RuntimeConfig as a plain `str`, and
# components.RuntimeConfiguration as the StrandsModelProvider enum. StrandsModelProvider
# is `class StrandsModelProvider(str, Enum)`, NOT a StrEnum, so `str(member)` is
# "StrandsModelProvider.OPENAI" — not "openai".
#
# Measured before the fix: canvas_model_providers(RuntimeConfiguration(model_provider=
# StrandsModelProvider.OPENAI)) returned ['StrandsModelProvider.OPENAI'], which matches no
# key in PROVIDER_STRANDS_EXTRA, so provider_bundle_keys_for returned [] — zero bundles —
# while needs_provider_api_key still returned True. A green deploy, a granted secret, and a
# container that dies at `from strands.models.openai import OpenAIModel`. Nothing raised.


def _runtime_configuration(**kw):
    """A real ``components.RuntimeConfiguration``, not a stand-in. The whole point of these
    tests is that this model types ``model_provider`` as the enum, so a dict or a dummy
    would be testing the case that already worked."""
    from app.models.components import RuntimeConfiguration

    kw.setdefault("model", {"model_id": "gpt-4o-mini"})
    return RuntimeConfiguration(name="a", system_prompt="p", **kw)


def test_an_enum_member_resolves_to_the_same_bundle_as_its_string():
    """The regression proper. `['StrandsModelProvider.OPENAI']` and `[]` were the measured
    pre-fix values."""
    as_enum = _runtime_configuration(model_provider=StrandsModelProvider.OPENAI)
    assert canvas_model_providers(as_enum) == ["openai"], "an enum member must yield its .value, not str(member)"
    assert provider_bundle_keys_for(canvas_model_providers(as_enum)) == ["agentcore-deps/provider-openai.zip"]


def test_every_provider_enum_member_resolves_the_same_as_its_value():
    """Not just openai: a per-provider check, so a future provider cannot regress alone."""
    for member in StrandsModelProvider:
        from_enum = canvas_model_providers(_runtime_configuration(model_provider=member))
        from_str = canvas_model_providers({"model_provider": member.value})
        assert from_enum == from_str == [member.value], f"{member!r}: enum gave {from_enum}, string gave {from_str}"
        assert provider_bundle_keys_for(from_enum) == provider_bundle_keys_for(from_str)


def test_the_nested_model_provider_is_normalized_too():
    """``ModelConfiguration.provider`` is the SECOND enum-typed field feeding this helper,
    and it is the fallback branch: it is read only when ``model_provider`` is absent. A fix
    applied to the outer field alone would leave this path returning
    'StrandsModelProvider.ANTHROPIC' and downloading nothing."""
    providers = canvas_model_providers({"model": {"provider": StrandsModelProvider.ANTHROPIC, "model_id": "claude"}})
    assert providers == ["anthropic"]
    assert provider_bundle_keys_for(providers) == ["agentcore-deps/provider-anthropic.zip"]


def test_a_sub_agent_enum_member_is_normalized_too():
    """The sub-agent branch reads a separate dict key, so it needed the same call. A
    Bedrock parent with an enum-typed OpenAI sub-agent must still pull the OpenAI SDK."""
    config = {
        "model_provider": StrandsModelProvider.BEDROCK,
        "multi_agent_config": {
            "agents": [
                {"agentId": "a", "modelProvider": StrandsModelProvider.MISTRAL, "modelId": "m"},
                {"agentId": "b", "modelId": "n"},
            ],
            "edges": [{"source": "a", "target": "b"}],
            "entryPoint": "a",
        },
    }
    assert canvas_model_providers(config) == ["bedrock", "mistral", "bedrock"]
    assert provider_bundle_keys_for(canvas_model_providers(config)) == ["agentcore-deps/provider-mistral.zip"]


def test_a_blank_or_missing_provider_still_means_bedrock():
    """The normalizer must not turn the absent case into a download. An empty string, a
    None and a missing key are all a Bedrock canvas, which asks for no bundle."""
    for config in [{}, {"model_provider": ""}, {"model_provider": None}, {"modelProvider": "  "}]:
        assert canvas_model_providers(config) == ["bedrock"], config
        assert provider_bundle_keys_for(canvas_model_providers(config)) == []


# ---------------------------------------------------------------------------
# The SECOND deploy path
# ---------------------------------------------------------------------------
# There are two code paths that upload an agent zip: the Step Functions codegen step and
# WorkflowExecutor's in-process direct deploy in services/deployment.py. Both honour
# `model_provider` (deployment.py's _get_model_code → _get_model_init_code), so both had the
# defect, and fixing one leaves the other shipping a container that cannot import. These
# assert the direct path reuses the step handler's helper instead of carrying a twin — the
# first version of the fix DID carry a twin, and it had already drifted: it raised on a
# download error but was silent when no artifacts bucket was configured at all.


def _direct_path_source() -> str:
    from app.services import deployment as deployment_module

    return Path(deployment_module.__file__).read_text(encoding="utf-8")


def test_the_direct_deploy_path_calls_the_shared_provider_bundle_helper():
    source = _direct_path_source()
    assert "from app.step_handlers.codegen_step import _provider_bundles" in source, (
        "the direct path must borrow the step handler's fail-closed helper, the same way it "
        "borrows _create_policy_when_engine_ready and _build_pii_config"
    )
    assert "_provider_bundles(s3_client, bucket, runtime_config)" in source
    assert "extra_bundles=extra_bundles" in source, (
        "downloading the bundles and not passing them to upload_code_to_s3 would be a "
        "silent no-op with every symptom of the original defect"
    )


def test_the_direct_deploy_path_keeps_no_second_copy_of_the_failure_message():
    """One message, one behaviour, one set of tests. A second copy is a second thing to
    forget to update, and the copy is the one that gets it wrong."""
    source = _direct_path_source()
    assert source.count("scripts/install-agentcore-deps.sh") == 0, (
        "the fail-closed message belongs only in codegen_step._provider_bundles"
    )
    assert "provider_bundle_keys_for" not in source, (
        "the direct path must not resolve bundle keys itself; that is the helper's job"
    )
