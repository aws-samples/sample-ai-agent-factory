---
title: "Explore the LLM Gateway"
weight: 32
---

The LLM Gateway CloudFormation stack was automatically deployed when your workshop environment was provisioned. In this step you will explore what was deployed, capture the outputs you need for the rest of this module, and verify the gateway is healthy.

::alert[The `workshop-llm-gateway-stack` is pre-provisioned by Workshop Studio — you do not need to deploy it yourself. It was deployed automatically when your environment started.]{type="info"}

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

::alert[**First-time Anthropic model access in a fresh account.** Before the LLM Gateway can invoke any Claude model on your behalf, Anthropic requires a one-time use case submission for this AWS account. You must prime **both** Claude Sonnet 4.5 and 4.6 — Module 2 and the LLM Gateway path use 4.6, and the baseline Module 4 agent uses 4.5. Open the Bedrock console, prime each model, and return here.]{type="warning"}

::alert[Before opening the Bedrock console, ensure you are logged into the correct Workshop Studio AWS account. If not, open the **Event Dashboard** first (link in the left menu) to access your account, then return here to open Bedrock.]{type="warning"}

::alert[Before clicking **Open Bedrock Console** below, ensure you have completed federated login as `wsparticipant` in your Workshop Studio AWS account. If you haven't accessed your account yet, log in to the Event Dashboard first to establish your federated session.]{type="info"}

::alert[When enabling model access in the Bedrock console, confirm the **region selector** (top-right of the console) matches the region you deployed the workshop into — model access is granted per region. The button below opens the Bedrock console; switch the region selector if needed.]{type="info"}

:button[Open Bedrock Console]{target="_blank" href="https://console.aws.amazon.com/bedrock/home" variant="primary" iconName="external" iconAlign="right"}

::::expand{header="First-time setup — step by step"}
1. In the Bedrock console left navigation, open **Model catalog**.
2. Find **Claude Sonnet 4.5** (use the search/filter if needed) and click the model card.
3. Click **Open in playground** in the top-right of the model page.
4. Type `hi` in the prompt box and click **Run**.
5. If the **Submit use case details for Anthropic** dialog appears, fill the form (use `Workshop testing` for the use case) and click **Submit**.
6. Wait for the model to return a response.
7. Go back to **Model catalog** and repeat steps 2–6 for **Claude Sonnet 4.6**.
8. You can now close the playground — the account is primed for both models (LiteLLM and the baseline FAST agent).
::::

## What Was Deployed

The `workshop-llm-gateway-stack` deployed the architecture described in the previous step — LiteLLM Proxy + PostgreSQL on ECS Fargate, fronted by API Gateway, running in an isolated VPC. All resources are listed in the CloudFormation **Resources** tab if you want to explore them.

## CLI Walkthrough

As the **infrastructure engineer**, your first task is to verify the pre-deployed LLM Gateway is healthy and capture the credentials you'll need to configure teams, virtual keys, and guardrails in the following steps.

### 2.1 Verify the stack was deployed

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=${AWS_REGION:-$(aws configure get region)}

aws cloudformation describe-stacks \
  --stack-name workshop-llm-gateway-stack \
  --query "Stacks[0].StackStatus" \
  --output text --region $REGION
:::

You should see `CREATE_COMPLETE`.

### 2.2 Capture stack outputs

Read the CloudFormation outputs into shell variables for use in subsequent steps:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=${AWS_REGION:-$(aws configure get region)}

export LLM_GATEWAY_URL=$(aws cloudformation describe-stacks \
  --stack-name workshop-llm-gateway-stack \
  --query "Stacks[0].Outputs[?OutputKey=='ProxyUrl'].OutputValue" \
  --output text --region $REGION)

export ALB_DNS=$(aws cloudformation describe-stacks \
  --stack-name workshop-llm-gateway-stack \
  --query "Stacks[0].Outputs[?OutputKey=='ALBDnsName'].OutputValue" \
  --output text --region $REGION)

export ADMIN_KEY_ARN=$(aws cloudformation describe-stacks \
  --stack-name workshop-llm-gateway-stack \
  --query "Stacks[0].Outputs[?OutputKey=='AdminKeySecretArn'].OutputValue" \
  --output text --region $REGION)

echo "Proxy URL:  ${LLM_GATEWAY_URL}"
echo "ALB DNS:    ${ALB_DNS}"
echo "Secret ARN: ${ADMIN_KEY_ARN}"
:::

::alert[Alternatively, you can view these same outputs in the AWS Console: navigate to **CloudFormation** → **Stacks** → **workshop-llm-gateway-stack** → **Outputs** tab.]{type="info"}

### 2.3 Retrieve the admin key

The admin key was auto-generated and stored in Secrets Manager. Retrieve it:

:::code{showCopyAction=true showLineNumbers=false language=bash}
export LLM_GATEWAY_ADMIN_KEY="sk-$(aws secretsmanager get-secret-value \
  --secret-id "${ADMIN_KEY_ARN}" \
  --query SecretString \
  --output text)"

echo "Admin Key: sk-****${LLM_GATEWAY_ADMIN_KEY: -4} (full value stored in \$LLM_GATEWAY_ADMIN_KEY)"
:::

::alert[The admin key has full admin access to the LiteLLM Proxy. In production, use it only for administrative tasks (creating teams, keys). Agents and applications should use virtual keys with scoped budgets.]{type="warning"}

### 2.4 Wait for the gateway to be healthy

The ECS task needs time to pull images, start PostgreSQL, run migrations, and begin accepting requests. Poll the liveliness endpoint until it returns `"I'm alive!"`:

:::code{showCopyAction=true showLineNumbers=false language=bash}
for i in $(seq 1 30); do
  if curl -fsS "${LLM_GATEWAY_URL}/health/liveliness" 2>/dev/null | grep -q "alive"; then
    echo "Gateway is healthy after ${i} attempts."
    break
  fi
  echo "Attempt ${i}/30 - gateway not ready yet, waiting 10s..."
  sleep 10
done
curl "${LLM_GATEWAY_URL}/health/liveliness"
:::

You should see, on the last line:

```text
"I'm alive!"
```

If the poll exits after 30 attempts (5 minutes) without `Gateway is healthy`, the ECS task likely failed to start. Check the ECS service events: `aws ecs describe-services --cluster <cluster> --services <service> --query "services[0].events[0:5]"`.

### 2.5 Open the LiteLLM Admin UI

The LLM Gateway includes a web-based admin dashboard for managing teams, virtual keys, models, and spend tracking. Open it in your browser:

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "Admin UI: ${LLM_GATEWAY_URL}/ui"
:::

Log in with:

| Field | Value |
|-------|-------|
| Username | `admin` |
| Password | Your admin key (the full `sk-...` value from step 2.3) |

Explore the dashboard — you will see the available models, and in later steps you will create teams and virtual keys that appear here with spend tracking.

![LiteLLM Admin UI dashboard showing models and configuration](/static/img/module-2/litellm-admin-ui.png)

---

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-2-llm-gateway/notebooks/` and open the corresponding notebook.

Open **`step-2-deploy.ipynb`**. This notebook walks you through verifying the pre-deployed stack:

1. **Install dependencies** — The first cell installs `boto3`, `requests`, and `pydantic`.
2. **Verify the stack** (cell 2.3) — Confirms `workshop-llm-gateway-stack` is `CREATE_COMPLETE` and reads CloudFormation outputs.
3. **Capture stack outputs** (cell 2.4) — Uses `boto3` to retrieve the gateway URL and admin key from CloudFormation outputs and Secrets Manager. Pay attention to how the outputs are extracted — you will reference these values in every subsequent step.
4. **Health check polling** (cell 2.5) — Polls the `/health/liveliness` endpoint in a loop until the gateway is ready, giving you visual feedback on the provisioning progress.
5. **Verify ECS service** (cell 2.6) — Queries the ECS API to confirm the service is running with the expected task count.
6. **Save state** (cell 2.7) — Persists the gateway URL and keys to `.state.json` so that subsequent notebooks can load them automatically without re-querying CloudFormation.

::alert[Run every cell in order. The final cell saves state that Step 3's notebook depends on.]{type="warning"}
