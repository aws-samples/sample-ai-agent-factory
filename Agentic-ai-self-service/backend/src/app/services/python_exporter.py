"""Python project exporter — "eject" a standalone, runnable agent project.

Phase 3 Gap 3G. Builds a self-contained Python project from any canvas that a
user can run locally or in their own infrastructure, mirroring the existing
CloudFormation-template export (cfn_template_generator.py) in shape but
targeting a plain ``python agent.py`` / Docker workflow instead of CFN.

The bundle contains:
- agent.py — the SAME generated agent source the CFN exporter embeds
  (code_generator.generate_agent_code in portable mode).
- requirements.txt / requirements.lock — exact direct roots constrained by the
  accepted complete runtime lock, plus a fail-closed installed-closure verifier.
- Dockerfile — an immutable-digest Python 3.13 official base.
- .dockerignore — deny-by-default; only the Dockerfile, agent, locks, and verifiers enter.
- .env.example — blank placeholders for the env-driven config (no secrets).
- README.md — local + Docker run instructions.
- run.sh / run-docker.sh — launchers; Docker publishes only on host loopback.
- artifact-manifest.json / SHA256SUMS / verify-artifact.py — canonical integrity
  metadata and a dependency-free safe extractor.
- .github/, buildspec.yml, and ci/ — inert integrity-first CI/CD examples.

This module is PURE: no AWS, no FastAPI. The deployment_handler endpoint owns
S3 upload / presigning / owner-stamping. Keeping it pure also makes it trivial
to unit-test without moto.
"""

import base64
import hashlib
import re
import shlex
import zlib

from app.models.deployment_models import DeployRequest, RuntimeConfig, validate_export_references
from app.services.cfn_template_generator import (
    _entrypoint_integrity_verifier_source,
    _sanitize_gateway_name,
)
from app.services.code_generator import generate_agent_code, provider_strands_extras_for
from app.services.downloadable_artifact import build_artifact_zip
from app.services.litellm_gateway_deployer import resolve_mcp_url
from app.services.region_models import to_regional_model_id
from app.services.runtime_deployer import canvas_model_providers

# Runtime SDK roots imported by every conversational generated agent. Provider
# dependencies come exclusively from the exact Strands extras closure below.
_RUNTIME_PACKAGES = ["bedrock-agentcore", "boto3"]

# Added on top of the runtime packages when observability/OTEL is enabled. The
# generated OTEL bootstrap relies on the AWS OpenTelemetry distro.
_OBSERVABILITY_PACKAGES = ["aws-opentelemetry-distro"]

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


# The deployed API Lambda intentionally excludes the build-time constraints file,
# so the exporter carries an integrity-bound canonical snapshot of its exact pins.
# ``test_python_exporter.py`` compares this snapshot to the committed
# agentcore-deps-constraints.txt on every change.  The artifact deliberately makes
# no wheel-hash claim: exact versions are constrained and the post-install verifier
# rejects every missing, unexpected, or version-drifted distribution.
_ACCEPTED_LOCK_SHA256 = "b497f4f8ef6df8fa711022f16ee26a29a393a8e35213de349415638815e8e165"  # pragma: allowlist secret
_ACCEPTED_LOCK_B85 = b"c-oa%-I}8)5WV+Z^oyFL(-(Odf@tWV(14wGo_<diCX<=%?2<bv4%9DDN1^RmIJc-porE#rcx<Y-sxAcNF}mZit?F&HrE$>1B+T(xSKDe+h-tD>MAemXaSH1;)q%zF?5)$11a-4x(W33~xUK3PZ5#&eRnHqYG>*}e)_qkI_8Q8Q>b#YgGTMup%0W%ai)cM5zCo;0Q&dKcD*C1Dbco)P!sZ6>>T^~hhEmM&Of%c6Elvs}-Onc2*=<*gHpNY!fT}31SEQ+ddBw>_+m<2Dr@(GQwmBr}ef-X2TJ{HU|8|!@K&6mp6|JQ>@2duJmCC~;jR0&Ag14$^3fc1*&enxI3m;T0C+kOH^e^QpaJvd9qvhpzJXGX@^oxsj@WP#!<8imC5Q?JH#w6G4;ufO1dI!9-5<@)=rK9aV=ZdnYzJ%d?mM0-E&ia@(*c82hceCyo$^vbav19^9NM+G``%6s*ibxIZ)`#GfTwNo*gSCTEYfhSKzxgdg>;-$>9PUElbSR}AM?00yBbprCw_UX>2JfV1e-32Kxvtr;J$<8WZ(-k5?>zo4Lr}LeU;$Ul0g*Y+!+=QiLaOpSpGe*!Uxn@_L9uQ&g&rvqMxRR5o9ILo6nApyLr))~;aaZt7{xV~Z!|Zm-~@S^>h>epiN>W{;@M6~yk~JhRg9vfXrUIQh#E4E+YV~#2Mk5KAukPd88dQ5YXrm?fgKRB5`}>}CNl^(3V-SBbt-j)VqX3r)&0W7j;N4mc0u%t+(D#@Q97DzDNAEFdoCt`i;;>+n3S&?vSM`O>S;^h*+kuG8L7z0{&8_jxgI8*vQb==Fa114J>E{o{!m!<AsOBlbi|W}9cB2>6AwRm-M?pk-v#^K>F-JPO^JkV8;wgPwbs{@-F{j?rS}3n-J(Cj^#ohz5hBibi95D*`XZiK{{x&he@uARqKoKX(cy+%EN=E}=rYOtjd7u)G)`Tv)m&=fGrRy#H%1m*iV(!q`CMxxX8e*1wo`9r49i#2)SdW5<Y7O=G}ugVFN~zqkqHhl-;UmhaLL4`GnWOu-|iXM+x49V=6u{Q@2n-p^4pS$9+&WGUctU}@L#xCTv#NIeQsdfGOCd~oBRbfzXXAPAuw+7VoYsH2HGpU7und+&PH@98=qRxOgLP`S;HH@<S#%E(B`6}@J7X$YkgQkR3kM!Udvn`(E2OU<ySnq`bl&)A04d@3axPF3iK@K+S-@Unc!>oWA_Aw?m(&#tVgRo4CsoU<D3|X0P#Ec?ZuKh+vYj&)IRAr9xxw54a)yN&c+hWp8OB<qJ6SU=q_l9qME|`iYHd{1MJxbXcf^UN_rK)Fp=8gAMF6KGi$H4&O0y|k*?o{8cGK`WUtWFa0?8>M+e_P#x6Rho_E)h*xm249lBt4@HF?-%eJ6ig$tzXcm!8N4%{NbRF|XeW|LU(_AX7WEVfwWjb~AN?3V8474;lofqUBaCI#J#usQ{6_kd>sTd)jNU5sGH>z0peT=8|0o~RN$MI}oc;wqK1A#U!I7HRoPz5n(m<h~u2@{<A@M1Cw{G~_yl1!%kSYLe>#Z;9o2RMGdH;x+HZ?`sdNf?|RDgO~w4A-L-T^N@8xU4$0pHHCg<PS~Re`kUDi?H>%%hz`xfI~PeJu9~*mQiKACOrPi=(RQNuL8~v7_(=io_jv!cOqm}9;FK(Y9k7mn1B7yA>GW6Q{jWJ%u_JO7meKKPWJZjL3aKLm?@W`TzVI&$_KVWQBjWJ9Z_^Uyvm1$&y%CGzA1UQu0U<q@AhLcxOM(AgJj~$A0e8IJHuS&de)Tm>#pf@LYU5d^Q&$n?xwAt`EynVyyY#1oGH_L=0B*0?*FFD<xO(BZ#wd7;@cbjFp?dqJonwRW{SOgnKcx"


def accepted_lock_text() -> str:
    """Return the exact canonical lock, refusing a corrupt embedded snapshot."""
    try:
        lock = zlib.decompress(base64.b85decode(_ACCEPTED_LOCK_B85)).decode("utf-8")
    except (ValueError, zlib.error, UnicodeDecodeError) as exc:
        raise RuntimeError("The embedded runtime dependency lock is corrupt") from exc
    if hashlib.sha256(lock.encode("utf-8")).hexdigest() != _ACCEPTED_LOCK_SHA256:
        raise RuntimeError("The embedded runtime dependency lock failed its SHA-256 check")
    return lock


def _normalized_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_versions() -> dict[str, tuple[str, str]]:
    pins: dict[str, tuple[str, str]] = {}
    for line in accepted_lock_text().splitlines():
        name, separator, version = line.partition("==")
        normalized = _normalized_package(name)
        if separator != "==" or not name or not version or normalized in pins:
            raise RuntimeError(f"The accepted runtime lock has an invalid or duplicate pin: {line!r}")
        pins[normalized] = (name, version)
    return pins


_REQUIREMENTS_HEADER = (
    "# Exact direct requirements constrained by requirements.lock.\n"
    f"# accepted-lock-sha256: {_ACCEPTED_LOCK_SHA256}\n"
    "# The source lock contains exact versions, not distribution-file hashes.\n"
    "# install-locked.sh verifies the complete installed closure and fails closed.\n"
)


def build_requirements(
    config: RuntimeConfig,
    connected_tools=None,
    *,
    template_id: str | None = None,
) -> str:
    """Build exact direct roots for the parent and every multi-agent provider."""
    if template_id == "mcp-server-runtime":
        packages = {"bedrock-agentcore", "mcp"}
        extras: list[str] = []
    else:
        extras = provider_strands_extras_for(canvas_model_providers(config))
        packages = {"bedrock-agentcore", "boto3", "strands-agents-tools"}
        if _observability_enabled(config, connected_tools or []):
            packages.update(_OBSERVABILITY_PACKAGES)

    lock = _locked_versions()
    exact: list[str] = []
    if template_id != "mcp-server-runtime":
        strands_pin = lock.get("strands-agents")
        if strands_pin is None:
            raise RuntimeError("strands-agents is absent from the accepted exact lock")
        extra_suffix = f"[{','.join(extras)}]" if extras else ""
        exact.append(f"{strands_pin[0]}{extra_suffix}=={strands_pin[1]}")
    for package in sorted(packages, key=_normalized_package):
        pin = lock.get(_normalized_package(package))
        if pin is None:
            raise RuntimeError(f"Python export dependency is absent from the accepted exact lock: {package}")
        exact.append(f"{pin[0]}=={pin[1]}")
    exact.sort(key=lambda requirement: _normalized_package(requirement.split("[", 1)[0].split("==", 1)[0]))
    return _REQUIREMENTS_HEADER + "-c requirements.lock\n" + "\n".join(exact) + "\n"


# Explicit Docker Official Image patch tag and immutable index digest verified
# against Docker Hub's tag API on 2026-10-27. Changing either is a reviewed
# dependency update; a moving ``3.13-slim`` tag is never a certified identity.
_PYTHON_IMAGE = (
    "docker.io/library/python:3.13.13-slim@sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286"
)


def build_dockerignore() -> str:
    """Deny every Docker-context path except the reviewed build inputs."""
    return """*
!.dockerignore
!Dockerfile
!agent.py
!requirements.txt
!requirements.lock
!verify-integrity.py
!verify-lock.py
!check-installed.sh
"""


def build_dockerfile() -> str:
    """Build a least-privilege image from an immutable official base."""
    return (
        "# Standalone AgentCore-compatible agent image.\n"
        f"FROM {_PYTHON_IMAGE}\n"
        "\n"
        "WORKDIR /app\n"
        "COPY requirements.txt requirements.lock verify-lock.py check-installed.sh ./\n"
        "RUN python -m pip install --disable-pip-version-check --no-cache-dir "
        "--requirement requirements.txt \\\n"
        "    && bash check-installed.sh python \\\n"
        "    && useradd --create-home --uid 10001 agent\n"
        "COPY --chown=10001:10001 agent.py ./\n"
        "USER 10001\n"
        "\n"
        "# The SDK binds 0.0.0.0 only inside the container. run-docker.sh\n"
        "# publishes it exclusively on the host loopback interface.\n"
        'ENV AWS_REGION=""\n'
        'ENV MODEL_ID=""\n'
        'ENV MCP_HOST="0.0.0.0"\n'
        'ENV PORT="8080"\n'
        'CMD ["python", "agent.py"]\n'
    )


def build_lock_verifier() -> str:
    """Dependency-free verifier for lock syntax, roots, extras, and installed closure."""
    return r"""#!/usr/bin/env python3
# Fail closed unless the selected dependency closure matches requirements.lock.
from __future__ import annotations
import argparse, importlib.metadata, re, sys
from pathlib import Path

BOOTSTRAP = {"pip", "setuptools", "wheel"}
LOCK = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)$")
ROOT = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[([A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*)\])?==([^\s;]+)$"
)

def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()

def pins(path="requirements.lock"):
    result = {}
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LOCK.fullmatch(line)
        if not match:
            raise ValueError(f"{path}:{number}: dependency is not exactly pinned")
        key = norm(match.group(1))
        if key in result:
            raise ValueError(f"{path}:{number}: duplicate normalized package {key}")
        result[key] = match.group(2)
    if not result:
        raise ValueError("requirements.lock has no pins")
    return result

def roots(path="requirements.txt"):
    result = {}
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-c "):
            continue
        match = ROOT.fullmatch(line)
        if not match:
            raise ValueError(f"{path}:{number}: direct dependency is not exactly pinned")
        key = norm(match.group(1))
        extras = match.group(2).split(",") if match.group(2) else []
        if extras != sorted(set(extras)):
            raise ValueError(f"{path}:{number}: extras must be unique and sorted")
        if key in result:
            raise ValueError(f"{path}:{number}: duplicate normalized package {key}")
        result[key] = match.group(3)
    return result

def verify_installed(lock, direct):
    installed = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            installed[norm(name)] = dist.version
    problems = []
    for name, version in sorted(installed.items()):
        if name in BOOTSTRAP:
            continue
        if name not in lock:
            problems.append(f"unexpected distribution {name}=={version}")
        elif lock[name] != version:
            problems.append(f"version drift {name}=={version}; lock requires {lock[name]}")
    for name, version in sorted(direct.items()):
        if installed.get(name) != version:
            problems.append(f"direct requirement absent or wrong: {name}=={version}")
    if problems:
        raise ValueError("; ".join(problems))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--lock-only", action="store_true")
    args = parser.parse_args()
    if args.installed == args.lock_only:
        parser.error("choose exactly one of --installed or --lock-only")
    lock = pins(); direct = roots()
    missing = sorted(name for name, version in direct.items() if lock.get(name) != version)
    if missing:
        raise ValueError(f"direct requirements drift from lock: {missing}")
    if args.installed:
        verify_installed(lock, direct)
    print(f"verified exact dependency lock ({len(lock)} accepted pins, {len(direct)} roots)")

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f"dependency verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
"""


def build_check_installed_sh() -> str:
    """Run pip's dependency oracle and exact-lock validation without leaking output."""
    return r"""#!/usr/bin/env bash
set -euo pipefail
python_bin="${1:?usage: check-installed.sh PYTHON_EXECUTABLE}"
log="$(mktemp)"
cleanup() {
  rm -f "$log"
}
trap cleanup EXIT
if ! "$python_bin" -m pip check >"$log" 2>&1; then
  "$python_bin" - "$log" <<'PY'
from pathlib import Path
import re
import sys

raw = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")[:16384]
raw = re.sub(r"https?://[^\s]+", "[url-redacted]", raw)
raw = re.sub(
    r"(?i)\b(authorization|token|password|secret|api[-_]?key)\b\s*[:=]\s*[^\s]+",
    r"\1=[redacted]",
    raw,
)
safe = "".join(character for character in raw if character in "\n\t" or character.isprintable())
print("pip dependency compatibility check failed:", file=sys.stderr)
for line in safe.splitlines()[:40]:
    print(line[:240], file=sys.stderr)
PY
  exit 1
fi
"$python_bin" verify-lock.py --installed
"""


def build_install_locked_sh() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
python_bin="${PYTHON_BIN:-python3}"
"$python_bin" -I verify-integrity.py
"$python_bin" - <<'PY'
import sys
if sys.version_info[:2] != (3, 13):
    raise SystemExit("Python 3.13 is required; set PYTHON_BIN to a Python 3.13 executable")
PY
"$python_bin" verify-lock.py --lock-only
venv="${1:-.venv}"
if [ -e "$venv" ]; then
  echo "ERROR: isolated environment already exists: $venv" >&2
  exit 2
fi
"$python_bin" -m venv "$venv"
"$venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir --requirement requirements.txt
bash check-installed.sh "$venv/bin/python"
"""


def build_runtime_bind_verifier() -> str:
    """Exercise the exact installed SDK default rather than trusting documentation."""
    return r"""#!/usr/bin/env python3
import os
from unittest import mock
from bedrock_agentcore.runtime import BedrockAgentCoreApp


def observed_host(*, in_container):
    seen = {}
    app = BedrockAgentCoreApp()
    with (
        mock.patch.dict(os.environ, {"DOCKER_CONTAINER": "1"} if in_container else {}, clear=True),
        mock.patch("os.path.exists", return_value=in_container),
        mock.patch("uvicorn.run", side_effect=lambda _app, **kwargs: seen.update(kwargs)),
    ):
        app.run()
    return seen.get("host")


if observed_host(in_container=False) != "127.0.0.1":
    raise SystemExit("locked BedrockAgentCoreApp no longer defaults to loopback outside a container")
if observed_host(in_container=True) != "0.0.0.0":
    raise SystemExit("locked BedrockAgentCoreApp no longer exposes its container-internal listener")
print("verified BedrockAgentCoreApp host defaults")
"""


def build_smoke_start() -> str:
    """Start the locked SDK on loopback and prove the listener opens, with a deadline."""
    return r"""#!/usr/bin/env python3
from __future__ import annotations
import os, socket, subprocess, sys, time

if "--serve" in sys.argv:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
    app = BedrockAgentCoreApp()
    @app.entrypoint
    def invoke(payload):
        return {"response": str(payload.get("prompt", "ok"))}
    app.run(host="127.0.0.1", port=8080)
    raise SystemExit(0)

env = {
    "PATH": os.environ.get("PATH", ""),
    "PYTHONUNBUFFERED": "1",
    "AWS_EC2_METADATA_DISABLED": "true",
}
process = subprocess.Popen(
    [sys.executable, __file__, "--serve"],
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    text=True,
)
try:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            error = process.stderr.read() if process.stderr else ""
            raise SystemExit(f"loopback smoke server exited early: {error[-1000:]}")
        try:
            with socket.create_connection(("127.0.0.1", 8080), timeout=0.25):
                print("verified loopback smoke listener on 127.0.0.1:8080")
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise SystemExit("loopback smoke server did not listen within 15 seconds")
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
"""


def _python_ci_files() -> dict[str, str]:
    verify = """#!/usr/bin/env bash
set -euo pipefail
python3 -I verify-integrity.py
python3 -I verify-artifact.py --directory . --artifact-type python
python3 verify-lock.py --lock-only
for script in run.sh run-docker.sh check-installed.sh install-locked.sh ci/verify.sh; do
  bash -n "$script"
done
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
./install-locked.sh "$work/venv"
# Repeat the compatibility oracle at the CI boundary, before compiling or
# starting any runtime process, so installer drift cannot silently remove it.
bash check-installed.sh "$work/venv/bin/python"
"$work/venv/bin/python" - <<'PY'
from pathlib import Path
for path in sorted(Path(".").rglob("*.py")):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
print("compiled every generated Python module")
PY
"$work/venv/bin/python" ci/verify-runtime-bind.py
"$work/venv/bin/python" ci/smoke-start.py
"""
    workflow = """name: validate-standalone-agent
on:
  pull_request:
  push:
  workflow_dispatch:
    inputs:
      build_container:
        description: Build the immutable-base container after verification
        required: true
        type: boolean
        default: false
permissions:
  contents: read
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: "3.13"
      - name: Verify externally anchored artifact integrity first
        env:
          EXPECTED_ARTIFACT_MANIFEST_SHA256: ${{ vars.EXPECTED_ARTIFACT_MANIFEST_SHA256 }}
        run: |
          python3 -I verify-integrity.py
          python3 -I verify-artifact.py --directory . --artifact-type python
      - name: Install exact lock, compile, and smoke-start loopback
        run: ./ci/verify.sh
      - name: Build container without publishing it
        if: ${{ github.event_name == 'workflow_dispatch' && inputs.build_container }}
        run: docker build --pull=false -t exported-agent:verified .
"""
    buildspec = """version: 0.2
env:
  variables:
    BUILD_CONTAINER: "false"
phases:
  install:
    runtime-versions:
      python: 3.13
  pre_build:
    commands:
      - python3 -I verify-integrity.py
      - python3 -I verify-artifact.py --directory . --artifact-type python
  build:
    commands:
      - ./ci/verify.sh
      - |
        if [ "$BUILD_CONTAINER" = "true" ]; then
          command -v docker >/dev/null
          docker build --pull=false -t exported-agent:verified .
        else
          echo "Container build disabled; set BUILD_CONTAINER=true explicitly"
        fi
artifacts:
  files:
    - artifact-manifest.json
    - SHA256SUMS
    - Dockerfile
"""
    guide = """# CI/CD examples

The GitHub Actions and CodeBuild examples are inert: they verify, install,
compile, smoke-start on `127.0.0.1`, and optionally build an image, but never
push or deploy it. Store the authenticated response's `artifact_manifest_sha256`
outside the checkout as `EXPECTED_ARTIFACT_MANIFEST_SHA256`; integrity fails
closed when it is absent or differs. CodeBuild requires Python 3.13 and leaves
`BUILD_CONTAINER=false` by default; its image must provide Docker only when that
explicit opt-in is enabled.

`requirements.txt` contains exact direct roots and constrains resolution with
the complete exact `requirements.lock`. The lock intentionally contains no
fabricated wheel hashes because the accepted source lock has versions only.
After resolution, `check-installed.sh` runs `python -m pip check` to reject
missing or incompatible required dependencies, then `verify-lock.py` rejects
unexpected distributions, version drift, and absent direct roots. This proves
the installed dependency graph is compatible with the exact accepted version
set; it does not claim wheel-byte reproducibility. Use
`install-locked.sh <new-venv-path>`; it refuses an existing environment.
"""
    return {
        ".github/workflows/verify.yml": workflow,
        "CI-CD.md": guide,
        "buildspec.yml": buildspec,
        "ci/smoke-start.py": build_smoke_start(),
        "ci/verify-runtime-bind.py": build_runtime_bind_verifier(),
        "ci/verify.sh": verify,
        "check-installed.sh": build_check_installed_sh(),
        "verify-integrity.py": _entrypoint_integrity_verifier_source("python"),
        "install-locked.sh": build_install_locked_sh(),
        "verify-lock.py": build_lock_verifier(),
    }


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


def build_readme(
    deployment_name: str,
    config: RuntimeConfig,
    has_memory: bool = False,
    *,
    template_id: str | None = None,
) -> str:
    """Build run instructions for the ejected project."""
    if template_id == "mcp-server-runtime":
        invoke_md = (
            "curl -s -X POST 127.0.0.1:8080/mcp \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            "  -H 'Accept: application/json, text/event-stream' \\\n"
            '  -d \'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"local-smoke","version":"1"}}}\'\n'
            "```\n\n"
            "This model-free template serves MCP on `/mcp`; it has no `/invocations` model endpoint.\n"
        )
    # A Memory agent refuses an invoke without both ids (F-56), so the documented call
    # must carry them, or following the README exactly is an error.
    elif has_memory:
        invoke_md = (
            "curl -s 127.0.0.1:8080/invocations \\\n"
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
            "curl -s 127.0.0.1:8080/invocations \\\n"
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
        "## Integrity anchor\n"
        "\n"
        "The authenticated export response includes `archive_sha256` and\n"
        "`artifact_manifest_sha256`. Keep both outside this directory, verify the ZIP\n"
        "before extraction, and export the manifest value before any entrypoint:\n"
        "\n"
        "```bash\n"
        "export EXPECTED_ARTIFACT_MANIFEST_SHA256=<artifact_manifest_sha256-from-response>\n"
        "```\n"
        "\n"
        "`run.sh`, `run-docker.sh`, `install-locked.sh`, and CI verify every declared\n"
        "file against that external anchor before side effects. Editing generated files\n"
        "invalidates the export: change the factory and re-export, or use an explicit\n"
        "reviewed release flow that regenerates metadata and replaces the externally\n"
        "stored digest. No self-resigning shortcut is included.\n"
        "\n"
        "## Run locally\n"
        "\n"
        "Python 3.13 is required. If `python3` names another version, set\n"
        "`PYTHON_BIN` to your Python 3.13 executable.\n"
        "\n"
        "```bash\n"
        "./install-locked.sh .venv\n"
        "cp .env.example .env   # then fill in the blanks\n"
        "./run.sh\n"
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
        "## Dependencies and integrity\n"
        "\n"
        "`requirements.txt` contains exact direct roots and applies the complete\n"
        "exact `requirements.lock`. `install-locked.sh` creates a new isolated\n"
        "environment, then `check-installed.sh` runs `python -m pip check` to\n"
        "reject missing or incompatible required dependencies. `verify-lock.py`\n"
        "also rejects unexpected distributions, version drift, and absent direct\n"
        f"roots. The accepted lock digest is `{_ACCEPTED_LOCK_SHA256}`. It contains\n"
        "no distribution-file hashes, so this proves exact-version compatibility,\n"
        "not wheel-byte reproducibility; mirror your package index or add reviewed\n"
        "hashes if artifact-level provenance is required.\n"
        "\n"
        "The container base is the explicit immutable identity\n"
        f"`{_PYTHON_IMAGE}`. The locked BedrockAgentCoreApp 1.23.1 default is\n"
        "verified by CI to bind\n"
        "`127.0.0.1` outside a container. Inside the container it binds on the\n"
        "container interface, while `run-docker.sh` publishes port 8080 only at\n"
        "`127.0.0.1:8080` on the host.\n"
        "\n"
        "See `CI-CD.md` for inert GitHub Actions and CodeBuild examples. Neither\n"
        "example deploys or publishes an image.\n"
    )


def build_run_sh() -> str:
    """One-command loopback launcher using only the verified isolated environment."""
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "python3 -I verify-integrity.py\n"
        "\n"
        'if [ ! -x ".venv/bin/python" ]; then\n'
        '  echo "Missing verified .venv; run ./install-locked.sh .venv first." >&2\n'
        "  exit 2\n"
        "fi\n"
        "if [ -f .env ]; then\n"
        "  set -a\n"
        "  # .env is created by the recipient at runtime.\n"
        "  # shellcheck disable=SC1091\n"
        "  . ./.env\n"
        "  set +a\n"
        "fi\n"
        "exec .venv/bin/python agent.py\n"
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
        "python3 -I verify-integrity.py\n"
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
        "# .env is created by the recipient at runtime.\n"
        "  # shellcheck disable=SC1091\n"
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
        'exec docker run --rm -p 127.0.0.1:8080:8080 "${env_args[@]}" "$image_name"\n'
    )


def _harden_standalone_source(source: str, template_id: str | None) -> str:
    """Apply local-export network defaults to the model-free FastMCP server.

    AgentCore's managed container requires the generator's 0.0.0.0:8000
    contract.  A standalone download has a different boundary: local execution
    must default to loopback, while Docker may bind internally on all interfaces
    only because run-docker.sh publishes host port 8080 on 127.0.0.1.
    """
    if template_id != "mcp-server-runtime":
        return source
    port = 'PORT = int(os.environ.get("PORT", "8000"))'
    host = '    host="0.0.0.0",'
    if source.count(port) != 1 or source.count(host) != 1:
        raise RuntimeError("Standalone MCP source no longer has the reviewed host/port contract")
    return source.replace(port, 'PORT = int(os.environ.get("PORT", "8080"))').replace(
        host,
        '    host=os.environ.get("MCP_HOST", "127.0.0.1"),',
    )


def build_python_project(deploy_request: DeployRequest) -> dict:
    """Build the full set of project files for the given deploy request.

    Returns a ``{filename: content}`` mapping. ``agent.py`` is generated with
    the EXACT same arguments cfn_template_generator uses (portable mode), so
    the ejected source matches the CFN-embedded source byte-for-byte.
    """
    validate_export_references(deploy_request)
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

    agent_code = _harden_standalone_source(agent_code, deploy_request.template_id)
    deployment_name = _sanitize_gateway_name(config.name)

    project = {
        ".dockerignore": build_dockerignore(),
        "agent.py": agent_code,
        "requirements.txt": build_requirements(config, connected_tools, template_id=deploy_request.template_id),
        "requirements.lock": accepted_lock_text(),
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
        "README.md": build_readme(
            deployment_name,
            config,
            has_memory="memory" in connected_tools,
            template_id=deploy_request.template_id,
        ),
        "run.sh": build_run_sh(),
        "run-docker.sh": build_run_docker_sh(),
    }
    project.update(_python_ci_files())
    return project


def zip_project(files: dict, deployment_name: str) -> bytes:
    """Package the project in the shared canonical artifact format."""
    executable = {
        "ci/verify.sh",
        "check-installed.sh",
        "install-locked.sh",
        "run-docker.sh",
        "run.sh",
    }
    return build_artifact_zip(
        prefix=f"{deployment_name}-python",
        artifact_type="python",
        files=files,
        executable_paths=executable,
    )


def build_and_zip(deploy_request: DeployRequest) -> tuple:
    """Convenience: build the project and zip it.

    Returns ``(zip_bytes, deployment_name)`` for the handler to upload /
    name the artifact.
    """
    deployment_name = _sanitize_gateway_name(deploy_request.config.name)
    files = build_python_project(deploy_request)
    return zip_project(files, deployment_name), deployment_name
