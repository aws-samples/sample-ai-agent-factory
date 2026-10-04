"""The scripts we generate run on the RECIPIENT's machine, so canvas values are code.

THE DEFECT, as measured. ``deploy.sh`` is built by interpolating values into a Python
f-string, and one of those values -- the LiteLLM ``litellm_api_key_ref`` off the canvas --
landed inside a double-quoted ``echo``. Bash expands ``$(...)`` inside double quotes, so a
canvas whose key reference was::

    arn:aws:secretsmanager:us-east-1:123456789012:secret:evil$(printf SHELL_INJECTION)

produced the line ``echo "  ...evil$(printf SHELL_INJECTION)"``, and running the exported
``deploy.sh`` executed it. The validator in front of it ended in ``:secret:.+$``, which
permits every shell metacharacter there is. Both halves were reproduced before anything was
changed: the substitution reaching the file, and bash performing it.

Who is hurt: not the canvas author. The bundle is a zip people forward to colleagues and
attach to tickets, so the command runs as whoever deploys it, with their credentials.

THE FIX IS TWO INDEPENDENT LAYERS, and this file tests them separately on purpose.

1. ``_shell_literal`` renders the value single-quoted, so bash reads it as data whatever it
   contains. This is the control. ARCC guidance on injection through interpolated
   identifiers (``cnt_rXL2B3TnBQKiBQ``) is this exact shape -- a validated-looking
   identifier expanded inside a generated shell script -- and prescribes passing the value
   as data rather than building script text out of it.
2. ``SECRETSMANAGER_ARN_PATTERN`` now spells out Secrets Manager's own name alphabet, so the
   payload does not reach the file at all. Defence in depth (``cnt_ik6StRHfs118ea``): a
   validator has to be right about every string anyone will ever paste on a canvas.

``test_the_rendering_alone_neutralises_the_payload`` proves they are independent by putting
the loose pattern back and checking bash still refuses to expand it. A single-layer fix
would pass every other test in this file.

AND THE AUDIT IS A TEST. ``test_no_canvas_value_reaches_a_script_unrendered`` reads the AST
of the three script generators and requires every interpolation to be declared, so the next
person who interpolates a canvas value into an executable file has to say so here rather
than reproduce this bug quietly. That is the part that outlives the specific payload.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from app.services import cfn_template_generator
from app.services.cfn_template_generator import (
    LITELLM_SERVER_ALIAS_PATTERN,
    LITELLM_SERVERS_PATTERN,
    LITELLM_URL_PATTERN,
    SECRETSMANAGER_ARN_PATTERN,
    CfnExportUnsupportedError,
    _shell_literal,
)

sys.path.insert(0, str(Path(__file__).parent))

from test_cfn_export_contract import LITELLM_KEY_ARN, _generate, _litellm  # noqa: E402

GENERATOR_SOURCE = Path(cfn_template_generator.__file__)

# The payload is inert by construction: it prints a word. It has to be a real command
# substitution to prove anything, and `printf` writes nothing anywhere.
MARKER = "SHELL_INJECTION"
INJECTED_ARN = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:evil$(printf {MARKER})"
# The loose tail that shipped, kept verbatim so the mutation test restores the real defect
# rather than an approximation of it.
LOOSE_ARN_PATTERN = r"^arn:aws[a-zA-Z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:.+$"


def _bash():
    if shutil.which("bash") is None:  # pragma: no cover - bash is present everywhere we run
        pytest.skip("bash is not on PATH")
    return "bash"


def _run_line(line: str, tmp_path: Path) -> str:
    """Execute one generated line and return what it printed.

    One line rather than the whole script: deploy.sh's next step is
    ``aws sts get-caller-identity``. The question here is only what bash does with the
    characters we emitted, and that is answered without an AWS call.
    """
    script = tmp_path / "line.sh"
    script.write_text(line + "\n")
    proc = subprocess.run([_bash(), str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _run_and_observe_arn_line(line: str, tmp_path: Path) -> str:
    """Execute an ARN-bearing line and expose assignment results as output.

    The generated script now carries the safely rendered ARN in two places:
    an ``echo`` used for guidance and a single-quoted
    ``LITELLM_EFFECTIVE_ARN=...`` fallback used by the region/account guards.
    Executing an assignment correctly prints nothing, so observing only stdout
    made the security test report a false failure without checking the assigned
    bytes.  Print the variable after assignment so both sinks are exercised.
    """
    if line.lstrip().startswith("LITELLM_EFFECTIVE_ARN="):
        line += '\nprintf "%s" "$LITELLM_EFFECTIVE_ARN"'
    return _run_line(line, tmp_path)


def _arn_lines(deploy_sh: str, needle: str) -> list[str]:
    return [line for line in deploy_sh.splitlines() if needle in line]


# ---------------------------------------------------------------------------
# Layer 2: the payload does not reach the artifact
# ---------------------------------------------------------------------------


def test_the_reported_payload_reaches_no_artifact():
    """Every file in the download, not just deploy.sh.

    The README's parameter table prints the same value, and a table cell is copied
    into terminals as readily as a script is run.
    """
    bundle = _generate(gateway_config=_litellm(litellm_api_key_ref=INJECTED_ARN))
    for label, content in (
        ("template.yaml", bundle.template_yaml),
        ("deploy.sh", bundle.deploy_sh),
        ("teardown.sh", bundle.teardown_sh),
        ("README.md", bundle.readme),
        ("agent.py", bundle.agent_code),
        ("build-dependency-bundle.sh", bundle.build_bundle_sh),
    ):
        assert MARKER not in (content or ""), f"the injected payload reached {label}"


def test_dropping_the_reference_leaves_a_deployable_stack_that_asks_for_one():
    """Dropped, not fatal -- and the consequence has to be the documented one.

    An unrecognised reference means the parameter gets no Default, which makes
    CloudFormation demand a value. Asserting the absence of the payload alone would
    also pass if the export had quietly baked in a placeholder.
    """
    template = yaml.safe_load(_generate(gateway_config=_litellm(litellm_api_key_ref=INJECTED_ARN)).template_yaml)
    assert "Default" not in template["Parameters"]["LiteLLMApiKeySecretArn"]


@pytest.mark.parametrize(
    "arn",
    [
        pytest.param("arn:aws:secretsmanager:us-east-1:123456789012:secret:litellm-key-AbCdEf", id="suffixed"),
        pytest.param("arn:aws:secretsmanager:us-east-1:123456789012:secret:my-litellm-key", id="unsuffixed"),
        # Every character Secrets Manager allows in a name, which is where a
        # hand-tightened pattern usually goes wrong: `/_+=.@-` are all legal.
        pytest.param(
            "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:a/b_c+d=e.f@g-AbCdEf",
            id="china-and-full-alphabet",
        ),
        pytest.param("arn:aws-us-gov:secretsmanager:us-gov-west-1:123456789012:secret:k", id="govcloud-one-char-name"),
    ],
)
def test_a_legitimate_arn_is_still_accepted_and_still_becomes_the_default(arn):
    """The half a refusal test cannot see.

    A pattern tightened until nothing passes would satisfy every other test here, and
    the symptom -- a stack that asks for a key the canvas already had -- looks like a
    missing feature rather than a broken validator.
    """
    template = yaml.safe_load(_generate(gateway_config=_litellm(litellm_api_key_ref=arn)).template_yaml)
    assert template["Parameters"]["LiteLLMApiKeySecretArn"]["Default"] == arn


@pytest.mark.parametrize(
    "tail",
    ["evil$(id)", "evil`id`", 'evil";id;#', "evil'", "evil;id", "evil|id", "evil&&id", "evil$IFS", "evil x", "evil\\"],
)
def test_the_pattern_rejects_every_shell_metacharacter(tail):
    assert not re.fullmatch(SECRETSMANAGER_ARN_PATTERN, f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{tail}")


# ---------------------------------------------------------------------------
# Layer 1: the rendering, proven on its own
# ---------------------------------------------------------------------------


def test_the_rendering_alone_neutralises_the_payload(monkeypatch, tmp_path):
    """Restore the shipped validator and confirm bash still treats the value as data.

    This is the test that distinguishes the real fix from a pattern patch. With the
    loose tail back, the payload DOES reach deploy.sh -- asserted here, so the test
    fails loudly if the mutation stops reproducing the defect instead of passing
    vacuously -- and bash must print it rather than run it.
    """
    monkeypatch.setattr(cfn_template_generator, "SECRETSMANAGER_ARN_PATTERN", LOOSE_ARN_PATTERN)
    deploy_sh = _generate(gateway_config=_litellm(litellm_api_key_ref=INJECTED_ARN)).deploy_sh

    lines = _arn_lines(deploy_sh, MARKER)
    assert lines, "the mutation no longer reproduces the defect, so this test proves nothing"

    for line in lines:
        out = _run_and_observe_arn_line(line, tmp_path)
        assert f"$(printf {MARKER})" in out, f"bash expanded the substitution: {out!r}"
        assert out.strip() != f"arn:aws:secretsmanager:us-east-1:123456789012:secret:evil{MARKER}"


def test_a_quote_in_the_value_cannot_close_the_quoting(monkeypatch, tmp_path):
    """The one character single-quoting cannot survive naively.

    ``'`` ends the quoted string, so a value containing one would let the rest escape
    into code. Reachable only with the loose pattern, which is why it is tested there:
    the rendering must not depend on the validator in front of it.
    """
    monkeypatch.setattr(cfn_template_generator, "SECRETSMANAGER_ARN_PATTERN", LOOSE_ARN_PATTERN)
    breakout = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:e'$(printf {MARKER})'x"
    deploy_sh = _generate(gateway_config=_litellm(litellm_api_key_ref=breakout)).deploy_sh

    lines = _arn_lines(deploy_sh, MARKER)
    assert lines
    for line in lines:
        out = _run_and_observe_arn_line(line, tmp_path)
        assert f"$(printf {MARKER})" in out, f"the quoting was closed: {out!r}"


def test_the_generated_script_is_still_valid_bash_with_a_hostile_value(monkeypatch, tmp_path):
    """Inert is not enough; the file also has to parse.

    A rendering that neutralised the payload by producing unbalanced quotes would
    trade a code-execution bug for a bundle nobody can deploy.
    """
    monkeypatch.setattr(cfn_template_generator, "SECRETSMANAGER_ARN_PATTERN", LOOSE_ARN_PATTERN)
    script = tmp_path / "deploy.sh"
    script.write_text(
        _generate(
            gateway_config=_litellm(
                litellm_api_key_ref=f"arn:aws:secretsmanager:us-east-1:123456789012:secret:e'\"$(printf {MARKER})"
            )
        ).deploy_sh
    )
    proc = subprocess.run([_bash(), "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "value",
    ["plain", "$(id)", "`id`", "a'b", "a'\\''b", '"$x"', "a\nb", "${IFS}", "*", "~", "a b; rm -rf /"],
)
def test_shell_literal_round_trips_any_value_as_data(value, tmp_path):
    """Whatever goes in comes back out of bash unchanged.

    Parameterised over the metacharacters rather than asserting the emitted text,
    because what matters is bash's behaviour, not the quoting style.
    """
    out = _run_line(f"printf '%s' {_shell_literal(value)}", tmp_path)
    assert out == value


# ---------------------------------------------------------------------------
# The audit, as a test
# ---------------------------------------------------------------------------

# Every value interpolated into a file the recipient EXECUTES, with where it comes
# from. Anything canvas-derived must be wrapped in _shell_literal at the call site.
#
# This map is the point of the test: the injection existed because one canvas value was
# interpolated into a script and nobody had a reason to look at the other fifteen. Adding
# an interpolation now fails this test until it is declared, which forces the question.
DECLARED_SCRIPT_INTERPOLATIONS = {
    "_generate_deploy_script": {
        # Canvas-derived, and therefore rendered as a shell literal.
        # Keys are ``ast.unparse`` output, which normalises string quoting to single
        # quotes regardless of how the source is written.
        "_shell_literal(litellm['secret_arn'])": "canvas: litellm_api_key_ref -- rendered",
        # Canvas-derived but reduced to [a-z0-9-] by _sanitize_gateway_name before it
        # gets here, so it cannot carry a metacharacter. Named rather than trusted
        # silently: if that sanitiser is ever loosened, this line is a sink.
        "deployment_name": "canvas: config.name via _sanitize_gateway_name -> [a-z0-9-]",
        "deployment_fallback": (
            "canvas: config.name via _sanitize_deployment_name and _bounded_deployment_default -> [a-z][a-z0-9]*"
        ),
        # Canvas-derived, constrained to [A-Za-z0-9._:/-] by _sanitize_identifier, which
        # raises rather than substitutes.
        "model_id": "canvas: config.model.modelId via _sanitize_identifier",
        # Generator-controlled: selected by classify_runtime_artifact from the
        # generated source, whose only results carry one of these module constants.
        "bundle_key": "classifier: STRANDS_BUNDLE_KEY, BASE_BUNDLE_KEY, or MCP_LEAN_BUNDLE_KEY",
        "bundle_file": "constant: basename of bundle_key",
        "baked_arn_literal": "canvas: litellm_api_key_ref pre-rendered by _shell_literal",
        "current_region()": "server: the platform's own region",
        "deployment_name_limit": "int: generated DeploymentName MaxLength",
        "deployment_hash_prefix": "int: deployment_name_limit minus the fixed 8-character hash",
        "explicit_role_stack_name_limit": "int: derived from generated IAM RoleName expressions",
        # Generated script fragments, built from the template's own parameter names and
        # from module constants. No canvas value reaches any of them.
        "explicit_role_stack_guard": "generated fragment: static text plus an integer limit",
        "lifecycle_json": "constant",
        "tls_policy_json": "constant",
        "mcp_upload": "generated fragment: no interpolation",
        "parameter_passthrough": "generated fragment: template parameter names",
        "litellm_preflight": "generated fragment: contains missing_arn",
        "litellm_account_guard": "generated fragment: static account-consistency guard",
        "litellm_overrides": "generated fragment: no interpolation",
        "litellm_usage_args": "constant",
        "litellm_usage_note": "constant",
        "missing_arn": "generated fragment: contains the rendered secret_arn",
        "mcp_bundle_staging": (
            "generated fragment: static shell plus the classifier-owned MCP_LEAN_BUNDLE_KEY and its basename"
        ),
        "mcp_bundle_overrides": "generated fragment: static parameter wiring; no canvas value",
        "model_override": (
            "generated fragment: model_id comes through resolve_model_id -> "
            "_sanitize_identifier before entering the fragment"
        ),
    },
    "_generate_teardown_script": {
        "bundle_key": "classifier: STRANDS_BUNDLE_KEY, BASE_BUNDLE_KEY, or MCP_LEAN_BUNDLE_KEY",
        "mcp_bundle_key": "classifier: MCP_LEAN_BUNDLE_KEY or None",
        "mcp_bundle_key_decl": ("generated fragment: assignment of classifier-owned MCP_LEAN_BUNDLE_KEY, or empty"),
        "mcp_bundle_retained_notice": "constant _TEARDOWN_MCP_BUNDLE_NOTICE_SH or empty",
        "current_region()": "server",
        "count": "int: len of a generated list",
        "len(log_groups)": "int",
        "chr(10).join(('    - ' + r for r in data_stores))": "template logical resource ids",
        "log_group_sweep": "generated fragment",
        "retained_notice": "generated fragment",
        "_TEARDOWN_PURGE_CALL_SH": "constant",
        "_TEARDOWN_PURGE_HELPER_SH": "constant",
    },
    "_generate_bundle_build_script": {
        "bundle_key": "classifier: STRANDS_BUNDLE_KEY, BASE_BUNDLE_KEY, or MCP_LEAN_BUNDLE_KEY",
        "bundle_name": "constant: basename of bundle_key",
        "script_name": (
            "generator-owned literal: build-dependency-bundle.sh or "
            "build-mcp-server-bundle.sh; all call sites are audited below"
        ),
        "package_args": "constant: DEPENDENCY_BUNDLE_PACKAGES",
        "pip_flags": "constant: DEPENDENCY_BUNDLE_PIP_FLAGS",
        "p": "constant: one DEPENDENCY_BUNDLE_PACKAGES entry",
        "constraints": "constant: DEPENDENCY_BUNDLE_CONSTRAINTS",
        "pip_select": "constant: _BUNDLE_PIP_SELECT_SH",
        "arch_check": "constant",
    },
}


def _interpolations(function_name: str) -> set[str]:
    tree = ast.parse(GENERATOR_SOURCE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return {ast.unparse(n.value) for n in ast.walk(node) if isinstance(n, ast.FormattedValue)}
    raise AssertionError(f"{function_name} not found in {GENERATOR_SOURCE.name}")


def test_bundle_build_script_name_is_never_canvas_derived():
    """A newline in this value would escape the generated header comment.

    The value is currently internal-only, but documenting that in the declaration map
    is insufficient: a future call site could pass a canvas field and leave the map
    technically true. Audit every call and the function default as exact literals.
    """
    tree = ast.parse(GENERATOR_SOURCE.read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_generate_bundle_build_script"
    )
    assert function.args.args[-1].arg == "script_name"
    default = function.args.defaults[-1]
    assert isinstance(default, ast.Constant)
    assert default.value == "build-dependency-bundle.sh"

    observed: list[str] = []
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "_generate_bundle_build_script"
        ):
            continue

        positional = call.args[1] if len(call.args) > 1 else None
        keywords = [keyword.value for keyword in call.keywords if keyword.arg == "script_name"]
        assert not (positional is not None and keywords), "script_name is supplied twice"
        value = positional or (keywords[0] if keywords else default)
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
            "_generate_bundle_build_script must receive script_name as a generator-owned "
            "string literal, never a value derived from a request or canvas"
        )
        observed.append(value.value)

    assert sorted(observed) == [
        "build-dependency-bundle.sh",
        "build-mcp-server-bundle.sh",
    ]


@pytest.mark.parametrize("function_name", sorted(DECLARED_SCRIPT_INTERPOLATIONS))
def test_no_canvas_value_reaches_a_script_unrendered(function_name):
    declared = DECLARED_SCRIPT_INTERPOLATIONS[function_name]
    found = _interpolations(function_name)

    undeclared = found - set(declared)
    assert not undeclared, (
        f"{function_name} interpolates {sorted(undeclared)} into a script the recipient executes. "
        "If any of it comes off a canvas, wrap it in _shell_literal(); then add it to "
        "DECLARED_SCRIPT_INTERPOLATIONS with where it comes from."
    )
    # The other direction, so the map cannot rot into a list of things that used to be
    # interpolated and silently stop constraining anything.
    stale = set(declared) - found
    assert not stale, f"{function_name} no longer interpolates {sorted(stale)}; remove them from the map"


def test_the_rendered_secret_is_the_only_canvas_string_in_a_double_quoted_echo():
    """A direct read of the emitted text, independent of the AST map above.

    The AST test would pass if someone interpolated a canvas value into a NEW script
    generator, so this one checks the artifact: the ARN must appear single-quoted.
    """
    deploy_sh = _generate(gateway_config=_litellm(litellm_api_key_ref=LITELLM_KEY_ARN)).deploy_sh
    lines = _arn_lines(deploy_sh, LITELLM_KEY_ARN)
    assert lines, "the ARN no longer appears in deploy.sh; this test is watching nothing"
    for line in lines:
        assert f"'{LITELLM_KEY_ARN}'" in line, f"the ARN is not single-quoted: {line!r}"


# ---------------------------------------------------------------------------
# The other two canvas values the audit covered
# ---------------------------------------------------------------------------


def test_the_base_url_and_servers_reach_no_executable_file():
    """Measured, not assumed: both were swept for with a marker payload.

    They land in template.yaml and README.md only. That is why the URL pattern is not
    tightened to exclude ``$`` -- which RFC 3986 permits in a path -- while the ARN
    pattern is: the ARN had a shell sink and these do not.
    """
    bundle = _generate(gateway_config=_litellm(litellm_base_url="https://proxy.example/p", litellm_servers=["github"]))
    for script in (bundle.deploy_sh, bundle.teardown_sh, bundle.build_bundle_sh):
        assert "proxy.example" not in script
        assert "github" not in script


@pytest.mark.parametrize(
    "bad",
    [
        'https://a"b',
        "https://a'b",
        "https://a`b",
        "https://a\\b",
        "https://a<b",
        "https://a{b",
        "https://a|b",
        "https://a b",
    ],
)
def test_a_url_with_characters_no_uri_may_contain_is_refused(bad):
    with pytest.raises(CfnExportUnsupportedError, match="not an https URL"):
        _generate(gateway_config=_litellm(litellm_base_url=bad))


@pytest.mark.parametrize(
    "good",
    [
        "https://litellm.example.internal",
        "https://litellm.example.internal:4000/v1",
        "https://user:pass@litellm.example.internal/base",
        # `$`, `(` and `)` are RFC 3986 sub-delims and appear in real paths. Accepted
        # deliberately -- the URL has no shell sink, and refusing it would break a
        # legitimate proxy for the sake of a danger that is not there.
        "https://litellm.example.internal/tenant$(a)/v1?x=1&y=2#frag",
    ],
)
def test_a_real_proxy_url_is_accepted(good):
    template = yaml.safe_load(_generate(gateway_config=_litellm(litellm_base_url=good)).template_yaml)
    assert template["Parameters"]["LiteLLMGatewayUrl"]["Default"].startswith(good.rstrip("/"))


def test_a_server_alias_that_could_not_be_sent_as_a_header_is_refused_and_named():
    """Refused rather than dropped: dropping a pinned server widens the export's scope.

    The alias becomes the ``x-mcp-servers`` header, so a newline in it makes every tool
    call fail at runtime with a stack that deployed cleanly. The message names the
    alias, which is not a secret.
    """
    with pytest.raises(CfnExportUnsupportedError, match="is not a valid alias"):
        _generate(gateway_config=_litellm(litellm_servers=["github", "ji\nra"]))
    with pytest.raises(CfnExportUnsupportedError, match=re.escape("gh$(id)")):
        _generate(gateway_config=_litellm(litellm_servers=["gh$(id)"]))


def test_the_servers_parameter_is_constrained_at_deploy_time_too():
    """An export-time check is not enough: the parameter is overridable.

    Whoever runs the stack update can put anything in ``LiteLLMMcpServers``, so the
    same rule has to exist as an AllowedPattern. An empty value stays legal -- it means
    every server the key can see.
    """
    params = yaml.safe_load(_generate(gateway_config=_litellm()).template_yaml)["Parameters"]
    assert params["LiteLLMMcpServers"]["AllowedPattern"] == LITELLM_SERVERS_PATTERN
    assert re.fullmatch(LITELLM_SERVERS_PATTERN, "")
    assert re.fullmatch(LITELLM_SERVERS_PATTERN, "github,jira")
    assert not re.fullmatch(LITELLM_SERVERS_PATTERN, "github,")
    assert not re.fullmatch(LITELLM_SERVERS_PATTERN, "gh$(id)")


def test_every_litellm_pattern_is_anchored_at_both_ends():
    """Because these double as CloudFormation AllowedPatterns.

    CloudFormation applies an AllowedPattern to the whole value, so an unanchored one
    here would be stricter in Python than in the template it is written into -- the
    export would accept what the deploy rejects, or worse, the other way round.
    """
    for pattern in (
        SECRETSMANAGER_ARN_PATTERN,
        LITELLM_URL_PATTERN,
        LITELLM_SERVER_ALIAS_PATTERN,
        LITELLM_SERVERS_PATTERN,
    ):
        assert pattern.startswith("^") and pattern.endswith("$"), pattern
        re.compile(pattern)
