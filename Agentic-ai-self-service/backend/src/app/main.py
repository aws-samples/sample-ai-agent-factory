"""FastAPI application entry point.

Requirements: 3.3, 4.2, 6.1
"""

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import workflows_router
from app.routers.flows import router as flows_router
from app.routers.git_sync import router as git_sync_router
from app.routers.observability import router as observability_router
from app.routers.workspaces import router as workspaces_router

# routers/deployment.py and routers/tools.py used to mount /api/deploy + tool
# routes here, but API Gateway routes those endpoints directly to the
# Deployment Lambda (deployment_handler.py). The router files were dead code
# (see tasks/lessons.md Bug 31) and have been deleted.
from app.services.config import load_config
from app.services.dynamodb_storage import DynamoDBWorkflowStorage
from app.services.flow_storage import DynamoDBFlowStorage, set_flow_storage
from app.services.storage import set_workflow_storage

logger = logging.getLogger(__name__)

# Load environment variables (for local development)
load_dotenv()

# Load application config from SSM or environment variables
config = load_config()

# Audit issue #6: silent in-memory fallback in Lambda would silently lose
# writes between cold-start invocations. When running inside Lambda
# (AWS_LAMBDA_FUNCTION_NAME is set by the runtime), require DynamoDB env
# vars and fail fast if they are missing. Local dev (no AWS_LAMBDA_FUNCTION_NAME)
# keeps the in-memory fallback so the FastAPI app still works without AWS.
_IS_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

# Select storage backend based on config
if config.dynamodb_table_name:
    logger.info(
        "Using DynamoDB storage: table=%s, region=%s",
        config.dynamodb_table_name,
        config.aws_region,
    )
    set_workflow_storage(
        DynamoDBWorkflowStorage(
            table_name=config.dynamodb_table_name,
            region=config.aws_region,
        )
    )
elif _IS_LAMBDA:
    raise RuntimeError("Storage misconfigured: DYNAMODB_TABLE_NAME unset in Lambda environment")
else:
    logger.info("Local mode: using in-memory workflow storage (no DYNAMODB_TABLE_NAME set)")

# Select flow storage backend based on config
if config.dynamodb_flows_table_name:
    logger.info(
        "Using DynamoDB flow storage: table=%s, region=%s",
        config.dynamodb_flows_table_name,
        config.aws_region,
    )
    set_flow_storage(
        DynamoDBFlowStorage(
            table_name=config.dynamodb_flows_table_name,
            region=config.aws_region,
        )
    )
elif _IS_LAMBDA:
    raise RuntimeError("Storage misconfigured: DYNAMODB_FLOWS_TABLE_NAME unset in Lambda environment")
else:
    logger.info("Local mode: using in-memory flow storage (no DYNAMODB_FLOWS_TABLE_NAME set)")

app = FastAPI(
    title="AgentCore Workflow Platform API",
    description="Backend API for visual workflow design and AWS AgentCore deployment",
    version="0.1.0",
)

# Configure CORS from config
# SECURITY: Restrict allowed methods and headers to what the API actually uses
# instead of wildcard "*" to reduce attack surface.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Amz-Date", "X-Api-Key"],
)

# Include routers
app.include_router(workflows_router)
app.include_router(flows_router, prefix="/api", tags=["flows"])
## deployment_router and tools_router were mounted here previously — see comment
## near imports above. The Deployment Lambda owns those endpoints now.
app.include_router(observability_router, prefix="/api", tags=["observability"])
# Phase 2 Gap 2E — workspace sharing + RBAC. Mounted on the workflow Lambda
# because it reads/writes workflow storage. Router carries its own /api prefix.
app.include_router(workspaces_router)
app.include_router(git_sync_router)  # Gap 3D GitOps - /api/workflows/{id}/git-sync + /git-token


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}
