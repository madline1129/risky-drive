---
name: l4-safebench-intervention-codegen
description: Modify a SafeBench replay intervention script so L4 risk events perturb the live actors in the original SafeBench/Scenic scene instead of rebuilding a new CARLA world.
---

# L4 SafeBench Intervention Codegen

Use this skill when asked to edit `generated_safebench_intervention.py` for the ChatScene L4 SafeBench replay backend.

## Required workflow

1. Read `scenario_config.json` first.
2. Read `l0_state.json` if present.
3. Read `generated_safebench_intervention.py` before editing. It is the execution template and already handles CARLA import, Scenic/SafeBench replay, cameras, six-view montage saving, trace writing, and cleanup.
4. Edit only `generated_safebench_intervention.py`.
5. Preserve the CLI:
   `--scenario-config`, `--l0-json`, `--carla-root`, `--host`, `--port`, `--timeout`, `--scenic-file`, `--scene-sample-attempts`, `--output-dir`, `--frames`, `--save-every`, `--pre-roll-frames`, `--trigger-frame`, `--warmup-ticks`, `--seed`, `--timestep`, `--ego-speed-difference`, `--weather`.
6. Keep the SafeBench replay flow intact:
   ScenicSimulator -> generateScene -> setSimulation -> get ego actor -> pre-roll -> attach ego cameras -> frame loop.
7. Implement or repair only the intervention logic: actor matching, generated actor setup, per-frame perturbation, and trace fields.
8. Save top-level `risk_rgb_XXXX.png` as six-view 2x3 montages, and write `event_trace.json` with top-level `frames`.
9. Before finishing, make sure the script would pass `python -m py_compile generated_safebench_intervention.py` and `python generated_safebench_intervention.py --help`.

## Guardrails

- Do not create a fresh CARLA world for L4.
- Do not call `client.load_world` or implement `spawn_ego_near_l0`.
- Do not spawn a replacement ego vehicle.
- Do not reconstruct all L0 actors from scratch.
- The ego must be the live ego actor exposed by SafeBench/Scenic.
- For existing primary objects, match a live actor in the replayed SafeBench scene by type, relative longitudinal/lateral distance to ego, and role from `risk_object_spec`.
- For generated risk objects such as pedestrians, obstacles, or payloads, spawn only the primary generated object needed for the risk event; do not replace the SafeBench base scene.
- Respect `scenario_config.risk_object_spec.primary_object` as the main perturbed object.
- Respect `scenario_config.physical_task` over free-form natural-language text.
- For `front_vehicle_brake`, brake/decelerate the matched live front vehicle. Do not spawn payloads or pedestrians.
- For `side_vehicle_intrusion`, move the matched live side vehicle laterally toward the ego lane.
- For `vulnerable_actor_intrusion`, spawn or use a vulnerable actor following `risk_object_spec.geometry.path_world` or start/end world points.
- For `road_obstacle_intrusion`, place or move the obstacle into the ego lane according to `risk_object_spec.geometry`.
- For `cargo_drop`, make the payload the primary event; do not reduce it to front-vehicle braking.
- Trace values must come from live CARLA actor state after the perturbation. Do not fabricate trace-only success.
- Do not write Markdown in generated Python files.
