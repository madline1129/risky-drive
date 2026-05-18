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
Do not put per-frame dictionaries under a separate `frame_data` key.

Acceptance is semantic, not only structural. The pipeline checks scenario-specific numeric behavior:

- `front_vehicle_brake`: front actor speed must drop and ego-front distance must change.
- `cargo_drop`: payload must exist, move after trigger, and approach the ego path.
- `vulnerable_actor_intrusion`: ego must still be moving near trigger; the vulnerable actor must move, approach ego, enter the ego lane laterally, and cross the lane centerline.
- `road_obstacle_intrusion`: obstacle must be in or move into the ego lane near the ego path.
- `side_vehicle_intrusion`: the same L0 side vehicle must move laterally toward the ego lane, enter the configured lateral band, and remain close enough to the ego vehicle.

The primary actor listed in `event_contract.primary_actor` must be responsible for the risk event. Do not let a background front vehicle braking/collision become the event for every scenario.

Use `carla_plan.actor_motion_plan` to decide actor behavior. L0 is only the initial geometry snapshot; it does not define the future motion by itself.

Examples:

- `front_vehicle_brake`: include front actor speed and ego-front distance before/after trigger.
- `cargo_drop`: include payload count and payload positions after trigger.
- `vulnerable_actor_intrusion`: include actor position and distance to ego.
- `road_obstacle_intrusion`: include obstacle positions.
- `side_vehicle_intrusion`: include `primary_actor_id`, `primary_actor_type_id`, `primary_actor_position`, `distance_to_ego_m`, and `relative_lateral_m`.

Do not fake a trace. Record values from spawned actors during simulation.
