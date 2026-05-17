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
