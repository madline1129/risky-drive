#!/usr/bin/env python3
"""Run one SafeBench scene from scratch, then execute every L4 risk chain."""

import argparse
import os
import subprocess
import sys


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def run_command(command):
    print("\n$ " + " ".join(command))
    subprocess.run(command, check=True)


def main():
    repo_root = repo_root_from_this_file()
    default_carla_root = os.environ.get("CARLA_ROOT", "/mnt/data2/congfeng/CARLA")
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
        description=(
            "From scratch: capture one SafeBench Scenic scene, run L0/L1/L2/L3, "
            "then execute every generated L4 risk chain."
        )
    )
    parser.add_argument("--carla-root", default=default_carla_root)
    parser.add_argument("--carla-python", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--scenic-dir", default=default_scenic_dir)
    parser.add_argument("--scenic-file", default=None)
    parser.add_argument("--scenario-index", type=int, default=0)
    parser.add_argument("--scene-sample-attempts", type=int, default=20)
    parser.add_argument("--frames", type=int, default=600, help="Scene capture ticks. 600 * 0.05s = 30s by default.")
    parser.add_argument("--save-every", type=int, default=20, help="Capture one scene image every N ticks. 20 * 0.05s = 1s by default.")
    parser.add_argument("--sample-count", type=int, default=5, help="Number of sampled montage frames for Qwen/L1.")
    parser.add_argument("--camera-mode", choices=["front", "surround"], default="surround")
    parser.add_argument("--timestep", type=float, default=0.05)
    parser.add_argument("--warmup-ticks", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--ego-speed-difference", type=float, default=-5.0)
    parser.add_argument("--weather", default="ClearNoon")
    parser.add_argument("--workdir-root", default=default_workdir_root)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--qwen-model", default="qwen3.5:0.8b")
    parser.add_argument("--deepseek-url", default="https://api.deepseek.com/chat/completions")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/chat")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--code-agent", choices=["template", "opencode"], default="opencode")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default="deepseek-v4-pro")
    parser.add_argument("--opencode-repair-attempts", type=int, default=3)
    parser.add_argument("--l4-frames", type=int, default=140)
    parser.add_argument("--l4-save-every", type=int, default=5)
    parser.add_argument("--validate-event-trace", action="store_true")
    parser.add_argument("--stop-on-chain-error", action="store_true", help="Stop all-chain execution when one L4 chain fails.")
    parser.add_argument("--extra-arg", action="append", default=[], help="Additional raw argument passed to run_safebench.py. May be repeated.")
    args = parser.parse_args()

    run_safebench = os.path.join(repo_root, "carla_smoke", "pipeline", "run_safebench.py")
    command = [
        sys.executable,
        run_safebench,
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
        "--sample-count",
        str(args.sample_count),
        "--camera-mode",
        args.camera_mode,
        "--timestep",
        str(args.timestep),
        "--warmup-ticks",
        str(args.warmup_ticks),
        "--seed",
        str(args.seed),
        "--ego-speed-difference",
        str(args.ego_speed_difference),
        "--weather",
        args.weather,
        "--workdir-root",
        args.workdir_root,
        "--model",
        args.model,
        "--deepseek-url",
        args.deepseek_url,
        "--qwen-model",
        args.qwen_model,
        "--ollama-url",
        args.ollama_url,
        "--api-key-env",
        args.api_key_env,
        "--code-agent",
        args.code_agent,
        "--opencode-bin",
        args.opencode_bin,
        "--opencode-model",
        args.opencode_model,
        "--opencode-repair-attempts",
        str(args.opencode_repair_attempts),
        "--l4-frames",
        str(args.l4_frames),
        "--l4-save-every",
        str(args.l4_save_every),
        "--l4-all-chains",
    ]
    if args.carla_python:
        command.extend(["--carla-python", args.carla_python])
    if args.scenic_file:
        command.extend(["--scenic-file", args.scenic_file])
    if args.run_id:
        command.extend(["--run-id", args.run_id])
    if args.env_file:
        command.extend(["--env-file", args.env_file])
    if args.validate_event_trace:
        command.append("--validate-event-trace")
    if not args.stop_on_chain_error:
        command.append("--continue-on-chain-error")
    command.extend(args.extra_arg)

    run_command(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
