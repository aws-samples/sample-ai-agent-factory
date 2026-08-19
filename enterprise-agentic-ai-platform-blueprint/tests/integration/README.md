# D-03 Live-AWS Integration Tests

End-to-end live-AWS tests for the D-03 deviation (centralised platform
LiteLLM + per-tenant Application Inference Profiles). Exercises the full
cross-account path: workload runtime role -> platform `BedrockCallerRole`
(AssumeRole + ExternalId, no SessionTags per BUG-005) -> Bedrock Converse
against the tenant-specific Application Inference Profile.

Run order against a live deployment:

1. Deploy both stacks (platform + workload) — see `DEPLOYMENT.md`.
2. Run `pytest tests/integration -v`.
3. Run `cdk destroy` for both stacks.
4. Run `pytest tests/teardown -v` to confirm no residual resources.

## Required AWS state

Deployed stacks in `us-east-1`:

| Stack                            | Account  | Key outputs consumed |
|---                               |---       |---                    |
| `AgenticAI-D03-PlatformCoreStack` | Platform | `AgenticAI-D03-BedrockCallerRoleArn`, `AgenticAI-D03-GuardrailId`, `AgenticAI-D03-GuardrailArn`, `AgenticAI-D03-AppInfProfile-<tenant>-<agent>`, `AgenticAI-D03-AgentRegistryTable`, `AgenticAI-D03-ExperimentTrackingTable`, `AgenticAI-D03-SharedEcrRepoUri`, `AgenticAI-D03-InvocationLogGroup` |
| `AgenticAI-D03-WorkloadAgentStack` | Workload | runtime role `AgenticAI-D03-<tenant>-<agent>-runtime` |

Region `us-east-1` is required — it carries the full Claude 4.5
cross-region inference profile used by the platform stack.

## Environment variables

Platform account (12-digit id supplied at deploy time):
```
export AWS_ACCESS_KEY_ID_PLATFORM=...
export AWS_SECRET_ACCESS_KEY_PLATFORM=...
# optional
export AWS_SESSION_TOKEN_PLATFORM=...
# or use a named profile instead:
export AWS_PROFILE_PLATFORM=agenticai-platform
```

Workload account (12-digit id supplied at deploy time):
```
export AWS_ACCESS_KEY_ID_WORKLOAD=...
export AWS_SECRET_ACCESS_KEY_WORKLOAD=...
# optional
export AWS_SESSION_TOKEN_WORKLOAD=...
# or use a named profile instead:
export AWS_PROFILE_WORKLOAD=agenticai-workload
```

Test parameters:
```
export AWS_REGION=us-east-1
export AGENTICAI_D03_EXTERNAL_ID=<the ExternalId the stack was deployed with>
export AGENTICAI_D03_TENANT_ID=demo        # default: demo
export AGENTICAI_D03_AGENT_ID=primary      # default: primary
```

If any of these are missing the suite short-circuits via an autouse
session fixture and skips every test — matching the pattern in
`tests/smoke/smoke.py` so collection-only runs on dev machines don't
fail.

## Run

```
pytest tests/integration -v
pytest tests/teardown -v            # after cdk destroy
pytest -m integration -v             # marker-only filter
pytest -m teardown -v
```

## Expected runtime + cost

- Runtime: ~3 minutes on `us-east-1` (CloudTrail polling is the long pole — up to 120s).
- Bedrock spend: < $0.50 total. Each Converse call is bounded to 16 output tokens on Haiku 4.5.
- No resources are created by the tests beyond one experiment-tracking row (deleted at end of test).

## What each test proves

| Test | Evidence |
|---|---|
| `test_assume_role_with_correct_external_id_succeeds`  | Workload root can assume platform BedrockCallerRole with ExternalId. |
| `test_assume_role_with_wrong_external_id_denied`      | Trust-policy ExternalId condition is enforced. |
| `test_assume_role_without_external_id_denied`         | Missing ExternalId is denied. |
| `test_bedrock_converse_via_tenant_profile_with_guardrail_succeeds` | Full live Converse through the per-tenant inference profile w/ guardrail. |
| `test_bedrock_converse_without_guardrail_denied`      | Identity-policy Deny-on-null-GuardrailIdentifier enforced. |
| `test_bedrock_converse_non_allowlisted_model_denied`  | Model allow-list is honoured by the BedrockCallerRole. |
| `test_cloudtrail_carries_inference_profile_arn`       | CloudTrail record carries both the inference-profile ARN AND the `workload-<acct>-<tenant>-<agent>` RoleSessionName — BUG-005 workaround proven live. |
| `test_workload_can_read_agent_registry`               | Per-tenant Query returns without auth error. |
| `test_workload_can_write_experiment_tracking`         | PutItem succeeds with scoped tenantId. |
| `test_workload_cannot_write_agent_registry`           | Write to agent registry denied. |
| `test_cross_account_kms_decrypt_on_table_read`        | Full Query exercises the `kms:ViaService` scoped statement. |
| `test_workload_can_get_auth_token`                    | Workload can auth to platform ECR. |
| `test_workload_can_batch_get_image_on_shared_repo`    | Read path to shared repo is allowed. |
| `test_workload_cannot_put_image`                      | Write to shared repo denied. |
