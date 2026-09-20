# Live evidence — pipeline-owned GA Agent Registry R1

- **Date:** 2026-09-20
- **Status:** PASS for the bounded R1 producer envelope below
- **Producer Git HEAD:** `f3ec7d6353f8760822c204ad668d02870999cb71`
- **Approval utility Git HEAD:** `39a13ab7a1466db874e07dd293bc46fe4546e6ac`
- **SDK contract:** Boto3 and Botocore `1.43.98`
- **Region:** `us-west-2`
- **Topology:** pipeline-owned nonproduction and production GA Registries alongside unchanged DynamoDB rollback tables

This is a sanitized summary. It contains no AWS account IDs, credentials,
Registry or RegistryRecord IDs, ARNs, approval tokens, or other account-scoped
resource identifiers. Raw evidence remained in session scratch and was not
committed.

## Preconditions

Before any Registry record submission:

- producer commit `f3ec7d6` passed exact-head PR CI, 70 related tests, strict
  deployed-context pipeline synthesis, cdk-nag, and AWS Access Analyzer;
- the Platform CloudFormation execution policy was expanded only with four
  tag-scoped GA Registry lifecycle statements and independently verified after
  IAM propagation;
- the approval utility passed 77 focused tests, 107 combined Registry tests,
  TypeScript build, lint, leakage scrub, and exact-head PR CI;
- the utility loaded the processed CloudFormation template and required each
  live governance document to match it JSON-semantically, including target ARN,
  MCP schema, Cedar policy, entitlements, and ownership; and
- both environments were independently observed with exactly two records in
  `DRAFT` before submission.

The first read-only utility run exposed one response-model defect: the GA
`GetRegistry` operation returns `authorizerType` under
`discoveryConfiguration`, not at the top level. The run failed before any
submission. Commit `39a13ab` corrected the parser, added a live-shaped
regression, passed all local gates and exact-head CI, and then passed the fresh
read-only preflight.

## Pipeline deployment results

The reviewed Platform pipeline execution sourced exact producer commit
`f3ec7d6` and completed:

1. Source and Synth;
2. SelfMutate and asset publication;
3. nonproduction Registry deployment;
4. explicit `Prod.SecurityReview`; and
5. production Registry deployment.

The final pipeline status was `Succeeded`. The already-deployed Guardrail and
inference Gateway actions also remained successful.

| Assertion | Nonproduction | Production |
|---|---:|---:|
| Registry stack | `UPDATE_COMPLETE` | `UPDATE_COMPLETE` |
| Native Registry count | 1 | 1 |
| RegistryRecord count | 2 | 2 |
| Initial record state | both `DRAFT` | both `DRAFT` |
| Explicit submissions | 2 | 2 |
| Final record state | both `APPROVED` | both `APPROVED` |
| Exact discoverable set | 2 approved records | 2 approved records |
| Versioned SSM parameters | 6 | 6 |
| Governance descriptor digests | exact template match | exact template match |

Both environments produced the same two template-bound descriptor SHA-256
values:

- `tool-echo`: `c7d7ec17c457d772efec4f8a74034197c28f0f238ce23eaeb553b2db763e2224`
- `tool-ping`: `7bcf341c19246e16560e197c72aab38812019c02e76856d01b3083041b50baf6`

A second verifier, independent of the approval utility, read both control-plane
records and the discovery data plane after each approval. It required exact
record IDs, names, `CUSTOM` type, `APPROVED` status, descriptor digests, and an
exact two-record discovery set.

## Trust and rollback-path checks

The live `RegistryReaderRole` trust denied both:

- the configured Workstream account's existing `Admin` principal, even with the
  correct ExternalId and `registry-*` session name, because its principal ARN
  does not match `AgenticAI-D03-*-RegistryValidator`; and
- the external adversarial account with the correct ExternalId and session-name
  pattern, because that account is not trusted.

No matching Workstream validator role exists yet. Creating one manually would
violate the blueprint's mandatory GitHub PR → Workload pipeline mutation path.
The positive assume/read case and wrong-ExternalId/session-name twins therefore
remain R2 gates and must use the validator role created by the reviewed Workload
pipeline.

The nonproduction stack retained both original DynamoDB table physical names;
both tables remained `ACTIVE`. The R1 inventory contained exactly one expected
nonproduction Registry, two records, six SSM parameters, and one reader role
before production deployment. No compatibility-spike resource remained. R1 did
not switch a Workstream consumer, so the legacy consumer path remained untouched.

## Bounded conclusion and remaining gates

This proves the pipeline-owned R1 producer, explicit record approval, exact
processed-template binding, independent control/data-plane read-back, trust
negative twins, and additive rollback-path preservation in one `us-west-2`
Platform test topology.

It does **not** yet prove:

- a positive cross-account read from the pipeline-created Workstream validator;
- wrong-ExternalId and wrong-session-name denials from that matching principal;
- the R2 Workstream consumer switch or rollback to the legacy consumer;
- Workstream Gateway, PolicyEngine, Runtime, Memory, or generated-agent parity;
- EMEA regional compatibility, load, chaos, upgrade, or soak behavior; or
- final multi-account teardown.

Those remain release blockers. R1 is a verified producer baseline, not a
broad-adoption readiness claim.
