"""Platform Lambda OTEL prologue — runs at import time.

Each platform Lambda handler module imports this FIRST so OTEL telemetry is
initialized before any other code runs.

Why this is manual and not ADOT-based: the AWS-managed ADOT Python Lambda
layer's exec wrapper (`/opt/otel-instrument`) calls `__import__()` on the
configured handler string, which fails for the slash-form handler paths used
by this stack (`src/app/lambda_handler.handler`). The ADOT layer also bundles
an older `typing_extensions` that shadows `/var/task/lib/typing_extensions/`
and breaks `pydantic_core`. Both issues were observed live on 2026-05-15
during platform-OTEL verification — see tasks/lessons.md Bug 22.

Manual setup avoids both issues by:
  1. Resolving OTEL_AUTH_SECRET_ARN to the OTLP auth header.
  2. Building a TracerProvider with an OTLP HTTP exporter directly.
  3. Auto-instrumenting boto3 via opentelemetry-instrumentation-botocore.

Resilient: any failure logs a warning and lets the Lambda run without traces.
Never raises.
"""

import logging
import os

logger = logging.getLogger("agentcore.otel.platform")

_provider = None  # set after successful bootstrap; used by force_flush helper


def _resolve_auth_header() -> None:
    """Read OTEL_AUTH_SECRET_ARN, write resolved value to OTEL_EXPORTER_OTLP_HEADERS."""
    secret_arn = os.environ.get("OTEL_AUTH_SECRET_ARN", "").strip()
    if not secret_arn:
        return
    if os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"):
        return  # already set by another bootstrap path; don't overwrite
    try:
        import boto3

        sm = boto3.client("secretsmanager")
        value = sm.get_secret_value(SecretId=secret_arn).get("SecretString", "").strip()
        if value:
            os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = value
    except Exception as e:
        logger.warning("Could not resolve OTEL auth secret: %s", e)


def _setup_tracer_provider() -> None:
    """Build a TracerProvider with an OTLP HTTP exporter and instrument boto3.

    Honors the standard OTEL_* env vars set by CDK:
      OTEL_EXPORTER_OTLP_ENDPOINT
      OTEL_EXPORTER_OTLP_HEADERS (set by _resolve_auth_header above)
      OTEL_SERVICE_NAME
      OTEL_RESOURCE_ATTRIBUTES
      OTEL_TRACES_SAMPLER / OTEL_TRACES_SAMPLER_ARG
    """
    global _provider
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        logger.warning(
            "OpenTelemetry SDK not installed in this Lambda layer; "
            "platform OTEL will be skipped. Add opentelemetry-sdk and "
            "opentelemetry-exporter-otlp-proto-http to requirements-lambda.txt. "
            "Detail: %s",
            e,
        )
        return

    try:
        # SDK auto-builds the resource from OTEL_SERVICE_NAME +
        # OTEL_RESOURCE_ATTRIBUTES env vars when we call Resource.create().
        resource = Resource.create({SERVICE_NAME: os.environ.get("OTEL_SERVICE_NAME", "agentcore-platform")})
        provider = TracerProvider(resource=resource)
        # OTLPSpanExporter reads endpoint + headers from env vars by default.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _provider = provider

        # Auto-instrument boto3 client calls so every AWS SDK call shows up
        # as a span. Optional — soft-fail if the package isn't bundled.
        try:
            from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

            BotocoreInstrumentor().instrument()
        except ImportError:
            logger.info("botocore instrumentor not bundled; AWS SDK calls won't auto-span.")

        logger.warning(
            "Platform OTEL bootstrap complete (endpoint=%s, service=%s).",
            endpoint,
            os.environ.get("OTEL_SERVICE_NAME", "?"),
        )
    except Exception as e:
        logger.warning("Platform OTEL bootstrap failed (continuing without traces): %s", e)


def force_flush() -> None:
    """Flush pending spans. Call from a Lambda handler's exception path or
    request-end hook so spans land before the runtime is suspended.
    """
    global _provider
    if _provider is None:
        return
    try:
        _provider.force_flush(timeout_millis=3000)
    except Exception as e:
        logger.debug("OTEL flush failed: %s", e)


_resolve_auth_header()
_setup_tracer_provider()
