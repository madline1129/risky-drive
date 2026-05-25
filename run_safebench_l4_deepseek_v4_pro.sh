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
  --agent-cfg adv_scenic.yaml
  --test-policy sac
  --test-epoch "$TEST_EPOCH"
)

if [[ -n "$RUN_ID" ]]; then
  CMD+=(--run-id "$RUN_ID")
fi

exec "${CMD[@]}"
