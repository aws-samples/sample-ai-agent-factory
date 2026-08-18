---
title: "Cleanup"
weight: 79
---

::alert[If you are using an AWS-provided event account, the account will be cleaned up automatically after the event ends. You can skip this step.]{type="info"}

## Destroy the FAST CDK stack

This removes the AgentCore Runtime, Memory, Gateway, Cognito User Pool, Amplify app, and all associated resources:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent/infra-cdk
npx cdk destroy --force
:::

::alert[This deletes all data including the Amplify frontend and the AgentCore Memory contents. This action cannot be undone.]{type="error"}

::alert[It does **not** delete the container image `cdk deploy` pushed. That image lives in the shared CDK **bootstrap** ECR repository, which is not part of this stack — see [Optional: remove the CDK bootstrap resources](#optional-remove-the-cdk-bootstrap-resources) below.]{type="info"}

## Clean up SSM parameters

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

# Report the three outcomes apart, rather than sending every failure to /dev/null.
# A parameter that survives because the delete was DENIED matters: the comment
# below is the bug it causes, and `|| true` made that failure look like "there was
# nothing to delete".
delete_param() {
  if aws ssm delete-parameter --name "$1" --region $REGION > /tmp/ssm-delete.log 2>&1; then
    echo "Deleted $1"
  elif grep -q "ParameterNotFound" /tmp/ssm-delete.log; then
    echo "Not present: $1"
  else
    echo "ERROR: $1 was NOT deleted:" >&2
    cat /tmp/ssm-delete.log >&2
  fi
}

for param in gateway_url llm_gateway_url llm_gateway_key gateway_credential_provider; do
  delete_param "/FAST-stack/${param}"
done

# Also remove the shared parameters written by Module 2 Step 3 / Connect LLM Gateway.
# If these survive, a later run in the same account reuses a gateway URL whose API
# Gateway no longer exists and every call fails with "Name or service not known".
for param in llm-gateway-url llm-gateway-key; do
  delete_param "/workshop/${param}"
done
:::

## Delete the OAuth2 credential provider

:::code{showCopyAction=true showLineNumbers=false language=bash}
# Re-derived here, not inherited from the block above. Run this block on its own —
# or in a new terminal — and $REGION is empty, `--region ""` is rejected, and the
# old `|| echo "Not found (already deleted)"` reported that as a success while the
# credential provider was still there.
REGION=$(aws configure get region)

if aws bedrock-agentcore-control delete-oauth2-credential-provider \
    --name "workshop-tools-gateway-auth" \
    --region $REGION > /tmp/oauth-delete.log 2>&1; then
  echo "Deleted OAuth2 credential provider: workshop-tools-gateway-auth"
elif grep -q "ResourceNotFoundException" /tmp/oauth-delete.log; then
  echo "Not found (already deleted): workshop-tools-gateway-auth"
else
  # Any other failure is a real one — an expired session, a missing permission, a
  # bad region. Show it rather than calling it "already deleted".
  echo "ERROR: workshop-tools-gateway-auth was NOT deleted:" >&2
  cat /tmp/oauth-delete.log >&2
fi
:::

## Optional: remove the CDK bootstrap resources

The `npx cdk bootstrap` you ran in the [deploy step](../deploy/) created three things that `cdk destroy` does **not** remove, because they are not part of the FAST stack:

| Resource | What it holds |
|---|---|
| `CDKToolkit` CloudFormation stack | The publishing roles and the two asset stores below |
| `cdk-hnb659fds-container-assets-<account>-<region>` ECR repository | The agent container image `cdk deploy` pushed (~150 MB per deploy) |
| `cdk-hnb659fds-assets-<account>-<region>` S3 bucket | Template and Lambda asset archives (a few MB) |

The [Workshop Cleanup](../../cleanup/) verify step lists only `workshop-*`, `code-editor` and `FAST-stack`, so it reports a clean account while all three are still there. The storage is cents per month rather than a surprise bill — treat this as tidiness, not urgency.

::alert[Bootstrap resources are shared by **every** CDK application in this account and region, not just this workshop. Skip this section if you use the CDK for anything else here: removing them makes the next `cdk deploy` of any app fail until it is bootstrapped again.]{type="warning"}

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REPO="cdk-hnb659fds-container-assets-${ACCOUNT_ID}-${REGION}"
BUCKET="cdk-hnb659fds-assets-${ACCOUNT_ID}-${REGION}"

# 1. The ECR repository first, and with --force. The bootstrap template sets no
#    EmptyOnDelete, and CloudFormation refuses to delete a repository that still
#    contains images -- so deleting the stack while the agent image is in there
#    leaves CDKToolkit in DELETE_FAILED instead.
if aws ecr delete-repository --repository-name "$REPO" --force \
    --region $REGION > /tmp/ecr-delete.log 2>&1; then
  echo "Deleted ECR repository: $REPO"
elif grep -q "RepositoryNotFoundException" /tmp/ecr-delete.log; then
  echo "Not present: $REPO"
else
  echo "ERROR: $REPO was NOT deleted:" >&2
  cat /tmp/ecr-delete.log >&2
fi

# 2. Now the stack. Check it exists first: `delete-stack` on a name that is
#    already gone succeeds, and so does the `wait`, so an unconditional message
#    here reports "Deleted" on a re-run when there was nothing to delete.
if ! aws cloudformation describe-stacks --stack-name CDKToolkit \
     --region $REGION >/dev/null 2>&1; then
  echo "Not present: CDKToolkit"
else
  aws cloudformation delete-stack --stack-name CDKToolkit --region $REGION
  if aws cloudformation wait stack-delete-complete \
      --stack-name CDKToolkit --region $REGION 2>/dev/null; then
    echo "Deleted stack: CDKToolkit"
  else
    echo "ERROR: CDKToolkit did not reach DELETE_COMPLETE:" >&2
    aws cloudformation describe-stack-events --stack-name CDKToolkit --region $REGION \
      --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
      --output text >&2
  fi
fi

# 3. The staging bucket outlives the stack (the template marks it
#    DeletionPolicy: Retain) and has versioning enabled, so `s3 rb --force`
#    leaves the non-current versions and delete markers and the bucket delete
#    then fails with BucketNotEmpty. Purge both explicitly, a page at a time.
# `head-bucket` prints the bucket's metadata on success, so send stdout to
# /dev/null as well -- otherwise a JSON blob lands in the middle of this block's
# output and hides the result lines below it.
if aws s3api head-bucket --bucket "$BUCKET" --region $REGION >/dev/null 2>&1; then
  for PASS in Versions DeleteMarkers; do
    while :; do
      N=$(aws s3api list-object-versions --bucket "$BUCKET" --region $REGION \
        --query "length(${PASS} || \`[]\`)" --output text)
      [ "$N" = "0" ] && break
      aws s3api delete-objects --bucket "$BUCKET" --region $REGION \
        --delete "$(aws s3api list-object-versions --bucket "$BUCKET" \
          --region $REGION --max-items 1000 \
          --query "{Objects: ${PASS}[].{Key:Key,VersionId:VersionId}}" \
          --output json)" > /dev/null || break
    done
  done
  if aws s3 rb "s3://$BUCKET" --region $REGION > /tmp/rb.log 2>&1; then
    echo "Deleted staging bucket: $BUCKET"
  else
    echo "ERROR: staging bucket $BUCKET was NOT deleted:" >&2
    cat /tmp/rb.log >&2
  fi
else
  echo "Not present: $BUCKET"
fi
:::

## Full workshop cleanup

For cleanup of platform resources (LLM Gateway, Registry, Tools Gateway stacks), follow the [Workshop Cleanup](../../cleanup/) page.

## Notebook walkthrough (optional alternative)

::alert[**Do not run `08-cleanup.ipynb` until you have finished Module 4.** It removes the FAST stack (Amplify, Runtime, Memory, Gateway, Cognito), the `/FAST-stack/*` SSM parameters, and the `workshop-tools-gateway-auth` OAuth2 credential provider. Platform foundations (Modules 2 and 3) remain untouched — use the global [Workshop Cleanup](../../cleanup/) page when you finish the whole workshop.]{type="warning"}

> This notebook covers the same cleanup as the CLI sections above — follow *either* path, you do not need to do both. The one thing it does not do is remove the CDK bootstrap resources; that section is optional, and if you want it, run its block from a terminal after the notebook finishes.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4b-fast/notebooks/` and open **`08-cleanup.ipynb`**.
