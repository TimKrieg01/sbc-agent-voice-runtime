#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="verify_trunk.sh"
VERSION="1.0.0"

BASE_DIR="${BASE_DIR:-/opt/agentvoice}"
GENERATED_DIR="${GENERATED_DIR:-$BASE_DIR/generated}"
LOG_FILE="${LOG_FILE:-/var/log/agentvoice/provisioning.log}"
ACTIVE_TRUNK_DIR="${ACTIVE_TRUNK_DIR:-$GENERATED_DIR/active/trunks}"

TRUNK_ID=""
STATE_FILE=""
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
JOB_ID="${TIMESTAMP}-$$"

json_out() {
  local status="$1"
  local code="$2"
  local message="$3"
  local extra="${4:-{}}"
  if ! printf "%s" "$extra" | jq -e . >/dev/null 2>&1; then
    extra='{}'
  fi
  jq -n \
    --arg script "$SCRIPT_NAME" \
    --arg version "$VERSION" \
    --arg timestamp "$TIMESTAMP" \
    --arg trunk_id "$TRUNK_ID" \
    --arg status "$status" \
    --arg code "$code" \
    --arg message "$message" \
    --argjson extra "$extra" \
    '{
      script: $script,
      version: $version,
      timestamp: $timestamp,
      trunk_id: $trunk_id,
      status: $status,
      code: $code,
      message: $message
    } + $extra'
}

log_json() {
  local action="$1"
  local result="$2"
  local details="${3:-{}}"
  if ! printf "%s" "$details" | jq -e . >/dev/null 2>&1; then
    details='{}'
  fi
  mkdir -p "$(dirname "$LOG_FILE")"
  jq -nc \
    --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg trunk_id "$TRUNK_ID" \
    --arg action "$action" \
    --arg result "$result" \
    --arg script "$SCRIPT_NAME" \
    --arg job_id "$JOB_ID" \
    --argjson details "$details" \
    '{
      timestamp: $timestamp,
      script: $script,
      job_id: $job_id,
      trunk_id: $trunk_id,
      action: $action,
      result: $result,
      details: $details
    }' >> "$LOG_FILE"
}

die() {
  local code="$1"
  local message="$2"
  local details="${3:-{}}"
  log_json "verify" "failure" "$details"
  json_out "failed" "$code" "$message" "$details"
  exit 1
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || die "missing_dependency" "Required command not found: $cmd"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --trunk-id)
        TRUNK_ID="${2:-}"
        shift 2
        ;;
      --state-file)
        STATE_FILE="${2:-}"
        shift 2
        ;;
      *)
        die "invalid_args" "Unknown argument: $1"
        ;;
    esac
  done

  [[ -n "$TRUNK_ID" ]] || die "invalid_args" "Missing required argument: --trunk-id"
  [[ -n "$STATE_FILE" ]] || die "invalid_args" "Missing required argument: --state-file"
  [[ "$TRUNK_ID" =~ ^[a-zA-Z0-9._-]+$ ]] || die "invalid_args" "Invalid trunk_id format"
  [[ "$STATE_FILE" = /* ]] || die "invalid_state_file" "State file must be an absolute path"
  [[ -f "$STATE_FILE" ]] || die "invalid_state_file" "State file does not exist"
  [[ -r "$STATE_FILE" ]] || die "invalid_state_file" "State file is not readable"
}

validate_state_json() {
  jq -e . "$STATE_FILE" >/dev/null 2>&1 || die "invalid_state_json" "State file is not valid JSON"
  jq -e '.ingress_hosts | type == "array"' "$STATE_FILE" >/dev/null 2>&1 || die "invalid_state_schema" "Missing ingress_hosts array"
}

verify_active_files() {
  local active_dir="$ACTIVE_TRUNK_DIR/$TRUNK_ID"
  local active_json="$active_dir/trunk_state.normalized.json"
  [[ -d "$active_dir" ]] || die "not_applied" "No active config directory found for trunk"
  [[ -f "$active_json" ]] || die "not_applied" "No active normalized state file found for trunk"

  local expected actual
  expected="$(jq -S . "$STATE_FILE")"
  actual="$(jq -S . "$active_json")"
  if [[ "$expected" != "$actual" ]]; then
    die "state_mismatch" "Active state does not match desired state"
  fi
}

verify_runtime_presence() {
  local endpoint_name="av-$TRUNK_ID"
  local output
  output="$(asterisk -rx "pjsip show endpoint $endpoint_name" 2>/dev/null || true)"
  if [[ -z "$output" ]] || printf "%s" "$output" | grep -qi "no objects found"; then
    die "runtime_mismatch" "Endpoint not visible in runtime" \
      "$(jq -n --arg endpoint "$endpoint_name" '{missing_endpoint: $endpoint}')"
  fi
}

verify_required_sections() {
  local active_dir="$ACTIVE_TRUNK_DIR/$TRUNK_ID"
  local missing=()
  local file
  for file in ingress_hosts.txt auth_users.txt routing_rules.json acls.json transport.json; do
    if [[ ! -f "$active_dir/$file" ]]; then
      missing+=("$file")
    fi
  done

  if [[ "${#missing[@]}" -gt 0 ]]; then
    die "runtime_mismatch" "Active trunk files incomplete" \
      "$(printf '%s\n' "${missing[@]}" | jq -Rsc 'split("\n")[:-1] | {missing_files: .}')"
  fi
}

main() {
  require_cmd jq
  require_cmd asterisk

  parse_args "$@"
  validate_state_json
  verify_active_files
  verify_required_sections
  verify_runtime_presence

  local details
  details="$(jq -n --arg active "$ACTIVE_TRUNK_DIR/$TRUNK_ID" '{active_path: $active}')"
  log_json "verify" "success" "$details"
  json_out "succeeded" "ok" "Trunk verification succeeded" "$details"
}

main "$@"
