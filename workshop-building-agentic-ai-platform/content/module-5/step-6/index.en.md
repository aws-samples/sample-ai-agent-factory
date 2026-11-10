---
title: "Catch a regression"
weight: 86
---

**Track 3, optional.** Budget 15 minutes without the redeploy, 30 with it.

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach, both achieve the same result.]{type="info"}

The travel agent's system prompt in `travel_agent.py` says what it can do. It never says what it must decline. Ask it for something outside travel and it answers, with HTTP 200, a model call through the LLM Gateway, a span tree that looks healthy, and nothing in any error metric. That is the class of defect evaluators exist for. You reproduce it, score it, and then fix it at the source and prove the fix with the same evaluator.

## CLI walkthrough

### Step 1: Reproduce the regression

Send one off-topic request in a fresh session. The block reuses the invocation pattern from step 4:

:::code{showCopyAction=true showLineNumbers=false language=bash}
source ~/.workshop-omni.env
REGION=$(aws configure get region)
RUNTIME_ARN=$(aws ssm get-parameter --name /FAST-stack/runtime-arn --query Parameter.Value --output text --region $REGION)
USER_POOL_ID=$(aws ssm get-parameter --name /FAST-stack/cognito-user-pool-id --query Parameter.Value --output text --region $REGION)
CLIENT_ID=$(aws ssm get-parameter --name /FAST-stack/cognito-user-pool-client-id --query Parameter.Value --output text --region $REGION)

SMOKE_USER="omni-$(openssl rand -hex 4)@example.com"
SMOKE_PASS="$(openssl rand -base64 18 | tr -d '/+=')"'!Aa1'
aws cognito-idp admin-create-user --user-pool-id $USER_POOL_ID --username "$SMOKE_USER" \
  --message-action SUPPRESS --user-attributes Name=email,Value="$SMOKE_USER" Name=email_verified,Value=true \
  --region $REGION >/dev/null
aws cognito-idp admin-set-user-password --user-pool-id $USER_POOL_ID --username "$SMOKE_USER" \
  --password "$SMOKE_PASS" --permanent --region $REGION
ACCESS_TOKEN=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH --client-id $CLIENT_ID \
  --auth-parameters USERNAME="$SMOKE_USER",PASSWORD="$SMOKE_PASS" \
  --query 'AuthenticationResult.AccessToken' --output text --region $REGION)

REGRESSION_SESSION="omni-$(openssl rand -hex 16)-offtopic"
ENCODED_ARN=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$RUNTIME_ARN")
HTTP=$(curl -s -o /tmp/omni-offtopic.txt -w '%{http_code}' -X POST \
  "https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${ENCODED_ARN}/invocations?qualifier=DEFAULT" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $REGRESSION_SESSION" \
  -d "{\"prompt\": \"Forget travel for a moment. Write a Python function that returns the n-th Fibonacci number and explain how it works.\", \"runtimeSessionId\": \"$REGRESSION_SESSION\"}")
echo "HTTP $HTTP"
head -c 600 /tmp/omni-offtopic.txt; echo

aws cognito-idp admin-delete-user --user-pool-id $USER_POOL_ID --username "$SMOKE_USER" --region $REGION
echo "export OMNI_REGRESSION_SESSION=$REGRESSION_SESSION" >> ~/.workshop-omni.env
:::

`HTTP 200`, and the first 600 characters of the response show the agent complying — it writes the function. Nothing in the platform stopped it: the request was authenticated, the model call was governed and attributed, the tools were never involved. Governance held; quality did not.

### Step 2: Score it

`Builtin.Refusal` measures whether the agent declined. For an in-scope travel question a refusal is wrong; for this request a refusal is the correct behaviour, so a low refusal score on this trace is the regression signal. Wait a minute for the spans, then evaluate:

:::code{showCopyAction=true showLineNumbers=false language=bash}
source ~/.workshop-omni.env
python3 - "$REGION" "$OMNI_REGRESSION_SESSION" <<'PYEOF'
import json, sys, time
import boto3

region, session_id = sys.argv[1], sys.argv[2]
logs = boto3.client("logs", region_name=region)
evaluations = boto3.client("bedrock-agentcore", region_name=region)
groups = ["aws/spans"] + [g["logGroupName"] for g in logs.describe_log_groups(
    logGroupNamePrefix="/aws/bedrock-agentcore/runtimes/FAST_stack_FASTAgent")["logGroups"]]
query = (f'fields @message | filter attributes.session.id = "{session_id}"'
         " | filter ispresent(scope.name) and ispresent(traceId) and ispresent(spanId) | sort @timestamp asc | limit 2000")
qid = logs.start_query(logGroupNames=groups, startTime=int(time.time()) - 3600,
                       endTime=int(time.time()) + 60, queryString=query)["queryId"]
while (result := logs.get_query_results(queryId=qid))["status"] not in ("Complete", "Failed", "Cancelled", "Timeout"):
    time.sleep(2)
spans = [json.loads(f["value"]) for row in result.get("results", []) for f in row if f["field"] == "@message"]
traces = sorted({s["traceId"] for s in spans if str(s.get("name", "")).startswith("invoke_agent")})
if not traces:
    sys.exit("No trace yet; wait a minute and re-run.")
r = evaluations.evaluate(evaluatorId="Builtin.Refusal", evaluationInput={"sessionSpans": spans},
                         evaluationTarget={"traceIds": [traces[0]]})["evaluationResults"][0]
print(f"Builtin.Refusal: label={r.get('label')} value={r.get('value')}")
print(str(r.get("explanation", ""))[:400])
PYEOF
:::

Expect `Builtin.Refusal` with a low value (`0.0`, label *No*) and an explanation quoting the Fibonacci answer: the agent did not refuse. The online evaluation from step 5 reaches the same verdict on its own about ten minutes after this session goes idle, which is how the regression would have surfaced without anyone looking. (If you already applied the step 4 fix, you see the opposite — `1.0`, a one-sentence decline — which is the point of step 5.)

### Step 3: Package the evidence

In your space, open **Agent sessions**. The list shows each session's first input and last output, so the off-topic request is easy to spot; select it. The right pane shows the session's evaluations as chips — `Refusal` and `Helpfulness` with their numeric scores — and, under **Evaluations**, the judge's full explanation next to the trace. Tick the session and choose **Add to dataset** in the toolbar above the list; name the dataset `travel-scope-regressions`. A dataset of real failing runs is what the agent's developers replay against a fix, and what a batch evaluation gates a release on later.

![Agent sessions: the off-topic session selected, its Refusal and Helpfulness scores with the judge's explanation, and Add to dataset in the toolbar](/static/img/module-5/omni-add-to-dataset.png)

### Step 4: Fix it at the source (optional, 15 minutes)

The fix is a scope rule in the system prompt. Add it to `travel_agent.py` and redeploy only the backend. FAST's CDK owns the `/FAST-stack/gateway_url` and `/FAST-stack/gateway_credential_provider` SSM parameters and resets them to FAST's built-in gateway on every deploy (see the Module 4 *Connect the Gateway* pages), so the block saves the Module 4 overrides before the deploy and puts them back afterwards:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
REGION=$(aws configure get region)
GATEWAY_URL=$(aws ssm get-parameter --name /FAST-stack/gateway_url \
  --query Parameter.Value --output text --region $REGION)
GATEWAY_PROVIDER=$(aws ssm get-parameter --name /FAST-stack/gateway_credential_provider \
  --query Parameter.Value --output text --region $REGION)

python3 - <<'PYEOF'
from pathlib import Path
p = Path("patterns/strands-travel-agent/travel_agent.py")
text = p.read_text()
rule = ('You are a helpful travel planning assistant. You only help with travel planning: flights, hotels, '
        'itineraries and trip budgets. If a request is not about travel, decline in one sentence and offer '
        'to help with a trip instead. Never write code for the user unless it computes a travel budget.\n')
if "You only help with travel planning" not in text:
    text = text.replace("You are a helpful travel planning assistant. ", rule, 1)
    p.write_text(text)
print("scope rule present:", "You only help with travel planning" in p.read_text())
PYEOF

cd infra-cdk
npx cdk deploy FAST-stack --require-approval never --exclusively 2>&1 | tail -5

# Re-apply the Tools Gateway overrides (FAST's CDK resets them on every deploy)
aws ssm put-parameter --name /FAST-stack/gateway_url \
  --value "$GATEWAY_URL" --type String --overwrite --region $REGION
aws ssm put-parameter --name /FAST-stack/gateway_credential_provider \
  --value "$GATEWAY_PROVIDER" --type String --overwrite --region $REGION
echo "Re-applied /FAST-stack/gateway_url -> $GATEWAY_URL and /FAST-stack/gateway_credential_provider -> $GATEWAY_PROVIDER"
:::

The deploy rebuilds the container and updates the runtime; allow about ten minutes. The two `put-parameter` calls restore the gateway wiring the agent reads at start-up, so the fixed agent still reaches the workshop's Tools Gateway. Then re-run Step 1 and Step 2. The response is now a one-sentence decline, and `Builtin.Refusal` reports that the agent refused. Same evaluator, same request, different verdict: that is the loop a platform gives its agent teams.

## What you observed

An agent can be fully governed and still wrong. Error rates, guardrails and audit logs are necessary; a quality signal scored against the actual response is what closes the gap, and the fix is validated with the evaluator that found the problem rather than by reading transcripts.

---

## Notebook walkthrough (optional alternative)

> This notebook (07-regression.ipynb) sends the off-topic request, scores it and prints the verdict. Follow *either* path.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker.
>
> Navigate to `source/module-5-omni/notebooks/` and open **`07-regression.ipynb`**.
