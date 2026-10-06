# Agent Registry: preview retirement and GA migration

**Status:** the preview path is removed. Nothing in this blueprint calls the
preview Registry APIs any more. If you have never deployed this blueprint, or
you never set `agenticai/d03EnableAgentRegistry=true`, there is nothing for you
to do and you can stop reading.

## What changed, and why it had to

AWS moved Agent Registry out of Amazon Bedrock AgentCore into its own GA service
on 2026-08-06:

| | Preview | GA |
|---|---|---|
| Control-plane endpoint | `bedrock-agentcore-control.<region>.amazonaws.com` | `agent-registry-control.<region>.api.aws` |
| SigV4 signing name | `bedrock-agentcore` | `agent-registry` |
| IAM actions | `bedrock-agentcore:*Registry*` | `agent-registry:*` |
| Resource ARNs | `arn:aws:bedrock-agentcore:…:registry/…` | `arn:aws:agent-registry:…:registry/…` |
| Record typing | `descriptorType` — `MCP` / `A2A` / `AGENT_SKILLS` / `CUSTOM` | `recordType` — `MCP` / `AGENT` / `SKILL` / `CUSTOM` |
| CloudFormation | custom resources | `AWS::AgentRegistry::Registry`, `AWS::AgentRegistry::RegistryRecord` |

Support for the preview APIs **ended on 2026-09-17**. The two are separate
services with separate data stores, so nothing migrates by itself.

That cutoff is the whole reason this is a removal rather than a rewrite. Code
calling the preview endpoints cannot work, so keeping it as a "rollback path"
would have been keeping a path that fails. Note the rollback this blueprint
actually documents is the DynamoDB registry in `RegistryStack`, which is
untouched and still there.

## What was removed on 2026-10-06

Deleted outright, all four with no remaining callers:

- `packages/agent-registry/src/platform-registry-construct.ts`
- `packages/agent-registry/src/registry-record-construct.ts`
- `packages/agent-registry/src/registry-consumer-grant.ts` — already dead code, zero callers
- `packages/agent-registry/src/registry-record-spec.ts` — the `descriptorType` model

Also removed:

- the `enableAgentRegistry` block in `apps/platform-account/lib/d03-platform-core-stack.ts`,
  with its `registryName` and `registryAutoApproveOnSeed` props, its two class
  fields, and the `GatewayAdminRole` curator grant;
- the `agenticai/d03EnableAgentRegistry`, `agenticai/d03RegistryName` and
  `agenticai/d03RegistryAutoApproveOnSeed` context keys in `bin/agentic-ai-platform.ts`;
- the `AgenticAI-D03-AgentRegistryId` and `AgenticAI-D03-AgentRegistryArn`
  CloudFormation exports. Neither had an `Fn::ImportValue` consumer anywhere,
  so no stack depended on them.

Migrated rather than deleted:

- `packages/developer-access/src/workstream-permission-sets.ts`. The Developer
  and ReadOnly Identity Center permission sets now grant the GA read and
  discovery surface, scoped to `arn:aws:agent-registry:*:<platformAccountId>:registry/*`.
  Their `DenyPlatformOwnedMutation` statement gained `agent-registry:Update*`,
  `Delete*`, `Create*` and `Put*`, because the GA service is not reached by a
  `bedrock-agentcore:*` wildcard.

Deliberately left alone:

- **SCP-11** (`scp-11-registry-mutation-lockdown.ts`) still denies **both**
  namespaces. That is intentional defence in depth, is recorded in that file,
  and is pinned by `scps.test.ts`. Do not "tidy" the preview half away.
- Every other `bedrock-agentcore` reference in the blueprint. Runtime, Gateway,
  Memory, Identity and Policy did **not** move and are correct as they are.

## Action naming: the GA surface is not a rename

This is the part most likely to trip you up. GA is not a find-and-replace of the
preview action names. Two preview actions have **no** direct GA equivalent:

| Preview | GA |
|---|---|
| `bedrock-agentcore:SearchRegistryRecords` | `agent-registry:SearchDiscoverableRegistryRecords` |
| `bedrock-agentcore:InvokeRegistryMcp` | no equivalent; discovery is the `*Discoverable*` family |
| `bedrock-agentcore:ListRegistries` | no equivalent; consumers are scoped to one registry |
| `bedrock-agentcore:GetRegistry` | `agent-registry:GetRegistry` |
| `bedrock-agentcore:ListRegistryRecords` | `agent-registry:ListRegistryRecords` |
| `bedrock-agentcore:GetRegistryRecord` | `agent-registry:GetRegistryRecord` |

The `*Discoverable*` family — `GetDiscoverableRegistryRecord`,
`ListDiscoverableRegistryRecords`, `SearchDiscoverableRegistryRecords` — returns
only APPROVED records, which is what a consumer persona should see.

The canonical grant for this repository is
`AGENT_BUILDER_INSPECT_ACTIONS` in `packages/agent-registry/src/agent-builder-inspect-role.ts`,
with the fuller split in `GaPlatformRegistryConstruct`. Copy from those rather
than inventing action names.

## If you have an existing deployment

### 1. Do you actually have preview data?

Only if you deployed `AgenticAI-D03-PlatformCoreStack` with
`agenticai/d03EnableAgentRegistry=true`. It defaulted to `false`, and no
documented workflow in this repository turned it on, so most deployments have
nothing to migrate. Confirm with:

```bash
aws cloudformation describe-stack-resources \
  --stack-name AgenticAI-D03-PlatformCoreStack \
  --query "StackResources[?starts_with(ResourceType, 'Custom::BedrockAgentCoreRegistry')].[LogicalResourceId,ResourceType]" \
  --output table
```

Empty output means you are done.

### 2. Deploy the GA registry

`AgenticAI-Platform-RegistryStack` provisions it through
`GaPlatformRegistryConstruct` and is already wired into the platform stage in
`bin/agentic-ai-platform.ts`. It seeds one record per tool from
`PLATFORM_TOOL_CATALOGUE`, the same source the preview path seeded from, so for
a catalogue-driven deployment the GA registry reaches the same contents without
any data copy.

### 3. Only if you added records outside the catalogue

Records created by hand in the preview registry are not reachable from GA and
must be re-created. Export them before the preview endpoint stops answering,
map each `descriptorType` to its GA `recordType` (`A2A` → `AGENT`,
`AGENT_SKILLS` → `SKILL`, `MCP` and `CUSTOM` unchanged), and re-publish against
GA. AWS publishes a worked example at
<https://github.com/awslabs/agentcore-samples/tree/main/01-features/07-centralize-and-govern-your-ai-infrastructure/03-registry/04-migrate-to-new-namespace>.

Note the descriptor payloads differ too, not just the type name, so this is a
re-publish rather than a field rename.

### 4. Remove the preview resources

Upgrading to this revision removes the constructs from the template, so
CloudFormation deletes the preview registry and its records on the next deploy.
If a delete fails because the preview endpoint no longer answers, the resources
are orphaned rather than blocking: set the stack's retained resources aside and
use `scripts/final_teardown.py`, which orders `AWS::AgentRegistry::RegistryRecord`
before `AWS::AgentRegistry::Registry` for the GA types.

Also drop the three retired context keys from any `cdk.context.json`,
`cdk.json` or pipeline configuration you maintain. They are now ignored, so
leaving them is harmless but misleading.

## Staying migrated

`tests/conformance/phase-l-preview-registry-retired.test.ts` is the regression
guard. It scans every tracked **and untracked** source file for
Registry-specific preview patterns — preview IAM actions, the preview ARN shape,
`Custom::BedrockAgentCoreRegistry*` resource types, `descriptorType`, and the
deleted construct names — and fails if any reappears.

It is deliberately precise rather than a ban on the string `bedrock-agentcore`,
because that namespace is still correct for Runtime, Gateway, Memory, Identity
and Policy. Its final test asserts that legitimate non-Registry AgentCore usage
is still present, so the guard cannot be satisfied by deleting good code. Its
allowlist is self-checking: an entry that stops needing its exemption fails the
suite rather than lingering.
