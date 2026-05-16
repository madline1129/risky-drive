# CARLA Risk Pipeline

This directory contains a small CARLA-to-risk-tree prototype. CARLA exports the factual L0 scene state through its Python API, local Qwen/Ollama provides visual observations, and DeepSeek performs L1/L2 text reasoning.

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
- `pipeline/deepseek_client.py`: shared DeepSeek chat-completions client.

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

`vision/observations.json` contains Qwen's visual observations. It is auxiliary evidence for DeepSeek; CARLA API state remains the source of truth for distances, speeds, and actor identities.
