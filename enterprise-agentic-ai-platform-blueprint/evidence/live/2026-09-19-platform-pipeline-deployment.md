# Live evidence — Platform pipeline deployment

- **Date:** 2026-09-19
- **Status:** PASS for the deployment and read-only assertions listed below
- **Region:** `us-west-2`
- **Git HEAD:** `f037b4ed8d6852325b0eb3585eed31bd7670286f`
- **Pipeline execution:** `08ed2063-7dd5-4c5b-96a3-15493c387c09`
- **Validation topology:** one Management/Governance account, one Platform account representing both environments for this test, and one Workstream sender account

This file is a sanitized summary. It contains no AWS account IDs, access keys,
client secrets, JWTs, authorization headers, or model response text.

## Pipeline result

The pipeline completed with status `Succeeded` on the exact Git commit above.

| Stage | Result |
|---|---|
| Source | Passed; revision matched `f037b4e` |
| Synth | Passed; dependency install, TypeScript build, Jest suite, and strict CDK synthesis completed |
| SelfMutate | Passed |
| File assets | Passed |
| Nonprod | Audit, Log Archive, Guardrail, Registry, and Inference Gateway passed |
| SecurityReview | Explicitly approved for this test execution |
| Prod | Guardrail, Registry, and Inference Gateway passed |

The consolidated validation topology deliberately mapped Platform nonproduction
and production to one account. The production stage reused the stable
`AgenticAI-GuardrailAdmin` role and created a separately named regional baseline
guardrail. Normal deployments use separate Platform accounts and retain the
stable unsuffixed names in each account.

## Management/Governance assertions

The live `Nonprod-LogArchive` stack reached `CREATE_COMPLETE` with 18 resources.
Read-only service checks verified:

1. The CloudWatch Logs destination policy names the Workstream sender as a
   12-digit account ID, not an IAM root ARN.
2. The destination targets `agenticai-central-logs` and uses
   `AgenticAI-LogArchive-CWLDestinationRole`.
3. The role trust is limited to `logs.amazonaws.com` with sender and recipient
   `aws:SourceArn` conditions.
4. Its inline policy grants only `kinesis:ListShards`, `PutRecord`, and
   `PutRecords` on the named stream, plus KMS encrypt/data-key/re-encrypt actions
   on the stack key.
5. The stream is `ACTIVE`, `ON_DEMAND`, KMS-encrypted, and retains records for
   24 hours.
6. CloudTrail and CUR buckets use the same customer-managed key; public access
   is blocked on the archive bucket.
7. KMS automatic rotation is enabled with a 365-day period.

## Production Platform assertions

Read-only service checks verified:

1. `Prod-Guardrail` reached `CREATE_COMPLETE` without creating a duplicate IAM
   admin role.
2. The production guardrail is named `agenticai-guardrail-baseline-prod` and is
   `READY`.
3. `Prod-InferenceGateway` reached `CREATE_COMPLETE` with native
   `AWS::BedrockAgentCore::Gateway`, `GatewayTarget`, and `GatewayRateLimit`
   resources plus Cognito M2M resources.
4. Gateway `agenticai-inference-prod-5pguxqqepp` is `READY` with `CUSTOM_JWT`
   inbound authorization.
5. Inference target `2CIAUE6YEK` is `READY` and uses `GATEWAY_IAM_ROLE` outbound
   authorization.
6. The production rate-limit resource reached `CREATE_COMPLETE`.
7. `Prod-Registry` reached `CREATE_COMPLETE` with the current DynamoDB-backed
   registry implementation; replacing it with the real AgentCore Registry
   remains an explicit release gate.

The host AWS CLI can read Gateway and target status but predates the
`get-gateway-rate-limit` operation. The CloudFormation resource status therefore
proves deployment of this rate-limit instance; the separate 2026-09-18
compatibility spike proves live HTTP 429 behavior for the same construct
contract.

## Defects found and closed during live deployment

1. Cross-account pipeline creation requires target deploy-role `PassRole` scoped
   to CodePipeline and target CloudFormation execution-role `PassRole` scoped to
   CloudFormation.
2. Root pipeline artifact buckets needed explicit destroy/auto-delete behavior
   to avoid rollback residue.
3. CodeBuild requires each BuildSpec command to be independently valid, and a
   nested checkout must publish `cdk.out` back to the artifact root.
4. Management CloudFormation requires read access to its CDK bootstrap version
   parameter.
5. Log Archive previously referenced a role and Kinesis stream it never created.
6. CDK's nonproduction S3 auto-delete provider requires stack-prefixed IAM/Lambda
   lifecycle permissions and Lambda-only `iam:PassRole`.
7. CloudWatch Logs destination policies require account IDs; the service rejects
   IAM root ARNs in `Principal.AWS`.
8. A consolidated Platform test account must share the fixed Guardrail admin role
   and isolate the regional production guardrail name.

Failed attempts were inventoried and removed before each retry. KMS keys created
by failed CloudFormation attempts remain only in AWS-managed pending-deletion
states with no active aliases.

## Not yet proven

This deployment does **not** prove:

- Cognito M2M or model invocation through this pipeline-created production
  Gateway. Those behaviors passed in the isolated compatibility spike, but must
  still be rerun against this deployed instance.
- AgentCore Gateway PolicyEngine enforcement or Bedrock Guardrail attachment on
  every inference call.
- Per-developer `sub` authorization; the current interim Lambda evaluator is
  group-based.
- Workstream Tool Gateway, Runtime, Memory, generated-agent deployment, or
  pipeline-only Workstream delivery.
- SCPs 01–12 through an organization sandbox soak.
- Full adversarial, canary, rollback, and final teardown gates.
- Gateway OTEL rate-limit span correlation, which remains a reproduced blocker
  in `us-west-2`.

These remain explicit release blockers rather than implied successes.
