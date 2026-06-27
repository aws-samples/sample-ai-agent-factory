"""Python project exporter — "eject" a standalone, runnable agent project.

Phase 3 Gap 3G. Builds a self-contained Python project from any canvas that a
user can run locally or in their own infrastructure, mirroring the existing
CloudFormation-template export (cfn_template_generator.py) in shape but
targeting a plain ``python agent.py`` / Docker workflow instead of CFN.

The bundle contains:
- agent.py — the SAME generated agent source the CFN exporter embeds
  (code_generator.generate_agent_code in portable mode).
- requirements.txt — a REAL dependency list derived from PROVIDER_PACKAGES
  plus the runtime SDK packages the generated code actually imports.
- Dockerfile — python:3.13-slim base matching the platform's PYTHON_3_13 runtime.
- .env.example — blank placeholders for the env-driven config (no secrets).
- README.md — local + Docker run instructions.
- run.sh — one-command local launcher.

This module is PURE: no AWS, no FastAPI. The deployment_handler endpoint owns
S3 upload / presigning / owner-stamping. Keeping it pure also makes it trivial
to unit-test without moto.
"""

import io
import zipfile

from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import _sanitize_gateway_name
from app.services.code_generator import PROVIDER_PACKAGES, generate_agent_code

# Runtime SDK packages the generated agent.py always imports but that
# PROVIDER_PACKAGES intentionally omits (PROVIDER_PACKAGES only lists the
# Strands framework + the provider's model SDK). The generated code does
# `from bedrock_agentcore.runtime import BedrockAgentCoreApp` and uses boto3,
# so a runnable export MUST include both. See module risks in the gap spec.
_RUNTIME_PACKAGES = ["bedrock-agentcore", "boto3"]

# Added on top of the runtime packages when observability/OTEL is enabled. The
# generated OTEL bootstrap relies on the AWS OpenTelemetry distro.
_OBSERVABILITY_PACKAGES = ["aws-opentelemetry-distro"]

# Fallback provider package set when config.model_provider is unknown — the
# bedrock entry (plain Strands, no extra SDK). Mirrors the PROVIDER_PACKAGES
# bedrock value so a missing provider never KeyErrors. See gap risk #6.
_DEFAULT_PROVIDER_PACKAGES = "strands-agents strands-agents-tools"


def _observability_enabled(config: RuntimeConfig, connected_tools: list) -> bool:
    """Derive the observability flag the same way cfn_template_generator does.

    Mirrors cfn_template_generator.py:338-342 so the exported agent.py is
    byte-identical to the CFN-embedded one for the same request.
    """
    return bool(
        getattr(config, "observability", None)
        or "observability" in (connected_tools or [])
        or getattr(config, "enable_otel", False)
    )


# Minimum-version floors for the packages we ship in the export (Holmes
# supply-chain finding). We deliberately use ">=" floors rather than exact "=="
# pins: the platform itself ships rolling bundles, so a hard pin here would drift
# from the real tested environment and mislead. A floor still prevents pip from
# silently resolving to an ancient/yanked release while leaving forward
# compatibility. Packages not listed here fall back to a bare name; the generated
# header tells the user to pin exact versions before a production build.
_MIN_VERSIONS = {
    "bedrock-agentcore": "0.1.0",
    "boto3": "1.35.0",
    "strands-agents": "0.1.0",
    "strands-agents-tools": "0.1.0",
    "aws-opentelemetry-distro": "0.8.0",
}

_REQUIREMENTS_HEADER = (
    "# Dependencies for this exported agent. Versions use '>=' floors, not exact\n"
    "# pins — review and PIN exact, tested versions (e.g. 'boto3==<ver>') before a\n"
    "# production build for reproducible, supply-chain-safe installs.\n"
)


def _pin(pkg: str) -> str:
    floor = _MIN_VERSIONS.get(pkg)
    return f"{pkg}>={floor}" if floor else pkg


def build_requirements(config: RuntimeConfig, connected_tools=None) -> str:
    """Build a real requirements.txt body for the given config.

    Starts from PROVIDER_PACKAGES[config.model_provider] (space-separated
    package names), always adds the runtime SDK packages the generated code
    imports (bedrock-agentcore, boto3), and adds the AWS OTEL distro when
    observability is enabled. Dedupes and sorts. Applies ">=" minimum-version
    floors for known packages (Holmes supply-chain finding) and prepends a header
    telling the user to pin exact versions for production.
    """
    provider = getattr(config, "model_provider", "bedrock") or "bedrock"
    provider_pkgs = PROVIDER_PACKAGES.get(provider, _DEFAULT_PROVIDER_PACKAGES)

    packages: set[str] = set(provider_pkgs.split())
    packages.update(_RUNTIME_PACKAGES)
    if _observability_enabled(config, connected_tools or []):
        packages.update(_OBSERVABILITY_PACKAGES)

    return _REQUIREMENTS_HEADER + "\n".join(_pin(p) for p in sorted(packages)) + "\n"


def build_dockerfile() -> str:
    """Build a Dockerfile mirroring the platform's PYTHON_3_13 runtime.

    Uses python:3.13-slim, installs requirements, copies agent.py, and runs
    it. Env vars are supplied at ``docker run`` time (never baked in).
    """
    return (
        "# Standalone agent image (Phase 3 Gap 3G — Python export).\n"
        "# Matches the platform's PYTHON_3_13 runtime.\n"
        "FROM python:3.13-slim\n"
        "\n"
        "WORKDIR /app\n"
        "\n"
        "COPY requirements.txt ./\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "\n"
        "COPY agent.py ./\n"
        "\n"
        "# Config is supplied via environment variables at run time.\n"
        "# See .env.example for the full list. Never bake secrets into the image.\n"
        "ENV AWS_REGION=\"\"\n"
        "ENV MODEL_ID=\"\"\n"
        "\n"
        "CMD [\"python\", \"agent.py\"]\n"
    )


def build_env_example(config: RuntimeConfig, connected_tools=None) -> str:
    """Build a .env.example with BLANK placeholders only — never real secrets.

    Always includes MODEL_ID, AWS_REGION, AGENT_PROVIDER. Adds a provider
    API-key placeholder for non-bedrock providers, and the OTLP_* vars when
    observability is enabled. All values are blank so the file can be checked
    in safely (rule 5: secrets never land in plaintext).
    """
    provider = getattr(config, "model_provider", "bedrock") or "bedrock"
    model_id = ""
    if isinstance(config.model, dict):
        model_id = config.model.get("modelId") or config.model.get("model_id") or ""

    lines = [
        "# Standalone agent configuration. Copy to .env and fill in.",
        "# SECRETS (API keys, tokens) come from your environment / secrets",
        "# manager at run time — never commit real values to this file.",
        "",
        f"MODEL_ID={model_id}",
        "AWS_REGION=",
        f"AGENT_PROVIDER={provider}",
    ]

    if provider != "bedrock":
        # Blank placeholder for the provider API key. The platform stores the
        # real value in Secrets Manager (provider_api_key_ref); the export
        # intentionally ships only an empty placeholder.
        lines.append("")
        lines.append("# Provider API key — set this in your environment, do not commit it.")
        lines.append("PROVIDER_API_KEY=")

    if _observability_enabled(config, connected_tools or []):
        lines.append("")
        lines.append("# OpenTelemetry / OTLP export (observability enabled on this canvas).")
        lines.append("OTEL_EXPORTER_OTLP_ENDPOINT=")
        lines.append("OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf")
        lines.append("OTEL_SERVICE_NAME=")
        # Auth header for the OTLP backend — blank placeholder, no real secret.
        lines.append("OTEL_EXPORTER_OTLP_HEADERS=")

    return "\n".join(lines) + "\n"


def build_readme(deployment_name: str, config: RuntimeConfig) -> str:
    """Build run instructions for the ejected project."""
    return (
        f"# {deployment_name} — standalone agent\n"
        "\n"
        "This is a self-contained Python agent exported from the AgentCore Visual\n"
        "Workflow Platform. It uses the BedrockAgentCore Runtime SDK and runs as a\n"
        "plain Python process or a Docker container.\n"
        "\n"
        "## Run locally\n"
        "\n"
        "```bash\n"
        "pip install -r requirements.txt\n"
        "cp .env.example .env   # then fill in the blanks\n"
        "set -a && . ./.env && set +a\n"
        "python agent.py\n"
        "```\n"
        "\n"
        "Or use the bundled launcher:\n"
        "\n"
        "```bash\n"
        "./run.sh\n"
        "```\n"
        "\n"
        "Then invoke it (the SDK serves `POST /invocations` on port 8080):\n"
        "\n"
        "```bash\n"
        "curl -s localhost:8080/invocations \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  -d '{\"prompt\": \"hello\"}'\n"
        "```\n"
        "\n"
        "> **macOS note:** the built-in tools make outbound HTTPS calls with the\n"
        "> standard library, which on macOS may not find a CA bundle and fail with\n"
        "> an SSL certificate error. If a tool reports SSL issues, point Python at\n"
        "> certifi's bundle: `pip install certifi` then\n"
        "> `export SSL_CERT_FILE=$(python -c 'import certifi; print(certifi.where())')`\n"
        "> before running. On Linux / the AgentCore Runtime the system certs are\n"
        "> already present, so this is only needed for local macOS runs.\n"
        "\n"
        "## Run with Docker\n"
        "\n"
        "```bash\n"
        "docker build -t my-agent .\n"
        "docker run --rm -p 8080:8080 --env-file .env my-agent\n"
        "```\n"
        "\n"
        "## Configuration & secrets\n"
        "\n"
        "All configuration is supplied via environment variables (see\n"
        "`.env.example`). **Secrets — provider API keys, OTLP auth headers — are\n"
        "never written to the exported files.** Provide them through your own\n"
        "environment or secrets manager at run time.\n"
        "\n"
        "## Dependencies\n"
        "\n"
        "`requirements.txt` lists the packages this agent imports. Versions are\n"
        "intentionally left unpinned to match the platform's managed runtime. For\n"
        "production deployments, pin them to versions you have tested.\n"
    )


def build_run_sh() -> str:
    """One-command local launcher that loads .env and runs the agent."""
    return (
        "#!/usr/bin/env bash\n"
        "# Local launcher for the ejected agent.\n"
        "set -euo pipefail\n"
        "\n"
        "if [ -f .env ]; then\n"
        "  set -a\n"
        "  . ./.env\n"
        "  set +a\n"
        "fi\n"
        "\n"
        "python agent.py\n"
    )


def build_python_project(deploy_request: DeployRequest) -> dict:
    """Build the full set of project files for the given deploy request.

    Returns a ``{filename: content}`` mapping. ``agent.py`` is generated with
    the EXACT same arguments cfn_template_generator uses (portable mode), so
    the ejected source matches the CFN-embedded source byte-for-byte.
    """
    config = deploy_request.config
    connected_tools = deploy_request.connected_tools or []
    custom_tools = deploy_request.custom_tools or []

    agent_code = generate_agent_code(
        config=config,
        tools=connected_tools,
        gateway_config=None,
        template_id=deploy_request.template_id,
        gateway_tools=deploy_request.gateway_tools or [],
        custom_tools=[
            ct.model_dump() if hasattr(ct, "model_dump") else ct for ct in custom_tools
        ],
        portable=True,
        observability_enabled=_observability_enabled(config, connected_tools),
    )

    deployment_name = _sanitize_gateway_name(config.name)

    return {
        "agent.py": agent_code,
        "requirements.txt": build_requirements(config, connected_tools),
        "Dockerfile": build_dockerfile(),
        ".env.example": build_env_example(config, connected_tools),
        "README.md": build_readme(deployment_name, config),
        "run.sh": build_run_sh(),
    }


def zip_project(files: dict, deployment_name: str) -> bytes:
    """Package the project files into a downloadable zip.

    Mirrors CfnBundle.to_zip (cfn_template_generator.py:199-217): a single
    in-memory ZIP_DEFLATED archive with every file under a
    ``{deployment_name}-python/`` prefix directory.
    """
    buf = io.BytesIO()
    prefix = f"{deployment_name}-python"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content in files.items():
            zf.writestr(f"{prefix}/{filename}", content)
    buf.seek(0)
    return buf.read()


def build_and_zip(deploy_request: DeployRequest) -> tuple:
    """Convenience: build the project and zip it.

    Returns ``(zip_bytes, deployment_name)`` for the handler to upload /
    name the artifact.
    """
    deployment_name = _sanitize_gateway_name(deploy_request.config.name)
    files = build_python_project(deploy_request)
    return zip_project(files, deployment_name), deployment_name
