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
