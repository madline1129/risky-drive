#!/usr/bin/env python3
"""Recommended one-command entry point for the full SafeBench -> L4 pipeline."""

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
    runner = os.path.join(repo_root, "carla_smoke", "pipeline", "run_safebench_all_risks.py")

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
            "Run the full recommended pipeline from scratch: SafeBench Scenic capture, "
            "six-view source images, Qwen multi-frame vision, L0/L1/L2/L3, and all L4 risk chains."
        )
    )
    parser.add_argument("--carla-root", default=default_carla_root)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--scenic-dir", default=default_scenic_dir)
    parser.add_argument("--scenic-file", default=None)
    parser.add_argument("--scenario-index", type=int, default=0)
    parser.add_argument("--scene-sample-attempts", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--weather", default="ClearNoon")
    parser.add_argument("--workdir-root", default=default_workdir_root)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--opencode-model", default="deepseek-v4-flash")
    parser.add_argument("--qwen-model", default="qwen3.5:0.8b")
    parser.add_argument("--l4-backend", choices=["safebench-intervention", "code-agent", "scenario-language"], default="scenario-language")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--skip-plan-agent", action="store_true")
    parser.add_argument("--stop-on-chain-error", action="store_true")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Shorter debug run: 10s source capture, still running all generated L4 chains.",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Additional raw argument passed to run_safebench_all_risks.py. May be repeated.",
    )
    args = parser.parse_args()

    command = [
        sys.executable,
        runner,
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
        "200" if args.quick else "600",
        "--save-every",
        "20",
        "--sample-count",
        "1",
        "--single-random-frame",
        "--camera-mode",
        "surround",
        "--timestep",
        "0.05",
        "--warmup-ticks",
        "5",
        "--seed",
        str(args.seed),
        "--ego-speed-difference",
        "-5.0",
        "--weather",
        args.weather,
        "--workdir-root",
        args.workdir_root,
        "--model",
        args.model,
        "--qwen-model",
        args.qwen_model,
        "--api-key-env",
        args.api_key_env,
        "--code-agent",
        "opencode",
        "--opencode-bin",
        "opencode",
        "--opencode-model",
        args.opencode_model,
        "--opencode-repair-attempts",
        "3",
        "--l4-frames",
        "180",
        "--l4-save-every",
        "5",
        "--l4-local-trigger-frame",
        "20",
        "--l4-pre-trigger-seconds",
        "2.0",
        "--l4-backend",
        args.l4_backend,
    ]
    if args.skip_plan_agent:
        command.append("--skip-plan-agent")

    if args.scenic_file:
        command.extend(["--scenic-file", args.scenic_file])
    if args.run_id:
        command.extend(["--run-id", args.run_id])
    if args.env_file:
        command.extend(["--env-file", args.env_file])
    if args.stop_on_chain_error:
        command.append("--stop-on-chain-error")
    command.extend(args.extra_arg)

    run_command(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
