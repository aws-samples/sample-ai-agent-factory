"""cleanup.sh must delegate dynamic teardown instead of parsing legacy fields.

The former bash loop interpreted ``gateway_result`` itself, stripped provider
prefixes, guessed API namespaces, and swallowed every AWS error.  That entire
class of drift is removed: the script now sends a UUID deployment id to the
deployment Lambda and consumes the guarded teardown result synchronously.
"""

from __future__ import annotations

import pathlib
import re

_CLEANUP_SH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cleanup.sh"


def _cleanup_sh() -> str:
    return _CLEANUP_SH.read_text()


def _function(name: str) -> str:
    src = _cleanup_sh()
    start = src.index(f"{name}() {{")
    match = re.search(r"^\}", src[start:], re.MULTILINE)
    assert match
    return src[start : start + match.end()]


def _dynamic_cleanup() -> str:
    src = _cleanup_sh()
    start = src.index("cleanup_deployment_resources() {")
    end = src.index("# ── Resource ownership", start)
    return src[start:end]


def test_dynamic_cleanup_invokes_the_deployment_lambda_synchronously() -> None:
    body = _dynamic_cleanup()
    assert '"_stack_cleanup_delete": True' in body
    assert '"expected_stack_owner": sys.argv[2]' in body
    assert "--invocation-type RequestResponse" in body
    assert '--function-name "${function_name}"' in body


def test_only_canonical_uuid_rows_are_sent_to_teardown() -> None:
    body = _dynamic_cleanup()
    assert "uuid.UUID(raw)" in body
    assert "canonical != raw.lower()" in body
    assert "test-*" in body and "gen-*" in body


def test_lambda_failure_or_retention_stops_before_cdk_destroy() -> None:
    body = _dynamic_cleanup()
    assert 'if [[ "${success}" != "true" ]]' in body
    assert "Stopping before CDK destroy" in body
    assert "return 1" in body


def test_shell_no_longer_parses_resource_ids_or_provider_prefixes() -> None:
    body = _dynamic_cleanup()
    for legacy_field in (
        "gateway_result",
        "policy_result",
        "memory_result",
        "guardrails_result",
        "knowledge_base_result",
        "connector_credential_providers",
        "connector_secret_arns",
        "cp_entry",
    ):
        assert legacy_field not in body


def test_shell_never_directly_deletes_dynamic_agentcore_resources() -> None:
    src = _cleanup_sh()
    for command in (
        "delete-agent-runtime",
        "delete-gateway",
        "delete-gateway-target",
        "delete-policy",
        "delete-policy-engine",
        "delete-memory",
        "delete-oauth2-credential-provider",
        "delete-api-key-credential-provider",
        "delete-guardrail",
        "delete-knowledge-base",
        "delete-data-source",
    ):
        assert command not in src


def test_main_preserves_teardown_authority_until_delegation_succeeds() -> None:
    main = _function("main")
    cleanup = main.index("cleanup_deployment_resources")
    sweep = main.index("sweep_orphan_resources")
    destroy = main.index("run_cdk_destroy")
    assert cleanup < sweep < destroy
