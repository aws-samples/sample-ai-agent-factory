#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Deploy / destroy / cleanup engine for every workshop CloudFormation stack
# listed in contentspec.yaml. It creates an assets bucket, syncs assets/,
# static/cfn/ (nested templates), and source/ (IDE workshop code) to S3, then
# deploys the stacks in order (and tears them down in reverse).
#
# Self-paced participants: prefer the wrapper ./scripts/self-service-deploy.sh,
# which preflights region/credentials and prints the Code Editor IDE URL after
# deploy. Use this script directly for cleanup (./deploy-cfn.sh destroy) and for
# local testing in your own AWS account.

set -euo pipefail

CONTENTSPEC="contentspec.yaml"

OPERATION="${1:-}"

if [[ "$OPERATION" != "deploy" && "$OPERATION" != "destroy" && "$OPERATION" != "cleanup" ]]; then
  echo "Usage: $0 <deploy|destroy|cleanup>"
  exit 1
fi

if [[ ! -f "$CONTENTSPEC" ]]; then
  echo "Error: $CONTENTSPEC not found in current directory."
  exit 1
fi

# Off by default: a participant whose first stack fails wants the run to stop, not
# to spend another 30 minutes watching four dependent stacks fail for the same
# reason. Set DEPLOY_CONTINUE_ON_FAILURE=1 when *validating* the templates or the
# deploy IAM policy, where the goal is the opposite -- surface every failure in one
# pass instead of one redeploy cycle per missing permission. `destroy`/`cleanup`
# already behave this way unconditionally; this makes `deploy` able to.
CONTINUE_ON_FAILURE="${DEPLOY_CONTINUE_ON_FAILURE:-0}"
DEPLOY_FAILED_STACKS=""

# Record a stack failure. Exits immediately unless CONTINUE_ON_FAILURE=1, in which
# case the caller is expected to `continue` to the next stack. This cannot itself
# `continue` -- a `continue` inside a function body does not affect the caller's
# loop -- so every call site pairs it with one.
record_stack_failure() {
  DEPLOY_FAILED_STACKS="${DEPLOY_FAILED_STACKS}$1 ($2)\n"
  if [[ "$CONTINUE_ON_FAILURE" != "1" ]]; then
    echo ""
    echo "Rollback is disabled. Run with 'cleanup' to remove broken stacks before retrying deploy."
    exit 1
  fi
  echo "DEPLOY_CONTINUE_ON_FAILURE=1 -- recorded and continuing to the next stack."
  echo "Note: stacks that import this one's outputs will now fail on the missing"
  echo "      export rather than on a problem of their own."
}

# Resolve the target region. Order: AWS_REGION > AWS_DEFAULT_REGION >
# `aws configure get region`. The trailing `|| true` keeps `aws configure get`
# (which exits non-zero when no region is configured) from killing the script
# under `set -euo pipefail`. Refuse only when empty; never assume a default.
REGION="${AWS_REGION:-}"
[[ -n "$REGION" ]] || REGION="${AWS_DEFAULT_REGION:-}"
[[ -n "$REGION" ]] || REGION="$(aws configure get region 2>/dev/null || true)"
if [[ -z "$REGION" ]]; then
  echo "Error: no AWS region resolved. Set AWS_REGION, AWS_DEFAULT_REGION, or run"
  echo "       'aws configure set region <region>' and re-run."
  exit 1
fi
# Propagate so every child aws call targets the same region.
export AWS_REGION="$REGION"

# Derive S3 bucket name from the current directory name and region
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
DIR_NAME=$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9-]/-/g')
# S3 caps a bucket name at 63 characters, and the directory name is the one part
# of this that a participant does not choose: the public repository's own
# workshop directory is `workshop-building-agentic-ai-platform`, which makes the
# derived name 71 characters and failed the documented clone path on the very
# first deploy step with `InvalidBucketName`. A shorter checkout directory
# happened to fit (62), which is why it went unnoticed. Trim the directory
# component to the room that is left, and only when it does not fit, so an
# already-deployed environment keeps the bucket name it has and `destroy` still
# finds it.
BUCKET_SUFFIX="-${ACCOUNT_ID}-${REGION}"
MAX_DIR=$(( 63 - ${#BUCKET_SUFFIX} - 11 ))   # 11 = len("cfn-deploy-")
if (( MAX_DIR < 3 )); then
  echo "Error: region '$REGION' leaves no room for a derived bucket name."
  echo "       Set S3_BUCKET to a bucket you own and re-run."
  exit 1
fi
if (( ${#DIR_NAME} > MAX_DIR )); then
  DIR_NAME="${DIR_NAME:0:MAX_DIR}"
  # A bucket name may not end in '-', and the cut can land on one (or on several).
  while [[ "$DIR_NAME" == *- ]]; do DIR_NAME="${DIR_NAME%-}"; done
fi
# Overridable so that a participant whose derived name has already been taken in
# the global S3 namespace has a way out (see the ownership check below).
S3_BUCKET="${S3_BUCKET:-cfn-deploy-${DIR_NAME}${BUCKET_SUFFIX}}"

# Ensure the bucket exists in the correct region AND that we are the ones who own
# it. The name above is fully predictable, and S3 bucket names are one global
# namespace, so any other AWS account can create this exact name before you do.
# A bare `head-bucket` only answers "does this name resolve" -- if a squatter had
# taken the name and left the bucket writable, the deploy would upload the nested
# templates into their bucket and CloudFormation would then READ the templates
# back from it, which hands the squatter control over what gets created in your
# account. `--expected-bucket-owner` turns that into a 403 instead.
if HEAD_ERR=$(aws s3api head-bucket --bucket "$S3_BUCKET" --region "$REGION" \
     --expected-bucket-owner "$ACCOUNT_ID" 2>&1 >/dev/null); then
  : # Exists, and this account owns it.
elif printf '%s' "$HEAD_ERR" | grep -q '404' && [[ "$OPERATION" == "destroy" ]]; then
  # Nothing to create for a teardown: `destroy` uploads nothing (CloudFormation
  # deletes from the template it already stored) and removes this bucket at the
  # end. Creating it here made a re-run of `destroy` create an empty bucket only
  # to delete it again -- and if anything in between exited early, leave it
  # behind, which is the exact residue this bucket removal exists to prevent.
  echo "Deploy bucket '$S3_BUCKET' is already gone -- nothing to upload for a destroy."
elif printf '%s' "$HEAD_ERR" | grep -q '404'; then
  echo "Creating S3 bucket '$S3_BUCKET' in $REGION ..."
  if [[ "$REGION" == "us-east-1" ]]; then
    # us-east-1 must NOT pass a LocationConstraint (the API rejects it).
    aws s3api create-bucket --bucket "$S3_BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$S3_BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"
  fi
  # This bucket only ever serves CloudFormation and the IDE bootstrap, both of
  # which read it with your credentials. Nothing in it should be public.
  aws s3api put-public-access-block --bucket "$S3_BUCKET" --region "$REGION" \
    --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
else
  # 403, or anything else we did not expect. Do NOT fall through to create-bucket:
  # that would fail with a bare BucketAlreadyExists and hide the real problem.
  echo "ERROR: cannot confirm that account $ACCOUNT_ID owns the deploy bucket"
  echo "       '$S3_BUCKET'. S3 said:"
  echo "       $HEAD_ERR"
  echo ""
  echo "       If another AWS account has taken this name, do NOT deploy into it."
  echo "       Rename the checkout directory (the bucket name is derived from it)"
  echo "       and re-run, or create a bucket you own and set S3_BUCKET yourself."
  exit 1
fi

echo "Using S3 bucket: $S3_BUCKET"

# Verify every pinned container image resolves BEFORE creating any stack. A tag
# that does not exist in its registry is invisible to cfn-lint and checkov; it
# only surfaces as ECS `CannotPullContainerError` about 20 minutes in, leaving a
# half-created stack behind. Definitive 404s abort; an unreachable registry only
# warns, so a restricted network cannot block a deploy.
if [[ "$OPERATION" == "deploy" && -f scripts/verify-images.py ]]; then
  echo "Preflight: verifying pinned container image tags ..."
  if IMG_OUT=$(python3 scripts/verify-images.py . 2>&1); then
    echo "All pinned image tags resolve."
  elif grep -q "HTTP 404" <<<"$IMG_OUT"; then
    echo "$IMG_OUT"
    echo "Error: a pinned container image tag does not exist in its registry."
    echo "Fix the tag before deploying — ECS would fail with CannotPullContainerError."
    exit 1
  else
    echo "$IMG_OUT"
    echo "Warning: could not reach one or more registries; continuing."
  fi
  echo ""
fi

# The five participant IAM policies exist twice: as the JSON files contentspec
# attaches to WSParticipantRole, and embedded as managed policies inside
# code-editor.yaml (CloudFormation cannot read a sibling file). Nothing on the
# IDE assumes WSParticipantRole, so the embedded copies are the ones actually
# enforced — if they drift, the workshop silently runs on stale permissions.
# This aborts, unlike the assets check below: a drifted copy means the deploy
# would grant something other than what was reviewed.
if [[ "$OPERATION" == "deploy" && -f scripts/verify-ide-policy-parity.py ]]; then
  echo "Preflight: verifying IDE participant policy parity ..."
  if ! python3 scripts/verify-ide-policy-parity.py; then
    echo "Error: the participant policies embedded in static/cfn/code-editor.yaml"
    echo "       do not match static/cfn/workshop-iam-policy-*.json."
    echo "       Run: scripts/verify-ide-policy-parity.py --sync"
    exit 1
  fi
  echo ""
fi

# assets/ is mirrored into the shared Workshop Studio assets bucket, which a git
# push does NOT update. A fix landing in static/ while the assets copy stays
# stale means the same file is scanned twice and fixed once — that is exactly how
# 7 already-fixed Checkov HIGHs kept being reported against assets/cfn/. Warn
# loudly here; do not abort, because a local deploy syncs its own copies and is
# unaffected by bucket drift.
if [[ "$OPERATION" == "deploy" && -f scripts/verify-assets-parity.py ]]; then
  echo "Preflight: verifying assets/ parity with static/ and source/ ..."
  if ! python3 scripts/verify-assets-parity.py .; then
    echo "Warning: assets/ has drifted. Re-sync the Workshop Studio assets bucket"
    echo "         before relying on a Workshop Studio build."
  fi
  echo ""
fi

# Nothing below is needed to DELETE a stack: CloudFormation deletes from the
# template it already stored, never from the bucket. Uploading the whole of
# static/cfn/ and source/ as the first act of a teardown was pure noise -- and with
# the bucket removal at the end of `destroy`, it would upload files only to delete
# them again a few minutes later. `cleanup` keeps the syncs, because it is a repair
# in the middle of a deploy cycle.
if [[ "$OPERATION" != "destroy" ]]; then

# Sync assets directory to S3 if it exists
if [[ -d "assets" ]]; then
  echo "Syncing assets/ to s3://$S3_BUCKET/assets/ ..."
  aws s3 sync assets/ "s3://$S3_BUCKET/assets/"
  echo "Assets synced."
  echo ""
fi

# Sync static/cfn/ to S3 so nested CloudFormation templates resolve correctly.
# Workshop Studio does this automatically; for local testing we replicate the layout.
if [[ -d "static/cfn" ]]; then
  echo "Syncing static/cfn/ to s3://$S3_BUCKET/assets/cfn/ (nested templates) ..."
  aws s3 sync static/cfn/ "s3://$S3_BUCKET/assets/cfn/"
  echo "Nested CFN templates synced."
  echo ""
fi

# Sync source/ to S3 so the CodeEditor SSM doc can pull notebooks, scripts, and
# CDK code into /workshop/ on boot. Workshop Studio does this automatically;
# for local testing we replicate the same layout.
if [[ -d "source" ]]; then
  echo "Syncing source/ to s3://$S3_BUCKET/assets/source/ (workshop code for IDE) ..."
  # Exclude build/venv artifacts at ANY depth. The leading "*/" globs are
  # required because plain ".venv/*" only matches a top-level .venv — a nested
  # one (e.g. source/module-4/cdk/.venv/) would otherwise upload thousands of
  # botocore files and bloat the participant IDE.
  aws s3 sync source/ "s3://$S3_BUCKET/assets/source/" \
    --exclude "*.pyc" \
    --exclude "__pycache__/*" --exclude "*/__pycache__/*" \
    --exclude ".venv/*" --exclude "*/.venv/*" \
    --exclude "node_modules/*" --exclude "*/node_modules/*" \
    --exclude "cdk.out/*" --exclude "*/cdk.out/*" \
    --exclude ".pytest_cache/*" --exclude "*/.pytest_cache/*" \
    --exclude "*.egg-info/*" --exclude "*/*.egg-info/*" \
    --exclude ".ipynb_checkpoints/*" --exclude "*/.ipynb_checkpoints/*" \
    --exclude "*.state.json" --exclude ".state.json"
  echo "Source code synced."
  echo ""
fi

fi  # OPERATION != destroy

TEMPLATES=$(yq -r '.infrastructure.cloudformationTemplates // [] | length' "$CONTENTSPEC")

if [[ "$TEMPLATES" -eq 0 ]]; then
  echo "No CloudFormation templates found in $CONTENTSPEC."
  exit 0
fi

echo "Found $TEMPLATES CloudFormation template(s). Operation: $OPERATION"
echo ""

if [[ "$OPERATION" == "deploy" ]]; then
  for ((i = 0; i < TEMPLATES; i++)); do
    TEMPLATE_LOCATION=$(yq -r ".infrastructure.cloudformationTemplates[$i].templateLocation" "$CONTENTSPEC")
    LABEL=$(yq -r ".infrastructure.cloudformationTemplates[$i].label" "$CONTENTSPEC")
    STACK_NAME="${LABEL}"

    if [[ ! -f "$TEMPLATE_LOCATION" ]]; then
      echo "Error: Template file not found: $TEMPLATE_LOCATION"
      exit 1
    fi

    # Build parameter overrides from contentspec
    PARAM_OVERRIDES=()
    PARAM_COUNT=$(yq -r ".infrastructure.cloudformationTemplates[$i].parameters // [] | length" "$CONTENTSPEC")
    for ((p = 0; p < PARAM_COUNT; p++)); do
      PARAM_NAME=$(yq -r ".infrastructure.cloudformationTemplates[$i].parameters[$p].templateParameter" "$CONTENTSPEC")
      PARAM_VALUE=$(yq -r ".infrastructure.cloudformationTemplates[$i].parameters[$p].defaultValue" "$CONTENTSPEC")

      # Resolve magic variables ({{.Something}}) — substitute known ones, skip unknown
      PARAM_VALUE="${PARAM_VALUE//\{\{.AssetsBucketName\}\}/$S3_BUCKET}"
      PARAM_VALUE="${PARAM_VALUE//\{\{.AssetsBucketPrefix\}\}/assets/}"
      PARAM_VALUE="${PARAM_VALUE//\{\{.TeamId\}\}/${TEAM_ID:-d30035ed-7bef-405a-8741-6144faa15e17}}"
      PARAM_VALUE="${PARAM_VALUE//\{\{.TeamIndex\}\}/0}"

      # Skip if unresolvable magic variables remain
      if [[ "$PARAM_VALUE" == *'{{.'*'}}'* ]]; then
        continue
      fi

      PARAM_OVERRIDES+=("${PARAM_NAME}=${PARAM_VALUE}")
    done

    # code-editor: if the account has NO default VPC (deleted as a security
    # baseline in many enterprise landing zones), the instance/SG cannot fall
    # back to it. Auto-detect and supply the LLM-gateway VPC's public subnet
    # instead, or honor explicit CODE_EDITOR_VPC_ID/CODE_EDITOR_SUBNET_ID.
    if [[ "$STACK_NAME" == "code-editor" ]]; then
      if [[ -n "${CODE_EDITOR_VPC_ID:-}" && -n "${CODE_EDITOR_SUBNET_ID:-}" ]]; then
        PARAM_OVERRIDES+=("VpcId=${CODE_EDITOR_VPC_ID}" "SubnetId=${CODE_EDITOR_SUBNET_ID}")
        echo "code-editor: using explicit network (CODE_EDITOR_VPC_ID/CODE_EDITOR_SUBNET_ID)."
      else
        DEFAULT_VPC=$(aws ec2 describe-vpcs --region "$REGION" \
          --filters Name=isDefault,Values=true \
          --query 'Vpcs[0].VpcId' --output text 2>/dev/null || true)
        if [[ -z "$DEFAULT_VPC" || "$DEFAULT_VPC" == "None" ]]; then
          echo "code-editor: no default VPC in $REGION — reusing the workshop LLM-gateway VPC."
          CE_VPC=$(aws cloudformation describe-stacks --region "$REGION" \
            --stack-name workshop-llm-gateway-stack \
            --query "Stacks[0].Outputs[?OutputKey=='VpcId'].OutputValue" --output text 2>/dev/null || true)
          # Public subnet = one whose route table has an IGW route.
          CE_SUBNET=""
          if [[ -n "$CE_VPC" && "$CE_VPC" != "None" ]]; then
            for sn in $(aws ec2 describe-subnets --region "$REGION" \
                --filters "Name=vpc-id,Values=$CE_VPC" \
                --query 'Subnets[].SubnetId' --output text 2>/dev/null); do
              RT_IGW=$(aws ec2 describe-route-tables --region "$REGION" \
                --filters "Name=association.subnet-id,Values=$sn" \
                --query "RouteTables[].Routes[?starts_with(GatewayId||'','igw-')]|[]" \
                --output text 2>/dev/null || true)
              [[ -n "$RT_IGW" ]] && CE_SUBNET="$sn" && break
            done
          fi
          if [[ -n "$CE_VPC" && -n "$CE_SUBNET" ]]; then
            PARAM_OVERRIDES+=("VpcId=${CE_VPC}" "SubnetId=${CE_SUBNET}")
            echo "code-editor: VpcId=$CE_VPC SubnetId=$CE_SUBNET"
          else
            echo "ERROR: no default VPC in $REGION and no public subnet found in the"
            echo "       workshop-llm-gateway-stack VPC. Set CODE_EDITOR_VPC_ID and"
            echo "       CODE_EDITOR_SUBNET_ID (a public subnet) and re-run."
            exit 1
          fi
        fi
      fi
    fi

    if [[ ${#PARAM_OVERRIDES[@]} -gt 0 ]]; then
      echo "Parameter overrides:"
      for OVERRIDE in "${PARAM_OVERRIDES[@]}"; do
        echo "  $OVERRIDE"
      done
    fi

    echo "[$((i + 1))/$TEMPLATES] Deploying stack '$STACK_NAME' from $TEMPLATE_LOCATION ..."

    # Templates with nested stacks (TemplateURL) must use create/update-stack with
    # --template-url pointing to S3, because `aws cloudformation deploy --s3-bucket`
    # repacks the template into a single file, breaking nested TemplateURL references.
    HAS_NESTED=$(grep -c 'TemplateURL' "$TEMPLATE_LOCATION" 2>/dev/null || true)

    if [[ "$HAS_NESTED" -gt 0 ]]; then
      # Nested stacks: use create-stack/update-stack with S3 template URL.
      # Derive the S3 endpoint suffix from the partition: China regions use
      # amazonaws.com.cn, everything else uses amazonaws.com.
      case "$REGION" in
        cn-*) S3_ENDPOINT_SUFFIX="amazonaws.com.cn" ;;
        *)    S3_ENDPOINT_SUFFIX="amazonaws.com" ;;
      esac
      TEMPLATE_S3_KEY="assets/cfn/$(dirname "$TEMPLATE_LOCATION" | xargs basename)/$(basename "$TEMPLATE_LOCATION")"
      TEMPLATE_URL="https://${S3_BUCKET}.s3.${REGION}.${S3_ENDPOINT_SUFFIX}/${TEMPLATE_S3_KEY}"

      # Build --parameters list for create/update-stack
      CFN_PARAMS=()
      for OVERRIDE in "${PARAM_OVERRIDES[@]}"; do
        PKEY="${OVERRIDE%%=*}"
        PVAL="${OVERRIDE#*=}"
        CFN_PARAMS+=(ParameterKey="$PKEY",ParameterValue="$PVAL")
      done

      # Check if stack exists
      EXISTING_STATUS=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].StackStatus" \
        --output text 2>/dev/null || echo "DOES_NOT_EXIST")

      CREATE_ARGS=(
        --stack-name "$STACK_NAME"
        --template-url "$TEMPLATE_URL"
        --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
        --disable-rollback
        --tags Key=Workshop,Value=AgentCore-Platform Key=Environment,Value=Workshop
      )

      if [[ ${#CFN_PARAMS[@]} -gt 0 ]]; then
        CREATE_ARGS+=(--parameters "${CFN_PARAMS[@]}")
      fi

      if [[ "$EXISTING_STATUS" == "DOES_NOT_EXIST" ]]; then
        if ! aws cloudformation create-stack "${CREATE_ARGS[@]}"; then
          echo "Create failed for '$STACK_NAME'."
          record_stack_failure "$STACK_NAME" "create-stack call rejected"
          continue
        fi
        echo "Waiting for stack creation..."
        if ! aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME"; then
          echo ""
          echo "Stack creation failed for '$STACK_NAME'. Fetching failure details ..."
          aws cloudformation describe-stack-events \
            --stack-name "$STACK_NAME" \
            --query "StackEvents[?ResourceStatus=='CREATE_FAILED'].[LogicalResourceId, ResourceStatusReason]" \
            --output table
          record_stack_failure "$STACK_NAME" "create"
          continue
        fi
      else
        if ! aws cloudformation update-stack "${CREATE_ARGS[@]}"; then
          echo "Update failed for '$STACK_NAME'."
          record_stack_failure "$STACK_NAME" "update-stack call rejected"
          continue
        fi
        echo "Waiting for stack update..."
        if ! aws cloudformation wait stack-update-complete --stack-name "$STACK_NAME"; then
          echo ""
          echo "Stack update failed for '$STACK_NAME'. Fetching failure details ..."
          aws cloudformation describe-stack-events \
            --stack-name "$STACK_NAME" \
            --query "StackEvents[?ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId, ResourceStatusReason]" \
            --output table
          record_stack_failure "$STACK_NAME" "update"
          continue
        fi
      fi
    else
      # Simple templates: use `aws cloudformation deploy` (handles changesets automatically)
      DEPLOY_ARGS=(
        --template-file "$TEMPLATE_LOCATION"
        --stack-name "$STACK_NAME"
        --s3-bucket "$S3_BUCKET"
        --s3-prefix "cfn-deploy"
        --region "$REGION"
        --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
        --no-fail-on-empty-changeset
        --disable-rollback
        # Stack tags propagate to taggable resources; the participant policy's
        # aws:ResourceTag/Workshop condition on EC2 deletes relies on this
        # (Workshop Studio applies the same tags from contentspec.yaml).
        --tags Workshop=AgentCore-Platform Environment=Workshop
      )

      if [[ ${#PARAM_OVERRIDES[@]} -gt 0 ]]; then
        DEPLOY_ARGS+=(--parameter-overrides "${PARAM_OVERRIDES[@]}")
      fi

      if ! aws cloudformation deploy "${DEPLOY_ARGS[@]}"; then
        echo ""
        echo "Deploy failed for stack '$STACK_NAME'. Fetching failure details ..."
        echo ""
        aws cloudformation describe-stack-events \
          --stack-name "$STACK_NAME" \
          --query "StackEvents[?ResourceStatus=='CREATE_FAILED' || ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId, ResourceStatusReason]" \
          --output table
        record_stack_failure "$STACK_NAME" "deploy"
        continue
      fi
    fi

    echo "Stack '$STACK_NAME' deployed successfully."

    OUTPUTS=$(aws cloudformation describe-stacks \
      --stack-name "$STACK_NAME" \
      --query "Stacks[0].Outputs" \
      --output table 2>/dev/null || true)

    if [[ -n "$OUTPUTS" ]]; then
      echo "Outputs:"
      echo "$OUTPUTS"
    fi
    echo ""
  done

  if [[ -n "$DEPLOY_FAILED_STACKS" ]]; then
    echo "The following stack(s) FAILED:"
    printf '%b' "$DEPLOY_FAILED_STACKS" | while IFS= read -r s; do
      [[ -n "$s" ]] && echo "  - $s"
    done
    echo ""
    echo "Rollback is disabled. Run with 'cleanup' to remove broken stacks before retrying deploy."
    exit 1
  fi

  echo "All $TEMPLATES stack(s) deployed."
elif [[ "$OPERATION" == "destroy" || "$OPERATION" == "cleanup" ]]; then
  # Pre-cleanup: delete GuardDuty VPC endpoints and managed SGs (always block VPC deletion)
  # Wrapped so an empty result (grep finds nothing) cannot stop the run under
  # `set -euo pipefail`: grep returns 1 on no match, which pipefail would treat
  # as fatal and skip the actual stack deletions below, leaving costly resources.
  # GuardDuty creates these itself, so they carry no Workshop tag and always block
  # VPC deletion. An empty result must not abort the run under `set -euo pipefail`,
  # but a *failed* delete has to be reported: swallowing it turns a missing
  # permission into an unexplained VPC dependency error several minutes later.
  echo "Pre-cleanup: removing GuardDuty VPC endpoints and managed security groups..."
  # Scope this to the workshop's own VPCs. GuardDuty's endpoints and managed SGs
  # carry no Workshop tag, so there is nothing on the endpoint itself to filter
  # on -- but the VPC that contains them does, because the stack-level
  # Workshop=AgentCore-Platform tag propagates to it. Without this the queries
  # below are account-wide, and in an account that also runs unrelated workloads
  # a self-service teardown would strip GuardDuty coverage from every one of
  # their VPCs. The workshop VPC still exists at this point -- an undeletable
  # endpoint inside it is the whole reason this block runs.
  WORKSHOP_VPCS=$(aws ec2 describe-vpcs \
    --filters "Name=tag:Workshop,Values=AgentCore-Platform" \
    --query 'Vpcs[].VpcId' --output text 2>/dev/null || true)
  if [[ -z "${WORKSHOP_VPCS//[[:space:]]/}" ]]; then
    echo "  No workshop-tagged VPCs found -- nothing to unblock"
  else
    # describe-* wants one comma-separated Values list, not whitespace.
    VPC_FILTER=$(printf '%s' "$WORKSHOP_VPCS" | tr -s '[:space:]' ',')
    VPC_FILTER="${VPC_FILTER%,}"
    echo "  Limiting to workshop VPCs: $VPC_FILTER"

    GD_ENDPOINTS=$(aws ec2 describe-vpc-endpoints \
      --filters "Name=vpc-endpoint-state,Values=available,pending" \
                "Name=vpc-id,Values=${VPC_FILTER}" \
      --query "VpcEndpoints[?contains(ServiceName,'guardduty')].VpcEndpointId" \
      --output text 2>/dev/null || true)
    if [[ -z "${GD_ENDPOINTS//[[:space:]]/}" ]]; then
      echo "  No GuardDuty VPC endpoints found"
    else
      for ep in $GD_ENDPOINTS; do
        if aws ec2 delete-vpc-endpoints --vpc-endpoint-ids "$ep" >/dev/null 2>&1; then
          echo "  Deleted GuardDuty VPC endpoint $ep"
        else
          echo "  WARNING: could not delete GuardDuty VPC endpoint $ep -- it will block"
          echo "           VPC deletion. Requires ec2:DeleteVpcEndpoints on a resource"
          echo "           GuardDuty owns (it carries no Workshop tag)."
        fi
      done
    fi

    GD_SGS=$(aws ec2 describe-security-groups \
      --filters "Name=group-name,Values=GuardDutyManagedSecurityGroup*" \
                "Name=vpc-id,Values=${VPC_FILTER}" \
      --query "SecurityGroups[*].GroupId" --output text 2>/dev/null || true)
    if [[ -z "${GD_SGS//[[:space:]]/}" ]]; then
      echo "  No GuardDuty managed security groups found"
    else
      for sg in $GD_SGS; do
        if aws ec2 delete-security-group --group-id "$sg" >/dev/null 2>&1; then
          echo "  Deleted GuardDuty security group $sg"
        else
          echo "  WARNING: could not delete GuardDuty security group $sg -- it will"
          echo "           block VPC deletion. Requires ec2:DeleteSecurityGroup on a"
          echo "           resource GuardDuty owns (it carries no Workshop tag)."
        fi
      done
    fi
  fi

  # Destroy in reverse order to handle potential dependencies.
  #
  # A stack that fails to delete must NOT abort the run. Under `set -e` the
  # `wait stack-delete-complete` below used to kill the whole script, and because
  # the reverse order starts at the cheapest, most disposable stack (code-editor),
  # one leaf failure left DocumentDB, Aurora, the Fargate services and the NAT
  # Gateways running and billing -- and skipped the orphaned-log-group sweep that
  # the next deploy depends on. Observed for real: code-editor's `CodeEditorSecret`
  # could not be deleted, and all four expensive stacks survived a "destroy".
  # Every stack is now attempted, failures are collected, and the script exits
  # non-zero at the end naming each one.
  # Newline-delimited string, not an array: the shebang is /bin/bash, which on
  # macOS is still bash 3.2, and there `${#arr[@]}` on an EMPTY array is an
  # unbound-variable error under `set -u` -- it would abort the run at the final
  # summary, which is the exact failure mode this block exists to prevent.
  DELETED=0
  FAILED_STACKS=""
  for ((i = TEMPLATES - 1; i >= 0; i--)); do
    LABEL=$(yq -r ".infrastructure.cloudformationTemplates[$i].label" "$CONTENTSPEC")
    STACK_NAME="${LABEL}"

    # For cleanup, only target stacks not in a healthy state
    if [[ "$OPERATION" == "cleanup" ]]; then
      STATUS=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].StackStatus" \
        --output text 2>/dev/null || echo "DOES_NOT_EXIST")

      if [[ "$STATUS" == "CREATE_COMPLETE" || "$STATUS" == "UPDATE_COMPLETE" || "$STATUS" == "DOES_NOT_EXIST" ]]; then
        echo "Skipping '$STACK_NAME' (status: $STATUS)"
        continue
      fi

      echo "Stack '$STACK_NAME' is in state '$STATUS', cleaning up ..."
    else
      # `delete-stack` on a stack that does not exist succeeds, and
      # `wait stack-delete-complete` then returns immediately -- so without this
      # check the run prints "Deleting stack ... deleted successfully" five times
      # and "All 5 stack(s) destroyed" for an account that never had them. A
      # participant pointed at the wrong region would read that as proof their
      # resources are gone. Say what actually happened instead.
      if ! aws cloudformation describe-stacks --stack-name "$STACK_NAME" >/dev/null 2>&1; then
        echo "[$((TEMPLATES - i))/$TEMPLATES] Stack '$STACK_NAME' does not exist -- already gone."
        echo ""
        continue
      fi
      echo "[$((TEMPLATES - i))/$TEMPLATES] Deleting stack '$STACK_NAME' ..."
    fi

    if ! aws cloudformation delete-stack --stack-name "$STACK_NAME"; then
      echo "WARNING: could not start deletion of '$STACK_NAME' -- continuing."
      FAILED_STACKS="${FAILED_STACKS}${STACK_NAME} (delete-stack call rejected)\n"
      echo ""
      continue
    fi
    echo "Waiting for stack '$STACK_NAME' to be deleted ..."
    if aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME"; then
      echo "Stack '$STACK_NAME' deleted successfully."
      DELETED=$((DELETED + 1))
    else
      # Name the resource and the reason here. The waiter only says it "matched
      # expected path DELETE_FAILED", which tells a participant nothing.
      REASON=$(aws cloudformation describe-stack-events --stack-name "$STACK_NAME" \
        --query "StackEvents[?ResourceStatus=='DELETE_FAILED']|[0].[LogicalResourceId,ResourceStatusReason]" \
        --output text 2>/dev/null || true)
      echo "ERROR: stack '$STACK_NAME' failed to delete."
      [[ -n "${REASON//[[:space:]]/}" ]] && echo "       $REASON"
      FAILED_STACKS="${FAILED_STACKS}${STACK_NAME}\n"
    fi
    echo ""
  done

  # CloudFormation deletes a log group it declared, but a custom resource's
  # in-flight Delete invocation makes Lambda re-create it seconds later, outside
  # CloudFormation. (Proof: the orphan comes back with NO retention policy while
  # the template sets one, and its last log event predates its own creation
  # time.) The orphan then fails the NEXT deploy with
  #   The following hook(s)/validation failed: [AWS::EarlyValidation::ResourceExistenceCheck]
  # because the template declares a log group that now already exists — so
  # destroy-then-deploy in the same account and region stays broken until it is
  # removed, with an error that names no resource.
  #
  # Sweep exactly the log groups the templates DECLARE, and only for stacks that
  # no longer exist: never a name the templates do not own, and never one a
  # surviving stack still manages.
  #
  # Two classes, both derived from the templates so nothing is deleted on a name
  # guess:
  #
  #   1. `LogGroupName: /aws/lambda/<literal>` -- a group CloudFormation owns.
  #      A re-created orphan here BLOCKS the next deploy (see above).
  #   2. Groups the AWS service creates for itself, which CloudFormation never
  #      owned and therefore never deletes: /aws/lambda/<FunctionName>,
  #      /aws/ecs/containerinsights/<ClusterName>/performance and
  #      /aws/docdb/<DBClusterIdentifier>/audit. Ten of these survived a
  #      verified-clean `destroy` in a real account -- seven registry Lambdas,
  #      two Container Insights groups and the DocumentDB audit group. They do
  #      NOT block a redeploy, since nothing declares them; they are swept
  #      because "All 5 stack(s) destroyed" is the last word a self-paced
  #      participant reads, and the content's Verify Cleanup step lists stacks,
  #      not log groups.
  #
  # Class 2 names are resolved, not guessed: the only intrinsics involved are
  # ${EnvironmentName} (this template's own contentspec defaultValue -- not
  # assumed to be "workshop") and ${AWS::StackName} (the contentspec label,
  # which is the name this script deploys under). A name using anything else is
  # skipped rather than approximated, and every candidate is confirmed to exist
  # before a delete is attempted.
  echo "Post-delete: removing the log groups the deleted stacks left behind..."
  ORPHANS=0
  for ((i = 0; i < TEMPLATES; i++)); do
    TEMPLATE_LOCATION=$(yq -r ".infrastructure.cloudformationTemplates[$i].templateLocation" "$CONTENTSPEC")
    LABEL=$(yq -r ".infrastructure.cloudformationTemplates[$i].label" "$CONTENTSPEC")

    if aws cloudformation describe-stacks --stack-name "$LABEL" >/dev/null 2>&1; then
      continue
    fi

    # Nested templates sit beside the root template in its own directory. A
    # template directly in static/cfn/ has no directory of its own, so scanning
    # the whole of static/cfn/ there would pull in every other stack's names.
    TDIR="$(dirname "$TEMPLATE_LOCATION")"
    if [[ "$TDIR" == "static/cfn" ]]; then
      SCAN_PATH="$TEMPLATE_LOCATION"
    else
      SCAN_PATH="$TDIR"
    fi

    # Empty when this template has no EnvironmentName parameter; resolve_names
    # then drops every name that needs it, which is the safe direction.
    ENV_NAME=$(yq -r ".infrastructure.cloudformationTemplates[$i].parameters // []
                      | map(select(.templateParameter == \"EnvironmentName\"))
                      | .[0].defaultValue // \"\"" "$CONTENTSPEC")

    # Class 1: declared log groups -- these are the ones that block a redeploy.
    #
    # `|| true` on every grep-fed assignment in this block. "No match" is grep's
    # exit 1, and with `set -euo pipefail` a pipeline ending in a failed grep
    # aborts the script at the assignment. The previous `for lg in $(grep ...)`
    # form was accidentally immune (a command substitution in a `for` word list
    # has its status discarded); a plain assignment is not. code-editor.yaml
    # declares no log group at all, so this fires on a normal teardown.
    DECLARED=$(grep -rhoE '^[[:space:]]*LogGroupName:[[:space:]]*/aws/lambda/[A-Za-z0-9._/-]+' "$SCAN_PATH" 2>/dev/null |
                 awk '{print $2}' | sort -u || true)

    # Class 2. `resolve_names <yaml-key> <log-group-prefix> [suffix]` pulls the
    # values of one property, resolves the two known intrinsics, and prints the
    # log group each one implies. Anything still holding a `${...}` after
    # substitution is dropped rather than deleted on a partial name. `sort -u`
    # over the union because a FunctionName that DOES have a LogGroup resource
    # beside it yields the same string twice.
    #
    # An intrinsic whose value is EMPTY is deliberately left unsubstituted: with
    # ENV_NAME="", substituting would turn `${EnvironmentName}-cognito-init` into
    # `-cognito-init` and pass the filter as a plausible-looking wrong name.
    # Leaving the `${...}` in place makes the filter drop it, which is correct.
    #
    # if/then, not `[[ ... ]] && ...`: under `set -e` a trailing test that
    # evaluates false is the command's exit status, so the one-liner form killed
    # the whole script on the first template with no EnvironmentName -- silently,
    # right after printing the header above, taking the failure summary and the
    # bucket removal below with it.
    SUBST=""
    if [[ -n "$ENV_NAME" ]]; then
      SUBST="s/\\\$\{EnvironmentName\}/${ENV_NAME}/g;"
    fi
    if [[ -n "$LABEL" ]]; then
      SUBST="${SUBST}s/\\\$\{AWS::StackName\}/${LABEL}/g;"
    fi
    resolve_names() {
      grep -rhoE "^[[:space:]]*$1:[[:space:]]*(!Sub[[:space:]]+)?['\"]?[A-Za-z0-9._\$\{\}:-]+" "$SCAN_PATH" 2>/dev/null |
        sed -E "s/^[[:space:]]*$1:[[:space:]]*//; s/!Sub[[:space:]]+//; s/^['\"]//" |
        sed -E "${SUBST}" |
        grep -vE '[$}]|^$' |
        while IFS= read -r n; do echo "${2}${n}${3:-}"; done || true
    }
    LG_NAMES=$( { echo "$DECLARED"
                  resolve_names FunctionName /aws/lambda/
                  resolve_names ClusterName /aws/ecs/containerinsights/ /performance
                  resolve_names DBClusterIdentifier /aws/docdb/ /audit
                } | sort -u || true )

    for lg in $LG_NAMES; do
      FOUND=$(aws logs describe-log-groups --log-group-name-prefix "$lg" \
        --query "logGroups[?logGroupName=='$lg'].logGroupName" --output text 2>/dev/null || true)
      [[ -n "${FOUND//[[:space:]]/}" ]] || continue
      if aws logs delete-log-group --log-group-name "$lg" >/dev/null 2>&1; then
        echo "  Deleted orphaned log group $lg"
        ORPHANS=$((ORPHANS + 1))
      elif printf '%s\n' "$DECLARED" | grep -qxF "$lg"; then
        echo "  WARNING: could not delete orphaned log group $lg -- the next deploy"
        echo "           will fail AWS::EarlyValidation::ResourceExistenceCheck"
        echo "           because the template declares a name that already exists."
      else
        # Not declared anywhere, so it cannot collide -- leaving it behind costs
        # a few cents of storage, not a broken redeploy. Do not send anyone
        # hunting for an EarlyValidation failure that will not happen.
        echo "  NOTE: could not delete leftover log group $lg (Lambda created it,"
        echo "        not CloudFormation). Harmless, but yours to delete:"
        echo "        aws logs delete-log-group --log-group-name $lg"
      fi
    done
  done
  if [[ "$ORPHANS" -eq 0 ]]; then
    echo "  No orphaned log groups found"
  fi
  echo ""

  if [[ -n "$FAILED_STACKS" ]]; then
    echo "$DELETED of $TEMPLATES stack(s) deleted. The following FAILED:"
    printf '%b' "$FAILED_STACKS" | while IFS= read -r s; do
      [[ -n "$s" ]] && echo "  - $s"
    done
    echo ""
    echo "Resolve the reason above and re-run '$0 $OPERATION' -- it is safe to"
    echo "re-run, already-deleted stacks are skipped. Until every stack is gone you"
    echo "are still being charged for DocumentDB, Aurora, Fargate and the NAT Gateways."
    exit 1
  fi
  if [[ "$OPERATION" == "cleanup" ]]; then
    echo "$DELETED stack(s) cleaned up."
  else
    # Report what was actually deleted, not the template count. On a re-run, or
    # against the wrong region, DELETED is 0 and "All 5 stack(s) destroyed" would
    # be a false all-clear.
    if [[ "$DELETED" -eq "$TEMPLATES" ]]; then
      echo "All $TEMPLATES stack(s) destroyed."
    elif [[ "$DELETED" -eq 0 ]]; then
      echo "Nothing to destroy -- none of the $TEMPLATES stack(s) exist in $REGION."
    else
      echo "$DELETED of $TEMPLATES stack(s) destroyed; the rest were already gone."
    fi

    # Remove the assets bucket this script created. Nothing else can: CloudFormation
    # never owned it, so `destroy` used to finish by printing "All 5 stack(s)
    # destroyed" while leaving a bucket holding every nested template plus a full
    # copy of source/. The Verify Cleanup step in the content cannot catch that --
    # it lists stacks, not buckets -- so a self-paced participant who followed the
    # teardown exactly was still left with it.
    #
    # Only for `destroy`, and only once every stack is gone: a `cleanup` run is a
    # repair before another deploy and still needs the bucket, and the
    # FAILED_STACKS branch above has already exited, so a re-run keeps its
    # templates. `--expected-bucket-owner` for the same reason as the create path:
    # the name is predictable, so never empty a bucket this account does not own.
    if aws s3api head-bucket --bucket "$S3_BUCKET" --region "$REGION" \
         --expected-bucket-owner "$ACCOUNT_ID" >/dev/null 2>&1; then
      echo "Removing assets bucket '$S3_BUCKET' ..."
      if aws s3 rm "s3://$S3_BUCKET" --recursive --only-show-errors &&
         aws s3api delete-bucket --bucket "$S3_BUCKET" --region "$REGION"; then
        echo "Assets bucket removed."
      else
        echo "WARNING: '$S3_BUCKET' could not be removed. It holds only templates and"
        echo "         a copy of source/, so it costs very little, but it is yours to"
        echo "         delete: aws s3 rb s3://$S3_BUCKET --force"
      fi
    else
      echo "Assets bucket '$S3_BUCKET' is already gone."
    fi
  fi
fi
