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
- run-docker.sh — Docker launcher that preserves shell-quoted env values.

This module is PURE: no AWS, no FastAPI. The deployment_handler endpoint owns
S3 upload / presigning / owner-stamping. Keeping it pure also makes it trivial
to unit-test without moto.
"""

import io
import shlex
import zipfile

from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import _sanitize_gateway_name
from app.services.code_generator import PROVIDER_PACKAGES, generate_agent_code
from app.services.litellm_gateway_deployer import resolve_mcp_url
from app.services.region_models import to_regional_model_id

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

_GATEWAY_TEMPLATE_IDS = {
    "strands-gateway-agent",
    "customer-support-assistant",
    "customer-support-blueprint",
    "mcp-server-gateway-target",
}

# Environment names the generated agent may consume. The Docker launcher clears
# these names before sourcing .env, then forwards only names that the file
# actually assigned. That prevents an optional value omitted from the export
# from accidentally inheriting a same-named host secret.
_STANDALONE_ENV_NAMES = (
    "MODEL_ID",
    "AWS_REGION",
    "AGENT_PROVIDER",
    "PROVIDER_API_KEY",
    "PROVIDER_API_KEY_SECRET_ARN",
    "PROVIDER_BASE_URL",
    "GATEWAY_URL",
    "GATEWAY_AUTH_MODE",
    "GATEWAY_MCP_SERVERS",
    "GATEWAY_API_KEY",
    "GATEWAY_API_KEY_SECRET_ARN",
    "COGNITO_CLIENT_ID",
    "COGNITO_USER_POOL_ID",
    "COGNITO_TOKEN_ENDPOINT",
    "COGNITO_SCOPE",
    "OAUTH_CLIENT_ID",
    "OAUTH_CLIENT_SECRET_REF",
    "OAUTH_TOKEN_ENDPOINT",
    "OAUTH_SCOPE",
    "MEMORY_ID",
    "KB_ID",
    "GUARDRAIL_ID",
    "GUARDRAIL_VERSION",
    "A2A_SELF_URL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_SERVICE_NAME",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_AUTH_SECRET_ARN",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_EXTRA_HEADERS",
)


def _env_assignment(name: str, value: object = "") -> str:
    """A shell-safe assignment for the ``run.sh``-sourced env file."""
    text = "" if value is None else str(value)
    return f"{name}={shlex.quote(text) if text else ''}"


def _config_dict(value: object | None) -> dict:
    """Return a plain mapping for raw dict and Pydantic component configs."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python", by_alias=True)
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _first_config_value(config: object | None, *keys: str, default: object = "") -> object:
    """Read the first present, non-empty alias while preserving ``False``/``0``."""
    values = _config_dict(config)
    for key in keys:
        if key in values and values[key] is not None and values[key] != "":
            return values[key]
    return default


def _scope_string(identity_config: object | None, client_info: object | None) -> str:
    """Canonical OAuth scope string from either request or deployed client info."""
    raw_scopes = _first_config_value(identity_config, "scopes", default=None)
    if isinstance(raw_scopes, set):
        raw_scopes = sorted(raw_scopes, key=str)
    if isinstance(raw_scopes, (list, tuple)):
        joined = " ".join(str(scope).strip() for scope in raw_scopes if str(scope).strip())
        if joined:
            return joined
    elif raw_scopes:
        return str(raw_scopes).strip()

    return str(
        _first_config_value(
            identity_config,
            "scope",
            default=_first_config_value(client_info, "scope"),
        )
        or ""
    ).strip()


def _effective_connected_tools(request: DeployRequest) -> list[str]:
    """Make code generation agree with the components carried by the request.

    The live and CloudFormation paths both infer these edges when a caller sends
    a component config but omits the redundant ``connectedTools`` entry. The
    Python export used the raw list, so it could ship a perfectly runnable zip
    whose agent silently omitted its Gateway, Memory, KB, or A2A integration.
    """
    tools = list(request.connected_tools or [])

    gateway_implied = bool(
        request.gateway_config is not None
        or request.gateway_tools
        or request.connectors
        or request.external_mcp_servers
        or request.mcp_server_config is not None
        or request.template_id in _GATEWAY_TEMPLATE_IDS
    )
    memory_implied = bool(
        request.memory_config is not None
        or request.template_id in {"customer-support-assistant", "customer-support-blueprint"}
    )
    implied = (
        (gateway_implied, "gateway"),
        (memory_implied, "memory"),
        (request.knowledge_base_config is not None, "knowledge_base"),
        (request.guardrails_config is not None, "guardrails"),
        (request.a2a_config is not None, "a2a"),
        (request.observability_config is not None, "observability"),
    )
    for enabled, tool_id in implied:
        if enabled and tool_id not in tools:
            tools.append(tool_id)
    return tools


def _gateway_env_lines(
    gateway_config: dict | None,
    identity_config: object | None = None,
) -> list[str]:
    """Blank-safe runtime wiring for a standalone Gateway-connected agent."""
    cfg = _config_dict(gateway_config)
    client_info = _config_dict(_first_config_value(cfg, "client_info", "clientInfo", default={}))
    identity = _config_dict(identity_config)
    client_info_provider = str(_first_config_value(client_info, "provider") or "").strip().lower()
    gateway_provider = (
        str(
            _first_config_value(
                cfg,
                "gateway_provider",
                "gatewayProvider",
                default=_first_config_value(client_info, "gateway_provider", "gatewayProvider"),
            )
            or ""
        )
        .strip()
        .lower()
    )
    idp_provider = (
        str(
            _first_config_value(
                identity,
                "provider",
                default=client_info_provider or "cognito",
            )
            or "cognito"
        )
        .strip()
        .lower()
    )
    is_litellm = (
        gateway_provider == "litellm"
        or client_info_provider == "litellm"
        or bool(_first_config_value(cfg, "litellm_base_url", "litellmBaseUrl"))
    )

    lines = [
        "",
        "# MCP Gateway connection. Secrets stay blank or travel by reference.",
    ]
    if is_litellm:
        raw_servers = _first_config_value(cfg, "litellm_servers", "litellmServers", default=[])
        if isinstance(raw_servers, str):
            servers = [value.strip() for value in raw_servers.split(",") if value.strip()]
        else:
            servers = [str(value).strip() for value in raw_servers if str(value).strip()]
        gateway_url = _first_config_value(cfg, "gateway_url", "gatewayUrl")
        if not gateway_url:
            base_url = str(_first_config_value(cfg, "litellm_base_url", "litellmBaseUrl") or "").strip()
            gateway_url = resolve_mcp_url(base_url, servers) if base_url else ""
        key_ref = (
            _first_config_value(
                cfg,
                "litellm_api_key_ref",
                "litellmApiKeyRef",
                default=_first_config_value(client_info, "api_key_ref", "apiKeyRef"),
            )
            or ""
        )
        lines.extend(
            [
                _env_assignment("GATEWAY_URL", gateway_url),
                "GATEWAY_AUTH_MODE=static_bearer",
                _env_assignment("GATEWAY_MCP_SERVERS", ",".join(servers)),
                "# Set ONE of the next two values. Never commit the plaintext key.",
                "GATEWAY_API_KEY=",
                _env_assignment("GATEWAY_API_KEY_SECRET_ARN", key_ref),
            ]
        )
        return lines

    is_external_idp = idp_provider not in {"", "cognito", "agentcore", "litellm"}
    identity_client_id = _first_config_value(identity, "client_id", "clientId")
    client_info_client_id = _first_config_value(client_info, "client_id", "clientId")
    client_secret_ref = _first_config_value(
        identity,
        "client_secret_ref",
        "clientSecretRef",
        default=_first_config_value(client_info, "client_secret_ref", "clientSecretRef"),
    )
    token_endpoint = _first_config_value(client_info, "token_endpoint", "tokenEndpoint")
    scope = _scope_string(identity, client_info)

    lines.extend(
        [
            _env_assignment("GATEWAY_URL", _first_config_value(cfg, "gateway_url", "gatewayUrl")),
            "GATEWAY_AUTH_MODE=oauth2",
            _env_assignment(
                "COGNITO_CLIENT_ID",
                "" if is_external_idp else client_info_client_id or identity_client_id,
            ),
            _env_assignment(
                "COGNITO_USER_POOL_ID",
                "" if is_external_idp else _first_config_value(client_info, "user_pool_id", "userPoolId"),
            ),
            _env_assignment(
                "COGNITO_TOKEN_ENDPOINT",
                "" if is_external_idp else token_endpoint,
            ),
            _env_assignment("COGNITO_SCOPE", "" if is_external_idp else scope),
            "# External IdP alternative. Prefer the secret reference to plaintext.",
            _env_assignment(
                "OAUTH_CLIENT_ID",
                (identity_client_id or client_info_client_id) if is_external_idp else "",
            ),
            _env_assignment("OAUTH_CLIENT_SECRET_REF", client_secret_ref if is_external_idp else ""),
            _env_assignment("OAUTH_TOKEN_ENDPOINT", token_endpoint if is_external_idp else ""),
            _env_assignment("OAUTH_SCOPE", scope if is_external_idp else ""),
        ]
    )
    return lines


def _observability_env_lines(
    observability_config: object | None,
    *,
    runtime_name: str,
) -> list[str]:
    """Standalone OTEL settings, preserving references but never header values."""
    obs = _config_dict(observability_config)
    endpoint = _first_config_value(obs, "otlp_endpoint", "otlpEndpoint")
    protocol = _first_config_value(obs, "otlp_protocol", "otlpProtocol", default="http/protobuf")
    service_name = _first_config_value(obs, "service_name", "serviceName", default=runtime_name)
    sample_rate = _first_config_value(obs, "sample_rate", "sampleRate", default=1.0)
    resource_attributes = _first_config_value(
        obs,
        "resource_attributes",
        "resourceAttributes",
        default={},
    )
    if isinstance(resource_attributes, dict):
        resource_value = ",".join(
            f"{key}={value}" for key, value in resource_attributes.items() if value is not None and str(value)
        )
    else:
        resource_value = ""
    auth_secret_ref = _first_config_value(
        obs,
        "auth_header_secret_arn",
        "authHeaderSecretArn",
    )

    return [
        "",
        "# OpenTelemetry / OTLP export (observability enabled on this canvas).",
        _env_assignment("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint),
        _env_assignment("OTEL_EXPORTER_OTLP_PROTOCOL", protocol),
        _env_assignment("OTEL_SERVICE_NAME", service_name),
        "OTEL_TRACES_SAMPLER=parentbased_traceidratio",
        _env_assignment("OTEL_TRACES_SAMPLER_ARG", sample_rate),
        _env_assignment("OTEL_RESOURCE_ATTRIBUTES", resource_value),
        _env_assignment("OTEL_AUTH_SECRET_ARN", auth_secret_ref),
        "# Plaintext auth and extra-header values are deliberately not exported.",
        "OTEL_EXPORTER_OTLP_HEADERS=",
        "OTEL_EXPORTER_OTLP_EXTRA_HEADERS=",
    ]


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
        'ENV AWS_REGION=""\n'
        'ENV MODEL_ID=""\n'
        "\n"
        'CMD ["python", "agent.py"]\n'
    )


def build_env_example(
    config: RuntimeConfig,
    connected_tools=None,
    gateway_config: dict | None = None,
    template_id: str | None = None,
    identity_config: object | None = None,
    memory_config: dict | None = None,
    knowledge_base_config: dict | None = None,
    guardrails_config: dict | None = None,
    observability_config: object | None = None,
) -> str:
    """Build a shell-safe .env.example that never contains secret values.

    Non-secret canvas values (model id, provider base URL, gateway URL, public
    client id, secret ARN) may be prefilled. Every plaintext credential stays
    blank, and all prefilled values are shell-quoted because ``run.sh`` sources
    this file directly.
    """
    provider = getattr(config, "model_provider", "bedrock") or "bedrock"
    model_id = ""
    if isinstance(config.model, dict):
        model_id = config.model.get("modelId") or config.model.get("model_id") or ""
    if model_id and provider == "bedrock":
        # This value is what the user copies into .env and runs with, so it has
        # to name a profile that exists where they are: a stored `us.` ID is not
        # resolvable in eu-central-1. Bedrock only — an OpenAI/Anthropic-direct
        # model name must never gain a geography prefix.
        model_id = to_regional_model_id(model_id)

    lines = [
        "# Standalone agent configuration. Copy to .env and fill in.",
        "# SECRETS (API keys, tokens) come from your environment / secrets",
        "# manager at run time — never commit real values to this file.",
        "",
        _env_assignment("MODEL_ID", model_id),
        "AWS_REGION=",
        _env_assignment("AGENT_PROVIDER", provider),
    ]

    if provider != "bedrock":
        # Blank placeholder for the provider API key. The platform stores the
        # real value in Secrets Manager (provider_api_key_ref). Preserve that
        # non-secret reference so the exported agent can use the same credential
        # without copying its plaintext value.
        lines.append("")
        lines.append("# Provider API key — set this in your environment, do not commit it.")
        lines.append("PROVIDER_API_KEY=")
        lines.append("# Or grant AWS credentials access to this one Secrets Manager ARN.")
        lines.append(
            _env_assignment(
                "PROVIDER_API_KEY_SECRET_ARN",
                getattr(config, "provider_api_key_ref", None),
            )
        )
        if provider in {"openai", "litellm"} or getattr(config, "provider_base_url", None):
            lines.append(_env_assignment("PROVIDER_BASE_URL", getattr(config, "provider_base_url", None)))

    has_gateway = (
        "gateway" in (connected_tools or []) or gateway_config is not None or template_id in _GATEWAY_TEMPLATE_IDS
    )
    if has_gateway:
        lines.extend(_gateway_env_lines(gateway_config, identity_config))

    if "memory" in (connected_tools or []):
        memory_id = _first_config_value(memory_config, "memoryId", "memory_id", "id")
        lines.extend(
            [
                "",
                "# Existing AgentCore Memory identifier. A memory name is not an ID.",
                _env_assignment("MEMORY_ID", memory_id),
            ]
        )

    if {"knowledge_base", "knowledgeBase"} & set(connected_tools or []):
        kb_mode = str(_first_config_value(knowledge_base_config, "kbMode", "kb_mode", default="")).lower()
        kb_id = _first_config_value(
            knowledge_base_config,
            "knowledgeBaseId",
            "knowledge_base_id",
        )
        if kb_mode == "create_new":
            kb_id = ""
        lines.extend(
            [
                "",
                "# Existing Bedrock Knowledge Base identifier.",
                _env_assignment("KB_ID", kb_id),
            ]
        )

    if "guardrails" in (connected_tools or []):
        guardrail_mode = str(_first_config_value(guardrails_config, "mode", default="")).lower()
        guardrail_id = _first_config_value(
            guardrails_config,
            "guardrailId",
            "guardrail_id",
        )
        if guardrail_mode == "create_new":
            guardrail_id = ""
        guardrail_version = (
            _first_config_value(
                guardrails_config,
                "guardrailVersion",
                "guardrail_version",
                default="DRAFT",
            )
            if guardrail_id
            else ""
        )
        lines.extend(
            [
                "",
                "# Existing Bedrock Guardrail identifier and deployed version.",
                _env_assignment("GUARDRAIL_ID", guardrail_id),
                _env_assignment("GUARDRAIL_VERSION", guardrail_version),
            ]
        )

    if "a2a" in (connected_tools or []) or getattr(config, "protocol", "HTTP") == "A2A":
        lines.extend(
            [
                "",
                "# Public HTTPS base URL advertised in the A2A agent card.",
                "A2A_SELF_URL=",
            ]
        )

    if _observability_enabled(config, connected_tools or []):
        lines.extend(
            _observability_env_lines(
                observability_config or getattr(config, "observability", None),
                runtime_name=config.name,
            )
        )

    return "\n".join(lines) + "\n"


def build_readme(deployment_name: str, config: RuntimeConfig, has_memory: bool = False) -> str:
    """Build run instructions for the ejected project."""
    # A Memory agent refuses an invoke without both ids (F-56), so the documented call
    # must carry them, or following the README exactly is an error.
    if has_memory:
        invoke_md = (
            "curl -s localhost:8080/invocations \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            '  -d \'{"prompt": "hello", "session_id": "' + "0" * 32 + '-local-session", "actor_id": "end-user-42"}\'\n'
            "```\n"
            "\n"
            "This agent has Memory, so every invoke must carry `session_id` and\n"
            "`actor_id`; one without both is refused rather than answered. Memory is\n"
            "keyed by that pair: give each of your end users their own stable\n"
            "`actor_id`, never a shared one, or every user reads every other user's\n"
            "conversation. The session must be 33 to 100 characters of letters,\n"
            "digits, `-` and `_`; reuse it to continue the same conversation.\n"
        )
    else:
        invoke_md = (
            "curl -s localhost:8080/invocations \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            '  -d \'{"prompt": "hello"}\'\n'
            "```\n"
        )
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
        "```bash\n" + invoke_md + "\n"
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
        "cp .env.example .env   # then fill in the blanks\n"
        "./run-docker.sh my-agent\n"
        "```\n"
        "\n"
        "The launcher builds the image, sources the shell-safe `.env`, and\n"
        "forwards only the agent's allowlisted variables. Do not replace it with\n"
        "`docker run --env-file .env`: Docker does not parse shell quoting the\n"
        "same way as `run.sh`, so a quoted value can reach the container with\n"
        "literal quote marks.\n"
        "\n"
        "## Configuration & secrets\n"
        "\n"
        "All configuration is supplied via environment variables (see\n"
        "`.env.example`). **Secrets — provider API keys, OTLP auth headers — are\n"
        "never written to the exported files.** Provide them through your own\n"
        "environment or secrets manager at run time.\n"
        "\n"
        "## Scope of this export\n"
        "\n"
        "This ZIP exports the executable **agent process**, not the AWS\n"
        "infrastructure represented by the whole canvas. It does not create or\n"
        "update AgentCore Gateway, Memory, Knowledge Base, Guardrail, a separate\n"
        "MCP-server runtime, Evaluation, or Policy resources. When the agent uses\n"
        "one of those components, point `.env` at an existing resource or deploy\n"
        "that resource separately. Resource tags, naming profiles, VPC/target\n"
        "account settings, and retention policy are deployment controls and are\n"
        "therefore not reproduced by this local/Docker bundle.\n"
        "\n"
        "For an external OAuth identity, the client ID, secret reference, and\n"
        "scopes are preserved in `.env.example`. `OAUTH_TOKEN_ENDPOINT` remains\n"
        "blank unless it was already supplied in deployed gateway client info:\n"
        "this pure exporter deliberately does not fetch an OIDC discovery URL.\n"
        "\n"
        "## Dependencies\n"
        "\n"
        "`requirements.txt` lists the packages this agent imports with minimum\n"
        "version floors. For reproducible production builds, pin exact versions\n"
        "that you have tested.\n"
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


def build_run_docker_sh() -> str:
    """Build and run the image without handing shell quotes to ``--env-file``.

    ``.env.example`` is valid shell syntax because ``run.sh`` sources it and
    customer-controlled values must not execute. Docker's plain ``--env-file``
    parser is not a shell parser, so quoted values can arrive with their quote
    marks intact. Source once in Bash, then use ``--env NAME`` to pass each
    allowlisted value byte-for-byte from the process environment.
    """
    names = " ".join(_STANDALONE_ENV_NAMES)
    return (
        "#!/usr/bin/env bash\n"
        "# Docker launcher for the ejected agent.\n"
        "set -euo pipefail\n"
        "\n"
        'if [ ! -f ".env" ]; then\n'
        '  echo "Missing .env. Copy .env.example to .env and fill in the required values." >&2\n'
        "  exit 1\n"
        "fi\n"
        "\n"
        'image_name="${1:-my-agent}"\n'
        'docker build -t "$image_name" .\n'
        "\n"
        f"env_names=({names})\n"
        'for name in "${env_names[@]}"; do\n'
        '  unset "$name" 2>/dev/null || true\n'
        "done\n"
        "\n"
        "set -a\n"
        ". ./.env\n"
        "set +a\n"
        "\n"
        "env_args=()\n"
        'for name in "${env_names[@]}"; do\n'
        '  if declare -p "$name" >/dev/null 2>&1; then\n'
        '    env_args+=(--env "$name")\n'
        "  fi\n"
        "done\n"
        "\n"
        'exec docker run --rm -p 8080:8080 "${env_args[@]}" "$image_name"\n'
    )


def build_python_project(deploy_request: DeployRequest) -> dict:
    """Build the full set of project files for the given deploy request.

    Returns a ``{filename: content}`` mapping. ``agent.py`` is generated with
    the EXACT same arguments cfn_template_generator uses (portable mode), so
    the ejected source matches the CFN-embedded source byte-for-byte.
    """
    config = deploy_request.config
    connected_tools = _effective_connected_tools(deploy_request)
    custom_tools = deploy_request.custom_tools or []

    agent_code = generate_agent_code(
        config=config,
        tools=connected_tools,
        gateway_config=None,
        template_id=deploy_request.template_id,
        gateway_tools=deploy_request.gateway_tools or [],
        custom_tools=[ct.model_dump() if hasattr(ct, "model_dump") else ct for ct in custom_tools],
        portable=True,
        observability_enabled=_observability_enabled(config, connected_tools),
        kb_config=deploy_request.knowledge_base_config,
        a2a_config=deploy_request.a2a_config,
    )

    deployment_name = _sanitize_gateway_name(config.name)

    return {
        "agent.py": agent_code,
        "requirements.txt": build_requirements(config, connected_tools),
        "Dockerfile": build_dockerfile(),
        ".env.example": build_env_example(
            config,
            connected_tools,
            gateway_config=deploy_request.gateway_config,
            template_id=deploy_request.template_id,
            identity_config=deploy_request.identity_config,
            memory_config=deploy_request.memory_config,
            knowledge_base_config=deploy_request.knowledge_base_config,
            guardrails_config=deploy_request.guardrails_config,
            observability_config=deploy_request.observability_config,
        ),
        "README.md": build_readme(deployment_name, config, has_memory="memory" in connected_tools),
        "run.sh": build_run_sh(),
        "run-docker.sh": build_run_docker_sh(),
    }


def zip_project(files: dict, deployment_name: str) -> bytes:
    """Package the project files into a downloadable zip.

    Mirrors CfnBundle.to_zip (cfn_template_generator.py:199-217): a single
    in-memory ZIP_DEFLATED archive with every file under a
    ``{deployment_name}-python/`` prefix directory.
    """
    buf = io.BytesIO()
    prefix = f"{deployment_name}-python"
    with zipfile.ZipFile(buf, "w") as zf:
        for filename, content in files.items():
            info = zipfile.ZipInfo(f"{prefix}/{filename}")
            info.create_system = 3  # Unix permission bits in external_attr.
            mode = 0o100755 if filename in {"run.sh", "run-docker.sh"} else 0o100644
            info.external_attr = mode << 16
            zf.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)
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
