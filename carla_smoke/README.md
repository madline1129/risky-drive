# CARLA Smoke And Risk Pipeline

This directory keeps the minimal CARLA experiments separate from the main ChatScene code. Existing scene scripts are intentionally left in place so server-side commands do not break.

## Layout

- `tests/`: minimal CARLA import/connect tests.
- `scenes/`: CARLA scene generation scripts.
- `pipeline/`: agent/Qwen risk-labeling pipeline scripts.
- `outputs/`: generated images, logs, and annotations. This directory is ignored by git.

Key scripts:

- `tests/test.py`: verifies Python can import CARLA and connect to the simulator.
- `scenes/spawn_scene_capture.py`: spawns a simple scene and saves front-camera frames.
- `scenes/normal_driving_scene.py`: generates a normal driving sequence with Traffic Manager and saves ego-camera frames.
- `scenes/ego_approach_truck.py`: builds the ego-approaches-truck scene, with optional cargo falling from the truck.
- `pipeline/qwen_vl_image_analyze.py`: general Qwen/Ollama image analysis utility.
- `pipeline/step1_qwen_risk_annotation.py`: first decision-tree pipeline step. It labels visible or inferred L1 risk weaknesses from CARLA frames and writes JSONL.
- `pipeline/run_normal_scene_to_qwen.py`: runs the minimal end-to-end pipeline: normal CARLA scene -> saved images -> Qwen L1 risk labels.

## Typical Flow

Start CARLA first:

```bash
bash /mnt/data2/congfeng/carla915/CarlaUE4.sh -carla-port=2000
```

Run the minimal normal-driving pipeline:

```bash
python carla_smoke/pipeline/run_normal_scene_to_qwen.py \
  --port 2000 \
  --frames 160 \
  --save-every 5 \
  --vehicles 30 \
  --qwen-limit 8 \
  --clean-output
```

This writes images to `carla_smoke/outputs/normal_driving/` and Qwen annotations to `carla_smoke/outputs/risk_labels/normal_driving_step1_qwen.jsonl`.

Generate only the normal-driving images:

```bash
python carla_smoke/scenes/normal_driving_scene.py \
  --port 2000 \
  --output-dir carla_smoke/outputs/normal_driving \
  --vehicles 30 \
  --clean-output
```

Generate the approach-truck risk scene:

```bash
python carla_smoke/scenes/ego_approach_truck.py \
  --port 2000 \
  --output-dir carla_smoke/outputs/approach_truck \
  --truck-distance 20 \
  --target-speed 6
```

Run decision-tree step 1 risk annotation:

```bash
python carla_smoke/pipeline/step1_qwen_risk_annotation.py \
  carla_smoke/outputs/approach_truck \
  --limit 5 \
  --output carla_smoke/outputs/risk_labels/step1_qwen_risk_annotations.jsonl
```

## Step 1 Output

Each JSONL row contains one frame:

- `image`: absolute image path.
- `model`: Ollama model name, default `qwen3.5:0.8b`.
- `step`: fixed label for this pipeline stage.
- `parsed`: parsed JSON if the model followed the format.
- `raw_response`: original model text for debugging.

The expected L1 labels include cargo instability, brake-light failure, cyclist proximity, wet road, A-pillar blind spot, large-vehicle occlusion, short following distance, and limited avoidance space.
