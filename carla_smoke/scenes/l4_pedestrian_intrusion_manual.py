#!/usr/bin/env python3
"""Manual L4 CARLA scene for a pedestrian hidden by the front vehicle."""

import argparse
import glob
import json
import math
import os
import queue
import sys
import time


def add_carla_python_api(carla_root):
    candidates = [
        os.path.join(carla_root, "PythonAPI", "carla"),
        os.path.join(carla_root, "PythonAPI", "carla", "agents"),
    ]
    candidates.extend(glob.glob(os.path.join(carla_root, "PythonAPI", "carla", "dist", "carla-*.egg")))
    candidates.extend(glob.glob(os.path.join(carla_root, "PythonAPI", "carla", "dist", "carla-*.whl")))
    for path in candidates:
        if os.path.exists(path) and path not in sys.path:
            sys.path.insert(0, path)


def import_carla(carla_root):
    try:
        import carla
        return carla
    except ImportError:
        add_carla_python_api(carla_root)
        import carla
        return carla


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def vec_len(v):
    return math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)


def loc_dict(location):
    return {"x": round(location.x, 3), "y": round(location.y, 3), "z": round(location.z, 3)}


def first_blueprint(blueprints, patterns):
    for pattern in patterns:
        matches = list(blueprints.filter(pattern))
        if matches:
            return matches[0]
    raise RuntimeError(f"No blueprint found for patterns: {patterns}")


def exact_or_fallback_blueprint(blueprints, type_id, fallback_patterns):
    if type_id:
        try:
            return blueprints.find(type_id)
        except (IndexError, RuntimeError):
            print(f"WARNING: blueprint not found for {type_id}; using fallback.", file=sys.stderr)
    return first_blueprint(blueprints, fallback_patterns)


def transform_from_state(carla, state, z_lift=0.6):
    loc = state.get("location", {})
    rot = state.get("rotation", {})
    location = carla.Location(
        x=float(loc.get("x", 0.0)),
        y=float(loc.get("y", 0.0)),
        z=float(loc.get("z", 0.0)) + z_lift,
    )
    rotation = carla.Rotation(
        pitch=float(rot.get("pitch", 0.0)),
        yaw=float(rot.get("yaw", 0.0)),
        roll=float(rot.get("roll", 0.0)),
    )
    return carla.Transform(location, rotation)


def transform_from_local(carla, base_transform, x, y, z, yaw_offset=0.0):
    forward = base_transform.get_forward_vector()
    right = base_transform.get_right_vector()
    location = base_transform.location + carla.Location(
        x=forward.x * x + right.x * y,
        y=forward.y * x + right.y * y,
        z=z,
    )
    rotation = carla.Rotation(
        pitch=0.0,
        yaw=base_transform.rotation.yaw + yaw_offset,
        roll=0.0,
    )
    return carla.Transform(location, rotation)


def relative_to_ego(ego_transform, location):
    ego_location = ego_transform.location
    forward = ego_transform.get_forward_vector()
    right = ego_transform.get_right_vector()
    dx = location.x - ego_location.x
    dy = location.y - ego_location.y
    dz = location.z - ego_location.z
    return (
        dx * forward.x + dy * forward.y + dz * forward.z,
        dx * right.x + dy * right.y + dz * right.z,
    )


def try_spawn_at(world, blueprint, transform):
    for z_offset in [0.0, 0.3, 0.6, 1.0]:
        candidate = transform
        candidate.location.z = transform.location.z + z_offset
        actor = world.try_spawn_actor(blueprint, candidate)
        if actor is not None:
            return actor
    return None


def cleanup_previous(world):
    for actor in list(world.get_actors()):
        role_name = actor.attributes.get("role_name", "")
        if role_name.startswith("manual_l4_"):
            try:
                actor.destroy()
            except RuntimeError:
                pass


def apply_weather(carla, world, weather_data):
    if not weather_data:
        return
    weather = world.get_weather()
    for key, value in weather_data.items():
        if hasattr(weather, key) and value is not None:
            setattr(weather, key, float(value))
    world.set_weather(weather)


def attach_camera(carla, world, ego, blueprints, image_queue):
    camera_bp = blueprints.find("sensor.camera.rgb")
    camera_bp.set_attribute("image_size_x", "800")
    camera_bp.set_attribute("image_size_y", "450")
    camera_bp.set_attribute("fov", "90")
    camera = world.spawn_actor(
        camera_bp,
        carla.Transform(carla.Location(x=1.5, z=2.4), carla.Rotation(pitch=-6.0)),
        attach_to=ego,
    )
    camera.listen(image_queue.put)
    return camera


def vehicle_control_to_speed(carla, vehicle, target_speed):
    speed = vec_len(vehicle.get_velocity())
    if speed < target_speed:
        return carla.VehicleControl(throttle=0.45, brake=0.0)
    return carla.VehicleControl(throttle=0.0, brake=0.08)


def main():
    parser = argparse.ArgumentParser(description="Manual CARLA L4 pedestrian intrusion scene.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--town", default=None)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--frames", type=int, default=140)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--ego-target-speed", type=float, default=1.45)
    parser.add_argument("--crossing-width", type=float, default=3.8)
    args = parser.parse_args()

    config = read_json(args.config)
    plan = config.get("carla_plan", {})
    scene = config.get("scene_reconstruction", {})
    ego_state = scene.get("ego", {})
    front_state = scene.get("nearest_front_actor", {})
    trigger_frame = int(plan.get("trigger_frame", config.get("trigger_frame", 90)))
    crossing_speed = float(plan.get("speed_mps", 1.5))

    preferred_town = scene.get("preferred_town") or os.path.basename(str(scene.get("source_map", ""))) or None
    town = args.town or preferred_town or "Town03"

    carla = import_carla(args.carla_root)
    os.makedirs(args.output_dir, exist_ok=True)
    for path in glob.glob(os.path.join(args.output_dir, "risk_rgb_*.png")):
        os.remove(path)

    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)
    world = None
    original_settings = None
    actors = []
    image_queue = queue.Queue()
    trace_frames = []

    try:
        world = client.load_world(town)
        original_settings = world.get_settings()
        settings = world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = 0.05
        world.apply_settings(settings)
        apply_weather(carla, world, scene.get("weather", {}))
        cleanup_previous(world)
        world.tick()

        blueprints = world.get_blueprint_library()

        ego_bp = exact_or_fallback_blueprint(blueprints, ego_state.get("type_id"), ["vehicle.lincoln.mkz_2020", "vehicle.*"])
        if ego_bp.has_attribute("role_name"):
            ego_bp.set_attribute("role_name", "manual_l4_ego")
        ego_transform = transform_from_state(carla, ego_state, z_lift=0.65)
        ego = try_spawn_at(world, ego_bp, ego_transform)
        if ego is None:
            raise RuntimeError("Failed to spawn ego at L0 transform.")
        actors.append(ego)
        ego.set_autopilot(False)

        front_bp = exact_or_fallback_blueprint(
            blueprints,
            front_state.get("type_id"),
            ["vehicle.nissan.patrol", "vehicle.carlamotors.carlacola", "vehicle.*"],
        )
        if front_bp.has_attribute("role_name"):
            front_bp.set_attribute("role_name", "manual_l4_front_vehicle")
        front_transform = transform_from_state(carla, front_state, z_lift=0.65)
        front_vehicle = try_spawn_at(world, front_bp, front_transform)
        if front_vehicle is None:
            # If exact L0 transform collides, keep the same local scene relation and place it ahead of ego.
            front_vehicle = try_spawn_at(
                world,
                front_bp,
                transform_from_local(carla, ego.get_transform(), 9.5, 0.25, 0.65),
            )
        if front_vehicle is None:
            raise RuntimeError("Failed to spawn front vehicle.")
        actors.append(front_vehicle)
        front_vehicle.set_autopilot(False)
        front_vehicle.apply_control(carla.VehicleControl(throttle=0.0, brake=1.0, hand_brake=True))

        walker_bp = first_blueprint(blueprints, ["walker.pedestrian.*"])
        if walker_bp.has_attribute("role_name"):
            walker_bp.set_attribute("role_name", "manual_l4_pedestrian")

        # Place the pedestrian just in front of the lead vehicle, initially slightly to the left.
        # This makes the lead vehicle occlude the pedestrian until the crossing starts.
        half_width = float(args.crossing_width) / 2.0
        start_local_x = float(plan.get("start_position", {}).get("x", 5.0))
        start_local_y = -half_width if plan.get("crossing_direction", "left_to_right") == "left_to_right" else half_width
        end_local_y = -start_local_y
        walker_transform = transform_from_local(carla, front_vehicle.get_transform(), start_local_x, start_local_y, 0.25, yaw_offset=90.0)
        walker = try_spawn_at(world, walker_bp, walker_transform)
        if walker is None:
            raise RuntimeError("Failed to spawn pedestrian.")
        actors.append(walker)

        camera = attach_camera(carla, world, ego, blueprints, image_queue)
        actors.append(camera)

        saved = 0
        for frame_idx in range(args.frames):
            ego.apply_control(vehicle_control_to_speed(carla, ego, args.ego_target_speed))
            front_vehicle.apply_control(carla.VehicleControl(throttle=0.0, brake=1.0, hand_brake=True))

            if frame_idx >= trigger_frame:
                elapsed = (frame_idx - trigger_frame) * 0.05
                direction = 1.0 if end_local_y > start_local_y else -1.0
                current_y = start_local_y + direction * min(abs(end_local_y - start_local_y), crossing_speed * elapsed)
                walker_transform = transform_from_local(
                    carla,
                    front_vehicle.get_transform(),
                    start_local_x,
                    current_y,
                    0.25,
                    yaw_offset=90.0 if direction > 0 else -90.0,
                )
                walker.set_transform(walker_transform)

            world.tick()
            image = image_queue.get(timeout=5.0)
            if frame_idx % args.save_every == 0:
                image.save_to_disk(os.path.join(args.output_dir, f"risk_rgb_{frame_idx:04d}.png"))
                saved += 1

            ego_transform_now = ego.get_transform()
            walker_location = walker.get_location()
            front_distance = ego.get_location().distance(front_vehicle.get_location())
            walker_distance = ego.get_location().distance(walker_location)
            rel_long, rel_lat = relative_to_ego(ego_transform_now, walker_location)
            trace_frames.append(
                {
                    "frame": frame_idx,
                    "triggered": frame_idx >= trigger_frame,
                    "ego_speed_mps": round(vec_len(ego.get_velocity()), 3),
                    "front_distance_m": round(front_distance, 3),
                    "vulnerable_actor_position": loc_dict(walker_location),
                    "distance_to_ego_m": round(walker_distance, 3),
                    "relative_longitudinal_m": round(rel_long, 3),
                    "relative_lateral_m": round(rel_lat, 3),
                }
            )

            if frame_idx % 20 == 0:
                print(
                    f"frame={frame_idx:04d} ego_speed={trace_frames[-1]['ego_speed_mps']:.2f} "
                    f"front_dist={front_distance:.2f} ped_dist={walker_distance:.2f} "
                    f"ped_lat={rel_lat:.2f} saved={saved}"
                )

        trace = {
            "scenario_type": "vulnerable_actor_intrusion",
            "trigger_frame": trigger_frame,
            "event_applied": "pedestrian emerges from in front of the lead vehicle and crosses the ego lane",
            "success_hint": "After trigger, relative_lateral_m should move across zero while ego remains moving.",
            "frames": trace_frames,
        }
        write_json(os.path.join(args.output_dir, "event_trace.json"), trace)
        print(f"Done. Saved {saved} risk images and event_trace.json: {os.path.abspath(args.output_dir)}")
        return 0
    finally:
        for actor in reversed(actors):
            try:
                if hasattr(actor, "stop"):
                    actor.stop()
            except Exception:
                pass
            try:
                if actor.is_alive:
                    actor.destroy()
            except RuntimeError:
                pass
        if world is not None and original_settings is not None:
            world.apply_settings(original_settings)
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
