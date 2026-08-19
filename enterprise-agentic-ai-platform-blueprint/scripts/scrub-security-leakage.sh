#!/usr/bin/env bash
# scrub-security-leakage.sh — source-tree leakage verifier.
#
# Fails if anything that would be committed contains an AWS account ID outside
# the documented placeholder set, a developer-specific absolute path, or a
# value listed in the site-specific deny-list. Idempotent — a second run on a
# clean tree does nothing and exits zero.
#
# Run this:
#   * Before every commit (git pre-commit hook)
#   * In CI, alongside gitleaks (see .gitleaks.toml)
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- What gets scanned ------------------------------------------------------
#
# Everything git would commit: tracked files plus untracked files that are not
# ignored. Deriving the list from git rather than hardcoding directory names is
# deliberate — build outputs, virtualenvs, dependency trees and local tool
# state are already ignored, so they need no mention here. That matters because
# naming them would put site-specific strings into a published file, which is
# the very thing this script exists to prevent.
#
# Outside a git work tree (an extracted tarball, say) fall back to a plain walk
# with the usual build directories pruned.
list_files() {
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git ls-files -z --cached --others --exclude-standard
  else
    find . -type f \
      \( -path './.git' -o -name node_modules -o -name '.venv' \
         -o -name '__pycache__' -o -name '.pytest_cache' -o -name dist \
         -o -name 'cdk.out*' \) -prune -o -type f -print0
  fi
}

# Files that legitimately carry the patterns and so cannot be scanned for them:
# this script, the paired gitleaks config, and the deny-list itself.
SELF_EXEMPT_RE='(^|/)(scrub-security-leakage\.sh|\.gitleaks\.toml|\.scrub-denylist\.local)$'

FILES=()
while IFS= read -r -d '' f; do
  f="${f#./}"
  [[ "$f" =~ $SELF_EXEMPT_RE ]] && continue
  FILES+=("$f")
done < <(list_files)

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "[FAIL] scrub-security-leakage.sh: no files to scan — is this a repository?"
  exit 1
fi

# --- Generic patterns -------------------------------------------------------
#
# Only classes whose *shape* is safe to publish belong in this file. A pattern
# whose value is itself identifying — an internal hostname, an internal tool or
# system name, an account ID, a developer alias, an event id — must live in the
# untracked deny-list instead. Writing such a value here would publish exactly
# what the rule exists to catch, and this file is exempt from its own scan, so
# nothing would flag it.
REGEX_PATTERNS=(
  "/Users/[A-Za-z0-9._-]+"   # hardcoded absolute developer home paths
  "/home/[A-Za-z0-9._-]+"    # ditto, Linux
)

# --- Site-specific deny-list ------------------------------------------------
#
# Optional, untracked, one pattern per line; blank lines and '#' comments
# ignored. This is where an adopting organisation puts the literals it must
# never publish: internal hostnames and tool names, confidentiality markings,
# historical identifiers inherited from an archived source tree. Keeping them
# out of the tree means the deny-list can be specific without leaking.
EXTRA_PATTERNS_FILE="${SCRUB_EXTRA_PATTERNS_FILE:-$REPO_ROOT/.scrub-denylist.local}"

fail=0

report() {
  echo "[LEAKAGE] $1 matched in:"
  echo "$2" | sed 's/^/    /'
  fail=1
}

# Both checkers must return 0 even when they find nothing: the script runs
# under `set -e`, so a bare test as the last statement would abort the run.
check_literal() {
  local matches
  matches=$(printf '%s\0' "${FILES[@]}" \
    | xargs -0 grep -lI --binary-files=without-match -- "$1" 2>/dev/null || true)
  if [[ -n "$matches" ]]; then report "Pattern '$1'" "$matches"; fi
  return 0
}

check_regex() {
  local matches
  matches=$(printf '%s\0' "${FILES[@]}" \
    | xargs -0 grep -lIE --binary-files=without-match -- "$1" 2>/dev/null || true)
  if [[ -n "$matches" ]]; then report "Pattern /$1/" "$matches"; fi
  return 0
}

for pattern in "${REGEX_PATTERNS[@]}"; do
  check_regex "$pattern"
done

if [[ -f "$EXTRA_PATTERNS_FILE" ]]; then
  while IFS= read -r pattern; do
    [[ -z "$pattern" || "$pattern" == \#* ]] && continue
    check_literal "$pattern"
  done < "$EXTRA_PATTERNS_FILE"
fi

# --- AWS account IDs --------------------------------------------------------
#
# Any 12-digit run in a text file is a leak unless it is a recognised
# placeholder. This is the rule that generically protects against real account
# IDs, so it deliberately scans every text file rather than an extension list:
# a leak in an extensionless file or a diagram source counts the same.
#
# Allowed placeholders:
#   123456789012                — AWS-canonical documentation example.
#   000000000000..000000000099  — Low-number placeholders used in examples
#                                 (cdk.context.json, README samples).
#   Repeated-digit sequences    — Test fixtures only (111...1, 222...2, etc.).
#                                 Production code must never contain these.
account_hits=$(
  printf '%s\0' "${FILES[@]}" \
    | xargs -0 grep -hIEo --binary-files=without-match "[0-9]{12}" 2>/dev/null \
    | sort -u \
    | grep -v '^123456789012$' \
    | grep -vE '^0{10}[0-9]{2}$' \
    | grep -vE '^([0-9])\1{11}$' \
    || true
)
if [[ -n "$account_hits" ]]; then
  echo "[LEAKAGE] Unexpected 12-digit AWS account IDs detected (allow-list: 123456789012, 000000000000..99, repeated digits):"
  echo "$account_hits" | sed 's/^/    /'
  echo "          Locate them with:"
  echo "$account_hits" | sed 's/^/            git grep -n /'
  fail=1
fi

if [[ $fail -eq 0 ]]; then
  echo "[OK] scrub-security-leakage.sh: no leakage patterns found."
  exit 0
fi

echo ""
echo "[FAIL] scrub-security-leakage.sh found leakage. Remove the offending"
echo "       value, or — if it is a legitimate placeholder — extend the"
echo "       allow-list above in the same commit."
exit 1
