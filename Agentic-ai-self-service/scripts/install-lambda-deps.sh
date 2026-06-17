#!/usr/bin/env bash
# Install Lambda dependencies into backend/lib/ with Linux x86_64 targeting.
#
# Lambda runs on Amazon Linux (x86_64), so native Python packages (like
# pydantic-core) must be compiled for that platform, not the local macOS/arm64.
#
# Usage:
#   ./scripts/install-lambda-deps.sh
#
# This is called automatically by scripts/deploy.sh, but can also be run
# manually when updating Lambda dependencies.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LIB_DIR="${PROJECT_ROOT}/backend/lib"
REQUIREMENTS="${PROJECT_ROOT}/backend/requirements-lambda.txt"

log_info() {
  echo -e "\033[1;34m[INFO]\033[0m $*"
}

log_success() {
  echo -e "\033[1;32m[SUCCESS]\033[0m $*"
}

log_error() {
  echo -e "\033[1;31m[ERROR]\033[0m $*" >&2
}

if [[ ! -f "${REQUIREMENTS}" ]]; then
  log_error "requirements-lambda.txt not found at ${REQUIREMENTS}"
  exit 1
fi

log_info "Installing Lambda dependencies into backend/lib/ (targeting Linux x86_64)..."

# Clean previous install
rm -rf "${LIB_DIR}"
mkdir -p "${LIB_DIR}"

pip3 install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  -r "${REQUIREMENTS}" \
  -t "${LIB_DIR}" \
  --quiet

log_success "Lambda dependencies installed into backend/lib/ ($(du -sh "${LIB_DIR}" | cut -f1) total)"
