<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Security Exceptions

This catalogue records every deliberate security exception in the blueprint —
cdk-nag suppressions and tolerated static-analysis findings — each with an
owner, the requirement/finding it relates to, and the justification. It is the
companion to the project's security policy and is referenced by inline
`SEC-0NN` markers in the CDK source.

Each exception is a conscious, reviewed trade-off. None grants standing access
beyond what the referenced control requires. Where an exception exists because
of an external service limitation (e.g. an AWS control plane that does not yet
support resource-level scoping), it is marked **service-limitation** and should
be revisited when the upstream service adds support.

| ID | Area | Type | Justification (summary) | Compensating control |
|----|------|------|--------------------------|----------------------|
| SEC-001 | S3 access logging / replication on audit + record-keeping buckets | deferred | Access logs / CRR deferred to a v2 DR roadmap; buckets are themselves the audit trail. | Object Lock + versioning + KMS + BlockPublicAccess. |
| SEC-002 | S3 cross-region replication | deferred | CRR deferred to v2. | Versioning + Object Lock retention. |
| SEC-003 | S3 object ownership vs CMK bucket-key | design | `ObjectWriter` ownership is incompatible with CMK + bucket-key; `BucketOwnerEnforced` is used. | BlockPublicAccess + bucket policy. |
| SEC-004 | Bedrock Guardrail admin actions on `*` | service-limitation | Guardrail Create/Update/Delete/Get/List do not support resource-level scoping; AWS returns AccessDenied with an ARN. | Single-purpose `GuardrailAdminRole`; SCP guardrail-enforcement at org level. |
| SEC-005 | Inline policies on single-purpose roles | design | Inline policy keeps the exact scope visible on the role. | One role per purpose; assumed only by the intended service principal. |
| SEC-006 | CDK `AwsCustomResource` Lambda runtime (L1) | framework | Runtime is CDK-managed; upgrades follow the CDK release train. | Provisioning-only, CFN-lifecycle-bound. |
| SEC-007 | Lambda reserved concurrency | design | Cadence-driven Lambdas; reserving concurrency would break deploys / schedules. | EventBridge schedule bounds invocation rate. |
| SEC-008 | Lambda DLQ | design | Failures emit CW metric + composite alarm + SNS; a DLQ would duplicate the failure path. | Alarms wired to the failures SNS topic. |
| SEC-009 | Lambda not in VPC | design | Control-plane-only Lambdas call Bedrock/CW/DDB via AWS-managed endpoints. | IAM-authenticated public service endpoints; no data-plane exposure. |
| SEC-010 | `AWSLambdaBasicExecutionRole` managed policy (IAM4) | framework | Documented default role for Lambda-backed custom resources; grants only CW Logs write. | Scope limited to log write. |
| SEC-011 | `ec2:DescribeSubnets` / `ec2:DescribeAvailabilityZones` on `*` | service-limitation | Account-wide describe APIs with no resource-level permission support; read-only, non-mutating. | Read-only; used only by the AZ-ID resolution custom resource. |
| SEC-012 | Internal ALB without its own WAF | design | WAF is at the public API Gateway stage (primary auth boundary); the ALB sits behind an API Gateway VPC Link. | Defense-in-depth at the public edge; ALB not internet-facing. |
| SEC-013 | (reserved) | — | See inline reference. | — |
| SEC-014 | Cognito Plus tier (advanced security / MFA) | cost opt-in | Plus tier is per-MAU priced; imposes cost on adopters. Opt-in via stack override. | 12-char password policy + email verification baseline. |
| SEC-015 | LiteLLM master key via ECS secret | design | Key injected from Secrets Manager as an ECS secret (never plaintext env). | Secrets Manager + ECS secret injection. |
| SEC-016 | LiteLLM master key rotation | manual | No target-service rotation contract; rotation is a gated manual-release operation. | Documented in `OPERATIONS.md`. |
| SEC-022 | Athena results bucket not versioned | design | Derived data with 7-day expiry; versioning conflicts with the lifecycle rule and inflates cost. | Lifecycle expiry + KMS + BlockPublicAccess. |
| SEC-023 | DynamoDB not in AWS Backup plan | design | PITR is enabled; AWS Backup is a customer opt-in for formal compliance. | PITR + CMK. |
| SEC-024 | CodeBuild `CloudWatchLogsFullAccess` (IAM4) | framework | AWS-managed policy CodeBuild requires for log streams. | Build-role scope; no data access. |
| SEC-025 | Scoped Bedrock/Athena/Glue/SES/metric wildcards | design | `bedrock:InvokeModel` scoped to the allowed-models SSOT; `cloudwatch:PutMetricData` scoped by namespace condition; chargeback Athena/Glue/SES scoped to the specific workgroup/database/table/identity (Holmes CSR remediation). | Resource-level ARNs + conditions. |
| SEC-026 | PrivateLink cross-account caller principal | design | Endpoint-service allow-list is the cross-account trust boundary. | Endpoint-service allow-listed principals only. |
| SEC-027 | `GatewayAdminRole` gateway/registry-record wildcards | service-limitation | Gateway mutation is the administrative contract; per-record curator promotion needs `<registryArn>/record/*`. | SCP-09 / SCP-11 org-level deny; role-name pin. |
| SEC-028 | `bedrock-agentcore:*` on registry/gateway custom-resource Lambdas | service-limitation | AgentCore control-plane action-family evaluator rejects narrow per-action lists for Create/Update/Delete of registries, records, gateways, and their internally-provisioned Workload Identities (live-verified 2026-05 / 2026-07). Provisioning-only CDK `AwsCustomResource` Lambdas — not runtime principals. **Application/runtime IAM MUST NOT copy this**; use explicit actions (`bedrock-agentcore:InvokeGateway`, etc.) there. Revisit when AgentCore GA publishes a least-privilege action list. | (1) CDK-managed Lambda lifetime, bounded to the CFN create/update/delete cycle; (2) SCP-09 (gateway) / SCP-11 (registry) org-level deny restricting who may call these APIs; (3) resource scope narrowed to the registry + its records where the API permits. |
| SEC-029 | CDK custom-resources Provider framework internals | framework | The registry readiness gate uses the CDK `Provider` framework, whose onEvent/isComplete/onTimeout Lambdas + waiter Step Function are framework-generated and reference each other with function-ARN version wildcards (`<arn>:*`), without ALL-events logging or X-Ray. Not authorable without forking the framework. | Read-only `GetRegistry` polling; provisioning-only; bounded by the CFN lifecycle. |

## Review cadence

Service-limitation exceptions (SEC-004, SEC-011, SEC-027, SEC-028) are
re-evaluated whenever the corresponding AWS service publishes resource-level
or least-privilege support. Framework exceptions (SEC-006, SEC-010, SEC-024,
SEC-029) are re-evaluated on each `aws-cdk-lib` major bump. All others are
reviewed at each release.
