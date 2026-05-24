#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# API key interface:
# 1. Recommended: export AIHUBMIX_API_KEY="your_key" before running.
# 2. Or set API_KEY here / in the shell. Example:
#    API_KEY="your_key" bash run.sh
# 3. Or still pass --api-key "your_key" manually in extra args.
API_KEY="${API_KEY:-}"
API_KEY_ENV="${API_KEY_ENV:-AIHUBMIX_API_KEY}"

CMD=(
  python "$SCRIPT_DIR/carla_smoke/pipeline/run_full_pipeline.py"
  --api-key-env "$API_KEY_ENV"
)

if [[ -n "$API_KEY" ]]; then
  CMD+=(--api-key "$API_KEY")
fi

"${CMD[@]}" "$@"
