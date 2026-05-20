# Known Failures

## `UnboundLocalError` or `NameError` for `carla`

Cause: a function references `carla` while also importing it locally, or assumes a global module exists.

Fix: use `carla = import_carla(args.carla_root)` in `main()`, then pass `carla` into helper functions.

## `ModuleNotFoundError: No module named 'carla'`

Cause: CARLA PythonAPI paths were not added before import.

Fix: add egg/whl paths from `--carla-root/PythonAPI/carla/dist` before `import carla`.

## `argparse` fails in `--help`

Cause: the script imports CARLA, connects to CARLA, or reads required runtime files before parsing help.

Fix: keep CARLA import and config loading inside `main()` after `args = parser.parse_args()`. `--help` must exit before runtime work.

## No output images

Cause: camera queue not drained after `world.tick()`, or `save_every` branch never calls `save_to_disk`.

Fix: call `image = image_queue.get(timeout=5.0)` after each tick and save frames named `risk_rgb_XXXX.png`.

## `event_trace.frames` is an integer or `frame_data` contains the trace

Cause: the script wrote a frame count into `frames` and put per-frame dictionaries under a different key.

Fix: write `frames` as the non-empty list of per-frame dictionaries. Do not use `frame_data`.

## Side-vehicle intrusion looks like an empty road or generic obstacle

Cause: the script ignored `physical_task.primary_actor` and spawned an unrelated obstacle, the lateral shift was too small to enter the ego lane band, or it saved a front-only camera even though the primary actor is beside/behind the ego.

Fix: use the same L0 actor id/type from `physical_task.primary_actor`, move it toward the ego lane after trigger, satisfy `physical_task.success_criteria`, and follow `physical_task.visualization` so `risk_rgb_XXXX.png` is a six-view montage which shows the side vehicle.

## `risk_rgb_XXXX.png` is front-only

Cause: the script used the old reference executor's single front camera pattern.

Fix: attach all cameras listed in `physical_task.visualization.camera_specs`, collect all images after each tick, and save the top-level `risk_rgb_XXXX.png` as the requested 2x3 montage.

## Trace shows the primary actor near `(0, 0)` or hundreds of meters from ego

Cause: the script ignored or lost `physical_task.primary_actor.initial_location`, or a spawn fallback placed the actor at world origin / an unrelated spawn point.

Fix: build the spawn transform from `physical_task.primary_actor.initial_location` and `initial_rotation` first. Immediately after spawning, check both `actor.get_location()` and the actor's ego-relative offset. If the absolute pose is not spawnable but the scenario can be relocated, recompute the actor pose from the actual ego transform and preserve the configured relative longitudinal/lateral geometry. Never accept world origin or an unrelated map region.

### Correct actor selected, but vehicle spawns at `(0,0,0)`

Cause: the script used the raw L0 transform directly even though the pose was not a valid vehicle spawn pose on the loaded map, or it accepted a failed spawn fallback as success.

Fix: for ego and vehicle primary actors, snap the requested L0 location to a nearby driving-lane waypoint with `world.get_map().get_waypoint(..., project_to_road=True, lane_type=carla.LaneType.Driving)`, try small `z` offsets and nearby lane shifts, then verify the live actor location. If exact absolute spawning still fails, relocate the scene and preserve relative geometry. Never continue if the actor is near world origin or far from both the requested L0 pose and the recomputed ego-relative pose.

## L0 pedestrian coordinate is not a legal walker spawn

Cause: the L0 coordinate is a snapshot of an already-existing actor, not a guaranteed legal navmesh birth point in a fresh CARLA replay.

Fix: treat the absolute coordinate as a hint. If it fails, place the walker on a nearby navmesh point computed from the actual ego pose and the configured relative longitudinal/lateral offsets. Record requested/actual absolute and relative locations in `event_trace.json`.

## `waypoint.next()` gets a negative distance

Cause: the script tried to move backward along a lane with `wp.next(-distance)`.

Fix: `Waypoint.next(distance)` only accepts non-negative values. Use `waypoint.previous(abs(distance))` when available, or compute `location - forward_vector * abs(distance)` for a backward offset.
