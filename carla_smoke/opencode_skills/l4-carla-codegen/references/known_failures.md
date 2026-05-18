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
