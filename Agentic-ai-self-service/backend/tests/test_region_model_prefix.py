"""Bedrock cross-region inference prefixes must follow the deployment region.

A ``us.`` inference profile does not exist in eu-central-1. The runtime accepts
``MODEL_ID=us.anthropic.claude-sonnet-5`` without complaint and then fails on
*every* invoke — which is why the prefix has to be derived from the region
rather than hardcoded.

Within the backend the rule lives in exactly one module,
``app.services.region_models``; ``code_generator`` and ``runtime_configure_step``
both delegate to it, and the tests below assert they still do rather than having
re-grown local copies. Two implementations necessarily live outside Python:

  * ``getRegionPrefixFor()`` in ``frontend/src/utils/awsRegion.ts``
  * the ``TOOL_GENERATOR_MODEL_ID`` expression in ``infra/.../lambdas.py``

covered by ``frontend/src/utils/awsRegion.test.ts`` and
``infra/tests/test_region_agnostic.py`` respectively.
"""

import pytest
from app.services.code_generator import (
    _get_model_id,
    _to_cross_region_model_id,
    region_inference_prefix,
    to_regional_model_id,
)
from app.step_handlers.runtime_configure_step import (
    _region_inference_prefix,
)
from app.step_handlers.runtime_configure_step import (
    _to_cross_region_model_id as _step_to_cross_region_model_id,
)

REGION_PREFIXES = [
    ("us-east-1", "us"),
    ("us-west-2", "us"),
    ("eu-central-1", "eu"),
    ("eu-west-1", "eu"),
    # apac, not ap. Verified against `bedrock list-inference-profiles`:
    # ap-northeast-1 publishes apac/global/jp, ap-southeast-2 apac/global/au.
    # No region anywhere publishes an `ap.` profile.
    ("ap-southeast-2", "apac"),
    ("ap-northeast-1", "apac"),
]


class TestRegionInferencePrefix:
    @pytest.mark.parametrize(("region", "expected"), REGION_PREFIXES)
    def test_prefix_follows_the_region(self, region, expected):
        assert region_inference_prefix(region) == expected

    @pytest.mark.parametrize(("region", "expected"), REGION_PREFIXES)
    def test_step_handler_agrees_with_the_code_generator(self, region, expected):
        """Both delegate to ``app.services.region_models``. Kept as a behavioural
        assertion (not just an identity check) so that re-growing a local copy in
        either module fails here rather than silently in Frankfurt."""
        assert _region_inference_prefix(region) == region_inference_prefix(region) == expected

    @pytest.mark.parametrize("region", ["", "ca-central-1", "sa-east-1", "nonsense"])
    def test_unknown_regions_fall_back_to_us(self, region):
        """Bedrock has no cross-region profile family for these; ``us`` is the
        widest-available default and matches the pre-existing behaviour."""
        assert region_inference_prefix(region) == "us"
        assert _region_inference_prefix(region) == "us"

    def test_reads_the_environment_when_no_region_is_passed(self, monkeypatch):
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        assert region_inference_prefix() == "eu"

    def test_app_aws_region_wins_over_aws_region(self, monkeypatch):
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert region_inference_prefix() == "eu"


class TestToRegionalModelId:
    """``to_regional_model_id`` RE-POINTS an existing prefix. It must, because
    the platform's own catalogs and every stored workflow carry ``us.``."""

    def test_repoints_a_us_prefix_to_the_deployment_region(self):
        assert to_regional_model_id("us.anthropic.claude-sonnet-5", "eu-central-1") == "eu.anthropic.claude-sonnet-5"

    def test_adds_the_prefix_when_there_is_none(self):
        assert to_regional_model_id("anthropic.claude-sonnet-5", "eu-central-1") == "eu.anthropic.claude-sonnet-5"

    def test_leaves_global_alone(self):
        """``global.`` profiles are region-independent by design — rewriting one
        to ``eu.`` would point at a different (possibly nonexistent) profile."""
        assert (
            to_regional_model_id("global.anthropic.claude-sonnet-5", "eu-central-1")
            == "global.anthropic.claude-sonnet-5"
        )

    def test_us_region_is_a_no_op_on_us_prefixed_ids(self):
        """The pre-existing behaviour every deployed agent depends on today."""
        assert to_regional_model_id("us.anthropic.claude-sonnet-5", "us-east-1") == "us.anthropic.claude-sonnet-5"

    def test_repoints_across_non_us_regions(self):
        assert (
            to_regional_model_id("eu.anthropic.claude-sonnet-5", "ap-southeast-2") == "apac.anthropic.claude-sonnet-5"
        )

    def test_preserves_the_legacy_dated_version_suffix(self):
        assert (
            to_regional_model_id("us.anthropic.claude-haiku-4-5-20251001-v1:0", "eu-central-1")
            == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        )

    def test_adds_v1_0_only_to_dateless_legacy_ids(self):
        """A dated ID missing its version suffix gets one; a current-generation
        dateless ID must NOT — ``…claude-sonnet-5-v1:0`` is not a valid model."""
        assert (
            _to_cross_region_model_id("anthropic.claude-haiku-4-5-20251001", "eu-central-1")
            == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        assert _to_cross_region_model_id("anthropic.claude-sonnet-5", "eu-central-1") == "eu.anthropic.claude-sonnet-5"


class TestStepHandlerModelId:
    """``runtime_configure_step`` sets the runtime's actual ``MODEL_ID`` env var,
    so its output is what the deployed agent invokes."""

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-sonnet-5",
            "anthropic.claude-sonnet-5",
            "eu.anthropic.claude-sonnet-5",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        ],
    )
    def test_agrees_with_the_code_generator_in_frankfurt(self, model_id):
        assert _step_to_cross_region_model_id(model_id, "eu-central-1") == to_regional_model_id(
            model_id, "eu-central-1"
        )

    def test_never_emits_a_us_profile_for_a_frankfurt_deployment(self):
        for model_id in ("us.anthropic.claude-sonnet-5", "anthropic.claude-sonnet-5"):
            assert _step_to_cross_region_model_id(model_id, "eu-central-1").startswith("eu.")

    def test_empty_model_id_passes_through(self):
        """``config.model`` may carry no ``modelId``; do not synthesize ``eu.``."""
        assert _step_to_cross_region_model_id("", "eu-central-1") == ""

    def test_falls_back_to_the_environment_region(self, monkeypatch):
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        assert _step_to_cross_region_model_id("anthropic.claude-sonnet-5") == "eu.anthropic.claude-sonnet-5"

    def test_leaves_global_alone(self):
        assert (
            _step_to_cross_region_model_id("global.anthropic.claude-sonnet-5", "eu-central-1")
            == "global.anthropic.claude-sonnet-5"
        )


class TestGetModelIdEndToEnd:
    """``_get_model_id`` is what lands in the generated agent source."""

    def _config(self, model_id=None):
        class _C:
            model = {} if model_id is None else {"modelId": model_id}

        return _C()

    def test_the_us_default_is_regionalized(self, monkeypatch):
        """The hardcoded default is ``us.anthropic.claude-sonnet-5``; in
        Frankfurt it must not reach the generated agent unchanged."""
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        assert _get_model_id(self._config()) == "eu.anthropic.claude-sonnet-5"

    def test_us_east_1_is_unchanged(self, monkeypatch):
        monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
        assert _get_model_id(self._config()) == "us.anthropic.claude-sonnet-5"

    def test_injection_attempt_is_still_rejected(self, monkeypatch):
        """Regionalizing must not weaken ``_sanitize_identifier``."""
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        with pytest.raises(ValueError):
            _get_model_id(self._config('anthropic.claude"; import os; os.system("x")'))


class TestRegionalizedDefaultsAcrossTheSweep:
    """Every hardcoded ``us.`` default that reaches AWS must follow the region.

    These are the sites a Frankfurt deployment would otherwise hit with a
    nonexistent inference profile: the CFN export's ``ModelId`` parameter, the
    Knowledge Base model ARNs, the model picker's offline fallback, the AI
    generators' own model, and the spec the agent-generator LLM emits.
    """

    @pytest.mark.parametrize(
        ("canvas_model", "expected"),
        [
            ("us.anthropic.claude-opus-4-8", "eu.anthropic.claude-opus-4-8"),
            ("eu.anthropic.claude-sonnet-5", "eu.anthropic.claude-sonnet-5"),
            ("anthropic.claude-sonnet-5", "eu.anthropic.claude-sonnet-5"),
        ],
        # No "canvas with no model" case: RuntimeConfig rejects an empty ``model``
        # ("model.modelId is required"), so ``_get_model_id``'s own default is
        # unreachable through the export path and is covered above on a duck-typed
        # config instead.
        ids=["us-prefixed-canvas-model", "already-correct", "unprefixed"],
    )
    def test_cfn_export_model_id_parameter(self, monkeypatch, canvas_model, expected):
        """The exported default is the canvas model, repointed at the platform region.

        Both halves matter and each was broken on its own. The parameter used to be
        hardcoded to ``us.anthropic.claude-sonnet-5``, so an Opus canvas exported a
        template that deployed Sonnet — silently, with a CREATE_COMPLETE stack and a
        working agent that was not the designed one. It now resolves through
        ``code_generator.resolve_model_id``, the same function the generated agent
        source uses, which is also where the regional prefix is applied: a ``us.``
        inference profile does not exist in eu-central-1, so a Frankfurt export
        carrying one fails on every invoke.
        """
        from app.models.deployment_models import RuntimeConfig
        from app.services.cfn_template_generator import CfnTemplateGenerator

        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        config = RuntimeConfig(name="demo", model={"modelId": canvas_model})
        template = CfnTemplateGenerator()._init_template("demo", None, config)
        assert template["Parameters"]["ModelId"]["Default"] == expected

    def test_cfn_export_teardown_script_region(self, monkeypatch):
        from app.services.cfn_template_generator import CfnTemplateGenerator

        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        script = CfnTemplateGenerator()._generate_teardown_script()
        assert 'REGION="${2:-eu-central-1}"' in script
        assert "us-east-1" not in script

    def test_knowledge_base_model_arn_follows_its_region_argument(self):
        """A geography-prefixed id is an inference profile, not a foundation model.

        Both halves of this assertion were wrong before. The prefix was not
        repointed, so a Frankfurt KB got a ``us.`` profile that does not exist
        there; and the ARN said ``foundation-model``, which for a prefixed id
        names nothing at all. Verified against the live API in us-east-1:
        ``get-foundation-model --model-identifier us.anthropic.claude-…``
        answers ResourceNotFoundException, while ``get-inference-profile`` on
        the same id returns an ARN that carries the account id.
        """
        from app.step_handlers.knowledge_base_step import _build_model_arn

        assert _build_model_arn("eu-central-1", "us.anthropic.claude-sonnet-5", "111122223333") == (
            "arn:aws:bedrock:eu-central-1:111122223333:inference-profile/eu.anthropic.claude-sonnet-5"
        )

    def test_an_unknown_account_is_an_error_not_a_broken_arn(self):
        """Without the account there is no valid profile ARN to build, so raise.

        ``_get_account_id`` swallows its failures and returns ``""``. Emitting
        ``arn:aws:bedrock:<region>::inference-profile/…`` from that would be
        accepted by ``create_knowledge_base`` and fail later inside Bedrock as
        "unable to assume the given role", pointing every reader at the IAM
        policy for a defect that is in the model ARN.
        """
        from app.step_handlers.knowledge_base_step import _build_model_arn

        with pytest.raises(ValueError, match="inference profile"):
            _build_model_arn("eu-central-1", "us.anthropic.claude-sonnet-5")

    @pytest.mark.parametrize(
        ("model_id", "is_profile"),
        [
            ("us.anthropic.claude-sonnet-5", True),
            ("eu.anthropic.claude-sonnet-5", True),
            ("apac.anthropic.claude-sonnet-5", True),
            ("ap.anthropic.claude-sonnet-5", True),
            ("anthropic.claude-sonnet-5", False),
            ("amazon.titan-embed-text-v2:0", False),
            ("cohere.embed-english-v3", False),
            # ``global.`` is region-INDEPENDENT, so repoint_regional_prefix must
            # leave it alone — but it is still a profile, and its ARN still
            # carries the account. ``list-inference-profiles`` in us-east-1
            # returns global.anthropic.*, global.cohere.embed-v4:0 and others
            # with account-bearing inference-profile ARNs.
            ("global.anthropic.claude-sonnet-5", True),
            ("global.cohere.embed-v4:0", True),
        ],
    )
    def test_is_inference_profile_id(self, model_id, is_profile):
        from app.services.region_models import is_inference_profile_id

        assert is_inference_profile_id(model_id) is is_profile

    def test_a_global_profile_keeps_its_prefix_but_gets_a_profile_arn(self):
        """The two rules pull in different directions and both must hold.

        ``global.`` is region-independent, so it must not be rewritten to
        ``eu.`` — that would name a different profile. But it IS a profile, so
        the ARN must be the account-bearing ``inference-profile`` form. An
        earlier draft of ``is_inference_profile_id`` reused the repoint-only
        geography set and got the second half wrong for every global model.
        """
        from app.step_handlers.knowledge_base_step import _build_model_arn

        assert _build_model_arn("eu-central-1", "global.anthropic.claude-sonnet-5", "111122223333") == (
            "arn:aws:bedrock:eu-central-1:111122223333:inference-profile/global.anthropic.claude-sonnet-5"
        )

    @pytest.mark.parametrize(
        "embedding_model_id",
        ["amazon.titan-embed-text-v2:0", "cohere.embed-english-v3", "cohere.embed-multilingual-v3"],
    )
    def test_knowledge_base_embedding_model_is_untouched(self, embedding_model_id):
        """``_build_model_arn`` also builds the ``embeddingModelId`` ARN.

        Embedding models have no cross-region inference profiles, so ADDING a
        geography prefix here yields ``foundation-model/eu.amazon.titan-…``,
        which Bedrock rejects. Repoint-only, never add.
        """
        from app.step_handlers.knowledge_base_step import _build_model_arn

        assert _build_model_arn("eu-central-1", embedding_model_id) == (
            f"arn:aws:bedrock:eu-central-1::foundation-model/{embedding_model_id}"
        )

    def test_model_picker_offline_fallback(self):
        from app.services.model_catalog import _fallback_for

        assert [m["modelId"] for m in _fallback_for("eu-central-1")] == [
            "eu.anthropic.claude-sonnet-5",
            "eu.anthropic.claude-opus-4-8",
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        ]

    def test_model_picker_fallback_does_not_mutate_the_shared_constant(self):
        """``_FALLBACK`` is a module-level list shared across requests."""
        from app.services import model_catalog

        model_catalog._fallback_for("eu-central-1")
        assert model_catalog._FALLBACK[0]["modelId"] == "us.anthropic.claude-sonnet-5"

    def test_generator_spec_runtime_node_is_repointed(self, monkeypatch):
        """The generator LLM emits ``us.`` in its examples; the spec is repaired."""
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        from app.services.agent_generator import _normalize_spec

        spec = {
            "nodes": [
                {"type": "runtime", "configuration": {"model": {"modelId": "us.anthropic.claude-sonnet-5"}}},
            ]
        }
        _normalize_spec(spec)
        assert spec["nodes"][0]["configuration"]["model"]["modelId"] == "eu.anthropic.claude-sonnet-5"

    def test_legacy_model_error_message_suggests_reachable_ids(self, monkeypatch):
        """Telling a Frankfurt user to type ``us.…`` sends them in circles."""
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        from app.models.deployment_models import _validate_bedrock_model_id

        with pytest.raises(ValueError) as exc:
            _validate_bedrock_model_id("us.anthropic.claude-3-5-sonnet-20241022-v2:0")
        # The rejected ID is echoed verbatim, so only assert on the suggestions.
        suggestions = str(exc.value).split("Use a current ID such as", 1)[1]
        assert "eu.anthropic.claude-sonnet-5" in suggestions
        assert "us." not in suggestions


class TestGeneratedAgenticRagHelper:
    """The RAG tools derive their prefix at RUNTIME, inside the deployed agent.

    Deriving it there rather than at codegen time means an agent generated
    before the platform became region-agnostic still resolves a live profile.
    """

    def _helper(self):
        from app.services.agentic_rag_codegen import _RAG_COMMON_PREAMBLE

        ns: dict = {}
        exec(compile(_RAG_COMMON_PREAMBLE, "<rag-preamble>", "exec"), ns)
        return ns["_rag_regional_model"]

    @pytest.mark.parametrize(("region", "prefix"), REGION_PREFIXES)
    def test_repoints_at_the_runtime_region(self, monkeypatch, region, prefix):
        monkeypatch.setenv("APP_AWS_REGION", region)
        assert self._helper()("us.anthropic.claude-sonnet-5") == f"{prefix}.anthropic.claude-sonnet-5"

    def test_global_profile_is_never_rewritten(self, monkeypatch):
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        assert self._helper()("global.anthropic.claude-sonnet-5") == "global.anthropic.claude-sonnet-5"

    def test_on_demand_id_is_never_prefixed(self, monkeypatch):
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        assert self._helper()("amazon.nova-2-lite-v1:0") == "amazon.nova-2-lite-v1:0"

    def test_app_aws_region_wins_over_aws_region(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        assert self._helper()("us.anthropic.claude-sonnet-5") == "eu.anthropic.claude-sonnet-5"


class TestRegionRestrictedMcpCatalog:
    """``aws-mcp``'s preview endpoint is us-east-1-only, not a regional alias."""

    def test_aws_mcp_is_unavailable_outside_us_east_1(self):
        from app.services.mcp_catalog import is_available_in_region

        assert is_available_in_region("aws-mcp", "us-east-1") is True
        assert is_available_in_region("aws-mcp", "eu-central-1") is False

    def test_unrestricted_entries_are_available_everywhere(self):
        from app.services.mcp_catalog import is_available_in_region

        assert is_available_in_region("aws-knowledge", "eu-central-1") is True
        assert is_available_in_region("deepwiki", "ap-southeast-2") is True

    def test_unknown_ids_are_not_reported_as_region_blocked(self):
        """Unknown ids are the deployer's error to raise, not this helper's."""
        from app.services.mcp_catalog import is_available_in_region

        assert is_available_in_region("no-such-server", "eu-central-1") is True

    def test_restricted_list_is_empty_in_us_east_1(self):
        from app.services.mcp_catalog import region_restricted_servers

        assert region_restricted_servers("us-east-1") == []
        assert region_restricted_servers("eu-central-1") == ["aws-mcp"]

    def test_the_restriction_reaches_the_api_response(self):
        """``McpServerSummary`` is an explicit whitelist, so a field added to the
        catalog is invisible to the UI unless it is added here too. Without this
        the only signal is a server-side log the user never sees."""
        from app.routers.mcp_servers import _summary
        from app.services.mcp_catalog import get_mcp_server

        assert _summary(get_mcp_server("aws-mcp")).region_restricted == ["us-east-1"]
        assert _summary(get_mcp_server("deepwiki")).region_restricted is None


class TestNewWorkflowDefaults:
    def test_empty_workflow_metadata_uses_the_deployment_region(self, monkeypatch):
        """Stored user data — a literal would show Frankfurt users us-east-1."""
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        from app.services.flow_storage import _create_empty_workflow

        assert _create_empty_workflow()["metadata"]["aws_region"] == "eu-central-1"

    def test_us_east_1_still_gets_us_east_1(self, monkeypatch):
        monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
        from app.services.flow_storage import _create_empty_workflow

        assert _create_empty_workflow()["metadata"]["aws_region"] == "us-east-1"


class TestExportedEnvExample:
    """``.env.example`` is copied to ``.env`` and run with, so its ``MODEL_ID``
    has to name a profile that resolves where the user is."""

    def _config(self, model_id, provider="bedrock"):
        from app.models.deployment_models import RuntimeConfig

        return RuntimeConfig(name="demo", model={"modelId": model_id}, system_prompt="hi", model_provider=provider)

    def test_bedrock_model_id_is_regionalized(self, monkeypatch):
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        from app.services.python_exporter import build_env_example

        assert "MODEL_ID=eu.anthropic.claude-sonnet-5" in build_env_example(
            self._config("us.anthropic.claude-sonnet-5")
        )

    def test_us_east_1_is_unchanged(self, monkeypatch):
        monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
        from app.services.python_exporter import build_env_example

        assert "MODEL_ID=us.anthropic.claude-sonnet-5" in build_env_example(
            self._config("us.anthropic.claude-sonnet-5")
        )

    def test_non_bedrock_model_name_is_never_prefixed(self, monkeypatch):
        """``eu.gpt-4o`` is not a thing; only Bedrock has geography profiles."""
        monkeypatch.setenv("APP_AWS_REGION", "eu-central-1")
        from app.services.python_exporter import build_env_example

        assert "MODEL_ID=gpt-4o" in build_env_example(self._config("gpt-4o", provider="openai"))


class TestPresignedUrlsUseTheRegionalEndpoint:
    """Presigned artifact URLs must be signed for the bucket's OWN region.

    botocore resolves ``s3`` to the global ``s3.amazonaws.com`` host regardless
    of ``region_name``. Ordinary API calls survive it (the region redirector
    retries the 301), but ``generate_presigned_url`` makes no request, so the
    global host is baked into a SigV4 signature that covers ``Host`` — the
    browser follows a 307 to the regional host and gets 403
    SignatureDoesNotMatch. Found by deploying to eu-central-1 for real; this was
    invisible in us-east-1, where the global host IS the regional host.
    """

    @pytest.fixture(autouse=True)
    def _dummy_credentials(self, monkeypatch):
        """Signing needs *some* credentials; no request is ever made."""
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x" * 40)
        monkeypatch.delenv("AWS_PROFILE", raising=False)

    @pytest.mark.parametrize("region", [r for r, _ in REGION_PREFIXES])
    def test_presigned_host_carries_the_region(self, region):
        from app.deployment_handler import _artifacts_s3_client

        client = _artifacts_s3_client(region)
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": "artifacts-bucket", "Key": "x.zip"}, ExpiresIn=60
        )
        assert url.split("?")[0] == f"https://artifacts-bucket.s3.{region}.amazonaws.com/x.zip"

    def test_the_global_endpoint_is_never_used(self):
        """The exact bug: ``bucket.s3.amazonaws.com`` for a Frankfurt bucket."""
        from app.deployment_handler import _artifacts_s3_client

        url = _artifacts_s3_client("eu-central-1").generate_presigned_url(
            "get_object", Params={"Bucket": "artifacts-bucket", "Key": "x.zip"}, ExpiresIn=60
        )
        assert "artifacts-bucket.s3.amazonaws.com" not in url


class TestRepointVersusAdd:
    """``repoint_regional_prefix`` and ``to_regional_model_id`` differ on ONE case.

    Both re-point an existing geography prefix. Only ``to_regional_model_id``
    *adds* one to a bare on-demand ID — correct for the agent's chat model (the
    converse API rejects un-prefixed Anthropic IDs), wrong anywhere the ID lands
    in a ``foundation-model/`` ARN or on the user's canvas.
    """

    @pytest.mark.parametrize("region", [r for r, _ in REGION_PREFIXES])
    @pytest.mark.parametrize(
        "bare_model_id",
        ["amazon.titan-embed-text-v2:0", "cohere.embed-english-v3", "amazon.nova-2-lite-v1:0"],
    )
    def test_repoint_never_adds_a_prefix(self, region, bare_model_id):
        from app.services.region_models import repoint_regional_prefix

        assert repoint_regional_prefix(bare_model_id, region) == bare_model_id

    def test_to_regional_still_adds_one(self):
        """The chat-model path relies on this — do not "fix" it to match."""
        from app.services.region_models import to_regional_model_id as _to_regional

        assert _to_regional("anthropic.claude-sonnet-5", "eu-central-1") == "eu.anthropic.claude-sonnet-5"

    @pytest.mark.parametrize(("region", "prefix"), REGION_PREFIXES)
    def test_both_repoint_an_existing_prefix_identically(self, region, prefix):
        from app.services.region_models import repoint_regional_prefix
        from app.services.region_models import to_regional_model_id as _to_regional

        mid = "us.anthropic.claude-sonnet-5"
        assert (
            repoint_regional_prefix(mid, region) == _to_regional(mid, region) == f"{prefix}.anthropic.claude-sonnet-5"
        )

    def test_repoint_leaves_global_alone(self):
        from app.services.region_models import repoint_regional_prefix

        assert (
            repoint_regional_prefix("global.anthropic.claude-sonnet-5", "eu-central-1")
            == "global.anthropic.claude-sonnet-5"
        )

    def test_repoint_preserves_the_legacy_version_suffix(self):
        from app.services.region_models import repoint_regional_prefix

        assert (
            repoint_regional_prefix("us.anthropic.claude-haiku-4-5-20251001-v1:0", "eu-central-1")
            == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        )

    def test_repoint_handles_empty_input(self):
        from app.services.region_models import repoint_regional_prefix

        assert repoint_regional_prefix("", "eu-central-1") == ""


class TestApacPrefixIsNotAp:
    """`ap` was this repo's convention at all three prefix sites and it is wrong.

    Checked against the live Bedrock control plane rather than from memory —
    `aws bedrock list-inference-profiles` in four regions, distinct prefixes:

        us-east-1        us, global
        eu-central-1     eu, global
        ap-northeast-1   apac, global, jp
        ap-southeast-2   apac, global, au

    So `ap.anthropic.…` resolves to nothing in any region, which means an APAC
    deployment on the old convention failed at first invoke exactly the way
    Frankfurt did with `us.`. These pin the corrected value in all three places
    that must agree, because the failure only shows up at runtime.
    """

    def test_the_backend_says_apac(self):
        assert region_inference_prefix("ap-northeast-1") == "apac"
        assert region_inference_prefix("ap-south-1") == "apac"

    def test_no_source_file_still_derives_a_bare_ap_prefix(self):
        """The three sites are in three languages, so no type checker links them."""
        import pathlib as _p

        root = _p.Path(__file__).resolve().parents[2]
        targets = [
            root / "backend" / "src" / "app" / "services" / "region_models.py",
            root / "backend" / "src" / "app" / "services" / "agentic_rag_codegen.py",
            root / "frontend" / "src" / "utils" / "awsRegion.ts",
            root / "infra" / "stacks" / "platform" / "lambdas.py",
        ]
        for f in targets:
            assert f.exists(), f
            lines = f.read_text().splitlines()
            for i, line in enumerate(lines):
                if not any(p in line for p in ("startswith('ap-')", 'startswith("ap-")', "startsWith('ap-')")):
                    continue
                # region_models.py puts the `return "apac"` on the NEXT line, so
                # look at a small window rather than the matching line alone.
                window = "\n".join(lines[i : i + 3])
                assert "'apac'" in window or '"apac"' in window, (
                    f"{f.name}:{i + 1} still maps ap-* to a bare 'ap': {line.strip()}"
                )

    def test_a_stale_ap_prefix_is_recognised_not_double_prefixed(self):
        """`ap.` stays in CROSS_REGION_PREFIXES on purpose: users were told to
        type it, and treating it as unprefixed would yield `eu.ap.anthropic.…`."""
        assert to_regional_model_id("ap.anthropic.claude-sonnet-5", "eu-central-1") == "eu.anthropic.claude-sonnet-5"
        assert to_regional_model_id("apac.anthropic.claude-sonnet-5", "eu-central-1") == "eu.anthropic.claude-sonnet-5"
