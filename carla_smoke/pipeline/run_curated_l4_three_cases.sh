#!/usr/bin/env bash
set -euo pipefail

DEFAULT_RUN_DIR="/mnt/data2/whz/risky-drive/carla_smoke/workdir/20260520_110246"
DEFAULT_ENV_FILE="/mnt/data2/whz/risky-drive/.env"

if [[ $# -ge 1 && "$1" != --* ]]; then
  RUN_DIR="$1"
  shift
else
  RUN_DIR="${DEFAULT_RUN_DIR}"
fi

EXTRA_ARGS=("$@")
if [[ -f "${DEFAULT_ENV_FILE}" ]]; then
  HAS_ENV_FILE=0
  for arg in "${EXTRA_ARGS[@]}"; do
    if [[ "${arg}" == "--env-file" ]]; then
      HAS_ENV_FILE=1
      break
    fi
  done
  if [[ "${HAS_ENV_FILE}" -eq 0 ]]; then
    EXTRA_ARGS+=(--env-file "${DEFAULT_ENV_FILE}")
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python "${SCRIPT_DIR}/run_curated_l4_three_cases.py" \
  --run-dir "${RUN_DIR}" \
  --carla-root "${CARLA_ROOT:-/mnt/data2/congfeng/CARLA}" \
  "${EXTRA_ARGS[@]}"
