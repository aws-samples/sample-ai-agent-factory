---
title: "Cleanup"
weight: 87
---

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach, both achieve the same result.]{type="info"}

Delete what this module created: the online evaluation configuration (it holds a reference to the space's evaluation role and keeps scoring until it is gone), then the local state files. The domain, the space, the Dataset integration, the access grants, the roles and Transaction Search belong to `workshop-agentcore-stack` (the instance-role grant to `code-editor`) and are removed with those stacks.

::alert[At an AWS event, Workshop Studio recycles the whole account when the event ends, so this page is optional there. In your own account, run it, then finish with `./deploy-cfn.sh destroy`: the online evaluation configuration bills while it exists, and the space's Dataset and Transaction Search indexing bill until the stack that owns them is deleted.]{type="warning"}

## CLI walkthrough

### Step 1: Delete the online evaluation configuration

Report each outcome instead of hiding failures: a configuration that survives because the delete was denied keeps scoring, and keeps costing judge inference.

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

for ID in $(aws bedrock-agentcore-control list-online-evaluation-configs --region $REGION \
    --query "onlineEvaluationConfigs[?onlineEvaluationConfigName=='workshop_travel_agent'].onlineEvaluationConfigId" \
    --output text); do
  if aws bedrock-agentcore-control delete-online-evaluation-config \
      --online-evaluation-config-id "$ID" --region $REGION > /tmp/eval-delete.log 2>&1; then
    echo "Deleted online evaluation config $ID"
  else
    echo "ERROR: $ID was NOT deleted:" >&2
    cat /tmp/eval-delete.log >&2
  fi
done
:::

The results log group `/aws/bedrock-agentcore/evaluations/results/<id>` stays behind as a record; delete it from the CloudWatch console if you want the account clean.

### Step 2: Remove the local state

The domain, the space, the Dataset integration and the two access grants belong to `workshop-agentcore-stack` and `code-editor` — CloudFormation created them and CloudFormation deletes them, with the account at the end of a Workshop Studio event or with `./deploy-cfn.sh destroy` in your own account. The participant policy has no `DeleteDomain`, `DeleteSpace` or `DeleteDatasetIntegration`, so there is nothing to delete from here; only clear the files this module wrote:

:::code{showCopyAction=true showLineNumbers=false language=bash}
rm -f ~/.workshop-omni.env /tmp/omni-service-name /tmp/online-eval.json
:::

### Step 3: Verify

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "Eval configs: $(aws bedrock-agentcore-control list-online-evaluation-configs --region $REGION --query 'length(onlineEvaluationConfigs)')"
echo "Domains:      $(aws cloudwatchomni list-domains --region $REGION --query 'length(items)')  (stays 1 until the stack is deleted)"
:::

The evaluation count is `0` (the delete completes a few seconds after the call returns, so re-run the block if it still shows `1`); the domain count stays `1` until `workshop-agentcore-stack` is deleted.

## What stays

| Left behind | Why | Action |
|-------------|-----|--------|
| `AWSServiceRoleForCloudWatch_Omni` service-linked role | Created by the service on first use; the service recreates it when needed | Leave it. Deleting it can fail with an internal error while Omni still references it |
| Domain, space, Dataset integration, the two access grants, Transaction Search (`CloudWatchLogs` destination, 100 % indexing) and the three roles | Owned by `workshop-agentcore-stack` (the instance-role grant by `code-editor`) | Removed when the stacks are deleted (self-paced: `./deploy-cfn.sh destroy`) |
| Spans and logs in `aws/spans` and the runtime log groups | Retention is set on the log groups, not by Omni | Expire on their own, or delete the groups |

Continue to the [workshop cleanup](/cleanup) page to remove the rest of the platform.

---

## Notebook walkthrough (optional alternative)

> This notebook (08-cleanup.ipynb) deletes the online evaluation configuration and the local state file from Python and prints the verification counts. Follow *either* path.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker.
>
> Navigate to `source/module-5-omni/notebooks/` and open **`08-cleanup.ipynb`**.
