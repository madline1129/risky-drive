# Event Contract

Every generated script must write:

`<output-dir>/event_trace.json`

The pipeline uses this file to verify that the script executed the selected chain-specific physical event, not only the shared L0 scene reconstruction.

Required top-level fields:

- `scenario_type`: must equal `scenario_config["carla_plan"]["scenario_type"]`.
- `trigger_frame`: copied from `carla_plan`.
- `event_applied`: short description of the physical event actually applied.
- `frames`: non-empty list of per-frame dictionaries.

Frame dictionaries should include the fields named by `scenario_config["event_contract"]["required_frame_fields"]`.

Examples:

- `front_vehicle_brake`: include front actor speed and ego-front distance before/after trigger.
- `cargo_drop`: include payload count and payload positions after trigger.
- `vulnerable_actor_intrusion`: include actor position and distance to ego.
- `road_obstacle_intrusion`: include obstacle positions.

Do not fake a trace. Record values from spawned actors during simulation.
