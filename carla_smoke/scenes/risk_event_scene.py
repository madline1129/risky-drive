#!/usr/bin/env python3
"""Execute a simple CARLA risk event from a generated L4 scenario config."""

import argparse
import glob
import json
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


def first_blueprint(blueprints, patterns):
    for pattern in patterns:
        matches = list(blueprints.filter(pattern))
        if matches:
            return matches[0]
    raise RuntimeError(f"No blueprint found for patterns: {patterns}")


def select_prop_blueprint(blueprints, object_type):
    static_bps = [bp for bp in list(blueprints) if bp.id.startswith("static.prop")]
    keywords = {
        "metal_pipe": ["barrier", "construction", "streetbarrier", "warning"],
        "road_obstacle": ["barrier", "cone", "box", "garbage"],
    }.get(object_type, ["barrier", "construction", "cone", "box"])
    for keyword in keywords:
        matches = [bp for bp in static_bps if keyword in bp.id.lower()]
        if matches:
            return matches[0]
    if static_bps:
        return static_bps[0]
    raise RuntimeError("No static prop blueprint found.")


def cleanup_previous(world):
    actor_ids = []
    for actor in world.get_actors():
        role_name = actor.attributes.get("role_name", "")
        if role_name.startswith("risk_"):
            actor_ids.append(actor.id)
    for actor_id in actor_ids:
        actor = world.get_actor(actor_id)
        if actor:
            actor.destroy()


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


def transform_from_local(carla, base_transform, x, y, z, yaw_offset=0.0):
    forward = base_transform.get_forward_vector()
    right = base_transform.get_right_vector()
    location = base_transform.location + carla.Location(
        x=forward.x * x + right.x * y,
        y=forward.y * x + right.y * y,
        z=z,
    )
    rotation = carla.Rotation(
        pitch=base_transform.rotation.pitch,
        yaw=base_transform.rotation.yaw + yaw_offset,
        roll=base_transform.rotation.roll,
    )
    return carla.Transform(location, rotation)


def spawn_ego(carla, world, blueprints):
    ego_bp = first_blueprint(blueprints, ["vehicle.lincoln.*", "vehicle.tesla.model3", "vehicle.*"])
    if ego_bp.has_attribute("role_name"):
        ego_bp.set_attribute("role_name", "risk_ego")
    for transform in world.get_map().get_spawn_points():
        spawn_transform = carla.Transform(transform.location, transform.rotation)
        spawn_transform.location.z += 0.6
        ego = world.try_spawn_actor(ego_bp, spawn_transform)
        if ego is not None:
            return ego, spawn_transform
    raise RuntimeError("Failed to spawn ego vehicle.")


def spawn_front_truck(carla, world, blueprints, ego_transform, distance):
    truck_bp = first_blueprint(blueprints, ["vehicle.carlamotors.carlacola", "vehicle.carlamotors.*", "vehicle.*"])
    if truck_bp.has_attribute("role_name"):
        truck_bp.set_attribute("role_name", "risk_front_truck")
    for candidate in [distance, distance + 5, distance + 10, distance + 15]:
        transform = transform_ahead(carla, world, ego_transform, candidate)
        truck = world.try_spawn_actor(truck_bp, transform)
        if truck is not None:
            truck.set_autopilot(False)
            truck.apply_control(carla.VehicleControl(hand_brake=True))
            return truck, transform
    raise RuntimeError("Failed to spawn front truck.")


def spawn_payload(carla, world, blueprints, base_transform, config):
    object_type = config.get("object_type", "metal_pipe")
    count = int(config.get("object_count", 5))
    prop_bp = select_prop_blueprint(blueprints, object_type)
    if prop_bp.has_attribute("role_name"):
        prop_bp.set_attribute("role_name", "risk_payload")
    initial = config.get("initial_position", {"x": -3.2, "y": 0.0, "z": 2.4})
    actors = []
    offsets = [-0.8, 0.0, 0.8, -0.4, 0.4]
    for idx in range(count):
        transform = transform_from_local(
            carla,
            base_transform,
            float(initial.get("x", -3.2)) + 0.2 * idx,
            float(initial.get("y", 0.0)) + offsets[idx % len(offsets)],
            float(initial.get("z", 2.4)) + 0.05 * idx,
            yaw_offset=90.0,
        )
        actor = world.try_spawn_actor(prop_bp, transform)
        if actor is None:
            continue
        try:
            actor.set_simulate_physics(False)
            actor.set_enable_gravity(False)
        except RuntimeError:
            pass
        actors.append({"actor": actor, "x0": float(initial.get("x", -3.2)) + 0.2 * idx, "y0": float(initial.get("y", 0.0)) + offsets[idx % len(offsets)], "z0": float(initial.get("z", 2.4)) + 0.05 * idx})
    return actors


def animate_payload(carla, base_transform, payload, frame_idx, trigger_frame, motion):
    elapsed = max(0.0, (frame_idx - trigger_frame) * 0.05)
    back_speed = float(motion.get("back_speed_mps", 8.0))
    lateral_drift = float(motion.get("lateral_drift_mps", 0.2))
    for idx, item in enumerate(payload):
        x = item["x0"] - (back_speed + idx * 0.2) * elapsed
        y = item["y0"] + ((idx % 3) - 1) * lateral_drift * elapsed
        z = max(0.2, item["z0"] - 0.5 * 9.8 * elapsed * elapsed)
        transform = transform_from_local(carla, base_transform, x, y, z, yaw_offset=90.0 + 120.0 * elapsed)
        try:
            item["actor"].set_transform(transform)
        except RuntimeError:
            pass


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Execute a generated CARLA risk event and save front-camera images.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--town", default="Town03")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--frames", type=int, default=140)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--target-speed", type=float, default=5.0)
    args = parser.parse_args()

    config = load_config(args.config)
    plan = config.get("carla_plan", {})
    carla = import_carla(args.carla_root)
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
        cleanup_previous(world)
        world.tick()

        blueprints = world.get_blueprint_library()
        ego, ego_transform = spawn_ego(carla, world, blueprints)
        actors.append(ego)
        ego.set_autopilot(False)

        truck_distance = float(config.get("truck_distance", 18.0))
        truck, truck_transform = spawn_front_truck(carla, world, blueprints, ego_transform, truck_distance)
        actors.append(truck)

        payload = spawn_payload(carla, world, blueprints, truck_transform, plan)
        actors.extend(item["actor"] for item in payload)
        print(f"Risk scene payload actors: {len(payload)}")

        camera_bp = blueprints.find("sensor.camera.rgb")
        camera_bp.set_attribute("image_size_x", "800")
        camera_bp.set_attribute("image_size_y", "450")
        camera_bp.set_attribute("fov", "90")
        camera = world.spawn_actor(
            camera_bp,
            carla.Transform(carla.Location(x=1.5, z=2.4), carla.Rotation(pitch=-6.0)),
            attach_to=ego,
        )
        actors.append(camera)
        camera.listen(image_queue.put)

        trigger_frame = int(plan.get("trigger_frame", config.get("trigger_frame", 45)))
        motion = plan.get("motion", {})
        saved = 0
        for frame_idx in range(args.frames):
            velocity = ego.get_velocity()
            speed = (velocity.x * velocity.x + velocity.y * velocity.y + velocity.z * velocity.z) ** 0.5
            distance = ego.get_location().distance(truck.get_location())
            if distance < 6.0:
                control = carla.VehicleControl(throttle=0.0, brake=0.9)
            elif speed < args.target_speed:
                control = carla.VehicleControl(throttle=0.8, brake=0.0)
            else:
                control = carla.VehicleControl(throttle=0.0, brake=0.05)
            ego.apply_control(control)

            if frame_idx >= trigger_frame:
                animate_payload(carla, truck_transform, payload, frame_idx, trigger_frame, motion)

            world.tick()
            image = image_queue.get(timeout=5.0)
            if frame_idx % args.save_every == 0:
                image.save_to_disk(os.path.join(args.output_dir, f"risk_rgb_{frame_idx:04d}.png"))
                saved += 1
            if frame_idx % 20 == 0:
                print(f"frame={frame_idx:04d} distance={distance:.2f} saved={saved}")

        print(f"Done. Saved {saved} risk images: {os.path.abspath(args.output_dir)}")
        return 0
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
