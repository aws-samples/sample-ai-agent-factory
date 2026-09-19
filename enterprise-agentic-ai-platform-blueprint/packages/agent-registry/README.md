# `@agenticai/agent-registry`

Registry governance constructs and helpers for the enterprise AgentCore platform.

## Migration state

The package currently exposes two generations while the migration is proven
blue-green:

- `PlatformRegistryConstruct` / `RegistryRecordConstruct` are the deprecated
  public-preview custom-resource path. Do not add new consumers.
- `GaPlatformRegistryConstruct` is the native GA producer used by the
  pipeline-owned `RegistryStack` in revision R1.

R1 is additive: the existing DynamoDB registry tables and their logical IDs stay
unchanged as the rollback path. Workstreams continue to use the old consumer
until R2 passes Platform deployment, record approval, cross-account read,
negative authorization, rollback, and teardown gates.

## Native GA producer

For each Platform environment, `GaPlatformRegistryConstruct` creates:

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

Its identity policy uses the GA `agent-registry` namespace and contains read and
discovery actions only. It has no create, update, submit, approve, or delete
action.

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

## Current proof boundary

The isolated API contract passed in `us-west-2` on exact product commit
`8e66dc3`; see
[`../../evidence/live/2026-09-19-agent-registry-compatibility-spike.md`](../../evidence/live/2026-09-19-agent-registry-compatibility-spike.md).

The pipeline-owned R1 producer is locally synthesized and tested but is not
live-verified until its reviewed Platform pipeline deployment completes. The R2
Workstream consumer, EMEA regions, load/chaos behavior, and final placeholder
retirement remain release gates.
