# ParticipantRole IAM policy — size budget and conventions

> Two unrelated policy sets live in this directory. This file is about the
> **ParticipantRole** files (`workshop-iam-policy-*.json`), which govern what a
> participant may do *inside* a Workshop-Studio-provisioned account. The
> **self-service deploy** files (`self-service-deploy-policy-*.json`) govern
> what the *deploying* principal may do on the self-paced path and are
> documented in the last section below.

The ParticipantRole permissions are split across seven files referenced from
`contentspec.yaml` (`awsAccountConfig.participantRole.iamPolicies`):

| File | Scope |
|------|-------|
| `workshop-iam-policy-core.json`    | Bedrock (model invocation, catalog, guardrail writes), AgentCore gateway/policy-engine create and read, events, memory reads, S3, STS — Identity, Secrets Manager, EventBridge and SSM moved to `-core-5.json` in 2026-11 |
| `workshop-iam-policy-core-2.json`  | IAM (roles/policies/PassRole), CloudFormation, CloudWatch Logs, AgentCore manage/delete |
| `workshop-iam-policy-core-3.json`  | Agent Registry (`agent-registry:*`, used by Module 3b), CDK bootstrap (staging bucket `cdk-hnb659fds-assets-*`), AWS Amplify, KMS and CloudWatch metrics/dashboards/alarms (moved here from `-core-2.json` in 2026-11 to make room for the PassRole and CloudFormation splits), Lambda (moved here from `-core.json` in 2026-11 to make room for the `UpdateFunctionConfiguration` split) |
| `workshop-iam-policy-infra.json`   | Cognito (writes fenced on the Workshop tag), DynamoDB, API Gateway (read-only), X-Ray, ECR, ECS, CloudFront (tag/untag/invalidate fenced on the Workshop tag) |
| `workshop-iam-policy-network.json` | VPC / EC2 networking, ELB |
| `workshop-iam-policy-core-4.json`  | Module 5: CloudWatch Omni (`cloudwatch` namespace), Observability Admin dataset integration, AgentCore Evaluations, PassRole for the three Stack 4 roles; AWS Marketplace `Subscribe` (product-ID scoped, moved here from `-core-3.json` in 2026-11) and `ViewSubscriptions`; EFS (creates region-fenced, tagging only at create time, deletes fenced on the Workshop tag — moved here from `-network.json` in 2026-11) |
| `workshop-iam-policy-core-5.json`  | AgentCore Identity (`ACIdentityScoped`, `ACIdentityRead`; the exact `CreateTokenVault` grant lives in the IDE role's inline policy `TokenVaultLazyCreate` in `code-editor.yaml`, see below), AgentCore Policy Engine management (`ACPolicyManage`), SSM parameters, EventBridge, Secrets Manager, CDK role tagging (`IAMCdkRoleTagsBootstrapKeysOnly`), EFS describe — created 2026-11 when IAM's real size count put `-core.json` over the quota (see "How IAM counts the 6,144 characters") |

> **Adding a file here is two edits, not one.** A new
> `workshop-iam-policy-*.json` takes effect only once it is listed in
> `contentspec.yaml`; the parity script and the size checks glob the directory and
> will happily report a file "OK" that is attached nowhere. `core-3.json` shipped
> in exactly that state — present, reviewed, size-checked, parity-synced, and
> absent from `contentspec.yaml`, so a Workshop-Studio-provisioned account got
> four managed policies and no `agent-registry:*` grant at all, and Module 3b
> failed on its first Agent Registry call. Verify against a live account rather
> than the file list:
>
> ```bash
> aws iam list-attached-role-policies --role-name WSParticipantRole \
>   --query 'AttachedPolicies[].PolicyName' --output text
> ```

## These seven files are consumed TWICE — edit the JSON, never the copy

1. `contentspec.yaml` attaches them to **WSParticipantRole**.
2. `code-editor.yaml` embeds them as
   `CodeEditorParticipantPolicy{Network,Infra,Core,Core2,Core3,Core4,Core5}` and attaches them
   to **CodeEditorInstanceBootstrapRole**.

Site 2 is the one that matters in practice. Nothing on the IDE instance ever
assumes WSParticipantRole — the bootstrap only runs `aws configure set region` —
so the instance role is the identity that actually executes every CLI command and
notebook cell in the workshop. It previously carried `AdministratorAccess`, which
meant these reviewed documents were enforced *nowhere*.

CloudFormation cannot read a sibling file, so site 2 has to be a copy. Do not
hand-edit it. Edit the JSON and run:

```bash
scripts/verify-ide-policy-parity.py --sync
```

`deploy-cfn.sh deploy` runs that script in check mode as an **aborting**
preflight. A drifted copy would silently run the workshop on permissions nobody
reviewed, so drift fails the deploy rather than warning.

> Beware the scanner here. `CKV_AWS_111` is implemented via cloudsplaining, which
> cannot see inside an AWS managed policy ARN — so `AdministratorAccess` **passed**
> while the scoped rewrite initially **failed** on two files. The gate rewarded
> the worse posture. Always re-diff Checkov against the previous template after a
> least-privilege change instead of assuming it improved the result.

## Size constraint (read before adding actions)

Each file is attached as an IAM **managed policy**, which has a hard
**6,144-character quota**, measured on the *minified* JSON — IAM does not count
whitespace, but keep the source readable anyway.

Current minified sizes (headroom in parentheses):

| File | Chars (IAM count) | Headroom to 6,144 |
|------|-------|----------|
| `workshop-iam-policy-core.json`       | 4,458 | 1686 |
| `workshop-iam-policy-core-2.json`     | 5,668 | 476 |
| `workshop-iam-policy-core-3.json`     | 6,010 | 134 |
| `workshop-iam-policy-infra.json`      | 5,761 | 383 |
| `workshop-iam-policy-network.json`    | 5,690 | 454 |
| `workshop-iam-policy-core-4.json`     | 5,605 | 539 |
| `workshop-iam-policy-core-5.json`     | 3,473 | 2671 |

(Measured 2026-11-10 with `scripts/verify-ide-policy-parity.py`'s `minified_len`, which
since the event 10 failure counts the way IAM does — see "How IAM counts the 6,144
characters" below; this table is the current source — sizes quoted in the dated change-log entries further down are the measurements of their day.) The script fails any
file above its 6,050-character budget, which keeps a margin under the quota. Put a new
statement in whichever file the table shows with the most headroom at the time, and
re-measure the table in the same commit. Statements have moved between files more than
once for room (the changelog rows say which); the Sid carries the meaning, not the file
name.

`scripts/verify-ide-policy-parity.py` re-checks all seven sizes on every run and
keeps this quota from being exceeded silently, so you do not have to remember to.

An IAM user or role accepts **10 managed policies** by default. Seven here plus
`IAMReadOnlyAccess` and the Workshop Studio `ws-default-policy` is nine on
WSParticipantRole, so one slot is left before a split stops being free. On the IDE
instance role the same seven plus `AmazonSSMManagedInstanceCore`,
`CloudWatchAgentServerPolicy` and `AmazonQDeveloperAccess` make exactly ten — the
next split has to merge or drop something there first.

Check every file before committing a change:

```bash
python3 scripts/verify-ide-policy-parity.py
```

It measures each file the way IAM does (a single-string `Action`/`Resource` counts as a
one-element array — see "How IAM counts the 6,144 characters" below) against a 6,050-character
budget, and also confirms the copies embedded in `code-editor.yaml` match. A plain
`len(json.dumps(doc, separators=(',', ':')))` under-counts: `-core.json` once measured 6,132
that way and 6,164 by IAM's count, and the deployment failed.

## Scoping conventions

- Do **not** list both a container ARN and its children — `arn:aws:s3:::workshop-*`
  already matches `arn:aws:s3:::workshop-*/*`, because an IAM wildcard matches `/`
  and `:` as well. Access Analyzer reports the redundant pair as
  `REDUNDANT_RESOURCE` and the Workshop Studio build surfaces it as a warning on
  every push. The same applies to `log-group:/…/name*` vs `…name*:*` and to
  `table/x*` vs `table/x*/index/*`.
- `iam:CreateServiceLinkedRole` must name the service in the resource path —
  `arn:aws:iam::*:role/aws-service-role/ecs.amazonaws.com/*`, not
  `…/aws-service-role/*`. The bare wildcard trips
  `CREATE_SLR_WITH_STAR_IN_RESOURCE`; pinning the service clears it while still
  letting AWS pick the role name.
- Scope by **resource name prefix** (`workshop-*`, and `ac-*` / `agentcore-*`
  for the log groups those stacks create). Never put the region in the **ARN** —
  that field is always `*`, because IAM JSON cannot reference Workshop Studio
  parameters.
- Where no ARN exists to scope to, fence the statement with an
  `aws:RequestedRegion` **condition** listing the `deployableRegions` set
  (`us-west-2`, `us-east-1`, `eu-west-1`). This is a real constraint, not
  scanner appeasement: `simulate-custom-policy` shows `kms:Decrypt` allowed via
  `secretsmanager.us-west-2` and `lambda.us-west-2` but `implicitDeny` via
  `ec2.us-west-2` under the `kms:ViaService` fence. Note that global services
  (IAM, STS, CloudFront, Route 53) report `us-east-1`, which is why it is in the
  list.
- `Resource: "*"` is retained **only** where AWS gives no resource-level ARN:
  `List*` / account-level `Get*` calls, account-level Bedrock and AgentCore
  APIs, and VPC/EC2 creates. Those statements carry a `*ReadOnly`,
  `*List`, or `*NoResourceLevel` Sid so the reason is visible in the file.
- **When an unscopeable write action turns out to be unnecessary, delete it
  rather than documenting it.** `workshop-iam-policy-infra.json` used to carry a
  `CloudFrontCreateOnlyNoArnAtCreateTime` statement granting
  `cloudfront:CreateDistribution` and `cloudfront:CreateOriginAccessControl` on
  `"*"`, on the grounds that neither action accepts a typed ARN. That premise was
  wrong: a live probe later showed both authorize against `distribution/*` and
  `origin-access-control/*` (see the self-service section below, which is why
  `iam simulate-custom-policy` is not trusted here). The statement is gone for a
  different reason, that the ParticipantRole never creates a distribution: the only two CloudFront distributions in the workshop are
  declared in `code-editor.yaml` and `registry/compute-stack.yaml`, and both
  stacks are deployed by Workshop Studio (event path) or by the deploying
  principal running `scripts/self-service-deploy.sh` from a **local** terminal
  under `self-service-deploy-policy-4.json` (self-service path) — never from the
  IDE. Every participant-facing CloudFront reference is a read: a
  `list-exports` lookup of `workshop-MainCloudFrontUrl`. Cleanup still works,
  because tearing a distribution down needs `UpdateDistribution` +
  `DeleteDistribution`, which live in `CloudFrontDeleteAndUpdateTaggedOnly` (typed
  `distribution/*` ARN plus the Workshop resource tag since 2026-11). This was the
  last remaining Checkov `CKV_AWS_111` failure in `code-editor.yaml`; note that the
  check runs through **cloudsplaining**, which *skips* any statement that carries a
  `Condition` block and otherwise flags every write action on a bare `*` (verified
  against checkov 3.3.1: the permissions boundary's conditioned `NotAction` statement
  passes `CKV_AWS_107`–`111`, the unconditioned version failed all five). A condition
  therefore does clear it — but only a *meaningful* one belongs in a policy, which is
  why the fences here are region, tag and record-name conditions rather than
  decoration.
- Write actions are split from read actions into separate Sids so a reviewer can
  see at a glance which statements can mutate state. For AgentCore this split is
  load-bearing: a `Create*` action authorizes against a literal wildcard ARN of
  its resource type (`gateway/*`, `policy-engine/*`: no id exists yet, so that is
  the tightest usable form; probed live, see "`bedrock-agentcore:CreateGateway`
  and `CreatePolicyEngine` ARE resource-scopeable" below), while the manage
  actions take a concrete ARN. Keeping `ACGatewayCreate` and `ACGatewayManage`
  as separate statements makes that difference visible. The exception is
  `CreateOauth2CredentialProvider`, which authorizes against the token vault.
- **A service may authorize an action against a resource other than the one the
  call creates.** Granting the right action on the "obvious" ARN is not enough,
  and this is the single most common reason a scoped run fails where
  `AdministratorAccess` passed. Three cases here, all found only by running the
  notebooks under the scoped role:

  | Call | Also authorizes | Against |
  |------|-----------------|---------|
  | `CreateGateway` | `bedrock-agentcore:SynchronizeGatewayTargets` | `gateway/*` — no ARN exists yet |
  | `CreatePolicy` | `bedrock-agentcore:ManageResourceScopedPolicy` | the **gateway** ARN, not the policy-engine ARN |
  | `CreateOauth2CredentialProvider` | `bedrock-agentcore:CreateTokenVault` | `token-vault/default`, created lazily on first use |

  The first row is `ACGatewaySyncAuthorizedOnWildcardArnByCreateGateway` in `-core.json`
  (`SynchronizeGatewayTargets` on `gateway/*`, region-fenced). It was removed once, on
  2026-11-08, as a "redundant" twin of the same action in `ACGatewayManage`; the next test
  event (11) answered `create_gateway.py` with `not authorized to perform:
  bedrock-agentcore:SynchronizeGatewayTargets on resource: …:gateway/*`, because the
  concrete `gateway/tools-gateway-*` ARNs in `ACGatewayManage` cannot match the wildcard the
  service checks during creation. Do not remove it again.
  The first two were already granted — in `ACGatewayManage` and `ACPolicyManage`
  respectively — and still produced a 403, because the grant was scoped to the
  ARN the *call* names rather than the one the *service* checks. That is why the
  same action now appears in two statements with two different resources. Read
  the resource out of the 403 message; do not infer it.
- **`*WorkloadIdentity` is authorized against the *directory*, not the identity.**
  `ACIdentityManage` must use `workload-identity-directory/default*` and cannot be
  scoped by identity name, because the 403 names:

  ```
  not authorized to perform: bedrock-agentcore:DeleteWorkloadIdentity on resource:
  arn:aws:bedrock-agentcore:us-west-2:<acct>:workload-identity-directory/default
  ```

  — the parent directory, with no `/workload-identity/<name>` segment at all. An
  earlier version of this statement listed four child ARNs
  (`.../workload-identity/ac-*`, `workshop-*`, `registry-*`, `tools-gateway-*`).
  **None of them ever matched**, so `UpdateWorkloadIdentity` and
  `DeleteWorkloadIdentity` were effectively ungranted, and the statement read as
  tightly scoped while granting nothing. Prefix-scoping was tried twice — once
  with the original two prefixes and once by adding two more after a live
  failure — before reading the resource out of the 403 rather than reasoning about
  what it ought to be.

  This is load-bearing, not cosmetic: **`CreateRegistry` silently creates a
  workload identity named `registry-<registryId>`, and `DeleteRegistry` cascades
  into `DeleteWorkloadIdentity` under the caller's own principal.** Without the
  permission the registry lands in `DELETE_FAILED` ("Unable to delete workload
  identity because access was denied"), or — seen on a second run — deletes
  successfully and orphans an identity that then cannot be removed by anyone,
  because a `registry-*` identity is service-linked and a direct
  `DeleteWorkloadIdentity` is refused for every caller, administrators included.
  Gateways (`tools-gateway-*`, `ac-tools-gateway-*`) behave the same way.
  `self-service-deploy-policy-7.json` already used `default*` and was never
  affected; only the Workshop Studio IDE path was.

  Verified by probing the *class* of error rather than the outcome: the call is
  refused for everyone, so `AccessDeniedException` → `ValidationException` is
  exactly the signal that the policy took effect.
- **Tagging actions on `Resource: "*"` are accepted.** cloudsplaining reports
  several dozen of these across the fifteen policy files (seven participant, eight
  self-service). CloudFormation tags a resource as
  part of creating it, before any ARN exists to scope to, and a `Tag*`/`Untag*`
  action cannot escalate privilege on its own. There is no Checkov check for this
  class, and it was not among the findings the Workshop Studio build reported.
  Unconstrained **write** and **permissions-management** actions are a different
  matter and are held at **zero** across all fifteen files.

## `cdk bootstrap` needs tagging permissions even though it sets zero tags

Module 4b runs `cdk bootstrap` and `cdk deploy` **from the IDE, as the
participant** — so the whole CDKToolkit stack is created under
`CodeEditorInstanceBootstrapRole`, not by Workshop Studio. When the IDE role
was cut down from `AdministratorAccess` to the five scoped policies, that broke:

```
CREATE_FAILED  ImagePublishingRole  AWS::IAM::Role
  Encountered a permissions error performing a tagging operation, please add
  required tag permissions. … is not authorized to perform: iam:TagRole on
  resource: …role/cdk-hnb659fds-image-publishing-role-<acct>-<region>
```

Three of the five bootstrap roles failed this way, CDKToolkit rolled back, and
**every** Module 4b notebook then failed downstream — 14 failing cells from one
missing action. `iam:TagRole` / `iam:UntagRole` / `iam:ListRoleTags` are now in
`-core-5.json` (today in `IAMCdkRoleTagsBootstrapKeysOnly`, after moves through `-core-2.json` and
`-core-4.json`; they sat in the since-retired `IAMRole` Sid when this was written).

The non-obvious part, and the reason to generalise rather than add one action:

- **The CDKToolkit stack has no tags at all.** `cdk.json` sets no `tags`, the app
  calls no `Tags.of()`, and `describe-stacks` returns `Tags: []`. The modern
  CloudFormation resource handler for `AWS::IAM::Role` calls the tagging API
  regardless, so the permission is required **unconditionally**. Do not reason
  "we set no tags, so no tagging permission is needed" — that inference is wrong
  for every registry-backed resource type.
- So the same class of grant is needed for the other bootstrap resources, which
  is what `CdkBootstrapStagingBucketConfiguration`,
  `CdkBootstrapEcrRepositoryConfiguration`, and `CdkBootstrapVersionParameter`
  in `-core-3.json` cover. The staging bucket also needs one action per
  **property** the template sets — `AccessControl` → `s3:PutBucketAcl`,
  `BucketEncryption` → `s3:PutEncryptionConfiguration`,
  `VersioningConfiguration` → `s3:PutBucketVersioning`,
  `LifecycleConfiguration` → `s3:PutLifecycleConfiguration`,
  `PublicAccessBlockConfiguration` → `s3:PutBucketPublicAccessBlock` — plus the
  matching `Get*`, which the handler calls to read state back.

Only `iam:TagRole` was *observed* failing, because CloudFormation cancelled the
bucket before attempting it. The rest were derived from the bootstrap template
(`cdk bootstrap --show-template`) and confirmed together by one clean run. When
a stack rolls back, read **all** `CREATE_FAILED` events — CloudFormation reports
every resource that failed, not just the first, which is the difference between
one redeploy and five.

Two further notes on this path:

- `cdk bootstrap` attaches **`AdministratorAccess`** to
  `cdk-hnb659fds-cfn-exec-role-*`, and `cdk deploy` then creates `FAST-stack`'s
  resources as that role. So for Module 4b the scoped participant policy governs
  only bootstrap itself; everything the FAST stack creates is admin-provisioned.
  This is the same intrinsic escalation documented below — worth knowing before
  anyone tries to scope `FAST-stack-*` resources into these files. They are never
  authorized against the participant.
- The `DynamoDBAccess` grant in `-infra.json` looks dead — no template declares a
  table — but it is **not** safe to assume it is unused: FAST's
  `backend-stack.ts` creates a `FeedbackTable`. That table is created by the
  admin `cfn-exec-role` above, and it is named `FAST-stack-*`, which the grant's
  `table/workshop-*` scope would not match anyway. The grant is therefore
  genuinely unexercised, but the reason is the exec role, not the absence of
  tables.

## `aps:CreateWorkspace` authorizes against the *collection* ARN, not `workspace/*`

Scanners repeatedly flag `ApsCreateWorkshopTaggedWorkspace` /`ApsTagOnCreate` in
`self-service-deploy-policy-5.json` for using `arn:aws:aps:*:*:/workspaces` and
recommend "scoping" them to `arn:aws:aps:*:*:workspace/*`. **Do not apply that
recommendation — it breaks the observability stack.** At create time no workspace
id exists yet, so there is no instance ARN to authorize against; the request is
authorized against the collection. Live probe matrix, run under a role holding
*only* the statement under test:

| Action                | Resource in policy                    | Result |
|-----------------------|---------------------------------------|--------|
| `aps:CreateWorkspace` | `arn:aws:aps:*:*:/workspaces`         | Allow  |
| `aps:CreateWorkspace` | `arn:aws:aps:*:*:workspace/*`         | **Deny** |
| `aps:TagResource` (on create) | `arn:aws:aps:*:*:/workspaces` | Allow  |
| `aps:DeleteWorkspace` | `arn:aws:aps:*:*:workspace/*`         | Allow  |

Two corollaries the probe also established:

- Tag-on-create needs `aps:TagResource` on the **collection** ARN as well, not
  just `CreateWorkspace`. A denial here names `aps:TagResource`, which makes it
  look like the `CreateWorkspace` resource form is wrong when it is not.
- The collection ARN honours `aws:RequestTag/${TagKey}`, which is why
  `ApsCreateWorkshopTaggedWorkspace` can fence on
  `aws:RequestTag/Workshop = AgentCore-Platform` without breaking the deploy:
  CloudFormation propagates stack tags into the underlying create call, and
  `deploy-cfn.sh` and `contentspec.yaml` both set that stack tag. Verified on the
  real CFN-created `workshop-prometheus` workspace, which carries the `Workshop`
  tag even though the template itself only sets `Name` and `Environment`.

Only `DeleteWorkspace` / `DescribeWorkspace` / `UntagResource` and the other
post-create operations take the `workspace/*` instance form — which is exactly
how `ApsManageTaggedWorkshopWorkspaces` is already written.

Related: `sns:Unsubscribe` authorizes against the **topic** ARN, not the
subscription ARN, so it belongs in the `arn:aws:sns:*:*:workshop-*` statement in
`-policy-2.json` rather than on `"*"`. And `amplify:CreateApp` *does* accept
`arn:aws:amplify:*:*:apps/*` (also live-proven) — unlike `aps:CreateWorkspace`,
it is not a collection-ARN action.

## `ecs:DeregisterTaskDefinition` is NOT resource-scopeable; `RegisterTaskDefinition` is

Scanners flag both together, in `EcsAccountReadNoResourceLevel`
(`self-service-deploy-policy-6.json`) and `ECSAccountLevel`
(`workshop-iam-policy-infra.json`), and recommend scoping both to
`arn:aws:ecs:*:*:task-definition/workshop-*:*`. **Only half of that is correct.**
Live probe, under a role holding only that one statement:

| Call | Result |
|---|---|
| `RegisterTaskDefinition`, family `workshop-probe2`, valid body | **Allow** — task definition created |
| `RegisterTaskDefinition`, family `otherfam2-probe`, valid body | **Deny**, naming `…:task-definition/otherfam2-probe:*` |
| `DeregisterTaskDefinition` on `workshop-probe-td:1` (exists, ARN matches the grant) | **Deny**, naming `on resource: *` |
| `DeregisterTaskDefinition` on `otherfam-probe-td:1` (exists) | **Deny**, naming `on resource: *` |

So `RegisterTaskDefinition` is scoped to `workshop-*` in both files
(`EcsRegisterTaskDefinitionWorkshopFamiliesOnly` /
`ECSTaskDefinitionWorkshopFamiliesOnly`), and `DeregisterTaskDefinition` gets its
own honestly-named `Resource: "*"` statement,
`EcsDeregisterTaskDefinitionNoResourceLevel`, region-fenced like every other
wildcard statement here. It is **absent from
`workshop-iam-policy-infra.json` entirely**: nothing participant-facing registers
or deregisters a task definition (`grep -rn "task-definition" content/ source/
scripts/` returns nothing but one unit test), so a grant that can never authorize
was only there to mislead the next reader.

This entry has been violated once since it was written. A least-privilege pass
scoped `DeregisterTaskDefinition` to `…:task-definition/workshop-*:*` anyway, on
the reasoning that `Deregister` takes a `family:revision` and therefore must be
scopeable. It is not, and the cost was a stack stuck in `DELETE_FAILED`:
`Delete TaskDefinition: … not authorized to perform: ecs:DeregisterTaskDefinition
on resource: *`, with the live family `workshop-llm-gateway-stack-task:28`
matching the grant ARN exactly. The revision suffix in the API call is not the
same thing as a resource context in the authorization call. The
`on resource: *` in its denial is ECS telling us the action carries no resource
context at all — a typed ARN can never match it, so "scoping" it would silently
break teardown. Every task-definition family the workshop creates already starts
with `workshop-` (`workshop-auth-server`, `workshop-currenttime`, `workshop-mcpgw`,
`workshop-realserverfaketools`, `workshop-registry`,
`workshop-llm-gateway-stack-task`), verified against a deployed account.

The probe must use a **valid** request body. `--container-definitions '[]'` returns
`ClientException: Container list cannot be empty` for a matching *and* a
non-matching family, because ECS validates the body before authorizing — which
reads as "resource-level is ignored" and is the wrong conclusion.

## `bedrock-agentcore:CreateGateway` and `CreatePolicyEngine` ARE resource-scopeable

Both were on `"Resource": "*"` with Sids that asserted the service had no
resource-level authorization (`ACGatewayCreateNoResourceLevel`). That assertion was
never measured, and it was wrong. Probed live on 2026-08-17 with the zero-permission
probe-role method described at the end of this file:

| Grant given to the probe role | `CreateGateway` in us-west-2 | `CreatePolicyEngine` in us-west-2 |
|---|---|---|
| `arn:aws:bedrock-agentcore:us-west-2:<acct>:gateway/*` / `:policy-engine/*` | passes authorization (fails later on `iam:PassRole`) | passes authorization (fails later on the `/name` regex) |
| same ARNs but `eu-central-1` (negative control) | `AccessDeniedException ... CreateGateway on resource: arn:aws:bedrock-agentcore:us-west-2:<acct>:gateway/*` | `AccessDeniedException ... CreatePolicyEngine on resource: arn:aws:bedrock-agentcore:us-west-2:<acct>:policy-engine/*` |

The negative control is the part that matters: the denial names a concrete resource
ARN, so authorization really is evaluated against it rather than against `*`. Note
the ARN the service authorizes against ends in a literal `/*` — there is no id yet
at create time — so `gateway/*` and `policy-engine/*` are the tightest usable forms.

Both are now scoped in `workshop-iam-policy-core.json` (Sids `ACGatewayCreate`,
`ACPolicyEngineCreate`) and in `self-service-deploy-policy-7.json`, where they were
split out of `ACCreateAndListNoResourceLevel`. Access Analyzer's `validate-policy`
cannot settle this question — it does not flag action/resource mismatch at all (it
accepted `sts:GetCallerIdentity` on a role ARN in the same run), so do not treat a
clean `validate-policy` as evidence that a `"Resource": "*"` was necessary.

## `lambda:UpdateFunctionCode` is no longer granted at all (2026-11)

Until 2026-11 `-core-3.json` carried a single-function `lambda:UpdateFunctionCode` on
`function:ac-auto-review` so that nb 04's "Step 10a.5" could push the current build into a
stack deployed before the agent-registry migration. The build rubric rated it HIGH (PE4):
the participant already holds `lambda:InvokeFunction` on `ac-*`, so code-overwrite plus
invoke is arbitrary code under the function's execution role. The grant is gone and the
notebook cell is verify-only — it reads the deployed zip, checks the
`ac-auto-review-raw-http-v2-agent-registry` sentinel, and on a mismatch tells the
participant to redeploy `workshop-agentcore-stack` (the single source of truth for the
function code) instead of patching the Lambda from a notebook. Nothing else in the
workshop updates a platform Lambda's code; `ZipFile` inline functions are changed only
through their stacks.

`lambda:CreateFunction` followed in 2026-11 (same build rubric, PE2/PE4): nothing a participant
runs creates a function, and together with `iam:PassRole` to Lambda and `InvokeFunction` it was
arbitrary code under a `workshop-*` role. `UpdateFunctionConfiguration` on the five platform
functions the content reconfigures is kept, with a `Null lambda:Layer = true` condition so a reconfiguration cannot attach
a layer; `--environment` and `--timeout` changes carry no layer and still pass.

The residual the rubric names — `UpdateFunctionConfiguration` can also change `Handler`, the
execution role, and interpreter-startup variables such as `AWS_LAMBDA_EXEC_WRAPPER` — has no
IAM condition key (Lambda offers `lambda:Layer`, VPC and function-URL keys, nothing for the
handler, role or environment). Two things bound it. First, `iam:PassRole` to
`lambda.amazonaws.com` left `PassRole` in `-core-2.json` in 2026-11 (no page or notebook passes
a role to Lambda: `CreateFunction` is gone and the one `update-function-configuration` the
content runs changes `--environment` only), so the function cannot be re-pointed at another
role at all. Second, the five functions run under `workshop-*`/`ac-*` roles that carry
`workshop-permissions-boundary`, so code that reaches one of them gains nothing a participant
does not already have. Accepted as the documented residual of a grant the content needs. Since 2026-11-10 the
statement also carries the `aws:RequestedRegion` fence the other statements use (Holmes scan `48f9df48`: the
five function ARNs have `*` in the region segment, so without the fence the write was valid in every Region);
core-3 is now at 6,010 characters by IAM's count, the tightest file.
`DeleteFunction`, `RemovePermission` and `GetPolicy` left at the same time; see the
`lambda:AddPermission` section for why nothing a participant runs needs them any more.

**The self-service files deliberately keep the broader grant.**
`LambdaForWorkshopFunctions` in `self-service-deploy-policy-5.json` still holds
`UpdateFunctionCode` over the `workshop-*` / `ac-*` / `agentcore-*` / `code-editor-*`
prefixes, because that principal *deploys* the templates: CloudFormation runs under the
caller's credentials, so a stack update that changes any inline `Code.ZipFile` needs
`UpdateFunctionCode` on the function it is replacing. Narrowing it there would break
`./deploy-cfn.sh deploy` on any re-deploy, and the escalation argument does not carry
over — that principal already holds `lambda:CreateFunction` plus `iam:PassRole`.

## `aws-marketplace:Subscribe` is scoped to the four Anthropic product IDs the workshop uses

`aws-marketplace:Subscribe` and `ViewSubscriptions` used to sit inside the
Bedrock-named `BedrockCatalogNoResourceLevel` statement, then in a joint
`MarketplaceSubscribeNoResourceLevel` statement with only the three-region
`aws:RequestedRegion` guard. Until 2026-11 the product IDs were treated as opaque and
the statement was carried as a documented exception.

That exception is gone. Bedrock publishes the **Marketplace product ID** on every
model card (*Models at a glance*) and documents the `aws-marketplace:ProductId`
condition key for exactly this purpose ("Use product ID condition keys to control
access"). Only Anthropic models in this workshop are sold through AWS Marketplace:
Amazon Nova, DeepSeek and Mistral AI have no product ID and never need `Subscribe`.
So the grant is now two statements in `workshop-iam-policy-core-4.json` (moved out of
`-core-3.json`, which at the time had 297 characters of headroom, less than the
condition needs):

| Sid | Action | Condition |
|-----|--------|-----------|
| `MarketplaceSubscribeWorkshopAnthropicModelsOnly` | `aws-marketplace:Subscribe` | region fence + `ForAnyValue:StringEquals aws-marketplace:ProductId` ∈ {`prod-ffvjxvh4ltq64` Claude Sonnet 4.6, `prod-xdkflymybwmvi` Claude Haiku 4.5, `prod-jhuafngbly644` Claude Opus 4.5, `prod-5ukwuglpt66kg` Claude Opus 4.6} |
| `MarketplaceViewSubscriptionsNoResourceLevel` | `aws-marketplace:ViewSubscriptions` | region fence only (`ProductId` applies to `Subscribe` alone) |

The same allow-list is on the deployer (`self-service-deploy-policy-3.json`
[`MarketplaceSubscribeWorkshopAnthropicModelsOnly`], moved from `-policy-4.json` for
the same size reason) and on the LiteLLM task role in
`llm-gateway/workshop-llm-gateway-stack.yaml` (`BedrockAccess`), which performs the
first-invoke auto-subscription for the gateway. Priming in Module 2 step 2 opens
Claude Sonnet 4.6 in the playground, which is in the list. **When a Claude model is
added to `setup_keys.py` / `litellm-config.yaml`, add its product ID here, in the
three places above, and in the Sid's comment** — the model card is the source of
truth. `ForAnyValue:StringEquals` is the operator the Bedrock docs use for this key.

## `cognito-idp:InitiateAuth` was removed — IAM is never consulted for it

Both policy sets used to grant `cognito-idp:InitiateAuth` on `"Resource": "*"`, and a
scanner flagged the wildcard. Scoping it would have been meaningless: `InitiateAuth`
is an **unauthenticated** API — the client proves who it is with a username and
password, not with SigV4 — so IAM never evaluates it at all.

Proven with an explicit `Deny`, which is the only test that distinguishes "allowed"
from "not evaluated". A role carrying `Allow cognito-idp:*` **plus**
`Deny cognito-idp:InitiateAuth` was pointed at a real user pool and client:

```
$ aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
    --client-id <real-client> --auth-parameters USERNAME=nosuchuser,PASSWORD=...
An error occurred (UserNotFoundException) ... User does not exist.
```

An explicit `Deny` always wins in IAM. Reaching `UserNotFoundException` means the
request was never authorized in the first place. So the grant was dead weight and
was **deleted** rather than scoped — from `CognitoNoResourceLevel` in
`workshop-iam-policy-infra.json` (the statement held nothing else and is gone) and
from `CognitoCreateAndListNoResourceLevel` in `self-service-deploy-policy-3.json`.

`AdminInitiateAuth` is a different action and **is** authorized; it stays, scoped to
`userpool/*`.

> Do not "fix" a wildcard by scoping it before checking whether IAM sees the call at
> all. A plain allow-test cannot tell the two cases apart — both succeed. Only the
> `Deny` differential can.

## `execute-api:Invoke` was removed — nothing in the workshop uses AWS_IAM auth

`self-service-deploy-policy-3.json` granted `execute-api:Invoke` on
`arn:aws:execute-api:*:*:*`. It was flagged, and the right fix turned out to be
deletion, not narrowing:

- `HttpApiRoute` in `workshop-llm-gateway-stack.yaml` declares no
  `AuthorizationType`, so it defaults to `NONE` — callers reach it over plain HTTPS
  and IAM is not in the path.
- The only AWS_IAM-authorized HTTP surface anywhere in the repo is on Lambda
  **Function URLs**, which are authorized by `lambda:InvokeFunctionUrl`, not by
  `execute-api:Invoke`.

So the statement authorized nothing that is ever called. The `apigateway:*`
management statement is unrelated and untouched.

## `bedrock:PutUseCaseForModelAccess` has no resource-level support at all

Its `"Resource": "*"` is not a shortcut. A denial for this action names **no**
resource:

```
User ... is not authorized to perform: bedrock:PutUseCaseForModelAccess
```

Compare any resource-scopeable action, whose denial ends with
`on resource: arn:aws:...`. **The absence of the `on resource:` clause is the
signal** — IAM had no ARN to evaluate against, so no ARN can be written. Leave it
on `*`.

## CloudFront distributions *do* honour `aws:ResourceTag` (live-proven)

`DeleteDistribution` and `UpdateDistribution` used to sit on
`arn:aws:cloudfront::*:distribution/*` with no condition, which made them the only
destructive grants in the set not narrowed by the workshop tag. They are now
conditioned on `aws:ResourceTag/Workshop = AgentCore-Platform`.

The condition works — differential probe against one real distribution, same policy
both times, using the harmless `CreateInvalidation` from the same statement:

| Distribution state | Result |
|---|---|
| no tags | `AccessDenied ... on resource: arn:aws:cloudfront::<acct>:distribution/<id>` |
| `Workshop=AgentCore-Platform` | authorized (invalidation created) |

Two things this change depends on, both deliberate:

1. **The tag is set explicitly in the templates**, on the `compute-stack.yaml`
   distribution and the `code-editor.yaml` distribution, rather than being left to
   CloudFormation stack-tag propagation. Propagation would probably work —
   `contentspec.yaml` and `deploy-cfn.sh` both pass `Workshop=AgentCore-Platform` at
   stack level — but "probably" is not good enough here: an untagged distribution
   cannot be deleted at cleanup and keeps billing.
2. **`TagResource`, `UntagResource` and `CreateInvalidation` were unconditioned when
   this was written; the 2026-11 least-privilege pass changed that** (see its table
   below): `TagResource` now requires `aws:RequestTag/Workshop = AgentCore-Platform`
   (`CloudFrontTagOnCreateWorkshopOnly`), and `UntagResource` plus `CreateInvalidation`
   require the Workshop *resource* tag (`CloudFrontManageTaggedOnly`); `ListTagsForResource`
   stays open. The self-blocking worry recorded here — a stack update that adds the tag to
   an untagged distribution being denied the call that fixes it — does not apply to the
   request-tag form: that very call carries `Workshop=AgentCore-Platform` in its request,
   so it is allowed. What the split closes is tagging a *foreign* distribution to unlock
   `UpdateDistribution`/`DeleteDistribution` on it.

Allow ~30s after tagging before the condition matches; CloudFront tag state is not
instantly visible to IAM. This never bites in practice because CloudFormation polls
`GetDistribution` (unconditioned) for minutes after create.

## `ecs:DeregisterTaskDefinition` has no resource-level support — the wildcard is required

`ecs:RegisterTaskDefinition` accepts `arn:aws:ecs:*:*:task-definition/workshop-*:*`,
so it is natural to assume its inverse does too. It does not. Differential test in
us-west-2, one role, one action, two policies:

| `Resource` | Result |
|---|---|
| `arn:aws:ecs:*:*:task-definition/workshop-*:*` | `AccessDeniedException` — "not authorized to perform: `ecs:DeregisterTaskDefinition` **on resource: `*`**" |
| `*` | allowed; the probe task definition went `INACTIVE` |

Read the denial message, not the docs: it names `*` as the resource IAM evaluated,
which is the signature of an action with no resource-level support. `Resource: "*"`
in `EcsDeregisterTaskDefinitionNoResourceLevel` (`policy-6`) is therefore load-bearing
— scoping it breaks cleanup with `AccessDenied`. A scanner will keep flagging it;
the Sid is the answer, and this is the evidence.

Note that IAM Access Analyzer `validate-policy` is **not** an oracle for this. It
returned zero findings for a policy that scoped `ecs:DescribeTaskDefinition` — which
also has no resource-level support — to a task-definition ARN. It validates action
names and ARN grammar, not action/resource compatibility.

## `bedrock:InvokeModel` is scopable to three ARN types, and that is enough

`workshop-iam-policy-core.json` [`BedrockInvokeModelsScoped`] and
`self-service-deploy-policy-7.json` [`BedrockInvokeModelsScoped`] both grant
`InvokeModel` / `InvokeModelWithResponseStream` on exactly:

    arn:aws:bedrock:*::foundation-model/*
    arn:aws:bedrock:*:*:inference-profile/*
    arn:aws:bedrock:*:*:application-inference-profile/*

Verified live: a role holding only those three ARNs invoked
`us.anthropic.claude-sonnet-4-5-20250929-v1:0` (a cross-region inference profile,
which needs both the profile ARN *and* the underlying foundation-model ARN) and got
HTTP 200. This excludes `provisioned-model/*`, `custom-model/*`, agents, prompts and
marketplace endpoints, none of which the workshop uses.

The rest of the old `BedrockModelAccessNoResourceLevel` statement stays on `*` under
the name `BedrockModelDiscoveryNoResourceLevel` — `PutUseCaseForModelAccess` and
the List/Get actions genuinely have no resource-level support.
`PutFoundationModelEntitlement` was dropped from `-core.json` and `policy-7` in
2026-11: it belonged to the console *Model access* page retired in 2025 (serverless
models are enabled on first invocation; Anthropic still needs the use-case form), and
nothing in the workshop calls it.

## Assuming CDK bootstrap roles: three of five, never `cfn-exec` or `lookup`

`cdk bootstrap` creates five roles. Four are assumed by the CDK CLI during a deploy;
the fifth, `cdk-hnb659fds-cfn-exec-role-*`, is the one CloudFormation itself runs as,
and it is created with `AdministratorAccess`. Wildcarding `role/cdk-hnb659fds-*` in
`STSAssumePersonaRolesAndBoundaryCappedCdkRoles` therefore handed the participant a one-call path to admin.

`workshop-iam-policy-core.json` [`STSAssumePersonaRolesAndBoundaryCappedCdkRoles`] now enumerates:

    arn:aws:iam::*:role/cdk-hnb659fds-deploy-role-*
    arn:aws:iam::*:role/cdk-hnb659fds-*-publishing-role-*   (file + image)

What this does and does not close. `deploy-role` can still create a stack that passes
`cfn-exec-role` to CloudFormation, and the CDK CLI fills
`CloudFormationExecutionPolicies` with `AdministratorAccess` when the bootstrap is run
without `--cloudformation-execution-policies` (the page and notebook 02 do not pass it).
Under the default CDK template that was a route to admin. It is not one here: the
workshop bootstraps with `source/module-4b-fast/cdk-bootstrap-template.yaml`, which puts
`workshop-permissions-boundary` on all five bootstrap roles, `cfn-exec-role` included, so
that role's `AdministratorAccess` is capped to what the boundary allows — the same cap the
participant already lives under, with no IAM write that escapes it (see "IAM escalation —
closed with a permissions boundary (2026-11)"). Proven in event 7: `CDKToolkit` and
`FAST-stack` deployed with every CDK and FAST role carrying the boundary.

**Account pin (2026-11).** The nine role ARNs (three persona roles, the CDK deploy and
publishing roles in three Regions) keep `*` in the account segment because
the same JSON is embedded, unchanged, into every Workshop Studio event account and into
self-hosted accounts, so no account id can be written into the file. The statement now
carries `"StringEquals": {"aws:ResourceAccount": "${aws:PrincipalAccount}"}` instead:
the participant can only assume a role that lives in the account the participant itself
runs in, which is where `cdk bootstrap` created the CDK roles and where the registry
persona roles are deployed. `iam simulate-custom-policy`: same-account deploy role
`allowed`, the same role name in another account `implicitDeny`; Access Analyzer accepts
the policy variable. The publishing roles stay: `cdk deploy` assumes the file-publishing role to upload
assets (and the image-publishing role for container assets). The lookup role left the list
in 2026-11: the FAST CDK app performs no context lookups (`grep -ri fromLookup
source/module-4b-fast` is empty), and when the CDK CLI cannot assume the lookup role it
says so and proceeds with the current credentials, which `CFNReadFastStacks` lets read the
deployed template. The CloudFormation execution role those deploys pass is the
boundary-carrying, non-admin one from `source/module-4b-fast/cdk-bootstrap-template.yaml`
(see "IAM escalation — closed with a permissions boundary (2026-11)" below).
permissions boundary (2026-11)" below).

## Neither `lambda:AddPermission` nor `lambda:RemovePermission` is granted to the participant any more

Only two templates contain `AWS::Lambda::Permission` —
`agentcore/workshop-agentcore-stack.yaml` and
`tools-gateway/workshop-tools-gateway-stack.yaml` — and both are pre-deployed by
Workshop Studio (`contentspec.yaml`) or by `deploy-cfn.sh` under
`self-service-deploy-policy-5.json`, never by the participant. No content page or
notebook calls `add-permission`. So `lambda:AddPermission` came out of
`workshop-iam-policy-core.json` [`Lambda`]: it is the action that can hand an
external principal the right to invoke a workshop function, and nothing needs it.

`lambda:RemovePermission` stayed for a while because the Cleanup module once had the
participant run `aws cloudformation delete-stack` on both of those stacks, and deleting an
`AWS::Lambda::Permission` calls it. That path closed when `CFNParticipantStacks` (now `CFNCdkToolkitBootstrap` +
`CFNReadFastStacks`) limited the participant's stack writes (the teardown runs under the
deployer identity), so `RemovePermission`, `GetPolicy` and `DeleteFunction` left the
participant `Lambda` statement in 2026-11 together with `CreateFunction`. The statement now
grants `GetFunction`, `GetFunctionConfiguration` and `InvokeFunction` only.

Same reasoning moved `s3:PutBucketPolicy` / `s3:DeleteBucketPolicy` out of
`[S3Access]` (which spanned `workshop-*`, `fast-*` and all of `cdk-*`) into
`workshop-iam-policy-core-3.json`
[`S3BucketPolicyOnCdkStagingBucketOnly`], scoped to
`cdk-hnb659fds-assets-*`. `cdk bootstrap` is run by the participant and does write
the staging bucket's policy, so the grant cannot simply be dropped — but it does not
need to cover every bucket whose name starts with `cdk-`.

## One EC2 statement, not five: how the typed-ARN rewrite paid for itself

`self-service-deploy-policy-1.json` had five statements sharing a byte-for-byte
identical `Condition` block (`aws:RequestedRegion` + `aws:ResourceTag/Workshop`),
three of which used `Resource: "*"`. They are now one statement,
`Ec2ModifyTagDeleteWorkshopTaggedOnly`, with 32 actions over 13 typed ARNs.

The file got **smaller** — 5,977 to 5,336 — because four duplicate condition blocks
cost more than one typed resource list. Worth remembering the next time a typed-ARN
narrowing looks unaffordable: merge first, then measure.

The merge is safe because an action can only ever be authorized against the resource
types it actually operates on. Listing `instance/*` alongside `vpc/*` for
`ec2:DeleteVpc` grants nothing — `DeleteVpc` presents a VPC ARN or nothing. So a
union of resource types across a union of actions is not the widening it looks like,
as long as every action in the statement carries the same conditions.

The EC2 resource types are the ones the templates actually create (`AWS::EC2::` grep:
VPC, Subnet, InternetGateway, NatGateway, EIP, RouteTable, SecurityGroup,
SecurityGroupIngress/Egress, VPCEndpoint, Instance) plus the three that appear only
implicitly (`network-interface`, `volume`, `launch-template`).

## EC2 instance mutation is CloudFormation-only for the self-service deployer (2026-11)

`Ec2ModifyTagDeleteWorkshopTaggedOnly` in `self-service-deploy-policy-1.json` used to carry
`ec2:StartInstances`, `StopInstances`, `ModifyInstanceAttribute`, `AssociateIamInstanceProfile`,
`DisassociateIamInstanceProfile` and `ReplaceIamInstanceProfileAssociation` behind the same
`aws:ResourceTag/Workshop` condition as the network delete actions. The only instance that carries
that tag is the Code Editor, whose role is a Space Admin on the Omni space and runs the notebooks.
A deployer who can stop it, rewrite its user data with `ModifyInstanceAttribute` and start it
again runs code under the instance role from a terminal — a PE4-style path the rubric flagged
(build `d3255266`).

The six actions now live in `self-service-deploy-policy-8.json`
(`Ec2IdeInstanceMutationOnlyThroughCloudFormation`), still Workshop-tag scoped, plus
`"ForAnyValue:StringEquals": {"aws:CalledVia": "cloudformation.amazonaws.com"}`. CloudFormation
forwards the deployer's own credentials when no service role is given (the self-service path
never passes `--role-arn`), so a stack update that changes `InstanceType`, `UserData` or the
instance profile in place keeps working, while the same calls from the CLI are denied. Nothing
in `deploy-cfn.sh` or the content issues those six calls directly. `AttachVolume`/`DetachVolume`
and `TerminateInstances` stay in `-policy-1.json`: they cannot run code under the instance role.
`-policy-1.json` dropped to 5,702 characters; `-policy-8.json` is at 3,057 after the 2026-11-10 CloudFormation-only fences (see below).

## `self-service-deploy-policy-8.json`: the account-level statements are CloudFormation-only (2026-11-10)

Holmes scan `f1a5fb55` flagged two things in `-policy-8.json`: `logs:PutResourcePolicy`/`DeleteResourcePolicy`
on `*` (no resource type exists, so the grant covered every Logs resource policy in the account), and the
API Gateway v2 child-resource and `TagResource`/`UntagResource` statements, which cannot carry the Workshop
tag fence (children are not taggable) and so let a deployer tag any API and then edit it. The deployer never
issues any of these calls directly — `deploy-cfn.sh` and the scripts contain no `apigatewayv2`, `logs
put-resource-policy` or `xray update-*` call; the only callers are the CloudFormation handlers for
`AWS::ApiGatewayV2::*`, `AWS::Logs::ResourcePolicy` and `AWS::XRay::TransactionSearchConfig` in the
LLM Gateway and AgentCore stacks, which run under the deployer's credentials (no service role). So the four
statements now carry `ForAnyValue:StringEquals aws:CalledVia = cloudformation.amazonaws.com`, the same fence
`Ec2IdeInstanceMutationOnlyThroughCloudFormation` uses: stack create/update/delete keeps working, the same
calls from a terminal are denied. `-policy-8.json` is at 3,057 characters (minified JSON).

## `aps:DeleteRuleGroupsNamespace`: name-scoped, deliberately not tag-scoped

Every other APS statement carries `aws:ResourceTag/Workshop`. The delete does not,
and adding it would be a regression: a tag condition on a resource that is already
gone cannot be evaluated, so an idempotent cleanup retry returns `AccessDenied`
instead of the `ResourceNotFound` the script handles.

The scoping is by name instead. The workshop creates exactly one rule groups
namespace, `mcp-gateway-alerts` (`registry/compute-stack.yaml:251`), so
`policy-5` [`ApsDeleteOnlyTheWorkshopRuleGroupsNamespaceByName`] grants
`arn:aws:aps:*:*:rulegroupsnamespace/*/mcp-gateway-alerts` — narrower than the tag
condition would have been, and retry-safe.

## `kms:CreateAlias` authorizes two resources, so it needs two statements

`kms:CreateAlias` and `kms:DeleteAlias` are evaluated against **both** the alias ARN and
the target key ARN. A single statement listing both resources can therefore only carry
conditions that are true of both — which is why `KmsWorkshopAlias` in `policy-2` originally
had `arn:aws:kms:*:*:alias/workshop-*` and `arn:aws:kms:*:*:key/*` under a region-only
condition, leaving the key side wide open: a `workshop-*` alias could be pointed at any CMK
in the account.

The fix is two statements, one per resource, which is how AWS documents this call:

- `KmsWorkshopAlias` → `alias/workshop-*`, region condition only. An alias cannot be tagged
  and does not exist yet at `CreateAlias` time, so a tag condition here would deny every
  create.
- `KmsAliasTargetKeyMustBeWorkshopTagged` → `key/*`, region **and**
  `aws:ResourceTag/Workshop: AgentCore-Platform`.

Do not collapse these back into one statement. The tag condition is only correct on the key
half, and IAM evaluates each resource in the request independently — two statements each
covering one resource satisfy the call.

Unlike the APS case above, tag-scoping the key is retry-safe: the alias depends on the key,
so CloudFormation deletes the alias while the key still exists and is still tagged. The
`Workshop` tag is not set on `DocumentDBKmsKey` explicitly — `data-stack.yaml` sets only
`Name` and `Component` — it arrives through CloudFormation stack-tag propagation from
`deploy-cfn.sh` (`--tags Key=Workshop,Value=AgentCore-Platform`). The sibling
`KmsManageTaggedWorkshopKeys` statement has depended on that same propagation for
`PutKeyPolicy`/`ScheduleKeyDeletion` through every live deploy, which is the evidence that
the tag is really there.

## `application-autoscaling` authorizes against a literal `scalable-target/*`

The service never presents a target-specific ARN. A denial names
`arn:aws:application-autoscaling:<region>:<account>:scalable-target/*` verbatim,
so `arn:aws:application-autoscaling:*:*:scalable-target/*` is the tightest ARN
that can be written, and a narrower one denies everything. The real narrowing is
the condition key, which **is** honoured (live-proven):

| Grant | Call | Result |
|---|---|---|
| `scalable-target/*`, no condition | `RegisterScalableTarget` ns=`ecs` | Allow (reaches `ValidationException`) |
| `scalable-target/zzzz-will-not-match*` | `RegisterScalableTarget` ns=`ecs` | Deny |
| `scalable-target/*` + `application-autoscaling:service-namespace = ecs` | `RegisterScalableTarget` ns=`ecs` | Allow |
| `scalable-target/*` + `application-autoscaling:service-namespace = ecs` | `RegisterScalableTarget` ns=`dynamodb` | **Deny** |

Hence `AasEcsScalingWrites` in `-policy-6.json` carries that condition; the
`Describe*` actions stay on `"*"`.

## EC2 network create/modify actions are all resource-scopeable

Every write action in `EC2VPCWriteScopedToResourceTypes`
(`workshop-iam-policy-network.json`) and `Ec2NetworkCreateScopedToResourceTypes`
(`-policy-1.json`) was dry-run probed under a role holding only the typed-ARN
statement, and all returned `DryRunOperation` (authorized): `CreateVpc`,
`ModifyVpcAttribute`, `CreateSubnet`, `CreateInternetGateway`,
`Attach`/`DetachInternetGateway`, `CreateNatGateway`, `AllocateAddress`,
`ReleaseAddress`, `CreateRouteTable`, `CreateRoute`,
`Associate`/`DisassociateRouteTable`, `CreateSecurityGroup`,
`Create`/`DeleteNetworkInterface`, `CreateVpcEndpoint`. The `Describe*` actions
have no resource form and stay on `"*"`.

One trap: **probe against resources that exist.** `CreateRoute` and
`DisassociateRouteTable` return `UnauthorizedOperation` when handed a
syntactically valid but nonexistent id, because EC2 cannot resolve the id into an
ARN to authorize against and fails closed. Both return `DryRunOperation` against
a real route table. Reading the first result as "not scopeable" would have left
two statements on `Resource: "*"` for no reason. `ec2:ModifyVpcAttribute` does not
accept `--dry-run` at all, so it has to be probed with a real, idempotent call
(`--enable-dns-support` on a VPC that already has it).

Because `-policy-1.json` had only 190 bytes of headroom, splitting its EC2
statement did not fit. Its read-only EC2 actions were merged with
`Ec2IdeDescribeOnly` into a single `Ec2DescribeOnlyNoResourceLevel` statement and
moved to `-policy-7.json`, which had room. All eight documents attach to the same
role, so which file holds a statement changes nothing at evaluation time — the Sid
carries the meaning, not the filename.

## Secrets Manager: participants only READ `workshop*` secrets (2026-11)

The participant policy touches Secrets Manager for two unrelated reasons, so `SM` is split:

- `SM` grants only `GetSecretValue` and `DescribeSecret`, on `secret:workshop*` (the pages
  read the registry token and the Cognito M2M client secret) and on
  `secret:bedrock-agentcore-identity*`. No page, notebook or bootstrap step ever *writes* a
  `workshop*` secret (`grep` of `content/` and `source/` for `create-secret`/`put-secret-value`
  is empty; `setup_keys.py` only calls `get_secret_value`). The `workshop*` secrets are created
  and written by the stacks' own custom-resource roles, which this policy never touches, so the
  participant needs no write there — and now cannot overwrite or delete any platform-owned
  `workshop*` secret (the Cognito M2M copy, the registry token, the LiteLLM admin key, …).
- `SMIdentityWrite` keeps `CreateSecret`, `PutSecretValue`, `DeleteSecret` and `TagResource`
  on `secret:bedrock-agentcore-identity*` only — the OAuth2 credential-provider / token-vault
  secrets the AgentCore Identity flow stores when the IDE role runs
  `CreateOauth2CredentialProvider` (Modules 3a/3b/4). This grant is unchanged from before; it is
  simply no longer paired with the `workshop*` prefix.

An earlier revision (`f189e91`) kept the broad `workshop*` write and layered a two-name
`SMNoWriteOnPlatformSecrets` Deny on top; the rubric correctly pointed out the Deny named only
two of several platform secrets under the prefix. Removing `workshop*` from the write actions
altogether closes the whole class and makes the Deny unnecessary. (Build rubric findings on
`1cae77f9` and `b30cda7b`, 2026-11-09.)

## CloudWatch Omni is pre-provisioned; the participant has no Create/Delete on domains, spaces or Dataset integrations (2026-11)

`workshop-agentcore-stack` (Omni resources added 2026-11-09; the instance-role grant is in
`code-editor.yaml`, since Workshop Studio allows at most five templates) creates the Omni domain, space,
Dataset integration and the two Space Admin access grants at provisioning — Workshop Studio events and the
self-paced `deploy-cfn.sh` path alike — so participants only *verify* them in Module 5 step 1. Proven on a
fresh event (16, 2026-11-09): every resource `CREATE_COMPLETE`, `WSParticipantRole` admitted from the
console with no manual step. The trim followed the same day: `OmniDomainSpaceReadUpdate` keeps
`cloudwatch:Get/UpdateDomain`, `Get/UpdateSpace`, `TagResource` (no Create/Delete), and
`OmniDatasetIntegrationRead` keeps `observabilityadmin:GetDatasetIntegration` only.
`cloudwatch:CreateAccessGrant`/`DeleteAccessGrant` stay: step 1's "admit another console identity" block is
the self-heal for an account whose console identity differs from the deployer's. Step 1 tells an account
whose stack predates the Omni outputs to re-run `./scripts/self-service-deploy.sh` (in-place stack update);
step 7 and notebook 08 no longer delete the resources — the stacks do. Core-4 dropped to 5,605 characters by IAM's count (5,587 as minified JSON).

`CreateAccessGrant` validates that an `IAM_ROLE`/`IAM_USER` principal exists — probed live on event 16
(`ValidationException: Principal does not exist for the supplied ARN` for a made-up role) — so a grant for
`WSParticipantRole` would fail `workshop-agentcore-stack` in any account without that role, i.e. the whole
self-paced path. `OmniParticipantSpaceAdminGrant` is therefore conditional on the `OmniConsolePrincipalArn`
parameter: `contentspec.yaml` passes `{{.ParticipantRoleArn}}` (a Workshop Studio magic variable), and
`deploy-cfn.sh` passes nothing unless `OMNI_CONSOLE_PRINCIPAL_ARN` names an identity other than the one
running the deploy — then no console grant is created and Module 5 step 1's grant block covers it. The
instance-role grant in `code-editor.yaml` is unconditional because the role is created in the same template.

The first version of `deploy-cfn.sh` passed the deploying identity itself, and the first self-paced deploy
(a fresh account, as an IAM user, 2026-11-10) failed `workshop-agentcore-stack` on exactly that grant:
`CreateAccessGrant` 409 `ConflictException: An active SPACE_ADMIN grant already exists for principal
arn:aws:iam::<account>:user/<name> in space <space-id>`. Omni gives the identity that creates a space a system-managed Space
Admin grant (`OmniDefaults-Space-Admin`), and on the self-paced path the deployer *is* the creator —
CloudFormation runs as it, there is no `WSSystemRole` in between — so the stack's grant duplicated Omni's
own. In an event the two identities differ (`WSSystemRole` creates, `WSParticipantRole` is granted), which
is why events 16 and 17 never hit it. The script now resolves the deployer's principal only to refuse an
override that names it.

## `s3:PutObjectAcl` on `fast-*/*`: own account only, never a public canned ACL (2026-11)

`S3ObjectAclForFastAmplifyStaging` in `-core.json` exists for one caller: Amplify `StartDeployment`
(`scripts/deploy-frontend.py` in the FAST repo uploads with a plain `aws s3 cp`, then Amplify acts on the
object with the caller's credentials — event 7 failed with `AccessDenied` on exactly `s3:PutObjectAcl`
when the grant was dropped). Amplify's ACL write is not under our control, so the statement cannot pin a
canned ACL value; it now carries `aws:ResourceAccount = ${aws:PrincipalAccount}` (no ACL writes on a
`fast-*` bucket in another account — the prefix is guessable) and
`StringNotEqualsIfExists s3:x-amz-acl ∉ {public-read, public-read-write, authenticated-read}` (a public
canned ACL is refused; a request without the header, which is what a service-side grant looks like, is
untouched). The FAST staging bucket itself (`fast-stack-…-stagingbucket…`, CDK default) is `BucketOwnerEnforced` with all four
Block Public Access flags on (read live on event 17, 2026-11-10): ACLs are disabled there, so the only `PutObjectAcl`
that can ever succeed is `bucket-owner-full-control` in the owning account — exactly what the two conditions allow.
Rubric build `58ebf369`.

## SSM: reads stay broad, writes name the exact paths (2026-11)

`SSM` in `-core-5.json` used to grant `PutParameter`/`DeleteParameter` on `parameter/workshop*`
and the whole `parameter/cdk-bootstrap/*` tree with no Region fence. The rubric (build
`0af9b045`) pointed out that write access to the bootstrap tree lets a participant change
account-wide CDK bootstrap state. The statement is now two: `SSMRead` keeps `GetParameter` /
`GetParametersByPath` on the four prefixes, and `SSMWriteWorkshopPathsAndBootstrapVersionOnly`
grants `PutParameter`/`DeleteParameter` only on `/workshop/*`, `/FAST-stack/*`,
`/agentcore-gateway/*` and the single bootstrap parameter the workshop's `cdk bootstrap`
template writes, `/cdk-bootstrap/hnb659fds/version` (`hnb659fds` is the CDK default qualifier
the `cdk-hnb659fds-*` roles use). Both carry the `aws:RequestedRegion` fence the other
statements use. `workshop*` became `workshop/*`: every parameter the content writes lives under
`/workshop/` (`/workshop/llm-gateway-url`, `/workshop/llm-gateway-key`).

## Why some Sids are abbreviated

To stay under the quota, Sids are short labels rather than sentences — e.g.
`ACRegistry` / `ACGatewayWrite` / `ACIdentityRead`, `CFN`, `CWAlarms`, `EB`,
`SM`, `SSM`. Sids are internal labels with **no consumers** (nothing references
them), so renaming is always safe.

## If you hit the limit again

Do **not** keep trimming Sids or descriptions to claw back a few bytes — that
trades readability for a brittle margin. Instead **move the new actions into a
new policy file** and add it to the `iamPolicies` list in `contentspec.yaml`
(this is exactly how `-core-2.json` came to exist). Workshop Studio attaches
each listed file as a separate managed policy, so splitting is transparent to
participants and resets the 6,144-char budget for the new file.

## Validating a change

Run Access Analyzer against every file; it catches invalid actions, malformed
ARNs, and type mismatches that `cfn-lint` never sees:

```bash
for f in static/cfn/workshop-iam-policy-*.json; do
  echo "== $f"
  aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY \
    --policy-document "$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1]))))' "$f")" \
    --query 'findings[?findingType==`ERROR`]'
done
```

> Access Analyzer's action catalogue lags new services. It reports
> `bedrock-agentcore:CreateTokenVault` as `INVALID_ACTION` ("the action does not
> exist"), yet AgentCore names it verbatim in its own 403 and `iam create-policy`
> accepts it — verified by creating a throwaway policy containing only that action.
> When Access Analyzer and a live 403 disagree, the 403 wins about *reality*:
> `iam create-policy` is the parser that gates the deploy.
>
> **The Workshop Studio build gates on Access Analyzer and treats `INVALID_ACTION`
> as an `error`** — but only for the participant files listed under `iamPolicies`
> in `contentspec.yaml`. The self-service documents are not validated by the build
> (the same reason the two `INVALID_ACTION`s on `policy-8` are tolerable, see
> below), and `iam create-policy` accepts the action string. `ACTokenVaultManage`
> therefore names the two actions it needs — `bedrock-agentcore:CreateTokenVault`
> and `bedrock-agentcore:CreateOauth2CredentialProvider` — instead of the
> `Create*` wildcard it carried until 2026-11 (a wildcard also matched every other
> AgentCore `Create*` action on the token-vault ARN, including future ones).
> Measured against `accessanalyzer validate-policy`:
>
> | Action | Result |
> |---|---|
> | `bedrock-agentcore:CreateTokenVault` | `ERROR INVALID_ACTION` — catalogue lag, accepted on this self-service file |
> | `bedrock-agentcore:CreateToken*` | `ERROR INVALID_ACTION` — no catalogued action matches |
> | `bedrock-agentcore:Create*` | clean, but wider than needed |
> | `bedrock-agentcore:*` | clean, but far wider than needed |
>
> Do **not** drop `CreateTokenVault` from the list to silence the validator: the
> vault is created lazily on the first `CreateOauth2CredentialProvider`, and
> without it the deploy fails with
> `not authorized to perform: bedrock-agentcore:CreateTokenVault on resource:
> arn:aws:bedrock-agentcore:<region>:<acct>:token-vault/default`. If this action
> ever has to live in a participant file, the `Create*` prefix is the only form
> that passes the build — which is why, since 2026-11, it does **not** have to:
> `workshop-agentcore-stack` carries a `Custom::TokenVaultInit` resource that
> probes `GetTokenVault` and, when the vault is absent, creates and deletes a
> throwaway `workshop-token-vault-bootstrap` API-key provider under a Lambda role
> that names `CreateTokenVault` (templates are not build-gated). That was meant to
> let `ACTokenVaultManage` in `-core-2.json` grant exactly `CreateOauth2CredentialProvider`
> + `DeleteOauth2CredentialProvider` — **and it does not work**. Event 9 (2026-11-08,
> account with a `default` vault dated 2026-07-04) answered the participant's
> `CreateOauth2CredentialProvider` in notebook 06 with `not authorized to perform:
> bedrock-agentcore:CreateTokenVault on resource: …:token-vault/default`: the service
> authorizes `CreateTokenVault` on that call whether or not the vault exists. So the
> participant set carried, from 2026-11-08 until later the same day, `ACTokenVaultLazyCreate`
> (later renamed `…ExactActionUnknownToAccessAnalyzer`; written for `-core.json`, in `-core-5.json` after the
> seventh-file split): `bedrock-agentcore:Create*`
> on the **bare** `token-vault/default` ARN (no sub-path), region-fenced. The bare ARN
> is what `CreateTokenVault` authorizes against; the other `Create*` actions authorize
> against sub-paths (`…/oauth2credentialprovider/*`, `…/apikeycredentialprovider/*`) that
> the bare ARN does not match, so the wildcard reaches `CreateTokenVault` and nothing
> else today (simulated: `CreateTokenVault` on `token-vault/default` allowed,
> `CreateApiKeyCredentialProvider` on `token-vault/default/apikeycredentialprovider/x`
> implicitDeny). For a few hours the same file also carried an explicit `Deny`
> (`ACTokenVaultDenyUnusedCreateActionsTheWildcardWouldAllow`) for the two published
> `Create*` actions that list the `token-vault` resource type and that the workshop never
> calls — `CreateApiKeyCredentialProvider` and `CreatePaymentCredentialProvider` — so the
> wildcard's reach (`CreateOauth2CredentialProvider`, already granted by `ACIdentityScoped`,
> plus the unpublished `CreateTokenVault`) is pinned in the JSON itself, not only here.
> The service reference publishes no `CreateTokenVault` action at all; the token vault is
> a secondary resource type on the credential-provider actions.
> Narrower spellings were tried against the build's validator on
> 2026-11-08: `bedrock-agentcore:CreateTokenVault`, `CreateToken*` and `CreateT*` all come
> back `INVALID_ACTION`; only `Create*` is clean.
>
> **Where the action lives now (2026-11-08, same day).** The shared JSON files no longer
> carry any token-vault `Create` statement. The exact action sits in an *inline* policy on
> the IDE instance role in `code-editor.yaml` — `TokenVaultLazyCreate`, Sid
> `ACTokenVaultLazyCreateExactAction`: `bedrock-agentcore:CreateTokenVault` on
> `arn:aws:bedrock-agentcore:*:<account>:token-vault/default`, fenced to the three
> Regions. Templates are not run through the build's Access Analyzer gate, and IAM accepts
> the action string (`workshop-agentcore-stack`'s TokenVaultInit role has named it since
> 2026-11); an inline policy also does not count against the role's ten-managed-policy
> quota. `ACIdentityScoped` keeps the named `CreateOauth2CredentialProvider` on
> `token-vault/default*`, so the IDE role — the only identity that runs the notebooks and
> CLI pages — holds exactly the two actions the call needs, both by name. The
> WSParticipantRole (console) no longer holds `CreateTokenVault`: creating a credential
> provider from the console is not a content path, and the parity script still checks the
> seven embedded copies against their JSON sources; the inline policy is the one documented
> IDE-only addition.
> `Custom::TokenVaultInit` stays — it is harmless and still makes the
> vault exist early — but it is no longer load-bearing. `policy-7` keeps
> `CreateTokenVault` because `./deploy-cfn.sh deploy` creates that stack and the
> Lambda role needs the action.
>
> One more asymmetry worth knowing: at **create** time the service authorizes
> `CreateOauth2CredentialProvider` against a literal `*` —
> `token-vault/default/oauth2credentialprovider/*` — so the `workshop-*` suffix in
> `ACTokenVaultManage` never constrains creation. It is a real constraint only on
> **deletion**, where the provider name does appear in the ARN. Keep it for that
> reason, but do not read it as bounding what can be created.
>
> `GetTokenVault` is scoped differently again: it authorizes against a path-style
> ARN (`arn:aws:bedrock-agentcore:<region>:<acct>:/identities/get-token-vault`), so
> it must sit on `Resource: "*"` — under `token-vault/default*` it returns
> `AccessDeniedException`.

### The two credential-retrieval actions are scoped, and to *different* resource types

`bedrock-agentcore:GetWorkloadAccessToken` and `bedrock-agentcore:GetResourceOauth2Token`
both return live credentials, so neither belongs on `Resource: "*"`. Both honour
resource-level scoping — but **not against the same resource type**, which is the
trap. Probed by granting each action on a deliberately non-matching ARN and reading
the resource out of the resulting `AccessDenied`:

| Action | ARN IAM actually evaluates |
|---|---|
| `GetWorkloadAccessToken`  | `…:workload-identity-directory/default/workload-identity/<name>` |
| `GetResourceOauth2Token`  | `…:token-vault/default/oauth2credentialprovider/<name>` |

So a single `workload-identity-directory/default*` scope covering both — the obvious
reading, and what a scanner will recommend — silently breaks
`GetResourceOauth2Token` and with it
`source/module-3b-agentcore/notebooks/06-test-gateway.ipynb`, the only place either
action is called. `workshop-iam-policy-core.json [ACIdentityScoped]` carries both
ARNs, which is safe: each action can only ever authorize against its own type, so
the other entry is inert. `self-service-deploy-policy-7.json` instead splits them
onto the statement already holding the matching type (`ACIdentityManage` and
`ACTokenVaultManage`).

Getting a usable signal out of `GetResourceOauth2Token` needs a **real** workload
access token: with a bogus one the service rejects the argument before IAM is
consulted and every grant looks identical. Mint one with
`create-workload-identity` followed by `get-workload-access-token` in the probe
account, then pass it in — the same input-validation-before-authorization ordering
that makes EC2 dry-run probes need real resource IDs.

> Self-paced note: the self-service path runs under the participant's own
> credentials rather than a scoped ParticipantRole, so these files gate only the
> Workshop-Studio-provisioned path. A new file added here must also be listed in
> `contentspec.yaml` to take effect in WS-run events.

## How IAM counts the 6,144 characters (event 10, 2026-11-08)

Event 10 failed to deploy before any stack existed. Workshop Studio creates the participant
managed policies itself from the `iamPolicies` files, and IAM answered one `CreatePolicy` with
`LimitExceeded: Cannot exceed quota for PolicySize: 6144` — for `-core.json`, which
`scripts/verify-ide-policy-parity.py` had measured at 6,132 characters minified. IAM does
not count the bytes you send: it counts a normalized document in which a single-string
`Action`, `NotAction`, `Resource` or `NotResource` is an array (`"Resource": "*"` costs the
same as `"Resource": ["*"]`). That form of `-core.json` was 6,164 characters, 20 over. The
documented rule ("white space is not counted") is still true; the two extra characters per
single-string field are the part the documentation does not mention.

Two changes followed. `minified_len` in the parity script now measures the normalized form,
and the script fails any file above a 6,050-character **budget** rather than at the 6,144
quota, so the next unknown in IAM's arithmetic has 94 characters to land in. And a seventh
file, `workshop-iam-policy-core-5.json` (`CodeEditorParticipantPolicyCore5` in
`code-editor.yaml`, seventh entry under `iamPolicies` in `contentspec.yaml`), took the
AgentCore Identity statements, `ACPolicyManage`, SSM/EventBridge/Secrets Manager,
`IAMCdkRoleTagsBootstrapKeysOnly` and `EFSDescribeNoResourceLevel`, so at that point (2026-11-08) every file sat between 2,808 and
5,933 characters by IAM's count. Seven managed policies plus the three AWS-managed ones on the
IDE role stay inside the default quota of ten.

## The self-service deploy policies (`self-service-deploy-policy-*.json`)

These eight files are the least-privilege alternative to `AdministratorAccess`
for the principal that runs `./deploy-cfn.sh deploy` on the self-paced path.
They are linked from `content/introduction/getting-started/self-service.en.md`,
which also carries the create-or-update loop participants run. Nothing attaches
them automatically — adding a ninth file means updating that page's list, its
`for n in 1 2 3 4 5 6 7 8` loop, and the `{1..8}` hint in
`scripts/self-service-deploy.sh` (all three were updated when `-8` was added in
2026-11). Eight still fits the default 10-managed-policy-per-principal limit with
two to spare; past that, attach them to a dedicated deploy role instead.

| File | Scope |
|------|-------|
| `self-service-deploy-policy-1.json` | CloudFormation, EC2/VPC networking, EC2 IDE instances |
| `self-service-deploy-policy-2.json` | DocumentDB/RDS, KMS, Secrets Manager, SSM parameters + documents, EventBridge, SNS |
| `self-service-deploy-policy-3.json` | Cloud Map (`servicediscovery`), Cognito, ELB, Agent Registry, Marketplace `Subscribe` (product-ID scoped) — API Gateway v2 moved to `-policy-8.json` in 2026-11 |
| `self-service-deploy-policy-4.json` | IAM, CloudFront, Route 53, STS, S3 (Marketplace `Subscribe` moved to `-policy-3.json` in 2026-11; `ViewSubscriptions` stays in `AccountLevelReadOnlyNoArnSupport`) |
| `self-service-deploy-policy-5.json` | Lambda, EFS, Prometheus (APS), Service Quotas, Route 53 hosted-zone management for the Cloud Map namespace (moved from `-3` in 2026-11) |
| `self-service-deploy-policy-6.json` | ECS + Application Auto Scaling, ECR, CloudWatch, CloudWatch Logs (the Transaction Search account-level writes moved to `-policy-8.json` in 2026-11) |
| `self-service-deploy-policy-7.json` | Bedrock + Bedrock Guardrails, AgentCore (gateway, identity, policy engine, tagging), EC2 describe-only |
| `self-service-deploy-policy-8.json` | API Gateway v2 (the LLM gateway's HTTP API and VPC link): creates fenced on the Workshop request tag, API/VPC-link updates and deletes on the Workshop resource tag, children region-fenced (see "API Gateway v2: what a tag fence can and cannot cover"); the two Transaction Search account-level statements (`xray:UpdateTraceSegmentDestination`/`UpdateIndexingRule`, `logs:PutResourcePolicy`/`DeleteResourcePolicy`) that `AWS::XRay::TransactionSearchConfig` and `AWS::Logs::ResourcePolicy` in Stack 4 need; no resource type or condition key exists for any of the four (service reference, 2026-11); the six IDE instance-mutation actions (`Start`/`StopInstances`, `ModifyInstanceAttribute`, the three instance-profile association actions), Workshop-tag scoped **and** fenced to calls made through CloudFormation (`aws:CalledVia`), moved here from `-policy-1.json` in 2026-11 (see "EC2 instance mutation is CloudFormation-only") |

`policy-7`'s AgentCore statements are narrowed to what `./deploy-cfn.sh deploy` does:
`ACGatewayManage` is scoped to the two workshop gateway name prefixes and carries no
`PutResourcePolicy`/`ManageResourceScopedPolicy` (only the IDE path sets gateway
policies, in `core-2`); `ACIdentityManage` has no `GetWorkloadAccessToken` (token
issuance is the runtime roles' job, Stack 4 `TokenExchangeWorkloadIdentity`) and
names the `default` directory and its identities; `ACTagging` is limited to the
three resource types the stacks create, in the caller's own account
(`aws:ResourceAccount = ${aws:PrincipalAccount}`) and to the tag keys the templates
and `deploy-cfn.sh` set (`aws:TagKeys` allowlist: Workshop, Environment, Module,
Component, Name, Service, Role, plus CloudFormation's `aws:*` system tags). The
Transaction Search
statements are split by direction: the read statement
(`TransactionSearchReadNoResourceLevel`) stays in `policy-6` with the other CloudWatch
Logs grants, and the two write statements (`XRayTransactionSearchConfigNoResourceLevel`,
`LogsResourcePolicyForTransactionSearchNoResourceLevel`) moved to `policy-8` in 2026-11
(see "Transaction Search and the two log-group prefixes in `policy-6`"). History: they
first left `policy-7`, which measured 6,327 bytes with them in place, 183 over the quota,
for `policy-6`, which then had the room. Every statement carries the Region fence.

No self-service policy grants `acm:` or `grafana:`, and none needs to:
`compute-stack.yaml`'s `AWS::CertificateManager::Certificate` is behind
`HasDnsConfig` and `observability-stack.yaml`'s Grafana resources are behind
`DeployGrafana` / `DeployGrafanaOSS` — all three conditions are false for a
default deploy (see "Derive the action list from the deploy GRAPH" below, which
walks these conditions).

Almost every statement is `Resource: "*"` narrowed by an
`aws:RequestedRegion` condition, because CloudFormation creates the resources it
then reads back and most of the create calls have no resource-level ARN to scope
to. The exceptions — the only statements where a *name* can fall outside the
grant — are the IAM and S3 statements in `policy-4`, which are scoped to the
`workshop-*`, `code-editor-*` and `cfn-deploy-*` prefixes.

CloudFront and Route 53 in `policy-4` are scoped by *resource type* instead:
`distribution/*`, `origin-access-control/*`, `cache-policy/*`, `hostedzone/*`,
`change/*`. Both are global services, so a region condition would be a no-op
(every call reports `us-east-1`) — a typed ARN is the only real constraint
available, and it stops these grants from reaching CloudFront functions, key
groups or field-level-encryption configs. One action rejects a typed ARN and must
stay on `"*"`: `route53:CreateHostedZone`, which has no resource type at all.

> **Do not use `iam simulate-custom-policy` to answer this question.** An earlier
> revision of this paragraph claimed *four* actions reject a typed ARN, adding
> `cloudfront:CreateDistribution`, `cloudfront:CreateOriginAccessControl` and
> `cloudfront:CreateCachePolicy`, on the strength of the simulator returning
> `implicitDeny` for them. **The simulator is wrong about these actions.** Probed
> against the real API under a role holding nothing but `CloudFrontCreateScoped`:
>
> ```
> CreateCachePolicy          -> created 24a43da6-…      AUTHORIZED
> CreateOriginAccessControl  -> created E2X6U6OMBJEC67  AUTHORIZED
> CreateDistribution         -> NoSuchCachePolicy       AUTHORIZED (failed after authz)
> ```
>
> All three accept `distribution/*`, `origin-access-control/*` and `cache-policy/*`,
> and `policy-4` correctly scopes them. The same false negative appears for
> `elasticfilesystem:CreateFileSystem` (see `FINDINGS-DISPOSITION.md` §7.ao). Use a
> real call and read the `AccessDenied` message, which names the ARN IAM actually
> evaluated; a `NoSuchX`/`InvalidArgument` error means the call was **authorized**
> and failed later. Beware botocore's client-side `ParamValidation`, which never
> reaches AWS and therefore proves nothing.
>
> Related, and also counter-intuitive: `aps:CreateWorkspace` is scoped to
> `arn:aws:aps:*:*:/workspaces` — a *collection* ARN with a leading slash. That looks
> malformed but is exactly what IAM evaluates for this action, and a real
> `amp create-workspace` under that grant **succeeds**. Do not "fix" it to
> `workspace/*` (never matches) or to `"*"` (a needless widening).

Two delete statements
add `aws:ResourceTag/Workshop: AgentCore-Platform` on top; that works because
`deploy-cfn.sh` passes `--tags Key=Workshop,Value=AgentCore-Platform` on every
stack and CloudFormation propagates stack tags into nested stacks.
`Ec2DeleteGuardDutyManagedNetworkResources` is fenced on a *different* tag:
GuardDuty creates its own VPC endpoint and security group inside the workshop
VPC (they block VPC deletion until removed) and tags both `GuardDutyManaged=true`,
so the statement conditions `DeleteSecurityGroup`/`DeleteVpcEndpoints` on
`aws:ResourceTag/GuardDutyManaged = true` instead of the Workshop tag they never carry.

The split is purely a size budget — the 6,144-char managed-policy quota applies
per file exactly as it does for the ParticipantRole set, so measure them with the
same IAM-style count (`scripts/verify-ide-policy-parity.py`'s `minified_len`) before
committing. Current minified sizes (headroom in parentheses, measured 2026-11-08):
`-1` 5,887 (257), `-2` 5,913 (231), `-3` 5,707 (437), `-4` 6,118 (26),
`-5` 6,136 (8), `-6` 5,965 (179), `-7` 6,125 (19). `-4`, `-5` and `-7` are
effectively full after the 2026-11 pass (`-4` took the boundary-conditioned role
statement and the CloudFront tag split, `-3` the Cognito/Cloud Map fences plus the
Route 53 statement, which has since moved on to `-5` with its record-name fence; `-7` the guardrail fence and
`workshop_policy_engine-*` scoping). `-8` was created in 2026-11 for the API Gateway v2 split
and the Transaction Search statements; at 3,057 characters after the six EC2 instance-mutation
actions and the CloudFormation-only fences moved in, it has about 3,090 free, so it takes the next grants; a `-9` would be the step after that.
`-7` had sat at 6,327 after the Module 5 narrowing until its Transaction Search
statements moved to `-6`, and in 2026-11 on to `-8` (see above). Keep one statement
per service so a reviewer can find a service's grants in one place; when a file
fills up, move a whole service statement to the newest file rather than splitting
that service across two.

The one place that rule is bent is `AccountLevelReadOnlyNoArnSupport` in
`policy-4`, which collects the read actions from six services that accept no ARN
at all (`iam:ListRoles`, `cloudfront:ListDistributions`,
`route53:ListHostedZonesByName`, `sts:GetCallerIdentity`,
`s3:ListAllMyBuckets`, `aws-marketplace:ViewSubscriptions`, …). Splitting them
per service cost ~90 bytes of statement scaffolding each and pushed the file over
quota; grouping them by *what can be scoped* rather than by service is the more
useful axis for a reviewer here, since the answer for all of them is "nothing".

### Deriving the action list

CloudTrail is the only reliable source. A CloudFormation-driven deploy calls
far more than the templates suggest — every resource read-back
(`elasticloadbalancing:DescribeLoadBalancerAttributes`,
`lambda:GetRuntimeManagementConfig`, `logs:DescribeIndexPolicies`, …) is a
separate authorized action, and drift/rollback paths add more still. To refresh
the list after a template change, deploy all five stacks under an
`AdministratorAccess` principal, then look up every event in the deploy window
where `userIdentity.invokedBy == cloudformation.amazonaws.com` and the record
names a `workshop-*`, `code-editor*` or `cfn-deploy-*` resource. Note that
CloudTrail *event* names are not always IAM *action* names: API Gateway v2
events (`CreateApi`, `CreateVpcLink`) authorize as `apigateway:POST` and
friends, and Lambda events carry an API-version suffix (`CreateFunction20150331`).

#### Tagging actions are invisible to a CloudTrail-derived list

`deploy-cfn.sh` passes `--tags` on every stack and CloudFormation propagates
stack tags into nested stacks, so every provider for a taggable resource type
makes a tag call — and for most services that is a **separate IAM action** with
no CloudTrail event of its own. Derive these from the **templates** instead:
for every taggable resource type present, confirm the service's tag verbs are
granted, and grant them symmetrically — `Tag` **and** `Untag` **and**
`ListTagsForResource`, since the update and read-back paths need the latter two
even when a first create does not. Note the verb is service-specific:
ACM uses `AddTagsToCertificate`/`RemoveTagsFromCertificate`/
`ListTagsForCertificate`, SSM uses `AddTagsToResource`/`RemoveTagsFromResource`,
ELB uses `AddTags`/`RemoveTags`, EC2 uses `CreateTags`/`DeleteTags`, and RDS
uses `AddTagsToResource`/`RemoveTagsFromResource`/`ListTagsForResource`.

Do not grant tag actions for a service whose resources here are not taggable —
`AWS::Route53::RecordSet` (the only Route 53 type in the templates) and
`AWS::ApplicationAutoScaling::ScalableTarget` have no `Tags` property, and the
Cloud Map hosted zone is tagged through `servicediscovery:TagResource`. Amazon
Managed Service for Prometheus splits the same way: the *workspace* is tagged (so
`ApsManageTaggedWorkshopWorkspaces` carries both tag verbs **and** an
`aws:ResourceTag/Workshop` fence), while a *rule-group namespace* is not, so
`ApsManageRuleGroupsNamespaces` grants no tag verbs and is region-fenced only. A
tag fence on that second statement would deny every call.

Watch the ARN *shape* while you are here — several of these services use a
path-style resource with a leading slash, and a conventional ARN silently matches
nothing: Grafana is `arn:aws:grafana:*:*:/workspaces/*`, and API Gateway is
`arn:aws:apigateway:*::/restapis/*`.

> **`apigateway:TagResource` is real and `policy-8` (`policy-3` until 2026-11) must keep it, even though
> Access Analyzer calls it `INVALID_ACTION`.** This entry has been flipped once
> already, twice, and cost a full deploy each time, so here is the evidence in
> both directions. If you are about to remove these because a linter told you
> to: that is exactly what the last two people did.
>
> `aws accessanalyzer validate-policy` returns
> `ERROR INVALID_ACTION — The action apigateway:TagResource does not exist.`
> (and the same for `UntagResource`). A probe role also shows that *REST* API
> tagging authorizes as an HTTP-verb action against the resource's own ARN:
>
> | Call | Authorizes as | Against |
> |---|---|---|
> | `CreateVpcLink` | `apigateway:POST` | `arn:aws:apigateway:<region>::/vpclinks` |
> | tag a REST API | `apigateway:PATCH` | `arn:aws:apigateway:<region>::/restapis/<id>` |
>
> Both of those are true and both are irrelevant to the case that matters. The
> **API Gateway v2 CloudFormation handlers** tag through the IAM-action-style API,
> and AWS itself names the action in the denial. `workshop-llm-gateway-stack`
> failed to create with the verb-only grant in place:
>
> ```
> VpcLink      | not authorized to perform: apigateway:TagResource
>                on resource: arn:aws:apigateway:us-west-2::/vpclinks
> HttpApiStage | not authorized to perform: apigateway:TagResource
>                on resource: arn:aws:apigateway:us-west-2::/apis/<apiId>/stages
> ```
>
> Granting `apigateway:POST` on `arn:aws:apigateway:*::/tags/*` does **not**
> satisfy it; only the literal `apigateway:TagResource` does. IAM's policy
> evaluator matches action strings, so an action missing from Access Analyzer's
> catalogue still authorizes correctly once granted. Where the validator and a
> live 403 disagree, the 403 wins — same as
> `cloudfront:CreateDistributionWithTags` and
> `bedrock-agentcore:CreateTokenVault`.
>
> Note the two `INVALID_ACTION` errors this leaves on `policy-8` (they sat on `policy-3`
> until the 2026-11 split) are **safe here
> and would not be elsewhere**: the Workshop Studio build fails on
> `INVALID_ACTION`, but it only validates the seven files listed under
> `iamPolicies` in `contentspec.yaml`. The self-service documents are not among
> them. Do **not** add these actions to `workshop-iam-policy-infra.json` — it
> would break the build, and participants never deploy an `AWS::ApiGatewayV2::*`
> resource, so its `APIGatewayAccess` twin genuinely does not need them.

## Transaction Search and the two log-group prefixes in `policy-6` (2026-11)

`TransactionSearchWriteNoResourceLevel` (`policy-6`) bundled `xray:UpdateTraceSegmentDestination`,
`xray:UpdateIndexingRule`, `logs:PutResourcePolicy` and `logs:DeleteResourcePolicy` on
`"Resource": "*"` behind the region fence. The rubric asked for the policy name or the
`aws/spans` resource to be pinned. The AWS service reference
(`servicereference.us-east-1.amazonaws.com/v1/{logs,xray}`) lists **no resource type and no
condition key** for `PutResourcePolicy`, `DeleteResourcePolicy` or `UpdateIndexingRule`, and
only `xray:TraceSegmentDestination` for `UpdateTraceSegmentDestination`, which cannot be
pinned to `CloudWatchLogs` either, because the `AWS::XRay::TransactionSearchConfig` handler
sets `XRay` back on stack delete. So the four actions stay account-level; what changed is
that they now live in two statements in `self-service-deploy-policy-8.json`
(`XRayTransactionSearchConfigNoResourceLevel`, `LogsResourcePolicyForTransactionSearchNoResourceLevel`)
whose names say which resources need them: `TransactionSearchConfig` and
`TransactionSearchLogsPolicy` (`AWS::Logs::ResourcePolicy`, policy name
`${EnvironmentName}-transaction-search`, principal `xray.amazonaws.com`, resource the
`aws/spans` log group) in `workshop-agentcore-stack.yaml`.

`LogsForWorkshopLogGroups` (`policy-6`) also lost two over-broad prefixes. `/aws/vendedlogs/*`
appeared nowhere else in the repository (no template, script or notebook creates or writes
a vended log group), so it is gone. `/aws/ssm/*` is now `/aws/ssm/code-editor-*`: the only
SSM log group any template creates is `/aws/ssm/${CodeEditorSSMDoc}` in `code-editor.yaml`,
and that document has no explicit `Name`, so CloudFormation names it
`code-editor-CodeEditorSSMDoc-<random>` after the stack (`code-editor` in both the self-service
script and Workshop Studio).

## API Gateway v2: what a tag fence can and cannot cover (2026-11)

`ApiGatewayV2ForLlmGateway` in `self-service-deploy-policy-3.json` granted `GET`, `POST`,
`PUT`, `DELETE`, `PATCH`, `TagResource` and `UntagResource` on every `/apis*`, `/vpclinks*`
and `/tags/*` path in the region, so the deployer could edit or delete any HTTP API in the
account. The build rubric flagged it on every scan. It is now five statements in
`self-service-deploy-policy-8.json` (a new file: `-policy-3.json` had 74 characters left):

| Sid | Verbs | Resource | Fence |
|-----|-------|----------|-------|
| `ApiGatewayV2Read` | `GET` | all five paths | region |
| `ApiGatewayV2CreateWorkshopTagged` | `POST` | `/apis`, `/vpclinks` | region + `aws:RequestTag/Workshop = AgentCore-Platform` |
| `ApiGatewayV2UpdateDeleteWorkshopTagged` | `PATCH`, `DELETE` | `/apis/??????????` (an API id is exactly ten characters, so this matches the API itself and never a child path), `/vpclinks/*` | region + `aws:ResourceTag/Workshop = AgentCore-Platform` |
| `ApiGatewayV2ChildrenOfApisNotTaggable` | `POST`, `PATCH`, `DELETE` | `/apis/*/*` (routes, integrations, stages, deployments, authorizers under an API), `/tags/*` | region only |
| `ApiGatewayV2TagResourceForCfnHandlers` | `TagResource`, `UntagResource` | all five paths | region only (see the `apigateway:TagResource` note above) |

Why the fences stop where they do. CloudTrail from a Workshop Studio deploy of
`workshop-llm-gateway-stack` shows CloudFormation's `CreateApi` and `CreateVpcLink` calls
carrying the stack tags (`Workshop=AgentCore-Platform`, `Module=LLM-Gateway`, plus the
`aws:cloudformation:*` keys) in `requestParameters.tags`, so the request-tag fence on the two
creates holds. `Api` and `VpcLink` are taggable, so their updates and deletes take the
resource-tag fence. Routes, integrations, stages-as-children and deployments are not: API
Gateway documents tag inheritance for attribute-based access control **only for the V1 REST
API** ("API Gateway resources that can be tagged" — `RestApi` children inherit; the V2 list
names `Api`, `DomainName`, `Stage` and `VpcLink` as taggable and says nothing inherits), so a
resource-tag condition on `/apis/*/*` would deny every child the stack creates. Those stay
region-fenced, which is the service's ceiling. `apigateway:PUT` is gone: the v2 handlers
create with `POST`, update with `PATCH` and delete with `DELETE`; `PUT` is `ReimportApi`,
used only for an `Api` with an OpenAPI `Body`, which no template has.

`iam simulate-custom-policy` on the new file: create `/apis` with the Workshop request tag
→ allowed, without → implicitDeny; `PATCH` a tagged `/apis/a1b2c3d4e5` → allowed, `DELETE`
an untagged one → implicitDeny; `POST /apis/<id>/routes` and `PATCH
/apis/<id>/stages/$default` → allowed; `DELETE` a tagged `/vpclinks/<id>` → allowed;
`TagResource` on `/apis/<id>/stages` → allowed; `PUT /apis/<id>` → implicitDeny; anything
in eu-central-1 → implicitDeny.

#### Narrowing a policy is a change that has to be deployed

Two participant-visible breakages here came from *tightening* passes that were never
re-deployed: `policy-4`'s `${aws:PrincipalAccount}` condition (below), and
`SecretsManagerForWorkshopSecrets` losing the pattern that matches
`code-editor.yaml`'s `CodeEditorSecret` — whose name is
`${InstanceName}-${RandomGUID}`, i.e. `CodeEditor-<guid>`, and so matches neither
`secret:workshop*` nor `secret:ac-admin-password*`. The second one only surfaced on
teardown, as `code-editor` sitting in `DELETE_FAILED`.

Reconstructing that regression is harder than it sounds: **IAM keeps only five policy
versions**, so by the time the failure appeared the pre-tightening document had been
pruned and could not be diffed. What proved it was a regression rather than an original
omission was CloudTrail — a `CreateSecret CodeEditor-…` event under the same scoped role
with `errorCode: None`. Treat the success records as the audit trail for a policy, because
the policy's own history may be gone.

#### Two cheap static checks, and one simulator that will mislead you

Do not use `iam simulate-principal-policy` to answer "is this policy complete". Run over
257 (action, resource) pairs from a known-*successful* deploy it reported 161 as
`implicitDeny`, almost all artifacts: a pair whose CloudTrail record carried no resource
ARN gets simulated against `*`, and `*` matches no statement that scopes its `Resource`.
It is an authorization oracle for one concrete call, nothing more — the same caveat as
`simulate-custom-policy` further down, one layer up.

The checks that do work are plain text ones over the JSON:

1. **Action presence** — for every action in the CloudTrail-derived list, does *any*
   statement's `Action` glob match it? Ignores resources entirely, so it has no false
   positives beyond the event-name-vs-action-name quirks already listed above (expect
   `apigateway:Create*`/`Get*` and Application Auto Scaling, which CloudTrail logs under
   `eventSource: autoscaling.amazonaws.com` while its IAM prefix is
   `application-autoscaling:`).
2. **ARN match** — for every pair that *does* carry a concrete ARN, does that ARN match
   the resources of a statement that grants the action? This is the check that finds the
   `CodeEditor-*` class of bug.

#### The teardown surface is not in a deploy's CloudTrail

A deploy log contains no `Delete*` calls, so deriving the action list from one leaves the
`destroy` path unverified — and an un-deletable stack is worse than an undeployable one,
because the participant is billed for it. Audit it structurally instead: for every
create-side verb granted, assert the matching teardown verb is too
(`Create`/`Delete`, `Put`/`Delete`, `Register`/`Deregister`, `Attach`/`Detach`,
`Associate`/`Disassociate`, `Allocate`/`Release`, `Authorize`/`Revoke`, `Tag`/`Untag`).
Expect noise — many create verbs have no delete counterpart, and AWS pluralises some
(`cloudwatch:DeleteAlarms`, `ec2:DeleteVpcEndpoints`) — so hand-check the hits against the
API reference rather than adding whatever the heuristic names.

#### Derive the action list from the deploy GRAPH, not from every `Type:` in every template

A `grep -h 'Type: AWS::' static/cfn/**/*.yaml | sort -u` inventory is wrong in both
directions, and each direction costs something different.

**It invents gaps.** `AWS::Lambda::LayerVersion` appears in
`agentcore/workshop-agentcore-stack.yaml`, so a naive sweep demands
`lambda:PublishLayerVersion`. The resource carries `Condition: HasBoto3Layer`, which is
`!Not [!Equals [!Ref Boto3LayerBucket, '']]`, and `Boto3LayerBucket` defaults to `''` with
neither `contentspec.yaml` nor `deploy-cfn.sh` overriding it. The layer is never created.
Granting the three layer actions would have added privilege for a resource that does not
exist, which is the exact opposite of the point of these files.

**It hides excess privilege**, which is the more expensive direction because nothing ever
fails to make you look. Two causes:

- **Orphan templates.** `registry/observability-stack.yaml` is in the repo but no root
  deploys it — `workshop-registry-stack.yaml` nests only network, data, compute, services
  and workshop-tools, and says in a comment to deploy observability separately and flip a
  flag on update. Every resource type unique to that file was being granted for nothing.
- **Nested-stack parameters.** A child's own `Default:` is not what it runs with. Evaluate
  each condition against the values the *parent* passes. `compute-stack.yaml`'s
  `HasDnsConfig` needs both `BaseDomain` and `HostedZoneId` non-empty; the parent defaults
  both to `''` and contentspec passes neither, so `RegistryCertificate`,
  `RegistryDnsRecord` **and** `MainAlbHttpsListener` — the only consumer of the cert — are
  all gated off together.

Walking the graph from the contentspec roots, evaluating `Fn::And`/`Or`/`Not`/`Equals` with
parent-supplied values, and diffing the result against the policies removed 23 dead
actions:

| Removed | Why it was dead |
| --- | --- |
| 7 `acm:*` (3 Sids) | `AWS::CertificateManager::Certificate` and the HTTPS listener that used it are both behind `HasDnsConfig`, which is false |
| 10 `grafana:*` (2 Sids) | `AWS::Grafana::Workspace` exists only in the orphan `observability-stack.yaml`. The Grafana in the deployed path is the `mcpgateway/grafana` **container** in `services-stack.yaml`, which needs no `grafana:` IAM at all |
| 6 `servicediscovery:*` service-level | Only `PrivateDnsNamespace` is in the graph (`compute-stack.yaml`); `AWS::ServiceDiscovery::Service` and the ECS `ServiceRegistries` that consume it are orphan-only |

Plus `grafana.amazonaws.com` out of `IamCreateServiceLinkedRoleOnly`'s resources and out of
`IamPassRoleToWorkshopServicesOnly`'s `iam:PassedToService`.

Two traps when running this in reverse:

- **A service with no resource type in the graph is not automatically removable.** The
  reverse diff is *advisory*. `cloudwatch:PutDashboard` has no `AWS::CloudWatch::Dashboard`
  in the graph — the dashboard is created by a notebook at runtime. Likewise `route53:*`
  survives with no `AWS::Route53::*` resource, because Cloud Map's `PrivateDnsNamespace`
  creates the hosted zone implicitly. Grep the whole repo — content, notebooks, scripts —
  for the service before deleting anything. ACM and Grafana were removed only after that
  grep came back empty.
- **Nothing here covers the custom resources' own actions.** CloudFormation invokes a
  Lambda-backed custom resource with the stack operation's credentials, so the deploy role
  needs only `lambda:InvokeFunction`. What the custom resource then does runs under its own
  execution role — a different principal, and a separate audit.

## 2026-11 least-privilege pass (Workshop Studio build rubric findings)

The Workshop Studio build's rubric scan (Content Champion Rubric, *Custom Policy Least
Privilege & Escalation* rule) flagged thirteen statements across both sets. The
dispositions, so nobody re-widens them by accident:

| File | Statement | Change | Why it is safe |
|------|-----------|--------|----------------|
| `-infra.json` | `CognitoWrite` | `aws:ResourceTag/Workshop = AgentCore-Platform` + region fence | Every pool the participant administers carries the tag: `data-stack.yaml` sets `UserPoolTags.Workshop` explicitly; FAST's pool gets it from the stack tag `cdk.json` now records (Module 4 deploy page / nb 02 verify it after `cdk deploy`). Service reference confirms `aws:ResourceTag` on every `cognito-idp:Admin*`/`*UserPoolClient`/`CreateGroup` action. |
| `-infra.json` | `CloudFrontTagAndInvalidate` → `CloudFrontTagOnCreateWorkshopOnly`, `CloudFrontManageTaggedOnly`, `CloudFrontListTags` | TagResource only with `aws:RequestTag/Workshop`; Tag/Untag/Invalidate only on tagged distributions; ListTags stays open | Closes the tag-then-delete self-escalation the scanner described (`CloudFrontDeleteAndUpdateTaggedOnly` was already tag-fenced). `code-editor.yaml` and `compute-stack.yaml` tag their distributions explicitly. |
| `-infra.json` | `APIGatewayAccess` → `APIGatewayReadOnly` | `apigateway:GET` only; POST/PUT/PATCH/DELETE and `execute-api:Invoke` dropped | Participants never mutate API Gateway: the HTTP API is provisioned by Workshop Studio / `deploy-cfn.sh`, FAST goes through the CDK execution role, and the five stacks are torn down with `./deploy-cfn.sh destroy` under the deployer identity. Nothing uses an AWS_IAM-authorized API (see `execute-api:Invoke` above). |
| `-network.json` → `-core-4.json` | `EFSFileSystemAndAccessPointScoped` + new `EFSDeleteWorkshopTaggedOnly` + new `EFSTagOnCreateOnly` | Creates stay region-fenced; `Delete{FileSystem,MountTarget,AccessPoint}` require the Workshop resource tag; `TagResource` is allowed only with `elasticfilesystem:CreateAction` ∈ {`CreateFileSystem`, `CreateAccessPoint`} (service reference), so a participant cannot tag a foreign file system `Workshop=AgentCore-Platform` and then delete it; both statements moved because `-network.json` went 195 B over quota | `workshop-llm-gateway-stack.yaml` tags its file system and access point explicitly. |
| `-core-2.json` | `CFN` | `stack/code-editor/*` removed from the write set | Nothing a participant runs touches the IDE's own stack: the Workshop Studio event owns it, and the self-service teardown (`./deploy-cfn.sh destroy`, or the manual `delete-stack code-editor` on the Cleanup page) runs under the deployer identity, whose `policy-1` already carries it. `CDKToolkit` stays because `cdk bootstrap` creates and updates it directly under the participant role, and the Module 4 cleanup page deletes it the same way. |
| `-core-3.json` | `S3BucketPolicyOnWorkshopAndCdkStagingBucketsOnly` → `S3BucketPolicyOnCdkStagingBucketOnly` | `arn:aws:s3:::workshop-*` dropped, region fence added | `cdk bootstrap` is the only participant-side bucket-policy write (the CDKToolkit stack sets the staging bucket's policy); no page or notebook writes a `workshop-*` bucket policy. |
| `policy-7` | `BedrockGuardrails` → `BedrockGuardrailsUpdateDeleteTaggedOnly` | `bedrock:TagResource`/`UntagResource` moved under the `aws:ResourceTag/Workshop` fence | Tagging an arbitrary guardrail `Workshop=AgentCore-Platform` would have unlocked update/delete on it. The deployer never creates a guardrail (no template does), so a create-time tag is not needed; the move costs 0 bytes on a file with 19 free. |
| `-core-2.json` | `PassRole` → `PassRole` + `PassCdkExecRoleToCloudFormation` | `workshop-*` roles may be passed to Lambda, Bedrock, AgentCore and Agent Registry only; the one CDK role a participant passes, `cdk-hnb659fds-cfn-exec-role-*`, may be passed to CloudFormation only | `cdk deploy` hands exactly that role to CloudFormation (`--role-arn`); the deploy, lookup and publishing roles are *assumed* (`STSAssumePersonaRolesAndBoundaryCappedCdkRoles` in `-core.json`), never passed, and no page passes a `workshop-*` role to CloudFormation (no template declares a service role). `KMSForWorkshopEncryptedResources` moved to `-core-3.json` unchanged to pay for the second statement. |
| `-core-2.json` | `CFN` → `CFNParticipantStacks` + `CFNReadWorkshopStacks` | stack writes only on `FAST-stack`, `FAST-stack-*` and `CDKToolkit`; the `workshop-*` platform stacks are read-only (`Describe*`, `Get*`, `List*`) | No page or notebook creates, updates or deletes a `workshop-*` stack: Workshop Studio provisions them, and the self-service teardown runs under the deployer identity (`./deploy-cfn.sh destroy`). The one `delete-stack workshop-llm-gateway-stack` in the Module 2 walkthrough is a self-service note, not a participant step. Notebooks read stack outputs constantly, hence the read statement. |
| `-core.json` → `-core-3.json` | `Lambda` + new `LambdaUpdateConfigurationPlatformFunctionsOnly` | `lambda:UpdateFunctionConfiguration` is granted on the seven functions the content reconfigured at the time (`agentcore-gateway-sync`, `agentcore-gateway-{request,response}-interceptor`, `ac-registry-gateway-sync`, `ac-gateway-{request,response}-interceptor`, `ac-auto-review`; a later row narrows this to five) instead of every `workshop-*`/`ac-*`/`agentcore-*` function; create/delete/get/invoke keep the prefixes | Reconfiguring a function (handler, layers, exec wrapper) runs code under its role, so the grant is pinned to the functions whose environment the pages actually edit (grep `update-function-configuration` in `content/` and the notebooks). Moved to `-core-3.json` with `LambdaList` because `-core.json` had 73 bytes free. |
| `-core-2.json` | `CFNParticipantStacks` | `ArnLikeIfExists cloudformation:RoleArn = arn:aws:iam::*:role/cdk-hnb659fds-cfn-exec-role-*` | The one service role a participant ever hands to CloudFormation is the bounded CDK execution role (`cdk deploy --role-arn`); `cdk bootstrap` and the `workshop-*` stacks pass none, which `IfExists` lets through. Together with `PassCdkExecRoleToCloudFormation` this says the same thing from both sides. |
| `-core-2.json` | `CFNParticipantStacks` → `CFNCdkToolkitBootstrap` + `CFNReadFastStacks`; `PassCdkExecRoleToCloudFormation` removed | the IDE role writes CloudFormation only on `CDKToolkit` (no `RoleArn` condition: `cdk bootstrap` passes no service role); `FAST-stack`/`FAST-stack-*` are read-only for it; it passes no CDK role at all | CloudTrail from event 7 (2026-11-08): every `CreateChangeSet`, `ExecuteChangeSet` and `DeleteStack` on `FAST-stack` was made by `cdk-hnb659fds-deploy-role-*` (assumed through `STSAssumePersonaRolesAndBoundaryCappedCdkRoles`) with `roleARN = cdk-hnb659fds-cfn-exec-role-*`, and the only CloudFormation writes by the IDE role itself were on `CDKToolkit`. So the participant principal never creates a stack under the execution role and never passes it — the deploy role does both under its own bootstrap-template policy and the boundary. The rubric's PE2 chain (`PassCdkExecRole` + `IfExists` stack writes + `IAMCdkRolesBounded`) therefore no longer starts from this policy. Simulated: `CreateStack FAST-stack` implicitDeny, `CreateChangeSet CDKToolkit` allowed, `DescribeStacks FAST-stack` allowed, `PassRole cfn-exec → cloudformation` implicitDeny. |
| `-core.json` | `S3Access` | `arn:aws:s3:::cdk-hnb659fds-assets-*` → `arn:aws:s3:::cdk-hnb659fds-assets-${aws:PrincipalAccount}-*` | The CDK staging bucket is `cdk-hnb659fds-assets-<account>-<region>`; the policy variable pins it to the caller's own account (the same device as the `STSAssumePersonaRolesAndBoundaryCappedCdkRoles` account pin). Simulated: own-account bucket allowed, another account's implicitDeny. |
| `-core-2.json` | `PassRole` | `lambda.amazonaws.com` removed from `iam:PassedToService` | No page or notebook passes a role to Lambda; with `CreateFunction` gone, the only Lambda write left is `UpdateFunctionConfiguration` on seven named functions, and this closes its `--role` re-pointing path (rubric PE4). Simulated: pass `workshop-x` to `lambda.amazonaws.com` implicitDeny, to `bedrock-agentcore.amazonaws.com` allowed. |
| `-core.json` | `STSAssumePersonaRolesAndBoundaryCappedCdkRoles` | `cdk-hnb659fds-lookup-role-*` removed | FAST performs no context lookups and the CDK CLI falls back to the current credentials for template reads; deploy and publishing roles stay. Simulated: deploy and file-publishing roles allowed, lookup role implicitDeny. |
| `-core-2.json` | `IAMCdkRolesBounded` → `IAMCdkRolesBounded` + `IAMCdkRolesBoundedManagedPolicies` | `AttachRolePolicy`/`DetachRolePolicy` on the CDK roles additionally require `ArnEquals iam:PolicyARN` ∈ {`AdministratorAccess`, `ReadOnlyAccess`} | Those are the only two managed policies `cdk-bootstrap-template.yaml` attaches (`CloudFormationExecutionPolicies`, which the CLI defaults to `AdministratorAccess`, and the lookup role's `ReadOnlyAccess`); everything else on those roles is inline (`PutRolePolicy`). Both statements keep the boundary condition. Simulated: attach `AdministratorAccess`/`ReadOnlyAccess` to a bounded role allowed, `PowerUserAccess` implicitDeny, `AdministratorAccess` to an unbounded role implicitDeny. |
| `policy-2` | `SsmParametersForWorkshopNoResourceLevel` removed; `ssm:GetParameters` joins `SsmParametersForWorkshop` | `GetParameters` takes the same parameter ARNs as `GetParameter` | The Sid was wrong: `GetParameters` supports the `parameter` resource type, so a `*` grant let the deployer read every parameter in the account. Simulated: `workshop/*` and `FAST-stack/*` allowed, `other/*` implicitDeny. |
| `policy-6` | `AasScalableTargetTagging` | statement removed | `AWS::ApplicationAutoScaling::ScalableTarget` has no `Tags` property, so CloudFormation never calls `application-autoscaling:TagResource`/`UntagResource`; the grant was dead weight. `AasEcsScalingWrites` stays as it is: scalable-target ARNs carry a generated id, `RegisterScalableTarget` is the only write that takes `aws:RequestTag` (and CloudFormation sends none), and `PutScalingPolicy`/`DeregisterScalableTarget` take no tag key at all (service reference, 2026-11), so the `ecs` service-namespace fence plus the Region fence is the ceiling. |
| `-core-3.json` | `S3BucketPolicyOnCdkStagingBucketOnly`, `CdkBootstrapStagingBucketConfiguration` | `arn:aws:s3:::cdk-hnb659fds-assets-*` → `arn:aws:s3:::cdk-hnb659fds-assets-${aws:PrincipalAccount}-*` | Same account pin as `S3Access` in `-core.json`: the staging bucket is `cdk-hnb659fds-assets-<account>-<region>`, and bucket-policy and bucket-configuration writes now reach only the caller's own account's bucket. |
| `policy-3` | `AgentRegistryGetDeleteNoConditionKeysAvailable` (named `AgentRegistryGetDeleteNoConditionKeysAvailable` until 2026-11) | unchanged, evidence recorded | `agent-registry:DeleteRegistry` and `GetRegistry` have the `registry` resource type but **no condition keys** in the service reference (2026-11) — `aws:ResourceTag/Workshop` is not evaluated for them — and registry ids are service-generated, so neither a tag fence nor a name pattern is available; the Region fence is the ceiling. |
| `-core-2.json` | `IAMCdkRolesBounded` | `iam:UpdateAssumeRolePolicy` and `iam:PutRolePermissionsBoundary` removed | CloudTrail (event 7, us-east-1) shows `cdk bootstrap` under the IDE role calling neither: the five roles are created with the boundary in `CreateRole`, their trust policies never change afterwards, and the re-bootstrap with the shipped template updated them without either call. Simulated: both implicitDeny; `CreateRole`/`PutRolePolicy` on a bounded CDK role still allowed. |
| `-core-2.json` | `IAMCdkRolesBoundedManagedPolicies` → `IAMCdkExecRoleAdminUnderBoundary` + `IAMCdkLookupRoleReadOnlyUnderBoundary` | `AdministratorAccess` may be attached only to `cdk-hnb659fds-cfn-exec-role-*`, `ReadOnlyAccess` only to `cdk-hnb659fds-lookup-role-*` | Exactly the two `AttachRolePolicy` calls CloudTrail shows for `cdk bootstrap`. Simulated: Admin→cfn-exec allowed, Admin→lookup or deploy implicitDeny, ReadOnly→lookup allowed, ReadOnly→cfn-exec implicitDeny. |
| `-core-2.json` | `ACIdentityManage` | `workload-identity-directory/default*` → the directory plus `…/default/workload-identity/registry-*` | The only workload identity a participant deletes is the `registry-<registryId>` one that `delete-registry` cascades into under the caller's principal (Cleanup page); `ac-agent-identity` is stack-owned and stays untouchable. The directory ARN stays because the service reference lists both resource types for the action. Simulated: `registry-*` allowed, `ac-agent-identity` implicitDeny. |
| `-core-2.json` → `-core-3.json` | `LogsReadOnly`, `SecretsManagerListOnly` | moved unchanged | Room for the two managed-policy statements above; `-core-3.json` already holds the CloudWatch statements. |
| `-core-2.json` → `-core-4.json` | `IAMCdkRoleTagsBootstrapKeysOnly` | `ForAllValues:StringEquals aws:TagKeys = Workshop` added; statement moved to `-core-4.json` | `iam:TagRole`/`UntagRole` offer no `iam:PermissionsBoundary` key (service reference), so the boundary condition the rubric asked for cannot be expressed; the tag-key fence pins the grant to the one key `cdk bootstrap --tags` sets (CloudTrail, event 7: five `TagRole` calls, keys = {`Workshop`}). Moved because `-core-2.json` would have exceeded the quota by 22 characters. Simulated: `Workshop` allowed, `Environment` implicitDeny. |
| `-core.json` | `STSAssumePersonaRolesAndBoundaryCappedCdkRoles` | CDK role patterns pinned to the three validated Regions (`…-deploy-role-*-us-west-2` / `-us-east-1` / `-eu-west-1`, same for `…-*-publishing-role-*-…`) | The role name ends in the Region the bootstrap ran in; `contentspec.yaml` and the self-service page allow exactly these three. Simulated: `us-west-2` deploy role allowed, `eu-central-1` implicitDeny. |
| `-core.json` | new `ACTokenVaultLazyCreateExactActionUnknownToAccessAnalyzer` | `bedrock-agentcore:Create*` on the bare `token-vault/default` ARN, region-fenced | Event 9 proved `CreateOauth2CredentialProvider` still authorizes `bedrock-agentcore:CreateTokenVault` with the vault present (notebook 06, 403); `Create*` is the only spelling the build's Access Analyzer gate accepts for an action it does not know, and the bare vault ARN keeps every other `Create*` (all on sub-path ARNs) out. See the `CreateTokenVault` note above. |
| `-core.json`, `-core-2.json`, `-core-4.json`, `-network.json` → new `-core-5.json` | `ACIdentityScoped`, `ACTokenVaultLazyCreateExactActionUnknownToAccessAnalyzer`, `ACIdentityRead`, `SSM`, `EB`, `EBList`, `SM`; `ACPolicyManage`; `IAMCdkRoleTagsBootstrapKeysOnly`; `EFSDescribeNoResourceLevel` | moved unchanged into the seventh file | IAM's real size count rejected `-core.json` at deploy time (event 10); every file now has at least 200 characters of headroom by that count. |
| `policy-3` | `ServiceDiscoveryUntagNeverTheWorkshopKey` | statement removed | `servicediscovery:UntagResource` has no resource type (service reference); a tag-key guard still allowed stripping every other tag from every namespace in the account; CloudTrail from an event deploy shows no `UntagResource` call (tags are set inline at create and never change). |
| `-core-2.json` | new `DenyTouchingTheWorkshopBoundaryPolicy` | `Deny` `DeletePolicy`, `CreatePolicyVersion`, `DeletePolicyVersion`, `SetDefaultPolicyVersion`, `Tag/UntagPolicy` on `policy/workshop-permissions-boundary` | `IAMPolicy` grants policy lifecycle on `policy/workshop-*`, a prefix the boundary's own name matches; the explicit Deny makes the guardrail unreachable from the participant policy itself, independent of the boundary's own self-protection (which only binds *bounded* principals). |
| `-core.json` | `S3Access` + new `S3ObjectAclForFastAmplifyStaging` | `arn:aws:s3:::cdk-*` → `cdk-hnb659fds-assets-*`; `s3:PutObjectAcl` moved out of `S3Access` and kept only on `fast-*/*` | The CDK staging bucket enforces bucket-owner ownership, so object ACL writes there were dead weight. FAST's frontend deploy is the one caller: `deploy-frontend.py` uploads the build to the FAST Amplify staging bucket and Amplify `StartDeployment` then needs `s3:PutObjectAcl` on that object — test event 7 failed the frontend step with `AccessDenied` on exactly that action when the grant was dropped wholesale (2026-11-08). |
| `policy-2` | `DocumentDbForDataStack` | `cluster-pg:*` → `cluster-pg:workshop-*` | The only cluster parameter group the templates create is `${EnvironmentName}-documentdb-params` (`data-stack.yaml`); the cluster, instance and subnet-group ARNs in the same statement already use the prefix. |
| `-core-2.json` | `PassRole` | `ecs-tasks.amazonaws.com` removed from `iam:PassedToService` | No page or notebook registers a task definition or runs a task (`aws ecs` appears only in the observability dashboard script, which lists services), so the pass to ECS only served the "run an arbitrary container under a platform role" path. The ECS write grants in `-infra.json` stay for `update-service`-style operations, which pass no role. |
| `-core-2.json` | `ACTokenVaultManage` | `bedrock-agentcore:Create*` → `CreateOauth2CredentialProvider` | The token vault is bootstrapped at provisioning (see the Access Analyzer note above). |
| `-core-3.json` | `CdkBootstrapStagingBucketConfiguration` | `arn:aws:s3:::cdk-*` → `cdk-hnb659fds-assets-*` | Same prefix the bucket-policy statement already used; nothing else `cdk-*` is touched. |
| `-core-3.json` | `LambdaUpdateCodeAutoReview` | statement removed | Code-overwrite plus the existing `InvokeFunction` on `ac-*` was arbitrary code under the function's role (PE4); nb 04 Step 10a.5 is verify-only now (see the `lambda:UpdateFunctionCode` section). |
| `-core.json`, `policy-7` | `BedrockCatalogNoResourceLevel` / `BedrockModelDiscoveryNoResourceLevel` | `bedrock:PutFoundationModelEntitlement` removed | Belonged to the retired console *Model access* page; nothing in the workshop calls it. |
| `policy-1` | `Ec2DeleteGuardDutyManagedNetworkResources` | `aws:ResourceTag/GuardDutyManaged = true` | GuardDuty tags its managed endpoint and SG; see above. |
| `-core-3.json` | `Lambda` + `LambdaUpdateConfigurationPlatformFunctionsOnly` | `lambda:CreateFunction` removed; `UpdateFunctionConfiguration` now carries `Null lambda:Layer = true` | No page or notebook creates a function: every workshop Lambda is created by its stack under the deployer identity or, for FAST, by the bounded CDK execution role. With `CreateFunction` next to `iam:PassRole` (workshop-* roles to Lambda, `-core-2.json`) and `InvokeFunction`, a participant could have created a function under a platform role and invoked it (PE2/PE4). `DeleteFunction`, `RemovePermission` and `GetPolicy` went with it: they only ever served a participant-run `delete-stack` of the two pre-deployed stacks, and since `CFNParticipantStacks` the participant cannot delete a `workshop-*` stack at all (the teardown runs under the deployer identity, whose `policy-5` keeps the full Lambda lifecycle). The `lambda:Layer` condition keeps the seven reconfigurable functions from gaining a layer (the only code-bearing input `UpdateFunctionConfiguration` accepts); the content only changes `--environment`, which carries no layer. Simulated: no `Layers` in the request → allowed, any layer ARN → implicitDeny. |
| `-core-3.json` | `LambdaUpdateConfigurationPlatformFunctionsOnly` | seven functions → the five the content reconfigures (`agentcore-gateway-sync`, `agentcore-gateway-response-interceptor`, `ac-gateway-request-interceptor`, `ac-gateway-response-interceptor`, `ac-auto-review`) | `grep` of every `update-function-configuration` / `update_function_configuration` call in `content/` and the notebooks: the Module 3a sync page and notebooks edit `agentcore-gateway-sync`, the guardrail pages edit the response interceptors, Module 3b step 7 edits both `ac-gateway-*` interceptors, notebook 04 toggles `AUTO_APPROVE` on `ac-auto-review`. Nothing touches `agentcore-gateway-request-interceptor` or `ac-registry-gateway-sync`, so they are out. The handler/environment residual on the remaining five is the documented one (no IAM condition key exists; `iam:PassRole` to Lambda is gone; the functions run under boundary-bearing roles). |
| `policy-3` | `AgentRegistryManage` → `AgentRegistryGetDeleteNoConditionKeysAvailable` | Sid only | The statement is unchanged; its name now states the accepted ceiling the service reference imposes (`DeleteRegistry`/`GetRegistry` have the `registry` resource type and no condition keys, and registry ids are service-generated). |
| `policy-1` | `Ec2NetworkCreateScopedToResourceTypes` | `DeleteNetworkInterface`, `ReleaseAddress` moved into `Ec2ModifyTagDeleteWorkshopTaggedOnly` | Stack-created ENIs/EIPs carry the propagated Workshop tag; nothing deletes foreign ones. |
| `policy-1` | `Ec2IdeCreateScopedToResourceTypes` → `Ec2IdeCreateSupportingResourceTypes`, `Ec2IdeCreateWorkshopTaggedOnly`, `Ec2IdeAddressWorkshopTaggedOnly` | `RunInstances` on `instance/*` requires `aws:RequestTag/Workshop` (CloudFormation puts the stack tag in the RunInstances request); the subnet/SG/ENI/image/snapshot/key-pair/launch-template/volume evaluations stay region-only; EIP (dis)association requires the Workshop resource tag on both the address and the instance | Only `instance/*` reliably carries the request tag — volume `TagSpecifications` depend on `PropagateTagsToVolumeOnCreation`, so the volume evaluation is left unconditioned rather than denying every `RunInstances`. `CreateVolume` was dropped: no template declares a standalone volume. |
| `policy-7` | `ACPolicyEngineManage` | `policy-engine/*` → `policy-engine/workshop_policy_engine-*` | `ENGINE_NAME = "workshop_policy_engine"` in Module 3b step 7 / nb 07; engine IDs are `<name>-<suffix>`. `ACPolicyEngineCreate` stays on `policy-engine/*` because the ID does not exist at create time. |
| `policy-7` | `BedrockGuardrails` + new `BedrockGuardrailsUpdateDeleteTaggedOnly` | update/delete require the Workshop resource tag; create/apply/get/version/tag stay | The deployer never creates a guardrail (no template does); the fence only matters if someone later points this role at one. |
| `-core-3.json` | `AmplifyAccess` → `AmplifyRead`, `AmplifyWriteScopedToApps` | reads and app/branch writes split; both region-fenced on the typed `apps/*` ARN | A first cut added `aws:ResourceTag/Workshop` to the writes; the Workshop Studio build's Access Analyzer pass reported `ALLOW_WITH_UNSUPPORTED_TAG_CONDITION_KEY_FOR_SERVICE` because no Amplify action lists `aws:ResourceTag` as an action-level key (service reference: only `CreateApp`, `CreateBranch`, `TagResource` take `aws:RequestTag`/`aws:TagKeys`), so the statement would have matched nothing and FAST's Amplify deploy would have failed. The typed ARN plus the region fence is what the service offers. |
| `-network.json` | `EC2VPCWriteScopedToResourceTypes` → creates only, new `EC2VPCModifyWorkshopTaggedOnly` | `ModifyVpcAttribute`, `Attach/DetachInternetGateway`, `ReleaseAddress`, `CreateRoute`, `Associate/DisassociateRouteTable`, `DeleteNetworkInterface` require the Workshop resource tag | Mirrors `EC2DeleteWorkshopTaggedOnly`; stack-created network resources carry the propagated tag. |
| `policy-3` | `CognitoManageWorkshopUserPool` | `aws:ResourceTag/Workshop` added | The registry pool carries the tag explicitly (`data-stack.yaml`); closes the tag-first bypass of the `DeleteUserPool` guard, since `TagResource` is in the same statement. |
| `policy-3` | `ServiceDiscoveryCreateAndListNoResourceLevel` → `TagResource` split into `ServiceDiscoveryTagWorkshopOnly` | `aws:RequestTag/Workshop` on `servicediscovery:TagResource` | Same bypass closure for `DeleteNamespace`. |
| `policy-3` | `ServiceDiscoveryTagWorkshopOnly` removed; `ServiceDiscoveryCreateAndListNoResourceLevel` → `ServiceDiscoveryCreateNamespaceWorkshopTagged` + `ServiceDiscoveryListNoResourceLevel` | `servicediscovery:TagResource` is gone; `CreatePrivateDnsNamespace` requires `aws:RequestTag/Workshop = AgentCore-Platform` | The rubric found the cross-statement bypass: `TagResource` on `*` with only a request-tag fence let the deployer stamp `Workshop=AgentCore-Platform` on any namespace and then delete it through `ServiceDiscoveryDeleteTagged`. `TagResource` has no resource type (service reference), so a resource-tag fence is not available. CloudTrail from event 7 (2026-11-08) shows CloudFormation's `CreatePrivateDnsNamespace` carrying the stack tags inline (`Workshop`, `Environment`, `Name`, `aws:cloudformation:*`) and no `servicediscovery:TagResource` call at all (the ECS-managed Cloud Map services are created by `ecs.amazonaws.com` under its own identity), so the grant was dead weight and the create-time request-tag fence now guarantees the tag that `ServiceDiscoveryDeleteTagged` keys on. Simulated: create with the tag allowed, without implicitDeny, `TagResource` implicitDeny. |
| `policy-4` → `policy-3` → `policy-5` | `Route53ManageCloudMapPrivateDnsHostedZone` → `Route53CloudMapPrivateZones` | region fence added, and `ForAllValues:StringLike route53:ChangeResourceRecordSetsNormalizedRecordNames = *.local` | Hosted zones expose no `aws:ResourceTag`/`aws:RequestTag` keys (service reference), so the typed ARN stays `hostedzone/*`; what IAM does offer is the record-name key, and every record Cloud Map writes for the `${EnvironmentName}.local` namespace ends in `.local`, so record changes can no longer touch a public zone. `ForAllValues` evaluates true when the key is absent, so `DeleteHostedZone`/`GetHostedZone`/VPC association are unaffected. Moved to `-5` (the Sid shortened to fit: `-5` is at 6,136) because `-3` had no room for the condition block. |
| `policy-4` | `CloudFrontReadTagAndInvalidateDistribution` → `CloudFrontReadDistribution`, `CloudFrontTagOnCreateWorkshopOnly`, `CloudFrontManageTaggedOnly` | same split as `-infra.json` | Closes the tag-then-delete path next to `CloudFrontDeleteAndUpdateTaggedOnly`. |
| `-core.json`, `-core-4.json`, `-core-5.json` | `STSAssume` → `STSAssumePersonaRolesAndBoundaryCappedCdkRoles`, `OmniWriteNoResourceLevel` → `OmniWriteNoResourceTypesInServiceReference`, `ACTokenVaultLazyCreate` → `ACTokenVaultLazyCreateExactActionUnknownToAccessAnalyzer` | Sid renames only; no action, resource or condition changed | The three statements that stay at their documented ceiling now carry the reason in the Sid, where a reader of the JSON (or a policy scanner) sees it without this file: the CloudWatch service reference defines no resource type for the Omni write actions (only the `cloudwatch:HasAccessGrant` key); IAM Access Analyzer, which the Workshop Studio build runs over every `iamPolicies` file, rejects `bedrock-agentcore:CreateTokenVault` as a non-existent action (re-checked 2026-11-08); the CDK execution role the deploy role passes is capped by the shipped bounded bootstrap template. Sizes at the time of this change: core 3,997, core-4 5,785, core-5 3,087 by IAM's count (see the table at the top for current values). |
| `-core-5.json` | new `Deny` `ACTokenVaultDenyUnusedCreateActionsTheWildcardWouldAllow` | `bedrock-agentcore:CreateApiKeyCredentialProvider`, `CreatePaymentCredentialProvider` denied on `token-vault/default*` | The service reference lists exactly three `Create*` actions with the `token-vault` resource type (OAuth2, API-key and payment credential providers) and no `CreateTokenVault`; the workshop calls only `CreateOauth2CredentialProvider` (already allowed by `ACIdentityScoped`). Denying the two unused siblings pins what the unavoidable `Create*` wildcard can reach inside the policy document: the OAuth2 create and the unpublished `CreateTokenVault` the service authorizes on it. Zero effect on any page or notebook (`grep` of `content/` and `source/`: no API-key or payment provider call). |
| `-core-5.json` → `code-editor.yaml` (IDE role inline policy `TokenVaultLazyCreate`) | `ACTokenVaultLazyCreateExactActionUnknownToAccessAnalyzer` and the `Deny` removed; new inline Sid `ACTokenVaultLazyCreateExactAction` | `bedrock-agentcore:Create*` on `token-vault/default` → exact `bedrock-agentcore:CreateTokenVault` on `arn:aws:bedrock-agentcore:*:<account>:token-vault/default`, three Regions | The build's Access Analyzer gate only covers the `iamPolicies` files, so the exact action can be named in the template (as the agentcore stack's TokenVaultInit role already does). The IDE role is the only identity that runs `CreateOauth2CredentialProvider`; it now holds both actions by name and no write wildcard. The WSParticipantRole loses the undocumented `CreateTokenVault` — not a content path. core-5 is 2,808 by IAM's count. |
| `-core.json` | new `ACGatewaySyncAuthorizedOnWildcardArnByCreateGateway` (restores the `ACGatewaySyncOnCreate` statement removed on 2026-11-08 as "redundant") | `bedrock-agentcore:SynchronizeGatewayTargets` on `gateway/*`, region-fenced | Event 11 (build bc5bb7dc): `create_gateway.py` failed with `not authorized to perform: bedrock-agentcore:SynchronizeGatewayTargets on resource: …:gateway/*` and every Module 3a block after it failed downstream (no gateway id). `CreateGateway` authorizes the sync action against the wildcard ARN because no id exists yet; `ACGatewayManage` grants the same action only on `gateway/tools-gateway-*` / `gateway/ac-tools-gateway-*`, which cannot match. The two statements are not duplicates: same action, different resource the service checks. |
| `-core-5.json` | `IAMCdkRoleTags` → `IAMCdkRoleTagsBootstrapKeysOnly` | `ForAllValues:StringEquals aws:TagKeys = Workshop` → `ForAllValues:StringLike aws:TagKeys = [Workshop, aws-cdk:bootstrap-role, aws:cloudformation:*]` | Event 11 (build bc5bb7dc): `npx cdk bootstrap` failed with `not authorized to perform: iam:TagRole on resource: …:role/cdk-hnb659fds-file-publishing-role-…` and both Module 4 `cdk deploy` blocks and everything after them failed downstream. The bootstrap template tags every role `aws-cdk:bootstrap-role` (grep `Tags:` in `source/module-4b-fast/cdk-bootstrap-template.yaml`) and CloudFormation may add its reserved `aws:cloudformation:*` system tags; no bootstrap role ever carries `Workshop`, so the 2026-11-08 fence denied every bootstrap. The fence now names the keys the bootstrap writes plus the workshop's own `Workshop` key (the set CloudTrail showed in event 7); `aws:`-prefixed keys cannot be set by callers, so the pattern adds no reach. Resource stays `role/cdk-hnb659fds-*`. |

Not changed on purpose: `ecs:DeregisterTaskDefinition` (`EcsDeregisterTaskDefinitionNoResourceLevel`,
`policy-6`) — the two probe sections above show IAM evaluating it against `*`. A scoped
version has been tried twice: the first attempt reached a deployed stack and left
`workshop-llm-gateway-stack` in `DELETE_FAILED` on `Delete TaskDefinition` (the incident the
`ecs:DeregisterTaskDefinition` section records), and a second attempt in 2026-11 was reverted
the same day, before any deploy saw it, on the strength of that record; `cloudfront:DeleteOriginAccessControl` (`CloudFrontManageOac`) —
origin access controls accept no tags and no condition key (service reference), and the
typed ARN is the only constraint available; `logs:PutResourcePolicy`/`DeleteResourcePolicy`
and the X-Ray Transaction Search settings in `policy-6`, which are account-level APIs with
no resource ARN to scope (the region fence is all IAM offers); and `aws-marketplace:Subscribe`
(see its own section). Three more self-service items (`policy-3`, `policy-5`) were
measured against the service references and left alone, each for a concrete reason:
`Route53CloudMapPrivateZones` keeps `DeleteHostedZone` on `hostedzone/*` (hosted zones expose
no `aws:ResourceTag` or `aws:RequestTag` key, and Cloud Map creates the zone under the
deployer's own credentials, so the deployer must hold the writes; the record-name fence above
is what the service offers, and the one further fence that would fit — `aws:ViaAWSService:
true`, since nothing calls Route 53 directly — has not been exercised by a self-service deploy
yet and is not shipped untested); `AgentRegistryGetDeleteNoConditionKeysAvailable` (the registry is created by participants in
Module 3b, not by a template, so no tag is guaranteed to be on it when the deployer deletes
it). `servicediscovery:UntagResource` is no longer granted at all: the service reference
lists no resource type for it (only `aws:TagKeys`), a tag-key guard still left every other tag
on every namespace in the account removable, and CloudTrail from an event deploy shows
CloudFormation never calling it (the namespace is tagged inline at create and
`deploy-cfn.sh` passes the same `--tags` on every run, so no tag is ever removed). And the two escalation items the scanner
rates HIGH — `IAMRole` in `-core-2.json` and `STSAssumePersonaRolesAndBoundaryCappedCdkRoles` in `-core.json` — are the
subject of the next section, not of this table.

Every participant file in the table was re-validated with `accessanalyzer validate-policy`
(0 findings each) and re-measured against the 6,144-byte quota after the change; the
self-service files were measured and `policy-7` still shows only its documented
`CreateTokenVault` INVALID_ACTION.

## IAM escalation — closed with a permissions boundary (2026-11)

Until 2026-11 this section documented an *accepted* residual: any principal holding
`iam:CreateRole` + `iam:PutRolePolicy` + `iam:PassRole` on `role/workshop-*` can write
`{"Action":"*","Resource":"*"}` onto a role and pass or assume it, and the two partial
fixes on offer (an `iam:PolicyARN` allow-list, or trimming the resource prefixes) were
measured and rejected because they leave `PutRolePolicy` open while costing hundreds of
bytes. The Workshop Studio build rubric rates the path HIGH in both policy sets, so the
real closure was built instead. The history of the rejected partial fixes is kept below
as "why not the cheap version", because it is still the right analysis.

### The design

One IAM managed policy, **`workshop-permissions-boundary`**, created by
`workshop-llm-gateway-stack.yaml` (the first stack in both `contentspec.yaml` and
`./deploy-cfn.sh`) and referenced by its fixed name everywhere else. Its document:

| Statement | Effect |
|---|---|
| `AllowWorkshopRegionsExceptIdentityAndOrgManagement` | `Allow` `NotAction` `iam:*`, `organizations:*`, `account:*`, `billing:*`, `sso:*`, `sso-directory:*` and the two Bedrock inference actions on `*`, fenced with `aws:RequestedRegion` ∈ {`${AWS::Region}`, `us-east-1`} — the same region fence the participant policies use (global services report `us-east-1`; the assets bucket is in `us-east-1`). A boundary is a ceiling, and workloads keep everything they do today: all ten templates deploy into one region. |
| `BedrockInferenceFollowsCrossRegionProfiles` | `bedrock:InvokeModel` and `InvokeModelWithResponseStream` (the Converse API is authorized through these; there is no `bedrock:Converse` action) on the typed `foundation-model/*` (Region-less form included), `inference-profile/*` and `application-inference-profile/*` ARNs, **without** the Region fence: cross-Region inference profiles (`global.*`, `us.*`) authorize the foundation model in the destination Region, and the fenced version denied the LLM Gateway task role `bedrock:InvokeModel on arn:aws:bedrock:::foundation-model/anthropic.claude-sonnet-4-6` "because no permissions boundary allows" it (test event 7, 2026-11-08). Mirrors `BedrockInvokeModelsScoped` in `-core.json`, which has never carried the fence for the same reason. |
| `IamReadOnly` | `iam:Get*`, `iam:List*` on `*` (`ListRoles`, `ListPolicies`, `GetAccountSummary` take no ARN) |
| `IamPassRoleAndServiceLinkedRolesInThisAccount` | `PassRole`, `CreateServiceLinkedRole`, `TagRole`/`UntagRole` on `arn:…:iam::<account>:role/*` (service-linked roles live under `role/aws-service-role/*`) |
| `IamPolicyAndInstanceProfileLifecycleInThisAccount` | managed-policy and instance-profile lifecycle plus their tags on `arn:…:iam::<account>:policy/*` and `:instance-profile/*` |
| `RoleWritesOnlyWhenTheRoleCarriesThisBoundary` | `CreateRole`, `DeleteRole`, `UpdateRole*`, `UpdateAssumeRolePolicy`, `Put/DeleteRolePolicy`, `Attach/DetachRolePolicy`, `PutRolePermissionsBoundary` on `role/*` in this account — **only** when `iam:PermissionsBoundary` equals this policy's ARN (self-propagation: a bounded principal can only make more bounded principals; every one of these actions carries the key per the service reference) |
| `DenyRemovingTheBoundary` | `Deny` `DeleteRolePermissionsBoundary`, `DeleteUserPermissionsBoundary` |
| `DenyChangingTheBoundaryPolicyItself` | `Deny` `CreatePolicyVersion`, `DeletePolicy`, `DeletePolicyVersion`, `SetDefaultPolicyVersion` on the boundary's own ARN |

The first cut had the `NotAction` statement and the IAM grants on an unconditioned `*`.
The Workshop Studio build's code scan (Checkov, `CKV_AWS_107`–`CKV_AWS_111`) rated that
HIGH five times over, and it had a point: a `NotAction` ceiling on `*` with no fence reads
as "everything, everywhere" to any reviewer, human or not. The shape above is what the
service offers instead — the region fence the participant policies already use, and
account-typed IAM ARNs (`role/*`, `policy/*`, `instance-profile/*`) wherever IAM has a
resource type. Only `iam:Get*`/`iam:List*` keep a bare `*`, because `ListRoles`,
`ListPolicies` and `GetAccountSummary` have no ARN. `checkov -f
static/cfn/llm-gateway/workshop-llm-gateway-stack.yaml --check CKV_AWS_107,…,CKV_AWS_111`
passes; CDK bootstrap and FAST deploys were re-run under the fenced boundary in a test
event (see `research/module5-live-report.md`).

Who carries it:

- **Every `AWS::IAM::Role` in the ten templates** (42 roles) has
  `PermissionsBoundary: arn:…:policy/workshop-permissions-boundary` (`!Ref` inside the
  LLM-gateway stack, the fixed ARN elsewhere). The one exception is `OmniSpaceAccessRole`:
  CloudWatch Omni requires the space access role to carry the AWS managed
  `CloudWatchOmniSpaceAccessPolicy` as its boundary, and a role takes exactly one.
- **The five CDK bootstrap roles**: Module 4 runs
  `npx cdk bootstrap --template /workshop/source/module-4b-fast/cdk-bootstrap-template.yaml
  --custom-permissions-boundary workshop-permissions-boundary`. The shipped template is the
  CDK CLI's own bootstrap template (version 30, from the CLI FAST v0.4.1 pins) with
  `InputPermissionsBoundary` applied to **all five** roles. The stock template applies it to
  `cdk-hnb659fds-cfn-exec-role-*` only (the role it otherwise creates with
  `AdministratorAccess`), and leaves the file-publishing, image-publishing, lookup and
  deployment roles unbounded — which `IAMCdkRolesBounded` forbids, so the stock bootstrap
  failed on `FilePublishingRole` with `AccessDenied` on `iam:CreateRole` in test event 7
  (2026-11-08) and succeeded with the shipped template in the same account minutes later.
  The deploy page and nb 02 verify the execution role's boundary before deploying.
- **Every role FAST's CDK app creates**: the Module 4 deploy step writes
  `"@aws-cdk/core:permissionsBoundary": {"name": "workshop-permissions-boundary"}` into
  `infra-cdk/cdk.json`, so every `cdk deploy` (including the later redeploys) attaches it.

What the two policy sets now grant:

- `-core-2.json`: `IAMRoleRead` (reads on `workshop-*` and `cdk-hnb659fds-*`),
  `IAMCdkRolesBounded` (the role writes on `cdk-hnb659fds-*` **only**, with
  `ArnLike iam:PermissionsBoundary = arn:aws:iam::*:policy/workshop-permissions-boundary`;
  `ArnLike` because the participant policy cannot know the account id and
  `StringEquals` never matches a `*`). `IAMCdkRoleTagsBootstrapKeysOnly` (`TagRole`/`UntagRole`, which
  take no boundary condition key) sits in `-core-5.json` since the seven-file split. Participants hold **no** write on `workshop-*` roles:
  nothing in the workshop creates one outside CloudFormation (`create_role(` has zero hits
  across `source/` and `content/`), so the grant was dead weight that only served the
  escalation.
- `-core.json` `STSAssumePersonaRolesAndBoundaryCappedCdkRoles`: the three persona roles by name
  (`workshop-ac-registry-{admin,publisher,consumer}-*`) plus the three CDK roles (deploy, file publishing, image publishing); the
  `role/workshop-*` wildcard is gone.
- `self-service-deploy-policy-4.json`: `IamRoleWritesOnlyWithWorkshopBoundary` carries the
  same role writes with `ArnLike iam:PermissionsBoundary` in
  {`workshop-permissions-boundary`, `CloudWatchOmniSpaceAccessPolicy`} (the Omni role);
  `IamRolesAndPoliciesScopedToWorkshopPrefix` keeps the reads, tags and managed-policy
  lifecycle. `IamCreateServiceLinkedRoleOnly` was rewritten as
  `aws-service-role/*` + `iam:AWSServiceName` (same eleven services, ~400 bytes smaller) to
  make room.

Why this closes the path: a participant (or the self-service deployer) can still create
roles, but only bounded ones; a bounded role can hold `AdministratorAccess` and still do
nothing IAM-shaped beyond creating further bounded roles, cannot shed its boundary, and
cannot edit the boundary policy. The CloudFormation execution role that CDK deploys with
is bounded the same way, so "deploy an arbitrary template through CDK" no longer reaches
admin either.

**Upgrade caveat (self-service accounts deployed before 2026-11).** `policy-4` now
conditions `PutRolePolicy`/`AttachRolePolicy`/`DeleteRole` on the *existing* role's
boundary, and roles created before this change have none. Updating such a stack can fail
on the first role write; the fix is to let CloudFormation add the boundary first (re-run
`./deploy-cfn.sh deploy`; `PutRolePermissionsBoundary` is allowed because the condition is
evaluated on the *new* boundary) or, if the update order still bites, attach
`workshop-permissions-boundary` to the existing `workshop-*` roles by hand once. Fresh
accounts and Workshop Studio events are unaffected.

### Why not the cheap version (kept for the record)


`content/introduction/getting-started/self-service.en.md` links here for the full
argument. In short: **these policies bound blast radius, not privilege.**

The deploy must create the workshop's IAM roles, so the principal necessarily
holds `iam:CreateRole` + `iam:PutRolePolicy` + `iam:PassRole`→Lambda on
`role/workshop-*`. Any principal holding those three can write an inline
`{"Action":"*","Resource":"*"}` policy onto a `workshop-*` role and then either
assume it or pass it to a Lambda it also controls. That is administrator access,
reached without touching anything this policy set denies.

Two consequences worth writing down, because both were proposed and rejected:

1. **An `iam:PolicyARN` condition that blocks attaching `AdministratorAccess` is
   security theater.** It closes one spelling of the escalation and leaves
   `PutRolePolicy` — the shorter path — wide open. Shipping it would buy a clean
   scanner line and a false sense of containment.

   This was written, measured, and reverted once, so the argument is now concrete
   rather than theoretical. An `ArnLikeIfExists` allow-list on `iam:PolicyARN` was
   added to `IAMRole` in `-core-2.json` and then removed, for three reasons:

   - **It cannot exclude `AdministratorAccess`.** `cdk bootstrap --show-template`
     attaches `AdministratorAccess`, `ReadOnlyAccess`, and
     `AWSCloudFormationReadOnlyAccess` to the CDK execution roles. Any allow-list
     that omits them breaks `cdk bootstrap`, so the list has to *permit* the exact
     policy the condition was supposed to block. What remains is a bar on attaching
     unrelated managed policies — while `PutRolePolicy` in the same statement can
     write `{"Action":"*","Resource":"*"}` inline.
   - **It cost 370 of 443 bytes of headroom** on what was then the tightest file
     in the set, leaving 73. See the size table above for the current figures.
   - **`ArnLikeIfExists`, not `ArnLike`.** Worth recording in case anyone retries
     this: `iam:PolicyARN` is present only on `AttachRolePolicy`/`DetachRolePolicy`.
     A plain `ArnLike` would evaluate false for `CreateRole` and `PutRolePolicy`,
     which never carry the key, and deny the whole deploy. That trap is the reason a
     retry is more likely to break the workshop than to secure it.

   No scanner finding ever asked for this. The one `AdministratorAccess` HIGH in the
   history was about `CodeEditorInstanceBootstrapRole`, which was fixed for real by
   replacing the managed policy with five scoped ones.
2. **The only real closure is an IAM permissions boundary**, and a role accepts
   exactly **one**. Applying boundaries here means authoring a coarse
   service-namespace allowlist (everything the stacks need, minus `iam:*`,
   `sts:*`, `organizations:*`) and attaching it to all **43** `AWS::IAM::Role`
   resources declared across the 10 templates in `static/cfn/` — plus the Module 4
   CDK app — and keeping it in sync as templates change. That analysis is only
   tractable because **no role is created outside CloudFormation**: `create_role(`
   has zero hits across `source/` and `content/`.

That is why the closure above is a boundary and not a trimmed allow-list. The other
containment layers the content insists on — a dedicated, disposable account per
participant, `deployableRegions`, and the Workshop Studio SCP — still apply on top.

The negative test suite used to assert `iam:PutRolePolicy` / `iam:AttachRolePolicy` on a
`workshop-*` role as **allow** (`accepted+documented`); since 2026-11 the participant
set grants neither, and the self-service set grants them only on bounded roles.

### `sts:AssumeRole` — a tightening that could not be attached

The self-service set grants `sts:AssumeRole` on `arn:aws:iam::*:role/workshop-*`, with
a `*` **account** field (the participant set did too until 2026-11; its `STSAssumePersonaRolesAndBoundaryCappedCdkRoles` in
`-core.json` now names the three persona roles and the CDK deploy and publishing roles,
pinned to the caller's account with `aws:ResourceAccount = ${aws:PrincipalAccount}` —
see "Assuming CDK bootstrap roles"). An earlier pass treated the wildcard as a defect and
tightened the self-service copy to a policy variable:

```json
"Resource": ["arn:aws:iam::${aws:PrincipalAccount}:role/workshop-*"]
```

**That document cannot be attached to anything.** IAM rejects it outright:

```
$ aws iam create-policy --policy-name X \
    --policy-document file://static/cfn/self-service-deploy-policy-4.json
An error occurred (MalformedPolicyDocument): The policy failed legacy parsing
```

Bisecting the file one statement at a time showed 11 of 12 accepted and only this
one rejected. The account field of an ARN takes 12 digits, `*`, `aws`, or empty —
a `${...}` variable is not a legal value there, in an attached policy exactly as
in an embedded one. The *resource* part of an ARN is different: IAM accepts policy
variables there, which is why `arn:aws:s3:::cdk-hnb659fds-assets-${aws:PrincipalAccount}-*`
(`S3Access` in `-core.json`, the two staging-bucket statements in `-core-3.json`) attached
cleanly — all six `CodeEditorParticipantPolicy*` managed policies of that build went
`CREATE_COMPLETE` in the event 9 account on 2026-11-08 (the seventh, `Core5`, was split out
afterwards) — and why the account pin on `STSAssumePersonaRolesAndBoundaryCappedCdkRoles` is a
`Condition` (`aws:ResourceAccount`), not an ARN edit. So the reason previously recorded for the IDE copy keeping the
wildcard (`cfn-lint` **E3510** on the document embedded in `code-editor.yaml`) was
never an asymmetry at all: it is the same constraint, and cfn-lint was simply the
first tool to say so.

> **Why review missed it.** The tightening was recorded as "verified by
> simulation": `iam simulate-custom-policy` returned `allowed` for the own
> account and `implicitDeny` for `111122223333`, which reads like proof. The
> simulator takes the document as an argument and evaluates it — it never has to
> *store* it, so it never runs IAM's ARN grammar over it. Every statement about a
> policy's validity has to come from `create-policy`/`create-policy-version` (or
> a real attach), never from the simulator alone. This is the same lesson as the
> action-string trap below, one level deeper: the simulator validates neither the
> actions nor the ARNs it is given.

The wildcard that remains is not a cross-account grant in practice.
`sts:AssumeRole` is authorised twice — by the caller's identity policy *and* by
the target role's trust policy — so a `workshop-*` role in someone else's account
is reachable only if that account's role already names this principal as trusted,
which no policy here can bring about. The restriction that does the work lives on
each role the stacks create, and those trust the deploying account only.

### Validating a change

Three tools, in increasing order of authority. Use all three — each catches a
class the others cannot see.

**1. `iam simulate-custom-policy` — coverage, not correctness.** It matches action
*strings*, so it reports `allowed` for actions that do not exist; it proved
`apigateway:TagResource` "worked" for months. It also **rejects wildcards in
`--action-names`** (`InvalidInput`), and one bad name fails the whole batch — so
exclude `Create*`-style entries and verify those live instead.

> **A simulator trap that silently invalidates results.** The AWS CLI keeps only
> the **last** occurrence of a repeated option. Emitting `--context-entries` once
> per key sends exactly **one** key and drops the rest, so every condition-fenced
> statement evaluates with its context missing and reports `implicitDeny`. That one
> mistake produced 78 phantom failures and 3 phantom passes in this repo. All
> entries must go under a **single** `--context-entries` flag:
>
> ```bash
> aws iam simulate-custom-policy --policy-input-list "$DOC" \
>   --action-names lambda:CreateFunction \
>   --resource-arns arn:aws:lambda:us-west-2:123456789012:function:workshop-x \
>   --context-entries \
>     ContextKeyName=aws:RequestedRegion,ContextKeyValues=us-west-2,ContextKeyType=string \
>     ContextKeyName=aws:ResourceTag/Workshop,ContextKeyValues=AgentCore-Platform,ContextKeyType=string
> ```
>
> Confidence-check the harness itself before trusting a failure: if `MatchedStatements`
> is `[]` and `MissingContextValues` is non-empty, the harness is broken, not the
> policy. The tag value is `AgentCore-Platform` (from
> `deploy-cfn.sh --tags Key=Workshop,Value=AgentCore-Platform`), **not**
> `agentic-ai-platform`.

**2. Access Analyzer — the action catalogue, and *only* the catalogue.** It is
what catches an invented action name, and the Workshop Studio build treats
`INVALID_ACTION` as an *error*. It does **not** check whether an action supports
the ARN you paired it with: in this account/region it returned zero findings for
`s3:GetObject` on an APS workspace ARN and for `aps:CreateWorkspace` on an S3
ARN. Its silence is never evidence that a resource form is valid — use the probe
role below for that:

```bash
for f in static/cfn/self-service-deploy-policy-*.json; do
  echo "== $f"
  aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY \
    --policy-document "$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1]))))' "$f")" \
    --query 'findings[?findingType==`ERROR`]'
done
```

**3. The zero-permission probe role — the authority on both.** When the simulator
and Access Analyzer disagree, neither is evidence. Create a role that grants
*nothing* relevant, assume it, make the real API call, and read the action name and
resource ARN straight out of the 403:

```bash
aws iam create-role --role-name tmp-403probe \
  --assume-role-policy-document "$(cat <<'J'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"AWS":"arn:aws:iam::<acct>:root"},"Action":"sts:AssumeRole"}]}
J
)"
# assume it, run the real call, read the 403, then delete the role
```

This is strictly better than the other two: the simulator only string-matches, and
Access Analyzer's catalogue lags new services. In this repo the probe role settled
three questions the other tools got wrong — it disproved the
`apigateway:TagResource` claim above, caught a regression where enumerating
`Create*` dropped the genuinely-required `CreateTokenVault`, and showed
`GetTokenVault` authorizing against `/identities/get-token-vault` rather than the
token-vault ARN. Clean up the probe role and any resources it created; leaving them
behind pollutes the next scan.

The only conclusive test is still a real deploy under a principal that holds *only*
these eight policies — create a role with them attached, assume it, and run
`./deploy-cfn.sh deploy` end to end.

## `workshop-iam-policy-core-4.json`: Module 5 (CloudWatch Omni, Evaluations)

Added 2026-11 for Module 5. Its own file because `core` and `core-2` were already
within a few hundred bytes of the quota (see the size table at the top) and the
module's actions are a coherent unit that may be dropped as one file if the module
is not run. It also took the EFS statements when `-network.json` overflowed.

What it grants and why:

- `cloudwatch:GetDomain/UpdateDomain/GetSpace/UpdateSpace/TagResource` (no
  Create/Delete since the 2026-11 trim; `workshop-agentcore-stack` owns the domain
  and space): CloudWatch Omni's actions live in the `cloudwatch` namespace (endpoint
  `cloudwatch-omni`, signing name `cloudwatch`). Resource types `domain/*`,
  `space/*`, `access-grant/*`, `dataset/*`, `omni-dashboard/*` are the ones the Omni
  IAM guide lists. The `List*` calls,
  the telemetry-query calls (`StartTelemetryQuerySession`, `StartTelemetryQuery`,
  `GetTelemetryQueryResults`, `GetRecords`, `Stop*`), `GetContextGraph`,
  `CreateOneTimeDeepLinkCode`, `CreateAccessGrant` and `CreateOmniDashboard` have
  no resource type in the service reference and were denied live "on resource:
  *" when scoped, so they sit on `Resource: "*"` behind the `aws:RequestedRegion`
  fence, reads (`OmniReadNoResourceLevel`) separated from writes
  (`OmniWriteNoResourceTypesInServiceReference`).
- `observabilityadmin:GetDatasetIntegration` only (Create/Delete trimmed in 2026-11;
  the stack owns the integration): CloudWatch Logs forwards log groups into the
  space's Dataset through one integration per account and Region. The service
  reference gives the action the `dataset-integration/*` resource type, so
  `OmniDatasetIntegrationRead` is scoped to that ARN pattern (plus the Region
  fence); only `ListDatasetIntegrations` stays on `*`.
- `iam:PassRole` on exactly the three roles `workshop-agentcore-stack` creates,
  conditioned on the service each is passed to. Participants never create roles.
- `iam:CreateServiceLinkedRole` for `AWSServiceRoleForCloudWatch_Omni`, scoped to
  the `cloudwatch.amazonaws.com` path and conditioned on
  `iam:AWSServiceName = cloudwatch.amazonaws.com`: that is the principal the
  service created the role under in a live single-account setup (role path
  `/aws-service-role/cloudwatch.amazonaws.com/`, trust `cloudwatch.amazonaws.com`).
  Workshop Studio warns on undeclared SLR principals in `contentspec.yaml`, so it
  is not declared there.
- `OmniWriteNoResourceTypesInServiceReference` (`cloudwatch:CreateOneTimeDeepLinkCode`, `CreateAccessGrant`,
  `CreateOmniDashboard`, `Start/StopTelemetryQuery(Session)`): the service reference
  (2026-11) lists **no resource type** for any of them — only the `cloudwatch:HasAccessGrant`
  condition key — so `Resource: "*"` behind the Region fence is the ceiling; the domain and
  space ARNs in `OmniDomainSpaceReadUpdate` are the scoped part.
- `bedrock-agentcore:*OnlineEvaluationConfig*`, `Evaluate`, `*Evaluator*`,
  `*Dataset*`: the documented minimum for AgentCore Evaluations, scoped to the
  three resource types.

Transaction Search is deliberately NOT participant-mutable: the stack owns
`xray:UpdateTraceSegmentDestination` and the Logs resource policy, participants
only get the two reads. The self-service deploy policies carry the mutating
actions (`self-service-deploy-policy-8.json`, `XRayTransactionSearchConfigNoResourceLevel`
and `LogsResourcePolicyForTransactionSearchNoResourceLevel`, on `Resource: "*"` because
none of the four actions has a resource type, behind the `aws:RequestedRegion` fence; the reads are
`TransactionSearchReadNoResourceLevel`) and the role creation
for the three new roles (`-4.json`).

Validated by the Workshop Studio build (Access Analyzer, 2026-11-07): every action
in this file is a real IAM action. `cloudwatch:HasAccessGrant` was listed at first
and rejected as INVALID_ACTION: it is a condition key on the Omni operator policy,
not an action, so it was removed.

Live-tested 2026-11-07 in three Workshop Studio test events under the participant
IDE role: the SCP admits the `cloudwatch` Omni, `observabilityadmin` and
`bedrock-agentcore` evaluation actions, and every Module 5 page block and notebook
passes (`research/module5-live-report.md` in the author's notes). The denials
seen along the way were identity-policy gaps, now fixed: the telemetry-query set,
`CreateOneTimeDeepLinkCode`, `CreateAccessGrant`, `CreateOmniDashboard` and
`GetContextGraph` are resource-less and were denied "on resource: *" when scoped;
`StartTelemetryQuery` additionally needs `cloudwatch:GetRecords` (denied against
the `dataset/default` ARN).
