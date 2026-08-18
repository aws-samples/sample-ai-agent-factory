# ParticipantRole IAM policy — size budget and conventions

> Two unrelated policy sets live in this directory. This file is about the
> **ParticipantRole** files (`workshop-iam-policy-*.json`), which govern what a
> participant may do *inside* a Workshop-Studio-provisioned account. The
> **self-service deploy** files (`self-service-deploy-policy-*.json`) govern
> what the *deploying* principal may do on the self-paced path and are
> documented in the last section below.

The ParticipantRole permissions are split across five files referenced from
`contentspec.yaml` (`awsAccountConfig.participantRole.iamPolicies`):

| File | Scope |
|------|-------|
| `workshop-iam-policy-core.json`    | Bedrock, AgentCore (gateway/identity/policy/memory reads), Lambda, S3, Secrets Manager, EventBridge, SSM, STS |
| `workshop-iam-policy-core-2.json`  | IAM (roles/policies/PassRole), CloudFormation, CloudWatch metrics/dashboards/alarms, CloudWatch Logs, KMS, AgentCore manage/delete |
| `workshop-iam-policy-core-3.json`  | Agent Registry (`agent-registry:*`, used by Module 3b), CDK bootstrap, AWS Marketplace subscribe, AWS Amplify, and the single-function `lambda:UpdateFunctionCode` grant |
| `workshop-iam-policy-infra.json`   | Cognito, DynamoDB, API Gateway, X-Ray, ECR, ECS, CloudFront |
| `workshop-iam-policy-network.json` | VPC / EC2 networking, EFS, ELB |

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

## These five files are consumed TWICE — edit the JSON, never the copy

1. `contentspec.yaml` attaches them to **WSParticipantRole**.
2. `code-editor.yaml` embeds them as
   `CodeEditorParticipantPolicy{Network,Infra,Core,Core2,Core3}` and attaches them
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

| File | Chars | Headroom |
|------|-------|----------|
| `workshop-iam-policy-core.json`     | 5,985 | 159 |
| `workshop-iam-policy-core-2.json`   | 5,721 | 423 |
| `workshop-iam-policy-core-3.json`   | 3,445 | 2,699 |
| `workshop-iam-policy-infra.json`    | 5,302 | 842 |
| `workshop-iam-policy-network.json`  | 6,028 | 116 |

`-network.json` (116 free) and `-core.json` (159 free) are the two to watch.
`-core.json` is tight because its `STSAssume` statement now enumerates the four CDK
bootstrap roles instead of wildcarding all five — see "Assuming CDK bootstrap roles"
below. Anything new belongs in `-core-3.json` (2,699 free); that is exactly why the
bucket-policy writes moved there.

`scripts/verify-ide-policy-parity.py` re-checks all five sizes on every run and
keeps this quota from being exceeded silently, so you do not have to remember to.

An IAM user or role accepts **10 managed policies** by default. Five here plus
`IAMReadOnlyAccess` and the Workshop Studio `ws-default-policy` is seven, so there
are three slots left before a split stops being free.

Check every file before committing a change:

```bash
for f in static/cfn/workshop-iam-policy-*.json; do
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); n=len(json.dumps(d,separators=(',',':'))); print(f'{sys.argv[1]}: {n} {\"OK\" if n<=6144 else \"OVER LIMIT\"}')" "$f"
done
```

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
  `"*"`, on the correct but irrelevant grounds that neither action accepts a
  typed ARN. The statement is gone, because the ParticipantRole never creates a
  distribution: the only two CloudFront distributions in the workshop are
  declared in `code-editor.yaml` and `registry/compute-stack.yaml`, and both
  stacks are deployed by Workshop Studio (event path) or by the deploying
  principal running `scripts/self-service-deploy.sh` from a **local** terminal
  under `self-service-deploy-policy-4.json` (self-service path) — never from the
  IDE. Every participant-facing CloudFront reference is a read: a
  `list-exports` lookup of `workshop-MainCloudFrontUrl`. Cleanup still works,
  because tearing a distribution down needs `UpdateDistribution` +
  `DeleteDistribution`, which stay in `CloudFrontManageDistribution` scoped to
  `distribution/*`. This was the last remaining Checkov `CKV_AWS_111` failure in
  `code-editor.yaml`; note that the check runs through **cloudsplaining**, which
  ignores `Condition` blocks entirely, so an `aws:RequestedRegion` fence will
  never clear it — only a typed ARN or removing the action will.
- Write actions are split from read actions into separate Sids so a reviewer can
  see at a glance which statements can mutate state. For AgentCore this split is
  load-bearing: `Create*` actions cannot be scoped (the service appends a random
  suffix, so no ARN exists at authorization time) while every corresponding
  manage action can, which is why `ACGatewayCreate` and `ACGatewayManage` are
  separate statements rather than one `ACGatewayWrite`.
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
  ~54 of these across the nine policy files. CloudFormation tags a resource as
  part of creating it, before any ARN exists to scope to, and a `Tag*`/`Untag*`
  action cannot escalate privilege on its own. There is no Checkov check for this
  class, and it was not among the findings the Workshop Studio build reported.
  Unconstrained **write** and **permissions-management** actions are a different
  matter and are held at **zero** across all nine files.

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
the `IAMRole` Sid of `-core-2.json`.

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

## `lambda:UpdateFunctionCode` is scoped to one function name, not a prefix

The `Lambda` statement in `workshop-iam-policy-core.json` used to grant
`lambda:UpdateFunctionCode` alongside `lambda:InvokeFunction` over
`function:workshop-*`, `function:ac-*` and `function:agentcore-*`. Those two actions
together on platform-owned functions are a code-injection escalation path: a
participant could overwrite the body of any workshop Lambda — including ones the
templates run with roles more privileged than ParticipantRole — and then invoke it.

The grant exists for exactly one call. Repo-wide, the only consumer is
`source/module-3b-agentcore/notebooks/04-register-tools.ipynb`:

```python
lam.update_function_code(FunctionName="ac-auto-review", ZipFile=buf.read())
```

No content page and no other notebook or script updates Lambda code, and no
participant step creates or updates a CloudFormation stack (all five stacks are
pre-deployed by Workshop Studio, and Module 4b's `cdk deploy` authorizes as the
`cdk-hnb659fds-cfn-exec-role-*` bootstrap role, not as ParticipantRole). So the
action is now a standalone `LambdaUpdateCodeAutoReview` statement in
`workshop-iam-policy-core-3.json`, on the single literal ARN
`arn:aws:lambda:*:*:function:ac-auto-review` — no wildcard at all. It lives in
`-core-3.json` rather than next to the `Lambda` statement it came from only because
`-core.json` is within 200 bytes of the 6144-byte cap.

**The self-service files deliberately keep the broader grant.**
`LambdaForWorkshopFunctions` in `self-service-deploy-policy-5.json` still holds
`UpdateFunctionCode` over the `workshop-*` / `ac-*` / `agentcore-*` /
`code-editor-*` prefixes, because that principal *deploys* the templates:
CloudFormation runs under the caller's credentials, so a stack update that changes
any inline `Code.ZipFile` needs `UpdateFunctionCode` on the function it is
replacing. Narrowing it there would break `./deploy-cfn.sh deploy` on any re-deploy.
The escalation argument also does not carry over — that principal already holds
`lambda:CreateFunction` plus `iam:PassRole`, so it can author a privileged function
outright; removing one action buys nothing.

## `aws-marketplace:Subscribe` is not scoped to product IDs, on purpose

`aws-marketplace:Subscribe` and `ViewSubscriptions` used to sit inside the
Bedrock-named `BedrockCatalogNoResourceLevel` statement, which made the Sid a lie
and left the two actions with no region condition. They now live in their own
`MarketplaceSubscribeNoResourceLevel` statement in
`workshop-iam-policy-core-3.json`, with the same three-region
`aws:RequestedRegion` guard as the rest of the file.

They stay on `"Resource": "*"` with no `aws-marketplace:ProductId` condition. The
condition key exists, but the product ids behind Bedrock model access are opaque and
change as models are added or retired; pinning them would silently break the "Prime
Anthropic model access" step in Module 4 the next time a model id moves. Accepted as
a documented exception rather than a brittle allowlist.

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
2. **`TagResource`, `UntagResource` and `CreateInvalidation` stay unconditioned.**
   Conditioning `TagResource` on the tag it is being used to apply is
   self-blocking — a stack update that adds the tag to an existing untagged
   distribution would be denied the call that fixes it.

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
the name `BedrockModelDiscoveryNoResourceLevel` — `PutUseCaseForModelAccess`,
`PutFoundationModelEntitlement` and the List/Get actions genuinely have no
resource-level support.

## Assuming CDK bootstrap roles: four of five, never `cfn-exec`

`cdk bootstrap` creates five roles. Four are assumed by the CDK CLI during a deploy;
the fifth, `cdk-hnb659fds-cfn-exec-role-*`, is the one CloudFormation itself runs as,
and it is created with `AdministratorAccess`. Wildcarding `role/cdk-hnb659fds-*` in
`STSAssume` therefore handed the participant a one-call path to admin.

`workshop-iam-policy-core.json` [`STSAssume`] now enumerates:

    arn:aws:iam::*:role/cdk-hnb659fds-deploy-role-*
    arn:aws:iam::*:role/cdk-hnb659fds-*-publishing-role-*   (file + image)
    arn:aws:iam::*:role/cdk-hnb659fds-lookup-role-*

Residual, and inherent to default CDK bootstrap: `deploy-role` can create a stack
that passes `cfn-exec-role` to CloudFormation, so admin is still reachable by a
longer route. Closing that needs a custom bootstrap
(`--cloudformation-execution-policies`), which would change the Module 4 deploy flow
for every participant. Not worth it inside a disposable workshop account; recorded
here so nobody assumes the narrowing is airtight.

## `lambda:AddPermission` is not needed by the participant; `RemovePermission` is

Only two templates contain `AWS::Lambda::Permission` —
`agentcore/workshop-agentcore-stack.yaml` and
`tools-gateway/workshop-tools-gateway-stack.yaml` — and both are pre-deployed by
Workshop Studio (`contentspec.yaml`) or by `deploy-cfn.sh` under
`self-service-deploy-policy-5.json`, never by the participant. No content page or
notebook calls `add-permission`. So `lambda:AddPermission` came out of
`workshop-iam-policy-core.json` [`Lambda`]: it is the action that can hand an
external principal the right to invoke a workshop function, and nothing needs it.

`lambda:RemovePermission` stays. The Cleanup module has the participant run
`aws cloudformation delete-stack` on both of those stacks, and deleting an
`AWS::Lambda::Permission` calls it.

Same reasoning moved `s3:PutBucketPolicy` / `s3:DeleteBucketPolicy` out of
`[S3Access]` (which spanned `workshop-*`, `fast-*` and all of `cdk-*`) into
`workshop-iam-policy-core-3.json`
[`S3BucketPolicyOnWorkshopAndCdkStagingBucketsOnly`], scoped to `workshop-*` and
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
moved to `-policy-7.json`, which had room. All seven documents attach to the same
role, so which file holds a statement changes nothing at evaluation time — the Sid
carries the meaning, not the filename.

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
> **But the Workshop Studio build gates on Access Analyzer, and it treats
> `INVALID_ACTION` as an `error`, not a `warn` — the build fails outright.** So
> "expected, ignore it" is not a workable disposition for a WS-delivered policy.
> `ACTokenVaultManage` therefore grants `bedrock-agentcore:Create*` instead of the
> literal action name. Measured against `accessanalyzer validate-policy`:
>
> | Action | Result |
> |---|---|
> | `bedrock-agentcore:CreateTokenVault` | `ERROR INVALID_ACTION` |
> | `bedrock-agentcore:CreateToken*` | `ERROR INVALID_ACTION` — no catalogued action matches |
> | `bedrock-agentcore:Create*` | clean |
> | `bedrock-agentcore:*` | clean, but far wider than needed |
>
> `Create*` is scoped to `token-vault/default` and the `workshop-*` provider path,
> so it confers nothing beyond `CreateTokenVault` plus
> `CreateOauth2CredentialProvider` — and the latter is already allowed on `*` in
> `workshop-iam-policy-core.json [ACIdentityScoped]`. A prefix wildcard is the only
> way to express a real-but-uncatalogued action without failing the build; revert to
> the literal name once the catalogue catches up.
>
> Do **not** enumerate `Create*` back into literal action names here. Replacing it
> with `CreateOauth2CredentialProvider` alone silently drops `CreateTokenVault` and
> the deploy fails with
> `not authorized to perform: bedrock-agentcore:CreateTokenVault on resource:
> arn:aws:bedrock-agentcore:<region>:<acct>:token-vault/default`.
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

## The self-service deploy policies (`self-service-deploy-policy-*.json`)

These seven files are the least-privilege alternative to `AdministratorAccess`
for the principal that runs `./deploy-cfn.sh deploy` on the self-paced path.
They are linked from `content/introduction/getting-started/self-service.en.md`,
which also carries the create-or-update loop participants run. Nothing attaches
them automatically — adding an eighth file means updating that page's list, its
`for n in 1 2 3 4 5 6 7` loop, and the hint in `scripts/self-service-deploy.sh`.
Seven still fits the default 10-managed-policy-per-principal limit; past that,
attach them to a dedicated deploy role instead.

| File | Scope |
|------|-------|
| `self-service-deploy-policy-1.json` | CloudFormation, EC2/VPC networking, EC2 IDE instances |
| `self-service-deploy-policy-2.json` | DocumentDB/RDS, KMS, Secrets Manager, SSM parameters + documents, EventBridge, SNS |
| `self-service-deploy-policy-3.json` | Cloud Map (`servicediscovery`), Cognito, API Gateway v2 + `execute-api`, ELB, Agent Registry |
| `self-service-deploy-policy-4.json` | IAM, CloudFront, Route 53, STS, Marketplace, S3 |
| `self-service-deploy-policy-5.json` | Lambda, EFS, Prometheus (APS), Service Quotas |
| `self-service-deploy-policy-6.json` | ECS + Application Auto Scaling, ECR, CloudWatch, CloudWatch Logs |
| `self-service-deploy-policy-7.json` | Bedrock + Bedrock Guardrails, AgentCore (gateway, identity, policy engine, tagging), EC2 describe-only |

No self-service policy grants `acm:` or `grafana:`, and none needs to:
`compute-stack.yaml`'s `AWS::CertificateManager::Certificate` is behind
`HasDnsConfig` and `observability-stack.yaml`'s Grafana resources are behind
`DeployGrafana` / `DeployGrafanaOSS` — all three conditions are false for a
default deploy (see the ParticipantRole table above, row 7).

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
`Ec2DeleteGuardDutyManagedNetworkResources` is deliberately *not* tag-scoped:
GuardDuty creates its own VPC endpoint and security group inside the workshop
VPC, they carry no Workshop tag, and they block VPC deletion until removed.

The split is purely a size budget — the 6,144-char managed-policy quota applies
per file exactly as it does for the ParticipantRole set, so use the same
`for f in static/cfn/self-service-deploy-policy-*.json` size check before
committing. Current minified sizes (headroom in parentheses): `-1` 5,336 (808),
`-2` 5,904 (240), `-3` 5,384 (760), `-4` 5,518 (626), `-5` 5,592 (552),
`-6` 5,396 (748), `-7` 5,314 (830). `-2` has the least room. Keep one statement
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

> **`apigateway:TagResource` is real and `policy-3` must keep it, even though
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
> Note the two `INVALID_ACTION` errors this leaves on `policy-3` are **safe here
> and would not be elsewhere**: the Workshop Studio build fails on
> `INVALID_ACTION`, but it only validates the five files listed under
> `iamPolicies` in `contentspec.yaml`. The self-service documents are not among
> them. Do **not** add these actions to `workshop-iam-policy-infra.json` — it
> would break the build, and participants never deploy an `AWS::ApiGatewayV2::*`
> resource, so its `APIGatewayAccess` twin genuinely does not need them.

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
  survives with no `AWS::Route53::*` resource, because CloudMap's `PrivateDnsNamespace`
  creates the hosted zone implicitly. Grep the whole repo — content, notebooks, scripts —
  for the service before deleting anything. ACM and Grafana were removed only after that
  grep came back empty.
- **Nothing here covers the custom resources' own actions.** CloudFormation invokes a
  Lambda-backed custom resource with the stack operation's credentials, so the deploy role
  needs only `lambda:InvokeFunction`. What the custom resource then does runs under its own
  execution role — a different principal, and a separate audit.

## Residual IAM escalation — accepted, and why a partial fix is worse

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
   - **It cost 370 of 443 bytes of headroom** on the tightest file in the set,
     leaving 66. See the size table above.
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
   `sts:*`, `organizations:*`) and attaching it to all **40** `AWS::IAM::Role`
   resources declared across the 10 templates in `static/cfn/` — plus the Module 4
   CDK app — and keeping it in sync as templates change. That analysis is only
   tractable because **no role is created outside CloudFormation**: `create_role(`
   has zero hits across `source/` and `content/`.

So the residual escalation is **accepted and documented** rather than
half-mitigated. The containment that actually holds is the one the content already
insists on: a dedicated, disposable account per participant, `deployableRegions`,
and the Workshop Studio SCP. `self-service.en.md` states this in a warning alert so
a self-paced reader does not mistake "scoped" for "sandboxed".

The negative test suite encodes this deliberately: `iam:PutRolePolicy` and
`iam:AttachRolePolicy` on a `workshop-*` role assert **allow**, labelled
`accepted+documented`, so nobody later "fixes" them into denials and breaks the
deploy while believing they closed the hole.

### `sts:AssumeRole` — a tightening that could not be attached

Both policy sets grant `sts:AssumeRole` on `arn:aws:iam::*:role/workshop-*`, with
a `*` **account** field. An earlier pass treated that wildcard as a defect and
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
in an embedded one. So the reason previously recorded for the IDE copy keeping the
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
these seven policies — create a role with them attached, assume it, and run
`./deploy-cfn.sh deploy` end to end.
