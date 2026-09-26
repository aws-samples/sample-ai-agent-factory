#!/usr/bin/env bash
# Behavioural checks for scripts/recover-stranded-toolgateway.sh against a stub
# `aws` on PATH. Nothing here calls AWS.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$ROOT/scripts/recover-stranded-toolgateway.sh"
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR"' EXIT
mkdir -p "$STUB_DIR/bin"
LOG="$STUB_DIR/aws.log"

cat > "$STUB_DIR/bin/aws" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_LOG:?}"
# put-role-policy passes the document as file://PATH; capture its contents so
# the checks can assert on the actual grants, not just argv.
prev=""
for a in "$@"; do
  if [ "$prev" = "--policy-document" ]; then
    cat "${a#file://}" >> "${STUB_LOG}.policies"
  fi
  prev="$a"
done
case "$*" in
  *get-caller-identity*) echo "${STUB_ACCOUNT:-333333333333}"; exit 0 ;;
  *"iam get-role"*) exit 254 ;;                       # roles never pre-exist
  *"iam create-role"*) echo "arn:aws:iam::000000000000:role/x"; exit 0 ;;
  *"wait stack-delete-complete"*) exit "${STUB_WAIT_EXIT:-0}" ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$STUB_DIR/bin/aws"
cat > "$STUB_DIR/bin/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUB_DIR/bin/sleep"

run() {
  : > "$LOG"
  : > "$LOG.policies"
  PATH="$STUB_DIR/bin:$PATH" AWS_REGION=us-west-2 STUB_LOG="$LOG" "$@" bash "$SCRIPT" >"$STUB_DIR/out" 2>"$STUB_DIR/err" && echo "exit=0" || echo "exit=$?"
}

fail() { printf 'CHECK FAILED: %s\n' "$1" >&2; exit 1; }

# 1. Wrong account -> refuses before any mutation.
r=$(run env AGENTICAI_EXPECTED_ACCOUNT=111111111111 STUB_ACCOUNT=333333333333)
[ "$r" = "exit=2" ] || fail "wrong account should exit 2, got $r"
grep -q "create-role" "$LOG" && fail "wrong account must not create roles"

# 2. Wait fails -> exit 1, roles created but NEVER deleted.
r=$(run env AGENTICAI_EXPECTED_ACCOUNT=333333333333 STUB_WAIT_EXIT=255)
[ "$r" = "exit=1" ] || fail "failed wait should exit 1, got $r"
grep -q "iam create-role --role-name AgenticAI-D03-nonprod-GatewayAdmin" "$LOG" || fail "roles should be created"
grep -q "bedrock-agentcore:DeleteGatewayTarget" "$LOG.policies" || fail "DeleteGatewayTarget grant must be present"
grep -q "bedrock-agentcore:Create" "$LOG.policies" && fail "recovery role must not carry create grants"
grep -q "iam delete-role " "$LOG" && fail "roles must be kept when a wait fails"
grep -q "describe-stack-events" "$LOG" || fail "failure path must dump FAILED events"

# 3. Happy path -> all four roles removed after both stacks are gone.
r=$(run env AGENTICAI_EXPECTED_ACCOUNT=333333333333)
[ "$r" = "exit=0" ] || fail "happy path should exit 0, got $r"
[ "$(grep -c 'iam delete-role --role-name' "$LOG")" = "4" ] || fail "expected 4 role deletions"
[ "$(grep -c 'cloudformation delete-stack' "$LOG")" = "2" ] || fail "expected 2 stack deletions"
# Deletion of roles must come after the last wait.
last_wait=$(grep -n "wait stack-delete-complete" "$LOG" | tail -1 | cut -d: -f1)
first_del=$(grep -n "iam delete-role " "$LOG" | head -1 | cut -d: -f1)
[ "$first_del" -gt "$last_wait" ] || fail "roles deleted before the stacks were confirmed gone"

echo "all checks passed"
