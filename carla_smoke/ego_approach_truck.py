#!/usr/bin/env python3
"""Spawn an ego vehicle slowly approaching a stopped truck in CARLA."""

import argparse
import glob
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

    added = []
    for path in candidates:
        if os.path.exists(path) and path not in sys.path:
            sys.path.insert(0, path)
            added.append(path)
    return added


def import_carla(carla_root):
    try:
        import carla
        return carla
    except ImportError:
        add_carla_python_api(carla_root)
        import carla
        return carla


def first_blueprint(blueprints, patterns):
    for pattern in patterns:
        matches = list(blueprints.filter(pattern))
        if matches:
            return matches[0]
    raise RuntimeError(f"No blueprint found for patterns: {patterns}")


def cleanup_previous(world):
    removed = 0
    for actor in world.get_actors():
        role_name = actor.attributes.get("role_name", "")
        if role_name.startswith("approach_"):
            actor.destroy()
            removed += 1
    if removed:
        print(f"Destroyed {removed} previous approach actors.")


def spawn_ego(carla, world, blueprint, max_tries=80):
    if blueprint.has_attribute("role_name"):
        blueprint.set_attribute("role_name", "approach_ego")

    for transform in world.get_map().get_spawn_points()[:max_tries]:
        spawn_transform = carla.Transform(transform.location, transform.rotation)
        spawn_transform.location.z += 0.6
        ego = world.try_spawn_actor(blueprint, spawn_transform)
        if ego is not None:
            return ego, spawn_transform
    raise RuntimeError("Failed to spawn ego vehicle.")


def transform_ahead(carla, world, base_transform, distance):
    waypoint = world.get_map().get_waypoint(
        base_transform.location,
        project_to_road=True,
        lane_type=carla.LaneType.Driving,
    )
    next_waypoints = waypoint.next(distance)
    if next_waypoints:
        transform = next_waypoints[0].transform
        transform.location.z += 0.6
        return transform

    forward = base_transform.get_forward_vector()
    return carla.Transform(
        base_transform.location + carla.Location(x=forward.x * distance, y=forward.y * distance, z=0.6),
        base_transform.rotation,
    )


def spawn_truck(carla, world, blueprint, ego_transform, requested_distance):
    if blueprint.has_attribute("role_name"):
        blueprint.set_attribute("role_name", "approach_truck")

    for distance in [requested_distance, requested_distance + 5.0, requested_distance + 10.0, requested_distance + 15.0]:
        truck_transform = transform_ahead(carla, world, ego_transform, distance)
        truck = world.try_spawn_actor(blueprint, truck_transform)
        if truck is not None:
            actual = ego_transform.location.distance(truck_transform.location)
            print(f"Truck spawned at requested {distance:.1f} m, actual {actual:.2f} m.")
            return truck, truck_transform
    raise RuntimeError("Failed to spawn stopped truck ahead of ego.")


def save_distance_log(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write("frame,distance_m,ego_speed_mps\n")
        for frame, distance, speed in rows:
            f.write(f"{frame},{distance:.3f},{speed:.3f}\n")


def main():
    parser = argparse.ArgumentParser(description="Ego slowly approaches a stopped truck and saves camera images.")
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--town", default="Town03")
    parser.add_argument("--output-dir", default="carla_smoke/output_approach_truck")
    parser.add_argument("--truck-distance", type=float, default=25.0)
    parser.add_argument("--target-speed", type=float, default=4.0, help="Approximate ego speed in m/s.")
    parser.add_argument("--frames", type=int, default=160)
    parser.add_argument("--save-every", type=int, default=5)
    args = parser.parse_args()

    carla = import_carla(args.carla_root)
    print(f"CARLA module: {carla.__file__}")

    os.makedirs(args.output_dir, exist_ok=True)
    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)
    world = client.load_world(args.town) if args.town else client.get_world()

    original_settings = world.get_settings()
    actors = []
    image_queue = queue.Queue()
    distance_rows = []

    try:
        settings = world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = 0.05
        world.apply_settings(settings)

        cleanup_previous(world)
        world.tick()

        blueprints = world.get_blueprint_library()
        ego_bp = first_blueprint(blueprints, ["vehicle.lincoln.*", "vehicle.tesla.model3", "vehicle.*"])
        truck_bp = first_blueprint(blueprints, ["vehicle.carlamotors.carlacola", "vehicle.carlamotors.*", "vehicle.*"])

        ego, ego_transform = spawn_ego(carla, world, ego_bp)
        actors.append(ego)
        ego.set_autopilot(False)

        truck, truck_transform = spawn_truck(carla, world, truck_bp, ego_transform, args.truck_distance)
        actors.append(truck)
        truck.set_autopilot(False)
        truck.apply_control(carla.VehicleControl(hand_brake=True))

        camera_bp = blueprints.find("sensor.camera.rgb")
        camera_bp.set_attribute("image_size_x", "1280")
        camera_bp.set_attribute("image_size_y", "720")
        camera_bp.set_attribute("fov", "90")
        camera = world.spawn_actor(
            camera_bp,
            carla.Transform(carla.Location(x=1.5, z=2.4), carla.Rotation(pitch=-6.0)),
            attach_to=ego,
        )
        actors.append(camera)
        camera.listen(image_queue.put)

        spectator = world.get_spectator()
        spectator.set_transform(
            carla.Transform(
                ego_transform.location + carla.Location(z=35.0),
                carla.Rotation(pitch=-90.0, yaw=ego_transform.rotation.yaw),
            )
        )

        print("Scene ready: ego slowly approaches a stopped truck.")
        print(f"Output directory: {os.path.abspath(args.output_dir)}")

        saved = 0
        for frame_idx in range(args.frames):
            velocity = ego.get_velocity()
            speed = (velocity.x * velocity.x + velocity.y * velocity.y + velocity.z * velocity.z) ** 0.5
            distance = ego.get_location().distance(truck.get_location())

            if distance < 7.0:
                control = carla.VehicleControl(throttle=0.0, brake=0.8)
            elif speed < args.target_speed:
                control = carla.VehicleControl(throttle=0.28, brake=0.0)
            else:
                control = carla.VehicleControl(throttle=0.0, brake=0.08)
            ego.apply_control(control)

            world.tick()
            image = image_queue.get(timeout=5.0)

            velocity = ego.get_velocity()
            speed = (velocity.x * velocity.x + velocity.y * velocity.y + velocity.z * velocity.z) ** 0.5
            distance = ego.get_location().distance(truck.get_location())
            distance_rows.append((frame_idx, distance, speed))

            if frame_idx % args.save_every == 0:
                image.save_to_disk(os.path.join(args.output_dir, f"rgb_{frame_idx:04d}.png"))
                saved += 1

            if frame_idx % 10 == 0:
                print(f"frame={frame_idx:04d} distance={distance:.2f} m speed={speed:.2f} m/s")

        log_path = os.path.join(args.output_dir, "distance_log.csv")
        save_distance_log(log_path, distance_rows)
        print(f"Done. Saved {saved} images and distance log: {log_path}")

    finally:
        for actor in reversed(actors):
            try:
                if actor.is_alive:
                    actor.destroy()
            except RuntimeError:
                pass
        world.apply_settings(original_settings)
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
