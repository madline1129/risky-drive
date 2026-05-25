'''Seed Scenic file. OpenCode must edit this file in place.'''
Town = "Town05"
param map = localPath("/mnt/data2/whz/risky-drive/safebench/scenario/scenario_data/scenic_data/maps/Town05.xodr")
param carla_map = Town
model scenic.simulators.carla.model

TRIGGER_FRAME = 20

ego = Car at (-184.499 @ -106.230),
    with heading 0.621 deg,
    with regionContainedIn None,
    with blueprint "vehicle.lincoln.mkz_2017"

primary_actor = Car at (-188.086 @ -98.268),
    with heading 0.621 deg,
    with regionContainedIn None,
    with blueprint "vehicle.bh.crossbike"
