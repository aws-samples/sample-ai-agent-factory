---
title: "Cleanup"
weight: 79
---

::alert[If you are using an AWS-provided event account, the account will be cleaned up automatically after the event ends. You can skip this step.]{type="info"}

## Destroy the FAST CDK Stack

This removes the AgentCore Runtime, Memory, Gateway, Cognito User Pool, Amplify app, and all associated resources:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent/infra-cdk
npx cdk destroy --force
:::

::alert[This deletes all data including the Amplify frontend, ECR images, and AgentCore Memory contents. This action cannot be undone.]{type="error"}

## Clean Up SSM Parameters

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

for param in gateway_url llm_gateway_url llm_gateway_key gateway_credential_provider; do
  aws ssm delete-parameter --name "/FAST-stack/${param}" --region $REGION 2>/dev/null && echo "Deleted /FAST-stack/${param}" || true
done
:::

## Delete the OAuth2 Credential Provider

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws bedrock-agentcore-control delete-oauth2-credential-provider \
  --name "workshop-tools-gateway-auth" \
  --region $REGION 2>/dev/null && echo "Deleted" || echo "Not found (already deleted)"
:::

## Full Workshop Cleanup

For cleanup of platform resources (LLM Gateway, Registry, Tools Gateway stacks), follow the [Workshop Cleanup](../../cleanup/) page.

## Notebook Walkthrough (Optional alternative)

::alert[**Do not run `08-cleanup.ipynb` until you have finished Module 4.** It removes the FAST stack (Amplify, Runtime, Memory, Gateway, Cognito), the `/FAST-stack/*` SSM parameters, and the `workshop-tools-gateway-auth` OAuth2 credential provider. Platform foundations (Modules 2 and 3) remain untouched — use the global [Workshop Cleanup](../../cleanup/) page when you finish the whole workshop.]{type="warning"}

> This notebook covers the same cleanup as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4b-fast/notebooks/` and open **`08-cleanup.ipynb`**.
