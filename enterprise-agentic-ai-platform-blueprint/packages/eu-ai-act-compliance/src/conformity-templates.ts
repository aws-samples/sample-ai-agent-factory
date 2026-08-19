/**
 * Conformity assessment Markdown templates.
 *
 * Pure functions. Generate the three documents the EU AI Act expects to be
 * available at any time for high-risk systems:
 *   1. technical-documentation.md (Article 11 + Annex IV)
 *   2. risk-assessment.md          (Article 9)
 *   3. human-oversight-protocol.md (Article 14)
 *
 * Returned strings are base inputs to a Lambda that uploads them to the
 * Object-Lock COMPLIANCE record-keeping bucket post-deploy. Templates are
 * intentionally simple and readable: customer reviewers can amend the
 * generated Markdown in place if their counsel requires nuance.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import type { RiskClass } from './risk-classification';
import { RISK_CLASS_ARTICLES } from './risk-classification';

export interface ConformityInputs {
  readonly tenantId: string;
  readonly agentId: string;
  readonly envName: string;
  readonly blueprintId: string;
  readonly riskClass: RiskClass;
  readonly providerName: string;
  readonly contactEmail: string;
  readonly modelIds: readonly string[];
  readonly humanOversightContact: string;
  readonly emittedAt: string;          // ISO-8601
  readonly platformVersion: string;     // e.g. v0.4.0
}

function header(title: string, inputs: ConformityInputs): string {
  return `# ${title}

| field | value |
|---|---|
| Tenant | \`${inputs.tenantId}\` |
| Agent | \`${inputs.agentId}\` |
| Environment | \`${inputs.envName}\` |
| Blueprint | \`${inputs.blueprintId}\` |
| Risk class | **${inputs.riskClass}** |
| Provider | ${inputs.providerName} |
| Provider contact | ${inputs.contactEmail} |
| Platform version | ${inputs.platformVersion} |
| Emitted at | ${inputs.emittedAt} |
| Models | ${inputs.modelIds.join(', ')} |
`;
}

export function technicalDocumentation(inputs: ConformityInputs): string {
  return `${header('Technical Documentation (EU AI Act Article 11 + Annex IV)', inputs)}

## 1. General description

Agent \`${inputs.tenantId}/${inputs.agentId}\` is deployed on the AWS Enterprise Agentic AI Platform (\`${inputs.platformVersion}\`). The blueprint pattern is \`${inputs.blueprintId}\`. Its risk class under Article 6 is **${inputs.riskClass}**.

Articles directly applicable: ${RISK_CLASS_ARTICLES[inputs.riskClass].map((a) => `Article ${a}`).join(', ')}.

## 2. Detailed description of the elements of the AI system

- **Foundation models**: ${inputs.modelIds.join(', ')}.
- **Inference path**: requests pass through Bedrock Guardrails (input + output filters), are gated by Cedar micro-policies on the AgentCore Gateway, and are tagged for cost attribution per the platform-baselines SSOT.
- **Memory**: per-tenant CMK-encrypted AgentCore Memory namespaces; TTL configurable per agent.
- **Identity**: AgentCore Identity OAuth2 + Cognito; M2M flows scoped via SCP-09 / SCP-10.

## 3. Monitoring, functioning and control

Online evaluation watchdog (Phase B) continuously samples production traffic and alarms on regression vs. golden baseline. Evaluation gates (Phase A) block CI/CD promotion on threshold breach.

## 4. Risk management

See \`risk-assessment.md\` (Article 9). All material decisions are logged into the immutable Object-Lock COMPLIANCE 7-year record-keeping bucket.

## 5. Logging

Every inference is logged via Bedrock Invocation Logging to the platform Audit account. Logs are CMK-encrypted, retained 7 years, and Object-Locked GOVERNANCE.
`;
}

export function riskAssessment(inputs: ConformityInputs): string {
  return `${header('Risk Assessment (EU AI Act Article 9)', inputs)}

## 1. Identified risks

| Risk | Mitigation |
|---|---|
| Hallucination — fabricated factual claims | Bedrock Guardrails grounding + judge-model regression alarm (Phase B) |
| Prompt injection | Bedrock Guardrails prompt-attack detection + Cedar tool deny-by-default (Phase E) |
| Tool misuse | Per-tool Cedar policies; SCP-10 deny on non-catalogued Lambda invokes |
| Cost runaway | Per-tenant Application Inference Profiles + budgets + Phase G chargeback |
| Service degradation | Phase F circuit breaker + fallback chain |
| Persistent stale state | Memory TTL + namespace isolation |
| Unauthorised access | OAuth2 + Cognito + AgentCore Identity + WAF on API Gateway |

## 2. Residual risks

Documented as a registered security exception per platform deployment.

## 3. Trigger to re-assess

- Bump of foundation-model major version
- New tool added to the platform tool catalogue
- Online-evaluation regression alarm fired and acknowledged
- Quarterly review by ${inputs.providerName} (operations team)
`;
}

export function humanOversightProtocol(inputs: ConformityInputs): string {
  return `${header('Human Oversight Protocol (EU AI Act Article 14)', inputs)}

## 1. Roles

- **Human-in-the-loop approver**: ${inputs.humanOversightContact}
- **Escalation owner**: Same — receives SQS messages from the platform HITL pattern (Phase H).

## 2. Triggers

- Confidence score below tenant-configured threshold (default 0.7).
- Agent decision tagged \`high-risk\` by the per-tool Cedar policy.
- Tenant-administrator manual override (kill-switch — Phase F).

## 3. Resume / abort

The HITL Step Function pauses execution and emits SNS to ${inputs.humanOversightContact}. The approver resumes via the \`ResumeTaskWithApproval\` API; abort routes the agent to a documented graceful-decline response.

## 4. Audit trail

Every pause / resume / abort writes a record to the EU AI Act compliance DDB and to the immutable record-keeping S3 bucket.
`;
}
