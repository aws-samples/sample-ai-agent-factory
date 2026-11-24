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
# loop -- so every call site pairs it with one. The retry advice differs per
# failure (an in-place update for most, `cleanup` first for a stack CloudFormation
# refuses to update, nothing to clean for a rejected create-stack call), so every
# call site prints its own before calling this; the line here stays neutral.
record_stack_failure() {
  DEPLOY_FAILED_STACKS="${DEPLOY_FAILED_STACKS}$1 ($2)\n"
  if [[ "$CONTINUE_ON_FAILURE" != "1" ]]; then
    echo ""
    echo "Stopping here: the deployment of stack '$1' could not be confirmed ($2). See the"
    echo "message above for what to check or how to retry."
    exit 1
  fi
  echo "DEPLOY_CONTINUE_ON_FAILURE=1 -- recorded and continuing to the next stack."
  echo "Note: stacks that import this one's outputs may fail if the exports they need are"
  echo "      unavailable, rather than on a problem of their own."
}

# The retry advice for a stack that CloudFormation did operate on and that is now
# CREATE_FAILED or UPDATE_FAILED. Not for a rejected API call (nothing changed) and
# not for a state CloudFormation refuses to update (those print `cleanup` instead).
retry_in_place_hint() {
  echo "Rollback is disabled, so stack '$1' keeps every resource it did provision. Fix the"
  echo "cause and run deploy again: the stack is updated in place, which keeps the successful"
  echo "resources the new template leaves unchanged, retries the failed ones and applies every"
  echo "other template or parameter change. Or run './deploy-cfn.sh cleanup' to delete every"
  echo "failed stack and start over."
}

# stack_is_healthy <status>: the three stable states with outputs. Shared by the
# FINAL STATUS CHECK after a deploy and by `cleanup`, so cleanup can never delete
# a stack that deploy accepts as healthy.
stack_is_healthy() {
  case "$1" in
    CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE) return 0 ;;
    *) return 1 ;;
  esac
}

# cfn_says_absent <error text>: true only for CloudFormation's own ValidationError
# on a Describe call saying the stack or resource does not exist. Other errors can
# contain the words "does not exist" too (an expired SSO token: "Token for ...
# does not exist") and must never read as absence.
cfn_says_absent() {
  grep -qE '\(ValidationError\) when calling the Describe(Stacks|StackResource) operation: (Stack with id [^ ]+ does not exist|Resource [^ ]+ does not exist for stack)' <<<"$1"
}

# current_stack_status <stack>: the stack's status, DOES_NOT_EXIST, or UNKNOWN when
# the read itself failed (access, network, throttling), so that no message guesses
# what CloudFormation did from the failure of an API call alone.
current_stack_status() {
  local out
  if out=$(aws cloudformation describe-stacks --stack-name "$1" \
      --query "Stacks[0].StackStatus" --output text 2>&1); then
    echo "$out"
  elif cfn_says_absent "$out"; then
    echo "DOES_NOT_EXIST"
  else
    echo "UNKNOWN"
  fi
}

# advise_after_failure <stack> <state>: recovery advice chosen from the state read
# back, never from the failing call alone. A call can return an error after
# CloudFormation accepted the request (a response timeout), and a waiter can give
# up while the work continues.
advise_after_failure() {
  case "$2" in
    DOES_NOT_EXIST)
      echo "No stack '$1' currently exists. Fix the cause reported above and run deploy again." ;;
    UNKNOWN)
      echo "The current state of '$1' could not be read, so the outcome of the call is unknown."
      echo "Check the stack in the CloudFormation console or with"
      echo "  aws cloudformation describe-stacks --stack-name $1"
      echo "before running deploy again." ;;
    *_IN_PROGRESS)
      echo "CloudFormation is still working on '$1' (state $2): the call returned before it"
      echo "finished. Wait for a terminal state (the console, or"
      echo "  aws cloudformation describe-stacks --stack-name $1"
      echo "), check the result, then run deploy again." ;;
    CREATE_FAILED|UPDATE_FAILED)
      retry_in_place_hint "$1" ;;
    ROLLBACK_COMPLETE|ROLLBACK_FAILED|DELETE_FAILED)
      echo "Stack '$1' is in state $2, which CloudFormation does not update; remove it, then run"
      echo "deploy again:"
      echo "  ./deploy-cfn.sh cleanup" ;;
    UPDATE_ROLLBACK_FAILED)
      echo "Stack '$1' is in state $2. Run"
      echo "  aws cloudformation continue-update-rollback --stack-name $1"
      echo "and deploy again, or remove the stack with ./deploy-cfn.sh cleanup." ;;
    CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE)
      echo "Stack '$1' reads as $2 despite the error above (for example the call timed out after"
      echo "CloudFormation had finished). Check that the template and parameters you requested"
      echo "are what is deployed, then run deploy again." ;;
    *)
      echo "Stack '$1' is in state $2. Fix the cause reported above and run deploy again, or"
      echo "remove the stack with ./deploy-cfn.sh cleanup." ;;
  esac
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

# The identity that will open CloudWatch Omni from the AWS console (Module 5).
# Workshop Studio resolves {{.ParticipantRoleArn}} to WSParticipantRole, a
# different principal from WSSystemRole, the role that runs CloudFormation there
# and so creates the space. On the self-paced path the identity running this
# script is both: CloudFormation runs as it (no service role), Omni's CreateSpace
# gives the creator a system-managed Space Admin grant (OmniDefaults-Space-Admin),
# and a second SPACE_ADMIN grant for the same principal is rejected. Observed on
# the first self-paced deploy (2026-11-10, reported, no captured response kept):
# CreateAccessGrant 409 ConflictException "An active SPACE_ADMIN grant already
# exists for principal", which failed workshop-agentcore-stack. So no console
# grant is created by default; the deployer that creates the space is already
# Space Admin. Set OMNI_CONSOLE_PRINCIPAL_ARN (and
# OMNI_CONSOLE_PRINCIPAL_TYPE=IAM_ROLE|IAM_USER) to admit a DIFFERENT identity,
# the one your browser signs in to the console as (Omni rejects a grant for a
# principal that does not exist). Module 5 step 1 shows how to add one later from
# the IDE.
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text)
CALLER_PARTITION="${CALLER_ARN#arn:}"; CALLER_PARTITION="${CALLER_PARTITION%%:*}"
OMNI_CONSOLE_PRINCIPAL_ARN="${OMNI_CONSOLE_PRINCIPAL_ARN:-}"
OMNI_CONSOLE_PRINCIPAL_TYPE="${OMNI_CONSOLE_PRINCIPAL_TYPE:-IAM_ROLE}"
# The deployer's own principal, only to recognise an override that names it. A
# user is itself. An assumed role is compared through its role ARN, read back with
# iam get-role so a path such as aws-reserved/sso.amazonaws.com/ is kept. When
# that read is refused (the scoped deploy policy does not grant it for SSO roles)
# nothing is fabricated: the override is parsed as
# arn:<partition>:iam::<account>:role/<path...>/<name> and names the deployer when
# partition and account match the caller's and the final path component is the
# caller's role name. The override itself is never rewritten.
CALLER_ROLE_NAME=""
DEPLOYER_ROLE_ARN=""
case "$CALLER_ARN" in
  arn:*:sts::*:assumed-role/*/*)
    CALLER_ROLE_NAME="${CALLER_ARN#*:assumed-role/}"; CALLER_ROLE_NAME="${CALLER_ROLE_NAME%%/*}"
    DEPLOYER_ROLE_ARN=$(aws iam get-role --role-name "$CALLER_ROLE_NAME" \
      --query Role.Arn --output text 2>/dev/null || true)
    [[ "$DEPLOYER_ROLE_ARN" != "None" ]] || DEPLOYER_ROLE_ARN="" ;;
esac
# override_names_caller <arn>: succeeds when <arn> is the identity running this deploy.
override_names_caller() {
  local override="$1" o_partition o_account o_name
  [[ -n "$override" ]] || return 1
  case "$CALLER_ARN" in
    arn:*:iam::*:user/*)
      [[ "$override" == "$CALLER_ARN" ]] ;;
    arn:*:sts::*:assumed-role/*/*)
      if [[ -n "$DEPLOYER_ROLE_ARN" ]]; then
        [[ "$override" == "$DEPLOYER_ROLE_ARN" ]]
        return
      fi
      [[ "$override" == arn:*:iam::*:role/* ]] || return 1
      o_partition="${override#arn:}"; o_partition="${o_partition%%:*}"
      o_account="${override#arn:*:iam::}"; o_account="${o_account%%:*}"
      o_name="${override##*/}"
      [[ "$o_partition" == "$CALLER_PARTITION" && "$o_account" == "$ACCOUNT_ID" && "$o_name" == "$CALLER_ROLE_NAME" ]] ;;
    *)
      return 1 ;;
  esac
}
# Whether a space already exists decides what an override naming the deployer
# means. No space yet: this deploy creates it, the deployer becomes its creator
# and holds Omni's own Space Admin grant, so the requested grant would be the
# rejected duplicate and is dropped. Space already there: its creator may be a
# different identity, and whether the deployer holds a grant could only be read
# with cloudwatch:ListAccessGrants, which static/cfn/self-service-deploy-policy-*
# does not grant, so the override is kept as asked and the one failure it can
# produce is explained.
#
# "Already there" is read in two steps, because a failed stack has no outputs
# even when its OmniSpace resource was created (the 2026-11-10 case: space
# created, the grant after it failed, stack CREATE_FAILED). First the OmniSpaceId
# output of a completed workshop-agentcore-stack; failing that, the physical ID
# of the stack's OmniSpace resource (cloudformation:DescribeStackResource is in
# self-service-deploy-policy-1). Both guarded with || true: a missing stack is
# the normal case on a first deploy. Read for every deploy so the summary below
# says whether this run creates the space or updates an existing one; destroy
# and cleanup never use it.
# Both reads distinguish "the stack (or resource) does not exist" from "the read
# failed": only the first means no space. Treating a failed read as absence would
# clear a caller-naming OMNI_CONSOLE_PRINCIPAL_ARN and, on an existing space, a
# later successful update would then remove the grant that admits the caller. So
# a read that fails for any other reason stops the run before any parameter is
# changed, and shows the error.
omni_probe_failed() {
  cat "$OMNI_PROBE_ERR"
  echo "Error: could not read workshop-agentcore-stack to tell whether a CloudWatch Omni space"
  echo "       already exists ($1 failed for a reason other than a missing stack or resource)."
  echo "       Nothing was changed. Fix the access or network problem above and run deploy again."
  rm -f "$OMNI_PROBE_ERR"
  exit 1
}
EXISTING_OMNI_SPACE_ID=""
if [[ "$OPERATION" == "deploy" ]]; then
  OMNI_PROBE_ERR=$(mktemp)
  # Remove the temp file on any exit, including an interruption, until the normal
  # removal below clears the traps.
  trap 'rm -f "$OMNI_PROBE_ERR"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  AGENTCORE_STACK_ABSENT=0
  if OMNI_PROBE_OUT=$(aws cloudformation describe-stacks \
      --stack-name workshop-agentcore-stack \
      --query "Stacks[0].Outputs[?OutputKey=='OmniSpaceId'].OutputValue" \
      --output text 2>"$OMNI_PROBE_ERR"); then
    [[ "$OMNI_PROBE_OUT" == "None" || -z "$OMNI_PROBE_OUT" ]] || EXISTING_OMNI_SPACE_ID="$OMNI_PROBE_OUT"
  elif cfn_says_absent "$(cat "$OMNI_PROBE_ERR")"; then
    AGENTCORE_STACK_ABSENT=1   # no stack yet: the normal first deploy
  else
    omni_probe_failed "describe-stacks"
  fi
  if [[ -z "$EXISTING_OMNI_SPACE_ID" && "$AGENTCORE_STACK_ABSENT" == "0" ]]; then
    # The stack exists but exports no OmniSpaceId (a failed create has no
    # outputs). A resource a rollback already deleted keeps its physical ID, so
    # that status is skipped.
    if OMNI_PROBE_OUT=$(aws cloudformation describe-stack-resource \
        --stack-name workshop-agentcore-stack --logical-resource-id OmniSpace \
        --query "[StackResourceDetail][?ResourceStatus!='DELETE_COMPLETE'].PhysicalResourceId | [0]" \
        --output text 2>"$OMNI_PROBE_ERR"); then
      [[ "$OMNI_PROBE_OUT" == "None" || -z "$OMNI_PROBE_OUT" ]] || EXISTING_OMNI_SPACE_ID="$OMNI_PROBE_OUT"
    elif cfn_says_absent "$(cat "$OMNI_PROBE_ERR")"; then
      :   # the stack never created an OmniSpace resource
    else
      omni_probe_failed "describe-stack-resource"
    fi
  fi
  rm -f "$OMNI_PROBE_ERR"
  trap - EXIT INT TERM
fi
# F9 of the review: the override handling and the narrative below describe a
# deploy. destroy and cleanup never probe the space, so they skip this block.
if [[ "$OPERATION" == "deploy" ]]; then
if [[ -n "$OMNI_CONSOLE_PRINCIPAL_ARN" ]] && override_names_caller "$OMNI_CONSOLE_PRINCIPAL_ARN"; then
  if [[ -z "$EXISTING_OMNI_SPACE_ID" ]]; then
    echo "CloudWatch Omni: OMNI_CONSOLE_PRINCIPAL_ARN ($OMNI_CONSOLE_PRINCIPAL_ARN) names the"
    echo "                 identity running this deploy, and workshop-agentcore-stack has created no"
    echo "                 space so far. It becomes Space Admin when CloudFormation creates the"
    echo "                 space, and Omni rejects a second grant for it, so no console grant is"
    echo "                 created."
    OMNI_CONSOLE_PRINCIPAL_ARN=""
  else
    echo "CloudWatch Omni: OMNI_CONSOLE_PRINCIPAL_ARN ($OMNI_CONSOLE_PRINCIPAL_ARN) names the"
    echo "                 identity running this deploy, and space $EXISTING_OMNI_SPACE_ID already"
    echo "                 exists. Whether this identity created it (and so already holds Omni's"
    echo "                 Space Admin grant) cannot be read with the deploy policy, so the grant"
    echo "                 is requested as asked. If workshop-agentcore-stack then fails on"
    echo "                 OmniParticipantSpaceAdminGrant with a 409 ConflictException ('An active"
    echo "                 SPACE_ADMIN grant already exists'), this identity is already Space Admin:"
    echo "                 re-run without OMNI_CONSOLE_PRINCIPAL_ARN."
  fi
fi
if [[ -n "$OMNI_CONSOLE_PRINCIPAL_ARN" ]]; then
  echo "CloudWatch Omni console grant will go to: $OMNI_CONSOLE_PRINCIPAL_ARN ($OMNI_CONSOLE_PRINCIPAL_TYPE)"
elif [[ -n "$EXISTING_OMNI_SPACE_ID" ]]; then
  # contentspec passes OmniConsolePrincipalArn={{.ParticipantRoleArn}}, which this
  # script resolves to the (now empty) override, and the template's grant is
  # conditional on a non-empty value. So an empty override is not "no change": it
  # removes a participant-space-admin grant that an earlier run's
  # OMNI_CONSOLE_PRINCIPAL_ARN created. Omni's own creator grant and the
  # code-editor instance-role grant are untouched.
  echo "CloudWatch Omni: space $EXISTING_OMNI_SPACE_ID already exists; this deploy updates it."
  echo "                 No console grant is requested, so a participant-space-admin grant"
  echo "                 created by an earlier run's OMNI_CONSOLE_PRINCIPAL_ARN is removed unless"
  echo "                 you pass the same value again. Omni's own creator grant and the Code"
  echo "                 Editor instance-role grant stay. To admit a console identity, set"
  echo "                 OMNI_CONSOLE_PRINCIPAL_ARN, or add a grant later (Module 5 step 1)."
else
  echo "CloudWatch Omni: $CALLER_ARN creates the space and becomes its Space Admin"
  echo "                 (Omni's own grant). To admit a different console identity, set"
  echo "                 OMNI_CONSOLE_PRINCIPAL_ARN, or add a grant later (Module 5 step 1)."
fi
fi
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

# The seven participant IAM policies exist twice: as the JSON files contentspec
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
      # {{.TeamId}} is not used by the current contentspec. If a template adds it,
      # export TEAM_ID; the fallback is a documented synthetic value, not an event's.
      PARAM_VALUE="${PARAM_VALUE//\{\{.TeamId\}\}/${TEAM_ID:-00000000-0000-4000-8000-000000000000}}"
      PARAM_VALUE="${PARAM_VALUE//\{\{.TeamIndex\}\}/0}"
      PARAM_VALUE="${PARAM_VALUE//\{\{.ParticipantRoleArn\}\}/$OMNI_CONSOLE_PRINCIPAL_ARN}"

      # Skip if unresolvable magic variables remain
      if [[ "$PARAM_VALUE" == *'{{.'*'}}'* ]]; then
        continue
      fi

      PARAM_OVERRIDES+=("${PARAM_NAME}=${PARAM_VALUE}")
    done

    # workshop-agentcore-stack: the console grant's principal type must match the
    # ARN resolved above (an IAM user on the self-paced path is not a role).
    if [[ "$STACK_NAME" == "workshop-agentcore-stack" && -n "$OMNI_CONSOLE_PRINCIPAL_ARN" ]]; then
      PARAM_OVERRIDES+=("OmniConsolePrincipalType=${OMNI_CONSOLE_PRINCIPAL_TYPE}")
    fi

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

    # A stack left in a failed state by an earlier run. Three states cannot be
    # repaired by an update and are refused here: ROLLBACK_COMPLETE (a failed
    # first create whose rollback finished; CloudFormation rejects every update
    # and only delete is left), ROLLBACK_FAILED (that rollback got stuck; same
    # outcome, and only reachable after a manual rollback-stack since this script
    # deploys with --disable-rollback) and DELETE_FAILED (a delete that got
    # stuck). A CREATE_FAILED or UPDATE_FAILED stack is different: rollback was
    # disabled, so the stack keeps every resource that did provision, and
    # CloudFormation lets the fixed template be applied as an update that keeps
    # the successful resources the new template leaves unchanged, retries the
    # failed ones and applies every other template or parameter change. Sending
    # those through `cleanup` would delete
    # the healthy resources too, so they proceed. UPDATE_ROLLBACK_FAILED (again
    # only after a manual rollback) also proceeds; if CloudFormation rejects the
    # update, the failure path names `continue-update-rollback` and `cleanup` as
    # the ways out.
    #
    # Proceeding is only safe because of the FINAL STATUS CHECK after the deploy:
    # an update that changes nothing leaves the stack exactly as failed as it was.
    # Reported from the first self-paced deploy (2026-11-10; no captured output is
    # kept in this repository): the fix removed the
    # failed resource, the change set was empty, `--no-fail-on-empty-changeset`
    # reported workshop-agentcore-stack "deployed successfully", the stack stayed
    # CREATE_FAILED with no outputs, and code-editor died on "No export named
    # ac-OmniSpaceId found". The check below turns that into a recorded failure.
    PRIOR_STATUS=$(current_stack_status "$STACK_NAME")
    case "$PRIOR_STATUS" in
      UNKNOWN)
        echo "The state of '$STACK_NAME' could not be read (access, network or throttling), so this"
        echo "run does not know whether it exists and does not deploy it blind. Fix the read and"
        echo "run deploy again."
        record_stack_failure "$STACK_NAME" "state could not be read before deploy"
        continue ;;
      ROLLBACK_COMPLETE|ROLLBACK_FAILED|DELETE_FAILED)
        echo "Stack '$STACK_NAME' is in state $PRIOR_STATUS from an earlier run. CloudFormation"
        echo "does not accept an update in this state; remove it first, then run deploy again:"
        echo "  ./deploy-cfn.sh cleanup"
        record_stack_failure "$STACK_NAME" "$PRIOR_STATUS from an earlier run"
        continue ;;
      CREATE_FAILED|UPDATE_FAILED)
        echo "Stack '$STACK_NAME' is in state $PRIOR_STATUS from an earlier run. Rollback was"
        echo "disabled, so the resources it did provision are still there. The update below keeps"
        echo "the successful resources the template leaves unchanged, retries the failed ones and"
        echo "applies every other template or parameter change. Its final state is checked before"
        echo "this run moves on." ;;
      UPDATE_ROLLBACK_FAILED)
        echo "Stack '$STACK_NAME' is in state $PRIOR_STATUS from an earlier run. An update is"
        echo "attempted; if CloudFormation rejects it, run"
        echo "  aws cloudformation continue-update-rollback --stack-name $STACK_NAME"
        echo "and deploy again, or remove the stack with ./deploy-cfn.sh cleanup." ;;
    esac

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

      # Whether the stack exists decides create-stack vs update-stack. PRIOR_STATUS
      # was read a moment ago and an unreadable state already stopped this stack.
      EXISTING_STATUS="$PRIOR_STATUS"

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
          echo "The create-stack call for '$STACK_NAME' returned an error (details above). That"
          echo "alone does not say whether CloudFormation accepted it, so the state is read back."
          STATE_NOW=$(current_stack_status "$STACK_NAME")
          echo "Current state of '$STACK_NAME': $STATE_NOW"
          advise_after_failure "$STACK_NAME" "$STATE_NOW"
          record_stack_failure "$STACK_NAME" "create-stack call failed (state $STATE_NOW)"
          continue
        fi
        echo "Waiting for stack creation..."
        if ! aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME"; then
          echo ""
          echo "Waiting for '$STACK_NAME' ended with an error: either the create failed or the"
          echo "waiter gave up while CloudFormation was still working. Reading the outcome ..."
          aws cloudformation describe-stack-events \
            --stack-name "$STACK_NAME" \
            --query "StackEvents[?ResourceStatus=='CREATE_FAILED'].[LogicalResourceId, ResourceStatusReason]" \
            --output table || true
          STATE_NOW=$(current_stack_status "$STACK_NAME")
          echo "Current state of '$STACK_NAME': $STATE_NOW"
          advise_after_failure "$STACK_NAME" "$STATE_NOW"
          record_stack_failure "$STACK_NAME" "create (state $STATE_NOW)"
          continue
        fi
      else
        # update-stack has no --no-fail-on-empty-changeset: an unchanged template
        # is rejected with "No updates are to be performed." That is not a
        # failure of the stack, so it falls through to the final status check,
        # which is what decides whether an unchanged stack is healthy.
        if UPDATE_OUT=$(aws cloudformation update-stack "${CREATE_ARGS[@]}" 2>&1); then
          echo "$UPDATE_OUT"
          echo "Waiting for stack update..."
          if ! aws cloudformation wait stack-update-complete --stack-name "$STACK_NAME"; then
            echo ""
            echo "Waiting for '$STACK_NAME' ended with an error: either the update failed or the"
            echo "waiter gave up while CloudFormation was still working. Reading the outcome ..."
            aws cloudformation describe-stack-events \
              --stack-name "$STACK_NAME" \
              --query "StackEvents[?ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId, ResourceStatusReason]" \
              --output table || true
            STATE_NOW=$(current_stack_status "$STACK_NAME")
            echo "Current state of '$STACK_NAME': $STATE_NOW"
            advise_after_failure "$STACK_NAME" "$STATE_NOW"
            record_stack_failure "$STACK_NAME" "update (state $STATE_NOW)"
            continue
          fi
        elif grep -q "No updates are to be performed" <<<"$UPDATE_OUT"; then
          echo "No changes to apply to '$STACK_NAME'; checking its current state."
        else
          echo "$UPDATE_OUT"
          echo "The update-stack call for '$STACK_NAME' returned an error (details above). That"
          echo "alone does not say whether CloudFormation accepted it, so the state is read back."
          STATE_NOW=$(current_stack_status "$STACK_NAME")
          echo "Current state of '$STACK_NAME': $STATE_NOW"
          advise_after_failure "$STACK_NAME" "$STATE_NOW"
          record_stack_failure "$STACK_NAME" "update-stack call failed (state $STATE_NOW)"
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
        echo "The deploy call for '$STACK_NAME' returned an error: the stack failed, or the call"
        echo "gave up while CloudFormation was still working. Reading the outcome ..."
        echo ""
        # `|| true`: the stack may not exist (a rejected create) or the events read
        # may be refused; neither should stop the failure from being recorded.
        aws cloudformation describe-stack-events \
          --stack-name "$STACK_NAME" \
          --query "StackEvents[?ResourceStatus=='CREATE_FAILED' || ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId, ResourceStatusReason]" \
          --output table || true
        STATE_NOW=$(current_stack_status "$STACK_NAME")
        echo "Current state of '$STACK_NAME': $STATE_NOW"
        advise_after_failure "$STACK_NAME" "$STATE_NOW"
        record_stack_failure "$STACK_NAME" "deploy (state $STATE_NOW)"
        continue
      fi
    fi

    # FINAL STATUS CHECK, both paths. A successful call above only proves that
    # CloudFormation accepted and finished the operation. An empty change set on
    # a stack that was already failed (the `--no-fail-on-empty-changeset` case
    # for simple templates, "No updates are to be performed" for nested ones)
    # finishes with the stack still CREATE_FAILED or UPDATE_FAILED and with no
    # outputs, and the next stack would die on a missing export. Success is
    # CREATE_COMPLETE, UPDATE_COMPLETE, or UPDATE_ROLLBACK_COMPLETE: the last is
    # the stable state with outputs that `continue-update-rollback` (which the
    # UPDATE_ROLLBACK_FAILED note above asks for) lands in, and a re-run whose
    # template is unchanged must not report it as a failure.
    FINAL_STATUS=$(current_stack_status "$STACK_NAME")
    if ! stack_is_healthy "$FINAL_STATUS"; then
      echo ""
      if [[ "$FINAL_STATUS" == "UNKNOWN" ]]; then
        echo "The state of '$STACK_NAME' could not be read after the deploy call, so its outcome"
        echo "is not confirmed."
      else
        echo "Current state of '$STACK_NAME' is $FINAL_STATUS; the requested healthy outcome"
        echo "(CREATE_COMPLETE, UPDATE_COMPLETE or UPDATE_ROLLBACK_COMPLETE) is not confirmed. A"
        echo "deploy call that reports no error while nothing in the template changed leaves a"
        echo "failed stack failed, with no exports."
      fi
      advise_after_failure "$STACK_NAME" "$FINAL_STATUS"
      record_stack_failure "$STACK_NAME" "final status $FINAL_STATUS"
      continue
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
    echo "The following stack(s) could not be confirmed as deployed:"
    printf '%b' "$DEPLOY_FAILED_STACKS" | while IFS= read -r s; do
      [[ -n "$s" ]] && echo "  - $s"
    done
    echo ""
    echo "Each failure above says how to retry it: a CREATE_FAILED or UPDATE_FAILED stack keeps"
    echo "what it provisioned and is updated in place by the next deploy; a stack CloudFormation"
    echo "refuses to update must be removed first with './deploy-cfn.sh cleanup'."
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
  if ! WORKSHOP_VPCS=$(aws ec2 describe-vpcs \
    --filters "Name=tag:Workshop,Values=AgentCore-Platform" \
    --query 'Vpcs[].VpcId' --output text 2>&1); then
    echo "  WARNING: could not list the workshop VPCs, so the GuardDuty unblock is skipped;"
    echo "           a VPC that still has endpoints will fail its stack delete below."
    echo "           $WORKSHOP_VPCS"
  elif [[ -z "${WORKSHOP_VPCS//[[:space:]]/}" ]]; then
    echo "  No workshop-tagged VPCs found -- nothing to unblock"
  else
    # describe-* wants one comma-separated Values list, not whitespace.
    VPC_FILTER=$(printf '%s' "$WORKSHOP_VPCS" | tr -s '[:space:]' ',')
    VPC_FILTER="${VPC_FILTER%,}"
    echo "  Limiting to workshop VPCs: $VPC_FILTER"

    if ! GD_ENDPOINTS=$(aws ec2 describe-vpc-endpoints \
      --filters "Name=vpc-endpoint-state,Values=available,pending" \
                "Name=vpc-id,Values=${VPC_FILTER}" \
      --query "VpcEndpoints[?contains(ServiceName,'guardduty')].VpcEndpointId" \
      --output text 2>&1); then
      echo "  WARNING: could not list GuardDuty VPC endpoints; none is removed here."
      echo "           $GD_ENDPOINTS"
    elif [[ -z "${GD_ENDPOINTS//[[:space:]]/}" ]]; then
      echo "  No GuardDuty VPC endpoints found"
    else
      for ep in $GD_ENDPOINTS; do
        # A successful call can still report the endpoint under "Unsuccessful";
        # and deletion is asynchronous, so success here is a request, not removal.
        if EP_OUT=$(aws ec2 delete-vpc-endpoints --vpc-endpoint-ids "$ep" --output json 2>&1) \
           && ! grep -q '"Error"' <<<"$EP_OUT"; then
          echo "  Deletion requested for GuardDuty VPC endpoint $ep (removal completes asynchronously)"
        else
          echo "  WARNING: could not delete GuardDuty VPC endpoint $ep -- it will block"
          echo "           VPC deletion. Requires ec2:DeleteVpcEndpoints on a resource"
          echo "           GuardDuty owns (it carries no Workshop tag)."
        fi
      done
    fi

    if ! GD_SGS=$(aws ec2 describe-security-groups \
      --filters "Name=group-name,Values=GuardDutyManagedSecurityGroup*" \
                "Name=vpc-id,Values=${VPC_FILTER}" \
      --query "SecurityGroups[*].GroupId" --output text 2>&1); then
      echo "  WARNING: could not list GuardDuty managed security groups; none is removed here."
      echo "           $GD_SGS"
    elif [[ -z "${GD_SGS//[[:space:]]/}" ]]; then
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
      STATUS=$(current_stack_status "$STACK_NAME")

      # The healthy set is the one the deploy's FINAL STATUS CHECK accepts
      # (stack_is_healthy), so cleanup can never delete a stack that deploy
      # reported as fine. UNKNOWN means the read failed; deleting on a guess is
      # the wrong default, so that stack is skipped and named.
      if stack_is_healthy "$STATUS" || [[ "$STATUS" == "DOES_NOT_EXIST" ]]; then
        echo "Skipping '$STACK_NAME' (status: $STATUS)"
        continue
      fi
      if [[ "$STATUS" == "UNKNOWN" ]]; then
        echo "Skipping '$STACK_NAME': its state could not be read, so it is not deleted. Check it"
        echo "in the CloudFormation console and run cleanup again."
        FAILED_STACKS="${FAILED_STACKS}${STACK_NAME} (state could not be read)\n"
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
      STATUS=$(current_stack_status "$STACK_NAME")
      if [[ "$STATUS" == "DOES_NOT_EXIST" ]]; then
        echo "[$((TEMPLATES - i))/$TEMPLATES] Stack '$STACK_NAME' does not exist -- already gone."
        echo ""
        continue
      fi
      if [[ "$STATUS" == "UNKNOWN" ]]; then
        # A failed read is not proof of absence: say so and count it as not destroyed.
        echo "[$((TEMPLATES - i))/$TEMPLATES] The state of '$STACK_NAME' could not be read, so it is"
        echo "not treated as gone and not deleted. Fix the read and run destroy again."
        FAILED_STACKS="${FAILED_STACKS}${STACK_NAME} (state could not be read)\n"
        echo ""
        continue
      fi
      echo "[$((TEMPLATES - i))/$TEMPLATES] Deleting stack '$STACK_NAME' (state $STATUS) ..."
    fi

    if ! aws cloudformation delete-stack --stack-name "$STACK_NAME"; then
      # The call returned an error; that alone does not say whether CloudFormation
      # accepted it, so the state is read back.
      STATE_NOW=$(current_stack_status "$STACK_NAME")
      case "$STATE_NOW" in
        DOES_NOT_EXIST)
          echo "delete-stack returned an error, but '$STACK_NAME' is gone."
          DELETED=$((DELETED + 1))
          echo ""
          continue ;;
        DELETE_IN_PROGRESS)
          echo "delete-stack returned an error, but '$STACK_NAME' is DELETE_IN_PROGRESS; waiting for it." ;;
        *)
          echo "WARNING: delete-stack returned an error for '$STACK_NAME' (current state $STATE_NOW)"
          echo "         -- continuing with the other stacks."
          FAILED_STACKS="${FAILED_STACKS}${STACK_NAME} (delete-stack call failed, state $STATE_NOW)\n"
          echo ""
          continue ;;
      esac
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
      STATE_NOW=$(current_stack_status "$STACK_NAME")
      case "$STATE_NOW" in
        DOES_NOT_EXIST)
          # The waiter gave up, but the stack is gone: the delete finished late.
          echo "Stack '$STACK_NAME' deleted (the waiter returned before the delete finished)."
          DELETED=$((DELETED + 1)) ;;
        *_IN_PROGRESS)
          echo "ERROR: waiting for '$STACK_NAME' ended while CloudFormation is still deleting it"
          echo "       (state $STATE_NOW). Check the stack in the console, then run destroy again."
          FAILED_STACKS="${FAILED_STACKS}${STACK_NAME} (still $STATE_NOW)\n" ;;
        UNKNOWN)
          echo "ERROR: waiting for '$STACK_NAME' ended with an error and its state could not be read."
          echo "       Check the stack in the console, then run destroy again."
          FAILED_STACKS="${FAILED_STACKS}${STACK_NAME} (state could not be read)\n" ;;
        *)
          echo "ERROR: stack '$STACK_NAME' failed to delete (state $STATE_NOW)."
          [[ -n "${REASON//[[:space:]]/}" ]] && echo "       $REASON"
          FAILED_STACKS="${FAILED_STACKS}${STACK_NAME}\n" ;;
      esac
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
  SWEEP_UNCONFIRMED=0
  for ((i = 0; i < TEMPLATES; i++)); do
    TEMPLATE_LOCATION=$(yq -r ".infrastructure.cloudformationTemplates[$i].templateLocation" "$CONTENTSPEC")
    LABEL=$(yq -r ".infrastructure.cloudformationTemplates[$i].label" "$CONTENTSPEC")

    # Sweep only behind a stack that is confirmed gone. Any other state keeps its
    # log groups: a surviving stack owns them, and an unreadable state is not
    # proof of absence.
    LABEL_STATUS=$(current_stack_status "$LABEL")
    if [[ "$LABEL_STATUS" != "DOES_NOT_EXIST" ]]; then
      if [[ "$LABEL_STATUS" == "UNKNOWN" ]]; then
        echo "  The state of '$LABEL' could not be read; its log groups are left in place."
        SWEEP_UNCONFIRMED=1
      fi
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
      if ! FOUND=$(aws logs describe-log-groups --log-group-name-prefix "$lg" \
        --query "logGroups[?logGroupName=='$lg'].logGroupName" --output text 2>&1); then
        echo "  WARNING: could not check whether log group $lg exists; it is left in place."
        SWEEP_UNCONFIRMED=1
        continue
      fi
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
    echo "  No orphaned log groups were deleted"
  fi
  if [[ "$SWEEP_UNCONFIRMED" == "1" ]]; then
    FAILED_STACKS="${FAILED_STACKS}log-group sweep incomplete (a stack or log-group state could not be read)\n"
  fi
  echo ""

  if [[ -n "$FAILED_STACKS" ]]; then
    echo "$DELETED of $TEMPLATES stack(s) deleted. The following FAILED:"
    printf '%b' "$FAILED_STACKS" | while IFS= read -r s; do
      [[ -n "$s" ]] && echo "  - $s"
    done
    echo ""
    echo "Resolve the reason above and re-run '$0 $OPERATION' -- it is safe to"
    echo "re-run, already-deleted stacks are skipped. Resources in surviving stacks may"
    echo "continue to incur charges; inspect those stacks."
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
    if HB_OUT=$(aws s3api head-bucket --bucket "$S3_BUCKET" --region "$REGION" \
         --expected-bucket-owner "$ACCOUNT_ID" 2>&1); then
      echo "Removing assets bucket '$S3_BUCKET' ..."
      if aws s3 rm "s3://$S3_BUCKET" --recursive --only-show-errors &&
         aws s3api delete-bucket --bucket "$S3_BUCKET" --region "$REGION"; then
        echo "Assets bucket removed."
      else
        echo "WARNING: '$S3_BUCKET' could not be removed. It holds only templates and"
        echo "         a copy of source/, so it costs very little, but it is yours to"
        echo "         delete: aws s3 rb s3://$S3_BUCKET --force"
      fi
    elif grep -qE '\(404\)|Not Found' <<<"$HB_OUT"; then
      # Only HeadBucket's own 404 means gone; a 403, a timeout or a credential
      # error leaves presence unconfirmed.
      echo "Assets bucket '$S3_BUCKET' is already gone."
    else
      echo "WARNING: could not confirm whether assets bucket '$S3_BUCKET' still exists:"
      echo "         $HB_OUT"
      echo "         Check it with: aws s3api head-bucket --bucket $S3_BUCKET"
      echo "         and remove it if present: aws s3 rb s3://$S3_BUCKET --force"
      BUCKET_UNCONFIRMED=1
    fi
    if [[ "${BUCKET_UNCONFIRMED:-0}" == "1" ]]; then
      exit 1
    fi
  fi
fi
