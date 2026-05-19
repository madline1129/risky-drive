# L4 Failure History

These are real failures observed in this pipeline. Read this before editing `generated_risk_scene.py`.

The lesson is simple: `scenario_config.json` is a physical task order, not a story prompt. The script must reproduce the configured actor, motion, camera view, and trace semantics. If the output only looks plausible but does not satisfy the physical task, it is a failure.

## 1. Template Cargo Drop Replaced the Requested Event

Symptom:

- The requested event was not cargo drop, but the generated scene still spawned payloads/metal pipes.
- The images showed the old template behavior instead of the configured risk.

Root cause:

- The generator reused `reference_executor.py` as an event template.

Do not:

- Copy reference event behavior.
- Add payloads, metal pipes, front braking, or any other familiar template unless `scenario_config` explicitly requests that exact scenario type.

Do:

- Use `reference_executor.py` only for CARLA mechanics: import, sync mode, camera queue, cleanup.
- Implement only the scenario type and primary actor in `physical_task` and `event_contract`.

## 2. Trigger Frame Was Outside the L4 Replay

Symptom:

- `event_trace` showed no actor motion.
- The configured trigger was an original SafeBench global frame such as `145`, but the generated L4 script only ran frames `0..139`.

Root cause:

- The generated script treated original scenario frame numbers as local L4 frame numbers.

Do not:

- Use `original_l3_trigger_frame` as the event start frame.

Do:

- Use `carla_plan.trigger_frame` / `physical_task.action.trigger_frame` as the local L4 trigger.
- Confirm `trigger_frame < args.frames` before the simulation loop.

## 3. Empty Front View for a Side Event

Symptom:

- Images were valid but showed an empty road.
- The primary actor was side-left/side-right or rear-left, outside the single front camera view.

Root cause:

- The generated script saved only a front camera image.

Do not:

- Save front-only `risk_rgb_XXXX.png` when `physical_task.visualization.default_mode` is `ego_surround_montage`.

Do:

- Attach all cameras listed in `physical_task.visualization.camera_specs`.
- Save top-level `risk_rgb_XXXX.png` as the requested six-view 2x3 montage.
- The default tile order is `CAM_FRONT`, `CAM_FRONT_LEFT`, `CAM_FRONT_RIGHT`, `CAM_BACK`, `CAM_BACK_LEFT`, `CAM_BACK_RIGHT`.

## 4. Six-View Montage Was Correct, But All Views Were Empty

Symptom:

- `risk_rgb_XXXX.png` was a 2400x900 six-view montage.
- All six views were empty.
- `event_trace` showed the primary actor near `(0, 0)` and over 200 meters from ego.

Observed example:

- Configured primary actor initial location: approximately `(-188.402, 109.314, 0.036)`.
- Trace primary actor position: approximately `(0.064, 0.0, -0.002)`.
- Distance to ego: about `213.758m`.

Root cause:

- The script ignored or lost `physical_task.primary_actor.initial_location`.
- A fallback placed the actor near world origin or an unrelated spawn point, then the script continued as if this was successful.

Do not:

- Continue after spawning an actor far from the requested L0 location.
- Fall back to a random map spawn point for a primary actor whose source is `l0_actor`.

Do:

- Build the primary actor transform directly from `physical_task.primary_actor.initial_location` and `initial_rotation`.
- Immediately after spawning, compare `actor.get_location()` with the requested coordinates.
- If the error is large, destroy the actor and retry near the requested pose with small `z` offsets only.
- If it still cannot spawn near the requested pose, fail clearly instead of producing empty images.

## 5. `event_trace.frames` Was Not a Frame List

Symptom:

- `event_trace.json` used `"frames": 180` and put per-frame dictionaries under `"frame_data"`.
- Validation failed with `event_trace.frames must be a non-empty list`.

Root cause:

- The generated script confused frame count with per-frame trace data.

Do not:

- Write per-frame trace under `frame_data`.
- Write an integer into `frames`.

Do:

- Write `frames` as a non-empty list of per-frame dictionaries.
- Each frame dictionary must include `event_contract.required_frame_fields`.

## 6. Side Vehicle Intrusion Was Implemented as Road Obstacle Intrusion

Symptom:

- The config text said an existing left-side vehicle should shift toward ego.
- The generated scene treated it as a generic `road_obstacle_intrusion`.
- The visual result looked unrelated or empty.

Root cause:

- The generator followed a broad scenario label instead of `physical_task.primary_actor`.

Do not:

- Spawn a new obstacle as the primary actor for `side_vehicle_intrusion`.
- Use a different actor id as the event owner.

Do:

- Use `physical_task.primary_actor.actor_id`, type, initial pose, and relative state.
- Move that same existing L0 side vehicle laterally toward the ego lane after trigger.
- Record `primary_actor_id`, `primary_actor_type_id`, `primary_actor_position`, `distance_to_ego_m`, and `relative_lateral_m`.

## 7. Lateral Shift Was Too Weak

Symptom:

- The actor moved, but stayed outside the ego-lane band.
- Example: relative lateral changed only from `-3.55m` to `-2.97m`, while acceptance required entering within `abs(relative_lateral_m) <= 2.2m`.

Root cause:

- The script followed a small textual shift such as `0.5m` instead of the stricter `physical_task.success_criteria`.

Do not:

- Stop after a visually subtle movement if success criteria require lane intrusion.

Do:

- Use `physical_task.action.minimum_lateral_shift_m` and `target_abs_relative_lateral_m_max`.
- Keep moving the primary actor toward the ego lane until the trace satisfies the criteria or fail.

## 8. Trace Looked Successful But Did Not Prove the Physical Event

Symptom:

- Images existed and trace existed.
- The actor was moving away or staying outside the risk zone.

Root cause:

- The trace recorded generic actor motion, not the configured risk.

Do not:

- Treat image existence as success.
- Fake trace values to satisfy validation.

Do:

- Record live actor states after `world.tick()`.
- Ensure trace values prove the event: correct actor id, correct position, correct distance/lateral trend, correct trigger timing.

## Final Pre-Flight Checklist

Before finishing `generated_risk_scene.py`, verify these points in code:

- `scenario_type` is exactly the configured type.
- The primary actor source, id, type, and initial pose match `physical_task`.
- Actor spawn location is checked after spawning.
- The local trigger frame is inside `args.frames`.
- Top-level `risk_rgb_XXXX.png` follows `physical_task.visualization`.
- `event_trace.frames` is a list of real per-frame states.
- No unrelated template artifacts appear in the scene or trace.
