# Scenic Examples

Use these as syntax references. Do not copy the event blindly.

## Basic SafeBench Scenic Header

```scenic
'''A concise scenario description.'''
Town = 'Town05'
param map = localPath('/absolute/path/to/safebench/scenario/scenario_data/scenic_data/maps/Town05.xodr')
param carla_map = Town
model scenic.simulators.carla.model
EGO_MODEL = "vehicle.lincoln.mkz_2017"
```

Use the exact absolute map path provided in `semantic_primitives.json`; the line above is only a shape example.

## Ego And Front Vehicle

```scenic
intersection = Uniform(*filter(lambda i: i.is4Way and not i.isSignalized, network.intersections))
egoInitLane = Uniform(*intersection.incomingLanes)
egoManeuver = Uniform(*filter(lambda m: m.type is ManeuverType.STRAIGHT, egoInitLane.maneuvers))
egoSpawnPt = OrientedPoint in egoManeuver.startLane.centerline

ego = Car at egoSpawnPt,
    with regionContainedIn None,
    with blueprint EGO_MODEL

param OPT_FRONT_DIST = Range(12, 25)
FrontSpawnPt = OrientedPoint following roadDirection from egoSpawnPt for globalParameters.OPT_FRONT_DIST
FrontAgent = Car at FrontSpawnPt,
    with behavior FollowLaneBehavior(target_speed=3)
```

## Pedestrian Intrusion Behavior

```scenic
behavior CrossingAdvBehavior():
    initialDirection = self.heading
    for _ in range(20):
        wait
    while True:
        take SetWalkingDirectionAction(initialDirection)
        take SetWalkingSpeedAction(2.5)
        wait

param OPT_Y_DIST = Range(8, 18)
param OPT_X_DIST = Range(2, 5)
IntPt = OrientedPoint following roadDirection from egoSpawnPt for globalParameters.OPT_Y_DIST

AdvAgent = Pedestrian right of IntPt by globalParameters.OPT_X_DIST,
    with heading IntPt.heading + 90 deg,
    with behavior CrossingAdvBehavior()
```

When `semantic_primitives.json` gives an L0-relative actor, preserve its side:

```scenic
PrimaryRefPt = OrientedPoint following roadDirection from egoSpawnPt for 4.849

AdvAgent = Pedestrian left of PrimaryRefPt by 2.93,
    with heading PrimaryRefPt.heading + 90 deg,
    with behavior CrossingAdvBehavior()
```

If `left of PrimaryRefPt by 2.93` is not spawnable, try nearby same-side values such as `left of PrimaryRefPt by 2.43` or `left of PrimaryRefPt by 3.43`; do not switch to `right of`.

## Front Vehicle Brake Behavior

```scenic
behavior BrakeBehavior():
    for _ in range(25):
        take SetSpeedAction(4)
        wait
    while True:
        take SetSpeedAction(0)
        wait

FrontAgent = Car at FrontSpawnPt,
    with behavior BrakeBehavior()
```

If `SetSpeedAction` is unavailable in this Scenic/CARLA setup, use a low `FollowLaneBehavior(target_speed=...)` followed by a stationary behavior supported by the local examples. Keep the scene executable over being overly elaborate.

## Notes

- `Range(...)` values can be floats; loop counts used by Python `range(...)` must be integers.
- Prefer fixed integer wait counts for behavior delays.
- For pedestrian and vehicle positions, relative placement around `egoSpawnPt` or an interaction point is often more robust than raw CARLA coordinates.
- If using L0 world coordinates, use the converted Scenic coordinates supplied by `semantic_primitives.json`; do not convert raw CARLA coordinates yourself.
- Never use `Point(-184.435, 113.147, 0.089)` in Scenic driving code.
