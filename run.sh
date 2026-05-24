#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# API key interface:
# 1. Recommended: export AIHUBMIX_API_KEY="your_key" before running.
# 2. Or set API_KEY here / in the shell. Example:
#    API_KEY="your_key" bash run.sh
# 3. Or still pass --api-key "your_key" manually in extra args.
API_KEY="sk-5kj0w3sYcIGsWW4kF82a7dB7A34743058b216c1613546138"
API_KEY_ENV="${API_KEY_ENV:-AIHUBMIX_API_KEY}"
MODEL="${MODEL:-glm-5.1}"
SEMANTIC_FEEDBACK="${SEMANTIC_FEEDBACK:-off}"

usage() {
  cat <<'EOF'
Usage: bash run.sh [run.sh options] [pipeline options]

run.sh options:
  --model MODEL                 One of: deepseek-v4-flash, deepseek-v4-pro, glm-5.1
                                Sets both pipeline LLM model and OpenCode model.
  --semantic-feedback on|off    Enable/disable event_trace semantic validation + OpenCode repair.
                                Default: off
  --semantic-feedback           Same as --semantic-feedback on.
  --no-semantic-feedback        Same as --semantic-feedback off.
  -h, --help                    Show this help.

Environment equivalents:
  MODEL=glm-5.1 SEMANTIC_FEEDBACK=off API_KEY=... bash run.sh
EOF
}

EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --model requires one of: deepseek-v4-flash, deepseek-v4-pro, glm-5.1" >&2
        exit 2
      fi
      MODEL="$2"
      shift 2
      ;;
    --model=*)
      MODEL="${1#*=}"
      shift
      ;;
    --semantic-feedback)
      if [[ $# -ge 2 && "$2" != --* ]]; then
        SEMANTIC_FEEDBACK="$2"
        shift 2
      else
        SEMANTIC_FEEDBACK="on"
        shift
      fi
      ;;
    --semantic-feedback=*)
      SEMANTIC_FEEDBACK="${1#*=}"
      shift
      ;;
    --no-semantic-feedback)
      SEMANTIC_FEEDBACK="off"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

case "$MODEL" in
  deepseek-v4-flash|deepseek-v4-pro|glm-5.1) ;;
  *)
    echo "ERROR: unsupported --model '$MODEL'. Use one of: deepseek-v4-flash, deepseek-v4-pro, glm-5.1" >&2
    exit 2
    ;;
esac

case "$SEMANTIC_FEEDBACK" in
  on|true|1|yes|y)
    SEMANTIC_FEEDBACK="on"
    ;;
  off|false|0|no|n)
    SEMANTIC_FEEDBACK="off"
    ;;
  *)
    echo "ERROR: --semantic-feedback must be on or off" >&2
    exit 2
    ;;
esac

CMD=(
  python "$SCRIPT_DIR/carla_smoke/pipeline/run_full_pipeline.py"
  --api-key-env "$API_KEY_ENV"
  --model "$MODEL"
  --opencode-model "$MODEL"
)

if [[ -n "$API_KEY" ]]; then
  CMD+=(--api-key "$API_KEY")
fi

if [[ "$SEMANTIC_FEEDBACK" == "off" ]]; then
  CMD+=("--extra-arg=--skip-event-trace-validation")
fi

"${CMD[@]}" "${EXTRA_ARGS[@]}"
