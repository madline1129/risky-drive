#!/usr/bin/env bash
set -euo pipefail

CARLA_SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_PATH="${1:-$CARLA_SMOKE_DIR/outputs/approach_truck/rgb_0065.png}"
LIMIT="${LIMIT:-3}"

python "$CARLA_SMOKE_DIR/pipeline/qwen_vl_image_analyze.py" \
  "$IMAGE_PATH" \
  --limit "$LIMIT"
