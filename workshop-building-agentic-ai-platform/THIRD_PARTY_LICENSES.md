# Third-Party Licenses

This workshop ships code and configuration that references, installs, or embeds the following third-party components. Each entry lists the component, how it is consumed by the workshop, its SPDX license identifier, and the upstream source.

The workshop itself is licensed under **MIT-0** (see `LICENSE`). Third-party components listed below retain their own licenses; the workshop's license does not supersede them.

## Python packages (pip)

Installed into participant environments via `requirements.txt` under each module's source directory. All versions are **pinned to exact tested releases for supply-chain reproducibility** — the published sample resolves the same dependency set on every install.

| Package | Workshop version (pinned) | SPDX license | Upstream |
|---|---|---|---|
| `boto3` | `==1.42.87` | Apache-2.0 | https://github.com/boto/boto3 |
| `botocore` | transitive from boto3 | Apache-2.0 | https://github.com/boto/botocore |
| `requests` | `==2.33.0` | Apache-2.0 | https://github.com/psf/requests |
| `pydantic` | `==2.10.6` | MIT | https://github.com/pydantic/pydantic |
| `litellm` | `==1.84.0` | MIT | https://github.com/BerriAI/litellm |
| `strands-agents[litellm]` | `==0.1.5` (Modules 2–3 venv/IDE); `==1.32.0` (Module 4 FAST agent pattern, matching FAST v0.4.1) | Apache-2.0 | https://github.com/strands-agents/sdk-python |
| `openai` | `==2.8.0` (Module 2 step-4 SDK demo) | Apache-2.0 | https://github.com/openai/openai-python |
| `httpx` | `==0.27.0` | BSD-3-Clause | https://github.com/encode/httpx |
| `aws-cdk-lib` | `==2.130.0` | Apache-2.0 | https://github.com/aws/aws-cdk |
| `constructs` | `==10.0.0` | Apache-2.0 | https://github.com/aws/constructs |
| `jupyterlab` | optional (`pip install jupyterlab==4.3.4`) | BSD-3-Clause | https://github.com/jupyterlab/jupyterlab |

Node.js dependencies for the Module 4 FAST deploy are installed with `npm ci` against the upstream FAST v0.4.1 `package-lock.json` (exact locked versions, no range re-resolution).

## Docker images

Pulled at workshop deploy time by ECS/Fargate task definitions.

| Image | Workshop version | SPDX license | Upstream |
|---|---|---|---|
| `docker.litellm.ai/berriai/litellm-database` | pinned by `LiteLLMImageTag` parameter | MIT | https://github.com/BerriAI/litellm |
| Grafana OSS (mirrored to workshop ECR with baked-in dashboards) | `mcpgateway/grafana:v1.0.16` (pinned) | AGPL-3.0 | https://github.com/grafana/grafana |
| PostgreSQL (official `postgres:*` image, used as LiteLLM metadata DB side-car where applicable) | upstream pin | PostgreSQL License | https://github.com/docker-library/postgres |
| ADOT Collector (AWS-maintained OpenTelemetry distribution used by the observability stack) | pinned by ECR image URI | Apache-2.0 | https://github.com/aws-observability/aws-otel-collector |

**Note on Grafana AGPL-3.0**: Grafana OSS is licensed under AGPL-3.0. The workshop runs Grafana as an internal-only dashboard inside the participant's sandbox account; it is not redistributed as a SaaS offering, and the baked-in dashboards (JSON configuration) do not constitute modifications to Grafana itself. Participants who adapt this pattern for production should consult Grafana's licensing guidance before offering Grafana OSS as a hosted service to third parties.

## Upstream open-source projects embedded or adapted

| Project | How the workshop uses it | SPDX license | Upstream |
|---|---|---|---|
| MCP Gateway & Registry (`agentic-community/mcp-gateway-registry`) | Module 3 deploys a fork of this project via nested CFN stacks (network, data, compute, services, observability). Workshop adds only deployment wiring and Cognito integration — no application-code fork. | Apache-2.0 | https://github.com/agentic-community/mcp-gateway-registry |
| Model Context Protocol (MCP) | Open protocol standard; the workshop uses the JSON schema and wire protocol unchanged. | MIT | https://github.com/modelcontextprotocol/specification |
| A2A Protocol | Open agent-to-agent protocol standard referenced in Module 1 content. | Apache-2.0 | https://github.com/a2aproject/A2A |

## Toolchain installers (fetched at EC2 bootstrap time)

Installed inside the participant's Code Editor EC2 instance during `code-editor.yaml` bootstrap. These are the canonical upstream distribution endpoints.

| Tool | Install method | SPDX license | Upstream |
|---|---|---|---|
| Node.js 20.x | `curl -fsSL https://rpm.nodesource.com/setup_20.x \| bash` then `dnf install nodejs` | MIT (Node.js core); NodeSource installer script is also MIT | https://github.com/nodejs/node |
| `uv` | `curl -fsSL https://astral.sh/uv/install.sh \| sh` | MIT OR Apache-2.0 (dual-licensed) | https://github.com/astral-sh/uv |
| Rust toolchain (via `rustup`) | `curl -fsSL https://sh.rustup.rs \| sh` | MIT OR Apache-2.0 (dual-licensed) | https://github.com/rust-lang/rustup |

These installers are fetched over HTTPS from their canonical upstream endpoints at bootstrap time; the `uv` installer is additionally checksum-verified against a pinned SHA-256 in `code-editor.yaml`.

## AWS SDKs and service integrations (not third-party, listed for completeness)

| Component | SPDX license | Upstream |
|---|---|---|
| AWS SDK for Python (`boto3`, `botocore`) | Apache-2.0 | https://github.com/boto/boto3 |
| AWS Cloud Development Kit (`aws-cdk-lib`) | Apache-2.0 | https://github.com/aws/aws-cdk |
| AWS CLI v2 | Apache-2.0 | https://github.com/aws/aws-cli |
| AWS Lambda runtime (built-in boto3 in `python3.12`) | Apache-2.0 | https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html |

## Verification

To regenerate this inventory after dependency changes:

```bash
# Python packages per module
find source -name "requirements.txt" -exec cat {} +

# Docker images in CFN templates
grep -rh "Image:\|image:" static/cfn/ | grep -v "^#"

# curl|bash installers in code-editor.yaml
grep -n "curl.*install" static/cfn/code-editor.yaml
```

Update this file whenever a new dependency is added or a pinned version changes.

---

**Last updated**: 2026-06-10 (versions pinned to exact tested releases for supply-chain reproducibility)
**Source of truth**: upstream package manifests (`requirements.txt`, CFN `Image:` parameters, `code-editor.yaml` bootstrap SSM document).
