#!/usr/bin/env python3
"""Generate a normal CARLA driving sequence with traffic and save ego-camera frames."""

import argparse
import csv
import glob
import os
import queue
import random
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


def cleanup_previous(carla, client, world):
    actor_ids = []
    for actor in world.get_actors():
        role_name = actor.attributes.get("role_name", "")
        if role_name.startswith("normal_"):
            actor_ids.append(actor.id)
    if actor_ids:
        client.apply_batch_sync([carla.command.DestroyActor(actor_id) for actor_id in actor_ids], True)
        print(f"Destroyed {len(actor_ids)} previous normal-scene actors.")


def choose_vehicle_blueprint(blueprints, rng):
    vehicle_bps = [bp for bp in blueprints.filter("vehicle.*") if int(bp.get_attribute("number_of_wheels")) == 4]
    bp = rng.choice(vehicle_bps)
    if bp.has_attribute("color"):
        colors = bp.get_attribute("color").recommended_values
        if colors:
            bp.set_attribute("color", rng.choice(colors))
    return bp


def spawn_ego(carla, world, blueprints, spawn_points, rng):
    ego_bp = blueprints.find("vehicle.lincoln.mkz_2020")
    if ego_bp.has_attribute("role_name"):
        ego_bp.set_attribute("role_name", "normal_ego")
    if ego_bp.has_attribute("color"):
        ego_bp.set_attribute("color", "0,0,0")

    for transform in spawn_points:
        spawn_transform = carla.Transform(transform.location, transform.rotation)
        spawn_transform.location.z += 0.6
        ego = world.try_spawn_actor(ego_bp, spawn_transform)
        if ego is not None:
            return ego

    rng.shuffle(spawn_points)
    for transform in spawn_points:
        ego = world.try_spawn_actor(ego_bp, transform)
        if ego is not None:
            return ego
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


def spawn_lead_vehicle(carla, world, traffic_manager, blueprints, ego, distance, speed_difference, rng):
    lead_bp = choose_vehicle_blueprint(blueprints, rng)
    if lead_bp.has_attribute("role_name"):
        lead_bp.set_attribute("role_name", "normal_lead_vehicle")
    if lead_bp.has_attribute("color"):
        lead_bp.set_attribute("color", "255,0,0")

    ego_transform = ego.get_transform()
    for candidate_distance in [distance, distance + 4.0, distance + 8.0, distance + 12.0]:
        lead_transform = transform_ahead(carla, world, ego_transform, candidate_distance)
        planned_distance = ego_transform.location.distance(lead_transform.location)
        if planned_distance < max(3.0, candidate_distance * 0.5):
            forward = ego_transform.get_forward_vector()
            lead_transform = carla.Transform(
                ego_transform.location
                + carla.Location(
                    x=forward.x * candidate_distance,
                    y=forward.y * candidate_distance,
                    z=0.6,
                ),
                ego_transform.rotation,
            )
            planned_distance = ego_transform.location.distance(lead_transform.location)

        lead = world.try_spawn_actor(lead_bp, lead_transform)
        if lead is None:
            continue
        lead.set_autopilot(True, traffic_manager.get_port())
        traffic_manager.vehicle_percentage_speed_difference(lead, speed_difference)
        print(f"Lead vehicle spawned at requested {candidate_distance:.1f} m, planned {planned_distance:.2f} m.")
        return lead

    print("WARNING: failed to spawn explicit lead vehicle; continuing with random traffic only.")
    return None


def spawn_npc_vehicles(carla, client, world, traffic_manager, blueprints, spawn_points, ego, count, rng):
    command = carla.command
    ego_location = ego.get_location()
    candidate_points = [
        transform
        for transform in spawn_points
        if transform.location.distance(ego_location) > 18.0
    ]
    rng.shuffle(candidate_points)

    batch = []
    for transform in candidate_points[:count]:
        bp = choose_vehicle_blueprint(blueprints, rng)
        if bp.has_attribute("role_name"):
            bp.set_attribute("role_name", "normal_npc")
        batch.append(command.SpawnActor(bp, transform).then(command.SetAutopilot(command.FutureActor, True, traffic_manager.get_port())))

    actors = []
    for response in client.apply_batch_sync(batch, True):
        if response.error:
            continue
        actor = world.get_actor(response.actor_id)
        if actor:
            actors.append(actor)
    return actors


def attach_front_camera(carla, world, ego, blueprints, image_queue, width, height, fov):
    camera_bp = blueprints.find("sensor.camera.rgb")
    camera_bp.set_attribute("image_size_x", str(width))
    camera_bp.set_attribute("image_size_y", str(height))
    camera_bp.set_attribute("fov", str(fov))
    camera = world.spawn_actor(
        camera_bp,
        carla.Transform(carla.Location(x=1.5, z=2.4), carla.Rotation(pitch=-6.0)),
        attach_to=ego,
    )
    camera.listen(image_queue.put)
    return camera


def save_ego_log(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["frame", "x", "y", "z", "yaw", "speed_mps"])
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Generate a normal driving scene with CARLA Traffic Manager.")
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--tm-port", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--town", default="Town03")
    parser.add_argument("--output-dir", default="carla_smoke/outputs/normal_driving")
    parser.add_argument("--frames", type=int, default=160)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--vehicles", type=int, default=30)
    parser.add_argument("--lead-distance", type=float, default=14.0, help="Distance in meters for an explicit vehicle ahead of ego.")
    parser.add_argument(
        "--lead-speed-difference",
        type=float,
        default=35.0,
        help="Traffic Manager speed difference for the lead vehicle; positive means slower than the speed limit.",
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--height", type=int, default=450)
    parser.add_argument("--fov", type=float, default=90.0)
    parser.add_argument("--clean-output", action="store_true", help="Remove old rgb_*.png and ego_log.csv in output dir.")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    carla = import_carla(args.carla_root)
    print(f"CARLA module: {carla.__file__}")

    os.makedirs(args.output_dir, exist_ok=True)
    if args.clean_output:
        for path in glob.glob(os.path.join(args.output_dir, "rgb_*.png")):
            os.remove(path)
        log_path = os.path.join(args.output_dir, "ego_log.csv")
        if os.path.exists(log_path):
            os.remove(log_path)

    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)
    world = client.load_world(args.town) if args.town else client.get_world()
    traffic_manager = client.get_trafficmanager(args.tm_port)
    traffic_manager.set_random_device_seed(args.seed)
    traffic_manager.set_global_distance_to_leading_vehicle(3.0)
    traffic_manager.global_percentage_speed_difference(10.0)

    original_settings = world.get_settings()
    actors = []
    actor_ids = []
    camera = None
    image_queue = queue.Queue()
    log_rows = []

    try:
        settings = world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = 0.05
        world.apply_settings(settings)
        traffic_manager.set_synchronous_mode(True)

        cleanup_previous(carla, client, world)
        world.tick()

        blueprints = world.get_blueprint_library()
        spawn_points = world.get_map().get_spawn_points()
        rng.shuffle(spawn_points)

        ego = spawn_ego(carla, world, blueprints, spawn_points, rng)
        actors.append(ego)
        actor_ids.append(ego.id)
        ego.set_autopilot(True, traffic_manager.get_port())
        traffic_manager.vehicle_percentage_speed_difference(ego, -5.0)
        world.tick()

        lead = spawn_lead_vehicle(
            carla,
            world,
            traffic_manager,
            blueprints,
            ego,
            args.lead_distance,
            args.lead_speed_difference,
            rng,
        )
        if lead is not None:
            actors.append(lead)
            actor_ids.append(lead.id)

        npcs = spawn_npc_vehicles(carla, client, world, traffic_manager, blueprints, spawn_points, ego, args.vehicles, rng)
        actors.extend(npcs)
        actor_ids.extend(actor.id for actor in npcs)
        print(f"Spawned ego, {1 if lead is not None else 0} lead vehicle, and {len(npcs)} NPC vehicles.")

        camera = attach_front_camera(carla, world, ego, blueprints, image_queue, args.width, args.height, args.fov)
        actors.append(camera)
        actor_ids.append(camera.id)

        spectator = world.get_spectator()
        saved = 0
        for frame_idx in range(args.frames):
            world.tick()
            image = image_queue.get(timeout=5.0)

            ego_transform = ego.get_transform()
            velocity = ego.get_velocity()
            speed = (velocity.x * velocity.x + velocity.y * velocity.y + velocity.z * velocity.z) ** 0.5
            log_rows.append(
                [
                    frame_idx,
                    f"{ego_transform.location.x:.3f}",
                    f"{ego_transform.location.y:.3f}",
                    f"{ego_transform.location.z:.3f}",
                    f"{ego_transform.rotation.yaw:.3f}",
                    f"{speed:.3f}",
                ]
            )

            spectator.set_transform(
                carla.Transform(
                    ego_transform.location + carla.Location(z=35.0),
                    carla.Rotation(pitch=-90.0, yaw=ego_transform.rotation.yaw),
                )
            )

            if frame_idx % args.save_every == 0:
                image.save_to_disk(os.path.join(args.output_dir, f"rgb_{frame_idx:04d}.png"))
                saved += 1

            if frame_idx % 20 == 0:
                print(f"frame={frame_idx:04d} ego_speed={speed:.2f} m/s saved={saved}")

        log_path = os.path.join(args.output_dir, "ego_log.csv")
        save_ego_log(log_path, log_rows)
        print(f"Done. Saved {saved} images and ego log: {os.path.abspath(log_path)}")
        return 0

    finally:
        if camera is not None:
            try:
                camera.stop()
            except Exception:
                pass
        if actor_ids:
            try:
                destroy_commands = [carla.command.DestroyActor(actor_id) for actor_id in reversed(dict.fromkeys(actor_ids))]
                client.apply_batch_sync(destroy_commands, True)
            except Exception as exc:
                print(f"WARNING: actor cleanup failed: {exc}", file=sys.stderr)
        traffic_manager.set_synchronous_mode(False)
        world.apply_settings(original_settings)
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
