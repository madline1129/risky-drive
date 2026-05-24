#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# Required configuration:
#   SERVER_SSH_TARGET: SSH target for the remote machine running CARLA, e.g. user@server_ip
#   CARLA_ROOT: local CARLA install root used by the Python API
SERVER_SSH_TARGET="${SERVER_SSH_TARGET:-user@server_ip}"
CARLA_ROOT="${CARLA_ROOT:-/path/to/your/local/CARLA}"

# Tunnel settings:
#   This forwards local port 2000 to the CARLA server port 2000 on the remote host.
LOCAL_CARLA_PORT="${LOCAL_CARLA_PORT:-2000}"
REMOTE_CARLA_HOST="${REMOTE_CARLA_HOST:-127.0.0.1}"
REMOTE_CARLA_PORT="${REMOTE_CARLA_PORT:-2000}"

# Pipeline settings:
RUN_ID="${RUN_ID:-local_$(date +%Y%m%d_%H%M%S)}"
WORKDIR_ROOT="${WORKDIR_ROOT:-$REPO_ROOT/carla_smoke/workdir}"
SCENIC_FILE="${SCENIC_FILE:-}"
SCENARIO_INDEX="${SCENARIO_INDEX:-0}"
SCENE_SAMPLE_ATTEMPTS="${SCENE_SAMPLE_ATTEMPTS:-20}"
SEED="${SEED:-7}"
WEATHER="${WEATHER:-ClearNoon}"
MODEL="${MODEL:-deepseek-v4-flash}"
OPENCODE_MODEL="${OPENCODE_MODEL:-deepseek-v4-flash}"
API_KEY_ENV="${API_KEY_ENV:-DEEPSEEK_API_KEY}"
OPENCODE_BIN="${OPENCODE_BIN:-opencode}"
QUICK="${QUICK:-1}"
HOST="${HOST:-127.0.0.1}"
TIMEOUT="${TIMEOUT:-300}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

if [[ -z "${DEEPSEEK_API_KEY:-}" && "${API_KEY_ENV}" == "DEEPSEEK_API_KEY" ]]; then
  echo "DEEPSEEK_API_KEY is not set." >&2
  exit 1
fi

if [[ "$SERVER_SSH_TARGET" == "user@server_ip" ]]; then
  echo "Set SERVER_SSH_TARGET to your SSH target, e.g. user@1.2.3.4" >&2
  exit 1
fi

if [[ "$CARLA_ROOT" == "/path/to/your/local/CARLA" ]]; then
  echo "Set CARLA_ROOT to your local CARLA installation root." >&2
  exit 1
fi

cleanup() {
  if [[ -n "${SSH_TUNNEL_PID:-}" ]] && kill -0 "$SSH_TUNNEL_PID" 2>/dev/null; then
    kill "$SSH_TUNNEL_PID" 2>/dev/null || true
    wait "$SSH_TUNNEL_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting SSH tunnel: localhost:${LOCAL_CARLA_PORT} -> ${REMOTE_CARLA_HOST}:${REMOTE_CARLA_PORT} on ${SERVER_SSH_TARGET}"
ssh -N \
  -L "${LOCAL_CARLA_PORT}:${REMOTE_CARLA_HOST}:${REMOTE_CARLA_PORT}" \
  -o ConnectTimeout=10 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=2 \
  "${SERVER_SSH_TARGET}" &
SSH_TUNNEL_PID=$!

sleep 2
if ! kill -0 "$SSH_TUNNEL_PID" 2>/dev/null; then
  echo "SSH tunnel failed to start." >&2
  exit 1
fi

PIPELINE_CMD=(
  bash "$REPO_ROOT/run.sh"
  --carla-root "$CARLA_ROOT"
  --host "$HOST"
  --port "$LOCAL_CARLA_PORT"
  --run-id "$RUN_ID"
  --weather "$WEATHER"
  --seed "$SEED"
  --scenario-index "$SCENARIO_INDEX"
  --scene-sample-attempts "$SCENE_SAMPLE_ATTEMPTS"
  --model "$MODEL"
  --opencode-model "$OPENCODE_MODEL"
  --api-key-env "$API_KEY_ENV"
  --workdir-root "$WORKDIR_ROOT"
)

if [[ -n "$SCENIC_FILE" ]]; then
  PIPELINE_CMD+=(--scenic-file "$SCENIC_FILE")
fi

if [[ "$QUICK" != "0" && "$QUICK" != "false" && "$QUICK" != "no" ]]; then
  PIPELINE_CMD+=(--quick)
fi

if [[ -n "$EXTRA_ARGS" ]]; then
  # shellcheck disable=SC2206
  EXTRA_ARGS_ARRAY=($EXTRA_ARGS)
  PIPELINE_CMD+=("${EXTRA_ARGS_ARRAY[@]}")
fi

echo "Running pipeline:"
printf '  %q' "${PIPELINE_CMD[@]}"
printf '\n'

"${PIPELINE_CMD[@]}"
