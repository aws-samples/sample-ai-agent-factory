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
S3_BUCKET="cfn-deploy-${DIR_NAME}-${ACCOUNT_ID}-${REGION}"

# Ensure the bucket exists in the correct region
if ! aws s3api head-bucket --bucket "$S3_BUCKET" --region "$REGION" 2>/dev/null; then
  echo "Creating S3 bucket '$S3_BUCKET' in $REGION ..."
  if [[ "$REGION" == "us-east-1" ]]; then
    # us-east-1 must NOT pass a LocationConstraint (the API rejects it).
    aws s3api create-bucket --bucket "$S3_BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$S3_BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"
  fi
fi

echo "Using S3 bucket: $S3_BUCKET"

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
    --exclude "*.state.json" --exclude ".state.json"
  echo "Source code synced."
  echo ""
fi

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
          exit 1
        fi
        echo "Waiting for stack creation..."
        if ! aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME"; then
          echo ""
          echo "Stack creation failed for '$STACK_NAME'. Fetching failure details ..."
          aws cloudformation describe-stack-events \
            --stack-name "$STACK_NAME" \
            --query "StackEvents[?ResourceStatus=='CREATE_FAILED'].[LogicalResourceId, ResourceStatusReason]" \
            --output table
          exit 1
        fi
      else
        if ! aws cloudformation update-stack "${CREATE_ARGS[@]}"; then
          echo "Update failed for '$STACK_NAME'."
          exit 1
        fi
        echo "Waiting for stack update..."
        if ! aws cloudformation wait stack-update-complete --stack-name "$STACK_NAME"; then
          echo ""
          echo "Stack update failed for '$STACK_NAME'. Fetching failure details ..."
          aws cloudformation describe-stack-events \
            --stack-name "$STACK_NAME" \
            --query "StackEvents[?ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId, ResourceStatusReason]" \
            --output table
          exit 1
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
        echo ""
        echo "Rollback is disabled. Run with 'cleanup' to remove broken stacks before retrying deploy."
        exit 1
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

  echo "All $TEMPLATES stack(s) deployed."
elif [[ "$OPERATION" == "destroy" || "$OPERATION" == "cleanup" ]]; then
  # Pre-cleanup: delete GuardDuty VPC endpoints and managed SGs (always block VPC deletion)
  # Wrapped so an empty result (grep finds nothing) cannot stop the run under
  # `set -euo pipefail`: grep returns 1 on no match, which pipefail would treat
  # as fatal and skip the actual stack deletions below, leaving costly resources.
  echo "Pre-cleanup: removing GuardDuty VPC endpoints and managed security groups..."
  {
    aws ec2 describe-vpc-endpoints \
      --filters "Name=vpc-endpoint-state,Values=available,pending" \
      --query "VpcEndpoints[?contains(ServiceName,'guardduty')].VpcEndpointId" \
      --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$' | \
      xargs -r -I{} aws ec2 delete-vpc-endpoints --vpc-endpoint-ids {} 2>/dev/null
  } || true
  echo "  GuardDuty VPC endpoints deleted (or none found)"
  {
    aws ec2 describe-security-groups \
      --filters "Name=group-name,Values=GuardDutyManagedSecurityGroup*" \
      --query "SecurityGroups[*].GroupId" --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$' | \
      xargs -r -I{} aws ec2 delete-security-group --group-id {} 2>/dev/null
  } || true
  echo "  GuardDuty managed security groups deleted (or none found)"

  # Destroy in reverse order to handle potential dependencies
  DELETED=0
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
      echo "[$((TEMPLATES - i))/$TEMPLATES] Deleting stack '$STACK_NAME' ..."
    fi

    aws cloudformation delete-stack --stack-name "$STACK_NAME"
    echo "Waiting for stack '$STACK_NAME' to be deleted ..."
    aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME"

    echo "Stack '$STACK_NAME' deleted successfully."
    DELETED=$((DELETED + 1))
    echo ""
  done

  if [[ "$OPERATION" == "cleanup" ]]; then
    echo "$DELETED stack(s) cleaned up."
  else
    echo "All $TEMPLATES stack(s) destroyed."
  fi
fi
