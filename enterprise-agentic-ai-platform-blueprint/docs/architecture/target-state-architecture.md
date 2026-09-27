# Target architecture

## Decision

The supported reference architecture is a shared governance and inference control plane with workstream-owned agent execution.

- **Management and Governance account:** Organizations controls, CloudWatch OAM sink, audit, and log archive.
- **Platform account:** AWS Agent Registry, shared AgentCore inference Gateway, Bedrock Guardrails, Cognito M2M, tool aliases, and deployment pipelines.
- **Workstream account:** AgentCore Runtime, Memory, the Tool Gateway, and agent execution roles.

See [`../../assets/d03-two-gateway.svg`](../../assets/d03-two-gateway.svg) for the request paths.

## Request paths

### Inference

1. The generated agent runs in AgentCore Runtime.
2. `LiteLLMModel` requests an AgentCore Identity token.
3. AgentCore Identity exchanges the workload identity for the environment Cognito M2M token.
4. The agent calls the Platform inference Gateway's OpenAI-compatible endpoint.
5. A Gateway request interceptor applies the stage Bedrock Guardrail to each untrusted turn.
6. The Gateway's Bedrock Mantle target invokes only a model allowed by its IAM condition.

The generated agent never invokes Bedrock directly.

### Tools

1. The generated agent uses `MCPClient` with AWS SigV4.
2. The Workstream Tool Gateway authenticates the Runtime role with AWS_IAM.
3. Gateway targets are derived from approved AWS Agent Registry governance records.
4. The Gateway service role can invoke only the subscribed Platform Lambda aliases.
5. Native AgentCore PolicyEngine can enforce per-tool policies; the Lambda Cedar wrapper remains as rollback and defense in depth.

The generated agent never invokes Lambda directly.

## Deployment path

All Workstream changes flow through GitHub and the Workload pipeline:

```text
Source → Synth → stable roles/OAM link → permission handoff
       → nonproduction Gateway/Runtime/Memory
       → deployed-runtime evaluation
       → human approval
       → production Gateway/Runtime/Memory
```

The Platform pipeline owns Registry records, tool aliases, Guardrails, inference Gateways, and exact resource-based grants to Workstream Gateway roles.

The handoff is deliberately two phase:

1. Workload pipeline creates stable roles and pauses.
2. Platform pipeline grants each environment's aliases to its exact role ARN.
3. Workload pipeline resumes only after the live alias policies are verified.

Grant retirement reverses this order before teardown.

## Trust boundaries

| Boundary                          | Primary controls                                                                |
| --------------------------------- | ------------------------------------------------------------------------------- |
| GitHub to pipeline                | CodeConnections, explicit feature branch, self-mutating CDK pipeline            |
| Platform to Workstream deployment | CDK bootstrap trust, scoped execution policies, exact target account and Region |
| Runtime to Tool Gateway           | AWS_IAM, SigV4, exact Runtime role, approved tool targets                       |
| Runtime to inference Gateway      | AgentCore Identity, Cognito M2M, CUSTOM_JWT, mandatory Guardrail interceptor    |
| Gateway to tools                  | Exact Lambda alias ARNs, exact Gateway service-role principal                   |
| Gateway to model                  | `bedrock-mantle:Model` allow-list, stage Guardrail, account quotas              |
| Memory                            | Actor-scoped event namespace, exact Memory ARN, customer-managed KMS key        |
| Observability                     | OAM source links, explicit or Organizations-scoped sink policy                  |

## Region support

The complete reference flow is validated in `eu-west-1`.

A Region is supportable only after all of the following pass independently:

1. AgentCore Runtime, Memory, Gateway, Identity, Policy, Registry, and Evaluations availability.
2. Bedrock Mantle model availability and data-residency review.
3. Region-scoped IAM and SCP validation.
4. AgentCore-compatible availability-zone IDs.
5. strict Platform and Workload synthesis.
6. positive and adversarial live calls.
7. rollback and re-run to green.
8. centralized observability.
9. dependency-ordered teardown and zero-residual inventory.

No result from one Region is extrapolated to another.

## Availability and resilience

- Platform and Workstream environments can use separate AWS accounts.
- Workstream Runtime and Memory are environment isolated.
- The pipeline evaluates the deployed nonproduction Runtime before production approval.
- Runtime upgrades are expected to preserve availability while AgentCore changes the serving version.
- An interrupted update must roll back to the prior image and remain invocable.
- Native Gateway rate limits are approximate and fail open; account quotas and IAM/SCP controls are the hard boundaries.
- Transaction Search is opt-in because it changes account-wide CloudWatch behavior and incurs cost.

## Data and secrets

- Cognito client secrets stay in Secrets Manager and are never emitted as CloudFormation outputs.
- Registry context files contain no secret but are environment-specific deployment inputs and should not be committed.
- Agent evidence stores hashes, counts, status codes, and metrics—not prompts, credentials, tokens, or response content.
- Every taggable resource carries `application-id`, `agent-id`, `tenant-id`, `cost-centre`, and `environment`.

## Teardown contract

Teardown runs Workstream → Platform → Management.

Before Workstream deletion, the Platform pipeline removes all four Lambda alias grants. Each account then runs `scripts/final_teardown.py` in dry-run mode and reviews the exact plan before `--apply`.

Completion requires direct inventory across all project surfaces. Zero stacks alone is not sufficient because services can leave log groups, image digests, managed secrets, Registry records, user pools, and alias-less KMS keys.

KMS keys can remain in AWS's seven-day pending-deletion window. Object Lock can intentionally prevent data deletion until retention expires.

## Explicit non-goals

- This sample is not a managed service or compliance attestation.
- It does not automatically enable Transaction Search.
- It does not claim every AWS Region.
- It does not make Gateway rate limiting a hard quota.
- It does not remove the Lambda Cedar wrapper automatically.
- Optional legacy direct-Bedrock/profile components are not part of the Ireland support envelope.
