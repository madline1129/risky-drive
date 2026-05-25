# L4 Scenario-Language Codegen Skill

Generate `generated_risk_scene.scenic` for the ChatScene L4 backend.

## Inputs

- `l4_task.json`: the single self-contained OpenCode task. It contains scene context, actors, selected risk type, the concrete action primitive, acceptance criteria, and output contract.
- `semantic_primitives.json`: the primitive graph trace to implement.
- `l0_state.json`: one-frame L0 scene facts for ego, weather, actors, and source map.

## Hard Rules

- Generate Scenic code only. Do not generate CARLA Python.
- Edit only `generated_risk_scene.scenic`.
- Use `l4_task.scene_context.map_absolute_path` exactly in `param map = localPath(...)`.
- Use Scenic 2D coordinates: `(x @ y)`. Never emit `Point(x, y, z)`, `carla.Location`, or `carla.Transform`.
- CARLA to Scenic conversion is already provided: Scenic x = CARLA x, Scenic y = -CARLA y, Scenic heading = `-(CARLA yaw + 90)`.
- Never use tolerance shorthand like `12.352 +/- 1.0`; Scenic does not support that syntax. Use `Range(11.352, 13.352)` instead.
- In `following roadDirection from ego for ...`, the distance must be a numeric literal or `Range(lower, upper)`, for example `following roadDirection from ego for Range(11.352, 13.352)`.
- Preserve `l4_task.risk.scenario_type`, primary actor kind/type, ego-relative side, trigger frame, and every numeric field in `l4_task.actions.action_primitive`.
- Implement primary risk actions aggressively. Do not weaken high lateral/crossing speeds, hard braking or conditional reverse motion, target-lane intrusion depth, or no-braking ego behavior into gentle lane following.
- Define every `behavior`, `monitor`, helper function, and constant before the first object declaration or `with behavior ...` reference that uses it. Scenic does not allow forward references to behavior names.
- Never write `require <object> do <Behavior>()`; Scenic `require` is only for boolean constraints. Bind actor behavior in the object declaration with `with behavior Behavior(...)`. Do not attach a custom behavior to `ego` unless the scenario type is `ego_action_risk`; the SafeBench runtime normally controls ego through CARLA Traffic Manager.
- If exact absolute placement fails, adjust only within `actor.relative_to_ego.same_side_search_policy`; never flip left/right.
- L0 absolute pose is a hint. Ego-relative geometry and the requested risk action are authoritative.
- The scene must be executable by `carla_smoke/scenes/safebench_scenic_scene.py`.

## Primitive Mapping

- `set_scene_context`: write Scenic header with Town, absolute map path, carla_map, and CARLA model.
- `spawn_ego`: create the ego vehicle from the converted Scenic pose or a nearby valid road point.
- `spawn_actor_relative`: create the primary/background actor while preserving relative longitudinal/lateral relation to ego.
- `front_vehicle_brake`: default behavior is hard braking after `trigger_frame`/`trigger_seconds` until `target_speed_mps`. If and only if `action_primitive.front_action_variant == "reverse_toward_ego"` or `reverse_speed_mps` is present, implement sudden reverse toward ego at `reverse_speed_mps`.
- `vulnerable_actor_intrusion`: vulnerable actor moves toward or into ego lane after trigger.
- `side_vehicle_intrusion`: side vehicle aggressively moves/cuts toward the target ego-lane lateral position after trigger.
- `cargo_drop` / `road_obstacle_intrusion`: visible object enters or blocks ego path after trigger.
- `ego_action_risk`: ego keeps moving toward the primary hazard actor after trigger, without changing the hazard actor identity or side.
- `weather_visibility_change`: no physical primary actor; use the selected `action_primitive.weather` profile from the four supported options and apply it with `simulation().world.get_weather()` / `set_weather(...)` in a behavior or monitor whose definition appears before it is referenced.

## Trace Target

The executor reconstructs `event_trace.json` from saved states, then semantic validation checks target/actual values. If repair feedback is given, fix the Scenic behavior so the reported failed checks pass; do not change JSON inputs or switch scenario type.
