# L0 Scene Reconstruction

When `l0_state.json` or `scenario_config.scene_reconstruction` is available, the L4 script should recreate the original scene context before applying the risk event.

Use these priorities:

1. Load the L0 map if available. `source.map` or `road.map` may contain a CARLA map path; the town name is usually the basename such as `Town03`.
2. Apply L0 weather fields when available.
3. Spawn the ego vehicle at `ego.location` and `ego.rotation`. Match `ego.type_id` if possible, otherwise use a similar vehicle blueprint.
4. For front-vehicle events, spawn `nearest_front_actor` at its L0 relative position or absolute `location`/`rotation`. Match `type_id` if possible.
5. Recreate only nearby actors that materially affect the selected risk event. Keep actor count low and deterministic.
6. Attach the camera to the ego vehicle with the same front-camera convention as the reference executor.

Fallbacks:

- If an exact transform is occupied, try the same transform with a small z offset, then a nearby waypoint along the same lane.
- If `nearest_front_actor` is missing but the scenario needs a front actor, spawn one ahead of the L0 ego pose at `truck_distance`.
- If L0 has no usable pose, fall back to CARLA spawn points.

Do not choose a random or unrelated spawn point when L0 pose data is present.
