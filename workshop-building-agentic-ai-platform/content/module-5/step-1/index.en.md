---
title: "Verify your domain and space"
weight: 81
---

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach, both achieve the same result.]{type="info"}

Your platform team — Workshop Studio, when it provisioned this account — created the CloudWatch Omni domain, the space for the workshop Region, the Dataset integration that forwards the account's telemetry into it, and the access grants that let both of your identities in. This step confirms each piece is in place and explains what it is for; the same commands double as the self-paced path for an account that deployed the stacks by hand.

## CLI walkthrough

### Step 1: Confirm Transaction Search is active

Every AgentCore span, from the Runtime, the Gateway and Memory, reaches CloudWatch only when the account's trace destination is CloudWatch Logs. Workshop Studio enabled this when it deployed `workshop-agentcore-stack`, because the change takes about 10 minutes to apply. Check it rather than trusting it:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws xray get-trace-segment-destination --region $REGION
:::

You can expect the following output:

:::code{showCopyAction=false showLineNumbers=false language=text}
{
    "Destination": "CloudWatchLogs",
    "Status": "ACTIVE"
}
:::

If `Destination` is `XRay`, the stack did not finish or you are in a self-paced account that skipped the AgentCore stack. Deploy `static/cfn/agentcore/workshop-agentcore-stack.yaml` first; participants do not have the permission to flip this setting by hand, by design.

### Step 2: Read the pre-created role ARNs

The AgentCore stack created the three roles Omni and AgentCore Evaluations assume. Read their ARNs from the stack outputs so you never paste an ARN:

:::code{showCopyAction=true showLineNumbers=false language=bash}
stack_output() {
  aws cloudformation describe-stacks --stack-name workshop-agentcore-stack --region $REGION \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
SPACE_ROLE_ARN=$(stack_output OmniSpaceAccessRoleArn)
DATASET_ROLE_ARN=$(stack_output OmniDatasetIntegrationRoleArn)
EVAL_ROLE_ARN=$(stack_output AgentCoreEvaluationRoleArn)

echo "Space operator role:      $SPACE_ROLE_ARN"
echo "Dataset integration role: $DATASET_ROLE_ARN"
echo "Evaluation role:          $EVAL_ROLE_ARN"
:::

All three lines should print an ARN ending in `-workshop`. An empty line means the stack is older than this module; confirm `workshop-agentcore-stack` is `UPDATE_COMPLETE` or `CREATE_COMPLETE` with the two `Omni*` outputs and `AgentCoreEvaluationRoleArn`.

### Step 3: Verify the domain, the space and your grants

Workshop Studio provisioned the Omni foundation for you. `workshop-agentcore-stack`, the same stack that created the three Omni roles in Step 2 and switched Transaction Search on in Step 1, created the account's **domain** (the sign-in boundary; its name becomes the sign-in host `https://ws-platform-<account-id>.cloudwatch-omni.global.app.aws`), the **space** for the workshop Region (where telemetry, dashboards and evaluations live, running as the space operator role) and the **Dataset integration** that forwards every log group and `aws/spans` into the Dataset. Access is the part that is easy to miss. Omni decides who may enter a space with **access grants**, never with IAM policies, and you work with two identities: the browser you are reading this in is signed in to the AWS console as `WSParticipantRole`, which `workshop-agentcore-stack` granted Space Admin, and this terminal and the notebooks run as the Code Editor's instance role, which the `code-editor` stack granted. Omni adds two grants of its own on top; the table below lists all four.

Read the stack's outputs and confirm every piece is `ACTIVE`:

:::code{showCopyAction=true showLineNumbers=false language=bash}
DOMAIN_ID=$(stack_output OmniDomainId)
SPACE_ID=$(stack_output OmniSpaceId)
echo "Sign-in URL: $(stack_output OmniSignInUrl)"

aws cloudwatchomni get-domain --domain-id $DOMAIN_ID --region $REGION \
  --query 'domain.{name:name,status:status}' --output table
aws cloudwatchomni get-space --space-id $SPACE_ID --region $REGION \
  --query 'space.{name:name,region:region,status:status,evaluationRole:agentCoreEvaluationRoleArn}' --output table
aws cloudwatchomni list-access-grants --domain-id $DOMAIN_ID --space-id $SPACE_ID --region $REGION \
  --query 'items[].{name:name,permission:permission,type:grantType,principal:principal.principalId}' --output table
aws cloudwatch get-dataset --dataset-identifier default --region $REGION
:::

**What you should see**

- `get-domain` and `get-space` both report `status` `ACTIVE`.
- `list-access-grants` lists four rows: two the stacks created and two Omni added itself.
- `get-dataset` prints the Dataset ID `default` and its ARN.

| Grant | Permission | Principal | Created by |
|-------|------------|-----------|------------|
| `participant-space-admin` | `SPACE_ADMIN`, customer managed | `WSParticipantRole`, the identity your browser uses in the console | `workshop-agentcore-stack` |
| `code-editor-space-admin` | `SPACE_ADMIN`, customer managed | The Code Editor instance role (`code-editor-CodeEditorInstanceBootstrapRole-...`), which runs this terminal, the notebooks and the deep link | The `code-editor` stack |
| `OmniDefaults-Space-Admin` | `SPACE_ADMIN`, service managed | `WSSystemRole`, the role Workshop Studio ran CloudFormation as, so the identity that created the space | Omni, automatically for whoever creates a space |
| `OmniDefaults-AsyncAccessProfile-AgentCanAssume` | `CUSTOM`, service managed | The space's own agent profile (`agent-<space id>`) | Omni |

In your own account expect three rows, not four: the identity that first ran `./scripts/self-service-deploy.sh` created the space and holds `OmniDefaults-Space-Admin` (a different identity that re-runs the deploy later does not inherit it and admits itself with the block below), there is no `WSSystemRole` row, and there is no `participant-space-admin` row unless you set `OMNI_CONSOLE_PRINCIPAL_ARN` to another identity at deploy time or add one with the block below; the `code-editor-space-admin` row is the same.

**Good to know.** On first enablement Omni backfills up to seven days of existing CloudWatch logs and traces into the space (telemetry older than seven days, and data encrypted with a customer managed KMS key, is not included), so the LLM Gateway and interceptor logs you produced in Modules 2 and 3 appear without re-running anything.

::alert[**If `DOMAIN_ID` and `SPACE_ID` are empty.** Your `workshop-agentcore-stack` predates the Omni resources (no `OmniDomainId` output): you are in a self-paced account that deployed an older template. Re-run `./scripts/self-service-deploy.sh` from your clone. It redeploys all five workshop stacks (allow 30-45 minutes) and creates the domain, the space and the Dataset integration; the identity you run it as creates the space and becomes its Space Admin (the script prints `CloudWatch Omni: ... creates the space and becomes its Space Admin`). Then run the block above again. The participant policy deliberately has no `CreateDomain`, `CreateSpace` or `CreateDatasetIntegration`: these are platform-team resources that CloudFormation owns.]{type="warning"}

**Admit another console identity.** Grants are the one Omni resource you manage from here: `cloudwatch:CreateAccessGrant` is in the participant policy and in the IDE policy the instance role uses. In a Workshop Studio event nothing is missing: `WSParticipantRole` already holds `participant-space-admin`, so you can skip this block.

**Deploying into your own account?** The identity that first ran the deploy created the space and holds Omni's own `OmniDefaults-Space-Admin`; a later deployer does not inherit it and is admitted with `OMNI_CONSOLE_PRINCIPAL_ARN` at deploy time or with this block. To admit a different role or user that your browser uses in the console, run `aws sts get-caller-identity` in CloudShell signed in as that identity, not in this IDE terminal: the terminal runs as the instance role, which already holds `code-editor-space-admin`, so with its role ARN the block only reports the grant it already holds, and with the raw `assumed-role` ARN it matches no existing grant; either way your browser identity is not admitted. For an assumed role use the role ARN, `arn:aws:iam::<account-id>:role/<name>`. Edit the first two assignments in the block below to your browser identity's full IAM ARN and principal type (`IAM_USER` for a user), then run it. Omni rejects a grant for a principal that does not exist, and a second Space Admin grant for one that already holds it; the block checks for an existing grant first, so it reports an existing grant or admits the chosen principal when none exists, and re-running it is safe:

:::code{showCopyAction=true showLineNumbers=false language=bash}
CONSOLE_PRINCIPAL_ARN="arn:aws:iam::${ACCOUNT_ID}:role/WSParticipantRole"   # or your own role / user ARN
CONSOLE_PRINCIPAL_TYPE=IAM_ROLE                                               # IAM_USER for a user ARN

EXISTING=$(aws cloudwatchomni list-access-grants --domain-id $DOMAIN_ID --space-id $SPACE_ID \
  --region $REGION --query "items[?principal.principalId=='${CONSOLE_PRINCIPAL_ARN}'].grantId" --output text)

if [ -z "$EXISTING" ] || [ "$EXISTING" = "None" ]; then
  aws cloudwatchomni create-access-grant \
    --domain-id $DOMAIN_ID --space-id $SPACE_ID \
    --name participant-space-admin \
    --principal principalType=$CONSOLE_PRINCIPAL_TYPE,principalId=$CONSOLE_PRINCIPAL_ARN \
    --permission SPACE_ADMIN \
    --region $REGION --query 'accessGrant.{name:name,permission:permission,type:grantType}' --output table \
    || echo "If the error says the principal does not exist, set CONSOLE_PRINCIPAL_ARN to the role or user your browser uses in the console and re-run."
else
  echo "${CONSOLE_PRINCIPAL_ARN##*/} already holds grant $EXISTING"
fi

aws cloudwatchomni list-access-grants --domain-id $DOMAIN_ID --space-id $SPACE_ID --region $REGION \
  --query 'items[].{name:name,permission:permission,type:grantType,principal:principal.principalId}' --output table
:::

### Step 4: Save the identifiers for the next steps

Later pages re-derive the IDs with `list-domains` and `list-spaces`, so nothing depends on this shell staying open. For convenience in this terminal:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cat > ~/.workshop-omni.env <<EOF
export OMNI_DOMAIN_ID=$DOMAIN_ID
export OMNI_SPACE_ID=$SPACE_ID
export OMNI_EVAL_ROLE_ARN=$EVAL_ROLE_ARN
EOF
echo "Saved to ~/.workshop-omni.env (source it in a new terminal)"
:::

## What the platform team created (and you verified)

| Resource | Scope | Purpose |
|----------|-------|---------|
| Domain `ws-platform-<account-id>` | Account | Sign-in URL and identity boundary, IAM sign-in only |
| Space `workshop-space` | Account + Region | Where you query, dashboard and evaluate |
| Access grants `participant-space-admin` (from `workshop-agentcore-stack`) and `code-editor-space-admin` (from the `code-editor` stack) | Space | Admit both of your identities: `WSParticipantRole` (the console and its *Launch Omni* button) and the Code Editor instance role (this terminal, the notebooks and the deep link). Omni adds `OmniDefaults-Space-Admin` for the space creator and a custom grant for the space's agent profile itself |
| Dataset integration | Account + Region | CloudWatch Logs forwards every log group and `aws/spans` into the Dataset |

Nothing you already run changed: log groups, Logs Insights queries, alarms and the GenAI Observability dashboard keep working. Omni reads the same stores.

---

## Notebook walkthrough (optional alternative)

> This notebook (02-create-space.ipynb) covers the same material as the CLI section above. Follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected; switch it from the kernel name in the top-right.
>
> Navigate to `source/module-5-omni/notebooks/` and open **`02-create-space.ipynb`**.
