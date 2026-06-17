---
title: "At an AWS Event"
weight: 12
---

If you are participating in an AWS Immersion Day, public workshop, or a similar instructor-led event you get access to a temporary AWS account already pre-configured with all required resources.

Follow the instructions in this section to sign in to the pre-provisioned AWS account.

## Before You Start

- Log out from all existing AWS console sessions in all browser windows.
- Review the terms and conditions of the event. Do not upload any personal or confidential information in the account.
- The AWS account is only available for the duration of this workshop and you will not be able to retain access after the workshop is complete. Back up any materials you wish to keep access to after the workshop.
- All workshop resources are deployed to **US West (Oregon) / us-west-2**. Make sure your AWS console region switcher matches this.

## Access AWS Console

Use the button below to access your workshop AWS account, it will open in a new window/tab:

:button[Open AWS console]{target="_blank" href="https://catalog.us-east-1.prod.workshops.aws/event/account-login" variant="primary" iconName="external" iconAlign="right"}

Alternatively you can find the **Open AWS console** link at the bottom of the menu on the left side of the screen.

::alert[The AWS console will open in **US West (Oregon) / us-west-2** — the region Workshop Studio has deployed your workshop resources to. If you ever navigate away and lose the region, switch back to us-west-2 using the region selector in the top-right of the console.]{type="info"}

## Open the Workshop IDE

Your workshop environment includes a browser-based VS Code IDE with all required tools pre-installed (AWS CLI, Python, Git, Docker). All workshop commands should be run in this IDE's terminal.

Workshop Studio surfaces the IDE URL directly on your [Event dashboard](https://catalog.us-east-1.prod.workshops.aws/event/dashboard/en-US). Scroll to the **Event outputs** section, find the row with stack name `code-editor`, and click the `URL` value to open the IDE in a new tab:

![Event Outputs section showing the code-editor stack URL](/static/img/getting-started/event-outputs-codeeditor.png)

::alert[Once the IDE is open in a new tab, return to this page to continue — leave the IDE tab open so you can switch to it when a module asks you to run commands.]{type="info"}

### Sign in to the IDE

The first time you open the IDE URL you will see a password prompt from code-server. This gates the IDE so only you — not anyone who happens to stumble on the CloudFront URL — can reach it.

Copy the **`IdePassword`** value from the Event outputs (right next to **`URL`**) and paste it into the login form:

| Output key | What to do with it |
|---|---|
| `URL` | Click to open the IDE (you did this already) |
| `IdePassword` | Copy and paste into the code-server "Welcome to code-server" password field |

::alert[The password is **randomly generated** on every fresh workshop deploy — a 32-character string, not shared across events or participants. code-server remembers your session via a cookie, so you won't be prompted again unless you close the browser.]{type="info"}

You should see the VS Code IDE with the `/workshop/` folder pre-populated with the workshop source code:

![VS Code IDE with workshop source pre-staged at /workshop/](/static/img/getting-started/code-editor-landing.png)

::::expand{header="Alternative: find the URL via the CloudFormation console"}
Open the CloudFormation console in us-west-2, select the **`code-editor`** stack, go to the **Outputs** tab, and click the `URL` value.

:button[Open CloudFormation Console]{target="_blank" href="https://us-west-2.console.aws.amazon.com/cloudformation/home?region=us-west-2#/stacks" variant="primary" iconName="external" iconAlign="right"}
::::

::alert[The IDE runs on an EC2 instance in your workshop account with pre-configured AWS credentials. You do not need to paste CLI credentials into the IDE terminal — they are available automatically via the instance role. The default AWS region is also pre-set to `us-west-2`.]{type="info"}

## Kiro CLI in the Workshop IDE (Optional)

**Kiro CLI** (the successor to the `q` CLI — same lineage, renamed) is pre-installed in the Workshop IDE. It gives you a terminal-based AI assistant with access to MCP tools. It is **optional** — everything in the workshop works without it, so skip this section at a time-boxed event if you prefer and come back later.

Kiro authenticates against AWS Builder ID (a free, personal identity separate from your workshop AWS account) using a device-flow login. The workshop can't sign you in on your behalf because Builder ID is per-user, not per-AWS-account.

### Sign in to Kiro CLI

1. Open a terminal in the IDE: **Terminal → New Terminal** (or `` Ctrl+` ``)
2. Run the login command:

:::code{showCopyAction=true showLineNumbers=false language=bash}
kiro-cli login --use-device-flow
:::

3. When prompted, choose **Use for Free with Builder ID** (or **Use with Pro license** if your employer has one)
4. The CLI prints a URL and a user code. Open the URL in a new browser tab
5. On the Builder ID page, confirm the code and click **Allow access**
6. Return to the terminal — you will see a confirmation message

### Using Kiro CLI

Start a conversation:

:::code{showCopyAction=true showLineNumbers=false language=bash}
kiro-cli chat
:::

A few prompts that pair well with this workshop:

- *"Explain what `create_gateway.py` does and which IAM permissions it needs."*
- *"This CloudFormation stack output is empty — how do I find the actual value and why might it be missing?"*
- *"What does this Lambda interceptor do? Walk me through the request/response flow."*
- *"Summarise the differences between the MCP path and the AgentCore path in Module 4."*

Useful in-chat commands: `/clear` (reset context), `/model` (switch models), `/tools` (list MCP tools).

::alert[Use Kiro for exploration and code explanation; do not paste real credentials, customer data, or anything confidential into the chat. Workshop accounts are short-lived and shared for testing.]{type="warning"}

## Where to Run CLI Commands

::alert[**Every `aws` / `bash` / `python` command in every module is intended for the Workshop IDE's terminal.** The IDE has pre-configured AWS credentials, the correct default region (`us-west-2`), and the `/workshop/` folder pre-staged. Do not run module commands from your laptop terminal — credentials, region defaults, and file paths will not match.]{type="warning"}

### First time using VS Code in the browser?

If you have not used VS Code in the browser before, three quick orientation tips:

1. **Open a terminal.** Top menu → **Terminal** → **New Terminal** (keyboard: `` Ctrl+` `` on Windows/Linux, `` Cmd+` `` on macOS). A panel opens at the bottom — this is where you paste the `aws` / `bash` / `python` commands from every module. The terminal already runs as user `participant` inside the `/workshop/` folder.
2. **Copy the IDE URL again if you lose it.** If you accidentally close the IDE tab, the URL is always available on the Workshop Studio **Event outputs** panel (the same row as `IdePassword`) — see screenshot above. Re-open that URL and paste your password again.
3. **Open a file from the explorer.** The left-hand panel shows the `/workshop/` folder tree. Click a `.py`, `.md`, or `.ipynb` file to open it. Notebooks open in VS Code's notebook view — when a kernel is needed, pick **`Python 3 (workshop)`** from the kernel-picker at the top-right of the notebook.

### Using a Local Terminal (not recommended)

If the Workshop IDE is unavailable and you must fall back to a local terminal, you can pull short-lived CLI credentials from the workshop menu. Select the **Get AWS CLI credentials** menu item on the left:

![AWS credentials menu item](/static/img/getting-started/credentials-menu.png)

![AWS credentials popup](/static/img/getting-started/credentials-account-access.png)

Copy the credentials and paste them into your local terminal. They are valid for the duration of the workshop session. Note that paths like `/workshop/source/...` referenced throughout the modules will not exist on your laptop — you will need to clone the workshop repository and adjust paths manually. For these reasons we strongly recommend using the IDE terminal.

## Pre-Provisioned Infrastructure

Your workshop environment comes with infrastructure already deployed:

| Stack | What It Provides | Used By |
|-------|------------------|---------|
| **workshop-llm-gateway-stack** | LiteLLM Proxy on ECS Fargate — virtual keys, budgets, Bedrock Guardrails, spend tracking, Bedrock model access | Module 2, Module 4 |
| **workshop-registry-stack** | MCP Gateway & Registry — Registry UI, Auth Server, Keycloak, MCP Gateway, demo MCP servers, Cognito User Pool (shared identity for all stacks) | Module 3a, Module 4 |
| **workshop-tools-gateway-stack** | AgentCore Gateway (MCP path) — Sync Lambda, Request/Response interceptors, demo tool Lambdas | Module 3a, Module 4 |
| **workshop-agentcore-stack** | AgentCore Registry & Gateway (AWS-native path) — persona IAM roles, interceptor Lambdas, EventBridge auto-review rule, WorkloadIdentity | Module 3b, Module 4 |
| **code-editor** | Browser-based VS Code IDE on EC2 — pre-installed AWS CLI, Python, Docker, Strands Agents, workshop source code | All modules |

::alert[You do not need to deploy any of these resources. They are all provisioned automatically when the workshop environment starts. Each module walks you through exploring and configuring the pre-deployed infrastructure.]{type="info"}

## Jupyter Notebooks (Optional)

Several modules offer a Jupyter notebook walkthrough as an alternative to the CLI steps. The workshop source code — including all notebooks — is pre-staged at `/workshop/source/`, and Jupyter, `ipykernel`, and all Python dependencies (`boto3`, `httpx`, `strands-agents`, etc.) are already installed in the IDE. Each module's notebook walkthrough will point you to the relevant `.ipynb` file.

To run a notebook, open the `.ipynb` file from the file explorer in the IDE. The first time you run a cell, VS Code will prompt you to select a kernel in the top-right — choose **`Python 3 (workshop)`**. This is the single most common silent failure: if cells raise `ModuleNotFoundError`, the wrong kernel is almost certainly selected.

::alert[The IDE's instance role credentials are automatically available to notebook kernels. You do not need to configure credentials separately inside the notebooks.]{type="info"}

