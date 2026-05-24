#!/usr/bin/env python3
"""Capture a SafeBench Scenic scenario into the carla_smoke L0 image format."""

import argparse
import csv
import glob
import json
import os
import queue
import random
import re
import shutil
import struct
import sys
import time
import zlib


def check_scenic_python_deps():
    missing = []
    for module_name, package_name in [
        ("dotmap", "dotmap~=1.3"),
        ("antlr4", "antlr4-python3-runtime~=4.11"),
        ("mapbox_earcut", "mapbox_earcut>=0.12.10"),
    ]:
        try:
            __import__(module_name)
        except ImportError:
            missing.append(package_name)

    if missing:
        raise RuntimeError(
            "Missing Scenic Python dependencies: "
            + ", ".join(missing)
            + ". Install Scenic dependencies with: cd Scenic && python -m pip install -e ."
        )

    try:
        import decorator
    except ImportError as exc:
        raise RuntimeError(
            "Missing Python package 'decorator'. Install the ChatScene/Scenic dependency with: "
            "pip install decorator==5.1.1"
        ) from exc

    try:
        import inspect

        signature = inspect.signature(decorator.decorate)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Could not inspect decorator.decorate; reinstall with: pip install decorator==5.1.1") from exc

    if "kwsyntax" not in signature.parameters:
        version = getattr(decorator, "__version__", "unknown")
        raise RuntimeError(
            "Incompatible 'decorator' package for Scenic: "
            f"version {version!r} has no decorate(..., kwsyntax=...) support. "
            "Fix the chatscene environment with: pip install decorator==5.1.1"
        )


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def add_repo_paths(repo_root):
    scenic_src = os.path.join(repo_root, "Scenic", "src")
    for path in [repo_root, scenic_src]:
        if os.path.exists(path) and path not in sys.path:
            sys.path.insert(0, path)


def carla_python_api_candidates(carla_root):
    py_tag = f"py{sys.version_info.major}.{sys.version_info.minor}"
    cp_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    candidates = [
        os.path.join(carla_root, "PythonAPI", "carla"),
        os.path.join(carla_root, "PythonAPI", "carla", "agents"),
    ]
    dist_candidates = []
    dist_candidates.extend(glob.glob(os.path.join(carla_root, "PythonAPI", "carla", "dist", "carla-*.egg")))
    dist_candidates.extend(glob.glob(os.path.join(carla_root, "PythonAPI", "carla", "dist", "carla-*.whl")))
    compatible_dist = [
        path
        for path in sorted(dist_candidates)
        if py_tag in os.path.basename(path) or cp_tag in os.path.basename(path)
    ]
    candidates.extend(compatible_dist)
    return candidates


def add_carla_python_api(carla_root):
    candidates = carla_python_api_candidates(carla_root)
    for path in candidates:
        if os.path.exists(path) and path not in sys.path:
            sys.path.insert(0, path)
    return candidates


def import_carla_from_root(carla_root):
    try:
        import carla

        return carla
    except ImportError:
        pass

    candidates = add_carla_python_api(carla_root)
    try:
        import carla

        return carla
    except ImportError as exc:
        dist_glob = os.path.join(carla_root, "PythonAPI", "carla", "dist", "carla-*")
        all_dist = sorted(glob.glob(dist_glob))
        existing = [path for path in candidates if os.path.exists(path)]
        message = [
            "Could not import the CARLA Python API before loading Scenic.",
            f"carla_root: {carla_root}",
            f"current Python: {sys.version_info.major}.{sys.version_info.minor}",
            "Existing CARLA PythonAPI candidates:",
        ]
        if existing:
            message.extend(f"  - {path}" for path in existing)
        else:
            message.append("  - none found")
        if all_dist:
            message.append("All CARLA dist files found under this root:")
            message.extend(f"  - {path}" for path in all_dist)
        message.extend(
            [
                "Fix by using a CARLA PythonAPI build compatible with the current Python version.",
                "For example, Python 3.8 needs a py3.8/cp38 CARLA egg/whl; Python 3.7 needs py3.7/cp37.",
                "If this CARLA install only has py3.7/cp37 files, run this pipeline from a Python 3.7 conda env or install/build a cp38 CARLA PythonAPI.",
            ]
        )
        raise RuntimeError("\n".join(message)) from exc


def natural_key(path):
    name = os.path.basename(path)
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def list_scenic_files(scenic_dir):
    files = glob.glob(os.path.join(scenic_dir, "*.scenic"))
    files = [path for path in files if ".ipynb_checkpoints" not in path]
    return sorted(files, key=natural_key)


def choose_scenic_file(args):
    if args.scenic_file:
        return os.path.abspath(args.scenic_file)
    scenic_dir = os.path.abspath(args.scenic_dir)
    files = list_scenic_files(scenic_dir)
    if not files:
        raise FileNotFoundError(f"No .scenic files found under {scenic_dir}")
    if args.scenario_index < 0 or args.scenario_index >= len(files):
        raise ValueError(f"--scenario-index {args.scenario_index} out of range; available: 0..{len(files) - 1}")
    return files[args.scenario_index]


def extract_scenario_description(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read(1200).lstrip()
    for quote in ("'''", '"""'):
        if text.startswith(quote):
            end = text.find(quote, len(quote))
            if end > len(quote):
                return " ".join(text[len(quote):end].strip().split())
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    return first_line[:300]


CAMERA_SPECS = [
    ("CAM_FRONT", {"x": 1.5, "y": 0.0, "z": 1.6, "pitch": 0.0, "yaw": 0.0, "roll": 0.0}),
    ("CAM_FRONT_LEFT", {"x": 1.2, "y": -0.4, "z": 1.6, "pitch": 0.0, "yaw": -55.0, "roll": 0.0}),
    ("CAM_FRONT_RIGHT", {"x": 1.2, "y": 0.4, "z": 1.6, "pitch": 0.0, "yaw": 55.0, "roll": 0.0}),
    ("CAM_BACK", {"x": -1.5, "y": 0.0, "z": 1.6, "pitch": 0.0, "yaw": 180.0, "roll": 0.0}),
    ("CAM_BACK_LEFT", {"x": -1.2, "y": -0.4, "z": 1.6, "pitch": 0.0, "yaw": -125.0, "roll": 0.0}),
    ("CAM_BACK_RIGHT", {"x": -1.2, "y": 0.4, "z": 1.6, "pitch": 0.0, "yaw": 125.0, "roll": 0.0}),
]


def clean_output_dir(output_dir):
    for pattern in [
        "rgb_*.png",
        "state_*.json",
        "scene_states.jsonl",
        "ego_log.csv",
        "safebench_scene.json",
        "camera_calibration.json",
    ]:
        for path in glob.glob(os.path.join(output_dir, pattern)):
            os.remove(path)
    for camera_name, _ in CAMERA_SPECS:
        camera_dir = os.path.join(output_dir, camera_name)
        if os.path.isdir(camera_dir):
            shutil.rmtree(camera_dir)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def save_ego_log(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["frame", "x", "y", "z", "yaw", "speed_mps"])
        writer.writerows(rows)


def save_scene_states(path, snapshots):
    with open(path, "w", encoding="utf-8") as f:
        for snapshot in snapshots:
            f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def vector_length(vector):
    return (vector.x * vector.x + vector.y * vector.y + vector.z * vector.z) ** 0.5


def import_capture_helpers(repo_root):
    add_repo_paths(repo_root)
    from carla_smoke.scenes.normal_driving_scene import attach_front_camera, build_scene_snapshot

    return attach_front_camera, build_scene_snapshot


def attach_surround_cameras(carla, world, ego, blueprints, width, height, fov):
    cameras = []
    queues = {}
    camera_bp = blueprints.find("sensor.camera.rgb")
    camera_bp.set_attribute("image_size_x", str(width))
    camera_bp.set_attribute("image_size_y", str(height))
    camera_bp.set_attribute("fov", str(fov))

    for camera_name, spec in CAMERA_SPECS:
        transform = carla.Transform(
            carla.Location(x=spec["x"], y=spec["y"], z=spec["z"]),
            carla.Rotation(pitch=spec["pitch"], yaw=spec["yaw"], roll=spec["roll"]),
        )
        image_queue = queue.Queue()
        camera = world.spawn_actor(camera_bp, transform, attach_to=ego)
        camera.listen(lambda image, q=image_queue: q.put(image))
        cameras.append((camera_name, camera))
        queues[camera_name] = image_queue
    return cameras, queues


def drain_camera_queues(camera_queues):
    for image_queue in camera_queues.values():
        while not image_queue.empty():
            image_queue.get_nowait()


def read_surround_images(camera_queues, timeout=5.0):
    return {camera_name: image_queue.get(timeout=timeout) for camera_name, image_queue in camera_queues.items()}


def carla_image_to_rgb(image):
    import numpy as np

    array = np.frombuffer(image.raw_data, dtype=np.uint8)
    array = array.reshape((image.height, image.width, 4))
    return array[:, :, [2, 1, 0]].copy()


def write_rgb_png(path, rgb_array):
    height, width, channels = rgb_array.shape
    if channels != 3:
        raise ValueError("write_rgb_png expects an RGB array with 3 channels.")

    def chunk(chunk_type, data):
        payload = chunk_type + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    raw_rows = b"".join(b"\x00" + rgb_array[row].tobytes() for row in range(height))
    png = [
        b"\x89PNG\r\n\x1a\n",
        chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
        chunk(b"IDAT", zlib.compress(raw_rows, level=6)),
        chunk(b"IEND", b""),
    ]
    with open(path, "wb") as f:
        f.write(b"".join(png))


def save_surround_images_and_montage(images, output_dir, frame_idx):
    import numpy as np

    per_camera_files = {}
    tiles = []
    for camera_name, _ in CAMERA_SPECS:
        image = images[camera_name]
        camera_dir = os.path.join(output_dir, camera_name)
        os.makedirs(camera_dir, exist_ok=True)
        image_file = f"rgb_{frame_idx:04d}.png"
        image_path = os.path.join(camera_dir, image_file)
        image.save_to_disk(image_path)
        per_camera_files[camera_name] = os.path.join(camera_name, image_file)
        tiles.append(carla_image_to_rgb(image))

    top = np.hstack(tiles[:3])
    bottom = np.hstack(tiles[3:])
    montage = np.vstack([top, bottom])
    montage_file = f"rgb_{frame_idx:04d}.png"
    write_rgb_png(os.path.join(output_dir, montage_file), montage)
    return montage_file, per_camera_files


def camera_calibration_metadata(width, height, fov):
    return {
        "camera_model": "carla_surround_rgb",
        "layout": "2x3",
        "tile_order": [camera_name for camera_name, _ in CAMERA_SPECS],
        "image_width": width,
        "image_height": height,
        "fov": fov,
        "cameras": {
            camera_name: {
                "location": {"x": spec["x"], "y": spec["y"], "z": spec["z"]},
                "rotation": {"pitch": spec["pitch"], "yaw": spec["yaw"], "roll": spec["roll"]},
            }
            for camera_name, spec in CAMERA_SPECS
        },
    }


def get_ego_actor(simulation):
    ego_object = getattr(simulation, "ego", None)
    if ego_object is not None and getattr(ego_object, "carlaActor", None) is not None:
        return ego_object.carlaActor
    if simulation.objects and getattr(simulation.objects[0], "carlaActor", None) is not None:
        return simulation.objects[0].carlaActor
    raise RuntimeError("SafeBench/Scenic simulation did not expose an ego CARLA actor.")


def prime_scenic_behavior(scenic):
    update_behavior = scenic.runSimulation()
    next(update_behavior)
    return update_behavior


def set_ego_autopilot(ego, traffic_manager, target_speed_diff):
    if not hasattr(ego, "set_autopilot"):
        return
    ego.set_autopilot(True, traffic_manager.get_port())
    traffic_manager.vehicle_percentage_speed_difference(ego, target_speed_diff)


def scenic_file_candidates(args):
    if args.scenic_file:
        return [(os.path.abspath(args.scenic_file), None)]
    scenic_dir = os.path.abspath(args.scenic_dir)
    files = list_scenic_files(scenic_dir)
    if not files:
        raise FileNotFoundError(f"No .scenic files found under {scenic_dir}")
    if args.scenario_index < 0 or args.scenario_index >= len(files):
        raise ValueError(f"--scenario-index {args.scenario_index} out of range; available: 0..{len(files) - 1}")
    if not args.try_next_on_failure:
        return [(files[args.scenario_index], args.scenario_index)]
    return [(path, index) for index, path in enumerate(files[args.scenario_index:], start=args.scenario_index)]


def capture_one_safebench_scene(
    args,
    scenic_file,
    scenario_index,
    repo_root,
    carla,
    ScenicSimulator,
    attach_front_camera,
    build_scene_snapshot,
    params,
):
    description = extract_scenario_description(scenic_file)
    os.makedirs(args.output_dir, exist_ok=True)

    scenic = None
    cameras = []
    image_queue = queue.Queue()
    camera_queues = None
    log_rows = []
    state_snapshots = []

    try:
        print(f"SafeBench Scenic file: {scenic_file}")
        scenic = ScenicSimulator(scenic_file, params)

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

        if args.camera_mode == "surround":
            cameras, camera_queues = attach_surround_cameras(carla, world, ego, blueprints, args.width, args.height, args.fov)
            write_json(os.path.join(args.output_dir, "camera_calibration.json"), camera_calibration_metadata(args.width, args.height, args.fov))
        else:
            camera = attach_front_camera(
                carla,
                world,
                ego,
                blueprints,
                image_queue,
                args.width,
                args.height,
                args.fov,
            )
            cameras = [("CAM_FRONT", camera)]

        update_behavior = prime_scenic_behavior(scenic)
        for _ in range(args.warmup_ticks):
            world.tick()
            simulation.updateObjects()
            if camera_queues is not None:
                drain_camera_queues(camera_queues)
            else:
                while not image_queue.empty():
                    image_queue.get_nowait()

        target_frame = None
        if args.single_random_frame:
            target_frame = random.SystemRandom().randrange(max(1, int(args.frames)))
            print(f"single_random_frame target_frame={target_frame:04d}")

        saved = 0
        for frame_idx in range(args.frames):
            try:
                next(update_behavior)
            except StopIteration:
                print(f"Scenic behavior ended at frame {frame_idx}.")
                break

            world.tick()
            simulation.updateObjects()
            if camera_queues is not None:
                surround_images = read_surround_images(camera_queues, timeout=5.0)
            else:
                image = image_queue.get(timeout=5.0)

            ego_transform = ego.get_transform()
            speed = vector_length(ego.get_velocity())
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

            should_save = False if args.no_save_images else (frame_idx == target_frame if target_frame is not None else frame_idx % args.save_every == 0)
            if should_save:
                per_camera_files = None
                if camera_queues is not None:
                    image_file, per_camera_files = save_surround_images_and_montage(surround_images, args.output_dir, frame_idx)
                else:
                    image_file = f"rgb_{frame_idx:04d}.png"
                    image.save_to_disk(os.path.join(args.output_dir, image_file))
                snapshot = build_scene_snapshot(carla, world, ego, frame_idx, image_file, args.state_radius)
                snapshot.setdefault("source", {})
                snapshot["source"].update(
                    {
                        "scenario_source": "safebench_scenic",
                        "safebench_scenic_file": os.path.relpath(scenic_file, repo_root),
                        "safebench_scenario_index": scenario_index,
                        "scenario_description": description,
                        "camera_mode": args.camera_mode,
                    }
                )
                if per_camera_files:
                    snapshot["source"]["camera_images"] = per_camera_files
                    snapshot["source"]["montage_layout"] = "2x3"
                state_snapshots.append(snapshot)
                write_json(os.path.join(args.output_dir, f"state_{frame_idx:04d}.json"), snapshot)
                saved += 1
                if target_frame is not None:
                    break
            elif args.no_save_images and frame_idx == 0:
                snapshot = build_scene_snapshot(carla, world, ego, frame_idx, None, args.state_radius)
                snapshot.setdefault("source", {})
                snapshot["source"].update(
                    {
                        "scenario_source": "safebench_scenic",
                        "safebench_scenic_file": os.path.relpath(scenic_file, repo_root),
                        "safebench_scenario_index": scenario_index,
                        "scenario_description": description,
                        "camera_mode": args.camera_mode,
                    }
                )
                state_snapshots.append(snapshot)
                write_json(os.path.join(args.output_dir, f"state_{frame_idx:04d}.json"), snapshot)

            if frame_idx % 20 == 0:
                print(f"frame={frame_idx:04d} ego_speed={speed:.2f} m/s saved={saved}")

        save_ego_log(os.path.join(args.output_dir, "ego_log.csv"), log_rows)
        save_scene_states(os.path.join(args.output_dir, "scene_states.jsonl"), state_snapshots)
        write_json(
            os.path.join(args.output_dir, "safebench_scene.json"),
            {
                "scenario_source": "safebench_scenic",
                "scenic_file": os.path.relpath(scenic_file, repo_root),
                "scenario_index": scenario_index,
                "description": description,
                "frames": args.frames,
                "save_every": args.save_every,
                "saved_images": saved,
                "capture_mode": "single_random_frame" if target_frame is not None else "sampled_sequence",
                "selected_frame": target_frame,
                "camera_mode": args.camera_mode,
                "camera_layout": "2x3_surround" if args.camera_mode == "surround" else "front",
                "params": params,
            },
        )
        print(f"Done. Saved {saved} SafeBench-derived images and L0 states: {os.path.abspath(args.output_dir)}")
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


def capture_safebench_scene(args):
    repo_root = repo_root_from_this_file()
    add_repo_paths(repo_root)
    check_scenic_python_deps()
    carla = import_carla_from_root(args.carla_root)

    import numpy as np
    from safebench.util.scenic_utils import ScenicSimulator

    attach_front_camera, build_scene_snapshot = import_capture_helpers(repo_root)

    params = {
        "address": args.host,
        "port": args.port,
        "timeout": args.timeout,
        "render": 0,
        "timestep": args.timestep,
    }
    if args.weather:
        params["weather"] = args.weather

    candidates = scenic_file_candidates(args)
    failures = []
    for attempt_index, (scenic_file, scenario_index) in enumerate(candidates):
        random.seed(args.seed + attempt_index)
        np.random.seed(args.seed + attempt_index)
        if args.clean_output or attempt_index > 0:
            clean_output_dir(args.output_dir)
        try:
            return capture_one_safebench_scene(
                args,
                scenic_file,
                scenario_index,
                repo_root,
                carla,
                ScenicSimulator,
                attach_front_camera,
                build_scene_snapshot,
                params,
            )
        except Exception as exc:
            failures.append((scenic_file, repr(exc)))
            if args.scenic_file or not args.try_next_on_failure:
                raise
            print(f"WARNING: SafeBench Scenic sample failed, trying next file: {scenic_file}", file=sys.stderr)
            print(f"WARNING: failure was: {exc}", file=sys.stderr)

    failure_lines = ["No SafeBench Scenic candidate could be captured."]
    failure_lines.extend(f"  - {path}: {error}" for path, error in failures)
    raise RuntimeError("\n".join(failure_lines))


def main():
    repo_root = repo_root_from_this_file()
    default_carla_root = os.environ.get("CARLA_ROOT", "/mnt/data2/congfeng/carla915")
    default_scenic_dir = os.path.join(
        repo_root,
        "safebench",
        "scenario",
        "scenario_data",
        "scenic_data",
        "dynamic_scenario",
    )

    parser = argparse.ArgumentParser(description="Run one SafeBench Scenic scenario and save carla_smoke-compatible frames.")
    parser.add_argument("--carla-root", default=default_carla_root)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--scenic-dir", default=default_scenic_dir)
    parser.add_argument("--scenic-file", default=None)
    parser.add_argument("--scenario-index", type=int, default=0)
    parser.add_argument("--scene-sample-attempts", type=int, default=20)
    parser.add_argument("--output-dir", default="carla_smoke/outputs/safebench_scenic")
    parser.add_argument("--frames", type=int, default=160)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--warmup-ticks", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timestep", type=float, default=0.05)
    parser.add_argument("--ego-speed-difference", type=float, default=-5.0)
    parser.add_argument("--weather", default="ClearNoon")
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--height", type=int, default=450)
    parser.add_argument("--fov", type=float, default=90.0)
    parser.add_argument("--camera-mode", choices=["front", "surround"], default="surround")
    parser.add_argument("--state-radius", type=float, default=80.0)
    parser.add_argument("--single-random-frame", action="store_true", help="Save exactly one randomly selected source frame.")
    parser.add_argument("--no-save-images", action="store_true", help="Run CARLA/Scenic and write state snapshots without image files.")
    parser.add_argument("--clean-output", action="store_true")
    parser.set_defaults(try_next_on_failure=True)
    parser.add_argument(
        "--try-next-on-failure",
        dest="try_next_on_failure",
        action="store_true",
        help="If a selected .scenic file fails during sampling/simulation, try following files in the same directory.",
    )
    parser.add_argument(
        "--no-try-next-on-failure",
        dest="try_next_on_failure",
        action="store_false",
        help="Fail immediately instead of trying following .scenic files.",
    )
    args = parser.parse_args()
    return capture_safebench_scene(args)


if __name__ == "__main__":
    raise SystemExit(main())
