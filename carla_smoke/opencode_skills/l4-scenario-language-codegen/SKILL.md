# L4 Scenario-Language Codegen Skill

Use this skill when asked to generate `generated_risk_scene.scenic` for the ChatScene L4 scenario-language backend.

## Hard Rules

- Generate Scenic scenario-language code, not Python.
- Edit only `generated_risk_scene.scenic`.
- Read `scenario_config.json`, `semantic_primitives.json`, and `l0_state.json` before writing the Scenic file.
- Treat `semantic_primitives.json` as the execution plan. It is a constrained primitive graph, not free-form inspiration.
- Use the configured `scenario_type` exactly. Do not replace the event with a different familiar template.
- Preserve the primary actor type/kind, relative position to ego, lane relationship, trigger timing, and action.
- L0 absolute coordinates are best-effort hints. If exact coordinates are awkward in Scenic, use ego-relative positions that preserve the relative geometry.
- Prefer compact Scenic code that follows existing SafeBench/Scenic examples in `context/scenic_examples.md`.
- Keep optimizable values as `param` / `Range(...)` only when useful. Avoid unsafe integer ranges for Python `range(...)`; cast to `int(...)` or use fixed integers for loop counts.
- Do not use `Waypoint.next(-x)` or any CARLA Python API in Scenic code.
- The generated Scenic file must define:
  - `Town`
  - `param map`
  - `param carla_map`
  - `model scenic.simulators.carla.model`
  - an `ego` object
  - the primary risk actor
  - behavior blocks needed by the primitive plan
- The generated scene must be executable by `carla_smoke/scenes/safebench_scenic_scene.py --scenic-file generated_risk_scene.scenic`.

## Primitive Mapping

- `set_scene_context`: choose map/town and weather-compatible setup.
- `spawn_ego`: create `ego = Car ...` using an ego spawn point or an ego-relative road point.
- `spawn_actor_relative`: create a primary/background actor relative to ego or an interaction point.
- `follow_lane`: use `FollowLaneBehavior(...)`.
- `front_vehicle_brake`: use a behavior that follows/lows speed first, then stops/brakes after trigger.
- `vulnerable_actor_intrusion`: use a pedestrian/cyclist behavior that crosses toward/through ego lane while ego is moving.
- `side_vehicle_intrusion`: move a side vehicle laterally toward ego lane if expressible; otherwise encode a close side cut-in using relative placement and lane-following behavior.
- `road_obstacle_intrusion`: place or move an obstacle into ego path.
- `record_expectation`: comments are fine; do not fake runtime traces in Scenic.

## Output Standard

The output must be plain Scenic code. Do not write Markdown.
