---
title: "Sign in to your space"
weight: 82
---

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach, both achieve the same result.]{type="info"}

Omni lives outside the AWS Management Console, at your domain's URL. Members normally sign in through IAM Identity Center. The workshop domain has no identity provider, so members sign in as IAM roles. You hold two of them: the browser you are reading this in is signed in to the AWS console as `WSParticipantRole`, and the Code Editor terminal runs as the instance role. CloudFormation created the space when Workshop Studio ran `workshop-agentcore-stack`, and the two stacks granted your two identities Space Admin: `workshop-agentcore-stack` for `WSParticipantRole`, the `code-editor` stack for the instance role. Step 1 verified those grants, so either path below works; the console path is the one your colleagues would use day to day.

## CLI walkthrough

### Step 1: Open the space from the CloudWatch console

:button[Open the Omni page in CloudWatch]{target="_blank" href="https://console.aws.amazon.com/cloudwatch/home#settings:/omni" variant="primary" iconName="external" iconAlign="right"}

Under **Launch Omni**, choose **Sign in**. The console mints and redeems a one-time code for the role you are signed in with (`WSParticipantRole`) and opens the space in a new tab. The same page shows the domain, the space and the **Space admins** tab, which is where a platform team adds administrators without entering Omni.

### Alternative: mint a deep link from the terminal

A deep link does the same from the command line, for the identity that runs the command — here the instance role:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)
DOMAIN_ID=$(aws cloudwatchomni list-domains --region $REGION --query 'items[0].domainId' --output text)

DOMAIN_NAME=$(aws cloudwatchomni get-domain --domain-id $DOMAIN_ID --region $REGION \
  --query 'domain.name' --output text)
DOMAIN_URL="https://${DOMAIN_NAME}.cloudwatch-omni.global.app.aws"

# The API returns the code as a path on your domain (/auth/code?code=...), not a full URL.
DEEP_LINK=$(aws cloudwatchomni create-one-time-deep-link-code \
  --domain-id $DOMAIN_ID \
  --ttl-seconds 600 \
  --region $REGION \
  --query 'deepLinkUrl' --output text)

echo "Open within 10 minutes: ${DOMAIN_URL}${DEEP_LINK}"
:::

The line is your domain's sign-in host followed by the one-time code path; the code expires 10 minutes out (600 seconds is the longest TTL the API accepts). Copy the whole URL into a new browser tab. The code is deleted the moment it is redeemed, so a second click on the same link fails with a `400`; mint another one instead.

### Step 2: Find your way around the space

You land in **workshop-space** directly; the space name sits bottom-left, above **Settings** and **Account**, and is the switcher if you ever have more than one. The home page summarises what the Dataset already holds ("Your space includes *N* services and 1 agent") and lists every view in three groups. The box at the bottom, *Ask anything…*, starts a **thread**: a conversation with Omni that can query the Dataset for you (step 3 uses it).

![Omni space home: services and agent counts, and the three groups of views](/static/img/module-5/omni-space-home.png)

The three groups are the map for the rest of this module:

| Group | Views | Used in |
|-------|-------|---------|
| Application performance monitoring | Application map, Services, Traces | step 3 |
| Analytics and dashboards | Explore logs, Explore metrics, Alerts, Investigations, Dashboards, Markdown | step 3 |
| Agent observability | Agent overview, Evaluation dashboard, Evaluators, Online evaluations, Agent traces, Agent sessions, Agent topology, Datasets, Prompt playground | steps 4, 5 and 6 |

### Step 3: Confirm who you are inside the space

1. Open **Settings** (bottom-left), then **Manage permissions** under *Space*.
2. Check that the table lists these four principals. The first four columns below are the screen's own headers; the last two add what the screen does not show.

| Principal name | Type | Permission | Grants | Created by | Why it exists |
|----------------|------|------------|--------|------------|---------------|
| Code Editor instance role (`code-editor-CodeEditorInstanceBootstrapRole-...`) | IAM Role | Space admin | `code-editor-space-admin` | The `code-editor` stack | Admits the terminal, the notebooks and the deep link |
| `agent-<space id>` | Agent | Custom | `OmniDefaults-AsyncAccessProfile-AgentCanAssume` | Omni | Lets Omni's background work act inside the space |
| `WSSystemRole` | IAM Role | Space admin | `OmniDefaults-Space-Admin` | Omni, for whoever creates a space (here the role Workshop Studio ran CloudFormation as) | The space's first admin |
| `WSParticipantRole` | IAM Role | Space admin | `participant-space-admin` | `workshop-agentcore-stack` | Admits your console sign-in |

The screen does not show the grant type. The CLI table in step 1 does: `CUSTOMER_MANAGED` for the two grants the stacks created, `SERVICE_MANAGED` for the two Omni created itself.

Then select a principal to see its grants. **Add grant** on this screen is where a platform team adds other roles, Identity Center users or groups, each at Viewer, Editor, Space admin or a custom action list, optionally with a data scope that filters which rows they can read.

**Deploying into your own account?** The table has three rows, not four. The identity that first ran `./scripts/self-service-deploy.sh` created the space, so it is the row holding `OmniDefaults-Space-Admin` (a later deployer does not inherit that row), and there is no `WSSystemRole` row. There is no `participant-space-admin` row either, unless you deployed with `OMNI_CONSOLE_PRINCIPAL_ARN` set to another identity or added one with step 1's **Admit another console identity** block: Omni rejects a grant for a principal that does not exist, and a second Space Admin grant for a principal that already holds one, so the original creator keeps Omni's grant and a later deployer receives an explicit grant through `OMNI_CONSOLE_PRINCIPAL_ARN` or the admission block. The instance role's `code-editor-space-admin` row is the same as here.

![Manage permissions in the workshop space: the four principals, their highest permission and their grants, with Add grant](/static/img/module-5/omni-manage-permissions.png)

Two properties of this model matter for a platform team. Grants only add, so a person's access is the union of every grant they hold, and you remove access by deleting a grant, never by adding one. And the space operator role is the ceiling: a Space Admin can do nothing the `CloudWatchOmniSpaceAccessRole-workshop` role cannot do.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `400` when opening the link | The code was already redeemed, or the 10 minutes passed | Mint a new code |
| `AccessDeniedException` on `create-one-time-deep-link-code` | The terminal is not running as the Code Editor instance role (which holds `code-editor-space-admin`): a Module 3b persona role is still exported and overrides it | `unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN` so the shell falls back to the instance role, then retry |
| *This IAM role isn't a member of this space* | The role or user your browser signed in with holds no grant on the space | In a Workshop Studio account `WSParticipantRole` gets its grant from `workshop-agentcore-stack`; confirm with step 1's verify block. In your own account run the **Admit another console identity** block under step 1's *Step 3* with `CONSOLE_PRINCIPAL_ARN` set to the identity your console uses, then sign in again |
| The deep link lands on the Omni login page | The URL was built on the `d-…` host from `domainEndpointUrl`; only the name-based host signs you in | Use `https://<domain-name>.cloudwatch-omni.global.app.aws` + the code path, as the block prints |

---

## Notebook walkthrough (optional alternative)

> This notebook (03-sign-in.ipynb) mints the deep link from Python and prints the URL. Follow *either* path.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker.
>
> Navigate to `source/module-5-omni/notebooks/` and open **`03-sign-in.ipynb`**.
