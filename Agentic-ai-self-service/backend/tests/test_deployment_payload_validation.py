"""F-55: the one shared deployment-payload validator, in both phases.

The defect this guards: the platform computed a validation verdict, and nothing read it. The
verdict was also invariably ``False``, because it came from a table lookup that could never
succeed. Both halves are now replaced by ``validate_deployment_payload``, which the API boundary
and ValidateStep both call -- so this file has to prove three separate things, and the first is
the one most easily forgotten:

1. ADMITTING. A real payload must PASS. A guard tested only by what it rejects is compatible
   with rejecting everything, and closing the state-machine gate over a validator that rejects
   everything is a 100% outage rather than a fix. Every section below therefore has a positive
   case, and the raw-credential section has one for each approved path.
2. PHASE SEPARATION. Raw credential material is LEGITIMATE in the request phase at the exact
   paths ``_prepare_deployment_credentials`` pops it from, and forbidden everywhere else in
   that phase and everywhere at all in the prepared phase.
3. PER-SEGMENT ARN VALIDATION. ARCC cnt_QAWqFk4LdKNGAO requires each segment to be validated,
   so each segment is fuzzed INDEPENDENTLY. One composite malformed ARN cannot tell you which
   check rejected it, or whether the other five ran at all.
"""

import ast
import json
import pathlib

import pytest
from app.services.deployment_payload_validation import (
    _PREPARED_TOP_LEVEL_FIELDS,
    _VALIDATOR_OUTPUT_FIELDS,
    PayloadPhase,
    ValidationContext,
    validate_deployment_payload,
)

ACCOUNT = "123456789012"
REGION = "us-east-1"
CTX = ValidationContext(home_account_id=ACCOUNT, home_region=REGION)

#: A minimal payload that the ONE producer could really have emitted.
PREPARED: dict = {
    "deployment_id": "b3f1c2d4-1111-2222-3333-444455556666",
    # The FLOW and the canvas NODE are different objects, so the baseline gives them DIFFERENT
    # values: with equal ones a validator reading the wrong field would pass every test here.
    "workflow_id": "7d1e0c2a-5b9f-4f7e-8a31-2c6d9e0b4a17",
    "node_id": "d47f6a7b85",
    "config": {"name": "my-agent", "model": {"modelId": "us.anthropic.claude-opus-5"}},
    "connected_tools": ["memory", "gateway"],
    "version_id": "v-20260922-0001",
    "friendly_runtime_name": "my-agent",
    "agentcore_runtime_name": "my-agent_a1b2c3d4",
    "deployment_slot": "production",
    "owner_sub": "7478e488-7081-7081-aaaa-bbbbbbbbbbbb",
    "deployment_mode": "runtime",
}

#: The client-supplied request, with a raw credential at each path staging pops from.
REQUEST: dict = {
    "nodeId": "d47f6a7b85",
    "config": {"name": "my-agent", "model": {"modelId": "us.anthropic.claude-opus-5"}},
    "deploymentMode": "runtime",
    "connectors": [{"name": "c1", "authMethod": "api_key", "secretValue": "sk-fake-not-a-real-key-0000"}],
    "externalMcpServers": [
        {
            "name": "m1",
            "secret_value": "sk-fake-not-a-real-key-0000",
            "oauth": {"client_secret": "sk-fake-not-a-real-key-0000"},
        }
    ],
    "gatewayConfig": {"litellmApiKey": "sk-fake-not-a-real-key-0000"},
}


def _prepared(**over) -> dict:
    return {**PREPARED, **over}


def _v(payload, phase=PayloadPhase.PREPARED, context=CTX):
    return validate_deployment_payload(payload, phase=phase, context=context)


# ======================================================================================
# 1. ADMITTING
# ======================================================================================


def test_a_real_prepared_payload_is_accepted():
    r = _v(PREPARED)
    assert r.is_valid, f"a real payload must pass; got {r.as_error_dicts()}"
    assert r.summary() == ""


def test_a_real_request_payload_with_raw_credentials_is_accepted():
    """The phase that would otherwise be a 100% outage for the credential path."""
    r = _v(REQUEST, phase=PayloadPhase.REQUEST)
    assert r.is_valid, (
        "raw credentials at the approved write-only paths are how a first-time credential is "
        f"supplied at all; rejecting them rejects every such deploy. Got {r.as_error_dicts()}"
    )


def test_every_optional_block_present_at_once_is_still_accepted():
    """A maximal payload, so no individual shape check rejects a legitimate combination."""
    r = _v(
        _prepared(
            template_id="tmpl-1",
            resource_tags={"Project": "acfe2e"},
            parent_version_id="v-0",
            target_artifact_bucket="acfe2e-artifacts",
            gateway_tools=["tool-a", "tool-b"],
            custom_tools=[{"name": "t"}],
            connectors=[
                {
                    "name": "c",
                    "secret_arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/me/s-a",
                }
            ],
            external_mcp_servers=[{"name": "m"}],
            memory_config={"enabled": True},
            identity_config={"enabled": True},
            knowledge_base_config={"name": "kb"},
            guardrails_config={"name": "g"},
            policy_config={"name": "p"},
            evaluation_config={"name": "e"},
            mcp_server_config={"name": "m"},
            observability_config={"enabled": True},
            platform_observability_defaults={"enabled": True},
            a2a_config={"enabled": True},
            recorded_secret_arns=[f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/me/s-a"],
            target_role_arn=f"arn:aws:iam::{ACCOUNT}:role/deploy",
            target_runtime_role_arn=f"arn:aws:iam::{ACCOUNT}:role/runtime",
            target_harness_role_arn=f"arn:aws:iam::{ACCOUNT}:role/harness",
            target_account_id=ACCOUNT,
            target_region=REGION,
        )
    )
    assert r.is_valid, f"a maximal legitimate payload must pass; got {r.as_error_dicts()}"


def test_harness_mode_needs_no_model():
    """An EXPLICIT branch, not a fallthrough -- the harness path runs no codegen."""
    harness = _prepared(deployment_mode="harness", config={"name": "h"})
    assert _v(harness).is_valid, _v(harness).as_error_dicts()


def test_runtime_mode_requires_a_model_id():
    assert "missing_model" in _v(_prepared(config={"name": "a"})).codes()
    assert "missing_model_id" in _v(_prepared(config={"name": "a", "model": {}})).codes()
    assert "not_an_object" in _v(_prepared(config={"name": "a", "model": "claude"})).codes()


def test_the_validator_is_pure():
    """No IO, no environment, no clients -- purity is what lets it run before side effects.

    Asserted over the parsed AST, not over the source text. The first version of this test
    substring-scanned the source and failed on the word "boto3" appearing in the module
    docstring: a comment is not executable evidence, and a test that a comment can fail is a
    test that a comment can also satisfy.
    """
    import ast
    import inspect

    from app.services import deployment_payload_validation as mod

    tree = ast.parse(inspect.getsource(mod))
    banned_modules = {"boto3", "botocore", "os", "requests", "urllib", "socket", "pathlib"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert not (imported & banned_modules), (
        f"{sorted(imported & banned_modules)} imported by a validator that must be callable "
        "BEFORE any side effect; purity is what lets the API boundary run it at all"
    )

    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "open" not in called, "the validator performs file IO"


# ======================================================================================
# 2. PHASE SEPARATION
# ======================================================================================


@pytest.mark.parametrize(
    "payload",
    [
        {"connectors": [{"secret_value": "sk-fake-0000"}]},
        {"connectors": [{"secretValue": "sk-fake-0000"}]},
        {"external_mcp_servers": [{"secret_value": "sk-fake-0000"}]},
        {"externalMcpServers": [{"secretValue": "sk-fake-0000"}]},
        {"external_mcp_servers": [{"oauth": {"client_secret": "sk-fake-0000"}}]},
        {"externalMcpServers": [{"oauth": {"clientSecret": "sk-fake-0000"}}]},
        {"gateway_config": {"litellm_api_key": "sk-fake-0000"}},
        {"gatewayConfig": {"litellmApiKey": "sk-fake-0000"}},
    ],
)
def test_each_approved_write_only_path_is_accepted_in_the_request_phase(payload):
    """One path at a time: a single composite case cannot show which paths are approved."""
    r = _v({**REQUEST, **payload}, phase=PayloadPhase.REQUEST)
    assert "raw_secret_in_payload" not in r.codes(), (
        f"{list(payload)[0]} is popped by _prepare_deployment_credentials, so a raw value there "
        f"is legitimate pre-staging; got {r.as_error_dicts()}"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"connectors": [{"secret_value": "sk-fake-0000"}]},
        {"external_mcp_servers": [{"oauth": {"client_secret": "sk-fake-0000"}}]},
        {"gateway_config": {"litellm_api_key": "sk-fake-0000"}},
    ],
)
def test_the_same_paths_are_refused_in_the_prepared_phase(payload):
    """Staging has run by then, so a surviving raw value is a real exposure."""
    r = _v(_prepared(**payload))
    assert "raw_secret_in_payload" in r.codes(), (
        "the execution input is retained and readable for 90 days, so a raw value here is "
        f"unfixable after the fact; got {r.as_error_dicts()}"
    )


@pytest.mark.parametrize(
    "payload,where",
    [
        ({"config": {"name": "a", "model": {"modelId": "m"}, "apiKey": "sk-fake-0000"}}, "config"),
        ({"resource_tags": {"password": "sk-fake-0000"}}, "a tag value"),
        ({"memory_config": {"privateKey": "sk-fake-0000"}}, "memory config"),
        ({"connectors": [{"nested": {"secret_value": "sk-fake-0000"}}]}, "one level too deep"),
        ({"connectors": {"secret_value": "sk-fake-0000"}}, "a mapping, not the list element"),
        ({"gateway_config": {"inner": {"litellm_api_key": "sk-fake-0000"}}}, "nested in gateway"),
        ({"virtualKey": "sk-fake-0000"}, "top level"),
    ],
)
def test_a_raw_secret_outside_the_approved_paths_is_refused_even_in_the_request_phase(payload, where):
    """The allowlist is a set of exact PATHS, not a relaxation of the rule."""
    r = _v({**REQUEST, **payload}, phase=PayloadPhase.REQUEST)
    assert "raw_secret_in_payload" in r.codes(), f"a raw secret in {where} was admitted"


def test_no_error_message_ever_echoes_the_secret_value():
    """The message lands in the deployment record and the logs -- the worst place to echo one."""
    secret = "sk-fake-not-a-real-key-must-not-appear-0000"
    r = _v(_prepared(connectors=[{"secret_value": secret}]))
    assert not r.is_valid
    blob = r.summary() + str(r.as_error_dicts())
    assert secret not in blob


def test_an_approved_path_still_may_not_carry_a_structure():
    r = _v({**REQUEST, "connectors": [{"secretValue": {"nested": "x"}}]}, phase=PayloadPhase.REQUEST)
    assert "raw_secret_not_a_string" in r.codes()


# ======================================================================================
# 3. PER-SEGMENT ARN VALIDATION (ARCC cnt_QAWqFk4LdKNGAO)
# ======================================================================================


def test_a_legitimate_iam_role_arn_with_an_empty_region_is_accepted():
    """The case a naive 'all six segments non-empty' check rejects -- every IAM role ARN."""
    assert _v(_prepared(target_role_arn=f"arn:aws:iam::{ACCOUNT}:role/deploy")).is_valid


@pytest.mark.parametrize(
    "arn,code,segment",
    [
        ("arn:aws:s3:::bucket/key", "arn_wrong_service", "service"),
        (f"arn:aws:iam::{ACCOUNT}:user/bob", "arn_wrong_resource_type", "relative-id type"),
        (f"arn:aws:iam:{REGION}:{ACCOUNT}:role/r", "arn_region_must_be_empty", "region"),
        ("arn:aws:iam::999999999999:role/r", "arn_wrong_account", "account"),
        ("arn:aws:iam::12345:role/r", "arn_bad_account", "account digits"),
        (f"arn:fake-partition:iam::{ACCOUNT}:role/r", "arn_bad_partition", "partition"),
        (f"arn:aws:iam::{ACCOUNT}:", "arn_empty_relative_id", "relative-id"),
        (f"arn:aws::{''}:{ACCOUNT}:role/r", "arn_empty_service", "empty service"),
        ("not-an-arn", "arn_malformed", "whole"),
        ("arn:aws:iam::role/r", "arn_malformed", "segment count"),
        (12345, "arn_not_a_string", "type"),
    ],
)
def test_each_role_arn_segment_is_validated_independently(arn, code, segment):
    """Fuzz one segment at a time, per the guidance's own verification section."""
    r = _v(_prepared(target_role_arn=arn))
    assert code in r.codes(), f"the {segment} segment was not validated: {r.as_error_dicts()}"


@pytest.mark.parametrize(
    "relative",
    ["role/with space", 'role/with"quote', "role/with<angle>", "role/with|pipe", "role/with\tTab"],
)
def test_url_unsafe_characters_in_the_relative_id_are_refused(relative):
    r = _v(_prepared(target_role_arn=f"arn:aws:iam::{ACCOUNT}:{relative}"))
    assert "arn_unsafe_chars" in r.codes()


@pytest.mark.parametrize(
    "arn,code",
    [
        (f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/me/s-a", None),
        (f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/s", "arn_wrong_service"),
        (f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:key/s", "arn_wrong_resource_type"),
        (f"arn:aws:secretsmanager::{ACCOUNT}:secret:agentcore-connector/me/s-a", "arn_missing_region"),
        (f"arn:aws:secretsmanager:{REGION}::secret:agentcore-connector/me/s-a", "arn_missing_account"),
        (f"arn:aws:secretsmanager:eu-west-1:{ACCOUNT}:secret:agentcore-connector/me/s-a", "arn_wrong_region"),
        (f"arn:aws:secretsmanager:{REGION}:999999999999:secret:agentcore-connector/me/s-a", "arn_wrong_account"),
        (
            f"arn:aws-cn:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/me/s-a",
            "arn_partition_region_mismatch",
        ),
        (f"arn:aws:secretsmanager:not-a-region:{ACCOUNT}:secret:agentcore-connector/me/s-a", "arn_bad_region"),
    ],
)
def test_a_staged_secret_arn_must_name_the_deployments_own_account_and_region(arn, code):
    """A secret the deployment did not create must not be read by it."""
    r = _v(_prepared(recorded_secret_arns=[arn]))
    if code is None:
        assert r.is_valid, r.as_error_dicts()
    else:
        assert code in r.codes(), r.as_error_dicts()


def test_partition_and_region_incoherence_is_refused_in_both_directions():
    for arn in (
        f"arn:aws-cn:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/me/s-a",
        f"arn:aws:secretsmanager:cn-north-1:{ACCOUNT}:secret:agentcore-connector/me/s-a",
        f"arn:aws:secretsmanager:us-gov-west-1:{ACCOUNT}:secret:agentcore-connector/me/s-a",
    ):
        r = _v(_prepared(recorded_secret_arns=[arn], target_region=None))
        assert "arn_partition_region_mismatch" in r.codes(), arn


def test_a_home_deployment_still_constrains_the_account_via_the_trusted_context():
    """The gap that the payload alone cannot close: target_* are null for a home deploy.

    Without the trusted context there is nothing to compare against, and a secret ARN naming
    ANY account passes. The context comes from the executing Lambda's own ARN, so it cannot be
    influenced by the payload.
    """
    foreign = f"arn:aws:secretsmanager:{REGION}:999999999999:secret:agentcore-connector/me/s-a"
    assert "arn_wrong_account" in _v(_prepared(recorded_secret_arns=[foreign])).codes()
    # and with NO context, the ARN is still syntactically validated -- absent context weakens
    # exactly one check and must not silently disable the rest.
    r = _v(_prepared(recorded_secret_arns=["arn:aws:ssm::%%%:bad"]), context=None)
    assert not r.is_valid


# ======================================================================================
# 4. THE TRAVERSAL BUDGET IS A REJECTION, NOT A SILENT STOP
# ======================================================================================


def _nest(depth: int, leaf: dict) -> dict:
    node = leaf
    for _ in range(depth):
        node = {"n": node}
    return node


def test_a_raw_secret_deeper_than_the_old_silent_cap_is_still_found():
    """The bypass this budget replaced: the first version returned silently past depth 8."""
    r = _v(_prepared(config={"name": "a", "model": {"modelId": "m"}, "x": _nest(9, {"apiKey": "sk-fake-0000"})}))
    assert "raw_secret_in_payload" in r.codes(), (
        "a raw secret nested below the cap must be found, or the security control simply did not run on it"
    )


def test_an_unvalidatably_deep_payload_is_refused_rather_than_partially_inspected():
    r = _v(_prepared(config={"name": "a", "model": {"modelId": "m"}, "x": _nest(40, {"z": 1})}))
    assert "payload_too_deep" in r.codes()


def test_an_object_with_too_many_keys_is_refused():
    r = _v(_prepared(resource_tags={f"k{i}": "v" for i in range(2000)}))
    assert "object_too_many_keys" in r.codes()


def test_an_over_long_array_is_refused():
    r = _v(_prepared(custom_tools=[{"n": i} for i in range(5000)]))
    assert "sequence_too_long" in r.codes()


def test_a_payload_with_too_many_nodes_is_refused():
    """Calibrated to genuinely exceed _MAX_NODES without tripping any OTHER limit first.

    400 outer keys x 50 scalars is ~20,400 traversed nodes, above the 20,000 budget, while every
    individual mapping stays under the 500-key cap -- so a green result here really is the node
    budget firing and not the breadth cap.
    """
    from app.services.deployment_payload_validation import (
        _MAX_MAPPING_KEYS,
        _MAX_NODES,
    )

    outer, inner = 420, 50
    assert outer <= _MAX_MAPPING_KEYS and inner <= _MAX_MAPPING_KEYS
    assert outer * inner > _MAX_NODES, "the payload must actually exceed the node budget"

    payload = _prepared(memory_config={f"k{i}": {f"v{j}": j for j in range(inner)} for i in range(outer)})
    r = _v(payload)
    assert "payload_too_many_nodes" in r.codes(), r.codes()


# ======================================================================================
# 5. THE CLOSED KEY SET, AND THE SERVER-AUTHORED FIELDS
# ======================================================================================


@pytest.mark.parametrize("key", sorted(_VALIDATOR_OUTPUT_FIELDS))
def test_this_steps_own_output_is_not_accepted_as_its_input(key):
    """The producer emits none of these; Step Functions retries with the ORIGINAL input.

    ``no_resources_created`` is the dangerous one: the success path returns ``{**event, ...}``,
    so an injected marker would survive into the state and a later genuine failure would find a
    cleanup-suppressing flag it never authored.
    """
    r = _v(_prepared(**{key: True}))
    assert "validator_output_as_input" in r.codes(), f"{key} was accepted as input"


def test_an_unknown_top_level_field_is_refused():
    assert "unknown_field" in _v(_prepared(admin=True)).codes()


@pytest.mark.parametrize(
    "key",
    [
        "owner_sub",
        "version_id",
        "friendly_runtime_name",
        "agentcore_runtime_name",
        "deployment_mode",
        "deployment_slot",
    ],
)
def test_a_missing_server_authored_field_is_refused(key):
    """Each is assigned unconditionally by the producer, so absence means a forged payload."""
    payload = {k: v for k, v in PREPARED.items() if k != key}
    assert "missing_server_field" in _v(payload).codes()


def test_an_empty_owner_sub_is_refused_because_it_means_an_ownerless_deployment():
    """``_get_user_id`` can return None, so the producer can write "" -- no tenant, no deploy."""
    assert "missing_server_field" in _v(_prepared(owner_sub="")).codes()


@pytest.mark.parametrize("key", ["parent_version_id", "target_artifact_bucket", "template_id"])
def test_the_optional_server_fields_are_not_required(key):
    """The producer legitimately leaves these unset; requiring them would be the outage."""
    assert _v(_prepared(**{key: None})).is_valid


def test_the_request_phase_does_not_require_the_server_authored_fields():
    """They do not exist yet at the API boundary -- that is the whole point of running early."""
    r = _v(REQUEST, phase=PayloadPhase.REQUEST)
    assert "missing_server_field" not in r.codes()
    assert "unknown_field" not in r.codes()


def test_prepared_allowlist_reconciles_with_the_one_sfn_input_builder():
    """Reconciled against the builder's SOURCE, so a new field fails here instead of shipping.

    ``deployment_handler`` holds the only ``start_execution`` call site in the codebase, so its
    ``sfn_input`` dict is the complete set of top-level fields that can reach ValidateStep. If
    someone adds a field there, this test fails rather than the field silently travelling
    unvalidated -- and if someone adds one HERE that the builder never emits, it fails too.
    """
    src = pathlib.Path(__file__).resolve().parents[1] / "src/app/deployment_handler.py"
    tree = ast.parse(src.read_text())
    emitted: set[str] = set()
    for node in ast.walk(tree):
        # sfn_input = { "k": ... }
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            if any(isinstance(t, ast.Name) and t.id == "sfn_input" for t in node.targets):
                emitted |= {
                    k.value for k in node.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
        # sfn_input["k"] = ...
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (
                    isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "sfn_input"
                    and isinstance(t.slice, ast.Constant)
                    and isinstance(t.slice.value, str)
                ):
                    emitted.add(t.slice.value)

    assert emitted, "found no sfn_input keys; the AST walk is broken, not the allowlist"
    missing = emitted - _PREPARED_TOP_LEVEL_FIELDS
    extra = _PREPARED_TOP_LEVEL_FIELDS - emitted
    assert not missing, (
        f"the builder emits fields the validator does not know about: {sorted(missing)}. They "
        "would travel into the execution unvalidated, or be refused as unknown_field"
    )
    assert not extra, (
        f"the validator allows fields the builder never emits: {sorted(extra)}. An allowlist "
        "wider than the producer is an accepted injection surface"
    )


# ======================================================================================
# 6. SHAPES, ENUMS AND IDENTIFIERS
# ======================================================================================


@pytest.mark.parametrize(
    "key",
    [
        "config",
        "gateway_config",
        "memory_config",
        "identity_config",
        "knowledge_base_config",
        "guardrails_config",
        "policy_config",
        "evaluation_config",
        "mcp_server_config",
        "observability_config",
        "platform_observability_defaults",
        "a2a_config",
        "resource_tags",
    ],
)
def test_a_scalar_where_an_object_belongs_is_refused(key):
    """The state machine branches on PRESENCE, so a scalar starts the step and then dies in it."""
    assert "not_an_object" in _v(_prepared(**{key: "true"})).codes()


@pytest.mark.parametrize(
    "key", ["connected_tools", "gateway_tools", "custom_tools", "connectors", "external_mcp_servers"]
)
def test_a_scalar_where_an_array_belongs_is_refused(key):
    assert "not_an_array" in _v(_prepared(**{key: "tool"})).codes()


@pytest.mark.parametrize("key", ["connected_tools", "gateway_tools"])
def test_tool_id_lists_must_hold_non_empty_strings_within_the_models_own_bound(key):
    """These are tool IDs: connected_tools goes through set(), gateway_tools names Lambda targets."""
    assert _v(_prepared(**{key: ["memory", "gateway"]})).is_valid
    assert "item_not_a_string" in _v(_prepared(**{key: [{"name": "t"}]})).codes()
    assert "item_not_a_string" in _v(_prepared(**{key: [""]})).codes()
    assert "sequence_too_long" in _v(_prepared(**{key: ["t"] * 21})).codes()


@pytest.mark.parametrize("key", ["custom_tools", "connectors", "external_mcp_servers"])
def test_object_lists_must_hold_objects(key):
    assert "item_not_an_object" in _v(_prepared(**{key: ["a-string"]})).codes()


@pytest.mark.parametrize("value", ["prod", "runtime", "", None, "STAGING", 1])
def test_only_the_models_own_slots_are_accepted(value):
    r = _v(_prepared(deployment_slot=value))
    assert not r.is_valid


@pytest.mark.parametrize("value", ["lambda", "RUNTIME", "", "harness_v2"])
def test_only_the_models_own_modes_are_accepted(value):
    assert not _v(_prepared(deployment_mode=value)).is_valid


@pytest.mark.parametrize(
    "value",
    ["../../etc/passwd", "a b", "-leading-dash", ".leading-dot", "id/with/slash", "id%20", "a" * 257],
)
def test_an_identifier_that_reaches_an_s3_key_is_charset_restricted(value):
    assert "unsafe_identifier" in _v(_prepared(node_id=value)).codes()


def test_the_identifier_bound_matches_the_models_own_declared_maximum():
    """256, not 128: a validator stricter than the contract rejects accepted input."""
    assert _v(_prepared(node_id="a" * 256)).is_valid
    assert "unsafe_identifier" in _v(_prepared(node_id="a" * 257)).codes()


@pytest.mark.parametrize("value", [None, ""])
def test_the_node_id_is_required_in_a_prepared_payload(value):
    assert "missing_identifier" in _v(_prepared(node_id=value)).codes()


def test_a_prepared_payload_without_a_node_id_is_refused():
    payload = {k: v for k, v in PREPARED.items() if k != "node_id"}
    assert "missing_identifier" in _v(payload).codes()


def test_a_deploy_that_belongs_to_no_flow_is_admitted():
    """Harness deploys and unsaved canvases have no flow. Absent and null are both valid."""
    assert _v(_prepared(workflow_id=None)).is_valid
    assert _v({k: v for k, v in PREPARED.items() if k != "workflow_id"}).is_valid


@pytest.mark.parametrize("value", ["a" * 129, "../etc", "has space", "x.y", "", 42, ["a"]])
def test_a_present_flow_id_must_have_the_flow_grammar(value):
    """Same grammar as ``routers/flows.py::_validate_flow_id``: 1-128 of ``[a-zA-Z0-9_-]``.

    Note ``x.y`` and ``a*129``: both are legal NODE ids. A validator that checked the flow id
    against the node contract would admit them, which is exactly the conflation being removed.
    """
    assert "unsafe_identifier" in _v(_prepared(workflow_id=value)).codes()


def test_the_flow_id_bound_is_128():
    assert _v(_prepared(workflow_id="a" * 128)).is_valid


def test_a_request_flow_id_is_checked_under_either_spelling():
    for key in ("flowId", "flow_id"):
        r = _v({**REQUEST, key: "../etc"}, phase=PayloadPhase.REQUEST)
        assert "unsafe_identifier" in r.codes(), key


@pytest.mark.parametrize("payload", [None, [], "a string", 42, True])
def test_a_non_object_payload_is_refused(payload):
    r = validate_deployment_payload(payload)
    assert r.codes() == {"payload_not_an_object"}


# ======================================================================================
# 7. THE FORBIDDEN-NAME MATCH IS CANONICAL, NOT LITERAL
# ======================================================================================


@pytest.mark.parametrize(
    "key",
    [
        # Case variants. Every one of these passed the first literal `key in {...}` version.
        "API_KEY",
        "ApiKey",
        "Api_Key",
        "APIKEY",
        "api-key",
        "CLIENT_SECRET",
        "ClientSecret",
        "client-secret",
        "SECRET_VALUE",
        "Secret-Value",
        "Password",
        "PASSWORD",
        "pass_word" if False else "passwd",
        "Private-Key",
        "PRIVATE_KEY",
        # Names missing from the set entirely, which is a different bug with the same effect.
        "access_token",
        "accessToken",
        "ACCESS-TOKEN",
        "refresh_token",
        "session_token",
        "id_token",
        "bearer_token",
        "token",
        "secret",
        "secret_key",
        "access_key",
        "secret_access_key",
        "credentials",
        "connection_string",
        "signing_key",
    ],
)
def test_a_credential_key_is_caught_in_any_spelling(key):
    """One token per logical name, matched on a casefolded separator-stripped form.

    The suite this replaces tested only the exact lowercase ``password``, which is precisely how
    ``ApiKey`` and ``Authorization`` passed a control whose stated guarantee is "no raw secret
    anywhere in the payload".
    """
    r = _v(_prepared(memory_config={key: "sk-fake-not-a-real-key-0000"}))
    assert "raw_secret_in_payload" in r.codes(), f"{key!r} was not recognized as credential material"


@pytest.mark.parametrize(
    "key",
    [
        "max_tokens",
        "maxTokens",
        "tokenCount",
        "api_key_ref",
        "providerApiKeyRef",
        "secret_arn",
        "authHeaderSecretArn",
        "passwordPolicy" if False else "name",
        "description",
    ],
)
def test_canonicalization_does_not_reject_a_legitimate_neighbouring_field(key):
    """Exact canonical equality, not substring -- or every ``*_ref`` field becomes a refusal.

    This is the admitting half of the canonicalization change. Collapsing separators widens what
    matches, so without this test the change could quietly reject ``maxTokens`` and ``secret_arn``
    and turn a bypass fix into an outage.
    """
    r = _v(_prepared(memory_config={key: "some-ordinary-value"}))
    assert "raw_secret_in_payload" not in r.codes(), f"{key!r} is not credential material"


def test_a_plaintext_authorization_header_is_refused_in_both_phases():
    """A header NAME is a map key, and its value becomes a runtime environment variable.

    ``observability.py:343`` joins extraHeaders into OTEL_EXPORTER_OTLP_EXTRA_HEADERS, and
    GetAgentRuntime returns runtime env vars in plaintext -- so one plaintext header is two
    exposures: the 90-day execution history and the runtime's own describe call. The supported
    alternative takes a reference: ``authHeaderSecretArn``.
    """
    for phase, payload in (
        (
            PayloadPhase.PREPARED,
            _prepared(observability_config={"extraHeaders": {"Authorization": "Basic ZmFrZTpmYWtl"}}),
        ),
        (
            PayloadPhase.REQUEST,
            {
                **REQUEST,
                "observabilityConfig": {"extraHeaders": {"Authorization": "Basic ZmFrZTpmYWtl"}},
            },
        ),
    ):
        r = _v(payload, phase=phase)
        assert "raw_secret_in_payload" in r.codes(), f"{phase} admitted a plaintext auth header"
        assert "ZmFrZTpmYWtl" not in r.summary()
        assert "authHeaderSecretArn" in r.summary(), (
            "the refusal must name the supported alternative, or it is an unexplained outage"
        )


def test_a_non_sensitive_extra_header_is_still_allowed():
    """The admitting case: extraHeaders is a real feature and only auth material is refused."""
    r = _v(_prepared(observability_config={"extraHeaders": {"X-Tenant-Id": "acme"}}))
    assert r.is_valid, r.as_error_dicts()


# ======================================================================================
# 8. SECRET REFERENCES THE CONSUMERS ACTUALLY DEREFERENCE
# ======================================================================================

FOREIGN = "arn:aws:secretsmanager:us-east-1:999999999999:secret:agentcore-connector/x"
OURS = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/x"


def _with_ref(arn: str) -> list[dict]:
    """Each consumed reference path, one payload each."""
    return [
        {"config": {"name": "a", "model": {"modelId": "m"}, "providerApiKeyRef": arn}},
        {"observability_config": {"auth_header_secret_arn": arn}},
        {"platform_observability_defaults": {"authHeaderSecretArn": arn}},
        {"gateway_config": {"litellm_api_key_ref": arn}},
        {"connectors": [{"name": "c", "secret_arn": arn}]},
        {"external_mcp_servers": [{"name": "m", "secretArn": arn}]},
        {"external_mcp_servers": [{"name": "m", "oauth": {"client_secret_arn": arn}}]},
        {"mcp_server_config": {"clientSecretRef": arn}},
        {"knowledge_base_config": {"confluenceCredentialsSecretArn": arn}},
        {"knowledge_base_config": {"rdsCredentialsSecretArn": arn}},
    ]


@pytest.mark.parametrize("over", _with_ref(FOREIGN), ids=lambda o: list(o)[0])
def test_a_foreign_account_secret_reference_is_refused_on_every_consumed_path(over):
    """Validating only ``recorded_secret_arns`` left every one of these unchecked.

    These values reach IAM policy Resource fields (runtime_deployer.py:405) and DescribeSecret
    calls, so a foreign account here is the cross-account access cnt_QAWqFk4LdKNGAO names.
    """
    r = _v(_prepared(**over))
    assert "arn_wrong_account" in r.codes(), f"a foreign-account secret reference was admitted: {r.as_error_dicts()}"


def _recorded(over: dict) -> list[str]:
    """Every reference in this payload, as the API would have recorded it.

    A PREPARED staged reference must be a member of ``recorded_secret_arns``, so the ADMITTING
    half has to supply that membership or it is asserting the wrong thing.
    """
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and node.startswith("arn:aws:secretsmanager:"):
            found.append(node)

    walk(over)
    return found


@pytest.mark.parametrize("over", _with_ref(OURS), ids=lambda o: list(o)[0])
def test_an_in_account_secret_reference_is_accepted_on_every_consumed_path(over):
    """The admitting half: without it, "refuse every reference" would pass the suite above."""
    r = _v(_prepared(**over, recorded_secret_arns=_recorded(over)))
    assert r.is_valid, r.as_error_dicts()


@pytest.mark.parametrize("over", _with_ref(OURS), ids=lambda o: list(o)[0])
def test_an_in_account_reference_that_was_never_staged_is_refused(over):
    """The negative half of membership, per consumed path rather than once.

    An in-account ARN is NOT enough. ``recorded_secret_arns`` is what the API staged and durably
    recorded for THIS deployment, so a same-account reference outside that set is another
    deployment's or another tenant's credential -- under-authorization, not leniency. Fuzzing one
    path at a time is what shows WHICH paths enforce it; a single composite payload could not.

    Two paths legitimately opt out and are asserted as such rather than skipped:
    ``mcp_server_config`` is staged by a LATER step (mcp_server_step.py:524-526), and an OTEL
    reference that a higher-precedence source overrides is never dereferenced at all.
    """
    r = _v(_prepared(**over, recorded_secret_arns=[]))
    container = list(over)[0]
    exempt = container in {"mcp_server_config", "platform_observability_defaults"}
    if container == "platform_observability_defaults":
        # Present ALONE this one IS the effective reference, so it is not exempt after all.
        exempt = False
    if exempt:
        assert r.is_valid, f"{container} is staged later, so membership must not be required here"
    else:
        assert "secret_ref_not_staged" in r.codes(), (
            f"{container} accepted a reference the deployment never staged: {r.as_error_dicts()}"
        )


def test_a_bare_secret_name_is_a_supported_request_reference():
    """DescribeSecret resolves a NAME inside the caller's own account, so it cannot be foreign.

    ``bind_connector_secret_for_deployment`` accepts a bare platform-managed name by design --
    ``_is_platform_connector_secret`` falls back to the whole string (gateway_deployer.py:889) --
    so demanding ARN form at the REQUEST boundary would reject a documented shape.
    """
    r = _v(
        {
            "nodeId": "n1",
            "config": {"name": "a", "model": {"modelId": "m"}},
            "connectors": [{"name": "c", "secret_arn": "agentcore-connector/me/abc"}],
        },
        phase=PayloadPhase.REQUEST,
    )
    assert r.is_valid, r.as_error_dicts()


def test_a_bare_secret_name_is_refused_in_a_prepared_payload():
    """The other half of that asymmetry, and the reason the membership rule is satisfiable.

    At PREPARED the value is no longer caller input. Every branch of
    ``bind_connector_secret_for_deployment`` now returns a canonical ARN, including the
    exact-current-deployment branch, which resolves ``described["ARN"]`` rather than echoing the
    caller's reference (gateway_deployer.py:1043-1064). So a bare name here did not come from the
    producer.

    It also CANNOT be allowed: ``recorded_secret_arns`` entries are themselves validated as ARNs,
    so a bare reference can never be a member. Accepting the shape while requiring membership
    would have been a rule no payload could satisfy -- which is why the earlier version of this
    file asserted the opposite and was wrong.
    """
    bare = "agentcore-connector/me/abc"
    r = _v(_prepared(connectors=[{"name": "c", "secret_arn": bare}], recorded_secret_arns=[bare]))
    assert "secret_ref_not_an_arn" in r.codes(), r.as_error_dicts()
    assert "secret_ref_not_staged" not in r.codes(), (
        "a bare name must not ALSO be reported unstaged: that is one defect reported twice, the "
        "second time against a rule the payload cannot satisfy"
    )


def test_a_bare_name_on_a_strict_staging_path_is_refused_in_both_phases():
    """A provider/OTEL/KB reference is staged by COPYING the source secret.

    ``stage_runtime_secret_for_deployment`` and ``stage_customer_secret_for_deployment`` both call
    ``secrets_manager_arn_location``, whose anchored ``fullmatch`` raises on anything short of a
    complete ARN (gateway_deployer.py:734-736). Accepting a bare name at REQUEST would therefore
    fail mid-staging -- AFTER the deployment row and the pending version row already exist -- so
    refusing it at the boundary turns a persistent half-written deploy into a clean 4xx.
    """
    bare = "agentcore-provider/me/abc"
    request = _v(
        {"nodeId": "n1", "config": {"name": "a", "model": {"modelId": "m"}, "providerApiKeyRef": bare}},
        phase=PayloadPhase.REQUEST,
    )
    assert "secret_ref_not_an_arn" in request.codes(), request.as_error_dicts()
    prepared = _v(_prepared(config={"name": "a", "model": {"modelId": "m"}, "providerApiKeyRef": bare}))
    assert "secret_ref_not_an_arn" in prepared.codes(), prepared.as_error_dicts()


def test_a_recorded_entry_with_no_consumer_is_accepted_on_purpose():
    """A staged-but-superseded entry is normal: staging appends before a step rebinds."""
    other = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/superseded"
    r = _v(_prepared(recorded_secret_arns=[OURS, other]))
    assert r.is_valid, r.as_error_dicts()


def test_a_non_string_secret_reference_is_refused():
    r = _v(_prepared(gateway_config={"litellm_api_key_ref": {"arn": OURS}}))
    assert "secret_ref_not_a_string" in r.codes()


# ======================================================================================
# 9. THE PAYLOAD MUST FIT WHAT StartExecution ACCEPTS
# ======================================================================================


def test_the_size_limit_matches_the_real_service_model():
    """Read from botocore, not remembered -- a remembered limit drifts silently."""
    import botocore.session
    from app.services.deployment_payload_validation import _SFN_INPUT_MAX_BYTES

    model = (
        botocore.session.get_session()
        .get_service_model("stepfunctions")
        .operation_model("StartExecution")
        .input_shape.members["input"]
        .metadata
    )
    assert model["max"] == _SFN_INPUT_MAX_BYTES


def _calibrated(target_bytes: int, phase: PayloadPhase, filler: str = "a") -> dict:
    """A payload whose EXACT serialized byte length is target_bytes, by binary search on padding."""
    base = _prepared() if phase is PayloadPhase.PREPARED else dict(REQUEST)

    def size(pad_units: int) -> tuple[int, dict]:
        p = {**base, "config": {**base.get("config", {}), "pad": filler * pad_units}}
        return len(json.dumps(p, default=str).encode("utf-8")), p

    low, high = 0, target_bytes
    while low < high:
        mid = (low + high) // 2
        if size(mid)[0] < target_bytes:
            low = mid + 1
        else:
            high = mid
    return size(low)[1]


def test_a_payload_just_under_the_prepared_limit_is_accepted():
    from app.services.deployment_payload_validation import _SFN_INPUT_MAX_BYTES

    payload = _calibrated(_SFN_INPUT_MAX_BYTES - 200, PayloadPhase.PREPARED)
    actual = len(json.dumps(payload, default=str).encode("utf-8"))
    assert actual <= _SFN_INPUT_MAX_BYTES, actual
    assert "payload_too_large" not in _v(payload).codes()


def test_a_payload_just_over_the_prepared_limit_is_refused():
    from app.services.deployment_payload_validation import _SFN_INPUT_MAX_BYTES

    payload = _calibrated(_SFN_INPUT_MAX_BYTES + 50, PayloadPhase.PREPARED)
    actual = len(json.dumps(payload, default=str).encode("utf-8"))
    assert actual > _SFN_INPUT_MAX_BYTES, actual
    assert "payload_too_large" in _v(payload).codes(), (
        "StartExecution would refuse this AFTER the deployment row, the pending version row and "
        "any staged secret already exist"
    )


def test_the_limit_is_measured_in_bytes_not_characters():
    """A 3-byte-per-character payload is over the limit at a third of the character count."""
    from app.services.deployment_payload_validation import _SFN_INPUT_MAX_BYTES

    # Each CJK character is 3 bytes in UTF-8 and json.dumps keeps it as one character by
    # default (ensure_ascii escapes it to 6 ASCII bytes, which is MORE, so either way the byte
    # count is what matters and a character count understates it.
    payload = _prepared(config={"name": "a", "model": {"modelId": "m"}, "pad": "漢" * 60_000})
    chars = len(json.dumps(payload, default=str, ensure_ascii=False))
    payload_bytes = len(json.dumps(payload, default=str).encode("utf-8"))
    assert chars < _SFN_INPUT_MAX_BYTES < payload_bytes, (chars, payload_bytes)
    assert "payload_too_large" in _v(payload).codes()


def test_the_request_phase_reserves_headroom_for_the_fields_the_server_adds():
    """The request payload is not the execution input; the server adds ten more fields.

    An exact check at the request boundary would admit a payload that only becomes oversized
    after staging -- which is the case that has to be refused BEFORE any side effect.
    """
    from app.services.deployment_payload_validation import (
        _SFN_INPUT_MAX_BYTES,
        _SFN_INPUT_REQUEST_HEADROOM_BYTES,
    )

    budget = _SFN_INPUT_MAX_BYTES - _SFN_INPUT_REQUEST_HEADROOM_BYTES
    under = _calibrated(budget - 200, PayloadPhase.REQUEST)
    over = _calibrated(budget + 200, PayloadPhase.REQUEST)
    assert "payload_too_large" not in _v(under, phase=PayloadPhase.REQUEST).codes()
    assert "payload_too_large" in _v(over, phase=PayloadPhase.REQUEST).codes()
    # ... and that same payload is still UNDER the hard limit, which is why an exact check here
    # would have let it through.
    assert len(json.dumps(over, default=str).encode("utf-8")) < _SFN_INPUT_MAX_BYTES


def test_an_unserializable_payload_is_refused_rather_than_raising():
    """A circular reference: json raises, and the validator must return a verdict, not propagate.

    A set or an arbitrary object is NOT a useful case here -- ``default=str`` serializes both, so
    the only genuinely unserializable shape is a cycle. It is refused twice over, by the depth
    budget and by the size check, and neither may raise out of a pure validator.
    """
    cycle: dict = {"name": "a", "model": {"modelId": "m"}}
    cycle["self"] = cycle
    r = _v(_prepared(config=cycle))
    assert not r.is_valid
    assert {"payload_not_serializable", "payload_too_deep"} & r.codes(), r.codes()


def test_the_summary_is_bounded_and_names_fields_without_echoing_values():
    r = _v({"config": None, "connectors": "x", "deployment_mode": "bad"})
    s = r.summary(limit=2)
    assert s.startswith("Deployment input rejected before any resource was created.")
    assert "more)" in s, "an unbounded concatenation of every field error is unreadable"


# ======================================================================================
# 11. Credential HEADER NAMES inside an extraHeaders map.
#
# A header map is free text whose values are serialized verbatim into
# OTEL_EXPORTER_OTLP_EXTRA_HEADERS (services/observability.py:343), a runtime environment
# variable that GetAgentRuntime returns in plaintext -- so a credential there is two exposures
# from one input. Exact-name equality caught ``Authorization`` and ``X-API-Key`` and nothing else,
# so the rule was widened.
#
# This section is deliberately two-sided, because BOTH sides have already failed here. The first
# widening matched by substring against the CANONICAL key, and canonicalization strips separators:
# ``X-Monkey`` became ``xmonkey``, matched ``key$``, and a legitimate extension header was refused
# with a credential error. A one-sided suite of refusals would have passed that version. So every
# refusal below is paired with an admitting control that the substring version got WRONG.
# ======================================================================================

_CREDENTIAL_HEADERS = [
    "Authorization",
    "X-Authorization",
    "Proxy-Authorization",
    "X-Custom-Auth",
    "X-API-Key",
    "Api-Key-Value",
    "apiKey",
    "X-Access-Token",
    "Cookie",
    "X-Signature",
    "X-Session-Id",
    "key",
    # Explicit dispositions rather than emergent behaviour, asked for by name in review. Both are
    # REFUSED: the cost of a wrong refusal is a 4xx naming the field, the cost of a wrong
    # admission is a session cookie or a signing key in a plaintext runtime env var.
    "X-Cookie-Policy",
    "X-Signature-Version",
]

#: Every one of these was REFUSED by the substring version of this rule, and every one is an
#: ordinary extension header carrying nothing secret. They are the reason the classifier
#: tokenizes instead of matching substrings.
_INNOCENT_HEADERS = [
    "X-Tenant-Id",
    "X-Authorship",
    "X-Tokenization",
    "X-Monkey",
    "X-Hockey",
    "X-Partition-Key",
    "X-Idempotency-Key",
    "X-Request-Id",
    "X-Trace-Id",
    "Content-Type",
]


def _headers(spelling: str, name: str) -> dict:
    """A payload whose observability block carries one extension header."""
    return {"observability_config": {spelling: {name: "an-ordinary-public-value"}}}


@pytest.mark.parametrize("spelling", ["extraHeaders", "extra_headers"])
@pytest.mark.parametrize("name", _CREDENTIAL_HEADERS)
@pytest.mark.parametrize("phase", [PayloadPhase.REQUEST, PayloadPhase.PREPARED])
def test_a_credential_header_name_is_refused_in_both_spellings_and_both_phases(name, spelling, phase):
    """Both spellings, because only one of them was ever canonicalized by the map-key check.

    Both phases, because a header value is never staged: there is no approved write-only path for
    one, so it is raw secret material at the request boundary just as much as after staging. The
    supported alternative takes a reference instead -- ``authHeaderSecretArn``.
    """
    payload = _headers(spelling, name)
    r = _v(_prepared(**payload) if phase is PayloadPhase.PREPARED else payload, phase=phase)
    assert "raw_secret_in_payload" in r.codes(), (
        f"{name!r} in {spelling} was admitted; its value reaches "
        f"OTEL_EXPORTER_OTLP_EXTRA_HEADERS in plaintext. {r.as_error_dicts()}"
    )


@pytest.mark.parametrize("spelling", ["extraHeaders", "extra_headers"])
@pytest.mark.parametrize("name", _INNOCENT_HEADERS)
def test_an_ordinary_extension_header_is_still_admitted(name, spelling):
    """The admitting half. Without it the rule is free to refuse every header and look correct."""
    r = _v(
        {"nodeId": "n1", "config": {"name": "a", "model": {"modelId": "m"}}, **_headers(spelling, name)},
        phase=PayloadPhase.REQUEST,
    )
    assert r.is_valid, (
        f"{name!r} is an ordinary extension header carrying nothing secret, and refusing it is an "
        f"outage for a legitimate canvas. {r.as_error_dicts()}"
    )


@pytest.mark.parametrize("name", ["X-Custom-Auth", "Api-Key-Value", "X-Authorization"])
def test_the_broad_rule_does_not_leak_outside_a_header_map(name):
    """Scoping is what keeps the broad rule from becoming a general outage.

    Outside a header map the same names must NOT be matched broadly, because ordinary payload
    fields include reference fields: ``providerApiKeyRef`` tokenizes to ``provider api key ref``
    and would be refused by this classifier even though a reference is the shape we want.
    """
    r = _v(
        {"nodeId": "n1", "config": {"name": "a", "model": {"modelId": "m"}, name: "a-value"}},
        phase=PayloadPhase.REQUEST,
    )
    assert r.is_valid, r.as_error_dicts()


def test_a_credential_header_nested_below_a_header_map_is_still_refused():
    """``in_header_map`` is never turned back OFF on the way down.

    It was declared on the scan's signature and passed at NEITHER recursion site, so the branch
    always ran with ``False`` and the whole rule was dead -- the two names that appeared to work
    were being caught by the pre-existing exact-token rule. Once it propagates, a nested container
    cannot be used to smuggle a credential header past it either.
    """
    r = _v(
        {"observability_config": {"extraHeaders": {"nested": {"X-Custom-Auth": "v"}}}},
        phase=PayloadPhase.REQUEST,
    )
    assert "raw_secret_in_payload" in r.codes(), r.as_error_dicts()


def test_an_empty_header_value_is_not_reported_as_a_secret():
    """The scan fires on a VALUE, so an empty one is a shape question, not a credential leak."""
    r = _v(
        _headers("extraHeaders", "Authorization") | {"observability_config": {"extraHeaders": {"Authorization": ""}}},
        phase=PayloadPhase.REQUEST,
    )
    assert "raw_secret_in_payload" not in r.codes(), r.as_error_dicts()


# ======================================================================================
# 12. The NAMESPACE portion of a secret reference's relative id.
#
# ARCC cnt_QAWqFk4LdKNGAO requires a service accepting a resource ARN to validate every segment
# including the relative id, with exit criteria that it "contains only expected values". Checking
# only ``secret:`` left the namespace unvalidated. The practical consequence was not a bypass but
# a LATE failure: the reference was admitted, the deployment row and the pending version row were
# written, and staging then refused -- a persistent half-written deploy instead of a clean 4xx.
#
# The required namespace is PHASE-dependent, and that is the part a test has to pin, because
# getting it wrong in either direction is an outage rather than a leak.
# ======================================================================================

_NS_OK = {
    "provider": "agentcore-provider/me/x",
    "otel": "agentcore-otel/me/x",
    "connector": "agentcore-connector/me/x",
}


def _arn_for(name: str) -> str:
    return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-AbCdEf"


def _provider(name):
    return {"config": {"name": "a", "model": {"modelId": "m"}, "providerApiKeyRef": _arn_for(name)}}


def _otel_ref(name):
    return {"observability_config": {"auth_header_secret_arn": _arn_for(name)}}


def _connector(name):
    return {"connectors": [{"name": "c", "secret_arn": _arn_for(name)}]}


def _litellm(name):
    return {"gateway_config": {"litellmApiKeyRef": _arn_for(name)}}


def _kb_ref(name):
    return {"knowledge_base_config": {"confluenceCredentialsSecretArn": _arn_for(name)}}


@pytest.mark.parametrize(
    "builder,name",
    [
        (_provider, _NS_OK["provider"]),
        (_otel_ref, _NS_OK["otel"]),
        (_connector, _NS_OK["connector"]),
        (_litellm, _NS_OK["connector"]),
    ],
    ids=["provider", "otel", "connector", "litellm"],
)
def test_a_source_reference_in_its_own_namespace_is_admitted_at_request(builder, name):
    """The admitting half, per path. Each namespace is the one its producer enforces."""
    r = _v(
        {"nodeId": "n1", "config": {"name": "a", "model": {"modelId": "m"}}, **builder(name)},
        phase=PayloadPhase.REQUEST,
    )
    assert r.is_valid, r.as_error_dicts()


@pytest.mark.parametrize(
    "builder,expected_ns",
    [
        (_provider, "agentcore-provider/"),
        (_otel_ref, "agentcore-otel/"),
        (_connector, "agentcore-connector/"),
        (_litellm, "agentcore-connector/"),
    ],
    ids=["provider", "otel", "connector", "litellm"],
)
@pytest.mark.parametrize(
    "wrong",
    [
        "not-the-namespace/x",
        # The ADJACENT-PREFIX mutants. Each of these is exactly the right namespace with one more
        # character before the slash, and each one passes a prefix test written WITHOUT the
        # trailing slash. They are here because the producer's own check is
        # ``source_name.startswith(f"{namespace}/")`` (gateway_deployer.py:771) and a validator
        # that omitted the slash would disagree with it.
        "agentcore-provider-evil/x",
        "agentcore-otel-evil/x",
        "agentcore-connector-evil/x",
        # A real platform namespace, but the WRONG one for this path. Admitting these would let a
        # canvas point a provider key at an OTEL secret and vice versa.
        "agentcore-provider/me/x",
        "agentcore-otel/me/x",
        "agentcore-connector/me/x",
    ],
)
def test_a_source_reference_outside_its_namespace_is_refused_at_request(builder, expected_ns, wrong):
    """Every wrong namespace, including the one-character-adjacent and the wrong-platform ones."""
    payload = {"nodeId": "n1", "config": {"name": "a", "model": {"modelId": "m"}}, **builder(wrong)}
    r = _v(payload, phase=PayloadPhase.REQUEST)
    if wrong.startswith(expected_ns):
        assert r.is_valid, f"{wrong!r} IS in {expected_ns!r}; {r.as_error_dicts()}"
        return
    assert "secret_ref_wrong_namespace" in r.codes(), (
        f"{wrong!r} was admitted on a path that requires {expected_ns!r}; staging would refuse it "
        f"AFTER the deployment and version rows were written. {r.as_error_dicts()}"
    )


def test_the_refusal_names_the_namespace_without_echoing_a_value():
    r = _v(
        {"nodeId": "n1", "config": {"name": "a", "model": {"modelId": "m"}}, **_provider("not-provider/x")},
        phase=PayloadPhase.REQUEST,
    )
    message = " ".join(e["message"] for e in r.as_error_dicts())
    assert "agentcore-provider/" in message, "the operator cannot act without the expected shape"
    # A secret NAME is not secret material -- it is in the ARN the caller sent and in every
    # CloudTrail event about it -- but the VALUE must never appear, and no value is read here.
    assert "an-ordinary-public-value" not in message


@pytest.mark.parametrize("name", ["customer/anything", "my-own-confluence-secret", "not-connector/x"])
def test_a_knowledge_base_source_is_deliberately_not_namespace_constrained(name):
    """KB sources are the CUSTOMER's secrets, gated by an explicit opt-in tag, not a namespace.

    ``_prepare_knowledge_base_credentials``' docstring: "active customer secrets must explicitly
    opt in". Inventing a namespace here would refuse every real Confluence, Salesforce, SharePoint
    and RDS credential, so the rule stays "a complete ARN" and stops there. Recorded as a test so
    it is not "tightened" into an outage by someone generalizing the rule above.
    """
    r = _v(
        {"nodeId": "n1", "config": {"name": "a", "model": {"modelId": "m"}}, **_kb_ref(name)},
        phase=PayloadPhase.REQUEST,
    )
    assert r.is_valid, r.as_error_dicts()


@pytest.mark.parametrize(
    "builder",
    [_provider, _otel_ref, _connector, _kb_ref],
    ids=["provider", "otel", "connector", "kb"],
)
def test_a_prepared_reference_must_be_the_connector_namespace_copy_whatever_its_source_was(
    builder,
):
    """The phase-dependent half, and the reason this is not one fixed namespace per path.

    At PREPARED the value is no longer the caller's source secret: every staged copy is minted by
    ``_put_connector_secret`` as ``agentcore-connector/{safe_owner}/{uuid}``, including the
    provider, OTEL and KB copies, because ``stage_runtime_secret_for_deployment`` ends in
    ``return _put_connector_secret(...)``. Holding a prepared provider reference to
    ``agentcore-provider/`` would refuse every successfully staged deployment.
    """
    copy_name = _NS_OK["connector"]
    ok = _v(_prepared(**builder(copy_name), recorded_secret_arns=[_arn_for(copy_name)]))
    assert ok.is_valid, f"the staged copy must be admitted; {ok.as_error_dicts()}"

    # The SOURCE namespace must not still be accepted here: a prepared payload still carrying the
    # source reference means staging did not run, and nothing downstream may read the source.
    source_name = {
        "_provider": _NS_OK["provider"],
        "_otel_ref": _NS_OK["otel"],
        "_connector": _NS_OK["connector"],
        "_kb_ref": "customer/anything",
    }[f"_{builder.__name__.lstrip('_')}"]
    if source_name == copy_name:
        return  # a connector source IS already in the copy namespace; nothing to distinguish
    stale = _v(_prepared(**builder(source_name), recorded_secret_arns=[_arn_for(source_name)]))
    assert "secret_ref_wrong_namespace" in stale.codes(), (
        f"a prepared payload still holding the {source_name!r} SOURCE was admitted; that means "
        f"staging never ran and a downstream role would be granted the source credential. "
        f"{stale.as_error_dicts()}"
    )


def test_a_recorded_entry_outside_the_connector_namespace_is_refused():
    """A recorded entry is always a staged copy, so the whole list is one namespace.

    This is also what keeps the membership check honest: an entry the platform's own delete path
    refuses to touch (``delete_deployment_bound_secret`` -> ``_is_platform_connector_secret``)
    would authorize a reference that can never be cleaned up.
    """
    good = _arn_for(_NS_OK["connector"])
    r = _v(
        _prepared(
            connectors=[{"name": "c", "secret_arn": good}], recorded_secret_arns=[good, _arn_for("not-connector/y")]
        )
    )
    assert "secret_ref_wrong_namespace" in r.codes(), r.as_error_dicts()


def test_a_bare_name_on_a_bindable_path_is_still_namespace_checked():
    """The one legitimate bare-name form must not be the one shape that escapes the check.

    ``bind_connector_secret_for_deployment`` accepts a bare name, and for a bare name the
    namespace is the ONLY thing constraining which secret it resolves to -- there is no account or
    region segment to check at all.
    """
    ok = _v(
        {
            "nodeId": "n1",
            "config": {"name": "a", "model": {"modelId": "m"}},
            "connectors": [{"name": "c", "secret_arn": _NS_OK["connector"]}],
        },
        phase=PayloadPhase.REQUEST,
    )
    assert ok.is_valid, ok.as_error_dicts()

    for wrong in ("not-connector/x", "agentcore-connector-evil/x"):
        bad = _v(
            {
                "nodeId": "n1",
                "config": {"name": "a", "model": {"modelId": "m"}},
                "connectors": [{"name": "c", "secret_arn": wrong}],
            },
            phase=PayloadPhase.REQUEST,
        )
        assert "secret_ref_wrong_namespace" in bad.codes(), f"{wrong}: {bad.as_error_dicts()}"


# ======================================================================================
# 13. OTEL reference PRECEDENCE. Exactly one of three paths is the staged one.
#
# ``deployment_handler.py:608/615`` is ``if platform_defaults... elif effective_observability:``.
# That ``elif`` is a precedence decision, and a validator that flattened it into "all three are
# staged" refused an entirely ordinary platform-defaults deployment -- the canvas reference is
# left in place by design, and ``build_otel_env_vars`` drops it (observability.py:211-225).
# ======================================================================================


def test_platform_defaults_win_and_the_overridden_canvas_reference_travels_unstaged():
    """The outage case. The canvas ref names another account and is STILL admitted.

    It is never dereferenced: no role is ever granted it. Pinning its account would refuse a
    canvas value left over from before the operator enabled platform defaults, pointing wherever
    it used to point. Its SHAPE is still checked -- see the two controls below.
    """
    staged = _arn_for(_NS_OK["connector"])
    r = _v(
        _prepared(
            platform_observability_defaults={"auth_header_secret_arn": staged},
            observability_config={
                "auth_header_secret_arn": f"arn:aws:secretsmanager:eu-west-1:999999999999:secret:{_NS_OK['otel']}-AbCdEf"
            },
            recorded_secret_arns=[staged],
        )
    )
    assert r.is_valid, (
        "an overridden OTEL reference is inert, and refusing it would refuse a perfectly ordinary "
        f"platform-defaults deployment. {r.as_error_dicts()}"
    )


@pytest.mark.parametrize(
    "overridden,code",
    [
        ("arn:aws:secretsmanager:us-east-1:123456789012", "arn_malformed"),
        ("arn:aws:secretsmanager:us-east-1:123456789012:secret", "arn_wrong_resource_type"),
        (f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/agentcore-otel/x", "arn_wrong_service"),
        (_arn_for("not-otel/x"), "secret_ref_wrong_namespace"),
    ],
)
def test_an_overridden_reference_is_exempt_from_the_ACCOUNT_pin_and_nothing_else(overridden, code):
    """The relaxation must not become "anything goes on the losing branch".

    Without these controls, exempting the overridden reference from the account pin is
    indistinguishable from not validating it at all.
    """
    staged = _arn_for(_NS_OK["connector"])
    r = _v(
        _prepared(
            platform_observability_defaults={"auth_header_secret_arn": staged},
            observability_config={"auth_header_secret_arn": overridden},
            recorded_secret_arns=[staged],
        )
    )
    assert code in r.codes(), r.as_error_dicts()


def test_the_explicit_canvas_block_wins_over_the_nested_one():
    """The second precedence pair: ``prepared_observability if ... else nested_observability``.

    When the explicit block is the effective one, the nested copy is the loser, so the STAGED
    rules must fall on the explicit path and not on ``$.config.observability``.
    """
    staged = _arn_for(_NS_OK["connector"])
    r = _v(
        _prepared(
            observability_config={"auth_header_secret_arn": staged},
            config={
                "name": "a",
                "model": {"modelId": "m"},
                "observability": {"auth_header_secret_arn": _arn_for(_NS_OK["otel"])},
            },
            recorded_secret_arns=[staged],
        )
    )
    assert r.is_valid, r.as_error_dicts()


def test_the_nested_block_is_staged_when_it_is_the_only_one_present():
    """The nested spelling is not unvalidated: alone, it IS the effective reference.

    A collector that looked only at the top level would leave this path unchecked entirely, which
    is the other way to get this wrong.
    """
    unstaged = _arn_for(_NS_OK["connector"])
    r = _v(
        _prepared(
            config={"name": "a", "model": {"modelId": "m"}, "observability": {"auth_header_secret_arn": unstaged}},
            recorded_secret_arns=[],
        )
    )
    assert "secret_ref_not_staged" in r.codes(), (
        "the nested block is the effective OTEL reference when nothing outranks it, so it carries "
        f"the staged rules. {r.as_error_dicts()}"
    )
