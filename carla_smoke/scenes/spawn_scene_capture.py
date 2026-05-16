#!/usr/bin/env python3
"""Spawn a small CARLA scene and save RGB camera images.

Start CARLA first, for example:
    /mnt/data2/congfeng/carla915/CarlaUE4.sh -RenderOffScreen -nosound -carla-port=2000

Then run:
    python carla_smoke/scenes/spawn_scene_capture.py --port 2000 --town Town03
"""

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
        return carla, []
    except ImportError as first_error:
        added_paths = add_carla_python_api(carla_root)
        try:
            import carla
            return carla, added_paths
        except ImportError:
            print("ERROR: failed to import carla.")
            print(f"Added paths: {added_paths}")
            print(first_error)
            raise


def first_blueprint(blueprints, patterns):
    for pattern in patterns:
        matches = blueprints.filter(pattern)
        if matches:
            return matches[0]
    raise RuntimeError(f"No blueprint found for patterns: {patterns}")


def spawn_vehicle(world, blueprint, transform, role_name):
    if blueprint.has_attribute("role_name"):
        blueprint.set_attribute("role_name", role_name)
    actor = world.try_spawn_actor(blueprint, transform)
    if actor is None:
        raise RuntimeError(f"Failed to spawn {role_name} at {transform}")
    return actor


def destroy_previous_smoke_actors(world):
    old_actors = []
    for actor in world.get_actors():
        role_name = actor.attributes.get("role_name", "")
        if role_name.startswith("smoke_"):
            old_actors.append(actor)
    for actor in old_actors:
        try:
            actor.destroy()
        except RuntimeError:
            pass
    if old_actors:
        print(f"Destroyed {len(old_actors)} previous smoke actors.")


def spawn_first_available_vehicle(carla, world, blueprint, spawn_points, role_name, max_tries=40):
    if blueprint.has_attribute("role_name"):
        blueprint.set_attribute("role_name", role_name)
    for transform in spawn_points[:max_tries]:
        spawn_transform = carla.Transform(transform.location, transform.rotation)
        spawn_transform.location.z += 0.6
        actor = world.try_spawn_actor(blueprint, spawn_transform)
        if actor is not None:
            return actor, spawn_transform
    raise RuntimeError(f"Failed to spawn {role_name} after trying {min(max_tries, len(spawn_points))} spawn points.")


def try_spawn_vehicle(carla, world, blueprint, transform, role_name):
    if blueprint.has_attribute("role_name"):
        blueprint.set_attribute("role_name", role_name)
    spawn_transform = world.get_map().get_waypoint(
        transform.location,
        project_to_road=True,
        lane_type=carla.LaneType.Driving,
    ).transform
    spawn_transform.location.z += 0.6
    return world.try_spawn_actor(blueprint, spawn_transform)


def make_front_transform(carla, world, ego_transform, distance=28.0):
    carla_map = world.get_map()
    waypoint = carla_map.get_waypoint(
        ego_transform.location,
        project_to_road=True,
        lane_type=carla.LaneType.Driving,
    )
    next_waypoints = waypoint.next(distance)
    if next_waypoints:
        return next_waypoints[0].transform

    forward = ego_transform.get_forward_vector()
    return carla.Transform(
        ego_transform.location + carla.Location(
            x=forward.x * distance,
            y=forward.y * distance,
            z=0.5,
        ),
        ego_transform.rotation,
    )


def try_spawn_walker(carla, world, ego_transform, actors):
    blueprints = world.get_blueprint_library()
    walker_bp = first_blueprint(blueprints, ["walker.pedestrian.*"])
    forward = ego_transform.get_forward_vector()
    right = ego_transform.get_right_vector()
    walker_location = ego_transform.location + carla.Location(
        x=forward.x * 18.0 + right.x * 4.0,
        y=forward.y * 18.0 + right.y * 4.0,
        z=1.0,
    )
    walker_transform = carla.Transform(walker_location, ego_transform.rotation)
    walker = world.try_spawn_actor(walker_bp, walker_transform)
    if walker is not None:
        actors.append(walker)
        return walker
    return None


def main():
    parser = argparse.ArgumentParser(description="Spawn a CARLA scene and save camera frames.")
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--town", default=None, help="Optional map to load, e.g. Town03.")
    parser.add_argument("--output-dir", default="carla_smoke/outputs/scene")
    parser.add_argument("--frames", type=int, default=80)
    parser.add_argument("--save-every", type=int, default=10)
    args = parser.parse_args()

    carla, added_paths = import_carla(args.carla_root)
    print(f"CARLA module: {carla.__file__}")
    if added_paths:
        print(f"Added CARLA PythonAPI paths: {added_paths}")

    os.makedirs(args.output_dir, exist_ok=True)

    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)
    world = client.load_world(args.town) if args.town else client.get_world()

    original_settings = world.get_settings()
    actors = []
    image_queue = queue.Queue()

    try:
        settings = world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = 0.05
        world.apply_settings(settings)

        blueprints = world.get_blueprint_library()
        spawn_points = world.get_map().get_spawn_points()
        if not spawn_points:
            raise RuntimeError("Current map has no vehicle spawn points.")

        destroy_previous_smoke_actors(world)
        world.tick()

        ego_bp = first_blueprint(blueprints, ["vehicle.lincoln.*", "vehicle.tesla.model3"])
        front_bp = first_blueprint(blueprints, ["vehicle.tesla.model3", "vehicle.audi.*", "vehicle.*"])

        ego, ego_transform = spawn_first_available_vehicle(carla, world, ego_bp, spawn_points, "smoke_hero")
        actors.append(ego)
        ego.set_autopilot(False)

        front_vehicle = None
        for distance in [35.0, 45.0, 55.0, 25.0, 65.0]:
            front_transform = make_front_transform(carla, world, ego_transform, distance=distance)
            front_vehicle = try_spawn_vehicle(carla, world, front_bp, front_transform, "smoke_front_blocker")
            if front_vehicle is not None:
                break
        if front_vehicle is None:
            raise RuntimeError("Failed to spawn front_blocker after trying multiple distances.")
        actors.append(front_vehicle)
        front_vehicle.set_autopilot(False)
        front_vehicle.apply_control(carla.VehicleControl(hand_brake=True))

        walker = try_spawn_walker(carla, world, ego_transform, actors)

        camera_bp = blueprints.find("sensor.camera.rgb")
        camera_bp.set_attribute("image_size_x", "1280")
        camera_bp.set_attribute("image_size_y", "720")
        camera_bp.set_attribute("fov", "90")
        camera_transform = carla.Transform(
            carla.Location(x=1.5, z=2.4),
            carla.Rotation(pitch=-6.0),
        )
        camera = world.spawn_actor(camera_bp, camera_transform, attach_to=ego)
        actors.append(camera)
        camera.listen(image_queue.put)

        spectator = world.get_spectator()
        spectator.set_transform(
            carla.Transform(
                ego_transform.location + carla.Location(z=35.0),
                carla.Rotation(pitch=-90.0, yaw=ego_transform.rotation.yaw),
            )
        )

        print("Scene spawned:")
        print(f"  ego id: {ego.id}, type: {ego.type_id}")
        print(f"  front vehicle id: {front_vehicle.id}, type: {front_vehicle.type_id}")
        print(f"  walker id: {walker.id if walker else 'not spawned'}")
        print(f"Saving camera frames to: {os.path.abspath(args.output_dir)}")

        saved = 0
        for frame_idx in range(args.frames):
            if frame_idx < 25:
                ego.apply_control(carla.VehicleControl(throttle=0.30, steer=0.0))
            else:
                ego.apply_control(carla.VehicleControl(throttle=0.0, brake=0.7))

            world.tick()
            image = image_queue.get(timeout=5.0)
            if frame_idx % args.save_every == 0:
                filename = os.path.join(args.output_dir, f"rgb_{frame_idx:04d}.png")
                image.save_to_disk(filename)
                saved += 1

        print(f"Done. Saved {saved} PNG images.")

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
