"""Contract tests for scripts/import/openai_agents_sdk.py (OpenAI Agent Builder exit importer).

The importer takes an Agent Builder *Code -> Agents SDK (Python)* export and makes it a governed
AgentCore Runtime without editing it. These tests pin the offline half of that contract:

* ``validate_export`` refuses, with an exact code and message, every construct that cannot run
  headless on AWS (literal secrets, OpenAI-hosted tools and vector stores, human approval,
  TypeScript-only, non-SDK code) and warns on MCP passthrough; the hosted-tool refusals are
  lifted only by the explicit flag;
* ``detect_entry`` resolves the one ``async def`` entry or demands ``--entry``;
* ``render_entrypoint`` / ``write_bundle`` copy the export unchanged, add agent.py with the
  payload/response contract the platform's own agents use, the pinned requirements and a manifest
  that hashes every file;
* ``build_zip`` is byte-reproducible;
* the ``CreateAgentRuntime`` request validates against the installed SDK model and carries the
  importer's tags, never the platform's ownership tags (an adopted runtime must stay ``imported``);
* the execution role is least privilege per model provider.

The live half (deploy, adopt, invoke, teardown) is exercised against the dev platform by
tools/run_openai_exit_probe.sh and recorded in the PR.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest
from botocore.session import Session
from botocore.validate import validate_parameters

IMPORTER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "import" / "openai_agents_sdk.py"


def _load():
    spec = importlib.util.spec_from_file_location("openai_agents_sdk_importer", IMPORTER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


imp = _load()

CLEAN_EXPORT = """
from pydantic import BaseModel
from agents import Agent, Runner, function_tool


class WorkflowInput(BaseModel):
    input_as_text: str


@function_tool
def lookup_order(order_id: str) -> str:
    return order_id


agent = Agent(name="Order agent", instructions="Answer.", tools=[lookup_order], model="gpt-5-mini")


async def run_workflow(workflow_input: WorkflowInput):
    return await Runner.run(agent, workflow_input.input_as_text)
"""


def _export(tmp_path: Path, source: str = CLEAN_EXPORT, name: str = "workflow.py") -> Path:
    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / name).write_text(source, encoding="utf-8")
    return export_dir


def _codes(report) -> list[str]:
    return [f.code for f in report.refusals]


# --------------------------------------------------------------------------- validate


def test_a_clean_export_validates_with_no_findings(tmp_path: Path) -> None:
    report = imp.validate_export(_export(tmp_path))
    assert report.ok and report.refusals == [] and report.warnings == []
    assert report.python_files == ["workflow.py"]
    assert report.as_dict()["ok"] is True


@pytest.mark.parametrize(
    "snippet, code, needle",
    [
        (
            'KEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"\n',  # pragma: allowlist secret
            imp.R_LITERAL_SECRET,
            "OpenAI API key is written in the code",
        ),
        (
            'import os\nos.environ["OPENAI_API_KEY"] = "sk-live-xxxxxxxxxxxxxxxxxxxx"\n',  # pragma: allowlist secret
            imp.R_LITERAL_SECRET,
            "OPENAI_API_KEY is assigned a literal value",
        ),
        (
            'OPENAI_API_KEY = "whatever"\n',  # pragma: allowlist secret
            imp.R_LITERAL_SECRET,
            "OPENAI_API_KEY is assigned a literal value",
        ),
        (
            "from agents import FileSearchTool\nt = FileSearchTool(vector_store_ids=['x'])\n",
            imp.R_HOSTED_TOOL,
            "Managed Knowledge Base",
        ),
        ('STORE = "vs_abcdef1234567890"\n', imp.R_HOSTED_TOOL, "OpenAI vector store id"),
        ("from agents import WebSearchTool\nt = WebSearchTool()\n", imp.R_HOSTED_TOOL, "managed Web Search"),
        (
            "from agents import CodeInterpreterTool\nt = CodeInterpreterTool(tool_config={})\n",
            imp.R_HOSTED_TOOL,
            "AgentCore Code Interpreter",
        ),
        ("from agents import ComputerTool\nt = ComputerTool(computer=None)\n", imp.R_HOSTED_TOOL, "AgentCore Browser"),
        (
            "from agents import function_tool\n@function_tool(needs_approval=True)\ndef send(x: str) -> str:\n    return x\n",
            imp.R_HUMAN_APPROVAL,
            "cannot pause for a human",
        ),
        ("from agents import HumanApproval\nh = HumanApproval\n", imp.R_HUMAN_APPROVAL, "cannot pause for a human"),
    ],
)
def test_each_unsupported_construct_is_refused_with_its_exact_reason(
    tmp_path: Path, snippet: str, code: str, needle: str
) -> None:
    report = imp.validate_export(_export(tmp_path, CLEAN_EXPORT + "\n" + snippet))
    assert not report.ok
    assert code in _codes(report), report.refusals
    assert any(needle in f.message for f in report.refusals if f.code == code), [f.message for f in report.refusals]
    assert all(f.path == "workflow.py" and f.line > 0 for f in report.refusals)


def test_hosted_tool_refusals_lift_only_with_the_explicit_flag(tmp_path: Path) -> None:
    export = _export(
        tmp_path,
        CLEAN_EXPORT + "\nfrom agents import WebSearchTool\nt = WebSearchTool()\nSTORE = 'vs_abcdef1234567890'\n",
    )
    assert imp.R_HOSTED_TOOL in _codes(imp.validate_export(export))
    allowed = imp.validate_export(export, allow_openai_hosted_tools=True)
    assert allowed.ok, allowed.refusals


def test_the_flag_never_lifts_secret_or_approval_refusals(tmp_path: Path) -> None:
    export = _export(tmp_path, CLEAN_EXPORT + '\nKEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"\n')
    assert _codes(imp.validate_export(export, allow_openai_hosted_tools=True)) == [imp.R_LITERAL_SECRET]


def test_mcp_tools_pass_through_with_a_warning_not_a_refusal(tmp_path: Path) -> None:
    report = imp.validate_export(
        _export(tmp_path, CLEAN_EXPORT + "\nfrom agents import HostedMCPTool\nm = HostedMCPTool(tool_config={})\n")
    )
    assert report.ok
    assert [w.code for w in report.warnings] == [imp.W_MCP_PASSTHROUGH]
    assert "Gateway target" in report.warnings[0].message and report.warnings[0].severity == "warn"


def test_non_sdk_code_typescript_only_and_empty_directories_are_refused(tmp_path: Path) -> None:
    assert _codes(imp.validate_export(_export(tmp_path, "print('hello')\n"))) == [imp.R_NO_AGENTS_IMPORT]
    ts_dir = tmp_path / "ts"
    ts_dir.mkdir()
    (ts_dir / "workflow.ts").write_text("export const x = 1;\n")
    report = imp.validate_export(ts_dir)
    assert _codes(report) == [imp.R_TYPESCRIPT] and "Agents SDK -> Python" in report.refusals[0].message
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _codes(imp.validate_export(empty)) == [imp.R_NO_PYTHON]
    assert _codes(imp.validate_export(tmp_path / "missing")) == [imp.R_NO_PYTHON]


def test_a_syntax_error_is_a_refusal_with_its_line(tmp_path: Path) -> None:
    report = imp.validate_export(_export(tmp_path, "from agents import Agent\ndef broken(:\n    pass\n"))
    assert _codes(report) == [imp.R_NO_PYTHON] and report.refusals[0].line == 2


# --------------------------------------------------------------------------- entry detection


def test_entry_is_auto_detected_from_the_single_async_function(tmp_path: Path) -> None:
    entry = imp.detect_entry(_export(tmp_path))
    assert (entry.module, entry.function, entry.is_async, entry.input_mode, entry.model_class) == (
        "workflow",
        "run_workflow",
        True,
        "model",
        "WorkflowInput",
    )


def test_entry_detection_demands_a_choice_when_ambiguous_or_missing(tmp_path: Path) -> None:
    two = CLEAN_EXPORT + "\nasync def run_other(text: str):\n    return text\n"
    with pytest.raises(imp.ImportRefused) as error:
        imp.detect_entry(_export(tmp_path, two))
    assert error.value.findings[0].code == imp.R_ENTRY_AMBIGUOUS
    assert (
        "workflow:run_workflow" in error.value.findings[0].message
        and "workflow:run_other" in error.value.findings[0].message
    )
    chosen = imp.detect_entry(_export(tmp_path, two), entry="workflow:run_other")
    assert (chosen.function, chosen.input_mode) == ("run_other", "text")
    none = CLEAN_EXPORT.replace("async def run_workflow", "def run_workflow")
    with pytest.raises(imp.ImportRefused) as missing:
        imp.detect_entry(_export(tmp_path, none))
    assert missing.value.findings[0].code == imp.R_ENTRY_MISSING
    with pytest.raises(imp.ImportRefused):
        imp.detect_entry(_export(tmp_path), entry="workflow:nope")
    with pytest.raises(imp.ImportRefused):
        imp.detect_entry(_export(tmp_path), entry="nocolon")


# --------------------------------------------------------------------------- wrap


def test_rendered_entrypoint_carries_the_platform_payload_contract_and_the_provider(tmp_path: Path) -> None:
    entry = imp.detect_entry(_export(tmp_path))
    source = imp.render_entrypoint(entry, "bedrock")
    compile(source, "agent.py", "exec")  # valid Python
    for needle in (
        'ENTRY_MODULE = "workflow"',
        'ENTRY_FUNCTION = "run_workflow"',
        'ENTRY_INPUT_MODE = "model"',
        'MODEL_PROVIDER = "bedrock"',
        'payload.get("prompt")',
        '"warmup": True',
        '"tool_receipts"',
        "set_tracing_disabled(True)",
        "bedrock-runtime.{region}.amazonaws.com/openai/v1",
        "provide_token(region=region)",
        f'"{imp.DEFAULT_BEDROCK_MODEL}"',
        f'"{imp.DEFAULT_BEDROCK_OPENAI_API}"',
        "OPENAI_API_KEY_SECRET_ARN",
        "InputGuardrailTripwireTriggered",
    ):
        assert needle in source, needle
    assert "__" + "ENTRY_MODULE" + "__" not in source  # every marker substituted
    assert "sk-" not in source
    openai_source = imp.render_entrypoint(entry, "openai")
    assert 'MODEL_PROVIDER = "openai"' in openai_source
    with pytest.raises(ValueError):
        imp.render_entrypoint(entry, "azure")


def test_write_bundle_copies_the_export_unchanged_and_adds_only_the_wrapper_files(tmp_path: Path) -> None:
    export = _export(tmp_path)
    (export / "helpers.py").write_text("VALUE = 1\n")
    (export / "__pycache__").mkdir()
    (export / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    bundle = tmp_path / "bundle"
    manifest = imp.write_bundle(export, bundle, model_provider="bedrock")
    assert sorted(p.name for p in bundle.iterdir()) == [
        "IMPORT_MANIFEST.json",
        "agent.py",
        "helpers.py",
        "requirements.in",
        "workflow.py",
    ]
    assert (bundle / "workflow.py").read_text() == CLEAN_EXPORT
    assert (bundle / "requirements.in").read_text().splitlines() == list(imp.BUNDLE_REQUIREMENTS)
    assert manifest["entry"]["function"] == "run_workflow" and manifest["model_provider"] == "bedrock"
    assert set(manifest["files"]) == {"agent.py", "helpers.py", "requirements.in", "workflow.py"}
    assert all(len(digest) == 64 for digest in manifest["files"].values())
    assert json.loads((bundle / "IMPORT_MANIFEST.json").read_text())["importer"]["version"] == imp.IMPORTER_VERSION


def test_write_bundle_refuses_an_invalid_export_and_an_export_that_already_has_agent_py(tmp_path: Path) -> None:
    bad = _export(tmp_path, CLEAN_EXPORT + '\nKEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"\n')
    with pytest.raises(imp.ImportRefused):
        imp.write_bundle(bad, tmp_path / "b1", model_provider="openai")
    assert not (tmp_path / "b1").exists()
    clash = _export(tmp_path / "second")
    (clash / "agent.py").write_text("x = 1\n")
    with pytest.raises(imp.ImportRefused) as error:
        imp.write_bundle(clash, tmp_path / "b2", model_provider="openai")
    assert "already contains agent.py" in error.value.findings[0].message


def test_requirements_pin_the_verified_sdk_and_the_platform_runtime_versions() -> None:
    pins = dict(req.split("==") for req in imp.BUNDLE_REQUIREMENTS)
    assert pins["openai-agents"] == "0.24.0"
    constraints = (IMPORTER_PATH.parents[2] / "backend" / "agentcore-deps-constraints.txt").read_text()
    for name in ("bedrock-agentcore", "boto3", "aws-bedrock-token-generator"):
        assert f"{name}=={pins[name]}" in constraints, f"{name} pin differs from the platform lock"


# --------------------------------------------------------------------------- package


def test_zip_is_byte_reproducible_regardless_of_mtimes_and_creation_order(tmp_path: Path) -> None:
    left, right = tmp_path / "left", tmp_path / "right"
    for root, order in ((left, ("a.py", "b.py", "sub/c.py")), (right, ("sub/c.py", "b.py", "a.py"))):
        for rel in order:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {rel}\n")
    import os
    import time

    old = time.time() - 86400
    os.utime(left / "a.py", (old, old))
    left_zip, right_zip = tmp_path / "left.zip", tmp_path / "right.zip"
    assert imp.build_zip(left, left_zip) == imp.build_zip(right, right_zip)
    assert left_zip.read_bytes() == right_zip.read_bytes()
    with zipfile.ZipFile(left_zip) as archive:
        assert [i.filename for i in archive.infolist()] == ["a.py", "b.py", "sub/c.py"]
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in archive.infolist())


# --------------------------------------------------------------------------- deploy request & IAM


def test_create_runtime_request_validates_against_the_sdk_model_and_mirrors_the_platform_shape() -> None:
    request = imp.create_runtime_request(
        runtime_name="oai_exit_probe",
        role_arn="arn:aws:iam::111122223333:role/ImportedAgentRuntime-oai_exit_probe",
        bucket="bucket",
        key="imports/openai-agents-sdk/oai_exit_probe/abc.zip",
        env_vars={"AWS_REGION": "us-east-1", "AGENT_MODEL_ID": imp.DEFAULT_BEDROCK_MODEL},
        tags=imp.importer_tags([]),
    )
    model = Session().get_service_model("bedrock-agentcore-control")
    validate_parameters(request, model.operation_model("CreateAgentRuntime").input_shape)
    code = request["agentRuntimeArtifact"]["codeConfiguration"]
    assert code["runtime"] == "PYTHON_3_13" and code["entryPoint"] == ["agent.py"]
    assert code["code"]["s3"] == {"bucket": "bucket", "prefix": "imports/openai-agents-sdk/oai_exit_probe/abc.zip"}
    assert request["protocolConfiguration"] == {"serverProtocol": "HTTP"}
    assert request["networkConfiguration"] == {"networkMode": "PUBLIC"}


def test_importer_tags_are_never_the_platform_ownership_tags() -> None:
    from app.services.resource_ownership import ACCESS_TAG_KEY, ACCESS_TAG_VALUE

    tags = imp.importer_tags(["team=payments", "bad", "=novalue"])
    assert tags == {
        "ManagedBy": "openai_agents_sdk_importer",
        "ImporterVersion": imp.IMPORTER_VERSION,
        "team": "payments",
    }
    # The platform's teardown gates read these two tags as "built by this platform"; an adopted
    # runtime must not carry that claim.
    assert tags.get(ACCESS_TAG_KEY) != ACCESS_TAG_VALUE
    assert "AgentCoreStack" not in tags


def test_execution_role_is_least_privilege_per_provider() -> None:
    bedrock = imp.execution_role_policy(
        "us-east-1",
        "111122223333",
        "oai_exit_probe",
        model_provider="bedrock",
        secret_arn=None,
        bedrock_model_id="global.openai.gpt-5.6-terra",
    )
    sids = {s["Sid"]: s for s in bedrock["Statement"]}
    assert set(sids) == {
        "RuntimeLogs",
        "RuntimeMetrics",
        "RuntimeTraces",
        "WorkloadIdentity",
        "BedrockModel",
        "BedrockDefaultProject",
        "BedrockShortTermBearer",
    }
    assert sids["BedrockShortTermBearer"]["Condition"] == {"StringEquals": {"bedrock:bearerTokenType": "SHORT_TERM"}}
    assert "arn:aws:bedrock:us-east-1::foundation-model/global.openai.gpt-5.6-terra" in sids["BedrockModel"]["Resource"]
    assert "arn:aws:bedrock:*::foundation-model/openai.gpt-5.6-terra" in sids["BedrockModel"]["Resource"]
    # The Responses API authorizes InvokeModel on the account's default project too (measured live:
    # without it every turn is "not authorized ... on resource: ...:project/default").
    assert sids["BedrockDefaultProject"]["Action"] == ["bedrock:InvokeModel"]
    assert sids["BedrockDefaultProject"]["Resource"] == "arn:aws:bedrock:us-east-1:111122223333:project/default"
    actions = {a for s in bedrock["Statement"] for a in s["Action"]}
    assert not any(a.startswith("bedrock-agentcore:") and "WorkloadAccessToken" not in a for a in actions), (
        "no control-plane actions"
    )
    assert "secretsmanager:GetSecretValue" not in actions
    openai = imp.execution_role_policy(
        "us-east-1",
        "111122223333",
        "oai_exit_probe",
        model_provider="openai",
        secret_arn="arn:aws:secretsmanager:us-east-1:111122223333:secret:openai-key-AbCdEf",  # pragma: allowlist secret
        bedrock_model_id="x",
    )
    osids = {s["Sid"]: s for s in openai["Statement"]}
    assert osids["OpenAIKey"]["Resource"] == "arn:aws:secretsmanager:us-east-1:111122223333:secret:openai-key-AbCdEf"
    assert not {"BedrockModel", "BedrockDefaultProject", "BedrockShortTermBearer"} & set(osids)
    with pytest.raises(ValueError, match="openai-key-secret-arn"):
        imp.execution_role_policy(
            "us-east-1",
            "111122223333",
            "oai_exit_probe",
            model_provider="openai",
            secret_arn=None,
            bedrock_model_id="x",
        )
    log_resources = sids["RuntimeLogs"]["Resource"]
    assert all("/aws/bedrock-agentcore/runtimes/oai_exit_probe" in r for r in log_resources)


def test_trust_policy_is_scoped_to_this_account_and_region() -> None:
    trust = imp._trust_policy("111122223333", "us-east-1")["Statement"][0]
    assert trust["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    assert trust["Condition"]["StringEquals"] == {"aws:SourceAccount": "111122223333"}
    assert trust["Condition"]["ArnLike"] == {"aws:SourceArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:*"}


def test_cli_validate_exit_codes(tmp_path: Path, capsys) -> None:
    good = _export(tmp_path)
    assert imp.main(["validate", str(good)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    bad = _export(tmp_path / "bad", CLEAN_EXPORT + '\nKEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"\n')
    assert imp.main(["validate", str(bad)]) == 2
    assert imp.main(["wrap", str(bad), "--out", str(tmp_path / "out")]) == 2
    assert "IMPORT REFUSED" in capsys.readouterr().err


def _elf(e_machine: int) -> bytes:
    """A minimal little-endian ELF64 header with the given e_machine."""
    return (
        b"\x7fELF"
        + bytes([2, 1, 1, 0])
        + b"\x00" * 8
        + (3).to_bytes(2, "little")
        + e_machine.to_bytes(2, "little")
        + b"\x00" * 44
    )


def test_bundle_dependencies_target_the_runtime_architecture_and_native_files_are_verified(tmp_path: Path) -> None:
    # AgentCore Runtime is Linux ARM64; an x86_64 bundle is accepted by CreateAgentRuntime and fails
    # minutes later ("binary files that are incompatible with Linux ARM64", measured live).
    assert imp.PIP_PLATFORM == "aarch64-manylinux2014"
    bundle = tmp_path / "bundle"
    (bundle / "pkg").mkdir(parents=True)
    (bundle / "pkg" / "fast.cpython-313-aarch64-linux-gnu.so").write_bytes(_elf(imp.ELF_MACHINE_AARCH64))
    (bundle / "pkg" / "libz.so.1").write_bytes(_elf(imp.ELF_MACHINE_AARCH64))
    (bundle / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    assert imp.verify_native_arch(bundle) == 2
    (bundle / "pkg" / "wrong.cpython-313-x86_64-linux-gnu.so").write_bytes(_elf(0x3E))
    with pytest.raises(
        RuntimeError, match=r"not Linux ARM64.*wrong\.cpython-313-x86_64-linux-gnu\.so \(e_machine=0x3e\)"
    ):
        imp.verify_native_arch(bundle)
    (bundle / "pkg" / "wrong.cpython-313-x86_64-linux-gnu.so").unlink()
    (bundle / "pkg" / "notelf.so").write_bytes(b"MZ" + b"\x00" * 30)
    with pytest.raises(RuntimeError, match=r"notelf\.so \(e_machine=None\)"):
        imp.verify_native_arch(bundle)


def test_smoke_invoke_uses_test_runtime_warms_up_first_and_retries_only_while_still_running(monkeypatch) -> None:
    calls: list[dict] = []
    answers = iter(
        [
            {"status": 200, "body": {"success": True, "response": ""}},  # warm-up
            {"status": 200, "body": {"success": False, "error": "Agent still running; exceeded 30s sync test limit"}},
            {"status": 200, "body": {"success": True, "response": "Order ORD-1 shipped."}},
        ]
    )

    def fake_invoke(platform_url, token, body):
        calls.append(body)
        return next(answers)

    monkeypatch.setattr(imp, "_platform_invoke", fake_invoke)
    monkeypatch.setattr(imp.time, "sleep", lambda _s: None)
    result = imp.smoke_invoke("https://p.example", "tok", "oai_exit_probe-abc", "What about ORD-1?")
    assert calls[0]["warmup"] is True and calls[0]["runtimeId"] == "oai_exit_probe-abc"
    assert all(c["runtimeId"] == "oai_exit_probe-abc" and "input" in c for c in calls[1:])
    assert len({c["sessionId"] for c in calls}) == 3, "each attempt is its own session"
    assert result["attempts"] == 2 and result["body"]["response"] == "Order ORD-1 shipped."
    assert result["warmup"]["body"]["success"] is True

    # A real failure is NOT retried: the platform's generic error is returned as is after one attempt.
    answers = iter(
        [
            {"status": 200, "body": {"success": True, "response": ""}},
            {"status": 200, "body": {"success": False, "error": "Runtime invocation failed."}},
        ]
    )
    result = imp.smoke_invoke("https://p.example", "tok", "oai_exit_probe-abc", "x")
    assert result["attempts"] == 1 and result["body"]["error"] == "Runtime invocation failed."

    # The retry budget is bounded.
    answers = iter(
        [{"status": 200, "body": {"success": True, "response": ""}}]
        + [{"status": 200, "body": {"success": False, "error": "Agent still running"}}] * 50
    )
    result = imp.smoke_invoke("https://p.example", "tok", "oai_exit_probe-abc", "x")
    assert result["attempts"] == imp.SMOKE_ATTEMPTS == 4


class _FakeControl:
    """bedrock-agentcore-control stand-in: delete is an acceptance, absence arrives after N reads."""

    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, reads_until_gone: int):
        self.reads_until_gone = reads_until_gone
        self.log: list[str] = []
        self.pages = [
            {"agentRuntimes": [{"agentRuntimeId": "other-1", "agentRuntimeName": "other"}], "nextToken": "p2"},
            {"agentRuntimes": [{"agentRuntimeId": "oai_exit_probe-abc", "agentRuntimeName": "oai_exit_probe"}]},
        ]

    def list_agent_runtimes(self, **kwargs):
        return self.pages[1] if kwargs.get("nextToken") == "p2" else self.pages[0]

    def delete_agent_runtime(self, agentRuntimeId):
        self.log.append(f"delete {agentRuntimeId}")

    def get_agent_runtime(self, agentRuntimeId):
        self.log.append(f"get {agentRuntimeId}")
        if self.reads_until_gone <= 0:
            raise self.exceptions.ResourceNotFoundException()
        self.reads_until_gone -= 1
        return {"status": "DELETING"}


class _FakeSession:
    def __init__(self, ctrl):
        self.ctrl = ctrl

    def client(self, name, **_):
        assert name == "bedrock-agentcore-control"
        return self.ctrl


def test_find_runtime_ids_paginates_and_destroy_runtime_confirms_absence(monkeypatch) -> None:
    ctrl = _FakeControl(reads_until_gone=2)
    session = _FakeSession(ctrl)
    monkeypatch.setattr(imp.time, "sleep", lambda _s: None)
    assert imp.find_runtime_ids(session, "oai_exit_probe") == ["oai_exit_probe-abc"]
    imp.destroy_runtime(session, "oai_exit_probe-abc")
    assert ctrl.log == [
        "delete oai_exit_probe-abc",
        "get oai_exit_probe-abc",
        "get oai_exit_probe-abc",
        "get oai_exit_probe-abc",
    ]

    failed = _FakeControl(reads_until_gone=99)
    failed.get_agent_runtime = lambda agentRuntimeId: {"status": "DELETE_FAILED"}
    with pytest.raises(RuntimeError, match="DELETE_FAILED"):
        imp.destroy_runtime(_FakeSession(failed), "oai_exit_probe-abc")


def test_teardown_releases_then_destroys_then_removes_role_log_groups_and_zip(monkeypatch, tmp_path: Path) -> None:
    order: list[str] = []
    monkeypatch.setattr(imp, "_session", lambda region, profile=None: "session")
    monkeypatch.setattr(imp, "find_runtime_ids", lambda session, name: ["oai_exit_probe-abc"])
    monkeypatch.setattr(
        imp,
        "release_from_platform",
        lambda url, token, rid: order.append(f"release {rid} {url} {token}") or {"status": 200, "body": {}},
    )
    monkeypatch.setattr(imp, "destroy_runtime", lambda session, rid: order.append(f"destroy {rid}"))
    monkeypatch.setattr(imp, "delete_role", lambda session, name: order.append(f"role {name}"))
    monkeypatch.setattr(
        imp,
        "delete_runtime_log_groups",
        lambda session, name: (
            order.append(f"logs {name}")
            or ["/aws/bedrock-agentcore/runtimes/oai_exit_probe-abc-DEFAULT"]  # pragma: allowlist secret
        ),
    )

    class _S3:
        def list_objects_v2(self, Bucket, Prefix):
            order.append(f"list {Bucket}/{Prefix}")
            return {"Contents": [{"Key": Prefix + "deadbeef.zip"}]}

        def delete_object(self, Bucket, Key):
            order.append(f"s3 {Key}")

    class _Session:
        def client(self, name, **_):
            assert name == "s3"
            return _S3()

    monkeypatch.setattr(imp, "_session", lambda region, profile=None: _Session())
    monkeypatch.setenv("API_BEARER_TOKEN", "tok")
    args = imp._parse(
        ["teardown", "--runtime-name", "oai_exit_probe", "--bucket", "b", "--platform-url", "https://p.example/"]
    )
    assert imp.teardown(args) == 0
    assert order == [
        "release oai_exit_probe-abc https://p.example/ tok",
        "destroy oai_exit_probe-abc",
        "role ImportedAgentRuntime-oai_exit_probe",
        "logs oai_exit_probe",
        "list b/imports/openai-agents-sdk/oai_exit_probe/",
        "s3 imports/openai-agents-sdk/oai_exit_probe/deadbeef.zip",
    ]
    # Without a platform URL nothing is released and no token is needed.
    order.clear()
    monkeypatch.delenv("API_BEARER_TOKEN")
    assert imp.teardown(imp._parse(["teardown", "--runtime-name", "oai_exit_probe", "--bucket", "b"])) == 0
    assert order[0] == "destroy oai_exit_probe-abc"
    # A platform URL without a token refuses before touching anything.
    with pytest.raises(RuntimeError, match="API_BEARER_TOKEN is not set"):
        imp.teardown(
            imp._parse(
                ["teardown", "--runtime-name", "oai_exit_probe", "--bucket", "b", "--platform-url", "https://p.example"]
            )
        )
    with pytest.raises(ValueError, match="runtime-name"):
        imp.teardown(imp._parse(["teardown", "--runtime-name", "bad-name!", "--bucket", "b"]))


def test_runtime_log_group_prefix_cannot_match_a_longer_runtime_name() -> None:
    prefix = imp.runtime_log_group_prefix("oai_exit_probe")
    assert prefix == "/aws/bedrock-agentcore/runtimes/oai_exit_probe-"
    longer_name_group = "/aws/bedrock-agentcore/runtimes/oai_exit_probe_v2-abc-DEFAULT"  # pragma: allowlist secret
    own_group = "/aws/bedrock-agentcore/runtimes/oai_exit_probe-abc-DEFAULT"  # pragma: allowlist secret
    assert not longer_name_group.startswith(prefix)
    assert own_group.startswith(prefix)
