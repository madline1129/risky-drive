#!/usr/bin/env python3
"""Run L4 only by replaying the original SafeBench scene and intervening in-place."""

import argparse
import json
import os
import subprocess
import sys
from types import SimpleNamespace

try:
    from l4 import (
        build_config,
        chain_output_dir,
        chains_from_data,
        read_json,
        select_chain,
        validate_event_trace,
        write_json,
    )
except ImportError:
    from .l4 import (
        build_config,
        chain_output_dir,
        chains_from_data,
        read_json,
        select_chain,
        validate_event_trace,
        write_json,
    )


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def run_command(command):
    print("\n$ " + " ".join(command))
    subprocess.run(command, check=True)


def read_json_if_exists(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_scenic_file(repo_root, run_dir, explicit_scenic_file):
    if explicit_scenic_file:
        return os.path.abspath(explicit_scenic_file)
    scene_info = read_json_if_exists(os.path.join(run_dir, "images", "safebench_scene.json"))
    if not scene_info:
        raise FileNotFoundError(f"SafeBench scene metadata not found: {os.path.join(run_dir, 'images', 'safebench_scene.json')}")
    scenic_file = scene_info.get("scenic_file")
    if not scenic_file:
        raise ValueError("images/safebench_scene.json has no scenic_file field.")
    if os.path.isabs(scenic_file):
        return scenic_file
    return os.path.abspath(os.path.join(repo_root, scenic_file))


def scene_defaults(run_dir):
    scene_info = read_json_if_exists(os.path.join(run_dir, "images", "safebench_scene.json")) or {}
    params = scene_info.get("params") if isinstance(scene_info.get("params"), dict) else {}
    return {
        "frames": scene_info.get("frames"),
        "save_every": scene_info.get("save_every"),
        "timestep": params.get("timestep"),
        "weather": params.get("weather"),
        "timeout": params.get("timeout"),
    }


def plan_agent_args_from_args(args):
    return SimpleNamespace(
        skip_plan_agent=args.skip_plan_agent,
        plan_model=args.plan_model,
        plan_url=args.plan_url,
        api_key_env=args.api_key_env,
        env_file=args.env_file,
        plan_timeout=args.plan_timeout,
    )


def run_one_chain(args, repo_root, run_dir, chain, chain_index, all_chains, l0_state, l0_json, scenic_file, output_root):
    chain_dir = chain_output_dir(output_root, chain, chain_index + 1, all_chains)
    os.makedirs(chain_dir, exist_ok=True)
    config = build_config(
        chain,
        l0_state,
        l0_json,
        l4_frames=args.frames,
        local_trigger_frame=args.local_trigger_frame,
        pre_trigger_seconds=args.pre_trigger_seconds,
        source_timestep=args.source_timestep,
        plan_agent_args=plan_agent_args_from_args(args),
    )
    config["execution_backend"] = "safebench_intervention"
    config_path = os.path.join(chain_dir, "scenario_config.json")
    write_json(config_path, config)
    print(f"Saved intervention scenario config: {os.path.abspath(config_path)}")

    images_dir = os.path.join(chain_dir, "risk_images")
    intervention_script = os.path.join(repo_root, "carla_smoke", "scenes", "safebench_intervention_scene.py")
    command = [
        args.carla_python or sys.executable,
        intervention_script,
        "--scenario-config",
        config_path,
        "--l0-json",
        l0_json,
        "--carla-root",
        args.carla_root,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--timeout",
        str(args.timeout),
        "--scenic-file",
        scenic_file,
        "--scene-sample-attempts",
        str(args.scene_sample_attempts),
        "--output-dir",
        images_dir,
        "--frames",
        str(args.frames),
        "--save-every",
        str(args.save_every),
        "--trigger-frame",
        str(args.local_trigger_frame),
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
    ]
    if args.pre_roll_frames is not None:
        command.extend(["--pre-roll-frames", str(args.pre_roll_frames)])
    run_command(command)
    if args.validate_event_trace:
        validate_event_trace(config_path, images_dir)
    return {
        "chain_id": chain.get("id"),
        "output_dir": os.path.abspath(chain_dir),
        "scenario_config": os.path.abspath(config_path),
        "risk_images": os.path.abspath(images_dir),
    }


def main():
    repo_root = repo_root_from_this_file()
    default_run_dir = "/mnt/data2/whz/risky-drive/carla_smoke/workdir/20260519_150544"
    default_carla_root = os.environ.get("CARLA_ROOT", "/mnt/data2/congfeng/CARLA")

    parser = argparse.ArgumentParser(
        description=(
            "Run only L4 from an existing workdir, using SafeBench replay plus in-place intervention. "
            "This avoids re-spawning the L0 world from scratch."
        )
    )
    parser.add_argument("--run-dir", default=default_run_dir)
    parser.add_argument("--output-dir", default=None, help="Defaults to <run-dir>/l4_safebench_intervention.")
    parser.add_argument("--scenic-file", default=None)
    parser.add_argument("--l0-json", default=None)
    parser.add_argument("--l3-json", default=None)
    parser.add_argument("--carla-root", default=default_carla_root)
    parser.add_argument("--carla-python", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--scene-sample-attempts", type=int, default=20)
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--pre-roll-frames", type=int, default=None, help="Defaults to scenario_config.time_axis_policy.reconstruction_frame.")
    parser.add_argument("--local-trigger-frame", type=int, default=20)
    parser.add_argument("--pre-trigger-seconds", type=float, default=2.0)
    parser.add_argument("--source-timestep", type=float, default=0.05)
    parser.add_argument("--warmup-ticks", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timestep", type=float, default=0.05)
    parser.add_argument("--ego-speed-difference", type=float, default=-5.0)
    parser.add_argument("--weather", default="ClearNoon")
    parser.add_argument("--chain-index", type=int, default=0, help="Run one chain by default.")
    parser.add_argument("--all-chains", action="store_true")
    parser.add_argument("--continue-on-chain-error", action="store_true")
    parser.add_argument("--validate-event-trace", action="store_true", default=True)
    parser.add_argument("--skip-event-trace-validation", action="store_true")
    parser.add_argument("--skip-plan-agent", action="store_true")
    parser.add_argument("--plan-model", default="deepseek-v4-pro")
    parser.add_argument("--plan-url", default="https://api.deepseek.com/chat/completions")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--plan-timeout", type=float, default=300.0)
    args = parser.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    l0_json = os.path.abspath(args.l0_json or os.path.join(run_dir, "l0", "state.json"))
    l3_json = os.path.abspath(args.l3_json or os.path.join(run_dir, "l3", "chains.json"))
    output_root = os.path.abspath(args.output_dir or os.path.join(run_dir, "l4_safebench_intervention"))
    scenic_file = resolve_scenic_file(repo_root, run_dir, args.scenic_file)
    defaults = scene_defaults(run_dir)
    args.timestep = args.timestep if args.timestep is not None else defaults["timestep"] or 0.05
    args.weather = args.weather or defaults["weather"] or "ClearNoon"
    args.timeout = args.timeout or defaults["timeout"] or 300.0

    if args.skip_event_trace_validation:
        args.validate_event_trace = False
    if not os.path.exists(l0_json):
        raise FileNotFoundError(f"L0 state not found: {l0_json}")
    if not os.path.exists(l3_json):
        raise FileNotFoundError(f"L3 chains not found: {l3_json}")
    if not os.path.exists(scenic_file):
        raise FileNotFoundError(f"SafeBench Scenic file not found: {scenic_file}")

    l0_state = read_json(l0_json)
    chains_data = read_json(l3_json)
    if args.all_chains:
        chains = chains_from_data(chains_data)
    else:
        chains = [select_chain(chains_data, args.chain_index)]

    results = []
    for index, chain in enumerate(chains):
        display_index = index if args.all_chains else args.chain_index
        print(f"\n=== SafeBench-intervention L4 chain {display_index + 1}/{len(chains) if args.all_chains else 1}: {chain.get('id')} ===")
        try:
            results.append(
                run_one_chain(
                    args,
                    repo_root,
                    run_dir,
                    chain,
                    display_index,
                    args.all_chains,
                    l0_state,
                    l0_json,
                    scenic_file,
                    output_root,
                )
            )
        except Exception as exc:
            if not args.all_chains or not args.continue_on_chain_error:
                raise
            print(f"WARNING: chain {chain.get('id')} failed: {exc}", file=sys.stderr)

    write_json(os.path.join(output_root, "l4_safebench_intervention_results.json"), {"results": results})
    print(f"\nSafeBench-intervention L4 outputs: {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
