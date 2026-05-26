'''RiskyWeaver generated Scenic scenario.
Generated from low-level action primitives.
Experiment 1: test whether side_vehicle behavior is executed using throttle control.
'''

# ===== RiskyWeaver generated map setup starts here =====
param map = localPath("/mnt/data2/whz/risky-drive/safebench/scenario/scenario_data/scenic_data/maps/Town05.xodr")
param carla_map = "Town05"
model scenic.simulators.carla.model
# ===== RiskyWeaver generated map setup ends here =====


# ===== RiskyWeaver generated constants start here =====
TIMESTEP = 0.2
TOTAL_STEPS = 20
TOTAL_DURATION = 4.0
SIDE_TRIGGER_TIME = 1.0
# ===== RiskyWeaver generated constants end here =====


# ===== RiskyWeaver generated behaviors start here =====

behavior EgoBehavior():
    while True:
        take SetAutopilotAction(True)
        take SetSpeedAction(8.0)
        wait


behavior SideVehicleThrottleBehavior():
    # Before trigger time: keep side vehicle stopped and disable autopilot.
    while simulation().currentTime < SIDE_TRIGGER_TIME:
        take SetAutopilotAction(False)
        take SetThrottleAction(0.0)
        take SetBrakeAction(1.0)
        wait

    # Active stage: apply throttle to verify whether this behavior actually controls the actor.
    while simulation().currentTime < TOTAL_DURATION:
        take SetAutopilotAction(False)
        take SetBrakeAction(0.0)
        take SetThrottleAction(1.0)
        wait

    # After active stage: stop the vehicle.
    while True:
        take SetAutopilotAction(False)
        take SetThrottleAction(0.0)
        take SetBrakeAction(1.0)
        wait

# ===== RiskyWeaver generated behaviors end here =====


# ===== RiskyWeaver generated objects start here =====

ego = Car at (-184.499 @ -106.23),
    with heading 0.621 deg,
    with regionContainedIn None,
    with blueprint "vehicle.lincoln.mkz_2017",
    with behavior EgoBehavior()

side_vehicle = Car at (-188.086 @ -98.268),
    with heading 0.621 deg,
    with regionContainedIn None,
    with blueprint "vehicle.tesla.model3",
    with behavior SideVehicleThrottleBehavior()

# ===== RiskyWeaver generated objects end here =====