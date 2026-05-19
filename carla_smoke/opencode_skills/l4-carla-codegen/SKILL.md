---
name: l4-carla-codegen
description: Generate or repair the L4 CARLA risk-scene Python script from scenario_config.json inside an opencode workspace.
---

# L4 CARLA Codegen

Use this skill when asked to create or fix `generated_risk_scene.py` for the ChatScene CARLA L4 risk pipeline.

## Required workflow

1. Read `scenario_config.json` in the current workspace.
2. If `l0_state.json` exists, read it before designing the scene. Treat it as the source of truth for map, weather, ego pose, nearest front actor, and relevant nearby actors.
3. Read `carla_plan.actor_motion_plan` in `scenario_config.json`. Treat it as the source of truth for each actor's behavior after the L0 snapshot.
4. Read `reference_executor.py` before editing. Reuse its CARLA import, synchronous mode, camera, cleanup, and generic spawn patterns only.
5. Read `context/failure_history.md` before designing the scene. Avoid every listed failure mode.
6. Edit only `generated_risk_scene.py` unless the user explicitly asks otherwise.
7. Keep the script self-contained. Do not import project modules.
8. Preserve these CLI arguments: `--carla-root`, `--host`, `--port`, `--town`, `--output-dir`, `--frames`, `--save-every`.
9. The script must default to reading `scenario_config.json` from its own directory.
10. Save risk frames as `risk_rgb_XXXX.png` in `--output-dir`. Follow `physical_task.visualization`; by default each top-level risk image must be a six-view 2x3 ego-camera montage, not a front-only camera.
11. Write `event_trace.json` in `--output-dir` according to `scenario_config.event_contract`.
12. Use CARLA synchronous mode with `fixed_delta_seconds = 0.05`, and restore original world settings in `finally`.
13. Destroy all spawned actors in reverse order in `finally`.
14. Before finishing, make sure the script would pass `python -m py_compile generated_risk_scene.py` and `python generated_risk_scene.py --help`.
15. Replace any seed `NotImplementedError` with the exact scenario behavior requested by `carla_plan.scenario_type`.

## Guardrails

- Import CARLA through a helper that adds PythonAPI egg/whl paths before `import carla`.
- Do not reference a global `carla` variable before importing it.
- Respect `carla_plan.scenario_type` exactly. Do not merge unrelated event types.
- Use `event_contract` as a hard acceptance contract. The script must execute that event and record trace fields proving it.
- Treat `event_contract.primary_actor` as the event owner. Background actors can occlude or provide context, but must not become the main event.
- Satisfy `event_contract.numeric_acceptance` with real actor state, not fabricated trace values.
- L0 is only the initial scene snapshot. Do not continue all L0 actors blindly if doing so contradicts `actor_motion_plan`.
- `actor_motion_plan` decides whether ego should approach, front actor should stop/brake/occlude, and what the primary actor should do.
- Preserve the L0 scene identity where possible: use the L0 map, weather, ego transform, actor types, relative distances, and lane relationships.
- If a L0 transform is occupied, move minimally along the lane or upward in z; do not switch to an unrelated map region.
- For `front_vehicle_brake`, do not spawn payloads, metal pipes, or projectile objects.
- For `vulnerable_actor_intrusion`, the vulnerable actor must cross into the ego lane while the ego is still moving; the front vehicle is only an occluder/context actor.
- For `cargo_drop`, the payload must be the moving risk actor; do not reduce it to a generic front-car braking scene.
- For `road_obstacle_intrusion`, the obstacle must be placed or moved into the ego lane; do not reduce it to a generic front-car braking scene.
- For `cargo_drop`, do not add front-vehicle braking unless explicitly configured as a compound event.
- Avoid random behavior unless it has deterministic fallbacks.
- Use `world.try_spawn_actor` for vehicles and props where possible; handle spawn failures with fallback transforms or clear errors.
- Avoid extra dependencies beyond the Python standard library and CARLA.
- Do not write Markdown in generated Python files.

## Reference files

- `context/config_schema.md`: meaning of the L4 config fields.
- `context/l0_scene_reconstruction.md`: how to rebuild the L4 scene from L0 state.
- `context/event_contract.md`: required per-chain trace output and semantic acceptance checks.
- `context/carla_recipes.md`: stable CARLA implementation patterns.
- `context/known_failures.md`: common generated-script failures and fixes.
- `context/failure_history.md`: concrete failure cases already observed in this project; treat it as a pre-flight checklist.
