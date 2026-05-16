#!/usr/bin/env python3
"""Approximate a truck cargo/rebar-drop risk scene in CARLA.

The default CARLA asset library usually does not include a true steel rebar
blueprint. This script therefore selects the closest available construction or
barrier prop and drops several copies from a truck bed into the ego lane.
It saves RGB camera frames from the ego vehicle.
"""

import argparse
import glob
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
        matches = list(blueprints.filter(pattern))
        if matches:
            return matches[0]
    raise RuntimeError(f"No blueprint found for patterns: {patterns}")


def select_cargo_blueprint(blueprints):
    """Pick a prop to stand in for steel rods/debris."""
    preferred_keywords = [
        "barrier",
        "construction",
        "warning",
        "cone",
        "box",
        "garbage",
        "static.prop",
    ]
    all_bps = list(blueprints)
    static_bps = [bp for bp in all_bps if bp.id.startswith("static.prop")]
    for keyword in preferred_keywords:
        matches = [bp for bp in static_bps if keyword in bp.id.lower()]
        if matches:
            return matches[0]
    if static_bps:
        return static_bps[0]
    raise RuntimeError("No static prop blueprint found for cargo/debris.")


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


def spawn_vehicle_at_available_point(carla, world, blueprint, spawn_points, role_name):
    if blueprint.has_attribute("role_name"):
        blueprint.set_attribute("role_name", role_name)
    for transform in spawn_points[:60]:
        spawn_transform = carla.Transform(transform.location, transform.rotation)
        spawn_transform.location.z += 0.6
        actor = world.try_spawn_actor(blueprint, spawn_transform)
        if actor is not None:
            return actor, spawn_transform
    raise RuntimeError("Could not spawn ego vehicle at any tested spawn point.")


def waypoint_ahead(carla, world, base_transform, distance):
    waypoint = world.get_map().get_waypoint(
        base_transform.location,
        project_to_road=True,
        lane_type=carla.LaneType.Driving,
    )
    next_wps = waypoint.next(distance)
    if next_wps:
        return next_wps[0].transform
    forward = base_transform.get_forward_vector()
    return carla.Transform(
        base_transform.location + carla.Location(x=forward.x * distance, y=forward.y * distance, z=0.6),
        base_transform.rotation,
    )


def spawn_truck(carla, world, blueprints, ego_transform, actors, distances):
    truck_bp = first_blueprint(
        blueprints,
        ["vehicle.carlamotors.carlacola", "vehicle.carlamotors.*", "vehicle.tesla.model3", "vehicle.*"],
    )
    if truck_bp.has_attribute("role_name"):
        truck_bp.set_attribute("role_name", "smoke_cargo_truck")

    for distance in distances:
        transform = waypoint_ahead(carla, world, ego_transform, distance)
        transform.location.z += 0.6
        truck = world.try_spawn_actor(truck_bp, transform)
        if truck is not None:
            actors.append(truck)
            truck.set_autopilot(False)
            truck.apply_control(carla.VehicleControl(hand_brake=True))
            return truck, transform
    raise RuntimeError("Could not spawn cargo truck ahead of ego.")


def transform_from_local(carla, base_transform, x, y, z, yaw_offset=0.0):
    forward = base_transform.get_forward_vector()
    right = base_transform.get_right_vector()
    loc = base_transform.location + carla.Location(
        x=forward.x * x + right.x * y,
        y=forward.y * x + right.y * y,
        z=z,
    )
    rot = carla.Rotation(
        pitch=base_transform.rotation.pitch,
        yaw=base_transform.rotation.yaw + yaw_offset,
        roll=base_transform.rotation.roll,
    )
    return carla.Transform(loc, rot)


def spawn_cargo_props(carla, world, cargo_bp, truck_transform, actors, count, cargo_start_x):
    props = []
    lateral_offsets = [-1.2, -0.4, 0.4, 1.2, 0.0, -0.8, 0.8]
    for idx in range(count):
        y = lateral_offsets[idx % len(lateral_offsets)]
        x = cargo_start_x + 0.18 * idx
        transform = transform_from_local(carla, truck_transform, x=x, y=y, z=2.4 + 0.08 * idx, yaw_offset=90.0)
        prop = world.try_spawn_actor(cargo_bp, transform)
        if prop is None:
            continue
        actors.append(prop)
        props.append((prop, x, y, 2.4 + 0.08 * idx))
        try:
            prop.set_simulate_physics(False)
            prop.set_enable_gravity(False)
        except RuntimeError:
            pass
    if not props:
        raise RuntimeError(f"Could not spawn any cargo props from blueprint {cargo_bp.id}.")
    return props


def release_cargo(carla, truck_transform, cargo_props):
    forward = truck_transform.get_forward_vector()
    right = truck_transform.get_right_vector()
    for idx, (prop, _, _, _) in enumerate(cargo_props):
        try:
            prop.set_enable_gravity(True)
            prop.set_simulate_physics(True)
            prop.add_impulse(
                carla.Vector3D(
                    x=-forward.x * (320.0 + 35.0 * idx) + right.x * ((idx % 3) - 1) * 80.0,
                    y=-forward.y * (320.0 + 35.0 * idx) + right.y * ((idx % 3) - 1) * 80.0,
                    z=80.0,
                )
            )
            prop.add_angular_impulse(carla.Vector3D(x=0.0, y=0.0, z=120.0 + idx * 30.0))
        except RuntimeError:
            pass


def scripted_drop(carla, truck_transform, cargo_props, release_frame, frame_idx, debris_back_speed):
    """Fallback visual motion if the selected prop has weak physics support."""
    t = (frame_idx - release_frame) * 0.05
    if t < 0:
        return
    for idx, (prop, x0, y0, z0) in enumerate(cargo_props):
        backward_speed = debris_back_speed + idx * 0.35
        side_speed = ((idx % 3) - 1) * 0.8
        drop = 0.5 * 9.8 * t * t
        x = x0 - backward_speed * t
        y = y0 + side_speed * t
        z = max(0.25, z0 - drop)
        transform = transform_from_local(carla, truck_transform, x=x, y=y, z=z, yaw_offset=90.0 + 140.0 * t)
        try:
            prop.set_transform(transform)
        except RuntimeError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Create an approximate cargo/rebar drop scene and save images.")
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--town", default="Town03")
    parser.add_argument("--output-dir", default="carla_smoke/outputs/rebar_drop")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--cargo-count", type=int, default=5)
    parser.add_argument("--release-frame", type=int, default=12)
    parser.add_argument("--truck-distance", type=float, default=22.0)
    parser.add_argument("--ego-throttle", type=float, default=0.45)
    parser.add_argument("--brake-frame-offset", type=int, default=45)
    parser.add_argument("--cargo-start-x", type=float, default=-3.2, help="Cargo initial longitudinal offset from truck center; negative is behind truck.")
    parser.add_argument("--debris-back-speed", type=float, default=10.0, help="Scripted debris speed toward ego, in m/s.")
    parser.add_argument("--scripted-drop", action="store_true", help="Animate debris manually after release.")
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
        destroy_previous_smoke_actors(world)
        world.tick()

        blueprints = world.get_blueprint_library()
        ego_bp = first_blueprint(blueprints, ["vehicle.lincoln.*", "vehicle.tesla.model3", "vehicle.*"])
        cargo_bp = select_cargo_blueprint(blueprints)
        print(f"Cargo/debris blueprint: {cargo_bp.id}")

        spawn_points = world.get_map().get_spawn_points()
        if not spawn_points:
            raise RuntimeError("Current map has no vehicle spawn points.")

        ego, ego_transform = spawn_vehicle_at_available_point(carla, world, ego_bp, spawn_points, "smoke_hero")
        actors.append(ego)
        ego.set_autopilot(False)

        truck_distances = [
            args.truck_distance,
            args.truck_distance + 6.0,
            args.truck_distance + 12.0,
            max(12.0, args.truck_distance - 5.0),
            args.truck_distance + 20.0,
        ]
        truck, truck_transform = spawn_truck(carla, world, blueprints, ego_transform, actors, truck_distances)
        actual_distance = ego_transform.location.distance(truck_transform.location)
        print(f"Actual ego-to-truck spawn distance: {actual_distance:.2f} m")
        cargo_props = spawn_cargo_props(
            carla,
            world,
            cargo_bp,
            truck_transform,
            actors,
            args.cargo_count,
            args.cargo_start_x,
        )

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

        collision_bp = blueprints.find("sensor.other.collision")
        collision_sensor = world.spawn_actor(collision_bp, carla.Transform(), attach_to=ego)
        actors.append(collision_sensor)
        collisions = []
        collision_sensor.listen(lambda event: collisions.append(event))

        spectator = world.get_spectator()
        spectator.set_transform(
            carla.Transform(
                ego_transform.location + carla.Location(z=40.0),
                carla.Rotation(pitch=-90.0, yaw=ego_transform.rotation.yaw),
            )
        )

        print("Scene spawned:")
        print(f"  ego: {ego.id} {ego.type_id}")
        print(f"  truck: {truck.id} {truck.type_id}")
        print(f"  cargo props: {len(cargo_props)}")
        print(f"  release frame: {args.release_frame}")
        print(f"  output: {os.path.abspath(args.output_dir)}")

        released = False
        saved = 0
        for frame_idx in range(args.frames):
            if frame_idx < args.release_frame + args.brake_frame_offset:
                ego.apply_control(carla.VehicleControl(throttle=args.ego_throttle, steer=0.0))
            else:
                ego.apply_control(carla.VehicleControl(throttle=0.0, brake=0.65))

            if frame_idx == args.release_frame:
                print("Releasing cargo/debris now.")
                release_cargo(carla, truck_transform, cargo_props)
                released = True

            if released and args.scripted_drop:
                scripted_drop(
                    carla,
                    truck_transform,
                    cargo_props,
                    args.release_frame,
                    frame_idx,
                    args.debris_back_speed,
                )

            world.tick()
            image = image_queue.get(timeout=5.0)
            if frame_idx % args.save_every == 0:
                image.save_to_disk(os.path.join(args.output_dir, f"rgb_{frame_idx:04d}.png"))
                saved += 1

        print(f"Done. Saved {saved} images.")
        print(f"Ego collisions recorded: {len(collisions)}")

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
