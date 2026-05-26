#!/usr/bin/env python3
"""Run a Scenic file directly in CARLA and save camera images.

This runner does not call SafeBench. It uses Scenic's CARLA simulator directly,
attaches RGB cameras to the first Scenic/CARLA actor as ego, advances the Scenic
dynamic scenario frame by frame, and writes PNG images.
"""

from __future__ import annotations

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
from collections import OrderedDict
from pathlib import Path
from typing import Any


CAMERA_SPECS = [
    ("CAM_FRONT", {"x": 1.5, "y": 0.0, "z": 1.6, "pitch": 0.0, "yaw": 0.0, "roll": 0.0}),
    ("CAM_FRONT_LEFT", {"x": 1.2, "y": -0.4, "z": 1.6, "pitch": 0.0, "yaw": -55.0, "roll": 0.0}),
    ("CAM_FRONT_RIGHT", {"x": 1.2, "y": 0.4, "z": 1.6, "pitch": 0.0, "yaw": 55.0, "roll": 0.0}),
    ("CAM_BACK", {"x": -1.5, "y": 0.0, "z": 1.6, "pitch": 0.0, "yaw": 180.0, "roll": 0.0}),
    ("CAM_BACK_LEFT", {"x": -1.2, "y": -0.4, "z": 1.6, "pitch": 0.0, "yaw": -125.0, "roll": 0.0}),
    ("CAM_BACK_RIGHT", {"x": -1.2, "y": 0.4, "z": 1.6, "pitch": 0.0, "yaw": 125.0, "roll": 0.0}),
]


def repo_root_from_this_file() -> Path:
    return Path(__file__).resolve().parents[2]


def add_repo_paths(repo_root: Path) -> None:
    for path in [repo_root, repo_root / "Scenic" / "src"]:
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def carla_python_api_candidates(carla_root: Path) -> list[Path]:
    py_tag = f"py{sys.version_info.major}.{sys.version_info.minor}"
    cp_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    candidates = [
        carla_root / "PythonAPI" / "carla",
        carla_root / "PythonAPI" / "carla" / "agents",
    ]
    dist_files = []
    dist_files.extend(glob.glob(str(carla_root / "PythonAPI" / "carla" / "dist" / "carla-*.egg")))
    dist_files.extend(glob.glob(str(carla_root / "PythonAPI" / "carla" / "dist" / "carla-*.whl")))
    candidates.extend(Path(path) for path in sorted(dist_files) if py_tag in Path(path).name or cp_tag in Path(path).name)
    return candidates


def import_carla(carla_root: Path):
    for path in carla_python_api_candidates(carla_root):
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    try:
        import carla

        return carla
    except ImportError as exc:
        raise RuntimeError(
            "Could not import CARLA Python API. Check --carla-root and use a Python "
            "version matching the CARLA egg/whl under PythonAPI/carla/dist."
        ) from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def clean_output_dir(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    for pattern in ["rgb_*.png", "ego_log.csv", "run_metadata.json"]:
        for path in output_dir.glob(pattern):
            path.unlink()
    for camera_name, _ in CAMERA_SPECS:
        camera_dir = output_dir / camera_name
        if camera_dir.is_dir():
            shutil.rmtree(camera_dir)


def natural_key(path: Path):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name)]


def vector_length(vector) -> float:
    return (vector.x * vector.x + vector.y * vector.y + vector.z * vector.z) ** 0.5


def attach_front_camera(carla, world, ego, width: int, height: int, fov: float):
    image_queue: queue.Queue = queue.Queue()
    camera_bp = world.get_blueprint_library().find("sensor.camera.rgb")
    camera_bp.set_attribute("image_size_x", str(width))
    camera_bp.set_attribute("image_size_y", str(height))
    camera_bp.set_attribute("fov", str(fov))
    transform = carla.Transform(carla.Location(x=1.5, z=2.4), carla.Rotation(pitch=-6.0))
    camera = world.spawn_actor(camera_bp, transform, attach_to=ego)
    camera.listen(image_queue.put)
    return [("CAM_FRONT", camera)], {"CAM_FRONT": image_queue}


def attach_surround_cameras(carla, world, ego, width: int, height: int, fov: float):
    cameras = []
    queues = {}
    camera_bp = world.get_blueprint_library().find("sensor.camera.rgb")
    camera_bp.set_attribute("image_size_x", str(width))
    camera_bp.set_attribute("image_size_y", str(height))
    camera_bp.set_attribute("fov", str(fov))
    for camera_name, spec in CAMERA_SPECS:
        transform = carla.Transform(
            carla.Location(x=spec["x"], y=spec["y"], z=spec["z"]),
            carla.Rotation(pitch=spec["pitch"], yaw=spec["yaw"], roll=spec["roll"]),
        )
        image_queue: queue.Queue = queue.Queue()
        camera = world.spawn_actor(camera_bp, transform, attach_to=ego)
        camera.listen(lambda image, q=image_queue: q.put(image))
        cameras.append((camera_name, camera))
        queues[camera_name] = image_queue
    return cameras, queues


def drain_queues(camera_queues: dict[str, queue.Queue]) -> None:
    for image_queue in camera_queues.values():
        while not image_queue.empty():
            image_queue.get_nowait()


def read_images(camera_queues: dict[str, queue.Queue], timeout: float = 5.0):
    return {name: image_queue.get(timeout=timeout) for name, image_queue in camera_queues.items()}


def carla_image_to_rgb(image):
    import numpy as np

    array = np.frombuffer(image.raw_data, dtype=np.uint8)
    array = array.reshape((image.height, image.width, 4))
    return array[:, :, [2, 1, 0]].copy()


def write_rgb_png(path: Path, rgb_array) -> None:
    height, width, channels = rgb_array.shape
    if channels != 3:
        raise ValueError("write_rgb_png expects RGB array.")

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
    with path.open("wb") as f:
        f.write(b"".join(png))


def save_images(images: dict[str, Any], output_dir: Path, frame_idx: int, montage: bool) -> str:
    if len(images) == 1:
        image = next(iter(images.values()))
        image_file = f"rgb_{frame_idx:04d}.png"
        image.save_to_disk(str(output_dir / image_file))
        return image_file

    tiles = []
    for camera_name, _ in CAMERA_SPECS:
        image = images[camera_name]
        camera_dir = output_dir / camera_name
        camera_dir.mkdir(parents=True, exist_ok=True)
        image_file = f"rgb_{frame_idx:04d}.png"
        image.save_to_disk(str(camera_dir / image_file))
        if montage:
            tiles.append(carla_image_to_rgb(image))

    if montage:
        import numpy as np

        top = np.hstack(tiles[:3])
        bottom = np.hstack(tiles[3:])
        write_rgb_png(output_dir / f"rgb_{frame_idx:04d}.png", np.vstack([top, bottom]))
        return f"rgb_{frame_idx:04d}.png"
    return f"CAM_FRONT/rgb_{frame_idx:04d}.png"


def get_ego_actor(simulation):
    scenic_ego = getattr(simulation, "ego", None)
    if scenic_ego is not None and getattr(scenic_ego, "carlaActor", None) is not None:
        return scenic_ego.carlaActor
    if simulation.objects and getattr(simulation.objects[0], "carlaActor", None) is not None:
        return simulation.objects[0].carlaActor
    raise RuntimeError("Scenic simulation did not expose an ego CARLA actor.")


class DirectScenicRunner:
    def __init__(self, scenic_file: Path, params: dict[str, Any], verbosity: int = 1):
        import scenic.core.errors as errors
        import scenic.syntax.translator as translator

        self.errors = errors
        self.translator = translator
        self.verbosity = verbosity
        translator.verbosity = verbosity
        translator.usePruning = True
        print("Beginning Scenic construction...")
        self.scenario = errors.callBeginningScenicTrace(
            lambda: translator.scenarioFromFile(
                str(scenic_file),
                params=params,
                model="scenic.simulators.carla.model",
                scenario=None,
            )
        )
        self.simulator = errors.callBeginningScenicTrace(self.scenario.getSimulator)
        self.simulator.render = False
        self.simulation = None

    def create_simulation(self, scene_sample_attempts: int):
        from scenic.core.simulators import SimulationCreationError

        for attempt in range(1, scene_sample_attempts + 1):
            scene, iterations = self.errors.callBeginningScenicTrace(
                lambda: self.scenario.generate(verbosity=self.verbosity)
            )
            print(f"Generated Scenic scene attempt {attempt} after {iterations} rejection iterations.")
            try:
                self.simulation = self.simulator.createSimulation(scene, verbosity=self.verbosity)
                return self.simulation
            except SimulationCreationError as exc:
                print(f"Failed to create CARLA simulation: {exc}")
        raise RuntimeError(f"Failed to create a CARLA simulation after {scene_sample_attempts} Scenic samples.")

    def begin_dynamic(self) -> None:
        import scenic.syntax.veneer as veneer

        simulation = self.simulation
        if simulation is None:
            raise RuntimeError("create_simulation must be called before begin_dynamic.")
        veneer.beginSimulation(simulation)
        simulation.scene.dynamicScenario._start()
        for obj in simulation.objects:
            obj.startDynamicSimulation()
        simulation.updateObjects()

    def step_dynamic(self) -> bool:
        from scenic.core.simulators import EndSimulationAction
        from scenic.core.errors import InvalidScenarioError

        simulation = self.simulation
        dynamic_scenario = simulation.scene.dynamicScenario

        termination_reason = dynamic_scenario._step()
        simulation.recordCurrentState()
        monitor_reason = dynamic_scenario._runMonitors()
        if monitor_reason is not None:
            termination_reason = monitor_reason
        if termination_reason is not None:
            return False
        termination_reason = dynamic_scenario._checkSimulationTerminationConditions()
        if termination_reason is not None:
            return False

        all_actions = OrderedDict()
        for agent in simulation.scheduleForAgents():
            behavior = agent.behavior
            if not behavior._runningIterator:
                behavior._start(agent)
            actions = behavior._step()
            if isinstance(actions, EndSimulationAction):
                return False
            assert isinstance(actions, tuple)
            if len(actions) == 1 and isinstance(actions[0], (list, tuple)):
                actions = tuple(actions[0])
            if not simulation.actionsAreCompatible(agent, actions):
                raise InvalidScenarioError(f"agent {agent} tried incompatible action(s) {actions}")
            all_actions[agent] = actions

        simulation.executeActions(all_actions)
        simulation.step()
        simulation.updateObjects()
        simulation.currentTime += 1
        return True

    def end(self) -> None:
        if self.simulation is not None:
            import scenic.syntax.veneer as veneer
            from scenic.core.object_types import disableDynamicProxyFor
            from scenic.core.requirements import RequirementType

            try:
                for scenario in tuple(veneer.runningScenarios):
                    scenario._stop("simulation terminated", quiet=True)
                dynamic_scenario = self.simulation.scene.dynamicScenario
                values = dynamic_scenario._evaluateRecordedExprs(RequirementType.recordFinal)
                for name, value in values.items():
                    self.simulation.records[name] = value
                self.simulation.destroy()
                for obj in self.simulation.scene.objects:
                    disableDynamicProxyFor(obj)
                for agent in self.simulation.agents:
                    if agent.behavior._isRunning:
                        agent.behavior._stop()
                for monitor in self.simulation.scene.monitors:
                    if monitor._isRunning:
                        monitor._stop()
                veneer.endSimulation(self.simulation)
            except Exception as exc:
                print(f"WARNING: Scenic simulation cleanup failed: {exc}", file=sys.stderr)
        try:
            self.simulator.destroy()
        except Exception as exc:
            print(f"WARNING: Scenic simulator cleanup failed: {exc}", file=sys.stderr)


def set_ego_autopilot(ego, traffic_manager, enabled: bool, speed_difference: float) -> None:
    if not enabled or not hasattr(ego, "set_autopilot"):
        return
    ego.set_autopilot(True, traffic_manager.get_port())
    traffic_manager.vehicle_percentage_speed_difference(ego, speed_difference)


def save_ego_log(path: Path, rows: list[list[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["frame", "x", "y", "z", "yaw", "speed_mps"])
        writer.writerows(rows)


def run(args: argparse.Namespace) -> int:
    repo_root = repo_root_from_this_file()
    add_repo_paths(repo_root)
    carla = import_carla(args.carla_root)

    random.seed(args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.clean_output:
        clean_output_dir(args.output_dir)

    params = {
        "address": args.host,
        "port": args.port,
        "timeout": args.timeout,
        "render": 0,
        "timestep": args.timestep,
    }
    if args.weather:
        params["weather"] = args.weather

    runner = None
    cameras = []
    log_rows = []
    saved_images = []
    try:
        runner = DirectScenicRunner(args.scenic_file, params, verbosity=args.verbosity)
        simulation = runner.create_simulation(args.scene_sample_attempts)
        world = simulation.world
        ego = get_ego_actor(simulation)
        set_ego_autopilot(ego, simulation.tm, args.ego_autopilot, args.ego_speed_difference)

        if args.camera_mode == "surround":
            cameras, camera_queues = attach_surround_cameras(carla, world, ego, args.width, args.height, args.fov)
        else:
            cameras, camera_queues = attach_front_camera(carla, world, ego, args.width, args.height, args.fov)

        runner.begin_dynamic()
        for _ in range(args.warmup_ticks):
            world.tick()
            simulation.updateObjects()
            drain_queues(camera_queues)

        for frame_idx in range(args.frames):
            if not runner.step_dynamic():
                print(f"Scenic scenario ended at frame {frame_idx}.")
                break

            images = read_images(camera_queues)
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

            if frame_idx % args.save_every == 0:
                image_file = save_images(images, args.output_dir, frame_idx, args.montage)
                saved_images.append(image_file)

            if frame_idx % 20 == 0:
                print(f"frame={frame_idx:04d} ego_speed={speed:.2f} m/s saved={len(saved_images)}")

        save_ego_log(args.output_dir / "ego_log.csv", log_rows)
        write_json(
            args.output_dir / "run_metadata.json",
            {
                "runner": "risky-weaver direct scenic_to_carla_images",
                "uses_safebench": False,
                "scenic_file": str(args.scenic_file),
                "output_dir": str(args.output_dir),
                "frames_requested": args.frames,
                "save_every": args.save_every,
                "saved_images": saved_images,
                "camera_mode": args.camera_mode,
                "width": args.width,
                "height": args.height,
                "fov": args.fov,
                "params": params,
            },
        )
        print(f"Done. Saved {len(saved_images)} image frames to {args.output_dir}")
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
        if runner is not None:
            runner.end()
        time.sleep(0.5)


def parse_args() -> argparse.Namespace:
    default_carla_root = Path(os.environ.get("CARLA_ROOT", "/mnt/data2/congfeng/CARLA"))
    default_scenic = repo_root_from_this_file() / "risky-weaver" / "opencode" / "workdir" / "generated_scene.scenic"
    default_output = repo_root_from_this_file() / "risky-weaver" / "run" / "images"

    parser = argparse.ArgumentParser(description="Run a Scenic file directly in CARLA and save images.")
    parser.add_argument("--carla-root", type=Path, default=default_carla_root)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2001)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--scenic-file", type=Path, default=default_scenic)
    parser.add_argument("--scene-sample-attempts", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, default=default_output)
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--warmup-ticks", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timestep", type=float, default=0.05)
    parser.add_argument("--weather", default="ClearNoon")
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--height", type=int, default=450)
    parser.add_argument("--fov", type=float, default=90.0)
    parser.add_argument("--camera-mode", choices=["front", "surround"], default="surround")
    parser.add_argument("--montage", action="store_true", help="Also write 2x3 surround montage images as rgb_XXXX.png.")
    parser.add_argument("--ego-autopilot", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--ego-speed-difference", type=float, default=-5.0)
    parser.add_argument("--verbosity", type=int, choices=[0, 1, 2, 3], default=1)
    parser.add_argument("--clean-output", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.scenic_file = args.scenic_file.resolve()
    args.output_dir = args.output_dir.resolve()
    args.carla_root = args.carla_root.resolve()
    if not args.scenic_file.exists():
        raise FileNotFoundError(f"Scenic file not found: {args.scenic_file}")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
