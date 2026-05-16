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
- `pipeline/l0_l1_qwen_subagent.py`: Qwen subagent that writes one L0 scene snapshot file and one L1 risk prediction file.
- `pipeline/l2_qwen_subagent.py`: Qwen text subagent that reads L1 JSON and writes L2 trigger-event hypotheses.
- `pipeline/run_normal_scene_to_qwen.py`: runs the minimal end-to-end pipeline: normal CARLA scene -> saved images -> Qwen L0/L1/L2 subagents.

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
  --lead-distance 14 \
  --lead-speed-difference 35 \
  --qwen-select middle \
  --qwen-timeout 300 \
  --clean-output
```

This writes images to `carla_smoke/outputs/normal_driving/`, L0/L1 outputs to `carla_smoke/outputs/agent_pipeline/l0_l1/`, and L2 outputs to `carla_smoke/outputs/agent_pipeline/l2/`. The explicit lead vehicle keeps another car visible near the ego vehicle instead of relying only on random traffic.

The L0/L1 subagent writes two main files:

- `L0_state_snapshot.json`: the current scene root node, including ego state, road state, visible objects, and a compact scene sentence.
- `L1_risk_predictions.json`: exactly five likely physical risk weaknesses, ranked from 1 to 5.

The L2 subagent reads `L1_risk_predictions.json` and writes:

- `L2_trigger_event_hypotheses.json`: exactly ten trigger-event hypotheses, roughly two per L1 weakness.

Generate only the normal-driving images:

```bash
python carla_smoke/scenes/normal_driving_scene.py \
  --port 2000 \
  --output-dir carla_smoke/outputs/normal_driving \
  --vehicles 30 \
  --lead-distance 14 \
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
python carla_smoke/pipeline/l0_l1_qwen_subagent.py \
  carla_smoke/outputs/approach_truck \
  --select middle \
  --output-dir carla_smoke/outputs/agent_pipeline/l0_l1
```

Run only L2 from an existing L1 JSON:

```bash
python carla_smoke/pipeline/l2_qwen_subagent.py \
  carla_smoke/outputs/agent_pipeline/l0_l1/L1_risk_predictions.json \
  --l0-json carla_smoke/outputs/agent_pipeline/l0_l1/L0_state_snapshot.json \
  --output-dir carla_smoke/outputs/agent_pipeline/l2
```

## L0/L1 Output

The L0 file contains the scene root node:

- `level`: fixed as `L0`.
- `name`: `场景根节点`.
- `description`: `当前时刻的场景结构化快照`.
- `ego`, `road`, `objects`: structured state fields.
- `scene_text`: one compressed sentence, e.g. ego speed, front vehicle distance, road state, and visible risk objects.

The L1 file contains five ranked physical risk predictions. Expected labels include cargo instability, brake-light failure, cyclist proximity, wet road, A-pillar blind spot, large-vehicle occlusion, short following distance, and limited avoidance space.

## L2 Output

The L2 file contains ten ranked trigger events:

- `parent_l1_rank` and `parent_l1_name`: which L1 weakness this event activates.
- `trigger_name`: a concrete event, such as rope breakage, front-car sudden braking, cyclist slipping, or a vehicle emerging from occlusion.
- `counterfactual_intervention`: how to force the event in simulation.
- `direct_physical_outcome`: the immediate physical consequence before later accident-chain expansion.
