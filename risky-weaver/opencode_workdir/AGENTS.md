# Risky Weaver OpenCode Workspace

This workspace is isolated from the old ChatScene pipeline.

OpenCode should edit only `generated_scene.scenic`.

Rules:

- Generate Scenic code, not CARLA Python.
- Read `opencode_task.json` as the only business input.
- Preserve the seed ego and primary actor declarations unless the action is `weather_visibility_change`.
- Use `simulation().currentTime`, never `simulation().current_time`.
- Use `wait` without arguments.
- Use `take` for actions and `do` for behaviors.
- Bind behavior with `with behavior BehaviorName(...)` in object declarations.
- Never use `require actor do Behavior()`.
- Use `Car` for `vehicle.*`, `Pedestrian` for `walker.*`, and `Prop` for `static.prop.*`.
