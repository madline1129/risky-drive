#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <run-dir> [extra args...]"
  echo "Example: $0 /mnt/data2/whz/risky-drive/carla_smoke/workdir/20260520_110246"
  exit 2
fi

RUN_DIR="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python "${SCRIPT_DIR}/run_curated_l4_three_cases.py" \
  --run-dir "${RUN_DIR}" \
  --carla-root "${CARLA_ROOT:-/mnt/data2/congfeng/CARLA}" \
  "$@"
