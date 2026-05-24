# L4 Scenario-Language Codegen Skill

Generate `generated_risk_scene.scenic` for the ChatScene L4 backend.

## Inputs

- `l4_task.json`: preferred single self-contained OpenCode task. It contains scene context, actors, selected risk type, selected action primitive, concrete action parameters, acceptance criteria, and output contract.
- `scenario_config.json`: minimal event contract, scenario type, primary actor, action, trigger frame, success criteria.
- `semantic_primitives.json`: the hard primitive graph to implement.
- `l0_state.json`: one-frame L0 scene facts for ego, weather, actors, and source map.

## Hard Rules

- Generate Scenic code only. Do not generate CARLA Python.
- Edit only `generated_risk_scene.scenic`.
- Prefer `l4_task.json` over the debug mirror files. Use `l4_task.scene_context.map_absolute_path` exactly in `param map = localPath(...)`.
- Use Scenic 2D coordinates: `(x @ y)`. Never emit `Point(x, y, z)`, `carla.Location`, or `carla.Transform`.
- CARLA to Scenic conversion is already provided: Scenic x = CARLA x, Scenic y = -CARLA y, Scenic heading = `-(CARLA yaw + 90)`.
- Preserve `l4_task.risk.scenario_type`, primary actor kind/type, ego-relative side, trigger frame, action primitive, velocity vector, and direction policy.
- If exact absolute placement fails, adjust only within `actor.relative_to_ego.same_side_search_policy`; never flip left/right.
- L0 absolute pose is a hint. Ego-relative geometry and the requested risk action are authoritative.
- The scene must be executable by `carla_smoke/scenes/safebench_scenic_scene.py`.

## Primitive Mapping

- `set_scene_context`: write Scenic header with Town, absolute map path, carla_map, and CARLA model.
- `spawn_ego`: create the ego vehicle from the converted Scenic pose or a nearby valid road point.
- `spawn_actor_relative`: create the primary/background actor while preserving relative longitudinal/lateral relation to ego.
- `front_vehicle_brake`: front actor moves/follows first, then brakes/stops after trigger.
- `vulnerable_actor_intrusion`: vulnerable actor moves toward or into ego lane after trigger.
- `side_vehicle_intrusion`: side vehicle moves/cuts toward ego lane after trigger.
- `cargo_drop` / `road_obstacle_intrusion`: visible object enters or blocks ego path after trigger.
- `ego_action_risk`: ego keeps moving toward the primary hazard actor after trigger, without changing the hazard actor identity or side.

## Trace Target

The executor reconstructs `event_trace.json` from saved states, then semantic validation checks target/actual values. If repair feedback is given, fix the Scenic behavior so the reported failed checks pass; do not change JSON inputs or switch scenario type.
