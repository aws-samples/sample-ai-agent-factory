# CloudWatch Omni integration guide

How the blueprint adopts Amazon CloudWatch Omni as the fleet observability and evaluation plane (ADR-15 to ADR-19 in the [README](../README.md#14-architecture-decision-record)), what each stage deploys, the two post-deploy operations without CloudFormation support, the two production-evaluation options, and the live gates a Region must pass before Omni enters its support envelope.

## 1. Topology

```text
Management account (Identity Center primary Region)
  omni-domain stage ........ AWS::CloudWatch::DomainForOrganization + CloudWatchOmniDomainAccessRole
                             -> sign-in URL https://<name>.cloudwatch-omni.global.app.aws, DomainId d-...

Audit / Observability account (CloudWatch delegated administrator), one per Omni Region
  omni-hub stage ........... OmniHubStack
                             - CloudWatchOmniOperatorRole (CloudWatchOmniSpaceAccessPolicy, boundary = same)
                             - CloudWatchOmniDatasetIntegrationRole-<region> (logs.amazonaws.com)
                             - AgentCoreEvaluationRole-omni-<region> (bedrock-agentcore.amazonaws.com)
                             - CMK for space + Dataset (documented Omni key policy, tag cw-omni)
                             - CMK for centralized log groups (tag LogsManaged=true)
                             - AWS::XRay::TransactionSearchConfig
                             - AWS::CloudWatch::Space (CUSTOMER_MANAGED encryption)
                             - AWS::ObservabilityAdmin::OrganizationCentralizationRule (when a source scope is given)
  post-deploy .............. scripts/omni/omni_space_bootstrap.py: Dataset integration, access grants
  existing ................. AuditStack OAM sink (unchanged)

Platform and Workstream accounts (sources)
  WorkstreamObservabilityStack ... OAM link (unchanged) + opt-in AWS::XRay::TransactionSearchConfig
  D03WorkstreamRuntimeMemoryStack  AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT / AWS_REDACT_SPAN_ATTRIBUTES (opt-in)
  RuntimeExecutionRole ........... unchanged (already carries the four xray delivery actions)
```

The centralization rule replicates the blueprint's log groups (`/agenticai/*`, `/aws/bedrock-agentcore/*`, `/aws/vendedlogs/bedrock-agentcore/*`, `aws/spans`), all metrics and the Context Graph into the hub under `/centralized/<account>/<region><logGroup>`. Centralized rows carry `@aws.account` and `@aws.region`; the Omni **By account** data scope uses `cloud.account.id`.

## 2. Deploy order

1. **Prerequisites (one time).** Organizations trusted access for CloudWatch; register the Audit account as CloudWatch delegated administrator; Identity Center multi-Region replication if spaces will live outside the Identity Center Region.
2. **Management account.**
   ```bash
   npx cdk deploy --context stage=omni-domain \
     --context agenticai/omniDomainName=<org-unique-name> \
     --context agenticai/identityCenterInstanceArn=arn:aws:sso:::instance/ssoins-...
   ```
   Record the `OmniDomainId` output.
3. **Audit account (hub Region).**
   ```bash
   npx cdk deploy --context stage=omni-hub \
     --context agenticai/omniHubAccountId=<audit-account> \
     --context agenticai/omniDomainId=d-... \
     --context agenticai/organizationId=o-... \
     --context "agenticai/omniSourceScope=OrganizationalUnitId IN ('ou-platform','ou-workstreams')" \
     --context agenticai/omniSourceRegions=eu-west-1,eu-west-3
   ```
   Then, with the hub account's credentials:
   ```bash
   python3 scripts/omni/omni_space_bootstrap.py --region eu-west-1 dataset-integration create \
     --role-arn <OmniDatasetIntegrationRoleArn output>
   python3 scripts/omni/omni_space_bootstrap.py --region eu-west-1 access-grants apply \
     --domain-id d-... --space-id <OmniSpaceId output> --grants-file assets/omni/grants.json
   python3 scripts/omni/omni_space_bootstrap.py --region eu-west-1 inventory --domain-id d-... --space-id <id>
   ```
   Start from `assets/omni/grants.example.json`. Put a data scope on every grant a scoped team receives, including group grants: grants only add, and one unscoped grant restores the full view.
4. **Workstream cells.** Add to the Workload pipeline context: `agenticai/enableTransactionSearch=true`, `agenticai/runtimeKeepPromptContentInSpans=true` (only for versions the cell's classification allows to be evaluated), `agenticai/runtimeRedactSpanAttributes='["gen_ai.input.messages","llm.input_messages.*"]'` as the team declares.

Context keys for the hub stage: `agenticai/omniSpaceName`, `agenticai/omniAllowModelInference` (ADR-16, default false), `agenticai/omniJudgeModelArns`, `agenticai/omniEnableAwsIntegrationDiscovery`, `agenticai/omniBackupRegion`, `agenticai/omniRetainKeys` (default true), `agenticai/transactionSearchIndexingPercentage`.

## 3. Evaluation

| | Option A: CloudWatch Omni | Option B: AgentCore native |
| --- | --- | --- |
| Where the configuration lives | Hub space (web UI or API); reads centralized spans | `AgentCoreOnlineEvaluationConstruct` in the cell stack; reads the cell's `aws/spans` |
| Execution role | `AgentCoreEvaluationRole-omni-<region>` on the space | `EvaluationExecutionRoleConstruct` in the cell |
| Where scores appear | Omni web UI (Evaluation dashboard, per trace) | CloudWatch GenAI Observability console; cross-account through OAM |
| Results | `/aws/bedrock-agentcore/evaluations/results/<id>` or a named group; `Bedrock-AgentCore/Evaluations` or a custom namespace | same |
| Alarms | CloudWatch alarms on the metrics, composite alarm to the evaluation-gates failures topic | same (built into the construct) |
| Judge inference (ADR-16) | `allowModelInference` on the hub, exact `judgeModelArns` | `judgeModelArns` on the execution role |

Both options select an agent by its OpenTelemetry `service.name` (for an AgentCore Runtime, `<runtime-name>.<endpoint>`; the construct accepts exactly one). The code-based `PolicyCodeEvaluatorConstruct` carries the blueprint's deterministic checks (refusal on adversarial input, no system-prompt echo, bounded output, tool allow-list) and its Lambda is named `cloudwatchCodeEvaluator-*` so the Omni space policy may invoke it. Online configurations lock their evaluators while enabled: clone and repoint to change criteria.

The pre-promotion gate (`scripts/evaluation_gate.py`) stays the promotion decision.

## 4. Live gates before Omni enters the support envelope

Positive test, adversarial twin and mutation fixture for each:

1. Cross-account centralized evaluation: a Workstream cell's traces are scored by an online evaluation in the hub space, results reference the original trace and session ids; an excluded account produces no rows; disabling Transaction Search in the source stops spans.
2. Centralization completeness and lag: Runtime spans, Gateway vended logs and `/agenticai/*` logs under `/centralized/...`; metrics present with account and Region dimensions; application map spans accounts.
3. Access control: no grant -> HTTP 403 `AccessDeniedException`; a scoped Viewer sees zero rows outside its account; access profiles cannot be Space Admin.
4. Operator-role ceiling: Space Admin without the inference policy cannot invoke a model.
5. Encryption: CMK attach succeeds; narrowed key policy fails closed; disassociation requires `kms:Decrypt`.
6. SCPs: `cloudwatch:CreateDomain*` outside Management and `cloudwatch:CreateSpace` / `xray:UpdateTraceSegmentDestination` by non-pipeline principals in the Workstream OU are denied.
7. Ingestion: unsigned OTLP -> 403; removing one `xray` delivery action from a test role stops delivery.
8. Judge governance (ADR-16 option 2 only): a non-allow-listed judge fails to enable; CloudTrail shows only allow-listed `InvokeModel`.
9. Privacy: redacted attributes read `REDACTED`; content absent unless the version opted in.
10. Teardown: evaluations, grants, Dataset integration, space, rule, keys (scheduled), destination log groups removed; Transaction Search restored; zero residue.
11. Cost: measured ingestion, first-copy and storage, scan allowance, evaluation charges for one representative cell.

## 5. Known limits (2026-11)

- Omni hosting Regions: us-east-1, us-west-2, eu-west-1, eu-central-1, eu-west-2, ca-central-1, ap-south-1, ap-southeast-1, ap-southeast-2, ap-northeast-1, sa-east-1. Paris is not one (ADR-19).
- `aws-cdk-lib` 2.272.0 has no L1 for `AWS::CloudWatch::Space`, `DomainForOrganization` or `OmniDashboard`; the constructs use `CfnResource` against the published schema. `CfnOrganizationCentralizationRule` lacks `SourceContextGraphConfiguration`, so the rule is also declared with `CfnResource`.
- No CloudFormation type exists for the Dataset integration or access grants; `scripts/omni/omni_space_bootstrap.py` owns them with create/verify/delete/inventory.
- Metrics centralization copies all metrics (`*` only). Centralization never backfills. The first centralized copy is free; the Omni pricing page and the logs centralization blog disagree on cross-Region data-transfer charges for that copy, so confirm with your account team.
- Data scopes filter rows, not fields, and do not filter AWS DevOps Agent investigation content.
- Omni has no FIPS 140-3 endpoints.
