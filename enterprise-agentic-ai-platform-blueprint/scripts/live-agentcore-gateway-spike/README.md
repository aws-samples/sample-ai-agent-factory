# AgentCore Gateway inference compatibility spike

This focused live test proves the two August 2026 contracts that the host's older
AWS CLI cannot model:

1. an AgentCore Gateway inference target using the built-in `bedrock-mantle`
   connector; and
2. a customer-defined Gateway rate limit using `CreateGatewayRateLimit`.

It is deliberately **not** the final platform deployment. Phase A uses AWS IAM
inbound authorization to prove service behavior with the smallest credential
surface. Phase B adds Cognito M2M and the Strands `LiteLLMModel` client only after
Phase A passes.

## Safety properties

- Requires the expected 12-digit account ID and refuses a different STS identity.
- Creates only resources beginning with the supplied `--prefix`.
- Applies `application-id`, `agent-id`, `tenant-id`, `cost-centre`, and
  `environment` tags to every taggable resource.
- Uses the exact Gateway service-role permissions from the official AgentCore
  sample: `bedrock-mantle:ListModels` and `bedrock-mantle:CreateInference`.
- Stores state and evidence under `$KIROCREW_SCRATCH`, never in `/tmp` or a
  committable directory.
- `all` runs cleanup in `finally`; cleanup verifies ownership before deletion.
- Never reads or handles access keys. Boto3 uses the credential provider already
  selected by the execution environment.

## Pinned tooling

The August 2026 API first appears after the repository's old `boto3==1.35.90`
pins. This spike uses exactly:

```text
boto3==1.43.97
botocore==1.43.97
```

Create an isolated environment:

```bash
SPIKE_DIR="$PWD/scripts/live-agentcore-gateway-spike"
VENV="$KIROCREW_SCRATCH/aiaf-gateway-spike-venv"
uv venv "$VENV" --python 3.13
uv pip install --python "$VENV/bin/python" -r "$SPIKE_DIR/requirements.txt"
```

## Run the complete IAM-authenticated spike

Use brokered credentials for the Platform account before running this command.
Do not set credential values in environment variables.

```bash
"$VENV/bin/python" "$SPIKE_DIR/gateway_spike.py" all \
  --account-id "$PLATFORM_ACCOUNT_ID" \
  --region us-west-2 \
  --prefix aiaf-live-20260918
```

The command must prove, in order:

1. STS identity matches `--account-id`.
2. Gateway service role is created with scoped trust and permissions.
3. Gateway reaches `READY`.
4. `bedrock-mantle` inference target reaches `READY`.
5. `/inference/v1/models` returns a qualified model.
6. Chat Completions succeeds without streaming.
7. Chat Completions succeeds with OpenAI-compatible SSE streaming.
8. A zero-rate explicit model entry returns HTTP 429 for that same known-good
   model, while the earlier call is its positive twin.
9. Rate limit, target, Gateway, inline role policy, and role are removed.
10. A second cleanup pass reports no residue and exits successfully.

Evidence contains request IDs, status codes, resource identifiers, manifest SHA,
and cleanup results. It excludes credentials, authorization headers, and model
response text.

## Manual recovery

If the process is interrupted, run cleanup with the same state file printed by
the command:

```bash
"$VENV/bin/python" "$SPIKE_DIR/gateway_spike.py" cleanup \
  --account-id "$PLATFORM_ACCOUNT_ID" \
  --region us-west-2 \
  --prefix aiaf-live-20260918
```

Cleanup refuses to delete a Gateway or IAM role unless both its exact name and
ownership tags match the spike configuration.

## Definition of done

A local unit test, successful synth, or HTTP error is not proof. Phase A passes
only when a real model call succeeds before the rate limit and the same model
then returns an exact 429 after the zero-rate rule, followed by a zero-residue
audit. Phase B separately proves JWT refresh and `LiteLLMModel` behavior.

## Phase B: Cognito M2M and Strands `LiteLLMModel`

After Phase A passes, run the binding generated-agent client test:

```bash
"$VENV/bin/python" "$SPIKE_DIR/cognito_litellm_spike.py" all \
  --account-id "$PLATFORM_ACCOUNT_ID" \
  --region us-west-2 \
  --prefix aiaf-live-20260918
```

Phase B creates an ephemeral Cognito User Pool, client-credentials resource
server/client/domain, and a `CUSTOM_JWT` Gateway. It retrieves each access token
in memory, passes it through `LiteLLMModel.client_args.api_key`, and never writes
or logs the token or client secret. The model uses:

```python
LiteLLMModel(
    model_id="openai/bedrock-mantle/openai.gpt-oss-120b",
    client_args={"api_base": "<gateway>/inference/v1", "api_key": token},
    params={"max_tokens": 256, "temperature": 0, "stream": stream},
)
```

The gate passes only when distinct streaming and non-streaming Strands events are
recorded, the exact 429 names the non-streaming event as its positive twin, and
both the AgentCore/IAM and Cognito cleanup audits report zero residue.
