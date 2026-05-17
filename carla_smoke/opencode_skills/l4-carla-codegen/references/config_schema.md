# L4 Scenario Config Schema

The generated script reads `scenario_config.json` from the same directory as the script unless `--config` is provided.

Shared fields:

- `truck_distance`: meters ahead of the ego vehicle where the front truck should spawn.
- `trigger_frame`: fallback frame index when the risk event starts.
- `carla_plan`: nested execution plan from L3.
- `carla_plan.scenario_type`: scenario category. Respect it exactly and do not merge unrelated categories.
- `carla_plan.trigger_frame`: preferred risk-event start frame.
- `scene_reconstruction`: compact L0 state used to rebuild the original scene context.
- `source_l0_state_file`: original L0 state path when provided.
- `reconstruction_policy`: requirements for preserving the L0 scene identity.
- `event_contract`: required per-chain event trace output. Treat this as a hard acceptance contract.

When `scene_reconstruction` is present, use it before generic spawn points:

- `scene_reconstruction.source_map`: preferred map/town.
- `scene_reconstruction.ego.location` and `.rotation`: ego spawn anchor.
- `scene_reconstruction.weather`: weather to apply.
- `scene_reconstruction.nearest_front_actor`: actor to recreate for front-vehicle events.
- `scene_reconstruction.actors`: relevant nearby actors to optionally recreate.

Event trace:

- Write `event_trace.json` under `--output-dir`.
- `event_trace.scenario_type` must match `carla_plan.scenario_type`.
- `event_trace.frames` must record the event-specific physical state over time.
- Use `event_contract.required_frame_fields` to choose per-frame keys.

Scenario-specific fields:

## `front_vehicle_brake`

Only implement front-vehicle deceleration/braking. Do not spawn payloads or projectile objects.

- `carla_plan.target_actor`: usually `front_vehicle`.
- `carla_plan.brake_intensity`: CARLA brake command after trigger.
- `carla_plan.deceleration_mps2`: intended deceleration when approximating scripted motion.
- `carla_plan.target_speed_mps`: desired speed after braking.

## `cargo_drop`

Only implement payload/obstacle dropping from a vehicle. Do not add front-vehicle braking unless a compound event is explicitly configured.

- `carla_plan.object_type`: requested obstacle or payload type, for example `metal_pipe`.
- `carla_plan.object_count`: optional number of payload actors. Default to a small visible group.
- `carla_plan.initial_position`: local offset from the front truck, with keys `x`, `y`, `z`.
- `carla_plan.motion.mode`: motion type. `scripted_projectile` means animate props manually with `set_transform`.
- `carla_plan.motion.back_speed_mps`: rearward local-x speed toward the ego vehicle.
- `carla_plan.motion.lateral_drift_mps`: side drift speed.
- `carla_plan.motion.gravity`: if true, lower the payload z position over time.

## `vulnerable_actor_intrusion`

Only implement pedestrian/cyclist intrusion.

- `carla_plan.actor_type`: `walker`, `cyclist`, or compatible CARLA actor approximation.
- `carla_plan.start_position`: local spawn position.
- `carla_plan.crossing_direction`: side-to-side motion direction.
- `carla_plan.speed_mps`: actor motion speed.

## `road_obstacle_intrusion`

Only implement static or slow obstacle intrusion, not vehicle braking or cargo drop.

- `carla_plan.object_type`: obstacle category.
- `carla_plan.initial_position`: obstacle start position.
- `carla_plan.motion`: static or slow lateral/longitudinal motion.

Keep missing fields safe by using defaults.
