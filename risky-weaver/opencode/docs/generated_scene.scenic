'''RiskyWeaver universal seed Scenic file.
OpenCode must edit this file in place.

Business input:
- Read only opencode_task.json.
- Do not infer scenario semantics from this seed file.
- This seed file only provides insertion anchors.
'''

# ===== RiskyWeaver generated map setup starts here =====
# OpenCode should replace this block using task.scene_context.
Town = "Town01"
param map = localPath("/tmp/placeholder.xodr")
param carla_map = Town
model scenic.simulators.carla.model
# ===== RiskyWeaver generated map setup ends here =====


# ===== RiskyWeaver generated constants start here =====
# OpenCode should replace this block using task.scene_context.
TIMESTEP = 0.05
TOTAL_STEPS = 100
TOTAL_DURATION = 5.0
# ===== RiskyWeaver generated constants end here =====


# ===== RiskyWeaver generated behaviors start here =====
# OpenCode should insert all generated behavior definitions here.
# Behaviors must appear before object declarations.
# ===== RiskyWeaver generated behaviors end here =====


# ===== RiskyWeaver generated objects start here =====
# OpenCode should insert all object declarations from task.low_level_objects here.
# ===== RiskyWeaver generated objects end here =====