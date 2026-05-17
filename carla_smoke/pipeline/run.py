#!/usr/bin/env python3
"""Run CARLA scene generation, Qwen vision, and DeepSeek L0-L4 risk pipeline."""

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

    parser = argparse.ArgumentParser(description="Generate a CARLA scene, run local Qwen vision, then DeepSeek L0-L4 agents.")
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
    parser.add_argument("--qwen-model", default="qwen3.5:0.8b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/chat")
    parser.add_argument("--vision-timeout", type=float, default=300.0)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--env-file", default=None, help="Optional .env path. Defaults to searching upward from cwd.")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--skip-scene", action="store_true", help="Use an existing images directory in this run folder.")
    parser.add_argument("--skip-vision", action="store_true", help="Skip local Qwen vision observation.")
    parser.add_argument("--skip-agents", action="store_true", help="Only generate CARLA images.")
    parser.add_argument("--skip-l2", action="store_true", help="Run L0/L1 only.")
    parser.add_argument("--skip-l3", action="store_true", help="Run through L2 only.")
    parser.add_argument("--skip-l4", action="store_true", help="Run through L3 only; do not execute CARLA risk scene.")
    parser.add_argument("--l4-chain-index", type=int, default=0)
    parser.add_argument("--l4-frames", type=int, default=140)
    parser.add_argument("--l4-save-every", type=int, default=5)
    parser.add_argument("--code-agent", choices=["template", "opencode"], default="template")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default="deepseek/deepseek-v4-pro")
    parser.add_argument("--opencode-repair-attempts", type=int, default=3)
    parser.add_argument("--clean-images", action="store_true", help="Clean old rgb_*.png inside the images directory.")
    args = parser.parse_args()

    run_id = args.run_id or timestamp()
    run_dir = os.path.abspath(os.path.join(args.workdir_root, run_id))
    image_dir = os.path.join(run_dir, "images")
    vision_dir = os.path.join(run_dir, "vision")
    l0_dir = os.path.join(run_dir, "l0")
    l2_dir = os.path.join(run_dir, "l2")
    l3_dir = os.path.join(run_dir, "l3")
    l4_dir = os.path.join(run_dir, "l4")

    scene_script = os.path.join(repo_root, "carla_smoke", "scenes", "normal_driving_scene.py")
    vision_script = os.path.join(repo_root, "carla_smoke", "pipeline", "vision.py")
    l0_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l0.py")
    l2_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l2.py")
    l3_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l3.py")
    l4_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l4.py")

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

    vision_json = os.path.join(vision_dir, "observations.json")
    if not args.skip_agents and not args.skip_vision:
        vision_command = [
            sys.executable,
            vision_script,
            image_dir,
            "--model",
            args.qwen_model,
            "--url",
            args.ollama_url,
            "--timeout",
            str(args.vision_timeout),
            "--select",
            args.select,
            "--output-dir",
            vision_dir,
        ]
        run_command(vision_command)

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
        if not args.skip_vision and os.path.exists(vision_json):
            l0_command.extend(["--vision-json", vision_json])
        if args.env_file:
            l0_command.extend(["--env-file", args.env_file])
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
                        sys.executable,
                        l4_script,
                        os.path.join(l3_dir, "chains.json"),
                        "--chain-index",
                        str(args.l4_chain_index),
                        "--output-dir",
                        l4_dir,
                        "--l0-json",
                        os.path.join(l0_dir, "state.json"),
                        "--carla-root",
                        args.carla_root,
                        "--host",
                        args.host,
                        "--port",
                        str(args.port),
                        "--town",
                        args.town,
                        "--frames",
                        str(args.l4_frames),
                        "--save-every",
                        str(args.l4_save_every),
                        "--code-agent",
                        args.code_agent,
                        "--opencode-bin",
                        args.opencode_bin,
                        "--opencode-model",
                        args.opencode_model,
                        "--opencode-repair-attempts",
                        str(args.opencode_repair_attempts),
                        "--execute",
                    ]
                    run_command(l4_command)

    manifest = {
        "run_id": run_id,
        "run_dir": run_dir,
        "images": image_dir,
        "vision": None if args.skip_vision or args.skip_agents else vision_dir,
        "l0": l0_dir,
        "l2": None if args.skip_l2 or args.skip_agents else l2_dir,
        "l3": None if args.skip_l2 or args.skip_l3 or args.skip_agents else l3_dir,
        "l4": None if args.skip_l2 or args.skip_l3 or args.skip_l4 or args.skip_agents else l4_dir,
        "model": args.model,
        "qwen_model": args.qwen_model,
        "deepseek_url": args.deepseek_url,
        "ollama_url": args.ollama_url,
        "scene": {
            "town": args.town,
            "frames": args.frames,
            "save_every": args.save_every,
            "vehicles": args.vehicles,
            "lead_distance": args.lead_distance,
            "lead_speed_difference": args.lead_speed_difference,
            "seed": args.seed,
            "state_source": "carla_api_state_json",
            "vision_source": None if args.skip_vision or args.skip_agents else "local_qwen_ollama",
            "l4_code_agent": args.code_agent,
        },
    }
    write_manifest(os.path.join(run_dir, "manifest.json"), manifest)

    print("\nPipeline finished.")
    print(f"Run dir: {run_dir}")
    print(f"Images: {image_dir}")
    if not args.skip_agents:
        if not args.skip_vision:
            print(f"Vision: {vision_dir}")
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
