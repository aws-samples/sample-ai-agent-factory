"""Knowledge Base RAG query Lambda (DEPLOYED-CODE TEMPLATE, not app code).

Queries a Bedrock Knowledge Base via retrieve_and_generate and returns a
RETRYABLE ``still_ingesting`` signal on zero citations (P-E2E fix) so callers
retry during post-deploy ingestion instead of reporting a hard failure.
Deployed by gateway_deployer and embedded inline in exported CFN templates.
"""

import json
import os

import boto3

bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def lambda_handler(event, context):
    query = event.get("query", "")
    kb_id = os.environ["KNOWLEDGE_BASE_ID"]
    model_arn = os.environ["FOUNDATION_MODEL_ARN"]

    try:
        resp = bedrock_runtime.retrieve_and_generate(
            input={"text": query},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": kb_id,
                    "modelArn": model_arn,
                },
            },
        )
        answer = resp.get("output", {}).get("text", "No answer found.")
        citations = []
        for c in resp.get("citations", [])[:5]:
            refs = c.get("retrievedReferences", [])
            for ref in refs[:2]:
                loc = ref.get("location", {})
                citations.append(
                    {
                        "text": ref.get("content", {}).get("text", "")[:200],
                        "source": loc.get("s3Location", {}).get("uri", "") or loc.get("webLocation", {}).get("url", ""),
                    }
                )
        # No citations => the KB retrieved nothing. Right after deploy this almost
        # always means ingestion has not yet produced queryable vectors (eventual
        # consistency), not that the fact is absent. Return a RETRYABLE signal so
        # the agent/caller retries instead of reporting a hard failure (P-E2E fix).
        if not citations:
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "answer": answer,
                        "citations": [],
                        "still_ingesting": True,
                        "retryable": True,
                        "message": "Knowledge base returned no matches yet — it may still be ingesting. Retry shortly.",
                    }
                ),
            }
        return {"statusCode": 200, "body": json.dumps({"answer": answer, "citations": citations})}
    except Exception as e:
        # Distinguish an ingestion-in-progress error from a real failure so the
        # caller can retry the former.
        msg = str(e)
        retryable = any(s in msg for s in ("still", "ingest", "no data", "empty", "ResourceNotReady"))
        return {"statusCode": 200, "body": json.dumps({"error": msg, "retryable": retryable})}
