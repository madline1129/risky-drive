# CARLA Recipes

## Import pattern

Use a helper that adds these paths from `--carla-root` before importing `carla`:

- `PythonAPI/carla`
- `PythonAPI/carla/agents`
- `PythonAPI/carla/dist/carla-*.egg`
- `PythonAPI/carla/dist/carla-*.whl`

Return the imported module from the helper and pass it to functions that need CARLA classes.

## World settings

Save `original_settings = world.get_settings()` before changing anything. In `try`, set:

- `settings.synchronous_mode = True`
- `settings.fixed_delta_seconds = 0.05`

In `finally`, destroy actors and call `world.apply_settings(original_settings)`.

## Image capture

Create `sensor.camera.rgb`, attach it to the ego vehicle, listen into a `queue.Queue`, tick the world, then call `image_queue.get(timeout=5.0)`. Save selected frames with:

`image.save_to_disk(os.path.join(args.output_dir, f"risk_rgb_{frame_idx:04d}.png"))`

## Actor cleanup

Keep every spawned actor in `actors`. In `finally`, iterate `reversed(actors)`, check `actor.is_alive`, and catch `RuntimeError` around destroy calls.

## L0 Pose Spawning

L0 actor locations are measured from the running SafeBench scene. A raw transform at that exact `(x, y, z)` may be slightly off the drivable lane, outside the pedestrian navmesh, or occupied when the generated L4 replay loads the map. Treat the absolute L0 pose as a best-effort hint. The hard requirement is preserving the configured actor type, lane relationship, and ego-relative longitudinal/lateral geometry. Do not fall back to world origin or an arbitrary spawn point.

For ego and vehicle primary actors:

1. Build the requested transform from `physical_task.*.initial_location` and `initial_rotation`.
2. Ask `world.get_map().get_waypoint(requested_location, project_to_road=True, lane_type=carla.LaneType.Driving)`.
3. If a waypoint exists near the requested location, use the waypoint transform with the requested yaw when available, and raise `z` by a small amount such as `0.2`.
4. Try `world.try_spawn_actor` at a small set of nearby transforms: requested transform with small z offsets, waypoint transform, and waypoint transform shifted slightly along the lane.
5. After spawning, compare `actor.get_location()` to the requested L0 location and to the requested ego-relative offset. For waypoint-snapped vehicles, exact absolute error can be relaxed when the relative longitudinal/lateral geometry is still close. If the actor appears near `(0, 0, 0)` or in an unrelated map region, destroy it and retry.

If the exact ego or primary-actor L0 pose still cannot spawn:

- Check `scenario_config.spawn_policy.relative_relocation_allowed`.
- Choose the closest valid driving waypoint or map spawn point for ego.
- Recompute primary and background actor world poses from the actual ego transform, preserving L0 relative longitudinal/lateral offsets. For example, use the actual ego forward/right vectors and place a front actor at `ego + forward * relative_longitudinal + right * relative_lateral`.
- Record `scene_relocated: true`, requested/actual ego/primary locations, requested/actual primary-relative-to-ego offsets, and `primary_relative_error_m` in `event_trace.json`.
- Continue into the simulation loop and save `risk_rgb_XXXX.png`; do not fail before frame 0 solely because the original absolute ego coordinate was not spawnable.

For pedestrians:

- Prefer the requested sidewalk/world location if it spawns correctly.
- If it fails, first compute the requested position from the actual ego pose and the configured relative longitudinal/lateral offsets.
- Use `world.get_random_location_from_navigation()` only if the returned point is near either the requested absolute L0 location or the recomputed ego-relative location. Do not use a random pedestrian point elsewhere in the map.

## Waypoint Direction

CARLA `Waypoint.next(distance)` expects a non-negative distance. Calling `next(-5.0)` can fail at runtime.

Use one of these instead:

- `waypoint.previous(abs(distance))` when available and you need to move backward along the lane.
- The actor or waypoint forward vector: `location - forward_vector * abs(distance)` for a simple backward offset.
- A positive `next(distance)` call only for forward travel.
