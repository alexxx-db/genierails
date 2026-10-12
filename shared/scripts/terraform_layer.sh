#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "Usage: $0 <layer> <env> <command> [args...]" >&2
  exit 1
fi

LAYER="$1"
ENV_NAME="$2"
COMMAND="$3"
shift 3

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOTS_DIR="$PROJECT_ROOT/roots"
ENVS_DIR="${ENVS_DIR:-$PROJECT_ROOT/envs}"

case "$LAYER" in
  account)
    ROOT_DIR="$ROOTS_DIR/account"
    ENV_DIR="${LAYER_ENV_DIR:-$ENVS_DIR/account}"
    ;;
  data_access)
    ROOT_DIR="$ROOTS_DIR/data_access"
    if [ -n "${LAYER_ENV_DIR:-}" ]; then
      ENV_DIR="$LAYER_ENV_DIR"
    elif [ "$ENV_NAME" = "data_access" ] && [ -d "$ENVS_DIR/data_access" ]; then
      ENV_DIR="$ENVS_DIR/data_access"
    else
      ENV_DIR="$ENVS_DIR/$ENV_NAME/data_access"
    fi
    ;;
  workspace)
    ROOT_DIR="$ROOTS_DIR/workspace"
    ENV_DIR="${LAYER_ENV_DIR:-$ENVS_DIR/$ENV_NAME}"
    ;;
  *)
    echo "Unknown layer: $LAYER" >&2
    exit 1
    ;;
esac

# The env dir that owns this layer's state and remote_state.hcl: the dir the
# layer runs in (envs/<env>, or envs/account), never one rebuilt from
# ENVS_DIR, so the CI guard (state_layout.py ci-allowed) and this runner read
# the same remote_state.hcl.
STATE_ENV_DIR="$ENV_DIR"
if [ "$LAYER" = "data_access" ] && [ "$(dirname "$ENV_DIR")" != "$ENVS_DIR" ]; then
  STATE_ENV_DIR="$(dirname "$ENV_DIR")"
fi
# state-pull prints only the state on stdout (state_layout.py state_json reads it).
if [ "$COMMAND" = "state-pull" ]; then
  exec 3>&1 1>&2
fi
STATE_BASE="$ENV_DIR"
USE_STATE_LAYOUT=0
if [ -n "${GENIERAILS_STATE_DIR:-}" ] || [ -f "$STATE_ENV_DIR/remote_state.hcl" ]; then
  USE_STATE_LAYOUT=1
fi
# Refuses a moved-state marker without GENIERAILS_STATE_DIR, and a state dir
# that lacks a state the checkout still holds (a split, not an empty env).
if [ "$USE_STATE_LAYOUT" = 1 ] || [ -f "$STATE_ENV_DIR/.state-moved" ]; then
  STATE_BASE="$(python3 "$SCRIPT_DIR/state_layout.py" layer-dir --env-dir "$STATE_ENV_DIR" --env "$ENV_NAME" --layer "$LAYER")" || exit 2
fi
mkdir -p "$STATE_BASE"

if [ ! -d "$ROOT_DIR" ]; then
  echo "Missing Terraform root: $ROOT_DIR" >&2
  exit 1
fi

mkdir -p "$ENV_DIR"

# Use TF_DATA_DIR for per-env .terraform/ isolation. The .terraform.lock.hcl
# file is always in the working directory — use -lockfile=readonly during init
# to prevent concurrent writes from corrupting it. On first run (no lock file),
# skip -lockfile=readonly so Terraform can generate the lock file.
export TF_DATA_DIR="$STATE_BASE/.terraform"
if [ "$USE_STATE_LAYOUT" = 1 ]; then
  ROOT_DIR="$(python3 "$SCRIPT_DIR/state_layout.py" root --env-dir "$STATE_ENV_DIR" --env "$ENV_NAME" --layer "$LAYER" --source "$ROOT_DIR")"
fi
cd "$ROOT_DIR"

INIT_CMD=(
  terraform
  init
  -input=false
  -reconfigure
)
REMOTE_BACKEND_ARGS=()
if [ "$USE_STATE_LAYOUT" = 1 ]; then
  # An invalid remote_state.hcl fails here; it never falls back to local state.
  backend_lines="$(python3 "$SCRIPT_DIR/state_layout.py" backend-args --env-dir "$STATE_ENV_DIR" --env "$ENV_NAME" --layer "$LAYER")" || exit 2
  while IFS= read -r arg; do
    if [ -n "$arg" ]; then REMOTE_BACKEND_ARGS+=("$arg"); fi
  done <<< "$backend_lines"
fi
if [ "${#REMOTE_BACKEND_ARGS[@]}" -gt 0 ]; then
  INIT_CMD+=("${REMOTE_BACKEND_ARGS[@]}")
else
  ci_value="$(printf '%s' "${CI:-}" | tr '[:upper:]' '[:lower:]')"
  is_ci=
  case "$ci_value" in true|1|yes) is_ci=1 ;; esac
  [ -n "${TF_BUILD:-}${JENKINS_URL:-}${GITLAB_CI:-}${BUILDKITE:-}${CIRCLECI:-}" ] && is_ci=1
  # Second line behind the make guard: in CI a write to a local backend would
  # land in the runner's throwaway state.
  case "$COMMAND" in
    apply|apply-plan|destroy|import|state-rm|state-mv)
      if [ "$is_ci" = 1 ] && [ "${GENIERAILS_ALLOW_CI_APPLY:-}" != 1 ]; then
        echo "ERROR: refusing terraform $COMMAND in CI: $STATE_ENV_DIR has no remote_state.hcl, so the backend is local." >&2
        exit 2
      fi
      ;;
  esac
  INIT_CMD+=(-backend-config="path=$STATE_BASE/terraform.tfstate")
fi
WRITABLE_INIT_CMD=("${INIT_CMD[@]}")
if [ -f .terraform.lock.hcl ]; then
  INIT_CMD+=(-lockfile=readonly)
fi

# Serialize terraform init per root directory — concurrent inits in the same
# working directory corrupt provider resolution even with isolated TF_DATA_DIR.
# Use mkdir as a portable lock (atomic on all POSIX systems including macOS).
INIT_LOCK="$ROOT_DIR/.terraform-init.lock.d"
INIT_LOCK_OWNER="$INIT_LOCK/owner"
INIT_LOCK_HOST="$(hostname)"
INIT_LOCK_TIMEOUT_SECONDS="${INIT_LOCK_TIMEOUT_SECONDS:-600}"
INIT_LOCK_ACQUIRED=0
case "$INIT_LOCK_TIMEOUT_SECONDS" in
  ''|*[!0-9]*) echo "INIT_LOCK_TIMEOUT_SECONDS must be a non-negative integer" >&2; exit 2 ;;
esac
_unlock_init() {
  if [ "$INIT_LOCK_ACQUIRED" = "1" ]; then
    rm -f "$INIT_LOCK_OWNER"
    rmdir "$INIT_LOCK" 2>/dev/null || true
    INIT_LOCK_ACQUIRED=0
  elif [ -f "$INIT_LOCK_OWNER" ]; then
    owner_pid="$(sed -n 's/^pid=//p' "$INIT_LOCK_OWNER" 2>/dev/null || true)"
    owner_host="$(sed -n 's/^host=//p' "$INIT_LOCK_OWNER" 2>/dev/null || true)"
    if [ "$owner_pid" = "$$" ] && [ "$owner_host" = "$INIT_LOCK_HOST" ]; then
      rm -f "$INIT_LOCK_OWNER"
      rmdir "$INIT_LOCK" 2>/dev/null || true
    fi
  fi
}
_lock_interrupted() {
  if [ "$INIT_LOCK_ACQUIRED" = "1" ]; then
    echo "Interrupted while holding Terraform init lock $INIT_LOCK" >&2
  else
    echo "Interrupted while waiting for Terraform init lock $INIT_LOCK" >&2
  fi
  exit "$1"
}
trap '_unlock_init' EXIT
trap '_lock_interrupted 130' INT
trap '_lock_interrupted 143' TERM
lock_wait_started="$(date +%s)"
while :; do
  if mkdir "$INIT_LOCK" 2>/dev/null; then
    INIT_LOCK_ACQUIRED=1
    break
  fi
  if [ -f "$INIT_LOCK_OWNER" ]; then
    owner_pid="$(sed -n 's/^pid=//p' "$INIT_LOCK_OWNER" 2>/dev/null || true)"
    owner_host="$(sed -n 's/^host=//p' "$INIT_LOCK_OWNER" 2>/dev/null || true)"
    if [ "$owner_host" = "$INIT_LOCK_HOST" ] && [ -n "$owner_pid" ] \
      && ! kill -0 "$owner_pid" 2>/dev/null && ! ps -p "$owner_pid" >/dev/null 2>&1; then
      stale_owner="$INIT_LOCK/owner.reclaim.$$"
      if mv "$INIT_LOCK_OWNER" "$stale_owner" 2>/dev/null; then
        claimed_pid="$(sed -n 's/^pid=//p' "$stale_owner" 2>/dev/null || true)"
        claimed_host="$(sed -n 's/^host=//p' "$stale_owner" 2>/dev/null || true)"
        if [ "$claimed_pid" = "$owner_pid" ] && [ "$claimed_host" = "$owner_host" ]; then
          echo "+ reclaiming stale Terraform init lock $INIT_LOCK (dead local PID $owner_pid)" >&2
          rm -f "$stale_owner"
          rmdir "$INIT_LOCK" 2>/dev/null || true
        else
          mv "$stale_owner" "$INIT_LOCK_OWNER" 2>/dev/null || true
        fi
      fi
      continue
    fi
  fi
  lock_now="$(date +%s)"
  if [ $((lock_now - lock_wait_started)) -ge "$INIT_LOCK_TIMEOUT_SECONDS" ]; then
    echo "Timed out after ${INIT_LOCK_TIMEOUT_SECONDS}s waiting for Terraform init lock $INIT_LOCK." >&2
    echo "  If its recorded owner is no longer running, clear it with: rm -rf '$INIT_LOCK'" >&2
    exit 1
  fi
  sleep 0.2
done
{
  printf 'pid=%s\n' "$$"
  printf 'host=%s\n' "$INIT_LOCK_HOST"
  printf 'started_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$INIT_LOCK_OWNER"
echo "+ ${INIT_CMD[*]}"
# The lock file is generated locally (gitignored), so an upgrade that adds a
# provider (even one only a tests/ module declares) leaves it stale and the
# read-only init refuses it. Inits are serialized above, so re-run it once
# writable to record the new provider instead of failing every command.
if ! init_output="$("${INIT_CMD[@]}" 2>&1)"; then
  if [[ "$init_output" != *"Provider dependency changes detected"* ]]; then
    printf '%s\n' "$init_output" >&2
    exit 1
  fi
  echo "+ $ROOT_DIR/.terraform.lock.hcl lacks providers this version needs; updating it" >&2
  _lock_providers() {
    awk '/^provider "/ { p = $2; gsub(/"/, "", p) } /^  version / { v = $3; gsub(/"/, "", v); print p " " v }' .terraform.lock.hcl
  }
  locked_before="$(_lock_providers)"
  echo "+ ${WRITABLE_INIT_CMD[*]}"
  "${WRITABLE_INIT_CMD[@]}" >/dev/null
  _lock_providers | while read -r provider version; do
    case "$locked_before" in
      *"$provider $version"*) ;;
      *) echo "+   recorded $provider $version" >&2 ;;
    esac
  done
fi
_unlock_init
trap - EXIT INT TERM

# The scheduled governance job moved from the workspace root to data_access:
# before any command on either root, move it between the env's state files
# (a no-op unless the workspace state still holds it), so neither root ever
# plans to destroy or recreate it.
if [ "$COMMAND" != "print-cmd" ]; then
  case "$LAYER" in
    workspace) JOB_WS_DIR="$ENV_DIR"; JOB_DA_DIR="$ENV_DIR/data_access" ;;
    data_access) JOB_WS_DIR="$(dirname "$ENV_DIR")"; JOB_DA_DIR="$ENV_DIR" ;;
    *) JOB_WS_DIR= ;;
  esac
  if [ -n "$JOB_WS_DIR" ] && [ "$(basename "$JOB_DA_DIR")" = "data_access" ]; then
    python3 "$SCRIPT_DIR/move_scheduled_job.py" --workspace-dir "$JOB_WS_DIR" --data-access-dir "$JOB_DA_DIR"
  fi
fi

VAR_ARGS=()
for tfvars in auth.auto.tfvars env.auto.tfvars abac.auto.tfvars classification.auto.tfvars discovered_uc_tables.auto.tfvars; do
  if [ -f "$ENV_DIR/$tfvars" ]; then
    VAR_ARGS+=(-var-file="$ENV_DIR/$tfvars")
  fi
done

if [ "$LAYER" != "account" ]; then
  VAR_ARGS+=(-var="env_dir=$ENV_DIR")
fi

case "$COMMAND" in
  plan|apply|destroy|import|console)
    CMD=(terraform "$COMMAND" "${VAR_ARGS[@]}" "$@")
    ;;
  show)
    CMD=(terraform show "$@")
    ;;
  apply-plan)
    # A saved plan already carries its variables; Terraform refuses -var-file.
    CMD=(terraform apply -input=false "$@")
    ;;
  state-list)
    CMD=(terraform state list "$@")
    ;;
  state-show)
    CMD=(terraform state show "$@")
    ;;
  state-pull)
    echo "+ terraform state pull"
    terraform state pull >&3
    exit $?
    ;;
  state-rm)
    CMD=(terraform state rm "$@")
    ;;
  state-mv)
    CMD=(terraform state mv "$@")
    ;;
  output)
    CMD=(terraform output "$@")
    ;;
  print-cmd)
    printf 'terraform %s (in %s, TF_DATA_DIR=%s)' "$1" "$ROOT_DIR" "$TF_DATA_DIR"
    shift || true
    for arg in "${VAR_ARGS[@]}" "$@"; do
      printf ' %q' "$arg"
    done
    printf '\n'
    exit 0
    ;;
  *)
    echo "Unsupported terraform command alias: $COMMAND" >&2
    exit 1
    ;;
esac

echo "+ ${CMD[*]}"
if [ "$LAYER" = "data_access" ] && { [ "$COMMAND" = "plan" ] || [ "$COMMAND" = "apply" ]; }; then
  # Terraform's output passes through unchanged; a copy lets the note below
  # explain a plan whose only destroys replace terraform_data.masking_functions.
  OUTPUT_COPY="$(mktemp)"
  STATUS_FILE="$(mktemp)"
  trap 'rm -f "$OUTPUT_COPY" "$STATUS_FILE"' EXIT
  set +e
  # On Ctrl-C (or TERM/HUP) Terraform shuts down gracefully and keeps writing
  # (saving state) through the pipe, so tee ignores those signals. This shell
  # and the group around Terraform trap them instead (a trap, unlike ignoring,
  # isn't inherited by Terraform) so they wait for it and return its status.
  trap ':' TERM HUP
  { trap ':' TERM HUP; "${CMD[@]}"; echo "$?" > "$STATUS_FILE"; } | (trap '' INT TERM HUP; exec tee "$OUTPUT_COPY")
  status="$(cat "$STATUS_FILE")"
  set -e
  python3 "$SCRIPT_DIR/masking_replace_note.py" "$OUTPUT_COPY" || true
  exit "${status:-1}"
fi
"${CMD[@]}"
