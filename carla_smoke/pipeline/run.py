#!/usr/bin/env python3
"""Run CARLA scene generation and DeepSeek L0/L1/L2 risk pipeline."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

from deepseek_client import DEFAULT_DEEPSEEK_MODEL, DEFAULT_DEEPSEEK_URL


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def run_command(command):
    print("\n$ " + " ".join(command))
    subprocess.run(command, check=True)


def write_manifest(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    repo_root = repo_root_from_this_file()
    default_workdir_root = os.path.join(repo_root, "carla_smoke", "workdir")

    parser = argparse.ArgumentParser(description="Generate a CARLA scene and run DeepSeek L0/L1/L2 risk agents.")
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--tm-port", type=int, default=8000)
    parser.add_argument("--town", default="Town03")
    parser.add_argument("--frames", type=int, default=160)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--vehicles", type=int, default=30)
    parser.add_argument("--lead-distance", type=float, default=14.0)
    parser.add_argument("--lead-speed-difference", type=float, default=35.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--workdir-root", default=default_workdir_root)
    parser.add_argument("--run-id", default=None, help="Timestamp folder name. Defaults to current time.")
    parser.add_argument("--select", choices=["first", "middle", "last"], default="middle")
    parser.add_argument("--scenario-hint", default="")
    parser.add_argument("--model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--deepseek-url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--skip-scene", action="store_true", help="Use an existing images directory in this run folder.")
    parser.add_argument("--skip-agents", action="store_true", help="Only generate CARLA images.")
    parser.add_argument("--skip-l2", action="store_true", help="Run L0/L1 only.")
    parser.add_argument("--clean-images", action="store_true", help="Clean old rgb_*.png inside the images directory.")
    args = parser.parse_args()

    run_id = args.run_id or timestamp()
    run_dir = os.path.abspath(os.path.join(args.workdir_root, run_id))
    image_dir = os.path.join(run_dir, "images")
    l0_dir = os.path.join(run_dir, "l0")
    l2_dir = os.path.join(run_dir, "l2")

    scene_script = os.path.join(repo_root, "carla_smoke", "scenes", "normal_driving_scene.py")
    l0_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l0.py")
    l2_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l2.py")

    os.makedirs(run_dir, exist_ok=True)

    if not args.skip_scene:
        scene_command = [
            sys.executable,
            scene_script,
            "--carla-root",
            args.carla_root,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--tm-port",
            str(args.tm_port),
            "--town",
            args.town,
            "--frames",
            str(args.frames),
            "--save-every",
            str(args.save_every),
            "--vehicles",
            str(args.vehicles),
            "--lead-distance",
            str(args.lead_distance),
            "--lead-speed-difference",
            str(args.lead_speed_difference),
            "--seed",
            str(args.seed),
            "--output-dir",
            image_dir,
        ]
        if args.clean_images:
            scene_command.append("--clean-output")
        run_command(scene_command)

    if not args.skip_agents:
        l0_command = [
            sys.executable,
            l0_script,
            image_dir,
            "--model",
            args.model,
            "--url",
            args.deepseek_url,
            "--api-key-env",
            args.api_key_env,
            "--timeout",
            str(args.timeout),
            "--select",
            args.select,
            "--output-dir",
            l0_dir,
        ]
        ego_log = os.path.join(image_dir, "ego_log.csv")
        if os.path.exists(ego_log):
            l0_command.extend(["--ego-log", ego_log])
        if args.scenario_hint:
            l0_command.extend(["--scenario-hint", args.scenario_hint])
        run_command(l0_command)

        if not args.skip_l2:
            l2_command = [
                sys.executable,
                l2_script,
                os.path.join(l0_dir, "risks.json"),
                "--l0-json",
                os.path.join(l0_dir, "state.json"),
                "--model",
                args.model,
                "--url",
                args.deepseek_url,
                "--api-key-env",
                args.api_key_env,
                "--timeout",
                str(args.timeout),
                "--output-dir",
                l2_dir,
            ]
            run_command(l2_command)

    manifest = {
        "run_id": run_id,
        "run_dir": run_dir,
        "images": image_dir,
        "l0": l0_dir,
        "l2": None if args.skip_l2 or args.skip_agents else l2_dir,
        "model": args.model,
        "deepseek_url": args.deepseek_url,
        "scene": {
            "town": args.town,
            "frames": args.frames,
            "save_every": args.save_every,
            "vehicles": args.vehicles,
            "lead_distance": args.lead_distance,
            "lead_speed_difference": args.lead_speed_difference,
            "seed": args.seed,
        },
    }
    write_manifest(os.path.join(run_dir, "manifest.json"), manifest)

    print("\nPipeline finished.")
    print(f"Run dir: {run_dir}")
    print(f"Images: {image_dir}")
    if not args.skip_agents:
        print(f"L0/L1: {l0_dir}")
        if not args.skip_l2:
            print(f"L2: {l2_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
