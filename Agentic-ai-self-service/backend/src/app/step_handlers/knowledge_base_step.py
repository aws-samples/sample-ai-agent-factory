"""Step handler: Create or validate a Bedrock Knowledge Base.

Handles two modes:
- existing: Validates the KB exists and returns its ID
- create_new: Creates KB + data source + starts ingestion
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import json
import logging
import os
import time

import boto3

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
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
            statements.append({
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [bucket_arn, f"{bucket_arn}/*"],
            })

    # Credential-based data sources need Secrets Manager access
    secret_arns = []
    if data_source_type == "confluence":
        secret_arns.append(kb_config.get("confluenceCredentialsSecretArn", ""))
    elif data_source_type == "salesforce":
        secret_arns.append(kb_config.get("salesforceCredentialsSecretArn", ""))
    elif data_source_type == "sharepoint":
        secret_arns.append(kb_config.get("sharePointCredentialsSecretArn", ""))

    # OpenSearch Serverless permissions
    if vector_store_type == "opensearch_serverless":
        statements.append({
            "Effect": "Allow",
            "Action": ["aoss:APIAccessAll"],
            "Resource": kb_config.get("opensearchCollectionArn", "*"),
        })

    # S3 Vectors permissions (Bug 78). The role needs to provision and
    # interact with the auto-managed vector bucket+index that Bedrock KB
    # creates on its behalf. Without these, Bedrock validates the role,
    # finds it can't create/describe the s3vectors resources, and rejects
    # the KB with `ValidationException: Bedrock Knowledge Base was unable
    # to assume the given role`.
    if vector_store_type == "s3_vectors":
        s3v_arn = kb_config.get("s3VectorsBucketArn", "*")
        # Some s3vectors verbs (QueryVectors, PutVectors, GetVectors,
        # DeleteVectors, DescribeIndex, ListIndexes) target the
        # `<bucket>/index/<idx>` sub-resource, not just the bucket.
        # Granting only the bucket ARN surfaces as a misleading
        # "Bedrock KB was unable to assume the given role" error.
        # See tasks/lessons.md Bug 84.
        if s3v_arn == "*":
            s3v_resources = ["*"]
        else:
            s3v_resources = [s3v_arn, f"{s3v_arn}/index/*"]
        statements.append({
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
                "s3vectors:GetVectorBucket",
                "s3vectors:ListIndexes",
                "s3vectors:ListVectorBuckets",
            ],
            "Resource": s3v_resources,
        })

    # RDS permissions
    if vector_store_type == "rds":
        statements.append({
            "Effect": "Allow",
            "Action": ["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement"],
            "Resource": kb_config.get("rdsResourceArn", "*"),
        })
        rds_secret = kb_config.get("rdsCredentialsSecretArn", "")
        if rds_secret:
            secret_arns.append(rds_secret)

    # Custom transformation Lambda permissions
    transform_lambda = kb_config.get("transformationLambdaArn", "")
    if transform_lambda:
        statements.append({
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            "Resource": transform_lambda,
        })

    # S3 access for transformation intermediate storage
    transform_s3 = kb_config.get("transformationS3Uri", "")
    if transform_s3 and transform_s3.startswith("s3://"):
        t_bucket = transform_s3[5:].split("/")[0]
        t_bucket_arn = f"arn:aws:s3:::{t_bucket}"
        statements.append({
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
            "Resource": [t_bucket_arn, f"{t_bucket_arn}/*"],
        })

    # Consolidate Secrets Manager permissions
    valid_secrets = [s for s in secret_arns if s]
    if valid_secrets:
        statements.append({
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": valid_secrets if len(valid_secrets) > 1 else valid_secrets[0],
        })

    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName="BedrockKBAccess",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}),
    )

    # IAM eventual consistency
    time.sleep(10)
    return role_arn


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


def _start_and_wait_ingestion(bedrock_agent, kb_id: str, ds_id: str, max_wait: int = 300) -> str:
    """Start a data ingestion job and poll until complete or timeout."""
    resp = bedrock_agent.start_ingestion_job(
        knowledgeBaseId=kb_id,
        dataSourceId=ds_id,
    )
    job_id = resp["ingestionJob"]["ingestionJobId"]
    logger.warning("Ingestion job started: %s for KB %s", job_id, kb_id)

    for _ in range(max_wait // 5):
        job_resp = bedrock_agent.get_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
            ingestionJobId=job_id,
        )
        status = job_resp.get("ingestionJob", {}).get("status", "")
        if status == "COMPLETE":
            logger.warning("Ingestion job %s completed", job_id)
            return job_id
        if status == "FAILED":
            failure = job_resp.get("ingestionJob", {}).get("failureReasons", [])
            raise RuntimeError(f"Ingestion job failed: {failure}")
        time.sleep(5)

    # Timeout is not fatal - ingestion continues in background
    logger.warning("Ingestion job %s still running after %ds (continuing)", job_id, max_wait)
    return job_id


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
        raw_urls = kb_config.get("webCrawlerUrls") or kb_config.get("webCrawlerUrl", "")
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

    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.KNOWLEDGE_BASE, DeploymentStatusEnum.IN_PROGRESS)
    except Exception:
        logger.exception("Failed to update step status for KB step")

    kb_mode = kb_config.get("kbMode", "existing")
    foundation_model_id = kb_config.get("foundationModelId", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    foundation_model_arn = _build_model_arn(region, foundation_model_id)

    bedrock_agent = boto3.client("bedrock-agent", region_name=region)

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
        iam_client = boto3.client("iam")
        role_name = f"AgentCoreKBRole-{deployment_id[:8]}"
        role_arn = _create_kb_role(iam_client, role_name, kb_config)
        logger.warning("KB role created: %s", role_arn)

        # Step 2: Check if KB already exists (idempotency for SFN retries)
        kb_id = _find_existing_kb(bedrock_agent, kb_name)
        if kb_id:
            logger.warning("Found existing KB with name %s: %s (reusing)", kb_name, kb_id)
        else:
            # If user provided an explicit S3 Vectors bucket, ensure the index
            # exists before calling CreateKnowledgeBase — Bedrock requires the
            # index to pre-exist and returns "The specified index could not
            # be found" otherwise. See tasks/lessons.md Bug 88.
            if kb_config.get("vectorStoreType", "s3_vectors") == "s3_vectors":
                vec_arn = kb_config.get("s3VectorsBucketArn", "")
                vec_idx = kb_config.get("s3VectorsIndexName") or "default-index"
                if vec_arn:
                    vec_bucket_name = vec_arn.rsplit("/", 1)[-1]
                    try:
                        s3v = boto3.client("s3vectors", region_name=region)
                        existing = s3v.list_indexes(vectorBucketName=vec_bucket_name).get("indexes", [])
                        if not any(ix.get("indexName") == vec_idx for ix in existing):
                            logger.warning("Auto-creating missing S3 Vectors index '%s' on bucket %s", vec_idx, vec_bucket_name)
                            # Titan Embed Text v2 default = 1024 dims, cosine
                            s3v.create_index(
                                vectorBucketName=vec_bucket_name,
                                indexName=vec_idx,
                                dataType="float32",
                                dimension=1024,
                                distanceMetric="cosine",
                            )
                    except Exception as ix_err:
                        logger.warning("S3 Vectors index pre-check/create skipped: %s", ix_err)

            storage_config = _build_storage_config(kb_config)
            vector_kb_config: dict = {
                "embeddingModelArn": embedding_model_arn,
            }
            # If BDA parsing is configured, attach supplementalDataStorage.
            # See tasks/lessons.md Bug 95.
            if kb_config.get("parsingStrategy") == "bedrock_data_automation":
                bda_supp_uri = kb_config.get("bdaSupplementalS3Uri") or (
                    f"s3://{_get_env('ARTIFACTS_BUCKET_NAME','')}/kb-supplemental/{kb_name}/"
                )
                vector_kb_config["supplementalDataStorageConfiguration"] = {
                    "supplementalDataStorageLocations": [
                        {"supplementalDataStorageLocationType": "S3", "s3Location": {"uri": bda_supp_uri}}
                    ]
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
            for attempt in range(8):
                try:
                    kb_resp = bedrock_agent.create_knowledge_base(**kb_params)
                    break
                except Exception as e:
                    err_str = str(e)
                    if (
                        "ValidationException" in err_str
                        and "unable to assume" in err_str.lower()
                    ):
                        last_err = e
                        logger.warning(
                            "create_knowledge_base IAM-propagation race (attempt %d/8): %s",
                            attempt + 1, err_str[:200],
                        )
                        time.sleep(10)
                        continue
                    raise
            if kb_resp is None:
                raise last_err if last_err else RuntimeError("create_knowledge_base failed")
            kb_id = kb_resp["knowledgeBase"]["knowledgeBaseId"]
            logger.warning("Knowledge Base created: %s", kb_id)

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
            parsing_model_id = kb_config.get("parsingModelId", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
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

        ds_resp = bedrock_agent.create_data_source(**ds_params)
        ds_id = ds_resp["dataSource"]["dataSourceId"]
        logger.warning("Data source created: %s for KB %s", ds_id, kb_id)

        # Step 5: Start ingestion
        _start_and_wait_ingestion(bedrock_agent, kb_id, ds_id, max_wait=300)

        event["knowledge_base_result"] = {
            "kb_id": kb_id,
            "data_source_id": ds_id,
            "kb_role_arn": role_arn,
            "created_by_flow": True,
            "foundation_model_arn": foundation_model_arn,
        }
        return event

    raise ValueError(f"Invalid kbMode: {kb_mode}")
