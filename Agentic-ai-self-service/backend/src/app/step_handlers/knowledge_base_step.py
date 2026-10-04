"""Step handler: Create or validate a Bedrock Knowledge Base.

Handles two modes:
- existing: Validates the KB exists and returns its ID
- create_new: Creates KB + data source + starts ingestion
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import copy
import json
import logging
import os
import time
from collections.abc import Callable

from botocore.exceptions import ClientError

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.aws_errors import error_code
from app.services.aws_pagination import list_all
from app.services.deployment_state_store import DeploymentStateStore
from app.services.iam_boundary import create_role_kwargs, ensure_role_boundary
from app.services.region_models import is_inference_profile_id, repoint_regional_prefix
from app.services.resource_ownership import (
    DEPLOYMENT_ID_TAG_KEY,
    OWNER_SUB_HASH_TAG_KEY,
    ResourceAccessRefused,
    aoss_policy_owner_description,
    assert_aoss_policy_owned,
    assert_resource_access_allowed,
    assert_resource_bound_to_deployment,
    assert_this_deployment_may_mutate,
    get_owned_aoss_collection,
    owner_sub_hash,
)
from app.services.resource_tagging import (
    governed_lower_tag_list,
    governed_tag_list,
    governed_tags,
)

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _list_all_vector_indexes(s3v, bucket_name: str) -> list[dict]:
    return list_all(
        s3v,
        "list_indexes",
        item_keys=("indexes",),
        request={"vectorBucketName": bucket_name, "maxResults": 100},
    )


def _read_compatible_s3_vectors_index(
    s3v,
    *,
    bucket_name: str,
    bucket_arn: str,
    index_name: str,
    requested_index_arn: str = "",
) -> dict | None:
    """Return one live compatible index, or ``None`` when it does not exist.

    The list call is authoritative for existence and is paginated.  The detail
    call then pins the deployment to the live ARN and schema instead of trusting
    a caller-supplied ARN or assuming that a same-named index is compatible.
    """
    summaries = _list_all_vector_indexes(s3v, bucket_name)
    if not any(item.get("indexName") == index_name for item in summaries):
        return None

    index = (
        s3v.get_index(
            vectorBucketName=bucket_name,
            indexName=index_name,
        ).get("index")
        or {}
    )
    if index.get("dataType") != "float32" or index.get("dimension") != 1024 or index.get("distanceMetric") != "cosine":
        raise RuntimeError(f"S3 Vectors index {index_name} has an incompatible schema; expected float32/1024/cosine.")

    live_index_arn = str(index.get("indexArn") or "")
    if not live_index_arn:
        raise ResourceAccessRefused(
            f"Access refused for S3 Vectors index {index_name}: its live response contained no ARN."
        )
    if not live_index_arn.startswith(f"{bucket_arn.rstrip('/')}/index/"):
        raise ResourceAccessRefused(
            f"Access refused for S3 Vectors index {index_name}: its live ARN "
            "does not belong to the authorized vector bucket."
        )
    if requested_index_arn and requested_index_arn != live_index_arn:
        raise ResourceAccessRefused(
            "Access refused for S3 Vectors index: the supplied ARN does not match the live index selected by name."
        )
    return index


def _oss_index_definition(kb_config: dict) -> tuple[str, dict]:
    """Return the configured AOSS index name and the required live schema."""
    index_name = str(kb_config.get("opensearchVectorIndexName") or "") or "bedrock-knowledge-base-default-index"
    vector_field = str(kb_config.get("opensearchVectorField") or "") or "bedrock-knowledge-base-default-vector"
    text_field = str(kb_config.get("opensearchTextField") or "") or "AMAZON_BEDROCK_TEXT_CHUNK"
    metadata_field = str(kb_config.get("opensearchMetadataField") or "") or "AMAZON_BEDROCK_METADATA"
    kb_config["opensearchVectorIndexName"] = index_name
    kb_config["opensearchVectorField"] = vector_field
    kb_config["opensearchTextField"] = text_field
    kb_config["opensearchMetadataField"] = metadata_field
    return index_name, {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                vector_field: {
                    "type": "knn_vector",
                    "dimension": 1024,
                    "method": {
                        "name": "hnsw",
                        "engine": "faiss",
                        "space_type": "l2",
                    },
                },
                text_field: {"type": "text"},
                metadata_field: {"type": "text"},
            }
        },
    }


def _assert_compatible_oss_index(
    index_name: str,
    live_schema: dict | None,
    expected_schema: dict,
) -> None:
    """Require the Bedrock-critical AOSS schema while allowing extra settings."""
    live = live_schema or {}
    settings = (live.get("settings") or {}).get("index") or {}
    properties = (live.get("mappings") or {}).get("properties") or {}
    expected_properties = expected_schema["mappings"]["properties"]
    vector_field = next(
        name for name, definition in expected_properties.items() if definition.get("type") == "knn_vector"
    )
    text_fields = [name for name, definition in expected_properties.items() if definition.get("type") == "text"]
    expected_vector = expected_properties[vector_field]
    live_vector = properties.get(vector_field) or {}
    live_method = live_vector.get("method") or {}
    expected_method = expected_vector["method"]
    compatible = (
        settings.get("knn") is True
        and live_vector.get("type") == "knn_vector"
        and live_vector.get("dimension") == 1024
        and live_method.get("name") == expected_method["name"]
        and live_method.get("engine") == expected_method["engine"]
        and live_method.get("space_type") == expected_method["space_type"]
        and all((properties.get(field_name) or {}).get("type") == "text" for field_name in text_fields)
    )
    if not compatible:
        raise RuntimeError(
            f"OpenSearch Serverless index {index_name} has an incompatible "
            "schema. Expected k-NN hnsw/faiss/l2 with a 1024-dimensional "
            "vector and the configured text/metadata fields."
        )


def _list_all_data_sources(bedrock_agent, kb_id: str) -> list[dict]:
    return list_all(
        bedrock_agent,
        "list_data_sources",
        item_keys=("dataSourceSummaries",),
        request={"knowledgeBaseId": kb_id, "maxResults": 100},
    )


def _list_all_ingestion_jobs(
    bedrock_agent,
    kb_id: str,
    ds_id: str,
) -> list[dict]:
    return list_all(
        bedrock_agent,
        "list_ingestion_jobs",
        item_keys=("ingestionJobSummaries",),
        request={
            "knowledgeBaseId": kb_id,
            "dataSourceId": ds_id,
            "maxResults": 50,
        },
    )


def _build_model_arn(region: str, model_id: str, account_id: str = "") -> str:
    """Build the Bedrock ARN for *model_id* — profile or foundation model.

    An existing cross-region prefix is re-pointed at ``region`` first: the
    defaults and stored KB configs carry ``us.``, and there is no
    ``us.anthropic.…`` profile in eu-central-1 — Bedrock would reject the ARN.

    Repoint-only, never add: this same helper builds the ``embeddingModelId``
    ARN, and embedding models (``amazon.titan-embed-text-v2:0``) have no
    cross-region profiles, so ``eu.amazon.titan-…`` would be invalid.

    A geography-prefixed id is an **inference profile**, and this used to emit it
    as a foundation model, which names nothing. Confirmed against the live API:

        aws bedrock get-foundation-model \\
            --model-identifier us.anthropic.claude-sonnet-4-5-20250929-v1:0
        ResourceNotFoundException: Model not found.

    while ``get-inference-profile`` on the same id answers with
    ``arn:aws:bedrock:us-east-1:<account>:inference-profile/us.anthropic.…``. Note
    the account id, which a ``foundation-model`` ARN never carries and which this
    therefore needs — hence *account_id*. Without it the only ARN this could build
    is the invalid one, so it raises rather than quietly emitting that: Bedrock's own
    rejection arrives later and says "unable to assume the given role", which sends
    the reader to the IAM policy for a problem that is in the model ARN.
    """
    resolved = repoint_regional_prefix(model_id, region)
    if is_inference_profile_id(resolved):
        if not account_id:
            raise ValueError(
                f"{resolved} is a cross-region inference profile, whose ARN includes the "
                "account id, and the account id could not be resolved. Either pass one or "
                "use the plain on-demand model id (no us./eu./apac./global. prefix)."
            )
        return f"arn:aws:bedrock:{region}:{account_id}:inference-profile/{resolved}"
    return f"arn:aws:bedrock:{region}::foundation-model/{resolved}"


def _get_account_id(event: dict) -> str:
    """Resolve the current AWS account id (for constructing ARNs)."""
    try:
        return step_clients.account_id_for_event(event)
    except Exception:  # noqa: BLE001
        return ""


def _create_kb_role(
    iam_client,
    role_name: str,
    kb_config: dict,
    region: str,
    *,
    account_id: str,
    deployment_id: str = "",
    owner_sub: str = "",
    resource_tags: dict | None = None,
) -> tuple[str, bool]:
    """Create/reuse the KB role and return ``(arn, created_by_this_deploy)``."""
    if not account_id:
        raise ValueError("The target account id is required to create a safely scoped Knowledge Base trust policy.")
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": account_id,
                    },
                    "ArnLike": {
                        "aws:SourceArn": (f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*"),
                    },
                },
            }
        ],
    }

    try:
        resp = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Role for Bedrock Knowledge Base created by AgentCore Flow",
            # Tagged at creation so the already-exists branch below has something to
            # check. Without this the role is indistinguishable from a foreign one on
            # the very next deploy, and the check could only ever refuse.
            Tags=governed_tag_list(
                region,
                resource_tags,
                _deployment_binding_tags(deployment_id, owner_sub) if deployment_id else None,
            ),
            **create_role_kwargs(),
        )
        role_arn = resp["Role"]["Arn"]
        role_created = True
    except iam_client.exceptions.EntityAlreadyExistsException:
        # `_put_kb_role_policy` below runs on BOTH paths and replaces the role's
        # inline policy, so adopting a role on name alone would rewrite the
        # permissions of whatever already holds this name.
        _existing = iam_client.get_role(RoleName=role_name)["Role"]
        if deployment_id:
            assert_resource_bound_to_deployment(
                f"IAM role {role_name}",
                _existing.get("Tags"),
                deployment_id=deployment_id,
                owner_sub=owner_sub,
                region=region,
            )
            # It was not created by this invocation, but its exact deployment
            # binding proves it is still lifecycle-owned by this deployment and
            # must remain deletable from a reconstructed manifest.
            role_created = True
        else:
            assert_this_deployment_may_mutate(
                f"IAM role {role_name}",
                _existing.get("Tags"),
                region,
            )
            role_created = False
        role_arn = _existing["Arn"]
        # F-06: both arms above have proven ownership; retrofit the boundary before the
        # trust policy and inline policy of the adopted role are rewritten.
        ensure_role_boundary(iam_client, role_name, role=_existing)
        iam_client.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(trust_policy),
        )

    _put_kb_role_policy(
        iam_client,
        role_name,
        kb_config,
        region=region,
        account_id=account_id,
    )

    # IAM eventual consistency
    time.sleep(10)
    return role_arn, role_created


def _put_kb_role_policy(
    iam_client,
    role_name: str,
    kb_config: dict,
    *,
    region: str,
    account_id: str,
) -> None:
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
        if not region or not account_id:
            raise ValueError(
                "The target region and account are required to scope the OpenSearch Serverless Knowledge Base role."
            )
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                "Resource": (
                    kb_config.get("opensearchCollectionArn") or f"arn:aws:aoss:{region}:{account_id}:collection/*"
                ),
            }
        )

    # S3 Vectors permissions. The deployment step provisions/validates the
    # bucket and index; this Bedrock service role receives only the documented
    # index data-plane actions needed for ingestion and retrieval.
    if vector_store_type == "s3_vectors":
        s3v_arn = kb_config.get("s3VectorsBucketArn", "")
        index_name = kb_config.get("s3VectorsIndexName") or "bedrock-knowledge-base-default-index"
        if not s3v_arn:
            if not region or not account_id:
                raise ValueError(
                    "The target region and account are required to scope the S3 Vectors Knowledge Base role."
                )
            # The role must exist before the deterministic bucket/index does.
            # This interim pattern is account+region+name scoped and is
            # replaced with the exact live index ARN immediately afterwards.
            s3v_resources = [f"arn:aws:s3vectors:{region}:{account_id}:bucket/agentcore-kbvec-*/index/*"]
        else:
            live_index_arn = str(kb_config.get("s3VectorsIndexArn") or "")
            s3v_resources = [live_index_arn or f"{s3v_arn}/index/{index_name}"]
        statements.append(
            {
                "Effect": "Allow",
                "Action": [
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:QueryVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:GetIndex",
                ],
                "Resource": s3v_resources,
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
    if transform_lambda and transform_s3 and transform_s3.startswith("s3://"):
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
        jobs = _list_all_ingestion_jobs(bedrock_agent, kb_id, ds_id)
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
    summaries = list_all(
        bedrock_agent,
        "list_knowledge_bases",
        item_keys=("knowledgeBaseSummaries",),
        request={"maxResults": 100},
    )
    for kb in summaries:
        if kb.get("name") == kb_name and kb.get("status") in (
            "ACTIVE",
            "CREATING",
        ):
            return kb["knowledgeBaseId"]
    return None


def _ensure_oss_collection(
    region: str,
    deployment_id: str,
    kb_role_arn: str,
    kb_config: dict,
    store,
    dep_id: str,
    event: dict,
    *,
    owner_sub: str = "",
    resource_tags: dict | None = None,
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
    idx_name, index_schema = _oss_index_definition(kb_config)

    def _is_existing_conflict(exc: botocore.exceptions.ClientError) -> bool:
        return (
            exc.response.get("Error", {}).get("Code") in ("ConflictException", "ValidationException")
            and "exist" in str(exc).lower()
        )

    def _create_or_update_policy(
        *,
        name: str,
        policy_type: str,
        description: str,
        policy: str,
    ) -> bool:
        """Create one AOSS policy, or update only this deployment's exact policy.

        A name conflict is not idempotency proof.  Policies are account-global,
        untaggable resources, so the live description must bind both the stack
        and deployment before we are allowed to replace its policy document.
        Returns True when this call created the policy and False on an owned
        retry/update.
        """
        create = aoss.create_access_policy if policy_type == "data" else aoss.create_security_policy
        try:
            create(
                name=name,
                type=policy_type,
                description=description,
                policy=policy,
            )
            return True
        except botocore.exceptions.ClientError as e:
            if not _is_existing_conflict(e):
                raise

        detail = assert_aoss_policy_owned(
            aoss,
            name,
            policy_type,
            region,
            deployment_id,
        )
        version = detail.get("policyVersion")
        if not version:
            raise RuntimeError(
                f"OpenSearch Serverless {policy_type} policy {name} exists, "
                "but its version was absent so an ownership-safe update could "
                "not be performed."
            )
        update = aoss.update_access_policy if policy_type == "data" else aoss.update_security_policy
        update(
            name=name,
            type=policy_type,
            policyVersion=version,
            description=description,
            policy=policy,
        )
        return False

    # 1+2. security policies (encryption + network), scoped to this collection.
    enc_name = f"{coll_name}-enc"[:32]
    enc_description = aoss_policy_owner_description(
        region,
        f"Encryption policy for AgentCore KB {deployment_id[:12]}",
        deployment_id,
    )
    _create_or_update_policy(
        name=enc_name,
        policy_type="encryption",
        description=enc_description,
        policy=json.dumps(
            {
                "Rules": [
                    {
                        "ResourceType": "collection",
                        "Resource": [f"collection/{coll_name}"],
                    }
                ],
                "AWSOwnedKey": True,
            }
        ),
    )
    # Journal the graph immediately after the first untaggable policy exists.
    # If any later policy, collection, index, or KB call fails, failure cleanup
    # can now find and live-verify every deterministic member of this graph.
    if store is not None:
        store.record_resource(
            dep_id,
            {
                "type": "oss_collection",
                "name": coll_name,
                "region": region,
                "created_by_deployment": True,
            },
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
    _create_or_update_policy(
        name=f"{coll_name}-net"[:32],
        policy_type="network",
        description=aoss_policy_owner_description(
            region,
            f"Network policy for AgentCore KB {deployment_id[:12]}",
            deployment_id,
        ),
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
    _create_or_update_policy(
        name=f"{coll_name}-acc"[:32],
        policy_type="data",
        description=aoss_policy_owner_description(
            region,
            f"Access policy for AgentCore KB {deployment_id[:12]}",
            deployment_id,
        ),
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
    try:
        aoss.create_collection(
            name=coll_name,
            type="VECTORSEARCH",
            description=f"AgentCore KB vector store for {deployment_id[:12]}",
            tags=governed_lower_tag_list(
                region,
                resource_tags,
                _deployment_binding_tags(deployment_id, owner_sub),
            ),
        )
    except botocore.exceptions.ClientError as e:
        if not _is_existing_conflict(e):
            raise
        existing_collection = get_owned_aoss_collection(
            aoss,
            coll_name,
            region,
            deployment_id,
        )
        if not existing_collection:
            raise RuntimeError(
                f"OpenSearch Serverless reported that collection {coll_name} "
                "already exists, but it could not be found for ownership "
                "verification."
            ) from e
        if existing_collection.get("type") not in (None, "VECTORSEARCH"):
            raise RuntimeError(
                f"Owned OpenSearch Serverless collection {coll_name} has type "
                f"{existing_collection.get('type')}, not VECTORSEARCH."
            ) from e

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
                existing_index = aoss.get_index(
                    id=coll_id,
                    indexName=idx_name,
                ).get("indexSchema")
                try:
                    _assert_compatible_oss_index(
                        idx_name,
                        existing_index,
                        index_schema,
                    )
                except RuntimeError as schema_error:
                    raise schema_error from e
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
        if not bucket:
            raise ValueError(f"Invalid S3 URI: {s3_uri}")
        return f"arn:aws:s3:::{bucket}"
    raise ValueError(f"Invalid S3 URI: {s3_uri}")


def _deployment_binding_tags(
    deployment_id: str,
    owner_sub: str,
) -> dict[str, str]:
    tags = {DEPLOYMENT_ID_TAG_KEY: str(deployment_id)}
    if owner_sub:
        tags[OWNER_SUB_HASH_TAG_KEY] = owner_sub_hash(owner_sub)
    return tags


def _validate_target_arn(
    value: str,
    *,
    service: str,
    label: str,
    region: str,
    account_id: str,
    resource_prefix: str = "",
) -> None:
    """Require a complete ARN in the selected deployment account and region."""
    parts = str(value or "").split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or not parts[1] or parts[2] != service or not parts[5]:
        raise ResourceAccessRefused(f"Access refused for {label}: the supplied value is not a valid {service} ARN.")
    arn_region, arn_account = parts[3], parts[4]
    if arn_region != region:
        raise ResourceAccessRefused(
            f"Access refused for {label}: it is in region {arn_region or '<none>'}, "
            f"not the selected deployment region {region}."
        )
    if not account_id:
        raise ResourceAccessRefused(
            f"Access refused for {label}: the selected deployment account could not be resolved."
        )
    if arn_account != account_id:
        raise ResourceAccessRefused(f"Access refused for {label}: it is not in the selected deployment account.")
    if resource_prefix and not parts[5].startswith(resource_prefix):
        raise ResourceAccessRefused(
            f"Access refused for {label}: the ARN does not identify the expected resource type."
        )


def _authorize_live_tags(
    label: str,
    read: Callable[[], dict],
    extract: Callable[[dict], object],
    *,
    owner_sub: str,
    deployment_id: str,
    region: str,
    require_deployment_binding: bool = False,
) -> None:
    """Read live tags and apply the access or exact-deployment policy."""
    try:
        response = read()
    except Exception as exc:  # noqa: BLE001
        # An S3 bucket with no tags raises NoSuchTagSet instead of returning an
        # empty TagSet.  Empty is still a refusal, but it is not an ambiguous API
        # outage; every other read failure fails closed with a distinct message.
        if error_code(exc) == "NoSuchTagSet":
            response = {}
        else:
            raise ResourceAccessRefused(
                f"Access refused for {label}: its live authorization tags could not be read."
            ) from exc

    tags = extract(response)
    if require_deployment_binding:
        assert_resource_bound_to_deployment(
            label,
            tags,  # type: ignore[arg-type]
            deployment_id=deployment_id,
            owner_sub=owner_sub,
            region=region,
        )
        return
    assert_resource_access_allowed(
        label,
        tags,  # type: ignore[arg-type]
        owner_sub=owner_sub,
        deployment_id=deployment_id,
        region=region,
    )


def _s3_bucket_name(s3_uri: str, label: str) -> str:
    if not str(s3_uri or "").startswith("s3://"):
        raise ResourceAccessRefused(f"Access refused for {label}: a valid s3:// bucket URI is required.")
    bucket = str(s3_uri)[5:].split("/", 1)[0]
    if not bucket:
        raise ResourceAccessRefused(f"Access refused for {label}: the S3 bucket name is empty.")
    return bucket


def _authorize_s3_uri(
    event: dict,
    s3_uri: str,
    *,
    label: str,
    owner_sub: str,
    deployment_id: str,
    region: str,
    trusted_buckets: set[str],
) -> None:
    bucket = _s3_bucket_name(s3_uri, label)
    if bucket in trusted_buckets:
        return
    s3_client = step_clients.client(event, "s3")
    _authorize_live_tags(
        label,
        lambda: s3_client.get_bucket_tagging(Bucket=bucket),
        lambda response: response.get("TagSet"),
        owner_sub=owner_sub,
        deployment_id=deployment_id,
        region=region,
    )


def _authorize_existing_knowledge_base(
    bedrock_agent,
    response: dict,
    *,
    kb_id: str,
    owner_sub: str,
    deployment_id: str,
    region: str,
    account_id: str,
    require_deployment_binding: bool = False,
) -> None:
    detail = response.get("knowledgeBase") or {}
    arn = str(detail.get("knowledgeBaseArn") or "")
    if not arn:
        raise ResourceAccessRefused(f"Access refused for knowledge base {kb_id}: the live response contained no ARN.")
    _validate_target_arn(
        arn,
        service="bedrock",
        label=f"knowledge base {kb_id}",
        region=region,
        account_id=account_id,
        resource_prefix="knowledge-base/",
    )
    if arn.rsplit("/", 1)[-1] != kb_id:
        raise ResourceAccessRefused(
            f"Access refused for knowledge base {kb_id}: the live ARN does not match the requested Knowledge Base id."
        )
    _authorize_live_tags(
        f"knowledge base {kb_id}",
        lambda: bedrock_agent.list_tags_for_resource(resourceArn=arn),
        lambda tag_response: tag_response.get("tags"),
        owner_sub=owner_sub,
        deployment_id=deployment_id,
        region=region,
        require_deployment_binding=require_deployment_binding,
    )


def _hydrate_storage_from_existing_kb(
    kb_config: dict,
    response: dict,
) -> None:
    """Pin a retry's role policy to the storage already attached to its KB."""
    storage = (response.get("knowledgeBase") or {}).get("storageConfiguration") or {}
    actual_type = str(storage.get("type") or "").upper()
    if not actual_type:
        raise ResourceAccessRefused(
            "Access refused for the existing Knowledge Base: its live storage "
            "configuration was absent, so the execution role cannot be scoped "
            "to exact resources."
        )
    expected_type = {
        "s3_vectors": "S3_VECTORS",
        "opensearch_serverless": "OPENSEARCH_SERVERLESS",
        "rds": "RDS",
    }.get(str(kb_config.get("vectorStoreType") or "s3_vectors").lower())
    if actual_type and expected_type and actual_type != expected_type:
        raise ResourceAccessRefused(
            "Access refused for the existing Knowledge Base: its vector store "
            "type does not match this deployment request."
        )

    if actual_type == "S3_VECTORS":
        existing = storage.get("s3VectorsConfiguration") or {}
        mapping = {
            "s3VectorsBucketArn": existing.get("vectorBucketArn"),
            "s3VectorsIndexArn": existing.get("indexArn"),
            "s3VectorsIndexName": existing.get("indexName"),
        }
        required_fields = ("s3VectorsBucketArn", "s3VectorsIndexName")
    elif actual_type == "OPENSEARCH_SERVERLESS":
        existing = storage.get("opensearchServerlessConfiguration") or {}
        fields = existing.get("fieldMapping") or {}
        mapping = {
            "opensearchCollectionArn": existing.get("collectionArn"),
            "opensearchVectorIndexName": existing.get("vectorIndexName"),
            "opensearchVectorField": fields.get("vectorField"),
            "opensearchTextField": fields.get("textField"),
            "opensearchMetadataField": fields.get("metadataField"),
        }
        required_fields = (
            "opensearchCollectionArn",
            "opensearchVectorIndexName",
        )
    elif actual_type == "RDS":
        existing = storage.get("rdsConfiguration") or {}
        fields = existing.get("fieldMapping") or {}
        mapping = {
            "rdsResourceArn": existing.get("resourceArn"),
            "rdsCredentialsSecretArn": existing.get("credentialsSecretArn"),
            "rdsDatabaseName": existing.get("databaseName"),
            "rdsTableName": existing.get("tableName"),
            "rdsPrimaryKeyField": fields.get("primaryKeyField"),
            "rdsVectorField": fields.get("vectorField"),
            "rdsTextField": fields.get("textField"),
            "rdsMetadataField": fields.get("metadataField"),
        }
        required_fields = (
            "rdsResourceArn",
            "rdsCredentialsSecretArn",
            "rdsDatabaseName",
            "rdsTableName",
        )
    else:
        mapping = {}
        required_fields = ()

    for field, value in mapping.items():
        if not value:
            continue
        requested = kb_config.get(field)
        if requested and requested != value:
            raise ResourceAccessRefused(
                f"Access refused for the existing Knowledge Base: {field} "
                "does not match its live storage configuration."
            )
        kb_config[field] = value

    missing = [field for field in required_fields if not mapping.get(field)]
    if missing:
        raise ResourceAccessRefused(
            "Access refused for the existing Knowledge Base: its live storage "
            "configuration omitted required fields "
            f"{', '.join(missing)}, so the execution role cannot be scoped "
            "safely."
        )


def _authorize_create_new_resources(
    event: dict,
    kb_config: dict,
    *,
    owner_sub: str,
    deployment_id: str,
    region: str,
    account_id: str,
    trusted_s3_buckets: set[str],
) -> None:
    """Authorize every customer resource before creating or rewriting a role."""

    def _client(service: str):
        return step_clients.client(event, service)

    data_source_type = str(kb_config.get("dataSourceType") or "s3").lower()
    vector_store_type = str(kb_config.get("vectorStoreType") or "s3_vectors").lower()

    if data_source_type == "s3":
        _authorize_s3_uri(
            event,
            str(kb_config.get("s3BucketUri") or ""),
            label="Knowledge Base S3 data source",
            owner_sub=owner_sub,
            deployment_id=deployment_id,
            region=region,
            trusted_buckets=trusted_s3_buckets,
        )

    if vector_store_type == "s3_vectors":
        bucket_arn = str(kb_config.get("s3VectorsBucketArn") or "")
        if bucket_arn:
            _validate_target_arn(
                bucket_arn,
                service="s3vectors",
                label="S3 Vectors bucket",
                region=region,
                account_id=account_id,
                resource_prefix="bucket/",
            )
            if "/index/" in bucket_arn.split(":", 5)[5]:
                raise ResourceAccessRefused(
                    "Access refused for S3 Vectors bucket: the supplied ARN identifies an index, not a vector bucket."
                )
            s3v = _client("s3vectors")
            bucket_tags_response = s3v.list_tags_for_resource(resourceArn=bucket_arn)
            _authorize_live_tags(
                "S3 Vectors bucket",
                lambda: bucket_tags_response,
                lambda response: response.get("tags"),
                owner_sub=owner_sub,
                deployment_id=deployment_id,
                region=region,
            )
            index_arn = str(kb_config.get("s3VectorsIndexArn") or "")
            if index_arn and not index_arn.startswith(f"{bucket_arn.rstrip('/')}/index/"):
                raise ResourceAccessRefused(
                    "Access refused for S3 Vectors index: its ARN does not belong to the authorized vector bucket."
                )
            bucket_name = bucket_arn.rsplit("/", 1)[-1]
            index_name = str(kb_config.get("s3VectorsIndexName") or "") or "bedrock-knowledge-base-default-index"
            kb_config["s3VectorsIndexName"] = index_name
            index = _read_compatible_s3_vectors_index(
                s3v,
                bucket_name=bucket_name,
                bucket_arn=bucket_arn,
                index_name=index_name,
                requested_index_arn=index_arn,
            )
            if index is None:
                raise ValueError(
                    f"S3 Vectors index {index_name} does not exist on the "
                    "customer-supplied bucket. Pre-create the compatible "
                    "index, or leave s3VectorsBucketArn empty so the platform "
                    "can manage the bucket and index lifecycle."
                )
            live_index_arn = str(index["indexArn"])
            kb_config["s3VectorsIndexArn"] = live_index_arn

            # A platform lifecycle bucket is stricter than opted-in customer
            # inventory: an existing child index must carry the same exact
            # deployment/caller binding before retry logic may adopt it.
            try:
                assert_resource_bound_to_deployment(
                    "S3 Vectors bucket",
                    bucket_tags_response.get("tags"),
                    deployment_id=deployment_id,
                    owner_sub=owner_sub,
                    region=region,
                )
            except ResourceAccessRefused:
                pass
            else:
                _authorize_live_tags(
                    f"S3 Vectors index {index_name}",
                    lambda: s3v.list_tags_for_resource(resourceArn=live_index_arn),
                    lambda response: response.get("tags"),
                    owner_sub=owner_sub,
                    deployment_id=deployment_id,
                    region=region,
                    require_deployment_binding=True,
                )
    elif vector_store_type == "opensearch_serverless":
        collection_arn = str(kb_config.get("opensearchCollectionArn") or "")
        if collection_arn:
            _validate_target_arn(
                collection_arn,
                service="aoss",
                label="OpenSearch Serverless collection",
                region=region,
                account_id=account_id,
                resource_prefix="collection/",
            )
            aoss = _client("opensearchserverless")
            _authorize_live_tags(
                "OpenSearch Serverless collection",
                lambda: aoss.list_tags_for_resource(resourceArn=collection_arn),
                lambda response: response.get("tags"),
                owner_sub=owner_sub,
                deployment_id=deployment_id,
                region=region,
            )
            collection_resource = collection_arn.split(":", 5)[5]
            if not collection_resource.startswith("collection/"):
                raise ResourceAccessRefused(
                    "Access refused for OpenSearch Serverless collection: the ARN does not identify a collection."
                )
            collection_id = collection_resource.split("/", 1)[1]
            index_name, expected_schema = _oss_index_definition(kb_config)
            try:
                live_schema = aoss.get_index(
                    id=collection_id,
                    indexName=index_name,
                ).get("indexSchema")
            except Exception as exc:  # noqa: BLE001
                code = error_code(exc)
                if code in {
                    "NotFoundException",
                    "ResourceNotFoundException",
                    "ValidationException",
                } and (
                    code != "ValidationException"
                    or "not found" in str(exc).lower()
                    or "does not exist" in str(exc).lower()
                ):
                    raise ValueError(
                        f"OpenSearch Serverless index {index_name} does not exist in the customer collection."
                    ) from exc
                raise ResourceAccessRefused(
                    f"Access refused for OpenSearch Serverless index {index_name}: its live schema could not be read."
                ) from exc
            _assert_compatible_oss_index(
                index_name,
                live_schema,
                expected_schema,
            )
    elif vector_store_type == "rds":
        cluster_arn = str(kb_config.get("rdsResourceArn") or "")
        _validate_target_arn(
            cluster_arn,
            service="rds",
            label="Aurora cluster",
            region=region,
            account_id=account_id,
            resource_prefix="cluster:",
        )
        rds = _client("rds")
        pagination_request_field = "Marker"
        pagination_response_field = "NextMarker"
        _authorize_live_tags(
            "Aurora cluster",
            lambda: rds.list_tags_for_resource(ResourceName=cluster_arn),
            lambda response: response.get("TagList"),
            owner_sub=owner_sub,
            deployment_id=deployment_id,
            region=region,
        )

    transform_lambda = str(kb_config.get("transformationLambdaArn") or "")
    transform_s3 = str(kb_config.get("transformationS3Uri") or "")
    if transform_lambda:
        _validate_target_arn(
            transform_lambda,
            service="lambda",
            label="Knowledge Base transformation Lambda",
            region=region,
            account_id=account_id,
            resource_prefix="function:",
        )
        lambda_client = _client("lambda")
        _authorize_live_tags(
            "Knowledge Base transformation Lambda",
            lambda: lambda_client.list_tags(Resource=transform_lambda),
            lambda response: response.get("Tags"),
            owner_sub=owner_sub,
            deployment_id=deployment_id,
            region=region,
        )
        _authorize_s3_uri(
            event,
            transform_s3,
            label="Knowledge Base transformation S3 bucket",
            owner_sub=owner_sub,
            deployment_id=deployment_id,
            region=region,
            trusted_buckets=trusted_s3_buckets,
        )

    if kb_config.get("parsingStrategy") == "bedrock_data_automation":
        _authorize_s3_uri(
            event,
            str(kb_config.get("bdaSupplementalS3Uri") or ""),
            label="Bedrock Data Automation supplemental S3 bucket",
            owner_sub=owner_sub,
            deployment_id=deployment_id,
            region=region,
            trusted_buckets=trusted_s3_buckets,
        )

    kms_key_arn = str(kb_config.get("kmsKeyArn") or "")
    if kms_key_arn:
        _validate_target_arn(
            kms_key_arn,
            service="kms",
            label="Knowledge Base KMS key",
            region=region,
            account_id=account_id,
            resource_prefix="key/",
        )
        kms = _client("kms")
        _authorize_live_tags(
            "Knowledge Base KMS key",
            lambda: {
                "Tags": list_all(
                    kms,
                    "list_resource_tags",
                    item_keys=("Tags",),
                    request={"KeyId": kms_key_arn},
                    request_token=pagination_request_field,
                    response_token=pagination_response_field,
                    continuation_flag="Truncated",
                )
            },
            lambda response: [
                {
                    "Key": item.get("TagKey"),
                    "Value": item.get("TagValue"),
                }
                for item in response.get("Tags") or []
            ],
            owner_sub=owner_sub,
            deployment_id=deployment_id,
            region=region,
        )

    active_secret_fields: list[str] = []
    if data_source_type == "confluence":
        active_secret_fields.append("confluenceCredentialsSecretArn")
    elif data_source_type == "salesforce":
        active_secret_fields.append("salesforceCredentialsSecretArn")
    elif data_source_type == "sharepoint":
        active_secret_fields.append("sharePointCredentialsSecretArn")
    if vector_store_type == "rds":
        active_secret_fields.append("rdsCredentialsSecretArn")

    if active_secret_fields:
        secrets = _client("secretsmanager")
        for field in active_secret_fields:
            secret_arn = str(kb_config.get(field) or "")
            _validate_target_arn(
                secret_arn,
                service="secretsmanager",
                label="staged Knowledge Base credential",
                region=region,
                account_id=account_id,
                resource_prefix="secret:",
            )
            _authorize_live_tags(
                "staged Knowledge Base credential",
                lambda arn=secret_arn: secrets.describe_secret(SecretId=arn),
                lambda response: response.get("Tags"),
                owner_sub=owner_sub,
                deployment_id=deployment_id,
                region=region,
                require_deployment_binding=True,
            )


def handler(event: dict, context) -> dict:  # noqa: ARG001
    kb_config = copy.deepcopy(event.get("knowledge_base_config"))
    if not kb_config:
        return event  # No KB configured, pass through

    deployment_id = str(event.get("deployment_id") or "")
    owner_sub = str(event.get("owner_sub") or "")
    # P0-B governance tags, resolved once per deploy by `tag_policy_store.resolve_governance`
    # and carried in the Step Functions state. Every resource THIS step creates is billable or
    # auditable -- an OSS collection alone is ~$350/mo standing -- so cost attribution and ABAC
    # (ARCC cnt_6gBImtb08AJqCB) are exactly what these tags are for. `governed_*` validates them
    # against the namespaces the step role may stamp before any create call, so an unstampable
    # key fails the step instead of half-building a knowledge base.
    resource_tags = event.get("resource_tags")
    # Keep service clients, generated ARNs, ownership tags, and manifest rows on
    # one authoritative target. step_clients already reads target_region; using
    # the Lambda's home region here caused cross-region deploys to tag their
    # account-global IAM role as belonging to the wrong stack instance.
    region = event.get("target_region") or _get_env(
        "APP_AWS_REGION",
        _get_env("AWS_REGION", "us-east-1"),
    )

    store = None
    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.KNOWLEDGE_BASE, DeploymentStatusEnum.IN_PROGRESS)
    except Exception:
        logger.exception("Failed to update step status for KB step")

    kb_mode = str(kb_config.get("kbMode") or "existing").lower()
    artifact_bucket = ""
    if kb_mode == "create_new":
        artifact_bucket = step_clients.artifacts_bucket_for_event(
            event,
            platform_bucket=_get_env("ARTIFACTS_BUCKET_NAME", ""),
        )
    if (
        kb_mode == "create_new"
        and kb_config.get("parsingStrategy") == "bedrock_data_automation"
        and not kb_config.get("bdaSupplementalS3Uri")
    ):
        if not artifact_bucket:
            raise RuntimeError(
                "Bedrock Data Automation parsing requires a deployment artifacts bucket for supplemental storage"
            )
        # Populate the one authoritative config before both the KB-role policy
        # and CreateKnowledgeBase params are built. Otherwise cross-account
        # deploys grant/use the platform account's bucket from Lambda env.
        kb_config["bdaSupplementalS3Uri"] = f"s3://{artifact_bucket}"
    foundation_model_id = kb_config.get("foundationModelId", "us.anthropic.claude-sonnet-5")
    # Resolved once: the default above and every other model id in this step are
    # geography-prefixed, so their ARNs are inference profiles and carry the account.
    account_id = _get_account_id(event)
    foundation_model_arn = _build_model_arn(region, foundation_model_id, account_id)

    bedrock_agent = step_clients.client(event, "bedrock-agent")

    if kb_mode == "existing":
        kb_id = kb_config.get("knowledgeBaseId", "").strip()
        if not kb_id:
            raise ValueError("knowledgeBaseId is required for existing KB mode")

        # Validate KB exists
        try:
            resp = bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
        except bedrock_agent.exceptions.ResourceNotFoundException:
            raise ValueError(f"Knowledge Base {kb_id} not found") from None
        status = resp.get("knowledgeBase", {}).get("status", "")
        if status != "ACTIVE":
            raise RuntimeError(f"Knowledge Base {kb_id} is not ACTIVE (status: {status})")
        _authorize_existing_knowledge_base(
            bedrock_agent,
            resp,
            kb_id=kb_id,
            owner_sub=owner_sub,
            deployment_id=deployment_id,
            region=region,
            account_id=account_id,
        )
        logger.warning("Validated existing KB: %s (status: %s)", kb_id, status)

        event["knowledge_base_result"] = {
            "kb_id": kb_id,
            "created_by_flow": False,
            "foundation_model_arn": foundation_model_arn,
        }
        if store is not None:
            store.record_resource(
                deployment_id,
                {
                    "type": "knowledge_base",
                    "id": kb_id,
                    "region": region,
                    "created_by_deployment": False,
                },
            )
        return event

    if kb_mode == "create_new":
        kb_name = kb_config.get("kbName", f"agentcore-kb-{deployment_id[:8]}")
        kb_description = kb_config.get("kbDescription", "Knowledge Base created by AgentCore Flow")
        embedding_model_id = kb_config.get("embeddingModelId", "amazon.titan-embed-text-v2:0")
        embedding_model_arn = _build_model_arn(region, embedding_model_id, account_id)

        # A supplied ARN is inventory, not authority.  Read every customer
        # resource's live opt-in tags before the first IAM role is created or
        # rewritten.  The registered target artifacts bucket is trusted because
        # registration already proves its account/region/ownership server-side.
        _authorize_create_new_resources(
            event,
            kb_config,
            owner_sub=owner_sub,
            deployment_id=deployment_id,
            region=region,
            account_id=account_id,
            trusted_s3_buckets={artifact_bucket} if artifact_bucket else set(),
        )

        # Check for an idempotent retry before touching IAM.  Name equality is
        # not ownership: the existing KB must carry this exact stack,
        # deployment, and caller binding.
        kb_id = _find_existing_kb(bedrock_agent, kb_name)
        if kb_id:
            existing_kb = bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
            _authorize_existing_knowledge_base(
                bedrock_agent,
                existing_kb,
                kb_id=kb_id,
                owner_sub=owner_sub,
                deployment_id=deployment_id,
                region=region,
                account_id=account_id,
                require_deployment_binding=True,
            )
            _hydrate_storage_from_existing_kb(kb_config, existing_kb)
            # Hydration replaces request inventory with the backing resources
            # attached to the live KB.  Re-authorize that authoritative state:
            # an exact-owned KB can still have its storage configuration changed
            # out of band, and its tags are not authority over a different
            # bucket, collection, cluster, key, function, or secret.
            _authorize_create_new_resources(
                event,
                kb_config,
                owner_sub=owner_sub,
                deployment_id=deployment_id,
                region=region,
                account_id=account_id,
                trusted_s3_buckets=({artifact_bucket} if artifact_bucket else set()),
            )

        # Step 1: Create/reuse the exact deployment-bound KB execution role.
        iam_client = step_clients.client(event, "iam")
        role_name = f"AgentCoreKBRole-{deployment_id[:8]}"
        role_arn, role_created = _create_kb_role(
            iam_client,
            role_name,
            kb_config,
            region,
            account_id=account_id,
            deployment_id=deployment_id,
            owner_sub=owner_sub,
            resource_tags=resource_tags,
        )
        logger.warning("KB role created: %s", role_arn)
        # Manifest: record the KB exec role for generic teardown. Best-effort.
        if store is not None:
            store.record_resource(
                deployment_id,
                {
                    "type": "iam_role",
                    "name": role_name,
                    "region": region,
                    "created_by_deployment": role_created,
                },
            )

        # Step 2: Reuse the exact deployment's KB or create it.
        if kb_id:
            logger.warning("Found existing KB with name %s: %s (reusing)", kb_name, kb_id)
            if store is not None:
                store.record_resource(
                    deployment_id,
                    {
                        "type": "knowledge_base",
                        "id": kb_id,
                        "region": region,
                        # Exact DeploymentId/caller tags were re-read above.
                        # This is a retry of our resource, not a customer-owned
                        # existing KB, so reconstructed cleanup must delete it.
                        "created_by_deployment": True,
                    },
                )
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
                auto_managed_vector_bucket = not bool(vec_arn)
                vec_idx = kb_config.get("s3VectorsIndexName") or "bedrock-knowledge-base-default-index"
                kb_config["s3VectorsIndexName"] = vec_idx
                s3v = step_clients.client(event, "s3vectors")
                if not vec_arn:
                    # Auto-managed mode: create our own vector bucket. Bucket
                    # names: 3-63 chars, lowercase alphanum + hyphen.
                    vec_bucket_name = f"agentcore-kbvec-{deployment_id[:12]}"
                    try:
                        s3v.create_vector_bucket(
                            vectorBucketName=vec_bucket_name,
                            tags=governed_tags(
                                region,
                                resource_tags,
                                _deployment_binding_tags(
                                    deployment_id,
                                    owner_sub,
                                ),
                            ),
                        )
                        logger.warning("Auto-created S3 Vectors bucket %s", vec_bucket_name)
                    except ClientError as vb_err:
                        is_conflict = (
                            error_code(vb_err)
                            in {
                                "ConflictException",
                                "ValidationException",
                            }
                            and "exist" in str(vb_err).lower()
                        )
                        if not is_conflict:
                            raise
                        # A deterministic name collision is not idempotency
                        # proof.  Re-read exact deployment ownership before
                        # creating an index or granting the KB role access.
                        existing_bucket = s3v.get_vector_bucket(vectorBucketName=vec_bucket_name)
                        existing_arn = str((existing_bucket.get("vectorBucket") or {}).get("vectorBucketArn") or "")
                        if not existing_arn:
                            raise ResourceAccessRefused(
                                "Access refused for the existing S3 Vectors bucket: its live response contained no ARN."
                            ) from vb_err
                        _authorize_live_tags(
                            "S3 Vectors bucket",
                            lambda: s3v.list_tags_for_resource(resourceArn=existing_arn),
                            lambda response: response.get("tags"),
                            owner_sub=owner_sub,
                            deployment_id=deployment_id,
                            region=region,
                            require_deployment_binding=True,
                        )
                    # Journal immediately after creation or exact-owned
                    # conflict recovery, before any follow-up read/index call
                    # can fail.
                    if store is not None:
                        store.record_resource(
                            deployment_id,
                            {
                                "type": "s3_vectors_bucket",
                                "name": vec_bucket_name,
                                "region": region,
                                "created_by_deployment": True,
                            },
                        )
                    desc = s3v.get_vector_bucket(vectorBucketName=vec_bucket_name)
                    vec_arn = str((desc.get("vectorBucket") or {}).get("vectorBucketArn") or "")
                    if not vec_arn:
                        raise RuntimeError("The created S3 Vectors bucket response contained no ARN.")
                    kb_config["s3VectorsBucketArn"] = vec_arn
                else:
                    vec_bucket_name = vec_arn.rsplit("/", 1)[-1]
                # Ensure the index exists (Titan Embed Text v2 = 1024 dims,
                # cosine) and pin the live ARN/schema. A customer bucket is
                # preflighted before IAM mutation above, but this second read
                # catches deletion or replacement between authorization and use.
                index = _read_compatible_s3_vectors_index(
                    s3v,
                    bucket_name=vec_bucket_name,
                    bucket_arn=vec_arn,
                    index_name=vec_idx,
                    requested_index_arn=str(kb_config.get("s3VectorsIndexArn") or ""),
                )
                index_created_now = False
                index_conflict_recovered = False
                if index is None:
                    if not auto_managed_vector_bucket:
                        raise ValueError(
                            f"S3 Vectors index {vec_idx} does not exist on the "
                            "customer-supplied bucket. Pre-create the compatible "
                            "index, or leave s3VectorsBucketArn empty so the "
                            "platform can manage the bucket and index lifecycle."
                        )
                    logger.warning(
                        "Auto-creating S3 Vectors index '%s' on bucket %s",
                        vec_idx,
                        vec_bucket_name,
                    )
                    try:
                        s3v.create_index(
                            vectorBucketName=vec_bucket_name,
                            indexName=vec_idx,
                            dataType="float32",
                            dimension=1024,
                            distanceMetric="cosine",
                            tags=governed_tags(
                                region,
                                resource_tags,
                                _deployment_binding_tags(
                                    deployment_id,
                                    owner_sub,
                                ),
                            ),
                        )
                        index_created_now = True
                    except ClientError as ix_err:
                        if error_code(ix_err) != "ConflictException":
                            raise
                        index_conflict_recovered = True
                    index = _read_compatible_s3_vectors_index(
                        s3v,
                        bucket_name=vec_bucket_name,
                        bucket_arn=vec_arn,
                        index_name=vec_idx,
                    )
                    if index is None:
                        raise RuntimeError(
                            f"S3 Vectors index {vec_idx} was not readable after its create or conflict response."
                        )

                live_index_arn = str(index["indexArn"])
                if auto_managed_vector_bucket and (not index_created_now or index_conflict_recovered):
                    _authorize_live_tags(
                        f"S3 Vectors index {vec_idx}",
                        lambda: s3v.list_tags_for_resource(resourceArn=live_index_arn),
                        lambda response: response.get("tags"),
                        owner_sub=owner_sub,
                        deployment_id=deployment_id,
                        region=region,
                        require_deployment_binding=True,
                    )
                kb_config["s3VectorsIndexArn"] = live_index_arn

            # OpenSearch Serverless: Bedrock requires a pre-existing collection ARN
            # (no auto-provision from storage config, unlike S3 Vectors). If the
            # caller didn't supply one, self-provision the collection + index here
            # and record it to the manifest for teardown (standing billable resource).
            elif kb_config.get("vectorStoreType") == "opensearch_serverless" and not kb_config.get(
                "opensearchCollectionArn"
            ):
                _ensure_oss_collection(
                    region,
                    deployment_id,
                    role_arn,
                    kb_config,
                    store,
                    deployment_id,
                    event,
                    owner_sub=owner_sub,
                    resource_tags=resource_tags,
                )

            # Least privilege: the role was created BEFORE the vector store
            # existed, so its interim policy used naming-convention patterns
            # (agentcore-kbvec-* / collection/*). Now that kb_config carries
            # the exact bucket/collection ARN, re-put the policy so every
            # statement is scoped to the real resource.  Failure is fatal:
            # leaving the interim collection/* or agentcore-kbvec-* policy in
            # place would turn a deployment error into persistent broad access.
            _put_kb_role_policy(
                iam_client,
                role_name,
                kb_config,
                region=region,
                account_id=account_id,
            )

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
                "tags": governed_tags(
                    region,
                    resource_tags,
                    _deployment_binding_tags(
                        deployment_id,
                        owner_sub,
                    ),
                ),
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
                    {
                        "type": "knowledge_base",
                        "id": kb_id,
                        "region": region,
                        "created_by_deployment": True,
                    },
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
                "modelArn": _build_model_arn(region, parsing_model_id, account_id),
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
            existing = _list_all_data_sources(bedrock_agent, kb_id)
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
