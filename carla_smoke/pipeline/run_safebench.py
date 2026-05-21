#!/usr/bin/env python3
"""Run a SafeBench Scenic scene, then feed it through the carla_smoke risk pipeline."""

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


def read_json_if_exists(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def derive_l4_town(explicit_town, l0_json_path):
    if explicit_town:
        return explicit_town
    l0_state = read_json_if_exists(l0_json_path) or {}
    source = l0_state.get("source") or {}
    source_map = source.get("source_map") or source.get("map") or (l0_state.get("road") or {}).get("map")
    if source_map:
        return os.path.basename(str(source_map))
    return "Town03"


def main():
    repo_root = repo_root_from_this_file()
    default_carla_root = os.environ.get("CARLA_ROOT", "/mnt/data2/congfeng/carla915")
    default_workdir_root = os.path.join(repo_root, "carla_smoke", "workdir")
    default_scenic_dir = os.path.join(
        repo_root,
        "safebench",
        "scenario",
        "scenario_data",
        "scenic_data",
        "dynamic_scenario",
    )

    parser = argparse.ArgumentParser(
        description="Select one SafeBench Scenic scenario, capture CARLA frames, and run L0-L4 risk generation."
    )
    parser.add_argument("--carla-root", default=default_carla_root)
    parser.add_argument(
        "--carla-python",
        default=None,
        help=(
            "Python executable for CARLA-dependent stages. Use this when the current "
            "environment cannot import CARLA, for example a Python 3.7 env for a cp37 CARLA API."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--scenic-dir", default=default_scenic_dir)
    parser.add_argument("--scenic-file", default=None)
    parser.add_argument("--scenario-index", type=int, default=0)
    parser.add_argument("--scene-sample-attempts", type=int, default=20)
    parser.add_argument("--frames", type=int, default=160)
    parser.add_argument("--save-every", type=int, default=20, help="Save one frame every N ticks. With --timestep 0.05, 20 ticks = 1 second.")
    parser.add_argument("--warmup-ticks", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timestep", type=float, default=0.05)
    parser.add_argument("--ego-speed-difference", type=float, default=-5.0)
    parser.add_argument("--weather", default="ClearNoon")
    parser.add_argument("--camera-mode", choices=["front", "surround"], default="surround")
    parser.add_argument("--town", default=None, help="Town for L4 replay. Defaults to the L0 source map captured from Scenic.")
    parser.add_argument("--workdir-root", default=default_workdir_root)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--select", choices=["first", "middle", "last"], default="middle")
    parser.add_argument("--sample-count", type=int, default=1, help="Number of saved frames sampled for L1 inference.")
    parser.set_defaults(single_random_frame=True)
    parser.add_argument("--single-random-frame", dest="single_random_frame", action="store_true", help="Capture exactly one random source frame from the SafeBench scene.")
    parser.add_argument("--sequence-capture", dest="single_random_frame", action="store_false", help="Capture the old saved-frame sequence instead of a single random frame.")
    parser.add_argument("--scenario-hint", default="")
    parser.add_argument("--model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--deepseek-url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--skip-scene", action="store_true", help="Reuse an existing images directory in this run folder.")
    parser.add_argument("--skip-agents", action="store_true", help="Only capture the SafeBench-derived CARLA scene.")
    parser.add_argument("--skip-l2", action="store_true")
    parser.add_argument("--skip-l3", action="store_true")
    parser.add_argument("--skip-l4", action="store_true")
    parser.add_argument("--l4-chain-index", type=int, default=0)
    parser.add_argument("--l4-all-chains", action="store_true")
    parser.add_argument("--continue-on-chain-error", action="store_true", help="When running all L4 chains, continue if one generated risk scene fails.")
    parser.add_argument("--l4-frames", type=int, default=140)
    parser.add_argument("--l4-save-every", type=int, default=5)
    parser.add_argument("--l4-local-trigger-frame", type=int, default=20)
    parser.add_argument("--l4-pre-trigger-seconds", type=float, default=2.0)
    parser.add_argument("--validate-event-trace", action="store_true")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--opencode-repair-attempts", type=int, default=3)
    parser.add_argument("--clean-images", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id or timestamp()
    carla_python = args.carla_python or sys.executable
    run_dir = os.path.abspath(os.path.join(args.workdir_root, run_id))
    image_dir = os.path.join(run_dir, "images")
    l0_dir = os.path.join(run_dir, "l0")
    l2_dir = os.path.join(run_dir, "l2")
    l3_dir = os.path.join(run_dir, "l3")
    l4_dir = os.path.join(run_dir, "l4")

    scene_script = os.path.join(repo_root, "carla_smoke", "scenes", "safebench_scenic_scene.py")
    l0_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l0.py")
    l2_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l2.py")
    l3_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l3.py")
    l4_scenario_language_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l4_scenario_language.py")

    os.makedirs(run_dir, exist_ok=True)

    if not args.skip_scene:
        scene_command = [
            carla_python,
            scene_script,
            "--carla-root",
            args.carla_root,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--timeout",
            str(args.timeout),
            "--scenic-dir",
            args.scenic_dir,
            "--scenario-index",
            str(args.scenario_index),
            "--scene-sample-attempts",
            str(args.scene_sample_attempts),
            "--frames",
            str(args.frames),
            "--save-every",
            str(args.save_every),
            "--warmup-ticks",
            str(args.warmup_ticks),
            "--seed",
            str(args.seed),
            "--timestep",
            str(args.timestep),
            "--ego-speed-difference",
            str(args.ego_speed_difference),
            "--weather",
            args.weather,
            "--camera-mode",
            args.camera_mode,
            "--output-dir",
            image_dir,
        ]
        if args.single_random_frame:
            scene_command.append("--single-random-frame")
        if args.scenic_file:
            scene_command.extend(["--scenic-file", args.scenic_file])
        if args.clean_images or args.single_random_frame:
            scene_command.append("--clean-output")
        run_command(scene_command)

    scene_info = read_json_if_exists(os.path.join(image_dir, "safebench_scene.json")) or {}
    scenario_hint = args.scenario_hint
    if not scenario_hint and scene_info.get("description"):
        scenario_hint = f"SafeBench Scenic scenario: {scene_info['description']}"

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
            "--sample-count",
            str(args.sample_count),
            "--output-dir",
            l0_dir,
        ]
        if args.env_file:
            l0_command.extend(["--env-file", args.env_file])
        ego_log = os.path.join(image_dir, "ego_log.csv")
        if os.path.exists(ego_log):
            l0_command.extend(["--ego-log", ego_log])
        if scenario_hint:
            l0_command.extend(["--scenario-hint", scenario_hint])
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
            if args.env_file:
                l2_command.extend(["--env-file", args.env_file])
            run_command(l2_command)

            if not args.skip_l3:
                l3_command = [
                    sys.executable,
                    l3_script,
                    os.path.join(l2_dir, "triggers.json"),
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
                    l3_dir,
                ]
                if args.env_file:
                    l3_command.extend(["--env-file", args.env_file])
                run_command(l3_command)

                if not args.skip_l4:
                    l4_command = [
                        carla_python,
                        l4_scenario_language_script,
                        os.path.join(l3_dir, "chains.json"),
                        "--output-dir",
                        l4_dir,
                        "--l0-json",
                        os.path.join(l0_dir, "state.json"),
                        "--carla-root",
                        args.carla_root,
                        "--carla-python",
                        carla_python,
                        "--host",
                        args.host,
                        "--port",
                        str(args.port),
                        "--timeout",
                        str(args.timeout),
                        "--scene-sample-attempts",
                        str(args.scene_sample_attempts),
                        "--frames",
                        str(args.l4_frames),
                        "--save-every",
                        str(args.l4_save_every),
                        "--local-trigger-frame",
                        str(args.l4_local_trigger_frame),
                        "--pre-trigger-seconds",
                        str(args.l4_pre_trigger_seconds),
                        "--source-timestep",
                        str(args.timestep),
                        "--warmup-ticks",
                        str(args.warmup_ticks),
                        "--seed",
                        str(args.seed),
                        "--timestep",
                        str(args.timestep),
                        "--ego-speed-difference",
                        str(args.ego_speed_difference),
                        "--weather",
                        args.weather,
                        "--opencode-bin",
                        args.opencode_bin,
                        "--opencode-model",
                        args.opencode_model,
                        "--opencode-repair-attempts",
                        str(args.opencode_repair_attempts),
                        "--plan-model",
                        args.model,
                        "--plan-url",
                        args.deepseek_url,
                        "--api-key-env",
                        args.api_key_env,
                        "--plan-timeout",
                        str(args.timeout),
                        "--execute",
                    ]
                    if args.env_file:
                        l4_command.extend(["--env-file", args.env_file])
                    if args.l4_all_chains:
                        l4_command.append("--all-chains")
                        if args.continue_on_chain_error:
                            l4_command.append("--continue-on-chain-error")
                    else:
                        l4_command.extend(["--chain-index", str(args.l4_chain_index)])
                    if args.validate_event_trace:
                        l4_command.append("--validate-event-trace")
                    run_command(l4_command)

    manifest = {
        "run_id": run_id,
        "run_dir": run_dir,
        "images": image_dir,
        "vision": None,
        "l0": l0_dir,
        "l2": None if args.skip_l2 or args.skip_agents else l2_dir,
        "l3": None if args.skip_l2 or args.skip_l3 or args.skip_agents else l3_dir,
        "l4": None if args.skip_l2 or args.skip_l3 or args.skip_l4 or args.skip_agents else l4_dir,
        "scene_source": "safebench_scenic",
        "safebench_scene": scene_info,
        "scenario_hint": scenario_hint,
        "model": args.model,
        "deepseek_url": args.deepseek_url,
        "scene": {
            "carla_python": carla_python,
            "scenic_dir": args.scenic_dir,
            "scenic_file": args.scenic_file,
            "scenario_index": args.scenario_index,
            "frames": args.frames,
            "save_every": args.save_every,
            "camera_mode": args.camera_mode,
            "sample_count": args.sample_count,
            "single_random_frame": args.single_random_frame,
            "seed": args.seed,
            "timestep": args.timestep,
            "weather": args.weather,
            "l4_town": args.town or "from_l0_source_map",
            "l4_backend": "opencode_scenic",
            "l4_local_trigger_frame": args.l4_local_trigger_frame,
            "l4_pre_trigger_seconds": args.l4_pre_trigger_seconds,
        },
    }
    write_manifest(os.path.join(run_dir, "manifest.json"), manifest)

    print("\nSafeBench smoke pipeline finished.")
    print(f"Run dir: {run_dir}")
    print(f"Images: {image_dir}")
    if scene_info.get("scenic_file"):
        print(f"SafeBench scene: {scene_info['scenic_file']}")
    if not args.skip_agents:
        print(f"L0/L1: {l0_dir}")
        if not args.skip_l2:
            print(f"L2: {l2_dir}")
            if not args.skip_l3:
                print(f"L3: {l3_dir}")
                if not args.skip_l4:
                    print(f"L4: {l4_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
