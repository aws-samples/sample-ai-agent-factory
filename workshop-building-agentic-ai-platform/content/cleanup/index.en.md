---
title: "Cleanup"
weight: 91
---

To avoid ongoing charges, delete the AWS resources created during this workshop.

::alert[If you are running this workshop at an AWS event, you can skip this section. Workshop Studio will automatically clean up your account resources when the event ends.]{type="info"}

::alert[Complete this section when you are finished with the workshop and running in your own AWS account. Skipping cleanup will result in ongoing charges.]{type="warning"}

## Cleanup order

Delete resources in reverse order of creation. The exact steps depend on which modules you completed.

### Step 1: Module 4 — FAST agent

If you completed Module 4, destroy the FAST CDK stack and clean up associated resources — this applies to both the MCP path and the AgentCore path. Follow the detailed cleanup instructions in the [Module 4 Cleanup](../module-4/cleanup/) page, which covers:

- OAuth2 Credential Provider deletion
- Cognito user deletion
- CDK stack destruction (`FAST-stack`)
- SSM parameter cleanup (`/FAST-stack/*`)
- LiteLLM virtual key deletion

### Step 2: Module 3a — Tools Gateway

If you completed Module 3a, delete the Bedrock Guardrail (if created) and the AgentCore Gateway API resource. The CloudFormation stack (`workshop-tools-gateway-stack`) is auto-provisioned and will be cleaned up automatically — that also removes the sync/request/response interceptor Lambdas, the EventBridge schedule, the `workshop-agentcore-gateway-role-<region>` IAM role, and the demo tool Lambdas. For self-service deployments, see the stack deletion block at the bottom of this page.

Delete the Module 3a guardrail by looking it up by name:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# Delete the Module 3a Bedrock Guardrail (if created)
TOOL_GUARDRAIL_ID=$(aws bedrock list-guardrails \
  --query "guardrails[?name=='workshop-tool-guardrail'].id | [0]" \
  --output text --region $REGION)

if [ -n "$TOOL_GUARDRAIL_ID" ] && [ "$TOOL_GUARDRAIL_ID" != "None" ]; then
  # Check the delete instead of echoing success after it: `delete-guardrail` also
  # prints its whole JSON response, which buries a failure in noise.
  if aws bedrock delete-guardrail \
      --guardrail-identifier $TOOL_GUARDRAIL_ID \
      --region $REGION > /tmp/gd-delete.log 2>&1; then
    echo "Deleted guardrail: workshop-tool-guardrail ($TOOL_GUARDRAIL_ID)"
  else
    echo "ERROR: guardrail workshop-tool-guardrail ($TOOL_GUARDRAIL_ID) was NOT deleted:" >&2
    cat /tmp/gd-delete.log >&2
  fi
else
  echo "No workshop-tool-guardrail found — nothing to do."
fi
:::

Delete the Tools Gateway AgentCore Gateway:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# Delete the AgentCore Gateway (Module 3a)
GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query 'items[?name==`tools-gateway`].gatewayId | [0]' --output text \
  --region $REGION)

if [ -n "$GATEWAY_ID" ] && [ "$GATEWAY_ID" != "None" ]; then
  # A Gateway cannot be deleted while it still has targets — DeleteGateway fails
  # with "has targets associated with it". Delete the targets first, then wait for
  # them to actually disappear: delete-gateway-target returns while the target is
  # still DELETING, so deleting the gateway right afterwards fails the same way.
  for TARGET_ID in $(aws bedrock-agentcore-control list-gateway-targets \
      --gateway-identifier $GATEWAY_ID --query 'items[].targetId' \
      --output text --region $REGION 2>/dev/null); do
    aws bedrock-agentcore-control delete-gateway-target \
      --gateway-identifier $GATEWAY_ID --target-id $TARGET_ID \
      --region $REGION --query 'targetId' --output text
  done

  for _ in $(seq 1 30); do
    LEFT=$(aws bedrock-agentcore-control list-gateway-targets \
      --gateway-identifier $GATEWAY_ID --query 'items[].targetId' \
      --output text --region $REGION 2>/dev/null)
    [ -z "$LEFT" ] && break
    sleep 5
  done

  # Report what actually happened. This used to echo "Deleted gateway" no matter
  # what, so a failed delete read as a success and the Gateway kept running.
  if aws bedrock-agentcore-control delete-gateway \
      --gateway-identifier $GATEWAY_ID --region $REGION >/dev/null 2>&1; then
    echo "Deleted gateway: $GATEWAY_ID"
    # create_gateway.py stored the ID here for cross-stack discovery; without this
    # the parameter is left pointing at a Gateway that no longer exists. It only
    # exists if you ran that script yourself -- at an AWS event the Gateway comes
    # from the pre-provisioned stack instead, so report both outcomes. Left as a
    # bare `&&`, a ParameterNotFound silently becomes this block's exit status.
    if aws ssm delete-parameter --name /agentcore-gateway/workshop/gateway-id \
        --region $REGION 2>/dev/null; then
      echo "Deleted SSM parameter: /agentcore-gateway/workshop/gateway-id"
    else
      echo "No /agentcore-gateway/workshop/gateway-id parameter to delete."
    fi
  else
    echo "ERROR: gateway $GATEWAY_ID was NOT deleted. Remaining targets:" >&2
    aws bedrock-agentcore-control list-gateway-targets \
      --gateway-identifier $GATEWAY_ID --query 'items[].[targetId,status]' \
      --output text --region $REGION >&2
  fi
fi
:::

### Step 3: Module 3b — AgentCore registry & gateway

If you completed Module 3b, clean up the AgentCore-specific resources:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# Delete the Module 3b Bedrock Guardrail (if created)
AC_GUARDRAIL_ID=$(aws bedrock list-guardrails \
  --query "guardrails[?name=='workshop-tool-output-guardrail'].id | [0]" \
  --output text --region $REGION)

if [ -n "$AC_GUARDRAIL_ID" ] && [ "$AC_GUARDRAIL_ID" != "None" ]; then
  if aws bedrock delete-guardrail \
      --guardrail-identifier $AC_GUARDRAIL_ID \
      --region $REGION > /tmp/gd-delete.log 2>&1; then
    echo "Deleted guardrail: workshop-tool-output-guardrail ($AC_GUARDRAIL_ID)"
  else
    echo "ERROR: guardrail workshop-tool-output-guardrail ($AC_GUARDRAIL_ID) was NOT deleted:" >&2
    cat /tmp/gd-delete.log >&2
  fi
else
  echo "No workshop-tool-output-guardrail found — nothing to do."
fi

# Detach the Gateway targets the Registry sync Lambda created (Module 3b).
#
# The `ac-tools-gateway` Gateway itself is NOT deleted here: it is the
# `AgentCoreGateway` resource of `workshop-agentcore-stack`, and so are its three
# built-in targets (`tg-workshop-flights-mcp`, `tg-workshop-hotels-mcp`,
# `tg-workshop-kb-search`). Deleting a CloudFormation-managed resource by hand
# leaves the stack drifted — CloudFormation still reports the Gateway as
# CREATE_COMPLETE while it no longer exists, and Module 3b notebook 02 then fails
# with "Gateway 'ac-tools-gateway' not found" with no way to recover short of
# deleting and recreating the whole stack. `delete-stack` below removes the
# Gateway and its three targets for you.
#
# What DOES need removing is any EXTRA target that the `ac-registry-gateway-sync`
# Lambda added for a tool you approved in the Registry. Those targets are not in
# the template, so CloudFormation does not know about them — and DeleteGateway
# refuses to delete a Gateway that still has any target attached, which would
# stall the stack teardown on "has targets associated with it".
AC_GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query 'items[?name==`ac-tools-gateway`].gatewayId | [0]' --output text \
  --region $REGION)

if [ -n "$AC_GATEWAY_ID" ] && [ "$AC_GATEWAY_ID" != "None" ]; then
  CFN_TARGETS="tg-workshop-flights-mcp tg-workshop-hotels-mcp tg-workshop-kb-search"

  aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier $AC_GATEWAY_ID \
    --query 'items[].[targetId,name]' --output text \
    --region $REGION 2>/dev/null | while read -r TARGET_ID TARGET_NAME; do
    case " $CFN_TARGETS " in
      *" $TARGET_NAME "*)
        echo "Keeping CloudFormation-managed target: $TARGET_NAME"
        continue
        ;;
    esac
    # Report a failed delete. A target left attached is the one thing this block
    # exists to prevent — DeleteGateway refuses while any target remains, so a
    # silent failure here stalls the stack teardown later with an error that
    # points at the Gateway rather than at this step.
    if aws bedrock-agentcore-control delete-gateway-target \
        --gateway-identifier $AC_GATEWAY_ID \
        --target-id $TARGET_ID --region $REGION \
        --query 'targetId' --output text > /tmp/tgt-delete.log 2>&1; then
      echo "Deleted sync-created target: $TARGET_NAME ($TARGET_ID)"
    else
      echo "ERROR: target $TARGET_NAME ($TARGET_ID) was NOT deleted:" >&2
      cat /tmp/tgt-delete.log >&2
    fi
  done

  echo "Left Gateway $AC_GATEWAY_ID in place — it is CloudFormation-managed."
fi
:::

The blocks below delete the remaining AgentCore resources created by Module 3b notebooks 03, 04, 06, and 07. If you completed Module 3b step-8, you have already run these and can skip ahead.

There are no EventBridge rules to delete by hand. Both of the Module 3b rules — `ac-registry-approved-sync` (the approved-tool sync rule) and `ac-registry-auto-review` — are CloudFormation-managed by `workshop-agentcore-stack` and are removed automatically when the workshop stacks are torn down. Deleting either one manually causes drift and makes subsequent stack updates fail.

Delete all registry records, then the registry itself:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# Every registry called workshop-registry, not just the first. The service does not
# enforce unique names, so an account that ran notebook 03 more than once has more
# than one, and `| [0]` deletes one and abandons the rest.
REGISTRY_IDS=$(aws agent-registry-control list-registries --no-paginate \
  --query "registries[?name=='workshop-registry'].registryId" \
  --output text --region $REGION 2>/dev/null)

if [ -z "$REGISTRY_IDS" ]; then
  echo "No workshop-registry found — nothing to do."
fi

for REGISTRY_ID in $REGISTRY_IDS; do
  for RECORD_ID in $(aws agent-registry-control list-registry-records \
      --registry-id $REGISTRY_ID \
      --query 'registryRecords[].recordId' --output text \
      --region $REGION 2>/dev/null); do
    if aws agent-registry-control delete-registry-record \
        --registry-id $REGISTRY_ID \
        --record-id $RECORD_ID --region $REGION > /tmp/rec-delete.log 2>&1; then
      echo "Deleted registry record: $RECORD_ID"
    else
      echo "ERROR: registry record $RECORD_ID was NOT deleted:" >&2
      cat /tmp/rec-delete.log >&2
    fi
  done

  aws agent-registry-control delete-registry \
    --registry-id $REGISTRY_ID --region $REGION

  # DeleteRegistry returns before it has finished, and it can still FAIL after
  # returning: it cascades into deleting the registry's own workload identity
  # (`registry-<id>`). Poll for the real outcome instead of announcing success.
  for ATTEMPT in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    STATUS=$(aws agent-registry-control list-registries --no-paginate \
      --query "registries[?registryId=='$REGISTRY_ID'].status | [0]" \
      --output text --region $REGION 2>/dev/null)
    case "$STATUS" in
      ""|None) STATUS=DELETED; break ;;
      DELETE_FAILED) break ;;
      *) sleep 3 ;;
    esac
  done

  if [ "$STATUS" = "DELETED" ]; then
    echo "Deleted registry: $REGISTRY_ID"
  elif [ "$STATUS" = "DELETE_FAILED" ]; then
    REASON=$(aws agent-registry-control list-registries --no-paginate \
      --query "registries[?registryId=='$REGISTRY_ID'].statusReason | [0]" \
      --output text --region $REGION 2>/dev/null)
    echo "ERROR: registry $REGISTRY_ID is DELETE_FAILED: $REASON" >&2
    echo "       If that mentions the workload identity, re-run this block with a" >&2
    echo "       principal that has bedrock-agentcore:DeleteWorkloadIdentity on" >&2
    echo "       .../workload-identity/registry-* — do NOT try to delete" >&2
    echo "       registry-$REGISTRY_ID yourself, it is service-linked and that" >&2
    echo "       call is refused for every caller." >&2
  else
    echo "Registry $REGISTRY_ID still $STATUS — re-run this block to check again."
  fi
done
:::

::alert[**`delete-registry` can fail after it has already returned.** Creating a registry silently creates a workload identity named `registry-<registryId>`, and deleting the registry cascades into deleting that identity **under your own principal**. If you cannot delete it, the registry lands in `DELETE_FAILED` with `Unable to delete workload identity because access was denied`. This was a real gap: the IDE role granted `bedrock-agentcore:DeleteWorkloadIdentity` only on `ac-*` and `workshop-*` names, while the auto-created one is `registry-*`. It is fixed in the workshop IAM policies, so you should not hit it — but the block above checks anyway, because a 200 from `delete-registry` does not mean the delete succeeded. If you ever do hit it, re-run the block with a principal that has the permission; do **not** try to delete `registry-<id>` yourself, because it is service-linked and `delete-workload-identity` is refused for every caller, administrators included.]{type="info"}

::alert[Registry cleanup uses the `agent-registry-control` API (the Registry's own namespace), while Gateway, Policy Engine, OAuth2, and WorkloadIdentity cleanup above stay on `bedrock-agentcore-control`. Requires **AWS CLI v2 ≥ 2.36.19**.]{type="info"}

Delete the Cedar policies and the policy engine created by notebook 07:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# The engine notebook 07 and the step-7 CLI page create is named
# `workshop_policy_engine`. This block used to look for `ac-gateway-policies`,
# which nothing creates, so it matched nothing, printed nothing, exited 0 — and
# the engine survived a full teardown.
ENGINE_ID=$(aws bedrock-agentcore-control list-policy-engines \
  --query "policyEngines[?name=='workshop_policy_engine'].policyEngineId | [0]" \
  --output text --region $REGION 2>/dev/null)

if [ -z "$ENGINE_ID" ] || [ "$ENGINE_ID" = "None" ]; then
  echo "No workshop_policy_engine found — notebook 07 was not run, nothing to do."
else
  echo "Policy engine: $ENGINE_ID"
  for POLICY_ID in $(aws bedrock-agentcore-control list-policies \
      --policy-engine-id $ENGINE_ID \
      --query 'policies[].policyId' --output text \
      --region $REGION 2>/dev/null); do
    # Without the redirect this prints the whole policy JSON, Cedar statement and
    # all, for every policy — and the echo below would claim success either way.
    if aws bedrock-agentcore-control delete-policy \
        --policy-engine-id $ENGINE_ID \
        --policy-id $POLICY_ID --region $REGION > /tmp/pol-delete.log 2>&1; then
      echo "Deleted Cedar policy: $POLICY_ID"
    else
      echo "ERROR: Cedar policy $POLICY_ID was NOT deleted:" >&2
      cat /tmp/pol-delete.log >&2
    fi
  done

  # DeletePolicy is eventually consistent: it returns before the engine stops
  # counting the policy. Deleting the engine on the next line without waiting
  # fails with "Policy engine still contains 2 policies and cannot be deleted".
  for ATTEMPT in 1 2 3 4 5 6 7 8 9 10; do
    LEFT=$(aws bedrock-agentcore-control list-policies \
      --policy-engine-id $ENGINE_ID \
      --query 'length(policies)' --output text --region $REGION 2>/dev/null)
    [ "$LEFT" = "0" ] && break
    sleep 3
  done

  # Report the real error instead of hiding it behind 2>/dev/null.
  if aws bedrock-agentcore-control delete-policy-engine \
      --policy-engine-id "$ENGINE_ID" --region $REGION > /tmp/pe-delete.log 2>&1; then
    echo "Deleted policy engine: $ENGINE_ID"
  else
    echo "Policy engine $ENGINE_ID not deleted yet:" >&2
    cat /tmp/pe-delete.log >&2
    echo "See the note below." >&2
  fi
fi
:::

::alert[**If that last delete reported `ConflictException: still associated with 1 resource`,** the engine is still attached to `ac-tools-gateway`. Notebook 07 attaches it with `update_gateway`, which is *not* part of the `AgentCoreGateway` CloudFormation resource — the template declares `InterceptorConfigurations` but no policy-engine configuration — so the attachment is out-of-band and detaching it restores the Gateway to its declared state rather than drifting it. **Module 3b notebook 08 step 2b does the detach for you** (it reads the Gateway's configuration and passes the interceptors back, because `UpdateGateway` is a full replace). Run that notebook, then re-run the block above. If you would rather not, the attachment also clears when `workshop-agentcore-stack` is deleted, and the step after `./deploy-cfn.sh destroy` picks the engine up. At an AWS event you do not need to do anything either way — Workshop Studio reclaims the whole account, and nothing is charged for an idle policy engine.]{type="info"}

Delete the OAuth2 credential providers that notebook 06 creates:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

for PROVIDER in workshop-gateway-oauth workshop-tools-gateway-auth; do
  # "not found" and "the delete failed" are different outcomes, and only one of
  # them means you are done. Sending stderr to /dev/null reported an expired
  # session or a missing permission as "not found", so a provider holding live
  # Cognito client credentials survived a cleanup that said it had removed it.
  if aws bedrock-agentcore-control delete-oauth2-credential-provider \
      --name $PROVIDER --region $REGION > /tmp/oauth-delete.log 2>&1; then
    echo "Deleted OAuth2 credential provider: $PROVIDER"
  elif grep -q "ResourceNotFoundException" /tmp/oauth-delete.log; then
    echo "OAuth2 credential provider not found: $PROVIDER"
  else
    echo "ERROR: OAuth2 credential provider $PROVIDER was NOT deleted:" >&2
    cat /tmp/oauth-delete.log >&2
  fi
done
:::

Leave the WorkloadIdentity alone. `ac-agent-identity` is the `WorkloadIdentity` resource of `workshop-agentcore-stack`, so `delete-stack` below removes it. Deleting it by hand leaves the stack drifted, and because AgentCore reports a missing WorkloadIdentity as `AccessDeniedException: Workload Identity does not belong to caller account` rather than a not-found error, Module 3b notebook 06 then fails with a message that looks like a permissions problem instead of a missing resource.

### Step 4: Module 2 — LLM Gateway

If you completed Module 2, delete the Bedrock Guardrail (if created). The LLM Gateway CloudFormation stack (`workshop-llm-gateway-stack`) is pre-provisioned and will be cleaned up automatically.

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# Delete the Module 2 Bedrock Guardrail (if created)
CONTENT_GUARDRAIL_ID=$(aws bedrock list-guardrails \
  --query "guardrails[?name=='workshop-content-filter'].id | [0]" \
  --output text --region $REGION)

if [ -n "$CONTENT_GUARDRAIL_ID" ] && [ "$CONTENT_GUARDRAIL_ID" != "None" ]; then
  if aws bedrock delete-guardrail \
      --guardrail-identifier $CONTENT_GUARDRAIL_ID \
      --region $REGION > /tmp/gd-delete.log 2>&1; then
    echo "Deleted guardrail: workshop-content-filter ($CONTENT_GUARDRAIL_ID)"
  else
    echo "ERROR: guardrail workshop-content-filter ($CONTENT_GUARDRAIL_ID) was NOT deleted:" >&2
    cat /tmp/gd-delete.log >&2
  fi
else
  echo "No workshop-content-filter found — nothing to do."
fi
:::

::alert[The LLM Gateway, MCP Gateway & Registry, Tools Gateway, and AgentCore stacks are all pre-provisioned and managed by the workshop platform. Do not delete them manually — they will be cleaned up automatically when the event ends.]{type="warning"}

## Delete pre-provisioned infrastructure (self-service only)

::alert[This step is only required if you deployed the workshop infrastructure yourself using the Self-Paced Setup instructions — those five stacks belong to you and you must remove them. At AWS events, these stacks are cleaned up automatically and you should skip this step.]{type="warning"}

::alert[**Module 4 (FAST) resources** are deployed separately via `cdk deploy` inside the workshop IDE — their cleanup is covered in **Step 1** / the [Module 4 Cleanup page](../module-4/cleanup/). Run that flow **before** deleting the workshop stacks below, so you still have access to the CDK environment when you tear down FAST.]{type="info"}

The repository's deploy engine tears down all five stacks for you, in reverse dependency order. From the repository root in your local terminal:

:::code{showCopyAction=true language=bash}
./deploy-cfn.sh destroy
:::

This removes the AgentCore, Tools Gateway, Registry, LLM Gateway, and Code Editor stacks. It also runs a **GuardDuty pre-cleanup step** (deleting GuardDuty-managed VPC endpoints and security groups) — without this, those managed resources block the Registry stack's VPC from deleting and the teardown hangs. Always prefer this script over deleting stacks by hand.

If a stack fails to delete, the script names it and the reason, then keeps going and deletes the rest — a single stuck stack will not leave DocumentDB, Fargate and the NAT Gateways running. Fix the reason it reports and re-run `./deploy-cfn.sh destroy`; it is safe to re-run, because stacks that are already gone are skipped.

Once every stack is gone, the script also removes the `cfn-deploy-…` S3 bucket it created for the nested templates. That bucket is **not** a CloudFormation resource, so nothing else would ever delete it, and the verification step below lists stacks rather than buckets — it would report a clean account while the bucket was still there. If the removal fails, the script prints the exact `aws s3 rb` command to finish the job. A re-run after a failed stack keeps the bucket, because the retry needs the templates in it.

### Check the policy engine is gone (Module 3b notebook 07 only)

Step 3 above already deletes the policy engine. Run this last sweep anyway: the engine is **not** a CloudFormation resource, so if Step 3 was skipped or reported a conflict, `./deploy-cfn.sh destroy` did not remove it and it is still there. Now that `workshop-agentcore-stack` is gone, nothing holds it and the delete succeeds. Skip this if you never ran Module 3b notebook 07.

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

ENGINE_ID=$(aws bedrock-agentcore-control list-policy-engines \
  --query "policyEngines[?name=='workshop_policy_engine'].policyEngineId | [0]" \
  --output text --region $REGION 2>/dev/null)

if [ -z "$ENGINE_ID" ] || [ "$ENGINE_ID" = "None" ]; then
  echo "No workshop_policy_engine found — already deleted, nothing to do."
else
  # Delete any Cedar policies still in it, then wait: DeletePolicy is eventually
  # consistent and DeletePolicyEngine rejects an engine that still counts one.
  for POLICY_ID in $(aws bedrock-agentcore-control list-policies \
      --policy-engine-id $ENGINE_ID \
      --query 'policies[].policyId' --output text \
      --region $REGION 2>/dev/null); do
    # Without the redirect this prints the whole policy JSON, Cedar statement and
    # all, for every policy — and the echo below would claim success either way.
    if aws bedrock-agentcore-control delete-policy \
        --policy-engine-id $ENGINE_ID \
        --policy-id $POLICY_ID --region $REGION > /tmp/pol-delete.log 2>&1; then
      echo "Deleted Cedar policy: $POLICY_ID"
    else
      echo "ERROR: Cedar policy $POLICY_ID was NOT deleted:" >&2
      cat /tmp/pol-delete.log >&2
    fi
  done
  for ATTEMPT in 1 2 3 4 5 6 7 8 9 10; do
    LEFT=$(aws bedrock-agentcore-control list-policies \
      --policy-engine-id $ENGINE_ID \
      --query 'length(policies)' --output text --region $REGION 2>/dev/null)
    [ "$LEFT" = "0" ] && break
    sleep 3
  done

  # Report the real error instead of hiding it behind 2>/dev/null. If this still
  # says ConflictException, workshop-agentcore-stack has not finished deleting.
  if aws bedrock-agentcore-control delete-policy-engine \
      --policy-engine-id "$ENGINE_ID" --region $REGION > /tmp/pe-delete.log 2>&1; then
    echo "Deleted policy engine: $ENGINE_ID"
  else
    echo "ERROR: policy engine $ENGINE_ID was NOT deleted:" >&2
    cat /tmp/pe-delete.log >&2
  fi
fi
:::

::::expand{header="Manual fallback: delete stacks individually"}
If you cannot run the script, delete the stacks in this order from the AWS CLI. Note this does **not** perform the GuardDuty pre-cleanup, so the Registry stack delete may stall on lingering network interfaces — if it does, remove the GuardDuty-managed VPC endpoints/security groups in that VPC and retry. Steps 6 and 7 replace the two things the script does after the stacks are gone: the orphaned-log-group sweep (needed only if you intend to redeploy into the same account and region) and removing the `cfn-deploy-…` bucket, which no CloudFormation stack owns.

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# 1. Delete the AgentCore stack first (depends on both Registry and Tools Gateway exports)
aws cloudformation delete-stack --stack-name workshop-agentcore-stack --region $REGION
aws cloudformation wait stack-delete-complete --stack-name workshop-agentcore-stack --region $REGION && echo "AgentCore stack deleted"

# 2. Delete the Tools Gateway stack (depends on Registry exports)
aws cloudformation delete-stack --stack-name workshop-tools-gateway-stack --region $REGION
aws cloudformation wait stack-delete-complete --stack-name workshop-tools-gateway-stack --region $REGION && echo "Tools Gateway stack deleted"

# 3. Delete the Registry stack
aws cloudformation delete-stack --stack-name workshop-registry-stack --region $REGION
aws cloudformation wait stack-delete-complete --stack-name workshop-registry-stack --region $REGION && echo "Registry stack deleted"

# 4. Delete the LLM Gateway stack (no dependencies)
aws cloudformation delete-stack --stack-name workshop-llm-gateway-stack --region $REGION
aws cloudformation wait stack-delete-complete --stack-name workshop-llm-gateway-stack --region $REGION && echo "LLM Gateway stack deleted"

# 5. Delete the Code Editor stack (no dependencies)
aws cloudformation delete-stack --stack-name code-editor --region $REGION
aws cloudformation wait stack-delete-complete --stack-name code-editor --region $REGION && echo "Code Editor stack deleted"

# 6. Remove the log groups that survive the delete and would block a redeploy.
#
# The AgentCore and Tools Gateway templates DECLARE these log groups. CloudFormation
# deletes them, but each stack's custom resources are still running their Delete
# handler at that moment, and the log lines they emit make Lambda re-create the
# group seconds later -- outside CloudFormation. You can see it afterwards: the
# orphan comes back with no retention policy even though the template sets one,
# and its newest log event predates its own creation time.
#
# Nothing charges for an empty log group, so this is not a cost issue. It matters
# because the next `deploy` fails with
#   The following hook(s)/validation failed: [AWS::EarlyValidation::ResourceExistenceCheck]
# which names no resource at all -- the template declares a log group that now
# already exists. `./deploy-cfn.sh destroy` sweeps these for you; deleting the
# stacks by hand does not.
#
# Whether an orphan comes back depends on whether anything actually logged during
# the delete window, so if you ran the module cleanup steps above first this loop
# often prints nothing at all. That is a pass, not a no-op -- the sweep is
# idempotent and safe to run either way.
for LG in \
  /aws/lambda/ac-cognito-init \
  /aws/lambda/ac-gateway-request-interceptor \
  /aws/lambda/ac-gateway-response-interceptor \
  /aws/lambda/ac-auto-review \
  /aws/lambda/ac-registry-gateway-sync \
  /aws/lambda/agentcore-gateway-request-interceptor \
  /aws/lambda/agentcore-gateway-response-interceptor \
  /aws/lambda/agentcore-gateway-sync \
  /aws/lambda/workshop-flights-mcp \
  /aws/lambda/workshop-hotels-mcp \
  /aws/lambda/workshop-search-knowledge-base \
  /aws/lambda/workshop-product-info-tool \
  /aws/lambda/workshop-order-processing-agent; do
  aws logs delete-log-group --log-group-name "$LG" --region $REGION 2>/dev/null \
    && echo "Deleted orphaned log group: $LG"
done
echo "Orphaned log group sweep complete."

# 7. Remove the deploy bucket. `./deploy-cfn.sh destroy` does this at the end;
# deleting the stacks by hand does not, and because the bucket is not a
# CloudFormation resource nothing else ever will -- the verification step below
# lists stacks, not buckets, so it would report a clean account with the bucket
# still sitting there. Its name is derived from the directory you cloned into, so
# match on the prefix and on this account and region rather than guessing it.
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
for B in $(aws s3api list-buckets \
    --query "Buckets[?starts_with(Name, 'cfn-deploy-')].Name" --output text); do
  case "$B" in
    *-${ACCOUNT_ID}-${REGION})
      if aws s3 rb "s3://$B" --force > /tmp/rb.log 2>&1; then
        echo "Deleted deploy bucket: $B"
      else
        echo "ERROR: deploy bucket $B was NOT deleted:" >&2
        cat /tmp/rb.log >&2
      fi
      ;;
  esac
done
:::
::::

## Verify cleanup

Confirm participant-deployed stacks have been removed:

:::code{showCopyAction=true language=bash}
aws cloudformation list-stacks \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query "StackSummaries[?contains(StackName, 'workshop-') || StackName == 'code-editor' || contains(StackName, 'FAST-stack')].StackName"
:::

For self-service deployments the output should be an empty list `[]`. At an AWS event the five pre-provisioned stacks will still appear — that is expected, because Workshop Studio removes them automatically when the event ends.

::alert[`[]` here does not mean the account is empty. This query deliberately lists only the stacks the workshop asks you to deploy, so it does not show the `CDKToolkit` stack that Module 4's `cdk bootstrap` created, nor its two asset stores. Those are shared with any other CDK app in the account, so removing them is optional and covered separately in [Module 4 Cleanup](../module-4/cleanup/#optional-remove-the-cdk-bootstrap-resources).]{type="info"}

::alert[Double-check that the highest-cost, always-on resources are gone, since these accrue charges by the hour even when idle: the **DocumentDB cluster** (Registry stack), the **ECS Fargate** services (LiteLLM and Registry), the **EFS file system** that persists the LLM gateway's PostgreSQL sidecar, **CloudFront** distributions, and the **NAT Gateway(s)**. If `./deploy-cfn.sh destroy` reported every stack deleted and the list above is empty, these are gone.]{type="warning"}

You can also check the AWS Cost Explorer to verify no unexpected charges are accruing:

:button[Open Cost Explorer]{target="_blank" href="https://console.aws.amazon.com/cost-management/home" variant="primary" iconName="external" iconAlign="right"}
