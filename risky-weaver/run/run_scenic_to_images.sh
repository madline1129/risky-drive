#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-/mnt/data2/congfeng/miniconda3/envs/resim/bin/python}"
CARLA_ROOT="${CARLA_ROOT:-/mnt/data2/congfeng/CARLA}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-2001}"
SCENIC_FILE="${SCENIC_FILE:-$REPO_ROOT/risky-weaver/opencode/workdir/generated_scene.scenic}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO_ROOT/risky-weaver/run/images}"
MONTAGE="${MONTAGE:-1}"

EXTRA_ARGS=()
if [[ "$MONTAGE" == "1" || "$MONTAGE" == "true" || "$MONTAGE" == "yes" ]]; then
  EXTRA_ARGS+=(--montage)
fi

"$PYTHON_BIN" "$REPO_ROOT/risky-weaver/run/scenic_to_carla_images.py" \
  --carla-root "$CARLA_ROOT" \
  --host "$HOST" \
  --port "$PORT" \
  --scenic-file "$SCENIC_FILE" \
  --output-dir "$OUTPUT_DIR" \
  --frames "${FRAMES:-180}" \
  --save-every "${SAVE_EVERY:-5}" \
  --warmup-ticks "${WARMUP_TICKS:-5}" \
  --timestep "${TIMESTEP:-0.05}" \
  --weather "${WEATHER:-ClearNoon}" \
  --camera-mode "${CAMERA_MODE:-surround}" \
  --clean-output \
  "${EXTRA_ARGS[@]}"
