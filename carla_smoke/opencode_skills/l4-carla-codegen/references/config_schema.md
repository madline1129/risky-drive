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
- `spawn_policy`: fallback policy when exact L0 absolute poses are not spawnable after reloading CARLA. Relative ego-to-actor geometry is more important than exact absolute coordinates.
- `event_contract`: required per-chain event trace output. Treat this as a hard acceptance contract.
- `carla_plan.actor_motion_plan`: explicit movement/behavior plan for all important actors after the L0 snapshot.
- `l4_plan_agent`: optional LLM translation from the natural-language L3 chain into the executable plan and object constraints.
- `object_registry`: participant list from L3/L4 PlanAgent. It distinguishes the primary risk object from background/occluder/affected objects.
- `risk_object_spec`: concrete translation of the one object that receives the risk perturbation.
- `physical_task`: hard physical task order. This is the most authoritative part of the config for the generated script.

If `physical_task` or `risk_object_spec` conflicts with free-form text such as `chain_description`, follow `physical_task` and `risk_object_spec`.

Important `risk_object_spec` fields:

- `risk_object_spec.primary_object`: the exact object that must be perturbed: front vehicle, payload, vulnerable actor, road obstacle, or existing side vehicle.
- `risk_object_spec.primary_object.source == "l0_actor"` means preserve that L0 actor as the semantic primary risk object: configured id, type/kind, relative position to ego, and required action. In a fresh CARLA replay, the live CARLA actor id may differ and the absolute pose may be relocated if relative geometry is preserved.
- `risk_object_spec.primary_object.source == "generated_actor"` means spawn a new object, but use the provided world coordinates when present.
- `risk_object_spec.geometry`: precomputed world-space start/end/target locations for generated actors. Do not treat these as local coordinates again.
- `risk_object_spec.action`: the required perturbation for the primary object only.
- `risk_object_spec.success_criteria`: numeric criteria the live scene and trace should satisfy.
- `risk_object_spec.forbidden_substitutions`: event templates that must not be used for this chain.

Important `object_registry` fields:

- `object_registry.primary_object`: the object that owns the primary risk event.
- `object_registry.participants`: ego, primary actor, and background actors involved in the chain.
- Objects marked `must_not_drive_primary_event` are context only. Recreate them only if useful, and never let them become the main event.

Important `physical_task` fields:

- `physical_task.primary_actor`: the actor that must drive the event.
- `physical_task.primary_actor.source == "l0_actor"` means reuse that same L0 actor identity semantically: same configured id/type/kind, same relative role in the scene, and same primary action. Do not replace it with a generic spawned obstacle or a different scenario template.
- After spawning an actor from `physical_task.primary_actor.initial_location`, verify the live actor location. If it appears near world origin or an unrelated spawn point, destroy it and retry near the requested L0 pose or near the recomputed ego-relative pose.
- `physical_task.action`: required motion, trigger timing, and target geometry.
- `physical_task.success_criteria`: numeric acceptance criteria. The generated physical scene must satisfy these.
- `physical_task.trace_schema.top_level_frames_key`: per-frame trace data must be written under this key, normally `frames`.
- `physical_task.visualization`: required image viewpoint. By default save every top-level `risk_rgb_XXXX.png` as a six-view 2x3 ego-camera montage.

When `scene_reconstruction` is present, use it before generic spawn points:

- `scene_reconstruction.source_map`: preferred map/town.
- `scene_reconstruction.ego.location` and `.rotation`: ego spawn anchor.
- `scene_reconstruction.weather`: weather to apply.
- `scene_reconstruction.nearest_front_actor`: actor to recreate for front-vehicle events.
- `scene_reconstruction.actors`: relevant nearby actors to optionally recreate.

Spawn policy:

- Prefer the exact L0 ego and actor poses when they spawn cleanly, with small z offsets and nearby waypoint/navmesh projection.
- If exact ego or primary-actor L0 spawning fails, `spawn_policy.relative_relocation_allowed` permits moving the entire L4 scene to a valid ego spawn/waypoint.
- When relocating, recompute the primary actor and relevant participants from the actual ego transform using their L0 relative longitudinal/lateral offsets. Do not keep the primary actor at the old absolute L0 world coordinate after relocating ego.
- It is acceptable for requested and actual absolute locations to differ when relative longitudinal/lateral offsets remain close enough for the visual risk event.
- Record relocation metadata in `event_trace.json`: `scene_relocated`, `relocation_reason`, `requested_ego_location`, `actual_ego_location`, `requested_primary_location`, `actual_primary_initial_location`, `requested_primary_relative_to_ego`, `actual_primary_relative_to_ego`, and `primary_relative_error_m`.
- Never accept `(0,0,0)` as a valid ego or primary actor spawn.

Risk images:

- Save top-level `risk_rgb_XXXX.png` files as the review images.
- When `physical_task.visualization.default_mode == "ego_surround_montage"`, attach the requested six cameras to the ego vehicle and compose a 2x3 montage.
- Use `physical_task.visualization.tile_order` exactly. The default order is `CAM_FRONT`, `CAM_FRONT_LEFT`, `CAM_FRONT_RIGHT`, `CAM_BACK`, `CAM_BACK_LEFT`, `CAM_BACK_RIGHT`.
- You may also save per-camera files in subdirectories, but the top-level `risk_rgb_XXXX.png` must be the montage.

Event trace:

- Write `event_trace.json` under `--output-dir`.
- `event_trace.scenario_type` must match `carla_plan.scenario_type`.
- `event_trace.frames` must record the event-specific physical state over time.
- Do not write per-frame trace data under `frame_data`.
- Use `event_contract.required_frame_fields` to choose per-frame keys.

Actor motion plan:

- L0 provides initial appearance and geometry only.
- `actor_motion_plan.ego` controls the ego behavior after the snapshot.
- `actor_motion_plan.front_actor` controls the nearest front actor behavior; it may be a primary actor, occluder, carrier, or background object.
- `actor_motion_plan.primary_actor` is the actor that must create the risk event.
- `actor_motion_plan.background_actors` describes whether to preserve, ignore, or hold background actors.
- Do not invent a simpler front-car braking/collision behavior if `actor_motion_plan.primary_actor` is not the front actor.

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
- Prefer `risk_object_spec.geometry.start_world` and `end_world` over raw `carla_plan.start_position`.
- If `risk_object_spec.primary_object.source == "l0_actor"`, use that original actor id/type/pose as the primary vulnerable actor and do not spawn a generated replacement.
- If `risk_object_spec.primary_object.source == "generated_actor"`, spawn the vulnerable actor near `risk_object_spec.primary_object.initial_location`, then move it along `risk_object_spec.geometry.path_world`.
- Never reset the actor to `(0, 0, 0)` or reinterpret a precomputed world point as a local offset.

## `road_obstacle_intrusion`

Only implement static or slow obstacle intrusion, not vehicle braking or cargo drop.

- `carla_plan.object_type`: obstacle category.
- `carla_plan.initial_position`: obstacle start position.
- `carla_plan.motion`: static or slow lateral/longitudinal motion.

## `side_vehicle_intrusion`

Only implement an existing L0 side vehicle laterally intruding toward the ego lane.

- Prefer `risk_object_spec.primary_object` for actor id, type, initial pose, required lateral shift, and forbidden substitutions.
- The primary actor must be `physical_task.primary_actor.actor_id`.
- Spawn/reconstruct that actor from `physical_task.primary_actor.initial_location` and `initial_rotation`.
- Move the same vehicle toward the ego lane after `physical_task.action.trigger_frame`.
- The motion must satisfy `physical_task.success_criteria.relative_lateral_delta_m_min` and `min_abs_relative_lateral_m_max`.
- The images must follow `physical_task.visualization`. For side-vehicle intrusion this means the six-view montage must show the side vehicle in at least one tile, not a front-only empty road.
- Do not spawn a new road obstacle as the primary actor.
- Do not implement this as front-vehicle braking, cargo drop, or a generic static obstacle.

Keep missing fields safe by using defaults.
