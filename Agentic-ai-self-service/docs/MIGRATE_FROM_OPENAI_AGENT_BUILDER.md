# Migrate from OpenAI Agent Builder

OpenAI deprecated Agent Builder on 2026-06-03 and shuts it down on **2026-11-30**. The only
export is *Code → Agents SDK* (Python or TypeScript): a code export of the workflow, not a graph
you can re-import anywhere. This guide takes that Python export and makes it a governed AgentCore
Runtime under this platform in one command, without editing the exported code.

Source for the dates and the export format: OpenAI's
[Migrate from Agent Builder](https://developers.openai.com/api/docs/guides/agent-builder/migrate-from-agent-builder),
[Node reference](https://developers.openai.com/api/docs/guides/agent-builder/node-reference) and
[Deprecations](https://developers.openai.com/api/docs/deprecations) pages (read 2026-11-12).

The tool is `scripts/import/openai_agents_sdk.py`. It runs with the backend virtualenv and needs
[`uv`](https://docs.astral.sh/uv/) on the PATH for the dependency step.

## What you get

| Before (Agent Builder) | After (this platform) |
|---|---|
| Runs on OpenAI's infrastructure | Runs as an AgentCore Runtime in your AWS account, invoked only through IAM (SigV4) |
| OpenAI API key in OpenAI's hands | Either your key read once per cold start from Secrets Manager, or **no OpenAI key at all**: the export runs on OpenAI GPT models served by Amazon Bedrock, authenticated with a short-term Bedrock API key minted per invocation from the runtime's own IAM role |
| Traces to OpenAI | Tracing to OpenAI off in Bedrock mode; CloudWatch logs (`/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`) and X-Ray from the runtime's least-privilege role |
| No owner, no tenant isolation | Adopted as a caller-owned deployment: `POST /api/test-runtime` answers 404 to anyone but the owner, 401 anonymously; the runtime appears in `GET /api/deployments` |
| Tool calls invisible to the caller | Every function-tool call returns a receipt (`name`, `status`, SHA-256 digests of the arguments; never the values) beside the answer |
| A tripped guardrail is an exception | A tripped Agents SDK guardrail is a clean refusal: `"Request blocked by guardrail <name>."`, zero receipts, the input never echoed |

## Step 1: export from Agent Builder

In Agent Builder open the workflow, choose **Code**, then **Agents SDK**, then **Python**, and
save the files into one directory (OpenAI's migration page describes the menu). The export is
ordinary Agents SDK code: a `WorkflowInput` pydantic model with `input_as_text`, one `Agent(...)`
per Agent node, `@function_tool` functions, guardrail functions, and one `async def` entry
function that calls `Runner.run`. The TypeScript export is not supported by this importer.

## Step 2: validate (offline, nothing created)

```bash
backend/.venv/bin/python scripts/import/openai_agents_sdk.py validate ./my-export
```

Prints the findings as JSON and exits 0 (clean), or 2 with every refusal. Each refusal is exact
and names the fix:

| Code | Refuses | Fix |
|---|---|---|
| `literal_secret` | an OpenAI key literal (`sk-...`) or `OPENAI_API_KEY = "..."` in code | remove it; the key lives in Secrets Manager (`--openai-key-secret-arn`) or is not needed (Bedrock mode) |
| `openai_hosted_tool` | `FileSearchTool`, `WebSearchTool`, `CodeInterpreterTool`, `ComputerTool`, `ImageGenerationTool`, `LocalShellTool`, `ShellTool`, and `vs_...` vector store ids | each message names the AWS replacement (Managed Knowledge Base, Web Search / Web Answer Gateway tools, AgentCore Code Interpreter, AgentCore Browser, a custom Gateway tool); `--allow-openai-hosted-tools` lifts only these and the tools still fail at run time on AWS |
| `human_approval` | `needs_approval=`, `HumanApproval`, approval items | a headless Runtime cannot pause for a person; model the approval as a platform HITL step and remove it from the export |
| `typescript_export` | only `.ts` files | re-export as Agents SDK → Python |
| `no_python_export` / `no_agents_sdk_import` | no Python, a syntax error (reported once, with its line), or code that never imports `agents` | this is not an Agent Builder export |
| `entry_ambiguous` / `entry_missing` | several or no top-level one-parameter `async def` functions, or an export that already has an `agent.py` | pass `--entry module:function` (the function that calls `Runner.run`); rename your `agent.py` |

Two findings are warnings, not refusals: `mcp_passthrough` (an `HostedMCPTool` / `MCPServer*` is
kept as is and called from inside the runtime; put the server behind an AgentCore Gateway target
to get Cedar authorization and Gateway guardrails) and `openai_tracing` (the export opens an OpenAI
trace; in Bedrock mode tracing to OpenAI is disabled).

### How Agent Builder nodes land

| Agent Builder node | In the Python export | Importer |
|---|---|---|
| Start | `WorkflowInput(input_as_text=...)` | detected as the entry's parameter type and instantiated with the caller's text |
| Agent | `Agent(name=, instructions=, model="gpt-...", tools=, ...)` | unchanged; in Bedrock mode the model id is overridden at run time (below) |
| Guardrails | `@input_guardrail` / `@output_guardrail` | unchanged; a tripwire is answered cleanly |
| If/else, While, Transform, Set state | plain Python | unchanged |
| MCP | `HostedMCPTool`, `MCPServerStreamableHttp`, ... | kept, warning `mcp_passthrough` |
| File search | `FileSearchTool(vector_store_ids=[...])` | refused (`openai_hosted_tool`): OpenAI vector stores are not reachable from AWS |
| Human approval | `needs_approval=True` and approval items | refused (`human_approval`) |
| Note | comments | nothing to do |

## Step 3: choose the model provider

**`--model-provider bedrock` (recommended, no OpenAI account needed).** The export runs against
Amazon Bedrock's OpenAI-compatible endpoint (`https://bedrock-runtime.<region>.amazonaws.com/openai/v1`).
The generated entrypoint points the Agents SDK's default client at that endpoint with a short-term
Bedrock API key from `aws-bedrock-token-generator` (minted from the runtime role, 12 h maximum,
never stored), switches the SDK to the **Responses API**, disables tracing to OpenAI, and wraps
`Runner.run` so every run carries a `RunConfig(model=AGENT_MODEL_ID)`: the export's OpenAI model
names (`gpt-5-mini`, ...) would not resolve on Bedrock, and the exported file stays byte-identical.

- Default model: `global.openai.gpt-5.6-terra` through the Responses API. Function tools work.
- Alternative: `--bedrock-openai-api chat_completions --bedrock-model-id openai.gpt-oss-120b-1:0`.
  Measured 2026-11-12: gpt-oss is served through chat completions only and declined the fixture's
  function tool while reasoning was enabled; GPT-5.6 refused function tools on `/chat/completions`
  and asked for `/v1/responses`. Use the alternative for exports without function tools.
- The runtime role gets `bedrock:InvokeModel*` on the one model (foundation model and inference
  profile ARNs), `bedrock:InvokeModel` on the account's default project (the Responses API
  authorizes both; without it every turn is a 401), and `bedrock:CallWithBearerToken` restricted
  to `bedrock:bearerTokenType = SHORT_TERM`. Nothing else.

**`--model-provider openai`.** The export keeps calling OpenAI. Create a Secrets Manager secret
holding the key and pass `--openai-key-secret-arn`; the runtime reads it at cold start through
`secretsmanager:GetSecretValue` on that one ARN. The key is never in code, in the bundle or in a
plain environment variable.

## Step 4: package (offline)

```bash
backend/.venv/bin/python scripts/import/openai_agents_sdk.py package ./my-export \
  --out ./my-export-bundle --model-provider bedrock
```

Copies the export unchanged, adds `agent.py` (the `BedrockAgentCoreApp` entrypoint),
`requirements.in` (`openai-agents==0.24.0`, `bedrock-agentcore==1.23.1`,
`aws-bedrock-token-generator==1.1.0`, `boto3==1.43.109`) and `IMPORT_MANIFEST.json` (entry, provider,
warnings, SHA-256 of every file), then resolves a hash-locked closure with `uv pip compile
--generate-hashes`, installs it with `--require-hashes --only-binary :all:` for
**Linux ARM64 / CPython 3.13** (the AgentCore Runtime container), verifies every native file is ELF
AArch64, and writes a deterministic ZIP (sorted entries, fixed 1980 timestamps, normalized modes):
the same export always produces the same `sha256`. The fixture bundle is about 32 MB.

Why the architecture check exists: `CreateAgentRuntime` accepts an x86_64 bundle and the runtime
then fails minutes later with "Your artifact contains binary files that are incompatible with
Linux ARM64" (measured 2026-11-12). The importer refuses offline instead.

## Step 5: deploy, adopt, smoke-test

Mint a platform access token for the owner (the signed-in user who will own the runtime) and run:

```bash
export API_BEARER_TOKEN=...   # the platform user's Cognito access token
backend/.venv/bin/python scripts/import/openai_agents_sdk.py deploy ./my-export \
  --out ./my-export-bundle --model-provider bedrock \
  --runtime-name support_triage --region us-east-1 --profile <aws-profile> \
  --bucket <platform-artifacts-bucket> \
  --platform-url https://<cloudfront-domain> \
  --smoke-prompt "What is the status of order ORD-1?" \
  --result-json ./import-result.json
```

In order:

1. **Execution role** `ImportedAgentRuntime-<runtime-name>`: trust limited to
   `bedrock-agentcore.amazonaws.com` from this account and Region (`aws:SourceAccount`,
   `aws:SourceArn`); permissions limited to reading its own ZIP key, writing its own log group,
   X-Ray, CloudWatch metrics in the `bedrock-agentcore` namespace, its own workload identity, and
   the model (or secret) chosen above.
2. **Upload** of the ZIP to `s3://<bucket>/imports/openai-agents-sdk/<runtime-name>/<sha16>.zip`
   with `ExpectedBucketOwner`.
3. **`CreateAgentRuntime`** with the same request shape the platform sends for its own runtimes
   (direct code deploy, `PYTHON_3_13`, entry `agent.py`, HTTP protocol, `PUBLIC` network mode
   for egress to Bedrock; invocation is still IAM-only), tagged
   `ManagedBy=openai_agents_sdk_importer`, `ImporterVersion=1.0.0` plus any `--tag key=value`.
   Deliberately **not** the platform's `ManagedBy`/`AgentCoreStack` ownership tags: the platform
   must keep seeing this runtime as one it did not build.
4. **Wait for `READY`** (well under a minute for the fixture).
5. **Adopt**: `POST /api/runtime/import {"runtimeArn", "awsRegion"}` (scope `agent:write`) records
   the runtime as a caller-owned, `imported: true` deployment. The response carries
   `deploymentId` and `runtimeId`.
6. **Smoke**: a warm-up call absorbs the cold start, then `POST /api/test-runtime
   {"input", "runtimeId"}` with your prompt (retried a bounded number of times only while the
   platform reports `Agent still running`). The result JSON holds the answer and the tool receipts.

The deploy prints `runtime created: <id> <arn>` before it waits, so a later failure leaves you with
the ids `teardown` needs.

## Invoking an adopted runtime

Use `POST /api/test-runtime` with the `runtimeId` from adoption (scope `invoke`):

```json
{"input": "What is the status of order ORD-1?", "runtimeId": "support_triage-AbCdEf1234", "sessionId": "..."}
```

The slot routes (`/api/runtimes/{name}/slots/{slot}/invoke`) answer `404 {"detail": "Not found"}`
for an adopted runtime: adoption writes a deployment record, not version or slot rows, so there is
no staging/production pointer, no promotion gate and no production trigger for it. If you need
those, rebuild the agent on the platform's own deploy path.

The payload the runtime accepts is the platform's: `{"prompt": ...}` (also `input_as_text` or
`input`), optional `session_id` (accepted; each call is one stateless Agents SDK run), and
`{"warmup": true}` which returns at once. The response is
`{"response": ..., "tool_receipts": [...]}`, or `{"response": "Request blocked by guardrail <name>.",
"blocked_by_guardrail": {...}, "tool_receipts": []}` when one of the export's own guardrails trips.

## Deleting

`DELETE /api/runtime/{runtimeId}` on an adopted runtime **releases** the platform's record and
leaves the AWS runtime running (the response says so). The platform's destroy path proves
ownership through its own tags before it deletes anything, and an imported runtime deliberately
does not carry them, so the importer owns destruction:

```bash
backend/.venv/bin/python scripts/import/openai_agents_sdk.py teardown \
  --runtime-name support_triage --region us-east-1 --profile <aws-profile> \
  --bucket <platform-artifacts-bucket> --platform-url https://<cloudfront-domain>
```

Releases the record, deletes the runtime and **confirms its absence** before anything the runtime
still depends on goes, then deletes the execution role, the service-created log groups (they
outlive the runtime) and the ZIP. Idempotent.

## Live evidence

Run against the `dev` platform on 2026-11-13 at commit `f3afbb4` (`tools/run_openai_exit_probe.sh`
in the working notes; 49 checks, 49 passed, zero residue):

- Four adversarial exports (literal key, `FileSearchTool`, TypeScript-only, `needs_approval`) each
  made `deploy` exit 2 with `IMPORT REFUSED` and the exact finding lines above; no role, runtime,
  log group or object was created.
- The fixture export (classifier → order agent with a `lookup_order` function tool and a
  `no_card_numbers` input guardrail) deployed in Bedrock mode: the whole `deploy` command
  (resolve and package with a warm `uv` cache, role, upload, create, READY, adopt, warm-up, smoke)
  took 63 s; adopted (`imported: true`); answered `Order ORD-1 has shipped on 2026-11-10.` on the
  first attempt after warm-up, with one receipt `{"name": "lookup_order", "status": "success",
  ...digests}` and no argument values.
- A prompt carrying a card number came back as `Request blocked by guardrail no_card_numbers.`
  with zero receipts and the digits absent from the response; the slot route answered 404; the
  anonymous invoke answered 401; `DELETE` without `?destroy=true` released the record and the
  runtime stayed READY.
- `teardown` finished in 12 s; the account held no runtime, role, log group or object under the
  importer's names afterwards.

The offline contract is `backend/tests/test_openai_agents_sdk_import.py` (33 tests) with 21
mutation fixtures that each break the suite when a control is lifted (refusals, the flag's scope,
entry ambiguity, deterministic ZIP, importer-only tags, the `SHORT_TERM` bearer condition, the
default-project grant, the trust policy, the ARM64 target and check, bounded smoke retry,
confirmed destroy, teardown ordering, log-group prefix).

## Limits

- Python exports only; one Region per run (`--region`); one runtime per `--runtime-name`
  (`[A-Za-z][A-Za-z0-9_]{0,47}`).
- Hosted OpenAI tools and vector stores have no automatic translation; the refusal names the AWS
  replacement and `--allow-openai-hosted-tools` only defers the failure to run time.
- Human-approval nodes need redesign as platform HITL steps.
- Adopted runtimes have no versions, slots, promotion gate, triggers or embeddable production
  alias.
- `uv` is required locally; packaging downloads ARM64 wheels from PyPI.
