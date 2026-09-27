# `@agenticai/agent-registry`

Registry governance constructs and helpers for the enterprise AgentCore platform.

## Migration state

The package currently exposes two generations while the migration is proven
blue-green:

- `PlatformRegistryConstruct` / `RegistryRecordConstruct` are the deprecated
  public-preview custom-resource path. Do not add new consumers.
- `GaPlatformRegistryConstruct` is the native GA producer used by the
  pipeline-owned `RegistryStack` in revision R1.

R1 remains additive: the existing DynamoDB registry tables and their logical
IDs stay unchanged as the rollback path. R2 now consumes the GA records through
the reviewed Workload pipeline; the old consumer remains available until a live
rollback deployment passes and maintainers explicitly retire it.

## Native GA producer

For each Platform environment, `RegistryStack` now creates:

- one CMK-backed, environment-qualified Lambda + `PROD` alias per platform tool,
  using an explicit `AgenticAI-Platform-<environment>-<tool>-exec` role scoped
  only to that function's pre-created log streams;
- exact alias resource policies for the corresponding Workstream Gateway role;
- one IAM-authorized `AWS::AgentRegistry::Registry`;
- one tagged `CUSTOM` governance `RegistryRecord` per platform catalogue tool;
- one read-only `AgenticAI-RegistryReader-<environment>` role;
- versioned SSM parameters under `/agenticai/registry/v1/<environment>/` for
  the Registry ID/ARN, reader role/ExternalId, and every generated record ID.

The reader trust requires all of:

1. a configured Workstream account principal;
2. the exact environment/account-derived `sts:ExternalId`;
3. an `AgenticAI-D03-*-RegistryValidator` principal ARN; and
4. a `registry-*` role-session name.

Its identity policy uses the GA `agent-registry` namespace and contains record,
discovery, and ownership-tag read actions only. `ListTagsForResource` is scoped
to the exact Registry ARN plus its generated `/record/*` family. It has no
create, update, submit, approve, or delete action.

Native Registry resources and late-binding parameters use
`RetainExceptOnCreate`: failed first creation cleans itself up, while a later
stack rollback cannot delete approved governance state. The IAM reader role is
not retained independently.

## Governance document

`buildGaToolGovernanceDocument()` validates the existing `ToolSpec`, resolves
its target ARN, and stores an opaque `agenticai.tool-governance/1.0` document
with target, MCP schema, Cedar metadata, desired approval state, and ownership.
Records remain `DRAFT` after CloudFormation creation; a curator must explicitly
submit them. `APPROVE_ALL` then transitions a valid submission to `APPROVED`.

## R2 Workstream consumer

R2 is opt-in through `agenticai/enableGaRegistryConsumer=true`. Developer repos
store stable tool IDs in `agenticai/gaRegistryExpectedToolIds`; opaque record IDs
remain environment-specific and are never committed by developers.

The Workload pipeline synth:

1. uses the exact named `AgenticAI-WLP-<tenant>-<agent>-RegistrySynth` role;
2. assumes each environment's `RegistryReaderRole` with a distinct synth
   ExternalId and `registry-synth-*` session name;
3. reads only the six versioned SSM parameters and expected approved records;
4. validates complete `agenticai.tool-governance/1.0` documents; and
5. writes strict non-secret context files consumed by CDK.

The Workstream stack builds exact target ARNs, MCP schemas, and Cedar from that
context. A pipeline-created
`AgenticAI-D03-<environment>-<tenant>-<agent>-RegistryValidator` role then
re-fetches each record through `agent-registry-control.<region>.api.aws` at
deployment (SigV4 service `agent-registry`) and
requires `APPROVED` status, the exact synth-time descriptor SHA-256, and an
explicit match between the live governance target ARN and the Gateway target.
This closes status, descriptor, and target drift between synth and deploy.

Before Gateway deployment, the Workload `RegistryRoles` stage emits one exact
`GatewayServiceRoleArn` per environment (plus a diagnostic
`GatewayServiceRoleId`) and pauses. The Platform permission phase accepts
exactly those two ARNs through `agenticai/gaGatewayServiceRoleArns`, validates
account/name/environment cardinality, and grants each environment's aliases only
to its matching role. It never derives a principal from independent Platform
tenant/agent settings. Lambda stores each grant against the role's RoleId, so the
phase must be switched off (statements removed) before a granted role is deleted
and on again once it exists; a grant left behind blocks every later
`AddPermission` on that alias until the same two-phase toggle removes it.
After nonproduction MCP proof, GA mode uses `ProdGatewayApproval`; app-only
evaluation/canary gates remain exclusive to legacy/full-agent mode.

Catalogue revision 2 emits RegistryRecord version `2.0.0` and environment-
qualified Platform tool Lambdas and Gateway roles. The old `allowedToolIds`
catalogue consumer path was retired on 2026-09-25; the GA Registry context is
the only subscription source.

## Validation boundary

The Registry test suites cover:

- native Registry and `CUSTOM` governance-record creation;
- observed `DRAFT`, explicit submission, approval, and discovery;
- byte-semantic descriptor and allocation-tag verification;
- conditioned `RegistryReaderRole` trust;
- stable tool-ID resolution through versioned SSM parameters;
- deploy-time `APPROVED`, version, digest, and target-ARN checks;
- wrong-account, wrong-ExternalId, and wrong-session-name denials;
- terminal-record generation recovery;
- target → barrier → Gateway teardown and residual inventory.

The complete producer/consumer flow has been exercised through nonproduction
and production pipelines in the supported Ireland reference topology. A
customer deployment must repeat the same checks in its own accounts and Region.
The retained DynamoDB placeholder is a migration rollback surface, not the tool
governance source of truth.
