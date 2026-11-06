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
  pre-deploy ............... scripts/omni/omni_space_bootstrap.py centralization ensure-service-linked-roles
  post-deploy .............. scripts/omni/omni_space_bootstrap.py: Dataset integration, access grants
  adopt mode ............... agenticai/omniExistingSpaceId=<uuid>: the account already hosts a space
                             (for example an account-level domain created in the console); the stack
                             deploys only the centralized-logs CMK and the centralization rule
  existing ................. AuditStack OAM sink (unchanged)

Platform and Workstream accounts (sources)
  WorkstreamObservabilityStack ... OAM link (unchanged) + opt-in AWS::XRay::TransactionSearchConfig
  D03WorkstreamRuntimeMemoryStack  AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT / AWS_REDACT_SPAN_ATTRIBUTES (opt-in)
  RuntimeExecutionRole ........... unchanged (already carries the four xray delivery actions)
```

The centralization rule replicates the blueprint's log groups (`/agenticai/*`, `/aws/bedrock-agentcore/*`, `/aws/vendedlogs/bedrock-agentcore/*`, `aws/spans`), all metrics and the Context Graph into the hub under `/centralized/<account>/<region><logGroup>`. Centralized rows carry `@aws.account` and `@aws.region`; the Omni **By account** data scope uses `cloud.account.id`.

## 2. Deploy order

1. **Prerequisites (one time).** Organizations trusted access for `observabilityadmin.amazonaws.com`; register the Audit account as CloudWatch delegated administrator (not needed when the management account itself is the hub, as in the live validation); Identity Center multi-Region replication if spaces will live outside the Identity Center Region.
   - Enabling trusted access from the **console** also creates the two service-linked roles. Enabling it through the Organizations API does not, and the rule's tag-propagation role cannot be created while `AWSServiceRoleForObservabilityAdmin_LogsCentralization` is absent (IAM refuses a trust policy whose principal does not exist). Run, with the hub account's credentials:
     ```bash
     python3 scripts/omni/omni_space_bootstrap.py --region eu-west-1 centralization ensure-service-linked-roles
     ```
     The command is idempotent (`created` or `exists` per role).
   - After trusted access is enabled the service initializes centralization across the organization. Until it finishes, `CreateCentralizationRuleForOrganization` answers HTTP 400 `Centralization is currently initializing. Please try again later.` and the hub stack rolls back. In the live run the state did not clear by itself in 35 minutes; disabling and re-enabling trusted access for `observabilityadmin.amazonaws.com` (with the service-linked roles already present) cleared it within five minutes. Suspended accounts in the organization are the documented cause of a longer stall.
   - **One domain per organization.** An account-level Omni domain already present in the management account blocks `CreateDomainForOrganization` (HTTP 409 `A domain already exists for this organization`). Either delete that domain first or run the hub stage in adopt mode against its space (step 3a).
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

   **3a. Adopt mode.** When the hub account already hosts a space (and so cannot take the organization domain), pass the space id instead of the space settings; the stack then owns only the centralized-logs CMK and the rule, and refuses `omniSpaceName`, `omniAllowModelInference`, `omniJudgeModelArns`, `omniEnableAwsIntegrationDiscovery` and `transactionSearchIndexingPercentage`:
   ```bash
   npx cdk deploy --context stage=omni-hub \
     --context agenticai/omniHubAccountId=<hub-account> \
     --context agenticai/omniDomainId=d-... --context agenticai/omniExistingSpaceId=<space uuid> \
     --context agenticai/organizationId=o-... \
     --context "agenticai/omniSourceScope=AccountId IN ('<platform>','<workstream>')" \
     --context agenticai/omniSourceRegions=eu-west-1
   ```
   Two more context keys exist for the organization-level stages: `agenticai/omniSynthesizer=cli-credentials` deploys with the operator's own credentials while the scoped CloudFormation execution policy has not been re-rendered with the Omni role families (`pipelines/bootstrap/render-cfn-execution-policy.py` now includes them), and `agenticai/omniEnableTransactionSearch=false` leaves Transaction Search alone in a hub account that already owns it (the setting is account-wide and must have exactly one owner).
4. **Workstream cells.** Add to the Workload pipeline context: `agenticai/enableTransactionSearch=true`, `agenticai/runtimeKeepPromptContentInSpans=true` (only for versions the cell's classification allows to be evaluated), `agenticai/runtimeRedactSpanAttributes='["gen_ai.input.messages","llm.input_messages.*"]'` as the team declares.

Context keys for the hub stage: `agenticai/omniSpaceName`, `agenticai/omniAllowModelInference` (ADR-16, default false), `agenticai/omniJudgeModelArns`, `agenticai/omniEnableAwsIntegrationDiscovery`, `agenticai/omniBackupRegion`, `agenticai/omniRetainKeys` (default true), `agenticai/transactionSearchIndexingPercentage`, `agenticai/omniEnableTransactionSearch` (default true), `agenticai/omniExistingSpaceId` (adopt mode), `agenticai/omniSynthesizer` (`bootstrap` | `cli-credentials`).

Destination log-group names follow `/centralized/${source.accountId}/${source.region}${source.logGroup}` (the documented AWS example form). A source group without a leading slash lands without a separator: the cell's `aws/spans` becomes `/centralized/<account>/eu-west-1aws/spans`. Every ARN the blueprint grants on centralized span groups uses `/centralized/*aws/spans`, which matches that name; use the same wildcard in any policy of your own.

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

An online evaluation reads two things and fails a trace without either: the spans in `aws/spans`, and the GenAI prompt/completion **log events** that the AWS Distro for OpenTelemetry mirrors out of those spans (an AgentCore Runtime writes them to its runtime log group; an agent elsewhere must run the distro with the OTLP **logs** exporter on, `x-aws-log-group`/`x-aws-log-stream` headers naming an existing group and stream). `logGroupNames` on `AgentCoreOnlineEvaluationConstruct` must list that group next to `aws/spans`; with spans alone every sampled trace ends in `LogEventMissingException` and the result group stays empty. Results arrive after the session idle timeout plus roughly ten minutes of processing.

What a **code-based evaluator** receives (recorded live through the on-demand `Evaluate` API, 2026-11-06): `evaluationInput.sessionSpans` holds the session's span records only, in the `aws/spans` shape with snake_case duplicates (`span_id`, `trace_id`, `session_id`) added. The GenAI log-event records are joined onto their span under **`span_events`**, each keeping the record's `body` (`input.messages`, `output.messages`; the Strands content is a JSON-encoded list of `{text}` parts) and its `attributes` (`event.name`, `session.id`); the OpenTelemetry `events` list is empty because the distro moved the content out. `PolicyCodeEvaluatorConstruct`'s function reads span attributes, `events`, `span_events` and record bodies, so it scores on this shape and on the SDK's normalized `gen_ai.user.message` / `gen_ai.choice` events. Before that fix every live trace scored `MISSING_OUTPUT_CONTENT` from the evaluator itself: run `bedrock_agentcore.evaluation.client.EvaluationClient.run(...)` against one session to check a new evaluator's contract in seconds instead of waiting for the online cycle.

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

## 5. Live validation (2026-11-06, eu-west-1)

The hub model and option B were exercised end to end in a three-account organization: the management account as hub (adopt mode against a pre-existing account-level space), one Platform-role member and one Workstream-role member as sources. The harness lives in `scripts/omni/live/` and every step is a real API call; nothing is mocked.

```bash
# hub account (management or delegated administrator)
python3 scripts/omni/omni_space_bootstrap.py --region eu-west-1 --profile <hub> centralization ensure-service-linked-roles
npx cdk deploy --profile <hub> -c stage=omni-hub -c agenticai/omniHubAccountId=<hub> -c agenticai/omniDomainId=d-... \
  -c agenticai/omniExistingSpaceId=<space uuid> -c agenticai/organizationId=o-... \
  -c "agenticai/omniSourceScope=AccountId IN ('<platform>','<workstream>')" -c agenticai/omniSourceRegions=eu-west-1 \
  -c agenticai/omniRetainKeys=false -c agenticai/omniSynthesizer=cli-credentials AgenticAI-Omni-HubStack-eu-west-1

# workstream cell: Transaction Search opt-in, policy code evaluator, execution role, online evaluation (option B)
npx cdk deploy --profile <cell> --app "npx ts-node -r tsconfig-paths/register scripts/omni/live/cell_validation_app.ts" \
  -c account=<workstream> -c region=eu-west-1 AgenticAI-OmniValidation-Cell

# a real Strands agent (Amazon Nova Micro, two tools, one adversarial turn) instrumented with ADOT, run anywhere
AGENT_PYTHON=<python with strands-agents + aws-opentelemetry-distro> bash scripts/omni/live/run_validation_agent.sh \
  --profile <cell> --region eu-west-1 --log-group /agenticai/omni-validation/omni-validation-agent --rounds 2

# the gates (evidence carries account suffixes only)
python3 scripts/omni/live/omni_live_gates.py --region eu-west-1 --hub-profile <hub> --cell-profile <cell> \
  --platform-profile <platform> --cell-account <workstream> --platform-account <platform> \
  --hub-key-arn <OmniCentralizedLogsKeyArn output> --online-config-id <OnlineEvaluationConfigId output> --out omni-gates.json
```

What passed, with the figures from the run:

| Gate | Result |
| --- | --- |
| G1 cell telemetry | 200 spans for the validation service in the cell's `aws/spans` (`gen_ai.*`, `session.id`, `aws.service.type=gen_ai_agent`), 20 structured turn events in `/agenticai/omni-validation/...` |
| G2 hub copies | both destination groups created by the rule in the hub, 200 and 20 events copied, every group encrypted under the hub CMK, the five allocation tags propagated to the copy of the tagged source group (`TagPropagationStatus: Healthy`) |
| G3 rule | `RuleHealth: Healthy` on `agenticai-fleet-eu-west-1`; the live rule carries `SourceMetricsConfiguration`, `EncryptionScope: NEW_DESTINATION_LOG_GROUPS` and the tag-propagation role |
| G4 Omni Dataset | an Omni telemetry query (`SELECT @aws.account, @logGroupName, COUNT(*) FROM default WHERE @timestamp BETWEEN ... AND @logGroupName LIKE '/centralized/%'`) over the hub Dataset returned rows for both source accounts and none for the hub itself |
| G5 option B | the AgentCore online evaluation sampled the agent's traces, invoked the model-free policy code evaluator (Lambda `cloudwatchCodeEvaluator-omni-validation`) and wrote one result record per trace to `/aws/bedrock-agentcore/evaluations/results/<config id>`; the result group was itself centralized into the hub. The first cycles scored `error.type=MISSING_OUTPUT_CONTENT` because the evaluator read span attributes only (§3); after the fix the on-demand `Evaluate` API scored all four traces of a session `PASS` (1.0), the adversarial turn included because the agent refused, and the online configuration scored the next session the same way |
| G6 second source | three probe events written to `/agenticai/omni-validation/platform-probe` in the Platform member appeared under `/centralized/<platform>/eu-west-1/...` within one minute |
| G7 isolation | from the Platform member: `GetCentralizationRuleForOrganization` -> HTTP 403 `AccessDeniedException`; `FilterLogEvents` on the hub's centralized span group -> `AccessDeniedException` |

Not covered by this run, and still outside the support envelope: option A (an online evaluation created inside the space; the pre-existing account-level domain in the test organization blocked the organization domain), grant isolation through Identity Center principals (the adopted space signs in through IAM), the CMK fail-closed and SCP gates, metrics centralization inspection, a `FAIL` verdict from the policy evaluator on live traffic (covered by its unit tests only), and cost measurement.

Teardown order used: delete `AgenticAI-OmniValidation-Cell` (removes the online evaluation, evaluator, Lambda, role and flips Transaction Search back off), delete `AgenticAI-Omni-HubStack-eu-west-1` (rule, tag-propagation role, CMK scheduled for deletion), delete the `/centralized/...` destination groups and the Platform probe group by hand (the rule never deletes what it created), restore the Transaction Search indexing baseline, and remove the two service-linked roles only if nothing else in the account uses centralization.

## 6. Known limits (2026-11)

- Omni hosting Regions: us-east-1, us-west-2, eu-west-1, eu-central-1, eu-west-2, ca-central-1, ap-south-1, ap-southeast-1, ap-southeast-2, ap-northeast-1, sa-east-1. Paris is not one (ADR-19).
- `aws-cdk-lib` 2.272.0 has no L1 for `AWS::CloudWatch::Space`, `DomainForOrganization` or `OmniDashboard`; the constructs use `CfnResource` against the published schema. `CfnOrganizationCentralizationRule` lacks `SourceContextGraphConfiguration`, so the rule is also declared with `CfnResource`.
- No CloudFormation type exists for the Dataset integration or access grants; `scripts/omni/omni_space_bootstrap.py` owns them with create/verify/delete/inventory.
- Metrics centralization copies all metrics (`*` only). Centralization never backfills. The first centralized copy is free; the Omni pricing page and the logs centralization blog disagree on cross-Region data-transfer charges for that copy, so confirm with your account team.
- Data scopes filter rows, not fields, and do not filter AWS DevOps Agent investigation content.
- Omni has no FIPS 140-3 endpoints.
- Omni telemetry queries are SQL against the account's Dataset: the table is `default` (or `logs.default`), the time range must be an explicit `` `@timestamp` BETWEEN to_timestamp('...') AND to_timestamp('...') `` (at most 365 days), and `StartTelemetryQuery` needs a session from `StartTelemetryQuerySession` (session names are unique per account; reuse one). Every query scans the whole Dataset for the range (3.3 GB for three days of one account in the live run), so keep gate queries narrow in time.
- SDK models lag the API: the AWS CLI shipped with this run (2.34.29) parsed a centralization rule without `SourceMetricsConfiguration`, `TagPropagationConfiguration` or `EncryptionScope` even though the raw response carried them, and had no `cloudwatchomni` command at all. `omni_space_bootstrap.py` refuses to re-submit a rule through a model that lacks those members; use boto3 >= 1.43 for everything Omni.
- `Centralization is currently initializing` (HTTP 400) after enabling trusted access through the API can persist until trusted access is cycled once the service-linked roles exist (§2).
- The CloudWatch Logs OTLP endpoint (`/v1/logs`) writes only into an existing log stream named by `x-aws-log-stream`; a missing stream is a 400 for every batch.
