# Live evidence — pipeline-owned GA Agent Registry R2 consumer

- **Date:** 2026-09-21
- **Status:** PASS for the bounded R2 Registry and Gateway-only envelope below
- **Deployed Git HEAD:** `3870e0e5702f8ff315efe2c0fb45dbb584d8bcc6`
- **Teardown-hardening Git HEAD:** `7774299df12ce4964802d0d66d09a1b5784de5d5`
- **SDK contract:** Boto3 and Botocore `1.43.98`
- **Region:** `us-west-2`
- **Topology:** pipeline-owned Platform Registries and tools consumed by pipeline-owned nonproduction and production Workstream Tool Gateways

This is a sanitized summary. It contains no AWS account IDs, credentials,
Registry, RegistryRecord, Gateway, pipeline-execution, approval-token, KMS-key,
or other account-scoped resource identifiers. Raw evidence remained in session
scratch and was not committed.

## Preconditions and local gates

Before live deployment:

- the R2 consumer, resolver, role handoff, Gateway validator, and teardown paths
  passed the TypeScript build, lint, formatting, leakage scrub, and strict CDK
  synthesis with cdk-nag;
- 221 affected Jest assertions passed across the Registry, pipeline, Gateway,
  developer-subscription, SCP, and teardown suites;
- 138 Registry Python assertions passed across context resolution,
  compatibility, approval, and version guards;
- AWS Access Analyzer returned zero findings for six rendered R2 identity-policy
  shapes; and
- every behavior-changing fix was committed to the feature branch, passed
  exact-head CodeQL, and received an independent Critical/High review before
  the next live attempt.

The Workload pipeline was configured with stable tool IDs only. Environment-
specific RegistryRecord IDs were resolved just in time by the named pipeline
synth role and never entered a developer repository.

## Pipeline deployment and subscription results

The reviewed Platform and Workload pipelines completed the intended two-phase
handoff:

1. the Platform pipeline deployed environment-qualified tool Lambdas and
   aliases plus version `2.0.0` governance records;
2. each record was observed in `DRAFT`, explicitly submitted, and independently
   read back as descriptor-identical and `APPROVED`;
3. the Workload synth role assumed each conditioned Registry reader, resolved
   the versioned SSM pointers, verified the five ownership tags and exact
   governance documents, and emitted strict non-secret context;
4. each Workstream `RegistryRoles` stage created the stable Gateway service,
   Registry validator, and Gateway administrator roles and output the exact
   service-role ARN;
5. the Platform permission phase accepted exactly one role ARN per environment
   and granted each alias only to its matching environment principal; and
6. the Workload pipeline paused before Gateway creation and again before
   production. Each approval was bound to the current pipeline execution and
   issued only after the preceding live checks passed.

The final pipeline execution deployed both environments successfully from exact
commit `3870e0e`.

| Assertion                  |                                                      Nonproduction |                                                         Production |
| -------------------------- | -----------------------------------------------------------------: | -----------------------------------------------------------------: |
| Tool Gateway state         |                                                            `READY` |                                                            `READY` |
| Gateway target state       |                                                        2 × `READY` |                                                        2 × `READY` |
| Deletion-barrier resources |                                                        15 complete |                                                        15 complete |
| Registry records           |                                          2 × `APPROVED` at `2.0.0` |                                          2 × `APPROVED` at `2.0.0` |
| Validator binding          | exact record IDs, descriptor digests, targets, and source revision | exact record IDs, descriptor digests, targets, and source revision |
| Alias resource policies    |                             exact matching Workstream service role |                             exact matching Workstream service role |

## Positive and adversarial behavior

The nonproduction Gateway passed MCP initialization, `tools/list`, and
`tools/call` for both `echo` and `ping`. The deployed validator accepted the
exact approved records and rejected wrong descriptor digest, wrong target,
wrong tool identity, and missing-record twins. Runtime negatives also rejected
an unsigned request, a request missing the required MCP protocol header, an
unknown tool, direct Lambda invocation outside the Gateway service-role path,
and an external-account caller.

An identical redeployment was a no-op: the Gateway physical identity and both
targets remained stable, and no second create event occurred.

Production was entered only after a fresh preflight proved its exact Registry
records, descriptor digests, targets, discovery set, and alias principal. Its
Gateway and targets reached `READY`; the production validator passed the exact
positive case and rejected wrong-digest and missing-record twins.

The positive pipeline role assumption and Registry reads prove the configured
cross-account reader path. The earlier R1 run separately denied a wrong
principal and an external account. This R2 run did not independently replay
wrong-ExternalId and wrong-session-name calls from an otherwise matching
validator principal; those remain outside this bounded result.

## Fail-closed rollback and terminal-record recovery

A controlled nonproduction drift was introduced only after a Workload synth had
resolved two `APPROVED` records: `tool-echo` was changed to `DEPRECATED` while
its version, target, and descriptor remained unchanged. The deploy-time
validator rejected the status before target creation, CloudFormation rolled
back, and independent inventory found no Gateway residue.

The live service then established that `DEPRECATED` is terminal: it cannot be
returned to `DRAFT` or `APPROVED`. Commit `3870e0e` added an environment-scoped,
monotonically increasing record-generation control. The recovery:

1. deleted only the terminal nonproduction record through the governed cleanup
   path;
2. rotated only that record's CloudFormation identity;
3. updated its existing SSM ID pointer;
4. preserved the Registry, other record, tools, reader role, and DynamoDB
   rollback tables;
5. recreated the same governance document as `DRAFT`;
6. explicitly submitted and independently verified the replacement as
   `APPROVED`; and
7. restored the exact two-record discovery set before the final Workload run.

Strict template comparison showed one record removal, one descriptor-identical
record addition, one SSM pointer change, and 27 unchanged resources. The
recovery path passed 59 focused tests, build, lint, scrub, strict synth, and two
independent Critical/High reviews.

## Ordered teardown and residual inventory

Production and nonproduction were deleted separately. In both environments,
CloudFormation and CloudTrail established the required order:

1. both `DeleteGatewayTarget` calls completed;
2. `TargetDeleteBarrier` continued signed `ListGatewayTargets` polling until the
   target set was empty; and
3. only then did `DeleteGateway` begin and complete.

After both Tool Gateway stacks were absent, the two `RegistryRoles` stacks and
all six stable Workstream roles were deleted. The Platform-side Workload
pipeline root was then deleted, including its pipeline, three CodeBuild
projects, artifact bucket, generated Lambda, and IAM roles.

The first independent inventory found service-created default CloudWatch log
groups that CloudFormation does not own: 35 empty Workstream Lambda groups and
four empty Platform CodeBuild/Lambda groups. Commit `7774299` hardened
`scripts/teardown.sh` to recover exact physical IDs from deleted-stack event
history, verify the corresponding function or project is absent, deduplicate
prior generations, and delete only those exact log groups. It passed 21 focused
tests, build, lint, formatting, scrub, and exact-head CodeQL. The hardened path
removed all 39 groups, and a second independent inventory found zero unintended
residue.

The artifact KMS key was disabled and entered its configured seven-day
`PendingDeletion` window. That delayed deletion is intentional and is not
counted as residue.

## Live-discovered defects closed by the run

| Defect exposed by live AWS                                                             | Fix commit | Closure evidence                                                                        |
| -------------------------------------------------------------------------------------- | ---------- | --------------------------------------------------------------------------------------- |
| Auto-generated tool execution-role names exceeded the scoped deployment boundary       | `adcf49b`  | Explicit Lambda-only roles deployed successfully                                        |
| Registry reader could not verify ownership tags                                        | `dd012ad`  | Scoped `ListTagsForResource` read passed live synth                                     |
| Documented Platform pipeline principal did not exist                                   | `98bb7f4`  | Pipeline-owned stable role migrated in place and completed deployment                   |
| Modeled Registry hostname did not resolve; synthetic custom-resource ID broke rollback | `02e7a56`  | `.api.aws` read passed and service-minted Gateway ID drove lifecycle                    |
| Target deletion was asynchronous and could orphan a Gateway                            | `d9add20`  | Barrier-enforced target → empty poll → Gateway order passed twice                       |
| Provider waiter role could not be passed to Step Functions                             | `2e216ea`  | One-value `iam:PassedToService` correction and positive/negative IAM simulations passed |
| `DEPRECATED` Registry records cannot be restored                                       | `3870e0e`  | Single-record generation rotation, explicit reapproval, and final deployment passed     |
| Service-created log groups survived stack deletion                                     | `7774299`  | Exact history-based cleanup removed 39 empty groups with zero unintended residue        |

## Bounded conclusion and remaining gates

This proves the pipeline-owned GA Agent Registry R2 consumer and per-workstream
AgentCore Tool Gateway slice in one `us-west-2` test topology: exact
cross-account resolution, role/permission handoff, approved-record and target
binding, nonproduction and production deployment, MCP positives and denial
twins, no-op redeployment, fail-closed status drift, deterministic terminal-
record recovery, dependency-ordered teardown, and independent zero-unintended-
residue inventory.

It does **not** prove:

- pipeline-owned Gateway PolicyEngine integration or retirement of the Lambda
  Cedar wrapper;
- AgentCore Runtime and Memory integration through this Workload pipeline;
- a live deployment back to the legacy consumer mode (the exact-head rollback
  assembly and its evaluation → approval → canary → soak ordering passed strict
  synthesis only);
- wrong-ExternalId and wrong-session-name denials from an otherwise matching
  live validator principal;
- SCPs 01–12 as an organization-level live soak;
- an EMEA region matrix, load/concurrency/soak, quota, chaos, upgrade, or
  interrupted-deployment campaign;
- Gateway OTEL rate-limit span correlation, which remains a reproduced blocker;
  or
- a measured 24-hour cost baseline.

R2 is therefore live-verified for the bounded Registry and Gateway-only envelope
above. It is not a broad-adoption or zero-defect claim for the complete target
platform.
