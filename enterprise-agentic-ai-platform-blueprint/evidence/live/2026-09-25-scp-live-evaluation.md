# Live evidence — SCP 01–12 evaluation with AWS's policy evaluators

- **Date:** 2026-09-25
- **Status:** PASS WITH FINDINGS — twelve defects found and fixed (three would
  have locked workloads out if attached, one let workloads bypass the
  guardrail entirely); after the fixes all evaluator twins pass, IAM Access
  Analyzer reports only a documented SCP-12 coverage warning, and a live
  Organization soak with nine SCPs attached passed 12/12 real-call twins, the
  governed agent sessions in both environments and a full Workload pipeline
  run
- **Evaluators:** IAM Access Analyzer `ValidatePolicy` with
  `policyType=SERVICE_CONTROL_POLICY`, and IAM `SimulateCustomPolicy` with each
  SCP (plus `FullAWSAccess`) as the permissions boundary under an allow-all
  identity policy, so the decision is exactly the SCP's effect
- **Input:** the twelve SCPs rendered by `buildScpSet` with this deployment's
  values (allow-listed models, approved guardrail, Workstream account ids,
  tool ARNs, VPC endpoint ids)

## Why the unit tests did not catch these

The 76 unit and regression tests pinned the JSON _shape_ of each SCP. Shape
tests cannot tell that a condition key does not exist, that an absent key
makes `ForAllValues:` true, or that a create call authorizes against `*`.
The earlier sandbox soak script only asserted denials, and one of its four
commands (`aws bedrock-agentcore create-agent-runtime`) does not exist in the
AWS CLI, so it "passed" by failing. An SCP that denies everything passes a
deny-only soak.

## Defects found and fixed

| #   | SCP        | Defect                                                                                                                                                                       | Effect if attached                                                                                                                                                | Fix                                                                                                               |
| --- | ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| D1  | 01         | Conditioned on `bedrock:FoundationModel`, which is not a Bedrock condition key; with `ForAllValues:` an absent key evaluates true                                            | **Lockout**: every model denied, allow-listed ones included (evaluator: allow-listed model → `explicitDeny`)                                                      | Allow-list on the resource: `NotResource` = exact foundation-model ARNs + the matching inference-profile ARNs     |
| D2  | 01, 02, 04 | Listed `bedrock:Converse` / `ConverseStream`, which are not IAM actions                                                                                                      | Access Analyzer `INVALID_ACTION`                                                                                                                                  | Removed; Converse authorizes as `InvokeModel*`                                                                    |
| D3  | 02         | `ForAllValues:` on the single-valued `GuardrailIdentifier`                                                                                                                   | Overly permissive (Access Analyzer)                                                                                                                               | `ArnNotLike` on `<arn>` and `<arn>:<version>`                                                                     |
| D4  | 02         | An ARN operator does not match an empty value                                                                                                                                | `GuardrailIdentifier=""` passed (evaluator)                                                                                                                       | Explicit `StringEquals ""` deny twin                                                                              |
| D5  | 03         | `{{resolve:ssm:…}}` of a **StringList** parameter resolves to one comma-joined string, and the parameter lives in the Workload account while the SCP deploys from Management | **Lockout**: every AgentCore call denied                                                                                                                          | Explicit endpoint ids rendered as a real list; SCP-03/04 emitted only when supplied                               |
| D6  | 03, 04     | VPC-endpoint controls on an architecture whose Runtime runs `networkMode: PUBLIC`                                                                                            | **Lockout** of the reference deployment's own Memory, Identity and Gateway calls and of the guardrail interceptor's `ApplyGuardrail` (evaluator, real principals) | Documented as VPC-mode-only controls, off by default                                                              |
| D7  | 09, 11     | Create actions authorize against `*`, but the statements were scoped to `gateway/*` / `registry/*`                                                                           | Anyone could create a rogue Gateway or Registry (evaluator: developer create → `allowed`)                                                                         | Separate `*`-scoped create statements                                                                             |
| D8  | 11         | Denied only `bedrock-agentcore:*Registry*`; the deployed GA Registry signs as `agent-registry`                                                                               | Governed nothing that is deployed                                                                                                                                 | Both namespaces covered; the Platform pipeline's CloudFormation execution role exempted (it deploys the Registry) |
| D9  | 12         | Listed `Create*` and `organizations:*` under a resource-tag condition                                                                                                        | Never matchable (a resource being created carries no tag; member accounts cannot call Organizations writes)                                                       | Removed; the remaining coverage limit documented                                                                  |
| D10 | set        | AWS Organizations allows 10 SCPs per OU including `FullAWSAccess`; the full set is 12                                                                                        | Deploy failure if all were attached                                                                                                                               | `SCP_MAX_ATTACHED_PER_TARGET` guard in the construct; README states which SCPs the reference construct attaches   |
| D11 | 07         | Always emitted, but denies `CreateAgentRuntime`/`UpdateAgentRuntime` without subnets                                                                                         | **Lockout** of the pipeline's own deployment of the PUBLIC-network reference Runtime                                                                              | Gated on the VPC-mode endpoint ids, like SCP-03/04                                                                |
| D12 | 02         | Governed only `bedrock:InvokeModel*`; Bedrock Mantle has no guardrail condition key                                                                                          | A workload principal could call `bedrock-mantle:CreateInference` directly and skip the guardrail and the model allow-list (live baseline: **HTTP 200**)           | `DenyDirectMantleInference`: only `AgenticAI-InferenceGateway-*` roles may call Mantle (live with SCPs: HTTP 403) |

## Result after the fixes

| Check                            | Result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Access Analyzer, SCP-01 … SCP-11 | no findings                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Access Analyzer, SCP-12          | `DENY_WITH_UNSUPPORTED_TAG_CONDITION_KEY_FOR_SERVICE` — some matched actions do not populate `aws:ResourceTag`; for those the deny does not fire. Documented in the SCP; the permission set's identity policies remain the primary control                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| IAM evaluator twins              | **43/43** (21 deny, 22 allow) across all twelve SCPs: unlisted model and unlisted-model profile denied, listed model / profile / streaming allowed; missing, rogue and empty guardrail denied, approved (and versioned) guardrail allowed; wrong or missing VPCE denied, each approved VPCE allowed; non-admin guardrail change denied, admin role and session allowed; unapproved region denied, approved region and global services allowed; public Runtime denied, VPC Runtime allowed; ECR Public denied, private ECR allowed; non-admin and cross-account Gateway mutation and rogue Gateway creation denied, pipeline GatewayAdmin allowed; uncatalogued Lambda denied for runtime roles, catalogued allowed; developer GA Registry mutation, approval and creation denied, Platform CloudFormation role and RegistryAdmin allowed, record publishing not denied; developer write on platform-tagged resource denied, on own resource allowed |
| Unit + regression suites         | 76/76 (shape tests rewritten to pin the evaluator-proven bodies); full suite 58 suites / 723 tests                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

## Organization soak — PASS (live, attached)

The Organization turned out to be the project's own (its management account
is the Audit / Log Archive account), with the Workstream test account in the
`AgenticAI-Workloads` OU and the Platform account at the root. SCPs were not
enabled on the root at all (no `FullAWSAccess` anywhere), so nothing had ever
been enforced.

A reversible soak runner (state recorded before every mutation):

1. enabled the `SERVICE_CONTROL_POLICY` type on the root (attaches
   `FullAWSAccess` everywhere, no permission change);
2. created and attached to the `AgenticAI-Workloads` OU only the nine SCPs
   that apply to this public-endpoint deployment — 01, 02, 05, 06, 08, 09, 10,
   11, 12 — which with `FullAWSAccess` is exactly the 10-per-target quota
   (SCP-03/04/07 are VPC-mode controls and are not emitted without endpoint
   ids);
3. ran the live twins below with real calls from the Workstream account;
4. ran the generated agent's governed sessions and a full Workload pipeline
   run with the SCPs in force;
5. detached and deleted the nine policies and disabled the policy type again.

Before attaching, the same calls ran as a baseline: **no** call was SCP-denied
and a direct Bedrock Mantle inference from the Workstream admin role returned
**HTTP 200** — the gap the new SCP-02 statement closes.

| SCP     | Twin  | Call from the Workstream account                                        | Result with SCPs attached                                                                 |
| ------- | ----- | ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| 06      | deny  | EC2 `DescribeAvailabilityZones` in `eu-west-1`                          | explicit SCP deny                                                                         |
| 06      | allow | the same in `us-west-2`                                                 | succeeded                                                                                 |
| 08      | deny  | ECR Public `DescribeRegistries`                                         | explicit SCP deny                                                                         |
| 08      | allow | private ECR `DescribeRepositories`                                      | succeeded                                                                                 |
| 01      | deny  | `InvokeModel` on an active unlisted model (with the approved guardrail) | explicit SCP deny                                                                         |
| 02      | deny  | `InvokeModel` on the allow-listed model without a guardrail             | explicit SCP deny                                                                         |
| 02      | deny  | the same with an unapproved guardrail ARN                               | explicit SCP deny                                                                         |
| 01 + 02 | allow | allow-listed model with the approved guardrail                          | passed SCP authorization (then rejected by Bedrock: the model needs an inference profile) |
| 02      | deny  | direct Bedrock Mantle `chat/completions` (SigV4)                        | explicit SCP deny, HTTP 403 (was HTTP 200 before attaching)                               |
| 05      | deny  | `CreateGuardrail` outside the platform admin role                       | explicit SCP deny                                                                         |
| 09      | deny  | `CreateGateway` outside the pipeline GatewayAdmin role                  | explicit SCP deny                                                                         |
| 11      | deny  | GA Registry `CreateRegistry` outside the Platform pipeline              | explicit SCP deny                                                                         |

**12/12**, and with the SCPs attached the generated agent's positive session
and the unsubscribed-tool twin passed in **both** environments (Runtime,
Memory, SigV4 MCP tool calls through the Workstream Gateway, inference through
the Platform Gateway). A full Workload pipeline run (`cf93d401` on `a096c6c`)
then deployed through both environments with the SCPs in force: the
`RegistryRoles` stacks (no change), both `ToolGateway` stacks (updated — the
Registry validator custom resources re-ran under their pipeline roles) and
both `RuntimeMemory` stacks (no change) all succeeded, so the SCPs deny the
misuse above without blocking the platform's own deployment path.

**Restoration (verified independently afterwards):** the nine policies were
detached and deleted, the `SERVICE_CONTROL_POLICY` type was disabled on the
root again, the root, the OU and the Workstream account have no attached SCPs,
and the only SCP definition left is the AWS-managed `FullAWSAccess` — exactly
the state before the soak.

## What this does not prove

SCP-10's runtime-role twins and SCP-12's Identity Center developer twins were
proven on the IAM evaluator only: the runtime role is assumable only by
AgentCore and the test accounts have no Identity Center permission sets.
SCP-03/04/07 were proven on the evaluator only, because the reference
deployment does not run in VPC mode.
