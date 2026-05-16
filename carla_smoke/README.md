# CARLA Smoke And Risk Pipeline

This directory keeps the minimal CARLA experiments separate from the main ChatScene code. Existing scene scripts are intentionally left in place so server-side commands do not break.

## Files

- `test.py`: minimal CARLA import/connect smoke test.
- `spawn_scene_capture.py`: spawns a simple CARLA scene and saves front-camera frames.
- `ego_approach_truck.py`: builds the current ego-approaches-truck scene. It can optionally add cargo falling from the truck.
- `qwen_vl_image_analyze.py`: general Qwen/Ollama image analysis utility.
- `step1_qwen_risk_annotation.py`: first decision-tree pipeline step. It asks Qwen-VL to label visible or inferred L1 risk weaknesses from CARLA frames and writes JSONL.
- `output*/`: generated images, logs, and annotations. These are ignored by git.

## Typical Flow

Start CARLA first:

```bash
bash /mnt/data2/congfeng/carla915/CarlaUE4.sh -carla-port=2000
```

Generate the approach-truck scene:

```bash
python carla_smoke/ego_approach_truck.py \
  --port 2000 \
  --output-dir carla_smoke/output_approach_truck \
  --truck-distance 20 \
  --target-speed 6
```

Run decision-tree step 1 risk annotation:

```bash
python carla_smoke/step1_qwen_risk_annotation.py \
  carla_smoke/output_approach_truck \
  --limit 5 \
  --output carla_smoke/output_risk_labels/step1_qwen_risk_annotations.jsonl
```

## Step 1 Output

Each JSONL row contains one frame:

- `image`: absolute image path.
- `model`: Ollama model name, default `qwen3.5:0.8b`.
- `step`: fixed label for this pipeline stage.
- `parsed`: parsed JSON if the model followed the format.
- `raw_response`: original model text for debugging.

The expected L1 labels include cargo instability, brake-light failure, cyclist proximity, wet road, A-pillar blind spot, large-vehicle occlusion, short following distance, and limited avoidance space.
