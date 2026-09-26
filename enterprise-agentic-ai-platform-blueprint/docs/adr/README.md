# Architecture Decision Records

MADR-lite records. ADR-0001 and ADR-0003 through ADR-0015 are currently summarised in
[`README.md`](../../README.md) §14 with full text in Git history; only the records below have been
relocated into this directory so far.

| ADR | Title | Status |
|---|---|---|
| ADR-0001 | LiteLLM in the inference path (deviation D-01) | Superseded by ADR-0016. Summary in README §14 |
| [ADR-0002](./ADR-0002-cdk-not-terraform.md) | Infrastructure as code authored in AWS CDK, not Terraform (was deviation D-02) | Accepted |
| ADR-0003 – ADR-0015 | See [`README.md`](../../README.md) §14 | Accepted |
| [ADR-0016](./ADR-0016-agentcore-gateway-inference-supersedes-litellm-proxy.md) | AgentCore Gateway inference targets supersede the self-managed LiteLLM proxy | Accepted; live release gates open |

Target-state architecture: [`docs/architecture/target-state-architecture.md`](../architecture/target-state-architecture.md).
