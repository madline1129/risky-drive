#!/usr/bin/env bash
set -euo pipefail

CARLA_ROOT="${CARLA_ROOT:-/mnt/data2/congfeng/CARLA}"
CARLA_PYTHON="${CARLA_PYTHON:-/mnt/data2/congfeng/miniconda3/envs/resim/bin/python}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-2001}"
TM_PORT="${TM_PORT:-8002}"
TEST_EPOCH="${TEST_EPOCH:-0}"
NUM_SOURCE_SCENES="${NUM_SOURCE_SCENES:-10}"
RUN_ID="${RUN_ID:-}"
DEEPSEEK_URL="${DEEPSEEK_URL:-https://api.deepseek.com/v1/chat/completions}"
API_KEY_ENV="${API_KEY_ENV:-DEEPSEEK_API_KEY}"
ENV_FILE="${ENV_FILE:-}"
API_KEY="${API_KEY:-}"

CMD=(
  python carla_smoke/pipeline/run_safebench_l4_experiment.py
  --carla-root "$CARLA_ROOT"
  --carla-python "$CARLA_PYTHON"
  --host "$HOST"
  --port "$PORT"
  --tm-port "$TM_PORT"
  --num-source-scenes "$NUM_SOURCE_SCENES"
  --model deepseek-v4-pro
  --opencode-model deepseek-v4-pro
  --deepseek-url "$DEEPSEEK_URL"
  --api-key-env "$API_KEY_ENV"
  --agent-cfg adv_scenic.yaml
  --test-policy sac
  --test-epoch "$TEST_EPOCH"
)

if [[ -n "$ENV_FILE" ]]; then
  CMD+=(--env-file "$ENV_FILE")
elif [[ -f ".env" ]]; then
  CMD+=(--env-file ".env")
fi

if [[ -n "$API_KEY" ]]; then
  CMD+=(--api-key "$API_KEY")
fi

if [[ -n "$RUN_ID" ]]; then
  CMD+=(--run-id "$RUN_ID")
fi

exec "${CMD[@]}"
