#!/usr/bin/env python3
"""Replay a SafeBench Scenic scene and apply an L4 risk intervention in-place."""

import argparse
import glob
import json
import math
import os
import queue
import random
import sys
import time


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


REPO_ROOT = repo_root_from_this_file()
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from carla_smoke.scenes.normal_driving_scene import build_scene_snapshot, classify_relative_position
from carla_smoke.scenes.safebench_scenic_scene import (
    CAMERA_SPECS,
    add_repo_paths,
    camera_calibration_metadata,
    carla_image_to_rgb,
    check_scenic_python_deps,
    drain_camera_queues,
    extract_scenario_description,
    get_ego_actor,
    import_carla_from_root,
    prime_scenic_behavior,
    read_surround_images,
    set_ego_autopilot,
    write_json,
    write_rgb_png,
)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def vector_length(vector):
    return (vector.x * vector.x + vector.y * vector.y + vector.z * vector.z) ** 0.5


def location_dict(location):
    return {"x": round(location.x, 3), "y": round(location.y, 3), "z": round(location.z, 3)}


def distance(a, b):
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def numeric(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def actor_id_text(value):
    if value is None:
        return None
    return str(value)


def scenario_type_from_config(config):
    return (
        ((config.get("carla_plan") or {}).get("scenario_type"))
        or ((config.get("physical_task") or {}).get("scenario_type"))
        or ((config.get("risk_object_spec") or {}).get("scenario_type"))
        or "front_vehicle_brake"
    )


def trigger_frame_from_config(config, default):
    for source in (config.get("physical_task") or {}, config.get("carla_plan") or {}, config.get("risk_object_spec") or {}):
        action = source.get("action") if isinstance(source.get("action"), dict) else {}
        value = action.get("trigger_frame") if action else source.get("trigger_frame")
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    return default


def pre_roll_frame_from_config(config, default=0):
    policy = config.get("time_axis_policy") or {}
    for key in ("reconstruction_frame", "source_frame", "risk_peak_frame"):
        try:
            value = policy.get(key)
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            pass
    scene = config.get("scene_reconstruction") or {}
    try:
        value = scene.get("source_frame")
        if value is not None:
            return max(0, int(value))
    except (TypeError, ValueError):
        pass
    return default


def primary_object_from_config(config):
    risk_primary = ((config.get("risk_object_spec") or {}).get("primary_object") or {})
    physical_primary = ((config.get("physical_task") or {}).get("primary_actor") or {})
    return risk_primary if risk_primary else physical_primary


def actor_location_from_l0(actor):
    location = (actor or {}).get("location") or (actor or {}).get("initial_location")
    if not isinstance(location, dict):
        return None
    x = numeric(location.get("x"))
    y = numeric(location.get("y"))
    z = numeric(location.get("z"), 0.0)
    if x is None or y is None:
        return None
    return {"x": x, "y": y, "z": z}


def actor_by_id(l0_state, actor_id):
    target = actor_id_text(actor_id)
    if target is None:
        return None
    for actor in l0_state.get("actors", []) if isinstance(l0_state.get("actors"), list) else []:
        if actor_id_text(actor.get("id")) == target:
            return actor
    nearest = l0_state.get("nearest_front_actor")
    if isinstance(nearest, dict) and actor_id_text(nearest.get("id")) == target:
        return nearest
    return None


def l0_primary_actor(config, l0_state):
    primary = primary_object_from_config(config)
    actor = actor_by_id(l0_state, primary.get("actor_id"))
    if actor:
        return actor
    physical = ((config.get("physical_task") or {}).get("primary_actor") or {})
    actor = actor_by_id(l0_state, physical.get("actor_id"))
    if actor:
        return actor
    return primary or physical


def relative_position(ego_transform, actor_location):
    forward = ego_transform.get_forward_vector()
    right = ego_transform.get_right_vector()
    ego_location = ego_transform.location
    dx = actor_location.x - ego_location.x
    dy = actor_location.y - ego_location.y
    dz = actor_location.z - ego_location.z
    longitudinal = dx * forward.x + dy * forward.y + dz * forward.z
    lateral = dx * right.x + dy * right.y + dz * right.z
    return longitudinal, lateral


def actor_match_score(actor, ego_transform, target_actor, target_kind):
    if target_kind == "vehicle" and not actor.type_id.startswith("vehicle."):
        return None
    if target_kind in {"pedestrian", "walker"} and not actor.type_id.startswith("walker."):
        return None
    actor_location = actor.get_location()
    longitudinal, lateral = relative_position(ego_transform, actor_location)
    score = 0.0
    target_type = target_actor.get("type_id")
    if target_type and actor.type_id == target_type:
        score -= 50.0
    target_long = numeric(target_actor.get("relative_longitudinal_m"))
    target_lat = numeric(target_actor.get("relative_lateral_m"))
    target_dist = numeric(target_actor.get("distance_m"))
    if target_long is not None:
        score += abs(longitudinal - target_long) * 2.0
    elif target_kind == "vehicle" and longitudinal < 0:
        score += 100.0
    if target_lat is not None:
        score += abs(lateral - target_lat) * 3.0
    elif target_kind == "vehicle":
        score += abs(lateral)
    if target_dist is not None:
        score += abs(distance(actor_location, ego_transform.location) - target_dist)
    return score


def match_live_actor(world, ego, target_actor, scenario_type):
    target_kind = target_actor.get("kind")
    if scenario_type in {"front_vehicle_brake", "side_vehicle_intrusion"}:
        target_kind = "vehicle"
    if scenario_type == "vulnerable_actor_intrusion":
        target_kind = target_kind or "walker"

    ego_transform = ego.get_transform()
    candidates = []
    for actor in world.get_actors():
        if actor.id == ego.id:
            continue
        score = actor_match_score(actor, ego_transform, target_actor, target_kind)
        if score is not None:
            candidates.append((score, actor))
    if not candidates and scenario_type == "front_vehicle_brake":
        for actor in world.get_actors().filter("vehicle.*"):
            if actor.id == ego.id:
                continue
            longitudinal, lateral = relative_position(ego_transform, actor.get_location())
            if longitudinal >= -1.0:
                candidates.append((longitudinal + abs(lateral) * 3.0, actor))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def spawn_blueprint_actor(carla, world, blueprints, pattern, transform, role_name):
    candidates = list(blueprints.filter(pattern))
    if not candidates and pattern != "static.prop.streetbarrier":
        candidates = list(blueprints.filter("static.prop.streetbarrier"))
    if not candidates:
        return None
    bp = candidates[0]
    if bp.has_attribute("role_name"):
        bp.set_attribute("role_name", role_name)
    actor = world.try_spawn_actor(bp, transform)
    return actor


def local_point_to_world(carla, ego_transform, local):
    forward = ego_transform.get_forward_vector()
    right = ego_transform.get_right_vector()
    up = ego_transform.get_up_vector()
    location = ego_transform.location + carla.Location(
        x=forward.x * local.get("x", 0.0) + right.x * local.get("y", 0.0) + up.x * local.get("z", 0.0),
        y=forward.y * local.get("x", 0.0) + right.y * local.get("y", 0.0) + up.y * local.get("z", 0.0),
        z=forward.z * local.get("x", 0.0) + right.z * local.get("y", 0.0) + up.z * local.get("z", 0.0),
    )
    return location


def transform_from_config_location(carla, location, rotation=None, z_offset=0.4):
    rotation = rotation or {}
    return carla.Transform(
        carla.Location(
            x=numeric(location.get("x"), 0.0),
            y=numeric(location.get("y"), 0.0),
            z=numeric(location.get("z"), 0.0) + z_offset,
        ),
        carla.Rotation(
            pitch=numeric(rotation.get("pitch"), 0.0),
            yaw=numeric(rotation.get("yaw"), 0.0),
            roll=numeric(rotation.get("roll"), 0.0),
        ),
    )


def save_surround_risk_images(images, output_dir, frame_idx):
    import numpy as np

    per_camera_files = {}
    tiles = []
    for camera_name, _ in CAMERA_SPECS:
        image = images[camera_name]
        camera_dir = os.path.join(output_dir, camera_name)
        os.makedirs(camera_dir, exist_ok=True)
        image_file = f"risk_rgb_{frame_idx:04d}.png"
        image_path = os.path.join(camera_dir, image_file)
        image.save_to_disk(image_path)
        per_camera_files[camera_name] = os.path.join(camera_name, image_file)
        tiles.append(carla_image_to_rgb(image))
    montage = np.vstack([np.hstack(tiles[:3]), np.hstack(tiles[3:])])
    montage_file = f"risk_rgb_{frame_idx:04d}.png"
    write_rgb_png(os.path.join(output_dir, montage_file), montage)
    return montage_file, per_camera_files


class InterventionState:
    def __init__(self):
        self.primary_actor = None
        self.reported_primary_id = None
        self.spawned_actors = []
        self.started = False
        self.initial_speed = None
        self.initial_lateral = None
        self.generated_start = None
        self.generated_end = None


def clean_output_dir(output_dir):
    for pattern in ["risk_rgb_*.png", "state_*.json", "event_trace.json", "camera_calibration.json"]:
        for path in glob.glob(os.path.join(output_dir, pattern)):
            os.remove(path)
    for camera_name, _ in CAMERA_SPECS:
        camera_dir = os.path.join(output_dir, camera_name)
        if os.path.isdir(camera_dir):
            for path in glob.glob(os.path.join(camera_dir, "risk_rgb_*.png")):
                os.remove(path)


def prepare_generated_actor(carla, world, blueprints, ego, config, scenario_type, state):
    risk_spec = config.get("risk_object_spec") or {}
    geometry = risk_spec.get("geometry") or {}
    primary = risk_spec.get("primary_object") or {}
    ego_transform = ego.get_transform()

    if scenario_type == "vulnerable_actor_intrusion":
        start = geometry.get("start_world") or primary.get("initial_location")
        end = geometry.get("end_world")
        if not start:
            start = {"x": local_point_to_world(carla, ego_transform, {"x": 14.0, "y": 4.0, "z": 0.2}).x,
                     "y": local_point_to_world(carla, ego_transform, {"x": 14.0, "y": 4.0, "z": 0.2}).y,
                     "z": local_point_to_world(carla, ego_transform, {"x": 14.0, "y": 4.0, "z": 0.2}).z}
        if not end:
            end_location = local_point_to_world(carla, ego_transform, {"x": 6.0, "y": -1.0, "z": 0.2})
            end = location_dict(end_location)
        transform = transform_from_config_location(carla, start, primary.get("initial_rotation"), z_offset=0.3)
        actor = spawn_blueprint_actor(carla, world, blueprints, "walker.pedestrian.*", transform, "l4_intervention_walker")
        if actor is not None:
            state.primary_actor = actor
            state.reported_primary_id = primary.get("actor_id") or "generated_vulnerable_actor"
            state.spawned_actors.append(actor)
            state.generated_start = actor.get_location()
            state.generated_end = carla.Location(x=end["x"], y=end["y"], z=end.get("z", state.generated_start.z))

    elif scenario_type in {"road_obstacle_intrusion", "cargo_drop"}:
        start = geometry.get("start_world") or geometry.get("initial_world_location") or primary.get("initial_location")
        if not start:
            local = (geometry.get("start_local") or geometry.get("initial_local_offset_from_carrier") or {"x": 12.0, "y": 2.0, "z": 0.4})
            start_location = local_point_to_world(carla, ego_transform, local)
            start = location_dict(start_location)
        transform = transform_from_config_location(carla, start, primary.get("initial_rotation"), z_offset=0.4)
        actor = spawn_blueprint_actor(carla, world, blueprints, "static.prop.*", transform, "l4_intervention_object")
        if actor is not None:
            state.primary_actor = actor
            state.reported_primary_id = primary.get("actor_id") or "generated_object"
            state.spawned_actors.append(actor)
            state.generated_start = actor.get_location()
            target = geometry.get("target_world") or geometry.get("end_world")
            if target:
                state.generated_end = carla.Location(x=target["x"], y=target["y"], z=target.get("z", state.generated_start.z))


def apply_intervention(carla, ego, config, scenario_type, state, local_frame, trigger_frame):
    if local_frame < trigger_frame:
        return

    state.started = True
    actor = state.primary_actor
    if actor is None:
        return

    if scenario_type == "front_vehicle_brake":
        if state.initial_speed is None:
            state.initial_speed = vector_length(actor.get_velocity())
        try:
            actor.set_autopilot(False)
        except RuntimeError:
            pass
        actor.apply_control(carla.VehicleControl(throttle=0.0, brake=1.0, hand_brake=False))
        return

    if scenario_type == "side_vehicle_intrusion":
        ego_transform = ego.get_transform()
        actor_location = actor.get_location()
        _, lateral = relative_position(ego_transform, actor_location)
        if state.initial_lateral is None:
            state.initial_lateral = lateral
        right = ego_transform.get_right_vector()
        direction = -1.0 if lateral > 0 else 1.0
        step = 0.08
        new_transform = actor.get_transform()
        new_transform.location.x += right.x * direction * step
        new_transform.location.y += right.y * direction * step
        actor.set_transform(new_transform)
        return

    if scenario_type in {"vulnerable_actor_intrusion", "road_obstacle_intrusion", "cargo_drop"}:
        if state.generated_start is None or state.generated_end is None:
            return
        progress = min(1.0, max(0.0, (local_frame - trigger_frame) / 35.0))
        transform = actor.get_transform()
        transform.location.x = state.generated_start.x + (state.generated_end.x - state.generated_start.x) * progress
        transform.location.y = state.generated_start.y + (state.generated_end.y - state.generated_start.y) * progress
        transform.location.z = state.generated_start.z + (state.generated_end.z - state.generated_start.z) * progress
        actor.set_transform(transform)


def trace_frame(ego, primary_actor, scenario_type, local_frame, reported_primary_id=None):
    ego_transform = ego.get_transform()
    ego_speed = vector_length(ego.get_velocity())
    frame = {
        "frame": local_frame,
        "ego_speed_mps": round(ego_speed, 3),
    }
    if primary_actor is None:
        frame["primary_actor_present"] = False
        return frame

    actor_location = primary_actor.get_location()
    longitudinal, lateral = relative_position(ego_transform, actor_location)
    actor_speed = vector_length(primary_actor.get_velocity())
    position = location_dict(actor_location)
    frame.update(
        {
            "primary_actor_present": True,
            "primary_actor_id": reported_primary_id if reported_primary_id is not None else primary_actor.id,
            "live_primary_actor_id": primary_actor.id,
            "primary_actor_type_id": primary_actor.type_id,
            "primary_actor_position": position,
            "distance_to_ego_m": round(distance(actor_location, ego_transform.location), 3),
            "relative_longitudinal_m": round(longitudinal, 3),
            "relative_lateral_m": round(lateral, 3),
            "primary_actor_speed_mps": round(actor_speed, 3),
        }
    )
    if scenario_type == "front_vehicle_brake":
        frame["front_actor_speed_mps"] = round(actor_speed, 3)
        frame["front_distance_m"] = round(max(0.0, longitudinal), 3)
    if scenario_type == "side_vehicle_intrusion":
        frame["side_actor_speed_mps"] = round(actor_speed, 3)
    if scenario_type == "vulnerable_actor_intrusion":
        frame["vulnerable_actor_position"] = position
    if scenario_type == "road_obstacle_intrusion":
        frame["obstacle_positions"] = [position]
        frame["obstacle_distance_to_ego_m"] = frame["distance_to_ego_m"]
        frame["obstacle_relative_lateral_m"] = frame["relative_lateral_m"]
    if scenario_type == "cargo_drop":
        frame["payload_count"] = 1
        frame["payload_positions"] = [position]
        frame["payload_distance_to_ego_m"] = frame["distance_to_ego_m"]
    return frame


def attach_surround_cameras(carla, world, ego, blueprints, width, height, fov):
    from carla_smoke.scenes.safebench_scenic_scene import attach_surround_cameras as attach

    return attach(carla, world, ego, blueprints, width, height, fov)


def run_intervention(args):
    repo_root = repo_root_from_this_file()
    add_repo_paths(repo_root)
    check_scenic_python_deps()
    carla = import_carla_from_root(args.carla_root)

    import numpy as np
    from safebench.util.scenic_utils import ScenicSimulator

    random.seed(args.seed)
    np.random.seed(args.seed)

    config = read_json(args.scenario_config)
    l0_state = read_json(args.l0_json) if args.l0_json else {}
    scenario_type = scenario_type_from_config(config)
    trigger_frame = trigger_frame_from_config(config, args.trigger_frame)
    pre_roll_frames = args.pre_roll_frames if args.pre_roll_frames is not None else pre_roll_frame_from_config(config, 0)
    description = extract_scenario_description(args.scenic_file)

    params = {
        "address": args.host,
        "port": args.port,
        "timeout": args.timeout,
        "render": 0,
        "timestep": args.timestep,
    }
    if args.weather:
        params["weather"] = args.weather

    os.makedirs(args.output_dir, exist_ok=True)
    clean_output_dir(args.output_dir)
    write_json(os.path.join(args.output_dir, "camera_calibration.json"), camera_calibration_metadata(args.width, args.height, args.fov))

    scenic = None
    cameras = []
    camera_queues = None
    state = InterventionState()
    trace_frames = []
    snapshots = []

    try:
        print(f"SafeBench intervention Scenic file: {args.scenic_file}")
        print(f"L4 intervention type: {scenario_type}, pre_roll={pre_roll_frames}, trigger={trigger_frame}")
        scenic = ScenicSimulator(args.scenic_file, params)
        scene = None
        for attempt in range(1, args.scene_sample_attempts + 1):
            candidate_scene, iterations = scenic.generateScene()
            print(f"Generated Scenic scene attempt {attempt} after {iterations} rejection iterations.")
            if scenic.setSimulation(candidate_scene):
                scene = candidate_scene
                break
            scenic.endSimulation()
        if scene is None:
            raise RuntimeError(f"Failed to create a CARLA simulation after {args.scene_sample_attempts} Scenic samples.")

        simulation = scenic.simulation
        world = simulation.world
        blueprints = world.get_blueprint_library()
        ego = get_ego_actor(simulation)
        traffic_manager = simulation.tm
        traffic_manager.set_random_device_seed(args.seed)
        set_ego_autopilot(ego, traffic_manager, args.ego_speed_difference)
        update_behavior = prime_scenic_behavior(scenic)

        for frame_idx in range(pre_roll_frames):
            try:
                next(update_behavior)
            except StopIteration:
                raise RuntimeError(f"Scenic behavior ended during pre-roll at frame {frame_idx}.")
            world.tick()
            simulation.updateObjects()
            if frame_idx % 50 == 0:
                print(f"pre_roll_frame={frame_idx:04d}")

        cameras, camera_queues = attach_surround_cameras(carla, world, ego, blueprints, args.width, args.height, args.fov)
        for _ in range(args.warmup_ticks):
            world.tick()
            simulation.updateObjects()
            drain_camera_queues(camera_queues)

        target_actor = l0_primary_actor(config, l0_state)
        if scenario_type in {"front_vehicle_brake", "side_vehicle_intrusion"}:
            state.primary_actor = match_live_actor(world, ego, target_actor or {}, scenario_type)
            if state.primary_actor is None:
                raise RuntimeError(f"Could not match a live SafeBench actor for {scenario_type}.")
            state.reported_primary_id = (target_actor or {}).get("id") or state.primary_actor.id
            print(f"Matched primary actor: id={state.primary_actor.id} type={state.primary_actor.type_id}")
        else:
            prepare_generated_actor(carla, world, blueprints, ego, config, scenario_type, state)
            if state.primary_actor is None:
                raise RuntimeError(f"Could not prepare generated primary actor for {scenario_type}.")
            print(f"Prepared primary actor: id={state.primary_actor.id} type={state.primary_actor.type_id}")

        saved = 0
        for local_frame in range(args.frames):
            try:
                next(update_behavior)
            except StopIteration:
                print(f"Scenic behavior ended at local L4 frame {local_frame}.")
                break

            apply_intervention(carla, ego, config, scenario_type, state, local_frame, trigger_frame)
            world.tick()
            simulation.updateObjects()
            images = read_surround_images(camera_queues, timeout=5.0)
            frame_trace = trace_frame(ego, state.primary_actor, scenario_type, local_frame, state.reported_primary_id)
            trace_frames.append(frame_trace)

            if local_frame % args.save_every == 0:
                image_file, per_camera = save_surround_risk_images(images, args.output_dir, local_frame)
                snapshot = build_scene_snapshot(carla, world, ego, local_frame, image_file, args.state_radius)
                snapshot.setdefault("source", {})
                snapshot["source"].update(
                    {
                        "scenario_source": "safebench_intervention",
                        "safebench_scenic_file": os.path.relpath(args.scenic_file, repo_root),
                        "scenario_description": description,
                        "camera_mode": "surround",
                        "camera_images": per_camera,
                        "montage_layout": "2x3",
                        "pre_roll_frames": pre_roll_frames,
                    }
                )
                snapshots.append(snapshot)
                write_json(os.path.join(args.output_dir, f"state_{local_frame:04d}.json"), snapshot)
                saved += 1

            if local_frame % 20 == 0:
                print(
                    "frame={:04d} ego_speed={:.2f} primary_speed={:.2f} saved={}".format(
                        local_frame,
                        frame_trace.get("ego_speed_mps", 0.0),
                        frame_trace.get("primary_actor_speed_mps", 0.0),
                        saved,
                    )
                )

        trace = {
            "version": "safebench_intervention_trace_v1",
            "scenario_type": scenario_type,
            "intervention_backend": "safebench_replay_in_place",
            "event_applied": {
                "scenario_type": scenario_type,
                "trigger_frame": trigger_frame,
                "method": "replayed SafeBench scene and perturbed the matched live primary actor in-place",
            },
            "scene_relocated": False,
            "pre_roll_frames": pre_roll_frames,
            "trigger_frame": trigger_frame,
            "source_scenic_file": os.path.relpath(args.scenic_file, repo_root),
            "primary_actor_match": {
                "id": state.primary_actor.id if state.primary_actor else None,
                "type_id": state.primary_actor.type_id if state.primary_actor else None,
                "l0_actor_id": (target_actor or {}).get("id") if isinstance(target_actor, dict) else None,
                "l0_type_id": (target_actor or {}).get("type_id") if isinstance(target_actor, dict) else None,
            },
            "frames": trace_frames,
        }
        write_json(os.path.join(args.output_dir, "event_trace.json"), trace)
        print(f"Done. Saved {saved} SafeBench-intervention risk images: {os.path.abspath(args.output_dir)}")
        return 0

    finally:
        for _, camera in reversed(cameras):
            try:
                camera.stop()
            except Exception:
                pass
            try:
                camera.destroy()
            except Exception:
                pass
        for actor in reversed(state.spawned_actors):
            try:
                actor.destroy()
            except Exception:
                pass
        if scenic is not None:
            try:
                scenic.endSimulation()
            except Exception as exc:
                print(f"WARNING: Scenic simulation cleanup failed: {exc}", file=sys.stderr)
            try:
                scenic.destroy()
            except Exception as exc:
                print(f"WARNING: Scenic simulator cleanup failed: {exc}", file=sys.stderr)
        time.sleep(0.5)


def main():
    default_carla_root = os.environ.get("CARLA_ROOT", "/mnt/data2/congfeng/CARLA")
    parser = argparse.ArgumentParser(description="Replay SafeBench and apply L4 risk intervention in the live scenario.")
    parser.add_argument("--scenario-config", required=True)
    parser.add_argument("--l0-json", required=True)
    parser.add_argument("--carla-root", default=default_carla_root)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--scenic-file", required=True)
    parser.add_argument("--scene-sample-attempts", type=int, default=20)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--pre-roll-frames", type=int, default=None)
    parser.add_argument("--trigger-frame", type=int, default=20)
    parser.add_argument("--warmup-ticks", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timestep", type=float, default=0.05)
    parser.add_argument("--ego-speed-difference", type=float, default=-5.0)
    parser.add_argument("--weather", default="ClearNoon")
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--height", type=int, default=450)
    parser.add_argument("--fov", type=float, default=90.0)
    parser.add_argument("--state-radius", type=float, default=80.0)
    args = parser.parse_args()
    return run_intervention(args)


if __name__ == "__main__":
    raise SystemExit(main())
