#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="apply_trunk.sh"
VERSION="1.0.0"

BASE_DIR="${BASE_DIR:-/opt/agentvoice}"
STATE_DIR="${STATE_DIR:-$BASE_DIR/state}"
GENERATED_DIR="${GENERATED_DIR:-$BASE_DIR/generated}"
BACKUP_DIR="${BACKUP_DIR:-$BASE_DIR/backups}"
LOG_FILE="${LOG_FILE:-/var/log/agentvoice/provisioning.log}"

ACTIVE_TRUNK_DIR="${ACTIVE_TRUNK_DIR:-$GENERATED_DIR/active/trunks}"
CANDIDATE_ROOT="${CANDIDATE_ROOT:-$STATE_DIR/candidates}"

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
  log_json "apply" "failure" "$details"
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

  jq -e --arg trunk_id "$TRUNK_ID" '
    .trunk_id? as $tid
    | ($tid == null or $tid == $trunk_id)
    and (.ingress_hosts | type == "array")
    and (.auth_users | type == "array")
    and (.routing_rules | type == "array")
    and (.acl.signaling_cidrs | type == "array")
    and (.acl.media_cidrs | type == "array")
    and (.transport.allowed_transports | type == "array" and length > 0)
    and (.transport.signaling_ports | type == "array" and length > 0)
    and (.transport.rtp.port_start | numbers)
    and (.transport.rtp.port_end | numbers)
    and (.transport.rtp.port_start <= .transport.rtp.port_end)
  ' "$STATE_FILE" >/dev/null 2>&1 || die "invalid_state_schema" "State JSON missing required fields or types"
}

active_channel_count() {
  local output
  output="$(asterisk -rx "core show channels count" 2>/dev/null || true)"
  local count
  count="$(printf "%s" "$output" | grep -Eo '[0-9]+' | head -n 1 || true)"
  if [[ -z "${count:-}" ]]; then
    echo "0"
  else
    echo "$count"
  fi
}

maybe_defer_for_active_calls() {
  local requires_disruptive
  requires_disruptive="$(jq -r '.requires_disruptive_action // false' "$STATE_FILE")"
  if [[ "$requires_disruptive" != "true" ]]; then
    return 0
  fi

  local count
  count="$(active_channel_count)"
  if [[ "$count" =~ ^[0-9]+$ ]] && [[ "$count" -gt 0 ]]; then
    die "deferred_active_calls" "Disruptive action blocked while calls are active" \
      "$(jq -n --arg count "$count" '{active_channels: ($count|tonumber)}')"
  fi
}

render_candidate_files() {
  local candidate_dir="$CANDIDATE_ROOT/$TRUNK_ID-$TIMESTAMP"
  mkdir -p "$candidate_dir"

  local normalized_json="$candidate_dir/trunk_state.normalized.json"
  local ingress_file="$candidate_dir/ingress_hosts.txt"
  local auth_file="$candidate_dir/auth_users.txt"
  local routing_file="$candidate_dir/routing_rules.json"
  local acl_file="$candidate_dir/acls.json"
  local transport_file="$candidate_dir/transport.json"
  local pjsip_file="$candidate_dir/pjsip_trunk_${TRUNK_ID}.conf"

  jq -S . "$STATE_FILE" > "$normalized_json"
  jq -r '.ingress_hosts[]' "$STATE_FILE" > "$ingress_file"
  jq -r '.auth_users[]' "$STATE_FILE" > "$auth_file"
  jq -S '.routing_rules' "$STATE_FILE" > "$routing_file"
  jq -S '.acl' "$STATE_FILE" > "$acl_file"
  jq -S '.transport' "$STATE_FILE" > "$transport_file"

  {
    echo "; generated by $SCRIPT_NAME at $TIMESTAMP"
    echo "; trunk_id=$TRUNK_ID"
    echo
    echo "[av-${TRUNK_ID}]"
    echo "type=endpoint"
    echo "context=from-twilio"
    echo "disallow=all"
    echo "allow=ulaw"
    echo "direct_media=no"
    echo "rtp_symmetric=yes"
    echo "rewrite_contact=yes"
    echo "force_rport=yes"

    local tls_enabled
    tls_enabled="$(jq -r '.transport.tls_enabled // false' "$STATE_FILE")"
    if [[ "$tls_enabled" == "true" ]]; then
      echo "media_encryption=sdes"
    fi
    echo
    echo "[av-identify-${TRUNK_ID}]"
    echo "type=identify"
    echo "endpoint=av-${TRUNK_ID}"
    while IFS= read -r cidr; do
      [[ -n "$cidr" ]] && echo "match=$cidr"
    done < <(jq -r '.acl.signaling_cidrs[]' "$STATE_FILE")
  } > "$pjsip_file"

  echo "$candidate_dir"
}

syntax_checks() {
  local candidate_dir="$1"
  [[ -s "$candidate_dir/trunk_state.normalized.json" ]] || die "render_failed" "Normalized state file missing"
  [[ -s "$candidate_dir/pjsip_trunk_${TRUNK_ID}.conf" ]] || die "render_failed" "PJSIP candidate file missing"

  asterisk -rx "core show version" >/dev/null 2>&1 || die "asterisk_unavailable" "Asterisk CLI check failed"
}

backup_active_files() {
  local backup_path="$BACKUP_DIR/$TRUNK_ID/$TIMESTAMP"
  mkdir -p "$backup_path"

  if [[ -d "$ACTIVE_TRUNK_DIR/$TRUNK_ID" ]]; then
    cp -a "$ACTIVE_TRUNK_DIR/$TRUNK_ID/." "$backup_path/" || die "backup_failed" "Failed to backup active trunk files"
  fi
  echo "$backup_path"
}

atomic_replace_active_files() {
  local candidate_dir="$1"
  local staged_dir="$ACTIVE_TRUNK_DIR/.staged-$TRUNK_ID-$TIMESTAMP"
  local trunk_dir="$ACTIVE_TRUNK_DIR/$TRUNK_ID"

  mkdir -p "$ACTIVE_TRUNK_DIR"
  rm -rf "$staged_dir"
  mkdir -p "$staged_dir"

  cp -a "$candidate_dir/." "$staged_dir/"
  mv "$staged_dir" "$trunk_dir.new"
  mv -Tf "$trunk_dir.new" "$trunk_dir"
}

safe_reload() {
  asterisk -rx "pjsip reload" >/dev/null 2>&1 || return 1
  return 0
}

health_check() {
  local endpoint_name="av-$TRUNK_ID"
  local output
  output="$(asterisk -rx "pjsip show endpoint $endpoint_name" 2>/dev/null || true)"
  [[ -n "$output" ]] || return 1
  if printf "%s" "$output" | grep -qi "no objects found"; then
    return 1
  fi
  return 0
}

rollback() {
  local backup_path="$1"
  local trunk_dir="$ACTIVE_TRUNK_DIR/$TRUNK_ID"
  if [[ -d "$backup_path" ]]; then
    rm -rf "$trunk_dir"
    mkdir -p "$trunk_dir"
    cp -a "$backup_path/." "$trunk_dir/" || true
    safe_reload || true
    return 0
  fi
  return 1
}

main() {
  require_cmd jq
  require_cmd asterisk
  require_cmd cp
  require_cmd mv
  require_cmd mkdir
  require_cmd date

  parse_args "$@"
  validate_state_json
  maybe_defer_for_active_calls

  local candidate_dir
  candidate_dir="$(render_candidate_files)"
  syntax_checks "$candidate_dir"

  local backup_path
  backup_path="$(backup_active_files)"

  if ! atomic_replace_active_files "$candidate_dir"; then
    die "apply_failed" "Atomic replace failed"
  fi

  if ! safe_reload; then
    rollback "$backup_path" >/dev/null 2>&1 || true
    die "reload_failed" "Asterisk safe reload failed" \
      "$(jq -n --arg backup "$backup_path" '{backup_path: $backup, rollback: "attempted"}')"
  fi

  if ! health_check; then
    local rollback_result="failed"
    if rollback "$backup_path"; then
      rollback_result="succeeded"
    fi
    die "health_check_failed" "Post-reload health check failed" \
      "$(jq -n --arg backup "$backup_path" --arg rollback "$rollback_result" '{backup_path: $backup, rollback: $rollback}')"
  fi

  local extra
  extra="$(jq -n \
    --arg candidate "$candidate_dir" \
    --arg backup "$backup_path" \
    --arg active "$ACTIVE_TRUNK_DIR/$TRUNK_ID" \
    '{candidate_dir: $candidate, backup_path: $backup, active_path: $active}')"
  log_json "apply" "success" "$extra"
  json_out "succeeded" "ok" "Trunk applied successfully" "$extra"
}

main "$@"
