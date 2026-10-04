#!/usr/bin/env bash
# Build aarch64-targeted Python dependency bundles for AgentCore Runtime.
#
# AgentCore Runtime enforces a 30-second init limit. Pre-building deps
# into zip bundles eliminates the pip install phase during cold start.
#
# Produces these bundles in backend/agentcore-deps/:
#   base.zip       — bedrock-agentcore + boto3 (Templates 1, 2, default)
#   strands-mcp.zip — bedrock-agentcore + boto3 + strands-agents + strands-agents-tools + mcp (Template 3, tools)
#   mcp-lean.zip   — bedrock-agentcore + boto3 + mcp only (generated MCP servers)
#   provider-<extra>.zip — the model-provider SDK for a non-Bedrock agent, as a DELTA
#                  on top of strands-mcp.zip (openai, anthropic, gemini, litellm,
#                  mistral, ollama, sagemaker, llamaapi). Eight bundles, not twelve:
#                  groq/deepseek/writer emit OpenAIModel and together emits LiteLLMModel,
#                  so four of the twelve providers share another's bundle.
#
# Every bundle includes boto3/botocore (NOT pre-installed in AgentCore Runtime)
# and strips __pycache__ directories and .pyc files.
#
# Why the provider bundles exist: none of the first three carries a model-provider SDK,
# and nothing pip-installs at container start, so every one of the twelve non-Bedrock
# providers produced a runtime that deployed `succeeded` and then died at
# `from strands.models.openai import OpenAIModel` with ModuleNotFoundError — visible
# only as AgentCore's "Runtime initialization time exceeded … 30s". Measured live.
# They are separate zips, and deltas rather than whole trees, because that same 30s
# budget is spent on every cold start: a canvas pays for the providers it uses and
# nothing else. Fetching them at deploy or run time instead is not an option —
# ARCC cnt_Vsqr5LAdJVd1Il requires third-party packages to be served from
# infrastructure we control.
#
# The extras list below is pinned against backend/src/app/services/code_generator.py's
# PROVIDER_STRANDS_EXTRA by backend/tests/test_provider_sdks_ship_in_a_bundle.py, so a
# provider added in one place cannot be forgotten in the other.
#
# Usage:
#   ./scripts/install-agentcore-deps.sh
#
# This is called automatically by scripts/deploy.sh before CDK deploy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_DIR="${PROJECT_ROOT}/backend/agentcore-deps"

# Version pins for every bundle, shared with backend/pyproject.toml's `dev` extra so the
# test environment exercises the same import surface the container gets. A pip CONSTRAINT
# file rather than a requirements file: it pins TRANSITIVE resolutions (the whole
# OpenTelemetry graph, and `mcp` when it arrives via strands-agents[<extra>]) without
# adding a package to a bundle that did not ask for one. Read that file for why each pin
# exists and which packages are deliberately left floating.
#
# Passed to EVERY install_packages call, including the provider deltas. That last part is
# the point: the old `mcp_pin` was passed to three bundles and not to the eight provider
# deltas, so each delta resolved mcp 2.1.1 as a transitive of strands-agents[<extra>] and
# shipped a partial 2.x tree on top of the pinned 1.x.
CONSTRAINTS_FILE="${PROJECT_ROOT}/backend/agentcore-deps-constraints.txt"

if [[ ! -f "${CONSTRAINTS_FILE}" ]]; then
  echo "[ERROR] missing ${CONSTRAINTS_FILE}; refusing to build unpinned bundles" >&2
  exit 1
fi

PIP_PLATFORM_FLAGS=(
  --platform manylinux2014_aarch64
  --python-version 3.13
  --implementation cp
  --only-binary=:all:
)

# ── Helper functions ──────────────────────────────────────────────────

log_info() {
  echo -e "\033[1;34m[INFO]\033[0m $*"
}

log_success() {
  echo -e "\033[1;32m[SUCCESS]\033[0m $*"
}

log_error() {
  echo -e "\033[1;31m[ERROR]\033[0m $*" >&2
}

# Install packages into a target directory.
install_packages() {
  local target_dir="$1"
  shift
  local packages=("$@")

  mkdir -p "${target_dir}"

  local constraint_flags=(--constraint "${CONSTRAINTS_FILE}")
  # A second, GENERATED constraint used only for the provider deltas: see
  # emit_tree_constraints below.
  if [[ -n "${EXTRA_CONSTRAINTS:-}" && -f "${EXTRA_CONSTRAINTS}" ]]; then
    constraint_flags+=(--constraint "${EXTRA_CONSTRAINTS}")
  fi

  pip3 install \
    "${PIP_PLATFORM_FLAGS[@]}" \
    "${constraint_flags[@]}" \
    --target "${target_dir}" \
    --quiet \
    "${packages[@]}"

  remove_cache_files "${target_dir}"
}

# Emit a pip constraint file naming the EXACT version of every distribution installed in
# a tree, skipping anything CONSTRAINTS_FILE already pins (pip rejects a package
# constrained twice, even to the same version).
#
# This is what makes a provider DELTA sound. The delta subtracts the baseline by FILE
# PATH, so a shared distribution that resolves to a different version in the provider's
# own resolution loses only its overlapping paths: its version-unique modules and its
# dist-info survive, and the container ends up with two versions of one package merged
# into a single tree. Measured across the eight deltas built without this:
#
#   mcp        2.1.1 over the baseline's 1.30.0, in ALL EIGHT. 62 orphan 2.x modules
#              plus a second dist-info, so importlib.metadata.version("mcp") is a coin
#              flip, and mcp 2.x's own httpx2/httpcore2/mcp_types came along for the ride
#              (provider-litellm.zip: 26 MB against provider-anthropic.zip's 2 MB, spent
#              against AgentCore's hard 30s cold-start budget).
#   websockets 16.1.1 over the baseline's 17.1, in provider-gemini.zip -- a DOWNGRADE,
#              because google-genai caps it lower than strands does.
#
# Pinning the offenders by hand does not generalize: any shared distribution can do this,
# and the next one is found in production. Constraining each delta to the baseline's own
# resolution removes the whole class -- every shared distribution then resolves to the
# same version, every one of its paths overlaps, and the subtraction deletes it entirely.
#
# If a provider SDK cannot accept a baseline version, pip FAILS HERE, loudly. That is the
# correct outcome and it is not hypothetical: the first build with these constraints stopped
# at provider-gemini with ResolutionImpossible, because every published google-genai caps
# websockets<17.0 while the baseline's own free resolution took 17.1. The lesson is that the
# BASELINE is the thing that has to give -- it must resolve inside the intersection of every
# provider extra's ranges, not at the newest version it can reach on its own -- so the fix
# is a pin in CONSTRAINTS_FILE (websockets==16.1.1), not a per-delta exemption. A delta
# carrying its own version of a shared distribution is unsound by construction, whatever
# makes it happen. Read this failure as "the baseline is wrong", never as "skip the
# constraint for this provider".
emit_tree_constraints() {
  local tree_dir="$1"
  local out_file="$2"
  local d base name ver normalized

  : >"${out_file}"
  for d in "${tree_dir}"/*.dist-info; do
    [ -d "${d}" ] || continue
    base="$(basename "${d}" .dist-info)"
    name="${base%-*}"
    ver="${base##*-}"
    [ -n "${name}" ] && [ -n "${ver}" ] || continue
    # Skip names CONSTRAINTS_FILE already pins, comparing PEP 503-normalized.
    normalized="$(printf '%s' "${name}" | tr '[:upper:]_.' '[:lower:]--')"
    if grep -qiE "^[[:space:]]*${normalized}[[:space:]]*==" \
      <(tr '_.' '--' <"${CONSTRAINTS_FILE}"); then
      continue
    fi
    printf '%s==%s\n' "${name}" "${ver}" >>"${out_file}"
  done
  LC_ALL=C sort -o "${out_file}" "${out_file}"
}

# Remove __pycache__ directories and .pyc files.
remove_cache_files() {
  local target_dir="$1"

  find "${target_dir}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
  find "${target_dir}" -type f -name "*.pyc" -delete 2>/dev/null || true
}

# Create a zip from a directory's contents and remove the directory.
create_bundle_zip() {
  local target_dir="$1"
  local zip_path="$2"

  (cd "${target_dir}" && zip -r -q "${zip_path}" .)
  rm -rf "${target_dir}"
}

# Create a zip from a directory's contents and KEEP the directory. Needed for
# strands-mcp, whose installed tree is the baseline the provider deltas subtract.
create_bundle_zip_keep() {
  local target_dir="$1"
  local zip_path="$2"

  (cd "${target_dir}" && zip -r -q "${zip_path}" .)
}

# Build one provider-extras bundle: strands-agents[<extra>] MINUS everything the
# strands-mcp bundle already carries.
#
# The subtraction is what keeps these small (a few MB instead of ~45), and it is safe
# because a provider bundle is only ever merged on top of strands-mcp.zip: every
# generated non-Bedrock agent imports strands, which is exactly what selects that
# bundle in codegen_step. If that ever stops being true the provider bundle alone will
# not be importable — backend/tests/test_provider_sdks_ship_in_a_bundle.py pins it.
#
# Empty is a FAILURE, not a no-op: an empty provider-openai.zip would merge cleanly and
# reproduce the original defect exactly, with a green deploy over a dead container.
build_provider_bundle() {
  local extra="$1"
  local base_manifest="$2"
  local prov_dir="${OUTPUT_DIR}/provider-${extra}"

  log_info "Building provider-${extra} bundle (strands-agents[${extra}] delta over strands-mcp)..."
  install_packages "${prov_dir}" "strands-agents[${extra}]"

  # Drop every path the base bundle already has. Read from a manifest file rather
  # than re-walking the base tree so the baseline cannot drift mid-loop.
  while IFS= read -r rel; do
    [ -n "${rel}" ] && rm -f "${prov_dir}/${rel}" 2>/dev/null
  done <"${base_manifest}"
  find "${prov_dir}" -type d -empty -delete 2>/dev/null || true

  if [ -z "$(ls -A "${prov_dir}" 2>/dev/null)" ]; then
    log_error "provider-${extra}.zip would be EMPTY — strands-agents[${extra}] resolved to"
    log_error "nothing the strands-mcp bundle does not already have. Either the extra name"
    log_error "is wrong (check PROVIDER_STRANDS_EXTRA in code_generator.py against"
    log_error "'Provides-Extra' in the strands-agents wheel metadata) or pip silently"
    log_error "skipped a wheel for ${PIP_PLATFORM_FLAGS[*]}. Shipping it would recreate the"
    log_error "very defect these bundles exist to fix: a green deploy over a container that"
    log_error "cannot import."
    return 1
  fi

  create_bundle_zip "${prov_dir}" "${OUTPUT_DIR}/provider-${extra}.zip"
}

# ── Main ──────────────────────────────────────────────────────────────

main() {
  log_info "Installing AgentCore deps into backend/agentcore-deps/ (targeting aarch64)"

  # Idempotent: clean and rebuild output directory each run
  rm -rf "${OUTPUT_DIR}"
  mkdir -p "${OUTPUT_DIR}"

  # OpenTelemetry packages — required when the Observability node is wired
  # to push traces to any OTLP backend (Langfuse, Phoenix, Honeycomb, AgentCore
  # native CloudWatch sidecar, etc.). Strands' setup_otlp_exporter() lazily
  # imports the HTTP exporter, so it MUST be in the bundle.
  local otel_packages=(
    "opentelemetry-api"
    "opentelemetry-sdk"
    "opentelemetry-semantic-conventions"
    "opentelemetry-exporter-otlp-proto-http"
  )

  # PIN, do not float. Everything code_generator.py / deployment.py emits is
  # mcp 1.x: `from mcp.client.streamable_http import streamablehttp_client`
  # and `from mcp.server.fastmcp import FastMCP`. mcp 2.x renamed BOTH
  # (streamable_http_client, mcp.server.mcpserver.MCPServer) with no
  # back-compat alias, and 2.x's own migration note says to pin "mcp<2" to
  # keep v1 code running.
  #
  # Left unpinned this broke every gateway-connected agent AND every generated
  # MCP server: the container died at import, so the only symptom surfaced to
  # the user was InvokeAgentRuntime's "Runtime initialization time exceeded.
  # Please make sure that initialization completes in 30s" — which reads like a
  # cold-start/perf problem and sends you looking in the wrong place entirely.
  # The two bundles below even resolved to *different* versions in one build
  # run (2.1.1 and 2.2.0), which is what an unpinned transitive floats to.
  # Bump this only together with the emitted imports in
  # backend/src/app/services/{code_generator,deployment}.py.
  #
  # Kept even though CONSTRAINTS_FILE now pins mcp to an exact 1.x, for two reasons.
  # It is the declaration of the API BOUND -- why 2.x is unacceptable at all, which an
  # `==` line cannot express -- and it is what makes `mcp` an explicit, named install in
  # the three bundles that need it rather than a transitive that happens to be pinned.
  # It is no longer the whole mechanism: this bound was only ever passed to these three
  # bundles, and the eight provider deltas below resolved mcp 2.1.1 through
  # strands-agents[<extra>] without it. The constraint file is what covers those.
  local mcp_pin="mcp<2"

  # Bundle 1: base (bedrock-agentcore + boto3 + opentelemetry)
  log_info "Building base bundle (bedrock-agentcore + boto3 + opentelemetry)..."
  local base_dir="${OUTPUT_DIR}/base"
  install_packages "${base_dir}" bedrock-agentcore boto3 "${otel_packages[@]}"
  create_bundle_zip "${base_dir}" "${OUTPUT_DIR}/base.zip"

  # Bundle 2: strands-mcp (everything in base + strands-agents + strands-agents-tools + mcp)
  log_info "Building strands-mcp bundle (bedrock-agentcore + boto3 + strands-agents + strands-agents-tools + mcp + opentelemetry)..."
  local strands_dir="${OUTPUT_DIR}/strands-mcp"
  install_packages "${strands_dir}" bedrock-agentcore boto3 strands-agents strands-agents-tools "${mcp_pin}" "${otel_packages[@]}"
  # Keep the tree: it is the baseline every provider delta subtracts.
  create_bundle_zip_keep "${strands_dir}" "${OUTPUT_DIR}/strands-mcp.zip"
  local strands_manifest="${OUTPUT_DIR}/.strands-mcp.manifest"
  (cd "${strands_dir}" && find . -type f | sed 's|^\./||' | LC_ALL=C sort) >"${strands_manifest}"
  # The baseline's own resolution, as constraints for the provider deltas below. Emitted
  # from the tree that was just BUILT, not from a checked-in list, so it cannot disagree
  # with the artifact the deltas are actually subtracted from. See emit_tree_constraints.
  local baseline_constraints="${OUTPUT_DIR}/.strands-mcp.constraints"
  emit_tree_constraints "${strands_dir}" "${baseline_constraints}"
  log_info "Baseline pins for provider deltas: $(wc -l <"${baseline_constraints}" | tr -d ' ') distributions"
  rm -rf "${strands_dir}"

  # Bundle 3: mcp-lean (Bug 171) — the generated MCP SERVER only does
  # `from mcp.server.fastmcp import FastMCP` (+ json/os); it does NOT import
  # strands or otel. Bundling it with the heavy strands-mcp.zip made the MCP
  # container cold-start exceed the Gateway's hard 30s tool-discovery probe, so
  # the MCP target landed FAILED ("Runtime initialization time exceeded ...
  # 30s") and the gateway served 0 tools. A lean bundle (just mcp + bedrock-
  # agentcore runtime + boto3, NO strands, NO otel) cuts cold-start well under
  # the probe limit. The MCP server step prefers this bundle, falling back to
  # strands-mcp.zip if absent.
  log_info "Building mcp-lean bundle (bedrock-agentcore + boto3 + mcp only — fast MCP-server cold start)..."
  local mcplean_dir="${OUTPUT_DIR}/mcp-lean"
  install_packages "${mcplean_dir}" bedrock-agentcore boto3 "${mcp_pin}"
  create_bundle_zip "${mcplean_dir}" "${OUTPUT_DIR}/mcp-lean.zip"

  # Bundles 4..n: one per model-provider SDK. MUST stay in sync with
  # PROVIDER_STRANDS_EXTRA in backend/src/app/services/code_generator.py — the test
  # named in the header greps this array, so do not inline the list anywhere else.
  local provider_extras=(
    openai
    anthropic
    gemini
    litellm
    mistral
    ollama
    sagemaker
    llamaapi
  )
  # NOT `writer`: strands-agents publishes a `writer` extra, but _get_model_init_code
  # emits `from strands.models.openai import OpenAIModel` for provider "writer" (it is
  # an OpenAI-compatible endpoint), so provider_bundle_key("writer") is never asked for
  # and provider-writer.zip would be a 1.0 MB artifact nothing can ever download.
  # Measured: it built fine and was dead weight. Same reasoning for groq/deepseek/
  # together, which map to the openai and litellm extras.
  # Every delta below resolves against the baseline's exact versions, so that a shared
  # distribution's paths all overlap and the subtraction removes it completely. Scoped to
  # this loop and unset afterwards: the three bundles above ARE the baseline and must
  # resolve freely within CONSTRAINTS_FILE.
  local extra
  EXTRA_CONSTRAINTS="${baseline_constraints}"
  for extra in "${provider_extras[@]}"; do
    build_provider_bundle "${extra}" "${strands_manifest}"
  done
  unset EXTRA_CONSTRAINTS
  rm -f "${strands_manifest}" "${baseline_constraints}"

  local base_size strands_size mcplean_size
  base_size=$(du -sh "${OUTPUT_DIR}/base.zip" | cut -f1)
  strands_size=$(du -sh "${OUTPUT_DIR}/strands-mcp.zip" | cut -f1)
  mcplean_size=$(du -sh "${OUTPUT_DIR}/mcp-lean.zip" | cut -f1)

  log_success "Bundles created: base.zip (${base_size}), strands-mcp.zip (${strands_size}), mcp-lean.zip (${mcplean_size})"
  for extra in "${provider_extras[@]}"; do
    log_success "  provider-${extra}.zip ($(du -sh "${OUTPUT_DIR}/provider-${extra}.zip" | cut -f1))"
  done
}

main "$@"
