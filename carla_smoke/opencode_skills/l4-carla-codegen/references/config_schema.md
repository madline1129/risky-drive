# L4 Scenario Config Schema

The generated script reads `scenario_config.json` from the same directory as the script unless `--config` is provided.

Important fields:

- `truck_distance`: meters ahead of the ego vehicle where the front truck should spawn.
- `trigger_frame`: fallback frame index when the risk event starts.
- `carla_plan`: nested execution plan from L3.
- `carla_plan.scenario_type`: scenario category. Treat `cargo_drop` and unknown values as a front-truck payload/projectile drop.
- `carla_plan.object_type`: requested obstacle or payload type, for example `metal_pipe`.
- `carla_plan.object_count`: optional number of payload actors. Default to a small visible group.
- `carla_plan.trigger_frame`: preferred risk-event start frame.
- `carla_plan.initial_position`: local offset from the front truck, with keys `x`, `y`, `z`.
- `carla_plan.motion.mode`: motion type. `scripted_projectile` means animate props manually with `set_transform`.
- `carla_plan.motion.back_speed_mps`: rearward local-x speed toward the ego vehicle.
- `carla_plan.motion.lateral_drift_mps`: side drift speed.
- `carla_plan.motion.gravity`: if true, lower the payload z position over time.

Keep missing fields safe by using defaults.
