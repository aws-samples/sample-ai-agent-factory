"""Step handler: Create or validate a Bedrock Knowledge Base.

Handles two modes:
- existing: Validates the KB exists and returns its ID
- create_new: Creates KB + data source + starts ingestion
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import json
import logging
import os
import time

from botocore.exceptions import ClientError

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.aws_errors import error_code
from app.services.deployment_state_store import DeploymentStateStore

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _build_model_arn(region: str, model_id: str) -> str:
    """Build a Bedrock foundation model ARN."""
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"


def _get_account_id(event: dict) -> str:
    """Resolve the current AWS account id (for constructing ARNs)."""
    try:
        return step_clients.account_id_for_event(event)
    except Exception:  # noqa: BLE001
        return ""


def _create_kb_role(iam_client, role_name: str, kb_config: dict) -> str:
    """Create an IAM role for the Knowledge Base with required permissions."""
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    try:
        resp = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Role for Bedrock Knowledge Base created by AgentCore Flow",
        )
        role_arn = resp["Role"]["Arn"]
    except iam_client.exceptions.EntityAlreadyExistsException:
        role_arn = iam_client.get_role(RoleName=role_name)["Role"]["Arn"]

    _put_kb_role_policy(iam_client, role_name, kb_config)

    # IAM eventual consistency
    time.sleep(10)
    return role_arn


def _put_kb_role_policy(iam_client, role_name: str, kb_config: dict) -> None:
    """Build + put the KB role's inline policy from *kb_config*.

    Separated from ``_create_kb_role`` so the handler can RE-put the policy
    after auto-provisioning the vector store (S3 Vectors bucket / OSS
    collection): the resource ARNs are unknowable before creation, so the
    first put uses the tightest naming-convention pattern and the re-put
    tightens each statement to the exact resource ARN (least privilege).
    """
    statements: list[dict] = [
        {
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel", "bedrock:ListFoundationModels"],
            "Resource": "*",
        },
    ]

    data_source_type = kb_config.get("dataSourceType", "s3")
    vector_store_type = kb_config.get("vectorStoreType", "s3_vectors")

    # S3 data source permissions
    if data_source_type == "s3":
        s3_uri = kb_config.get("s3BucketUri", "")
        if s3_uri:
            bucket_arn = _parse_s3_bucket_arn(s3_uri)
            statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:ListBucket"],
                    "Resource": [bucket_arn, f"{bucket_arn}/*"],
                }
            )

    # Credential-based data sources need Secrets Manager access
    secret_arns = []
    if data_source_type == "confluence":
        secret_arns.append(kb_config.get("confluenceCredentialsSecretArn", ""))
    elif data_source_type == "salesforce":
        secret_arns.append(kb_config.get("salesforceCredentialsSecretArn", ""))
    elif data_source_type == "sharepoint":
        secret_arns.append(kb_config.get("sharePointCredentialsSecretArn", ""))

    # OpenSearch Serverless permissions. APIAccessAll is required for aoss
    # DATA-PLANE access (index reads/writes) — but the Resource is scoped to
    # the collection ARN. When the collection hasn't been created yet (the
    # auto-provision path runs AFTER role creation because the data-access
    # policy needs the role ARN), fall back to a collection/* pattern; the
    # handler re-puts this policy with the exact collection ARN once
    # _ensure_oss_collection returns it.
    if vector_store_type == "opensearch_serverless":
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                "Resource": kb_config.get("opensearchCollectionArn") or "arn:aws:aoss:*:*:collection/*",
            }
        )

    # S3 Vectors permissions (Bug 78). The role needs to provision and
    # interact with the auto-managed vector bucket+index that Bedrock KB
    # creates on its behalf. Without these, Bedrock validates the role,
    # finds it can't create/describe the s3vectors resources, and rejects
    # the KB with `ValidationException: Bedrock Knowledge Base was unable
    # to assume the given role`.
    if vector_store_type == "s3_vectors":
        s3v_arn = kb_config.get("s3VectorsBucketArn", "")
        # Some s3vectors verbs (QueryVectors, PutVectors, GetVectors,
        # DeleteVectors, DescribeIndex, ListIndexes) target the
        # `<bucket>/index/<idx>` sub-resource, not just the bucket.
        # Granting only the bucket ARN surfaces as a misleading
        # "Bedrock KB was unable to assume the given role" error.
        # See tasks/lessons.md Bug 84.
        if not s3v_arn:
            # Auto-managed path: the handler creates the bucket AFTER this
            # role exists (agentcore-kbvec-<deployment_id> naming convention,
            # see the handler) and then RE-puts this policy with the exact
            # bucket ARN. Scope the interim grant to the naming-convention
            # prefix instead of "*".
            s3v_resources = [
                "arn:aws:s3vectors:*:*:bucket/agentcore-kbvec-*",
                "arn:aws:s3vectors:*:*:bucket/agentcore-kbvec-*/index/*",
            ]
        else:
            s3v_resources = [s3v_arn, f"{s3v_arn}/index/*"]
        statements.append(
            {
                "Effect": "Allow",
                "Action": [
                    "s3vectors:CreateVectorBucket",
                    "s3vectors:CreateIndex",
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:ListVectors",
                    "s3vectors:QueryVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:DescribeVectorBucket",
                    "s3vectors:DescribeIndex",
                    "s3vectors:GetIndex",
                    "s3vectors:GetVectorBucket",
                    "s3vectors:GetVectorBucketPolicy",
                    "s3vectors:PutVectorBucketPolicy",
                    "s3vectors:ListIndexes",
                ],
                "Resource": s3v_resources,
            }
        )
        # ListVectorBuckets is an account-level listing (no resource ARN
        # form) — isolated so the "*" is visible and minimal.
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["s3vectors:ListVectorBuckets"],
                "Resource": "*",
            }
        )

    # RDS permissions. The Aurora vector store CANNOT be auto-provisioned by
    # this step (the cluster must pre-exist and _build_storage_config wires
    # kb_config["rdsResourceArn"] straight into the Bedrock storage config),
    # so the ARN is always knowable — a missing value is a caller error, not
    # a reason to grant rds-data on "*".
    if vector_store_type == "rds":
        rds_resource_arn = kb_config.get("rdsResourceArn", "")
        if not rds_resource_arn:
            raise ValueError(
                "rdsResourceArn is required when vectorStoreType='rds' — the Aurora "
                "cluster must pre-exist and its ARN is used both in the KB storage "
                "configuration and to scope the KB role's rds-data permissions."
            )
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement"],
                "Resource": rds_resource_arn,
            }
        )
        rds_secret = kb_config.get("rdsCredentialsSecretArn", "")
        if rds_secret:
            secret_arns.append(rds_secret)

    # Custom transformation Lambda permissions
    transform_lambda = kb_config.get("transformationLambdaArn", "")
    if transform_lambda:
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction"],
                "Resource": transform_lambda,
            }
        )

    # BDA parsing writes intermediate output to the supplemental-storage
    # bucket configured on the KB (supplementalDataStorageConfiguration) —
    # without this grant CreateKnowledgeBase fails on role validation
    # (matrix-run finding, P-KB-013 third-stage error).
    if kb_config.get("parsingStrategy") == "bedrock_data_automation":
        supp_uri = kb_config.get("bdaSupplementalS3Uri") or f"s3://{os.environ.get('ARTIFACTS_BUCKET_NAME', '')}"
        if supp_uri.startswith("s3://"):
            supp_bucket = supp_uri[5:].split("/")[0]
            if supp_bucket:
                supp_arn = f"arn:aws:s3:::{supp_bucket}"
                statements.append(
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                        "Resource": [supp_arn, f"{supp_arn}/*"],
                    }
                )
        # BDA parsing also invokes Bedrock Data Automation on the KB's behalf.
        statements.append(
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeDataAutomationAsync",
                    "bedrock:GetDataAutomationStatus",
                ],
                "Resource": "*",
            }
        )

    # S3 access for transformation intermediate storage
    transform_s3 = kb_config.get("transformationS3Uri", "")
    if transform_s3 and transform_s3.startswith("s3://"):
        t_bucket = transform_s3[5:].split("/")[0]
        t_bucket_arn = f"arn:aws:s3:::{t_bucket}"
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                "Resource": [t_bucket_arn, f"{t_bucket_arn}/*"],
            }
        )

    # Consolidate Secrets Manager permissions
    valid_secrets = [s for s in secret_arns if s]
    if valid_secrets:
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": valid_secrets if len(valid_secrets) > 1 else valid_secrets[0],
            }
        )

    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName="BedrockKBAccess",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}),
    )


def _wait_for_kb_active(bedrock_agent, kb_id: str, max_wait: int = 120) -> None:
    """Poll until KB status is ACTIVE."""
    for _ in range(max_wait // 5):
        resp = bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
        status = resp.get("knowledgeBase", {}).get("status", "")
        if status == "ACTIVE":
            return
        if status in ("FAILED", "DELETE_IN_PROGRESS"):
            raise RuntimeError(f"Knowledge Base {kb_id} is in state: {status}")
        time.sleep(5)
    raise TimeoutError(f"Knowledge Base {kb_id} did not become ACTIVE within {max_wait}s")


def _start_and_wait_ingestion(bedrock_agent, kb_id: str, ds_id: str, max_wait: int = 600) -> tuple[str, str]:
    """Start a data ingestion job and poll until complete or timeout.

    Returns ``(job_id, terminal_status)`` where terminal_status is one of
    ``COMPLETE`` (KB is queryable) or ``IN_PROGRESS`` (still ingesting — the KB
    exists but a query may return nothing yet). ``FAILED`` raises.

    Why this matters (P-E2E matrix finding): the KB used to be reported as part
    of a ``succeeded`` deploy the moment the job STARTED, even if vectors weren't
    queryable yet. Under a combined deploy (Runtime+Gateway+Memory+KB) the extra
    contention meant the corpus hadn't produced queryable vectors within the old
    300s window, so the KB tool returned nothing and the agent said "technical
    error". We now (a) wait longer by default and (b) return the real terminal
    status so the deploy result can tell the caller the KB is still ingesting
    instead of silently implying it's ready.
    """
    try:
        resp = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
        )
        job_id = resp["ingestionJob"]["ingestionJobId"]
        logger.warning("Ingestion job started: %s for KB %s", job_id, kb_id)
    except ClientError as start_err:
        # A Step Functions retry (or the idempotent data-source recovery) can
        # find a job already ongoing — adopt it instead of failing the deploy
        # (matrix-run finding, P-KB-008 secondary bug).
        if error_code(start_err) != "ConflictException":
            raise
        jobs = bedrock_agent.list_ingestion_jobs(knowledgeBaseId=kb_id, dataSourceId=ds_id, maxResults=5).get(
            "ingestionJobSummaries", []
        )
        ongoing = next((j for j in jobs if j.get("status") in ("STARTING", "IN_PROGRESS")), None)
        if not ongoing:
            raise
        job_id = ongoing["ingestionJobId"]
        logger.warning("Ingestion job %s already ongoing for KB %s, adopting", job_id, kb_id)

    for _ in range(max_wait // 5):
        job_resp = bedrock_agent.get_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
            ingestionJobId=job_id,
        )
        status = job_resp.get("ingestionJob", {}).get("status", "")
        if status == "COMPLETE":
            logger.warning("Ingestion job %s completed", job_id)
            return job_id, "COMPLETE"
        if status == "FAILED":
            failure = job_resp.get("ingestionJob", {}).get("failureReasons", [])
            raise RuntimeError(f"Ingestion job failed: {failure}")
        time.sleep(5)

    # Timeout is not fatal — ingestion continues in the background — but report
    # it so the deploy result / KB tool can surface "still ingesting" honestly.
    logger.warning("Ingestion job %s still running after %ds (continuing)", job_id, max_wait)
    return job_id, "IN_PROGRESS"


def _find_existing_kb(bedrock_agent, kb_name: str) -> str | None:
    """Check if a Knowledge Base with the given name already exists (idempotency guard)."""
    try:
        paginator = bedrock_agent.get_paginator("list_knowledge_bases")
        for page in paginator.paginate():
            for kb in page.get("knowledgeBaseSummaries", []):
                if kb.get("name") == kb_name and kb.get("status") in ("ACTIVE", "CREATING"):
                    return kb["knowledgeBaseId"]
    except Exception:
        logger.warning("Failed to list knowledge bases for idempotency check", exc_info=True)
    return None


def _ensure_oss_collection(
    region: str, deployment_id: str, kb_role_arn: str, kb_config: dict, store, dep_id: str, event: dict
) -> str:
    """Auto-provision an OpenSearch Serverless collection + vector index for a KB.

    Bedrock's CreateKnowledgeBase requires a PRE-EXISTING OSS collection ARN — unlike
    S3 Vectors there is no auto-provision from the storage config. So when the caller
    did not supply `opensearchCollectionArn`, we create the whole OSS stack here with
    pure boto3 (control-plane `aoss.create_index` — no data-plane SigV4 needed):
      1. encryption security policy (AWS-owned key)
      2. network security policy (public — matches the managed KB default)
      3. data-access policy (KB role + caller principal: full index/collection perms)
      4. the collection (type VECTORSEARCH), wait ACTIVE
      5. the vector index with the Bedrock-default field mapping (1024-dim knn cosine)
    Every created resource is recorded to the deployment manifest so teardown removes
    it (an OSS collection is a STANDING billable resource — leaving it orphaned costs
    ~$350/mo). Returns the collection ARN. Idempotent on SFN retry.
    """
    import botocore.exceptions

    aoss = step_clients.client(event, "opensearchserverless")
    # Names: 3-32 chars, lowercase alphanumeric + hyphen, must start with a letter.
    coll_name = ("kb" + deployment_id.replace("-", "").lower())[:32]
    idx_name = kb_config.get("opensearchVectorIndexName") or "bedrock-knowledge-base-default-index"
    vec_field = kb_config.get("opensearchVectorField") or "bedrock-knowledge-base-default-vector"
    txt_field = kb_config.get("opensearchTextField") or "AMAZON_BEDROCK_TEXT_CHUNK"
    meta_field = kb_config.get("opensearchMetadataField") or "AMAZON_BEDROCK_METADATA"
    kb_config["opensearchVectorIndexName"] = idx_name

    def _ignore_conflict(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except botocore.exceptions.ClientError as e:
            if (
                e.response.get("Error", {}).get("Code") in ("ConflictException", "ValidationException")
                and "exist" in str(e).lower()
            ):
                return None
            raise

    # 1+2. security policies (encryption + network), scoped to this collection.
    _ignore_conflict(
        aoss.create_security_policy,
        name=f"{coll_name}-enc"[:32],
        type="encryption",
        policy=json.dumps(
            {"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{coll_name}"]}], "AWSOwnedKey": True}
        ),
    )
    # ── SECURITY TRADE-OFF (deliberate, sample platform) ────────────────
    # The network security policy defaults to AllowFromPublic=True because
    # the platform's Lambdas are NOT VPC-attached (matches the managed
    # Bedrock-KB console default): with a private-only policy, neither the
    # deployment Lambda's create_index call nor Bedrock's ingestion could
    # reach the collection. Note the collection is still protected by the
    # aoss DATA-ACCESS policy below (only the KB role + caller principal can
    # touch it) — "public" here means network reachability, not open data.
    #
    # HARDENING: set kb_config["allowPublicNetwork"] = False and supply the
    # OpenSearch Serverless VPC endpoint ids via
    # kb_config["opensearchVpcEndpointIds"] (list). This requires the
    # calling Lambdas to run inside that VPC (VPC-attach the platform's
    # Lambda functions + create an aoss VPC endpoint) or KB creation will
    # hang/fail on network access.
    allow_public = kb_config.get("allowPublicNetwork", True)
    net_rule: dict = {
        "Rules": [
            {"ResourceType": "collection", "Resource": [f"collection/{coll_name}"]},
            {"ResourceType": "dashboard", "Resource": [f"collection/{coll_name}"]},
        ],
        "AllowFromPublic": bool(allow_public),
    }
    if not allow_public:
        vpce_ids = kb_config.get("opensearchVpcEndpointIds") or []
        if not vpce_ids:
            raise ValueError(
                "allowPublicNetwork=False requires opensearchVpcEndpointIds "
                "(the aoss VPC endpoint ids that should reach this collection)."
            )
        net_rule["SourceVPCEs"] = list(vpce_ids)
    _ignore_conflict(
        aoss.create_security_policy,
        name=f"{coll_name}-net"[:32],
        type="network",
        policy=json.dumps([net_rule]),
    )

    # 3. data-access policy: KB role + the caller (deployment Lambda) principal.
    caller_arn = ""
    try:
        caller_arn = step_clients.client(event, "sts").get_caller_identity()["Arn"]
        # normalise assumed-role ARN -> role ARN for the policy principal
        if ":assumed-role/" in caller_arn:
            _, _, tail = caller_arn.partition(":assumed-role/")
            role = tail.split("/")[0]
            caller_arn = f"arn:aws:iam::{_get_account_id(event)}:role/{role}"
    except Exception:  # noqa: BLE001 — optional principal; the KB role alone is sufficient
        logger.debug("Could not resolve caller ARN for OSS data-access policy", exc_info=True)
    principals = [p for p in [kb_role_arn, caller_arn] if p]
    _ignore_conflict(
        aoss.create_access_policy,
        name=f"{coll_name}-acc"[:32],
        type="data",
        policy=json.dumps(
            [
                {
                    "Rules": [
                        {
                            "ResourceType": "index",
                            "Resource": [f"index/{coll_name}/*"],
                            "Permission": [
                                "aoss:CreateIndex",
                                "aoss:DescribeIndex",
                                "aoss:ReadDocument",
                                "aoss:WriteDocument",
                                "aoss:UpdateIndex",
                                "aoss:DeleteIndex",
                            ],
                        },
                        {
                            "ResourceType": "collection",
                            "Resource": [f"collection/{coll_name}"],
                            "Permission": [
                                "aoss:CreateCollectionItems",
                                "aoss:DescribeCollectionItems",
                                "aoss:UpdateCollectionItems",
                            ],
                        },
                    ],
                    "Principal": principals,
                }
            ]
        ),
    )

    # 4. the collection.
    _ignore_conflict(
        aoss.create_collection,
        name=coll_name,
        type="VECTORSEARCH",
        description=f"AgentCore KB vector store for {deployment_id[:12]}",
    )
    if store is not None:
        store.record_resource(dep_id, {"type": "oss_collection", "name": coll_name, "region": region})

    # wait ACTIVE (up to ~5 min) + capture id/arn
    coll_id = coll_arn = ""
    for _ in range(60):
        summ = aoss.batch_get_collection(names=[coll_name]).get("collectionDetails", [])
        if summ:
            st = summ[0].get("status")
            if st == "ACTIVE":
                coll_id = summ[0]["id"]
                coll_arn = summ[0]["arn"]
                break
            if st == "FAILED":
                raise RuntimeError(f"OSS collection {coll_name} creation FAILED")
        time.sleep(5)
    if not coll_arn:
        raise RuntimeError(f"OSS collection {coll_name} not ACTIVE after timeout")

    # 5. the vector index (control-plane create_index; knn_vector 1024-dim).
    # NOTE: the knn method params are OpenSearch snake_case ("space_type", not
    # "spaceType") — the camelCase form fails "Invalid parameter: spaceType".
    index_schema = {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                vec_field: {
                    "type": "knn_vector",
                    "dimension": 1024,
                    "method": {"name": "hnsw", "engine": "faiss", "space_type": "l2"},
                },
                txt_field: {"type": "text"},
                meta_field: {"type": "text"},
            }
        },
    }
    # The data-access policy (step 3) is eventually-consistent: create_index can
    # race it and return AccessDenied "Access denied to create index" for the first
    # ~30-60s. Retry with backoff until the policy propagates.
    import botocore.exceptions as _bce

    created = False
    for attempt in range(12):
        try:
            aoss.create_index(id=coll_id, indexName=idx_name, indexSchema=index_schema)
            created = True
            break
        except _bce.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            msg = str(e).lower()
            if code == "ConflictException" or "exist" in msg:
                created = True
                break
            if "access denied" in msg or code == "AccessDeniedException":
                logger.warning("create_index access-denied (policy propagating, attempt %d/12) — retrying", attempt + 1)
                time.sleep(10)
                continue
            raise
    if not created:
        raise RuntimeError(f"OSS create_index for {idx_name} failed after retries (access policy did not propagate)")
    # brief settle so the index is queryable before CreateKnowledgeBase validates it
    time.sleep(30)
    kb_config["opensearchCollectionArn"] = coll_arn
    logger.warning("Auto-provisioned OSS collection %s (%s) + index %s", coll_name, coll_arn, idx_name)
    return coll_arn


def _build_storage_config(kb_config: dict) -> dict:
    """Build storage configuration based on vector store type."""
    vector_store_type = kb_config.get("vectorStoreType", "s3_vectors")

    if vector_store_type == "opensearch_serverless":
        return {
            "type": "OPENSEARCH_SERVERLESS",
            "opensearchServerlessConfiguration": {
                "collectionArn": kb_config.get("opensearchCollectionArn", ""),
                "vectorIndexName": kb_config.get("opensearchVectorIndexName", "bedrock-knowledge-base-default-index"),
                "fieldMapping": {
                    "vectorField": kb_config.get("opensearchVectorField", "bedrock-knowledge-base-default-vector"),
                    "textField": kb_config.get("opensearchTextField", "AMAZON_BEDROCK_TEXT_CHUNK"),
                    "metadataField": kb_config.get("opensearchMetadataField", "AMAZON_BEDROCK_METADATA"),
                },
            },
        }

    if vector_store_type == "rds":
        return {
            "type": "RDS",
            "rdsConfiguration": {
                "resourceArn": kb_config.get("rdsResourceArn", ""),
                "credentialsSecretArn": kb_config.get("rdsCredentialsSecretArn", ""),
                "databaseName": kb_config.get("rdsDatabaseName", ""),
                "tableName": kb_config.get("rdsTableName", ""),
                "fieldMapping": {
                    "primaryKeyField": kb_config.get("rdsPrimaryKeyField", "id"),
                    "vectorField": kb_config.get("rdsVectorField", "embedding"),
                    "textField": kb_config.get("rdsTextField", "chunks"),
                    "metadataField": kb_config.get("rdsMetadataField", "metadata"),
                },
            },
        }

    # Default: S3_VECTORS (fully managed). Bedrock requires either an
    # explicit s3VectorsConfiguration (vectorBucketArn + indexArn/indexName)
    # or it can auto-create one if you pass `vectorIndexName` only — but in
    # practice the API rejects bare {"type":"S3_VECTORS"} with
    # "ValidationException: storageConfiguration ... is required". See
    # tasks/lessons.md Bug 73.
    s3_vec_bucket_arn = kb_config.get("s3VectorsBucketArn", "")
    s3_vec_index_name = kb_config.get("s3VectorsIndexName") or "bedrock-knowledge-base-default-index"
    config: dict = {"type": "S3_VECTORS"}
    if s3_vec_bucket_arn:
        config["s3VectorsConfiguration"] = {
            "vectorBucketArn": s3_vec_bucket_arn,
            "indexName": s3_vec_index_name,
        }
        if kb_config.get("s3VectorsIndexArn"):
            config["s3VectorsConfiguration"]["indexArn"] = kb_config["s3VectorsIndexArn"]
    else:
        # Auto-managed mode: provide indexName only, Bedrock will provision
        # a bucket+index for us.
        config["s3VectorsConfiguration"] = {"indexName": s3_vec_index_name}
    return config


def _build_data_source_config(kb_config: dict) -> tuple[dict, str | None]:
    """Build data source configuration. Returns (ds_config, credentials_secret_arn)."""
    data_source_type = kb_config.get("dataSourceType", "s3")
    vector_store_type = kb_config.get("vectorStoreType", "s3_vectors")

    # Bug 186 — AWS rejects a WEB (web_crawler) data source on any vector store
    # other than OpenSearch Serverless ("WEB data source is currently only
    # supported for knowledge bases created with an Amazon OpenSearch Serverless
    # vector database"). The platform defaults to s3_vectors, so this combo fails
    # at CreateDataSource with a raw ValidationException. Reject it EARLY with an
    # actionable message instead of surfacing the opaque AWS error mid-deploy.
    if data_source_type == "web_crawler" and vector_store_type != "opensearch_serverless":
        raise ValueError(
            "Web Crawler data source requires the OpenSearch Serverless vector store "
            f"(got vectorStoreType='{vector_store_type}'). Either set "
            "vectorStoreType='opensearch_serverless', or use an S3 data source with "
            "the default s3_vectors store."
        )

    if data_source_type == "s3":
        s3_uri = kb_config.get("s3BucketUri", "")
        bucket_arn = _parse_s3_bucket_arn(s3_uri)
        prefix = ""
        parts = s3_uri[5:].split("/", 1)
        if len(parts) > 1 and parts[1]:
            prefix = parts[1]
        s3_config: dict = {"bucketArn": bucket_arn}
        if prefix:
            s3_config["inclusionPrefixes"] = [prefix]
        return {"type": "S3", "s3Configuration": s3_config}, None

    if data_source_type == "web_crawler":
        # Filter empty seed URLs — Bedrock CreateDataSource rejects
        # `seedUrls.N.member.url=""` with ValidationException. See
        # tasks/lessons.md Bug 94. Accept either a string OR a list.
        raw_urls = kb_config.get("webCrawlerUrls") or kb_config.get("seedUrls") or kb_config.get("webCrawlerUrl", "")
        if isinstance(raw_urls, str):
            url_list = [u.strip() for u in raw_urls.split(",") if u.strip()]
        else:
            url_list = [u.strip() for u in raw_urls if isinstance(u, str) and u.strip()]
        if not url_list:
            raise ValueError("Web Crawler data source requires at least one non-empty seed URL")
        seed_urls = [{"url": u} for u in url_list]
        scope = kb_config.get("webCrawlerScope", "HOST_ONLY")
        return {
            "type": "WEB",
            "webConfiguration": {
                "sourceConfiguration": {
                    "urlConfiguration": {"seedUrls": seed_urls},
                },
                "crawlerConfiguration": {
                    "crawlerLimits": {"rateLimit": 10},
                    "scope": scope,
                },
            },
        }, None

    if data_source_type == "confluence":
        host_url = kb_config.get("confluenceHostUrl", "")
        # Bedrock API only supports SAAS hostType for Confluence
        host_type = "SAAS"
        secret_arn = kb_config.get("confluenceCredentialsSecretArn", "")
        return {
            "type": "CONFLUENCE",
            "confluenceConfiguration": {
                "sourceConfiguration": {
                    "hostUrl": host_url,
                    "hostType": host_type,
                    "authType": "OAUTH2_CLIENT_CREDENTIALS",
                    "credentialsSecretArn": secret_arn,
                },
                "crawlerConfiguration": {
                    "filterConfiguration": {
                        "type": "PATTERN",
                        "patternObjectFilter": {
                            "filters": [{"objectType": "Page", "inclusionFilters": [".*"]}],
                        },
                    },
                },
            },
        }, secret_arn

    if data_source_type == "salesforce":
        host_url = kb_config.get("salesforceHostUrl", "")
        secret_arn = kb_config.get("salesforceCredentialsSecretArn", "")
        return {
            "type": "SALESFORCE",
            "salesforceConfiguration": {
                "sourceConfiguration": {
                    "hostUrl": host_url,
                    "authType": "OAUTH2_CLIENT_CREDENTIALS",
                    "credentialsSecretArn": secret_arn,
                },
                "crawlerConfiguration": {
                    "filterConfiguration": {
                        "type": "PATTERN",
                        "patternObjectFilter": {
                            "filters": [{"objectType": "Knowledge", "inclusionFilters": [".*"]}],
                        },
                    },
                },
            },
        }, secret_arn

    if data_source_type == "sharepoint":
        domain = kb_config.get("sharePointDomain", "")
        site_urls_str = kb_config.get("sharePointSiteUrls", "")
        site_urls = [u.strip() for u in site_urls_str.split(",") if u.strip()]
        tenant_id = kb_config.get("sharePointTenantId", "")
        secret_arn = kb_config.get("sharePointCredentialsSecretArn", "")
        return {
            "type": "SHAREPOINT",
            "sharePointConfiguration": {
                "sourceConfiguration": {
                    "domain": domain,
                    "siteUrls": site_urls,
                    "tenantId": tenant_id,
                    "hostType": "ONLINE",
                    "authType": "OAUTH2_CLIENT_CREDENTIALS",
                    "credentialsSecretArn": secret_arn,
                },
                "crawlerConfiguration": {
                    "filterConfiguration": {
                        "type": "PATTERN",
                        "patternObjectFilter": {
                            "filters": [{"objectType": "Page", "inclusionFilters": [".*"]}],
                        },
                    },
                },
            },
        }, secret_arn

    raise ValueError(f"Unsupported data source type: {data_source_type}")


def _parse_s3_bucket_arn(s3_uri: str) -> str:
    """Convert s3://bucket/prefix to arn:aws:s3:::bucket."""
    if s3_uri.startswith("s3://"):
        bucket = s3_uri[5:].split("/")[0]
        return f"arn:aws:s3:::{bucket}"
    raise ValueError(f"Invalid S3 URI: {s3_uri}")


def handler(event: dict, context) -> dict:  # noqa: ARG001
    kb_config = event.get("knowledge_base_config")
    if not kb_config:
        return event  # No KB configured, pass through

    deployment_id = event.get("deployment_id", "")
    region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))

    store = None
    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.KNOWLEDGE_BASE, DeploymentStatusEnum.IN_PROGRESS)
    except Exception:
        logger.exception("Failed to update step status for KB step")

    kb_mode = kb_config.get("kbMode", "existing")
    foundation_model_id = kb_config.get("foundationModelId", "us.anthropic.claude-sonnet-5")
    foundation_model_arn = _build_model_arn(region, foundation_model_id)

    bedrock_agent = step_clients.client(event, "bedrock-agent")

    if kb_mode == "existing":
        kb_id = kb_config.get("knowledgeBaseId", "").strip()
        if not kb_id:
            raise ValueError("knowledgeBaseId is required for existing KB mode")

        # Validate KB exists
        try:
            resp = bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
            status = resp.get("knowledgeBase", {}).get("status", "")
            if status != "ACTIVE":
                raise RuntimeError(f"Knowledge Base {kb_id} is not ACTIVE (status: {status})")
            logger.warning("Validated existing KB: %s (status: %s)", kb_id, status)
        except bedrock_agent.exceptions.ResourceNotFoundException:
            raise ValueError(f"Knowledge Base {kb_id} not found") from None

        event["knowledge_base_result"] = {
            "kb_id": kb_id,
            "created_by_flow": False,
            "foundation_model_arn": foundation_model_arn,
        }
        return event

    if kb_mode == "create_new":
        kb_name = kb_config.get("kbName", f"agentcore-kb-{deployment_id[:8]}")
        kb_description = kb_config.get("kbDescription", "Knowledge Base created by AgentCore Flow")
        embedding_model_id = kb_config.get("embeddingModelId", "amazon.titan-embed-text-v2:0")
        embedding_model_arn = _build_model_arn(region, embedding_model_id)

        # Step 1: Create IAM role with permissions based on data source + vector store
        iam_client = step_clients.client(event, "iam")
        role_name = f"AgentCoreKBRole-{deployment_id[:8]}"
        role_arn = _create_kb_role(iam_client, role_name, kb_config)
        logger.warning("KB role created: %s", role_arn)
        # Manifest: record the KB exec role for generic teardown. Best-effort.
        if store is not None:
            store.record_resource(
                deployment_id,
                {"type": "iam_role", "name": role_name, "region": region},
            )

        # Step 2: Check if KB already exists (idempotency for SFN retries)
        kb_id = _find_existing_kb(bedrock_agent, kb_name)
        if kb_id:
            logger.warning("Found existing KB with name %s: %s (reusing)", kb_name, kb_id)
        else:
            # S3 Vectors requires the vector bucket AND index to pre-exist before
            # CreateKnowledgeBase. Bedrock does NOT auto-provision an S3 Vectors
            # bucket from a bare {"s3VectorsConfiguration":{"indexName": ...}}
            # storage config — instead it rejects the create with the misleading
            # `ValidationException: Bedrock Knowledge Base was unable to assume
            # the given role` (the role/perms are fine; the storage target just
            # doesn't exist). So when the user did NOT supply an explicit bucket
            # ARN we self-provision a vector bucket + index here and pin the ARN
            # into kb_config so _build_storage_config emits an explicit
            # vectorBucketArn. See tasks/lessons.md Bug 145.
            #
            # Index name MUST match the default used by _build_storage_config
            # ("bedrock-knowledge-base-default-index") or retrieval misses.
            if kb_config.get("vectorStoreType", "s3_vectors") == "s3_vectors":
                vec_arn = kb_config.get("s3VectorsBucketArn", "")
                vec_idx = kb_config.get("s3VectorsIndexName") or "bedrock-knowledge-base-default-index"
                kb_config["s3VectorsIndexName"] = vec_idx
                s3v = step_clients.client(event, "s3vectors")
                if not vec_arn:
                    # Auto-managed mode: create our own vector bucket. Bucket
                    # names: 3-63 chars, lowercase alphanum + hyphen.
                    vec_bucket_name = f"agentcore-kbvec-{deployment_id[:12]}"
                    try:
                        s3v.create_vector_bucket(vectorBucketName=vec_bucket_name)
                        logger.warning("Auto-created S3 Vectors bucket %s", vec_bucket_name)
                    except Exception as vb_err:
                        # AlreadyExists on SFN retry is fine.
                        logger.warning("create_vector_bucket: %s", str(vb_err)[:200])
                    try:
                        desc = s3v.get_vector_bucket(vectorBucketName=vec_bucket_name)
                        vec_arn = desc.get("vectorBucket", {}).get("vectorBucketArn", "")
                    except Exception:
                        vec_arn = ""
                    if not vec_arn:
                        vec_arn = f"arn:aws:s3vectors:{region}:{_get_account_id(event)}:bucket/{vec_bucket_name}"
                    kb_config["s3VectorsBucketArn"] = vec_arn
                    # Record for teardown.
                    if store is not None:
                        store.record_resource(
                            deployment_id,
                            {"type": "s3_vectors_bucket", "name": vec_bucket_name, "region": region},
                        )
                else:
                    vec_bucket_name = vec_arn.rsplit("/", 1)[-1]
                # Ensure the index exists (Titan Embed Text v2 = 1024 dims, cosine).
                try:
                    existing = s3v.list_indexes(vectorBucketName=vec_bucket_name).get("indexes", [])
                    if not any(ix.get("indexName") == vec_idx for ix in existing):
                        logger.warning("Auto-creating S3 Vectors index '%s' on bucket %s", vec_idx, vec_bucket_name)
                        s3v.create_index(
                            vectorBucketName=vec_bucket_name,
                            indexName=vec_idx,
                            dataType="float32",
                            dimension=1024,
                            distanceMetric="cosine",
                        )
                except Exception as ix_err:
                    logger.warning("S3 Vectors index pre-check/create skipped: %s", ix_err)

            # OpenSearch Serverless: Bedrock requires a pre-existing collection ARN
            # (no auto-provision from storage config, unlike S3 Vectors). If the
            # caller didn't supply one, self-provision the collection + index here
            # and record it to the manifest for teardown (standing billable resource).
            elif kb_config.get("vectorStoreType") == "opensearch_serverless" and not kb_config.get(
                "opensearchCollectionArn"
            ):
                _ensure_oss_collection(region, deployment_id, role_arn, kb_config, store, deployment_id, event)

            # Least privilege: the role was created BEFORE the vector store
            # existed, so its interim policy used naming-convention patterns
            # (agentcore-kbvec-* / collection/*). Now that kb_config carries
            # the exact bucket/collection ARN, re-put the policy so every
            # statement is scoped to the real resource. Best-effort — the
            # interim pattern is already functional.
            try:
                _put_kb_role_policy(iam_client, role_name, kb_config)
            except Exception:  # noqa: BLE001
                logger.warning("KB role policy tighten (re-put) skipped", exc_info=True)

            storage_config = _build_storage_config(kb_config)
            vector_kb_config: dict = {
                "embeddingModelArn": embedding_model_arn,
            }
            # If BDA parsing is configured, attach supplementalDataStorage.
            # See tasks/lessons.md Bug 95.
            if kb_config.get("parsingStrategy") == "bedrock_data_automation":
                # CreateKnowledgeBase rejects supplemental URIs with a key
                # prefix ("S3 URI should only contain the bucket name") —
                # bucket root only. Live-verified by the matrix run.
                bda_supp_uri = kb_config.get("bdaSupplementalS3Uri") or (
                    f"s3://{_get_env('ARTIFACTS_BUCKET_NAME', '')}"
                )
                # API shape (botocore bedrock-agent model): storageLocations,
                # each {type, s3Location} — live-verified by the matrix run.
                vector_kb_config["supplementalDataStorageConfiguration"] = {
                    "storageLocations": [{"type": "S3", "s3Location": {"uri": bda_supp_uri}}]
                }
            kb_params = {
                "name": kb_name,
                "description": kb_description,
                "roleArn": role_arn,
                "knowledgeBaseConfiguration": {
                    "type": "VECTOR",
                    "vectorKnowledgeBaseConfiguration": vector_kb_config,
                },
                "storageConfiguration": storage_config,
            }

            # Bedrock validates that it can assume the KB role at create time.
            # IAM propagation can lag put_role_policy by 10-60s; surfaced as
            # `ValidationException: Bedrock Knowledge Base was unable to assume
            # the given role`. Retry with backoff. See tasks/lessons.md Bug 80.
            kb_resp = None
            last_err = None
            for attempt in range(12):
                try:
                    kb_resp = bedrock_agent.create_knowledge_base(**kb_params)
                    break
                except Exception as e:
                    err_str = str(e).lower()
                    # Retry two transient races: (1) IAM role propagation ("unable
                    # to assume"); (2) OSS data-access-policy propagation — Bedrock's
                    # KB-role session hits the just-created collection before the
                    # access policy is live, surfacing as "server returned 401" /
                    # "storage configuration ... is invalid". Both clear within ~1-2 min.
                    transient = (
                        ("validationexception" in err_str and "unable to assume" in err_str)
                        or "server returned 401" in err_str
                        or ("storage configuration" in err_str and "invalid" in err_str)
                    )
                    if transient:
                        last_err = e
                        logger.warning(
                            "create_knowledge_base propagation race (attempt %d/12): %s",
                            attempt + 1,
                            str(e)[:200],
                        )
                        time.sleep(15)
                        continue
                    raise
            if kb_resp is None:
                raise last_err if last_err else RuntimeError("create_knowledge_base failed")
            kb_id = kb_resp["knowledgeBase"]["knowledgeBaseId"]
            logger.warning("Knowledge Base created: %s", kb_id)
            # Manifest: record the KB FIRST (Bug 167). Teardown must delete the
            # KnowledgeBase (and wait for it to reach a terminal deleted state)
            # BEFORE its backing S3 Vectors bucket + exec role — deleting a KB
            # with dataDeletionPolicy=DELETE makes Bedrock reach into the vector
            # store using the role, so both must OUTLIVE the KB delete. The
            # manifest delete is priority-ordered (knowledge_base before
            # s3_vectors_bucket/iam_role) in deployment_handler.
            if store is not None:
                store.record_resource(
                    deployment_id,
                    {"type": "knowledge_base", "id": kb_id, "region": region},
                )

        # Step 3: Wait for KB to become ACTIVE
        _wait_for_kb_active(bedrock_agent, kb_id)
        logger.warning("Knowledge Base %s is ACTIVE", kb_id)

        # Step 4: Create data source
        ds_config, credentials_secret_arn = _build_data_source_config(kb_config)

        chunking_strategy = kb_config.get("chunkingStrategy", "FIXED_SIZE")
        chunking_config: dict = {"chunkingStrategy": chunking_strategy}

        if chunking_strategy == "FIXED_SIZE":
            chunking_config["fixedSizeChunkingConfiguration"] = {
                "maxTokens": kb_config.get("maxTokens", 300),
                "overlapPercentage": kb_config.get("overlapPercentage", 20),
            }
        elif chunking_strategy == "HIERARCHICAL":
            chunking_config["hierarchicalChunkingConfiguration"] = {
                "levelConfigurations": [
                    {"maxTokens": 1500},
                    {"maxTokens": 300},
                ],
                "overlapTokens": 60,
            }
        elif chunking_strategy == "SEMANTIC":
            # Bedrock requires `semanticChunkingConfiguration` block when
            # chunkingStrategy=SEMANTIC. See tasks/lessons.md Bug 96.
            chunking_config["semanticChunkingConfiguration"] = {
                "maxTokens": kb_config.get("semanticMaxTokens", 300),
                "bufferSize": kb_config.get("semanticBufferSize", 0),
                "breakpointPercentileThreshold": kb_config.get("semanticBreakpointPercentile", 95),
            }

        # Build vectorIngestionConfiguration (chunking + parsing + transformation)
        ingestion_config: dict = {"chunkingConfiguration": chunking_config}

        # Parsing strategy
        parsing_strategy = kb_config.get("parsingStrategy", "default")
        if parsing_strategy == "bedrock_data_automation":
            ingestion_config["parsingConfiguration"] = {
                "parsingStrategy": "BEDROCK_DATA_AUTOMATION",
                "bedrockDataAutomationConfiguration": {"parsingModality": "MULTIMODAL"},
            }
            # BDA's intermediate-output bucket is configured on the KB itself
            # via `supplementalDataStorageConfiguration` at create_knowledge_base
            # time (see the vector_kb_config block above). create_data_source
            # rejects unknown keys on vectorIngestionConfiguration, so do not
            # add anything BDA-related here.
        elif parsing_strategy == "bedrock_foundation_model":
            parsing_model_id = kb_config.get("parsingModelId", "us.anthropic.claude-sonnet-5")
            fm_config: dict = {
                "modelArn": _build_model_arn(region, parsing_model_id),
                "parsingModality": "MULTIMODAL",
            }
            parsing_prompt = kb_config.get("parsingPrompt", "")
            if parsing_prompt:
                fm_config["parsingPrompt"] = {"parsingPromptText": parsing_prompt}
            ingestion_config["parsingConfiguration"] = {
                "parsingStrategy": "BEDROCK_FOUNDATION_MODEL",
                "bedrockFoundationModelConfiguration": fm_config,
            }

        # Custom transformation Lambda
        transform_lambda = kb_config.get("transformationLambdaArn", "")
        transform_s3 = kb_config.get("transformationS3Uri", "")
        if transform_lambda and transform_s3:
            ingestion_config["customTransformationConfiguration"] = {
                "intermediateStorage": {
                    "s3Location": {"uri": transform_s3},
                },
                "transformations": [
                    {
                        "transformationFunction": {
                            "transformationLambdaConfiguration": {"lambdaArn": transform_lambda},
                        },
                        "stepToApply": "POST_CHUNKING",
                    }
                ],
            }

        ds_params: dict = {
            "knowledgeBaseId": kb_id,
            "name": f"{kb_name}-source",
            "dataSourceConfiguration": ds_config,
            "vectorIngestionConfiguration": ingestion_config,
        }

        # Data deletion policy
        deletion_policy = kb_config.get("dataDeletionPolicy", "DELETE")
        if deletion_policy != "DELETE":
            ds_params["dataDeletionPolicy"] = deletion_policy

        # KMS key for transient data encryption
        kms_key = kb_config.get("kmsKeyArn", "")
        if kms_key:
            ds_params["serverSideEncryptionConfiguration"] = {"kmsKeyArn": kms_key}

        # Idempotent create: a Step Functions retry (or a slow first attempt
        # that timed out after the service-side create landed) hits
        # ConflictException on the same name — recover the existing data
        # source instead of failing the deploy (matrix-run finding, P-KB-008).
        try:
            ds_resp = bedrock_agent.create_data_source(**ds_params)
            ds_id = ds_resp["dataSource"]["dataSourceId"]
            logger.warning("Data source created: %s for KB %s", ds_id, kb_id)
        except ClientError as ds_err:
            if error_code(ds_err) != "ConflictException":
                raise
            existing = bedrock_agent.list_data_sources(knowledgeBaseId=kb_id, maxResults=50).get(
                "dataSourceSummaries", []
            )
            match = next((d for d in existing if d.get("name") == ds_params["name"]), None)
            if not match:
                raise
            ds_id = match["dataSourceId"]
            logger.warning("Data source '%s' already exists (%s), reusing", ds_params["name"], ds_id)

        # Step 5: Start ingestion. Wait for queryable vectors; record the
        # terminal status so a KB that's still ingesting is reported honestly
        # rather than silently implied ready (P-E2E matrix finding). The wait is
        # bounded to stay inside the SFN task timeout (600s) and the 30-min
        # state-machine budget — an IN_PROGRESS return is NOT a failure: the KB
        # exists and its vectors become queryable as the crawl finishes in the
        # background (verified live for web_crawler P-KB-008: example.com
        # dispatches + indexes shortly after this window, and the agent then
        # retrieves the crawled content).
        _ingest_wait = 540
        _job_id, ingestion_status = _start_and_wait_ingestion(bedrock_agent, kb_id, ds_id, max_wait=_ingest_wait)

        event["knowledge_base_result"] = {
            "kb_id": kb_id,
            "data_source_id": ds_id,
            "kb_role_arn": role_arn,
            "created_by_flow": True,
            "foundation_model_arn": foundation_model_arn,
            "ingestion_status": ingestion_status,  # COMPLETE | IN_PROGRESS
        }
        return event

    raise ValueError(f"Invalid kbMode: {kb_mode}")
