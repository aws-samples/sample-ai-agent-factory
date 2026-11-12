#!/usr/bin/env python3
"""Import an OpenAI Agent Builder export (Agents SDK, Python) as an AgentCore Runtime under this platform.

OpenAI deprecated Agent Builder on 2026-06-03 and scheduled its shutdown for 2026-11-30; the only
export is *Code -> Agents SDK* (Python or TypeScript), a code export of the workflow, not a graph.
This tool takes that Python export and, without editing it, makes it a governed agent here:

    validate  -> refuse what cannot run headless on AWS (exact messages, nothing created)
    wrap      -> add agent.py, a BedrockAgentCoreApp entrypoint around the export's entry callable
    package   -> a deterministic ZIP with hash-locked dependencies for Linux x86_64 / Python 3.13
    deploy    -> least-privilege execution role + S3 upload + CreateAgentRuntime (the platform's
                 own tested request shape, services.runtime_deployer.create_agent_runtime)
    adopt     -> POST /api/runtime/import so observability, cost, registry, slots and DELETE apply
    teardown  -> DELETE ... ?destroy=true, then the role and the ZIP; zero residue asserted

Model providers:
    openai   the export keeps calling OpenAI; the key is read at cold start from the Secrets Manager
             secret named by env OPENAI_API_KEY_SECRET_ARN (never from code or a plain env var).
    bedrock  the export runs on Amazon Bedrock's OpenAI-compatible endpoint
             (https://bedrock-runtime.{region}.amazonaws.com/openai/v1) with a short-term Bedrock API
             key minted per invocation by aws-bedrock-token-generator, the SDK switched to the
             chat-completions API, tracing to OpenAI disabled, and the model id overridden through a
             RunConfig injected by a thin Runner.run wrapper (the export's OpenAI model names would
             not resolve on Bedrock). Zero edits to the exported file.

Runs with the backend virtualenv (`backend/.venv/bin/python scripts/import/openai_agents_sdk.py ...`)
so `deploy` can import the platform's runtime deployer. Everything before `deploy` is pure and
offline. See docs/MIGRATE_FROM_OPENAI_AGENT_BUILDER.md.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

IMPORTER_VERSION = "1.0.0"
PYTHON_RUNTIME = "PYTHON_3_13"
PIP_PYTHON_VERSION = "3.13"
PIP_PLATFORM = "x86_64-manylinux2014"
MODEL_PROVIDERS = ("openai", "bedrock")
#: Verified live 2026-11-12 (us-east-1): GPT-5.6 on Bedrock serves the Agents SDK through the Responses API with
#: function tools, guardrails and handoffs untouched. gpt-oss models only support chat completions there and
#: reject function tools with reasoning, so they are the fallback (`--bedrock-model-id openai.gpt-oss-120b-1:0
#: --bedrock-openai-api chat_completions`).
DEFAULT_BEDROCK_MODEL = "global.openai.gpt-5.6-terra"
DEFAULT_BEDROCK_OPENAI_API = "responses"
BEDROCK_OPENAI_APIS = ("responses", "chat_completions")
ENTRYPOINT_FILE = "agent.py"
MANIFEST_FILE = "IMPORT_MANIFEST.json"
#: Exact pins the bundle installs. openai-agents is the version this importer was verified against;
#: the AgentCore / boto3 pins match backend/agentcore-deps-constraints.txt.
BUNDLE_REQUIREMENTS: tuple[str, ...] = (
    "openai-agents==0.24.0",
    "bedrock-agentcore==1.23.1",
    "aws-bedrock-token-generator==1.1.0",
    "boto3==1.43.109",
)

# --------------------------------------------------------------------------- refusal codes

R_NO_PYTHON = "no_python_export"
R_TYPESCRIPT = "typescript_export"
R_NO_AGENTS_IMPORT = "no_agents_sdk_import"
R_LITERAL_SECRET = "literal_secret"
R_HOSTED_TOOL = "openai_hosted_tool"
R_HUMAN_APPROVAL = "human_approval"
R_ENTRY_AMBIGUOUS = "entry_ambiguous"
R_ENTRY_MISSING = "entry_missing"
W_MCP_PASSTHROUGH = "mcp_passthrough"
W_TRACING = "openai_tracing"

#: Tools that run on OpenAI's infrastructure or against OpenAI-hosted data. They cannot be served
#: from an AgentCore Runtime; each names the AWS replacement.
OPENAI_HOSTED_TOOLS: dict[str, str] = {
    "FileSearchTool": "OpenAI vector stores are not reachable from AWS; load the documents into a Managed Knowledge Base (docs/KNOWLEDGE_BASE.md) and expose it as a Gateway tool, or use Web Answer for public web content",
    "WebSearchTool": "OpenAI hosted web search; use the platform's managed Web Search (`web_search`) or Web Answer (`web_answer`) Gateway tools",
    "CodeInterpreterTool": "OpenAI hosted code execution; use the AgentCore Code Interpreter",
    "ComputerTool": "OpenAI hosted computer use; use the AgentCore Browser tool",
    "ImageGenerationTool": "OpenAI hosted image generation; call an image model on Amazon Bedrock from a custom tool",
    "LocalShellTool": "shell execution inside an agent runtime is not permitted; move the logic into a custom Gateway tool Lambda",
    "ShellTool": "shell execution inside an agent runtime is not permitted; move the logic into a custom Gateway tool Lambda",
}
MCP_TOOL_NAMES = (
    "HostedMCPTool",
    "MCPServerStreamableHttp",
    "MCPServerSse",
    "MCPServerStdio",
    "MCPServerStreamableHTTP",
)
HUMAN_APPROVAL_MARKERS = ("needs_approval", "HumanApproval", "human_approval", "ToolApprovalItem", "approval_request")
VECTOR_STORE_ID = re.compile(r"^vs_[A-Za-z0-9]{8,}$")
OPENAI_KEY_LITERAL = re.compile(r"^sk-[A-Za-z0-9_-]{16,}$")
SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)
RUNTIME_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")


class ImportRefused(Exception):
    """The export cannot be imported as given; ``findings`` carry the exact reasons."""

    def __init__(self, findings: list[Finding]):
        self.findings = findings
        super().__init__("\n".join(f.render() for f in findings))


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    path: str = ""
    line: int = 0
    severity: str = "refuse"  # refuse | warn

    def render(self) -> str:
        where = f" ({self.path}:{self.line})" if self.path else ""
        return f"[{self.severity}:{self.code}]{where} {self.message}"


@dataclass(frozen=True)
class Entry:
    module: str
    function: str
    is_async: bool
    input_mode: str  # "text" | "model"
    model_class: str = ""


@dataclass
class ValidationReport:
    refusals: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    python_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refusals

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "refusals": [asdict(f) for f in self.refusals],
            "warnings": [asdict(f) for f in self.warnings],
            "python_files": list(self.python_files),
        }


# --------------------------------------------------------------------------- validate


def _python_files(export_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in export_dir.rglob("*.py")
        if "__pycache__" not in p.parts and not any(part.startswith(".") for part in p.relative_to(export_dir).parts)
    )


def _typescript_files(export_dir: Path) -> list[Path]:
    return sorted(p for p in export_dir.rglob("*.ts") if "node_modules" not in p.parts)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _imports_agents(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "agents" or alias.name.startswith("agents.") for alias in node.names
        ):
            return True
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "agents" or node.module.startswith("agents."))
        ):
            return True
    return False


def _string_literals(tree: ast.AST) -> Iterable[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value, getattr(node, "lineno", 0)


def _assigned_secret_names(tree: ast.AST) -> Iterable[tuple[str, int]]:
    """``OPENAI_API_KEY = "..."`` / ``os.environ["OPENAI_API_KEY"] = "..."`` with a literal value."""
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Assign)
            or not isinstance(node.value, ast.Constant)
            or not isinstance(node.value.value, str)
        ):
            continue
        if not node.value.value:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in SECRET_ENV_NAMES:
                yield target.id, node.lineno
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value in SECRET_ENV_NAMES
            ):
                yield str(target.slice.value), node.lineno


def _scan_file(path: Path, rel: str, allow_hosted: bool, report: ValidationReport) -> bool:
    """Append findings for one file; return whether it imports the Agents SDK."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except SyntaxError as exc:
        report.refusals.append(Finding(R_NO_PYTHON, f"not valid Python: {exc.msg}", rel, exc.lineno or 0))
        return False
    for value, line in _string_literals(tree):
        if OPENAI_KEY_LITERAL.match(value.strip()):
            report.refusals.append(
                Finding(
                    R_LITERAL_SECRET,
                    "an OpenAI API key is written in the code; remove it and store it in Secrets Manager (OPENAI_API_KEY_SECRET_ARN)",
                    rel,
                    line,
                )
            )
        if VECTOR_STORE_ID.match(value.strip()) and not allow_hosted:
            report.refusals.append(
                Finding(
                    R_HOSTED_TOOL,
                    f"OpenAI vector store id {value!r}: {OPENAI_HOSTED_TOOLS['FileSearchTool']}",
                    rel,
                    line,
                )
            )
    for name, line in _assigned_secret_names(tree):
        report.refusals.append(
            Finding(
                R_LITERAL_SECRET,
                f"{name} is assigned a literal value in code; secrets come from Secrets Manager at runtime, never from the export",
                rel,
                line,
            )
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in OPENAI_HOSTED_TOOLS and not allow_hosted:
                report.refusals.append(Finding(R_HOSTED_TOOL, f"{name}: {OPENAI_HOSTED_TOOLS[name]}", rel, node.lineno))
            if name in MCP_TOOL_NAMES:
                report.warnings.append(
                    Finding(
                        W_MCP_PASSTHROUGH,
                        f"{name} is kept as is; the MCP server is called from inside the runtime. Put it behind an AgentCore Gateway target to get Cedar authorization and Gateway guardrails.",
                        rel,
                        node.lineno,
                        "warn",
                    )
                )
            if name == "trace":
                report.warnings.append(
                    Finding(
                        W_TRACING,
                        "the export opens an OpenAI trace; in bedrock mode tracing to OpenAI is disabled and AgentCore observability applies",
                        rel,
                        node.lineno,
                        "warn",
                    )
                )
            for kw in node.keywords:
                if kw.arg in HUMAN_APPROVAL_MARKERS:
                    report.refusals.append(
                        Finding(
                            R_HUMAN_APPROVAL,
                            f"{kw.arg}=: a headless AgentCore Runtime cannot pause for a human; model the approval as a platform HITL step (docs/ENTERPRISE_CAPABILITIES.md) and remove it from the export",
                            rel,
                            node.lineno,
                        )
                    )
        if isinstance(node, ast.Name | ast.Attribute):
            ident = node.id if isinstance(node, ast.Name) else node.attr
            if ident in ("HumanApproval", "human_approval"):
                report.refusals.append(
                    Finding(
                        R_HUMAN_APPROVAL,
                        f"{ident}: a headless AgentCore Runtime cannot pause for a human; model the approval as a platform HITL step and remove it from the export",
                        rel,
                        node.lineno,
                    )
                )
    return _imports_agents(tree)


def validate_export(export_dir: Path, *, allow_openai_hosted_tools: bool = False) -> ValidationReport:
    """Static validation of an Agent Builder Python export. Pure; touches no AWS API."""
    report = ValidationReport()
    export_dir = Path(export_dir)
    if not export_dir.is_dir():
        report.refusals.append(Finding(R_NO_PYTHON, f"{export_dir} is not a directory"))
        return report
    py_files = _python_files(export_dir)
    report.python_files = [str(p.relative_to(export_dir)) for p in py_files]
    if not py_files:
        ts = _typescript_files(export_dir)
        if ts:
            report.refusals.append(
                Finding(
                    R_TYPESCRIPT,
                    f"only a TypeScript export was found ({ts[0].name}); re-export the workflow from Agent Builder as Agents SDK -> Python (TypeScript imports are not supported in this release)",
                )
            )
        else:
            report.refusals.append(
                Finding(
                    R_NO_PYTHON, "no Python files found; export the workflow from Agent Builder as Agents SDK -> Python"
                )
            )
        return report
    imports_sdk = False
    for path in py_files:
        imports_sdk = (
            _scan_file(path, str(path.relative_to(export_dir)), allow_openai_hosted_tools, report) or imports_sdk
        )
    unparseable = any(f.code == R_NO_PYTHON for f in report.refusals)
    if not imports_sdk and not unparseable:
        # A file that did not parse is reported once, for its syntax; piling "no SDK import" on top
        # would blame the export for a second thing the first finding already explains.
        report.refusals.append(
            Finding(
                R_NO_AGENTS_IMPORT,
                "no file imports the OpenAI Agents SDK (`agents`); this does not look like an Agent Builder export",
            )
        )
    # Deterministic order: refusals by (path, line, code).
    report.refusals.sort(key=lambda f: (f.path, f.line, f.code))
    report.warnings.sort(key=lambda f: (f.path, f.line, f.code))
    return report


# --------------------------------------------------------------------------- entry detection


def _module_name(export_dir: Path, path: Path) -> str:
    return ".".join(path.relative_to(export_dir).with_suffix("").parts)


def _candidate_entries(tree: ast.Module) -> list[tuple[str, bool, str]]:
    """Top-level ``(name, is_async, first parameter annotation)`` for functions that take exactly one positional parameter."""
    out = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            if node.name.startswith("_") or len(node.args.args) != 1:
                continue
            annotation = node.args.args[0].annotation
            ann = (
                annotation.id
                if isinstance(annotation, ast.Name)
                else (annotation.attr if isinstance(annotation, ast.Attribute) else "")
            )
            out.append((node.name, isinstance(node, ast.AsyncFunctionDef), ann))
    return out


def detect_entry(export_dir: Path, entry: str | None = None) -> Entry:
    """Resolve the callable the entrypoint will invoke with the user's text.

    ``entry`` is ``module:function``; when omitted, the export must contain exactly one top-level
    ``async def`` with one parameter across its files (Agent Builder exports one ``run_workflow``-style
    function). The parameter's annotation decides how the text is passed: a class name (e.g. the
    export's ``WorkflowInput`` pydantic model with ``input_as_text``) is instantiated; anything else
    receives the plain string.
    """
    export_dir = Path(export_dir)
    trees = {
        _module_name(export_dir, p): ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for p in _python_files(export_dir)
    }
    if entry:
        if ":" not in entry:
            raise ImportRefused([Finding(R_ENTRY_MISSING, f"--entry must be module:function, got {entry!r}")])
        module, function = entry.split(":", 1)
        tree = trees.get(module)
        if tree is None:
            raise ImportRefused(
                [Finding(R_ENTRY_MISSING, f"--entry module {module!r} is not in the export ({sorted(trees)})")]
            )
        for name, is_async, ann in _candidate_entries(tree):
            if name == function:
                return Entry(module, function, is_async, "model" if ann and ann[:1].isupper() else "text", ann)
        raise ImportRefused(
            [
                Finding(
                    R_ENTRY_MISSING,
                    f"--entry {entry!r}: no top-level function {function!r} with exactly one parameter in {module}",
                )
            ]
        )
    found = [
        (module, name, is_async, ann)
        for module, tree in trees.items()
        for name, is_async, ann in _candidate_entries(tree)
        if is_async
    ]
    if len(found) == 1:
        module, name, is_async, ann = found[0]
        return Entry(module, name, is_async, "model" if ann and ann[:1].isupper() else "text", ann)
    if not found:
        raise ImportRefused(
            [
                Finding(
                    R_ENTRY_MISSING,
                    "no top-level `async def` with one parameter found; pass --entry module:function naming the workflow entry (the function that calls Runner.run)",
                )
            ]
        )
    names = ", ".join(f"{m}:{n}" for m, n, _a, _ann in found)
    raise ImportRefused(
        [Finding(R_ENTRY_AMBIGUOUS, f"several candidate entry functions ({names}); pass --entry module:function")]
    )


# --------------------------------------------------------------------------- wrap


_ENTRYPOINT_TEMPLATE = '''"""AgentCore Runtime entrypoint generated by scripts/import/openai_agents_sdk.py v__IMPORTER_VERSION__.

Wraps the OpenAI Agent Builder export (`__ENTRY_MODULE__.__ENTRY_FUNCTION__`) without editing it.
Payload contract (same as every agent this platform generates): {"prompt": "<user text>", "session_id"?,
"warmup"?}; response {"response": "<text>", "tool_receipts": [...]}. Model provider: __MODEL_PROVIDER__.
"""

import asyncio
import hashlib
import inspect
import json
import os

from bedrock_agentcore import BedrockAgentCoreApp

ENTRY_MODULE = "__ENTRY_MODULE__"
ENTRY_FUNCTION = "__ENTRY_FUNCTION__"
ENTRY_INPUT_MODE = "__ENTRY_INPUT_MODE__"
MODEL_PROVIDER = "__MODEL_PROVIDER__"
_RECEIPT_LIMIT = 64

app = BedrockAgentCoreApp()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _read_secret(secret_arn):
    import boto3

    region = os.environ.get("AWS_REGION") or secret_arn.split(":")[3]
    value = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=secret_arn)["SecretString"]
    try:
        decoded = json.loads(value)
    except ValueError:
        return value.strip()
    if isinstance(decoded, dict):
        for key in ("OPENAI_API_KEY", "api_key", "apiKey", "key", "value"):
            if decoded.get(key):
                return str(decoded[key])
    return value.strip()


def _configure_openai():
    """Bring-your-own OpenAI key: read once per container from Secrets Manager, never from code."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    secret_arn = os.environ.get("OPENAI_API_KEY_SECRET_ARN", "")
    if not secret_arn.startswith("arn:aws:secretsmanager:"):
        raise RuntimeError("OPENAI_API_KEY_SECRET_ARN must name the Secrets Manager secret holding the OpenAI API key")
    os.environ["OPENAI_API_KEY"] = _read_secret(secret_arn)


def _configure_bedrock():
    """Run the export on Bedrock's OpenAI-compatible endpoint with a short-term Bedrock API key."""
    from agents import Runner, RunConfig, set_default_openai_api, set_default_openai_client, set_tracing_disabled
    from aws_bedrock_token_generator import provide_token
    from openai import AsyncOpenAI

    region = os.environ.get("AWS_REGION") or "us-east-1"
    model_id = os.environ.get("AGENT_MODEL_ID", "__DEFAULT_BEDROCK_MODEL__")
    api = os.environ.get("BEDROCK_OPENAI_API", "__DEFAULT_BEDROCK_OPENAI_API__")
    client = AsyncOpenAI(base_url=f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1", api_key=provide_token(region=region))
    set_default_openai_client(client, use_for_tracing=False)
    set_default_openai_api(api)
    set_tracing_disabled(True)  # the SDK would otherwise export traces to OpenAI with the Bedrock token
    original_run = Runner.run

    async def _run_with_model_override(starting_agent, input, **kwargs):
        run_config = kwargs.get("run_config")
        if run_config is None:
            kwargs["run_config"] = RunConfig(model=model_id, tracing_disabled=True)
        elif isinstance(run_config, dict):
            kwargs["run_config"] = {"model": model_id, "tracing_disabled": True, **run_config}
        elif getattr(run_config, "model", None) is None:
            run_config.model = model_id
        return await original_run(starting_agent, input, **kwargs)

    if getattr(Runner.run, "__name__", "") != "_run_with_model_override":
        Runner.run = staticmethod(_run_with_model_override)


def _configure_provider():
    if MODEL_PROVIDER == "bedrock":
        _configure_bedrock()
    else:
        _configure_openai()


def _load_entry():
    module = __import__(ENTRY_MODULE, fromlist=["*"])
    return getattr(module, ENTRY_FUNCTION)


def _build_input(entry, prompt):
    if ENTRY_INPUT_MODE != "model":
        return prompt
    parameter = next(iter(inspect.signature(entry).parameters.values()))
    cls = parameter.annotation
    if not isinstance(cls, type):
        return prompt
    fields = getattr(cls, "model_fields", {})
    if "input_as_text" in fields:
        return cls(input_as_text=prompt)
    return cls(prompt)


def _tool_receipts(result):
    """One receipt per tool call the run made; argument VALUES never leave the runtime, digests do."""
    receipts = []
    outputs = {}
    for item in getattr(result, "new_items", []) or []:
        if type(item).__name__ == "ToolCallOutputItem":
            raw = getattr(item, "raw_item", None)
            call_id = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", None)
            output = getattr(item, "output", None)
            failed = isinstance(output, dict) and bool(output.get("error"))
            outputs[call_id] = "error" if failed else "success"
    for item in getattr(result, "new_items", []) or []:
        if type(item).__name__ != "ToolCallItem":
            continue
        raw = getattr(item, "raw_item", None)
        name = str(getattr(raw, "name", None) or (raw.get("name") if isinstance(raw, dict) else "") or "")
        call_id = getattr(raw, "call_id", None) or (raw.get("call_id") if isinstance(raw, dict) else None)
        arguments = getattr(raw, "arguments", None) or (raw.get("arguments") if isinstance(raw, dict) else None) or "{}"
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
        except (ValueError, TypeError):
            parsed = {}
        receipts.append(
            {
                "name": name[:256],
                "status": outputs.get(call_id, "missing"),
                "input_sha256": _digest(parsed if isinstance(parsed, dict) else {}),
                "argument_sha256": {
                    str(k)[:128]: _digest(v) for k, v in (list(parsed.items()) if isinstance(parsed, dict) else [])[:32]
                },
            }
        )
        if len(receipts) >= _RECEIPT_LIMIT:
            break
    return receipts


def _final_text(result):
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    final = getattr(result, "final_output", result)
    if isinstance(final, str):
        return final
    if hasattr(final, "model_dump"):
        return json.dumps(final.model_dump(), default=str)
    if isinstance(final, (dict, list)):
        return json.dumps(final, default=str)
    return str(final)


def _guardrail_block(exc):
    """A tripped SDK guardrail is a clean refusal, not a runtime error: name it, never echo the input."""
    result = getattr(exc, "guardrail_result", None)
    guardrail = getattr(result, "guardrail", None)
    name = getattr(guardrail, "name", None) or getattr(getattr(guardrail, "guardrail_function", None), "__name__", "guardrail")
    info = getattr(getattr(result, "output", None), "output_info", None)
    return {
        "response": f"Request blocked by guardrail {name}.",
        "blocked_by_guardrail": {"name": str(name), "kind": type(exc).__name__, "output_info": info},
        "tool_receipts": [],
    }


@app.entrypoint
async def invoke(payload, context=None):
    if isinstance(payload, dict) and payload.get("warmup") is True:
        return {"response": "", "warmup": True}
    prompt = ""
    if isinstance(payload, dict):
        prompt = str(payload.get("prompt") or payload.get("input_as_text") or payload.get("input") or "")
    if not prompt:
        return {"response": "", "error": "payload.prompt is required"}
    _configure_provider()
    from agents.exceptions import InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered

    entry = _load_entry()
    try:
        outcome = entry(_build_input(entry, prompt))
        if inspect.isawaitable(outcome):
            outcome = await outcome
    except (InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered) as exc:
        return _guardrail_block(exc)
    return {"response": _final_text(outcome), "tool_receipts": _tool_receipts(outcome)}


if __name__ == "__main__":
    app.run()
'''


def render_entrypoint(entry: Entry, model_provider: str) -> str:
    if model_provider not in MODEL_PROVIDERS:
        raise ValueError(f"model_provider must be one of {MODEL_PROVIDERS}, got {model_provider!r}")
    return (
        _ENTRYPOINT_TEMPLATE.replace("__IMPORTER_VERSION__", IMPORTER_VERSION)
        .replace("__ENTRY_MODULE__", entry.module)
        .replace("__ENTRY_FUNCTION__", entry.function)
        .replace("__ENTRY_INPUT_MODE__", entry.input_mode)
        .replace("__MODEL_PROVIDER__", model_provider)
        .replace("__DEFAULT_BEDROCK_MODEL__", DEFAULT_BEDROCK_MODEL)
        .replace("__DEFAULT_BEDROCK_OPENAI_API__", DEFAULT_BEDROCK_OPENAI_API)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bundle(
    export_dir: Path,
    bundle_dir: Path,
    *,
    model_provider: str,
    entry: str | None = None,
    allow_openai_hosted_tools: bool = False,
) -> dict:
    """validate + detect entry + copy the export unchanged + add agent.py, requirements.in and the manifest."""
    report = validate_export(export_dir, allow_openai_hosted_tools=allow_openai_hosted_tools)
    if not report.ok:
        raise ImportRefused(report.refusals)
    resolved = detect_entry(export_dir, entry)
    export_dir, bundle_dir = Path(export_dir), Path(bundle_dir)
    if (export_dir / ENTRYPOINT_FILE).exists():
        raise ImportRefused(
            [
                Finding(
                    R_ENTRY_AMBIGUOUS,
                    f"the export already contains {ENTRYPOINT_FILE}; rename it, the importer writes the Runtime entrypoint under that name",
                )
            ]
        )
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    shutil.copytree(
        export_dir, bundle_dir, ignore=shutil.ignore_patterns("__pycache__", ".git", ".venv", "*.pyc", "node_modules")
    )
    (bundle_dir / ENTRYPOINT_FILE).write_text(render_entrypoint(resolved, model_provider), encoding="utf-8")
    (bundle_dir / "requirements.in").write_text("\n".join(BUNDLE_REQUIREMENTS) + "\n", encoding="utf-8")
    manifest = {
        "importer": {"name": "openai_agents_sdk", "version": IMPORTER_VERSION},
        "source": "OpenAI Agent Builder export (Agents SDK, Python)",
        "entry": asdict(resolved),
        "model_provider": model_provider,
        "python_runtime": PYTHON_RUNTIME,
        "requirements": list(BUNDLE_REQUIREMENTS),
        "warnings": [asdict(f) for f in report.warnings],
        "files": {
            str(p.relative_to(bundle_dir)): _sha256_file(p) for p in sorted(bundle_dir.rglob("*")) if p.is_file()
        },
    }
    (bundle_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------- package


def _uv() -> str:
    found = shutil.which("uv")
    if not found:
        raise RuntimeError(
            "uv is required to resolve and install the bundle's dependencies (https://docs.astral.sh/uv/)"
        )
    return found


def compile_requirements(bundle_dir: Path) -> Path:
    """Hash-lock the bundle's dependencies for the Runtime's platform (same flags as backend/requirements-lambda.txt)."""
    bundle_dir = Path(bundle_dir)
    lock = bundle_dir / "requirements.lock"
    subprocess.run(
        [
            _uv(),
            "pip",
            "compile",
            str(bundle_dir / "requirements.in"),
            "--python-platform",
            PIP_PLATFORM,
            "--python-version",
            PIP_PYTHON_VERSION,
            "--only-binary",
            ":all:",
            "--generate-hashes",
            "--no-annotate",
            "--quiet",
            "--output-file",
            str(lock),
        ],
        check=True,
    )
    return lock


def install_requirements(bundle_dir: Path) -> None:
    """Install the hash-locked closure INTO the bundle root, the layout AgentCore's direct code deploy expects."""
    bundle_dir = Path(bundle_dir)
    subprocess.run(
        [
            _uv(),
            "pip",
            "install",
            "--quiet",
            "--require-hashes",
            "--no-deps",
            "-r",
            str(bundle_dir / "requirements.lock"),
            "--target",
            str(bundle_dir),
            "--python-platform",
            PIP_PLATFORM,
            "--python-version",
            PIP_PYTHON_VERSION,
            "--only-binary",
            ":all:",
        ],
        check=True,
    )
    for pycache in bundle_dir.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)


def build_zip(bundle_dir: Path, zip_path: Path) -> str:
    """Deterministic ZIP: sorted paths, fixed timestamp, normalized permissions. Returns its SHA-256."""
    bundle_dir, zip_path = Path(bundle_dir), Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    fixed = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(bundle_dir).as_posix()
            info = zipfile.ZipInfo(rel, date_time=fixed)
            mode = 0o755 if (path.stat().st_mode & stat.S_IXUSR) or rel.endswith(".so") else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return _sha256_file(zip_path)


# --------------------------------------------------------------------------- AWS wrappers (deploy / teardown)


def _session(region: str, profile: str | None = None):
    import boto3

    return boto3.session.Session(region_name=region, profile_name=profile)


def _account_id(session) -> str:
    return session.client("sts").get_caller_identity()["Account"]


def execution_role_policy(
    region: str,
    account_id: str,
    runtime_name: str,
    *,
    model_provider: str,
    secret_arn: str | None,
    bedrock_model_id: str,
    bucket: str = "",
    key: str = "",
) -> dict:
    """Least privilege for one imported runtime.

    Read of its own code ZIP (AgentCore fetches the artifact with the execution role, as the
    platform's own runtime role does), its own log group and traces, its workload identity, and
    either the one OpenAI key secret (openai) or short-term-bearer model invocation for the one
    model id (bedrock). Nothing AgentCore-control, nothing on other runtimes.
    """
    statements = []
    if bucket:
        statements.append(
            {
                "Sid": "CodeArtifact",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{bucket}",
                    f"arn:aws:s3:::{bucket}/{key or 'imports/openai-agents-sdk/' + runtime_name + '/*'}",
                ],
            }
        )
    statements += [
        {
            "Sid": "RuntimeLogs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogStreams",
                "logs:DescribeLogGroups",
            ],
            "Resource": [
                f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/runtimes/{runtime_name}*",
                f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/runtimes/{runtime_name}*:log-stream:*",
            ],
        },
        {
            "Sid": "RuntimeMetrics",
            "Effect": "Allow",
            "Action": ["cloudwatch:PutMetricData"],
            "Resource": "*",
            "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
        },
        {
            "Sid": "RuntimeTraces",
            "Effect": "Allow",
            "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
            "Resource": "*",
        },
        {
            "Sid": "WorkloadIdentity",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:GetWorkloadAccessToken",
                "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
            ],
            "Resource": [
                f"arn:aws:bedrock-agentcore:{region}:{account_id}:workload-identity-directory/default",
                f"arn:aws:bedrock-agentcore:{region}:{account_id}:workload-identity-directory/default/workload-identity/{runtime_name}-*",
            ],
        },
    ]
    if model_provider == "openai":
        if not secret_arn or not secret_arn.startswith("arn:aws:secretsmanager:"):
            raise ValueError("openai mode needs --openai-key-secret-arn (the Secrets Manager secret holding the key)")
        statements.append(
            {"Sid": "OpenAIKey", "Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": secret_arn}
        )
    else:
        bare = (
            bedrock_model_id.split(".", 1)[1]
            if bedrock_model_id.startswith(("us.", "eu.", "apac.", "global."))
            else bedrock_model_id
        )
        statements.append(
            {
                "Sid": "BedrockModel",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": [
                    f"arn:aws:bedrock:{region}::foundation-model/{bedrock_model_id}",
                    f"arn:aws:bedrock:*::foundation-model/{bare}",
                    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/{bedrock_model_id}",
                ],
            }
        )
        # The OpenAI-compatible endpoint authenticates with a short-term Bedrock API key minted
        # from this role's credentials; bearer-token calls need this action (Bedrock user guide,
        # "Control permissions for generating and using Amazon Bedrock API keys").
        statements.append(
            {
                "Sid": "BedrockShortTermBearer",
                "Effect": "Allow",
                "Action": ["bedrock:CallWithBearerToken"],
                "Resource": "*",
                "Condition": {"StringEquals": {"bedrock:bearerTokenType": "SHORT_TERM"}},
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _trust_policy(account_id: str, region: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"},
                },
            }
        ],
    }


def importer_tags(pairs: list[str]) -> dict:
    """The importer's OWN tags. Deliberately not the platform's ManagedBy/AgentCoreStack ownership
    tags: an adopted runtime is `imported`, and the platform's teardown gates must keep seeing it
    as a resource the platform did not build."""
    tags = {"ManagedBy": "openai_agents_sdk_importer", "ImporterVersion": IMPORTER_VERSION}
    for pair in pairs:
        key, _, value = pair.partition("=")
        if key and value:
            tags[key] = value
    return tags


def ensure_execution_role(
    session,
    *,
    role_name: str,
    region: str,
    account_id: str,
    runtime_name: str,
    model_provider: str,
    secret_arn: str | None,
    bedrock_model_id: str,
    tags: dict,
    bucket: str = "",
    key: str = "",
) -> str:
    iam = session.client("iam")
    created = False
    try:
        arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(_trust_policy(account_id, region)),
            Description=f"Execution role for imported OpenAI Agents SDK runtime {runtime_name}",
            Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
        )["Role"]["Arn"]
        created = True
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="ImportedRuntimeExecution",
        PolicyDocument=json.dumps(
            execution_role_policy(
                region,
                account_id,
                runtime_name,
                model_provider=model_provider,
                secret_arn=secret_arn,
                bedrock_model_id=bedrock_model_id,
                bucket=bucket,
                key=key,
            )
        ),
    )
    if created:
        time.sleep(10)  # IAM propagation before CreateAgentRuntime passes the role
    return arn


def upload_zip(session, *, bucket: str, key: str, zip_path: Path, expected_owner: str) -> None:
    with Path(zip_path).open("rb") as handle:
        session.client("s3").put_object(Bucket=bucket, Key=key, Body=handle, ExpectedBucketOwner=expected_owner)


def create_runtime_request(
    *, runtime_name: str, role_arn: str, bucket: str, key: str, env_vars: dict, tags: dict
) -> dict:
    """CreateAgentRuntime for a direct code deploy: the same shape the platform sends for its own
    runtimes (services/runtime_deployer.create_agent_runtime), minus the platform ownership tags."""
    return {
        "agentRuntimeName": runtime_name,
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {"s3": {"bucket": bucket, "prefix": key}},
                "runtime": PYTHON_RUNTIME,
                "entryPoint": [ENTRYPOINT_FILE],
            }
        },
        "roleArn": role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": dict(env_vars),
        "tags": dict(tags),
    }


def create_runtime(session, **kwargs) -> dict:
    ctrl = session.client("bedrock-agentcore-control")
    return ctrl.create_agent_runtime(**create_runtime_request(**kwargs))


def wait_for_runtime(session, runtime_id: str, *, timeout: int = 600) -> dict:
    ctrl = session.client("bedrock-agentcore-control")
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        resp = ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
        last = resp.get("status", "")
        if last == "READY":
            return resp
        if last.endswith("FAILED"):
            raise RuntimeError(f"runtime {runtime_id} entered {last}: {resp.get('failureReason') or ''}")
        time.sleep(10)
    raise RuntimeError(f"runtime {runtime_id} did not reach READY in {timeout}s (last {last})")


# --------------------------------------------------------------------------- platform adoption


def adopt_runtime(platform_url: str, token: str, runtime_arn: str, region: str) -> dict:
    import requests

    resp = requests.post(
        f"{platform_url.rstrip('/')}/api/runtime/import",
        headers={"Authorization": f"Bearer {token}"},
        json={"runtimeArn": runtime_arn, "awsRegion": region},
        timeout=60,
    )
    if resp.status_code != 201:
        raise RuntimeError(f"adopt failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def smoke_invoke(platform_url: str, token: str, runtime_name: str, prompt: str) -> dict:
    import uuid

    import requests

    resp = requests.post(
        f"{platform_url.rstrip('/')}/api/runtimes/{runtime_name}/slots/production/invoke",
        headers={"Authorization": f"Bearer {token}"},
        json={"input": prompt, "sessionId": str(uuid.uuid4())},
        timeout=240,
    )
    return {
        "status": resp.status_code,
        "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:500],
    }


def destroy_via_platform(platform_url: str, token: str, runtime_id: str) -> int:
    import requests

    return requests.delete(
        f"{platform_url.rstrip('/')}/api/runtime/{runtime_id}",
        params={"destroy": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    ).status_code


def delete_role(session, role_name: str) -> None:
    iam = session.client("iam")
    try:
        for name in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role_name, PolicyName=name)
        iam.delete_role(RoleName=role_name)
    except iam.exceptions.NoSuchEntityException:
        pass


# --------------------------------------------------------------------------- CLI


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="openai_agents_sdk", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("export_dir", type=Path, help="directory holding the Agents SDK Python export")
        p.add_argument(
            "--allow-openai-hosted-tools",
            action="store_true",
            help="do not refuse FileSearchTool/WebSearchTool/... (they will still fail at run time on AWS)",
        )

    v = sub.add_parser("validate", help="static checks only; prints the findings as JSON")
    common(v)
    for name in ("wrap", "package", "deploy", "all"):
        p = sub.add_parser(name)
        common(p)
        p.add_argument("--out", type=Path, required=True, help="bundle directory (recreated)")
        p.add_argument("--entry", help="module:function to call with the user's text (auto-detected when unambiguous)")
        p.add_argument("--model-provider", choices=MODEL_PROVIDERS, default="openai")
    for name in ("deploy", "all"):
        p = sub.choices[name]
        p.add_argument("--runtime-name", required=True, help="[A-Za-z][A-Za-z0-9_]{0,47}")
        p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
        p.add_argument("--profile", default=None)
        p.add_argument("--bucket", required=True, help="S3 bucket for the code ZIP (must exist, same account)")
        p.add_argument("--openai-key-secret-arn", default=None)
        p.add_argument("--bedrock-model-id", default=DEFAULT_BEDROCK_MODEL)
        p.add_argument("--bedrock-openai-api", choices=BEDROCK_OPENAI_APIS, default=DEFAULT_BEDROCK_OPENAI_API)
        p.add_argument(
            "--platform-url", default=None, help="adopt into the platform at this CloudFront URL after READY"
        )
        p.add_argument(
            "--platform-token-env", default="API_BEARER_TOKEN", help="env var holding the platform access token"
        )
        p.add_argument("--smoke-prompt", default=None)
        p.add_argument("--tag", action="append", default=[], help="key=value resource tag (repeatable)")
    t = sub.add_parser("teardown")
    t.add_argument("--runtime-name", required=True)
    t.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    t.add_argument("--profile", default=None)
    t.add_argument("--bucket", required=True)
    t.add_argument("--platform-url", default=None)
    t.add_argument("--platform-token-env", default="API_BEARER_TOKEN")
    t.add_argument(
        "--deployment-runtime-id", default=None, help="the platform runtime_id to DELETE ?destroy=true (from adopt)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    try:
        if args.command == "validate":
            report = validate_export(args.export_dir, allow_openai_hosted_tools=args.allow_openai_hosted_tools)
            print(json.dumps(report.as_dict(), indent=2))
            return 0 if report.ok else 2
        manifest = write_bundle(
            args.export_dir,
            args.out,
            model_provider=args.model_provider,
            entry=args.entry,
            allow_openai_hosted_tools=args.allow_openai_hosted_tools,
        )
        print(
            f"bundle written: {args.out} entry={manifest['entry']['module']}:{manifest['entry']['function']} provider={manifest['model_provider']}"
        )
        for warning in manifest["warnings"]:
            print(Finding(**warning).render())
        if args.command == "wrap":
            return 0
        compile_requirements(args.out)
        install_requirements(args.out)
        zip_path = args.out.with_suffix(".zip")
        digest = build_zip(args.out, zip_path)
        print(f"zip written: {zip_path} sha256={digest}")
        if args.command == "package":
            return 0
        if not RUNTIME_NAME.match(args.runtime_name):
            raise ValueError("--runtime-name must match [A-Za-z][A-Za-z0-9_]{0,47}")
        session = _session(args.region, args.profile)
        account_id = _account_id(session)
        tags = importer_tags(args.tag)
        role_name = f"ImportedAgentRuntime-{args.runtime_name}"[:64]
        key = f"imports/openai-agents-sdk/{args.runtime_name}/{digest[:16]}.zip"
        role_arn = ensure_execution_role(
            session,
            role_name=role_name,
            region=args.region,
            account_id=account_id,
            runtime_name=args.runtime_name,
            model_provider=args.model_provider,
            secret_arn=args.openai_key_secret_arn,
            bedrock_model_id=args.bedrock_model_id,
            tags=tags,
            bucket=args.bucket,
            key=key,
        )
        upload_zip(session, bucket=args.bucket, key=key, zip_path=zip_path, expected_owner=account_id)
        env_vars = {"AWS_REGION": args.region}
        if args.model_provider == "openai":
            env_vars["OPENAI_API_KEY_SECRET_ARN"] = args.openai_key_secret_arn or ""
        else:
            env_vars["AGENT_MODEL_ID"] = args.bedrock_model_id
            env_vars["BEDROCK_OPENAI_API"] = args.bedrock_openai_api
        created = create_runtime(
            session,
            runtime_name=args.runtime_name,
            role_arn=role_arn,
            bucket=args.bucket,
            key=key,
            env_vars=env_vars,
            tags=tags,
        )
        runtime_id = created["agentRuntimeId"]
        runtime_arn = created["agentRuntimeArn"]
        print(f"runtime created: {runtime_id} {runtime_arn}")
        wait_for_runtime(session, runtime_id)
        print("runtime READY")
        result = {
            "runtime_id": runtime_id,
            "runtime_arn": runtime_arn,
            "role_name": role_name,
            "bucket": args.bucket,
            "key": key,
            "zip_sha256": digest,
        }
        if args.platform_url:
            token = os.environ.get(args.platform_token_env, "")
            if not token:
                raise RuntimeError(f"{args.platform_token_env} is not set; mint a platform access token first")
            adopted = adopt_runtime(args.platform_url, token, runtime_arn, args.region)
            result["adopted"] = adopted
            print(f"adopted into the platform: deployment {adopted.get('deploymentId')}")
            if args.smoke_prompt:
                result["smoke"] = smoke_invoke(args.platform_url, token, args.runtime_name, args.smoke_prompt)
                print(f"smoke invoke: {result['smoke']['status']}")
        print(json.dumps(result, indent=2, default=str))
        return 0
    except ImportRefused as refused:
        print("IMPORT REFUSED", file=sys.stderr)
        for finding in refused.findings:
            print(finding.render(), file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"dependency step failed: {exc}", file=sys.stderr)
        return 3


def teardown(args: argparse.Namespace) -> int:
    session = _session(args.region, args.profile)
    if args.platform_url and args.deployment_runtime_id:
        token = os.environ.get(args.platform_token_env, "")
        status = destroy_via_platform(args.platform_url, token, args.deployment_runtime_id)
        print(f"platform DELETE ?destroy=true -> {status}")
    ctrl = session.client("bedrock-agentcore-control")
    for runtime in ctrl.list_agent_runtimes(maxResults=100).get("agentRuntimes", []):
        if runtime.get("agentRuntimeName") == args.runtime_name:
            ctrl.delete_agent_runtime(agentRuntimeId=runtime["agentRuntimeId"])
            print(f"deleted runtime {runtime['agentRuntimeId']}")
    delete_role(session, f"ImportedAgentRuntime-{args.runtime_name}"[:64])
    s3 = session.client("s3")
    prefix = f"imports/openai-agents-sdk/{args.runtime_name}/"
    for obj in s3.list_objects_v2(Bucket=args.bucket, Prefix=prefix).get("Contents", []):
        s3.delete_object(Bucket=args.bucket, Key=obj["Key"])
    print("teardown complete")
    return 0


if __name__ == "__main__":
    _args = _parse(None)
    sys.exit(teardown(_args) if _args.command == "teardown" else main(None))
