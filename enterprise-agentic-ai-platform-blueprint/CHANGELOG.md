# Changelog

All notable public changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [Unreleased]

### Added

- Multi-account AWS CDK reference architecture for Management/Governance, Platform, and Workstream account roles.
- Native Amazon Bedrock AgentCore inference Gateway with Cognito M2M authentication and a Bedrock Mantle target.
- Workstream-owned AgentCore Tool Gateway using AWS_IAM authentication and Registry-approved tool subscriptions.
- Generated Strands agent using `LiteLLMModel` for inference and `MCPClient` for tools.
- AgentCore Runtime, Memory, Identity credential-provider, and workload-identity integration.
- AWS Agent Registry producer and consumer flow with approved governance descriptors and versioned SSM discovery parameters.
- Mandatory inference request interceptor that applies the stage Bedrock Guardrail before model invocation.
- Digest-bound ECR image scan gate that blocks Critical and High findings before Runtime creation.
- Platform and Workload CDK pipelines with nonproduction deployment, deployed-runtime evaluation, approval, and production promotion.
- Scoped Platform, Workstream, and Management CloudFormation execution-policy generator.
- CloudWatch OAM links from Platform and each distinct Workstream account/Region to the Management sink.
- Deployment-continuity, adversarial, load, rate-limit, guardrail, Registry, Runtime, and teardown probes.
- Fail-closed dependency-ordered teardown and direct residual-resource inventory across CloudFormation, AgentCore, IAM, Logs, KMS, S3, Registry, Cognito, DynamoDB, and pipeline surfaces.
- Ireland (`eu-west-1`) support envelope and AgentCore-compatible availability-zone ID mapping.

### Changed

- The supported generated-agent path uses the two-Gateway architecture: a Platform inference Gateway and a Workstream Tool Gateway.
- AWS Agent Registry is the tool-governance source of truth; developer configuration stores stable tool IDs rather than environment-specific record IDs.
- Runtime evaluation invokes the deployed AgentCore Runtime instead of calling Bedrock directly.
- Evaluation uses measured first-token latency rather than total model-call duration.
- Native Gateway rate limiting is documented as approximate, fail-open traffic shaping rather than an authorization or hard-quota control.
- Region selection is explicit and fail-closed across CDK entry points and live utilities.
- OAM sink policy supports both Organizations-scoped trust and an independent explicit-account path for standalone validation accounts.
- Platform alias grants must be retired before Workstream role deletion to prevent stale role-principal IDs.

### Fixed

- Guardrail intervention responses are preserved as terminal agent outcomes; unrelated 401, 403, and 5xx failures still propagate.
- Evaluation role authorizes both the Runtime ARN and its `DEFAULT` Runtime endpoint ARN.
- Pipeline evaluation enters the blueprint source directory in both monorepo and standalone checkouts.
- Workstream OAM deployment includes exact source-link lifecycle and dependent `xray:Link` permissions without granting sink administration.
- Runtime Region selection no longer falls back silently to a US Region.
- Workload Registry validation uses the Regional STS endpoint and the live Agent Registry `api.aws` endpoint.
- Tool Gateway deletion waits for asynchronous target deletion before deleting the Gateway.
- Runtime/Memory teardown removes managed credential-provider secrets and preserves dependency order.
- Final inventory includes alias-less KMS keys and service-created log groups.

### Verified

The complete supported path has been exercised in `eu-west-1` through:

- strict synthesis and clean cdk-nag results;
- scoped IAM policy validation and positive/negative simulations;
- Platform and Workload pipelines through production;
- authorized and adversarial Runtime/Gateway calls;
- exact Guardrail and rate-limit outcomes;
- Runtime update cancellation, rollback, restore, and production transition with no failed sampled session;
- deployed-runtime evaluation;
- centralized OAM Logs/Metrics visibility and Gateway request-to-span correlation;
- grant retirement, dependency-ordered teardown, and independent zero-live-residue inventories.

## [1.0.0]

### Added

- Initial public reference architecture and reusable CDK construct packages.
