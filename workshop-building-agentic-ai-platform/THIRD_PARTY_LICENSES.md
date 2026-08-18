# Third-party licenses

This workshop ships code and configuration that references, installs, or embeds the following third-party components. Each entry lists the component, how it is consumed by the workshop, its SPDX license identifier, and the upstream source.

The workshop itself is licensed under **MIT-0** (see `LICENSE`). Third-party components listed below retain their own licenses; the workshop's license does not supersede them.

## Python packages (pip)

Installed into participant environments via `requirements.txt` under each module's source directory. All versions are **pinned to exact tested releases for supply-chain reproducibility** — the published sample resolves the same dependency set on every install.

| Package | Workshop version (pinned) | SPDX license | Upstream |
|---|---|---|---|
| `boto3` | `==1.43.72` | Apache-2.0 | https://github.com/boto/boto3 |
| `botocore` | `==1.43.72` | Apache-2.0 | https://github.com/boto/botocore |
| `requests` | `==2.33.0` | Apache-2.0 | https://github.com/psf/requests |
| `pydantic` | `==2.13.4` | MIT | https://github.com/pydantic/pydantic |
| `litellm` | `==1.84.0` (Modules 2–3 IDE kernel); `==1.83.0` (Module 4 FAST agent pattern) | MIT | https://github.com/BerriAI/litellm |
| `strands-agents` | `[litellm,openai]==1.52.0` (Modules 2–3 IDE kernel); `[litellm]==1.32.0` (Module 4 FAST agent pattern, matching FAST v0.4.1) | Apache-2.0 | https://github.com/strands-agents/sdk-python |
| `openai` | `==2.54.0` | Apache-2.0 | https://github.com/openai/openai-python |
| `httpx` | `==0.28.1` | BSD-3-Clause | https://github.com/encode/httpx |
| `aws-cdk-lib` | `==2.265.0` | Apache-2.0 | https://github.com/aws/aws-cdk |
| `constructs` | `==10.8.1` | Apache-2.0 | https://github.com/aws/constructs |
| `bedrock-agentcore` | `==1.4.7` (Module 4 FAST agent pattern) | Apache-2.0 | https://github.com/aws/bedrock-agentcore-sdk-python |
| `mcp` | `==1.26.0` (Module 4 FAST agent pattern) | MIT | https://github.com/modelcontextprotocol/python-sdk |
| `PyJWT[crypto]` | `==2.12.1` (Module 4 FAST agent pattern) | MIT | https://github.com/jpadilla/pyjwt |

The four `bedrock-agentcore` / `mcp` / `PyJWT` / `strands-agents` pins marked
"Module 4 FAST agent pattern" come from upstream FAST v0.4.1's own
`patterns/strands-single-agent/requirements.txt`, which the participant copies —
they are not set by this repo. `litellm` is the one line Module 4 content appends.

Jupyter toolchain installed into the Code Editor IDE kernel by `code-editor.yaml`:

| Package | Workshop version (pinned) | SPDX license | Upstream |
|---|---|---|---|
| `jupyter` | `==1.0.0` | BSD-3-Clause | https://github.com/jupyter/jupyter |
| `ipykernel` | `==6.29.4` | BSD-3-Clause | https://github.com/ipython/ipykernel |
| `nbformat` | `==5.10.4` | BSD-3-Clause | https://github.com/jupyter/nbformat |
| `nbconvert` | `==7.16.4` | BSD-3-Clause | https://github.com/jupyter/nbconvert |
| `pyyaml` | `==6.0.3` | MIT | https://github.com/yaml/pyyaml |
| `pip` | `==24.0` | MIT | https://github.com/pypa/pip |

Test-only (not installed in participant environments; `requirements-dev.txt`):

| Package | Workshop version (pinned) | SPDX license | Upstream |
|---|---|---|---|
| `pytest` | `==9.0.3` | MIT | https://github.com/pytest-dev/pytest |
| `responses` | `==0.25.3` | Apache-2.0 | https://github.com/getsentry/responses |
| `cfn-lint` | `==1.55.1` | MIT-0 | https://github.com/aws-cloudformation/cfn-lint |

Node.js dependencies for the Module 4 FAST deploy are installed with `npm ci` against the upstream FAST v0.4.1 `package-lock.json` (exact locked versions, no range re-resolution).

## Docker images

Pulled at workshop deploy time by ECS/Fargate task definitions.

| Image | Workshop version | SPDX license | Upstream |
|---|---|---|---|
| `docker.litellm.ai/berriai/litellm-database` | `v1.84.0` (`LiteLLMImageTag` default) | MIT | https://github.com/BerriAI/litellm |
| Grafana OSS (mirrored to workshop ECR with baked-in dashboards) | `mcpgateway/grafana:v1.0.16` (pinned) | AGPL-3.0 | https://github.com/grafana/grafana |
| PostgreSQL (official `postgres` image, LiteLLM metadata DB sidecar) | `16.7` (`PostgresImageTag` default) | PostgreSQL License | https://github.com/docker-library/postgres |
| ADOT Collector (AWS-maintained OpenTelemetry distribution used by the observability stack) | `v0.43.3` | Apache-2.0 | https://github.com/aws-observability/aws-otel-collector |

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
| Node.js 22.x | Amazon Linux: `curl -fsSL https://rpm.nodesource.com/setup_22.x` then `dnf install nodejs`. Debian/Ubuntu: the NodeSource `node_22.x` apt repository | MIT (Node.js core); NodeSource installer script is also MIT | https://github.com/nodejs/node |
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
# Python packages per module (and the test-only manifest)
find source -name "requirements*.txt" -exec cat {} +

# The IDE kernel pin block (jupyter toolchain + module 2/3 libraries)
grep -n -A6 "python3.13 -m pip install --user" static/cfn/code-editor.yaml

# Docker images and their pinned tags in CFN
grep -rhE "Image:|image:|ImageTag:" static/cfn/ | grep -v "^#"
grep -rn -A3 "ImageTag:" static/cfn/llm-gateway/workshop-llm-gateway-stack.yaml

# Versions the Module 4 content appends on top of upstream FAST
grep -n "requirements.txt" content/module-4/deploy/index.en.md

# curl|bash installers in code-editor.yaml
grep -n "curl.*install" static/cfn/code-editor.yaml
```

Update this file whenever a new dependency is added or a pinned version changes.

---

**Last updated**: 2026-08-15 (versions pinned to exact tested releases for supply-chain reproducibility; table reconciled against every manifest in the repo)
**Source of truth**: upstream package manifests (`requirements.txt`, CFN `Image:` parameters, `code-editor.yaml` bootstrap SSM document).
