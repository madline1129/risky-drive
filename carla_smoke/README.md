# CARLA Risk Pipeline

This directory contains a small CARLA-to-risk-tree prototype. CARLA exports factual L0 scene state, local Qwen/Ollama provides visual observations, DeepSeek performs L1-L3 text reasoning, and L4 executes a CARLA risk scene from the generated plan.

## Layout

- `tests/`: minimal CARLA import/connect tests.
- `scenes/`: CARLA scene generation scripts.
- `pipeline/`: Qwen vision and DeepSeek reasoning pipeline.
- `outputs/`: older/generated scene outputs, ignored by git.
- `workdir/`: timestamped pipeline runs, ignored by git.

Key pipeline files:

- `pipeline/run.py`: main entry point.
- `pipeline/vision.py`: calls local Qwen/Ollama on the selected CARLA image and writes visual observations.
- `pipeline/l0.py`: reads CARLA API L0 state plus optional Qwen vision observations, then asks DeepSeek for five L1 risk weaknesses.
- `pipeline/l2.py`: DeepSeek L2 agent. It reads L1 risks and writes ten trigger-event hypotheses.
- `pipeline/l3.py`: DeepSeek L3 agent. It expands L2 triggers into initial accident chains and CARLA execution plans.
- `pipeline/l4.py`: code-agent stage. It turns an L3 plan into a CARLA scenario config and can execute it through either the built-in template executor or real `opencode run`.
- `pipeline/deepseek_client.py`: shared DeepSeek chat-completions client.
- `scenes/risk_event_scene.py`: generic CARLA executor used by L4.

## Requirements

Start CARLA first:

```bash
bash /mnt/data2/congfeng/carla915/CarlaUE4.sh -carla-port=2000
```

Set your DeepSeek API key:

```bash
export DEEPSEEK_API_KEY="your_api_key"
```

Note: DeepSeek text chat does not inspect image pixels directly. Qwen handles image observation first; DeepSeek receives the Qwen observation JSON plus CARLA API state.

## Run Full Pipeline

```bash
python carla_smoke/pipeline/run.py \
  --port 2000 \
  --frames 160 \
  --save-every 5 \
  --vehicles 30 \
  --lead-distance 14 \
  --lead-speed-difference 35 \
  --select middle \
  --qwen-model qwen3.5:0.8b \
  --timeout 300 \
  --clean-images
```

Output is grouped by timestamp:

```text
carla_smoke/workdir/YYYYMMDD_HHMMSS/
  manifest.json
  images/
    rgb_0000.png
    state_0000.json
    scene_states.jsonl
    ego_log.csv
  vision/
    observations.json
    qwen_raw.json
  l0/
    state.json
    risks.json
    deepseek_raw.json
  l2/
    triggers.json
    deepseek_raw.json
  l3/
    chains.json
    deepseek_raw.json
  l4/
    scenario_config.json
    risk_images/
      risk_rgb_0000.png
```

## Run Individual Agents

Run L0/L1 from an existing image directory:

```bash
python carla_smoke/pipeline/l0.py \
  carla_smoke/workdir/YYYYMMDD_HHMMSS/images \
  --select middle \
  --vision-json carla_smoke/workdir/YYYYMMDD_HHMMSS/vision/observations.json \
  --output-dir carla_smoke/workdir/YYYYMMDD_HHMMSS/l0
```

Run L2 from existing L1 risks:

```bash
python carla_smoke/pipeline/l2.py \
  carla_smoke/workdir/YYYYMMDD_HHMMSS/l0/risks.json \
  --l0-json carla_smoke/workdir/YYYYMMDD_HHMMSS/l0/state.json \
  --output-dir carla_smoke/workdir/YYYYMMDD_HHMMSS/l2
```

## Risk Tree Files

`l0/state.json` contains the selected CARLA API snapshot:

- `level: L0`
- scene root node and structured current-state snapshot
- ego speed/location/lane, road/weather, nearby actors, relative distances, and nearest front actor

`l0/risks.json` contains exactly five L1 physical risk weaknesses.

`l2/triggers.json` contains exactly ten L2 trigger-event hypotheses, roughly two per L1 weakness.

`l3/chains.json` contains initial accident chains and executable `carla_plan` objects.

`l4/scenario_config.json` is the code-agent output used by `scenes/risk_event_scene.py`.

`l4/risk_images/` contains the rendered CARLA risk scenario frames. Use `--skip-l4` if you only want plans and do not want to run the second CARLA execution.

By default L4 runs one selected L3 chain (`--l4-chain-index`, default `0`). To run every L3 chain, pass:

```bash
python carla_smoke/pipeline/run.py \
  --code-agent opencode \
  --l4-all-chains
```

In all-chains mode, each chain writes to a separate subdirectory under `l4/`, and `l4/l4_manifest.json` lists the generated outputs.

To use real opencode for L4 code generation, install/configure opencode with a DeepSeek model and run:

```bash
python carla_smoke/pipeline/run.py \
  --port 2000 \
  --env-file .env \
  --code-agent opencode \
  --opencode-model deepseek-v4-pro
```

In this mode, `pipeline/l4.py` creates `opencode_workspace/`, copies reusable skills from `carla_smoke/opencode_skills/` into `.opencode/skills/`, seeds `generated_risk_scene.py`, copies L0 state when available, and calls `opencode run` to edit that script in place. The pipeline then validates the script with `py_compile` and `--help`, allows up to three opencode repair attempts for local validation or execution failures, and executes the generated script to produce `risk_images/`. By default, L4 only requires `risk_rgb_*.png` images after execution; pass `--validate-event-trace` to also check `event_trace.json` structure and numeric event semantics.

The opencode workspace contains:

- `.opencode/skills/l4-carla-codegen/`
- `AGENTS.md`
- `scenario_config.json`
- `l0_state.json` when `--l0-json` is provided
- `reference_executor.py`
- `generated_risk_scene.py`
- `context/`
- `opencode_prompt.txt`

`vision/observations.json` contains Qwen's visual observations. It is auxiliary evidence for DeepSeek; CARLA API state remains the source of truth for distances, speeds, and actor identities.
