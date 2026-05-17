---
name: l4-carla-codegen
description: Generate or repair the L4 CARLA risk-scene Python script from scenario_config.json inside an opencode workspace.
---

# L4 CARLA Codegen

Use this skill when asked to create or fix `generated_risk_scene.py` for the ChatScene CARLA L4 risk pipeline.

## Required workflow

1. Read `scenario_config.json` in the current workspace.
2. Read `reference_executor.py` before editing. Reuse its CARLA import, synchronous mode, camera, cleanup, and generic spawn patterns only.
3. Edit only `generated_risk_scene.py` unless the user explicitly asks otherwise.
4. Keep the script self-contained. Do not import project modules.
5. Preserve these CLI arguments: `--carla-root`, `--host`, `--port`, `--town`, `--output-dir`, `--frames`, `--save-every`.
6. The script must default to reading `scenario_config.json` from its own directory.
7. Save front camera frames as `risk_rgb_XXXX.png` in `--output-dir`.
8. Use CARLA synchronous mode with `fixed_delta_seconds = 0.05`, and restore original world settings in `finally`.
9. Destroy all spawned actors in reverse order in `finally`.
10. Before finishing, make sure the script would pass `python -m py_compile generated_risk_scene.py` and `python generated_risk_scene.py --help`.
11. Replace any seed `NotImplementedError` with the exact scenario behavior requested by `carla_plan.scenario_type`.

## Guardrails

- Import CARLA through a helper that adds PythonAPI egg/whl paths before `import carla`.
- Do not reference a global `carla` variable before importing it.
- Respect `carla_plan.scenario_type` exactly. Do not merge unrelated event types.
- For `front_vehicle_brake`, do not spawn payloads, metal pipes, or projectile objects.
- For `cargo_drop`, do not add front-vehicle braking unless explicitly configured as a compound event.
- Avoid random behavior unless it has deterministic fallbacks.
- Use `world.try_spawn_actor` for vehicles and props where possible; handle spawn failures with fallback transforms or clear errors.
- Avoid extra dependencies beyond the Python standard library and CARLA.
- Do not write Markdown in generated Python files.

## Reference files

- `context/config_schema.md`: meaning of the L4 config fields.
- `context/carla_recipes.md`: stable CARLA implementation patterns.
- `context/known_failures.md`: common generated-script failures and fixes.
