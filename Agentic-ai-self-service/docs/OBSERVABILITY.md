# Observability

How platform Lambdas and deployed agents export OTLP traces — per-canvas and platform-level modes, plus the platform-OTEL deploy path.

[← Back to README](../README.md)

The platform supports two OTEL deployment modes — they are not mutually exclusive.

**Per-canvas (the original mode).** Drop an Observability node onto the canvas, configure the OTLP endpoint + credentials in the modal, and the agent's traces are exported to that backend.

**Platform-level (added later).** Configure once at deploy time via the `OTEL_*` env vars (see [Deploying with platform-level OTEL](#deploying-with-platform-level-otel) below). Every deployed agent inherits the configuration automatically, AND every platform Lambda (workflow Lambda, deployment Lambda, all Step Functions step handlers) emits OTLP spans to the same backend. When a deploy is stuck, the stuck step Lambda shows up as a span in your backend alongside the agent invocations it's trying to set up.

Platform-level config takes precedence over per-canvas: the endpoint, secret ARN, sample rate, and service-name prefix are admin-locked. Per-canvas Observability nodes can still add resource attributes additively (e.g. `team=ops`), but cannot override the endpoint.

## Implementation

- `backend/src/app/services/_otel_platform.py` — module-load OTel SDK setup imported as the first import of every Lambda handler. Resolves `OTEL_AUTH_SECRET_ARN` from Secrets Manager into `OTEL_EXPORTER_OTLP_HEADERS`, sets up `BatchSpanProcessor` + `OTLPSpanExporter` (HTTP), instruments boto3.
- `backend/src/app/services/observability.py::get_platform_observability_defaults()` — reads SSM at module load, cached via `lru_cache`.
- `backend/src/app/services/code_generator.py::_inject_otel()` — post-processes generated agent code to bootstrap Strands `StrandsTelemetry` + force-flush spans on shutdown.

Verified live against Langfuse Cloud — both platform Lambda traces and deployed-agent traces appear under the same project.

## Deploying with platform-level OTEL

Use this when you want **every deployed agent AND every platform Lambda** (workflow, deployment, all 13 Step Functions step handlers) to export OTLP traces to a single backend automatically. The endpoint becomes admin-locked — per-canvas Observability nodes can still add resource attributes (e.g. `team=ops`) but cannot override the endpoint.

**Prerequisite (one-time)**: create the auth-header secret with your OTLP backend credentials. The example below uses Langfuse Cloud; any OTLP-compatible backend with HTTP Basic auth works.

```bash
LANGFUSE_PUBLIC_KEY=pk-... LANGFUSE_SECRET_KEY=sk-... ./scripts/bootstrap-otel-secret.sh
# Prints the secret ARN — copy it for the next command.
```

**Deploy with platform OTEL enabled**:

```bash
COGNITO_USERS="user@example.com" \
  OTEL_ENDPOINT="https://cloud.langfuse.com/api/public/otel" \
  OTEL_AUTH_SECRET_ARN="arn:aws:secretsmanager:us-east-1:...:secret:agentcore-otel/platform/dev-XXXXXX" \
  OTEL_SAMPLE_RATE=1.0 \
  ./scripts/deploy.sh
```

To switch an existing deployment between OTEL modes, just re-run `./scripts/deploy.sh` with the new env vars set/unset. CDK reconciles the change in place; no resource churn.

### OTEL configuration variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_ENDPOINT` | *(unset)* | OTLP HTTP endpoint for platform-level observability (e.g. `https://cloud.langfuse.com/api/public/otel`). When set, every platform Lambda + every deployed agent exports traces here. |
| `OTEL_AUTH_SECRET_ARN` | *(unset)* | ARN of a Secrets Manager secret holding the precomputed `Authorization` header value (e.g. `Basic <base64>`). Created by `scripts/bootstrap-otel-secret.sh`. Required when `OTEL_ENDPOINT` is set. |
| `OTEL_SAMPLE_RATE` | `1.0` | Trace sampling ratio (0.0–1.0). |
| `OTEL_SERVICE_NAME_PREFIX` | `{PROJECT_NAME}` | Prefix prepended to `service.name` resource attribute on every span. |
