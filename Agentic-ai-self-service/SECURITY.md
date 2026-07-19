# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, please **do not**
create a public GitHub issue.

Instead, notify AWS/Amazon Security via our
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/)
or email [aws-security@amazon.com](mailto:aws-security@amazon.com) directly.

## Supported Versions

This is an AWS sample. The `main` branch always contains the latest supported
code; older tags are provided for reference and do not receive security fixes.

## Security Posture (what this sample already does)

- **Credentials** are stored only in AWS Secrets Manager — never in DynamoDB,
  logs, or generated code. Secret values are write-only through the API.
- **Tenant isolation**: every resource is scoped to the caller's Cognito
  subject; cross-tenant reads return 404 (not 403) to avoid existence leaks.
- **SSRF guards** on all user-supplied URLs (external MCP endpoints, OpenAPI
  specs) with private-range and metadata-endpoint blocking.
- **Pre-commit scanning**: `detect-secrets` with a checked-in baseline, plus
  private-key and merge-conflict checks (`.pre-commit-config.yaml`).
- **Infrastructure gating**: `cdk-nag` (AWS Solutions rule pack) runs at synth
  time; every suppression is per-construct and justified in code.
- **Log hygiene**: taint-aware scrubbing keeps secret-bearing payloads out of
  CloudWatch logs.

Run the checks locally:

```bash
pip install pre-commit && pre-commit install
pre-commit run --all-files
```
