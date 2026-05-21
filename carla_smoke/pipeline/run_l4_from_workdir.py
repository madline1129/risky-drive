#!/usr/bin/env python3
"""Run only the L4 stage from an existing carla_smoke workdir."""

import argparse
import json
import os
import subprocess
import sys


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def read_json_if_exists(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def derive_town(run_dir, explicit_town):
    if explicit_town:
        return explicit_town
    l0_state = read_json_if_exists(os.path.join(run_dir, "l0", "state.json")) or {}
    source = l0_state.get("source") or {}
    source_map = source.get("source_map") or source.get("map") or (l0_state.get("road") or {}).get("map")
    if source_map:
        return os.path.basename(str(source_map))
    return "Town05"


def run_command(command):
    print("\n$ " + " ".join(command))
    subprocess.run(command, check=True)


def main():
    repo_root = repo_root_from_this_file()
    default_run_dir = "/mnt/data2/whz/risky-drive/carla_smoke/workdir/20260518_172554"
    default_carla_root = os.environ.get("CARLA_ROOT", "/mnt/data2/congfeng/CARLA")

    parser = argparse.ArgumentParser(
        description=(
            "Reuse an existing carla_smoke run directory and run only L4. "
            "This does not rerun SafeBench capture, Qwen/L1, L2, or L3."
        )
    )
    parser.add_argument("--run-dir", default=default_run_dir)
    parser.add_argument("--output-dir", default=None, help="Defaults to <run-dir>/l4_physical_task.")
    parser.add_argument("--carla-root", default=default_carla_root)
    parser.add_argument("--carla-python", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--town", default=None)
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument(
        "--l4-backend",
        choices=["safebench-intervention", "code-agent", "scenario-language"],
        default="scenario-language",
        help=(
            "Default scenario-language uses semantic primitives plus OpenCode-generated Scenic. "
            "Use code-agent to build a fresh CARLA Python scene with opencode/template. "
            "Use safebench-intervention to replay the original SafeBench scene and perturb live actors in-place."
        ),
    )
    parser.add_argument("--local-trigger-frame", type=int, default=20)
    parser.add_argument("--pre-trigger-seconds", type=float, default=2.0)
    parser.add_argument("--source-timestep", type=float, default=0.05)
    parser.add_argument("--chain-index", type=int, default=None, help="Run only one chain. Defaults to all chains.")
    parser.add_argument("--all-chains", action="store_true", help="Run all chains. Kept for consistency with other L4 runners.")
    parser.add_argument("--continue-on-chain-error", action="store_true", default=True)
    parser.add_argument("--stop-on-chain-error", action="store_true")
    parser.add_argument("--validate-event-trace", action="store_true", default=True)
    parser.add_argument("--skip-event-trace-validation", action="store_true")
    parser.add_argument("--execute", action="store_true", default=True)
    parser.add_argument("--no-execute", action="store_true")
    parser.add_argument("--code-agent", choices=["template", "opencode"], default="opencode")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default="deepseek-v4-flash")
    parser.add_argument("--opencode-repair-attempts", type=int, default=3)
    parser.add_argument("--skip-plan-agent", action="store_true")
    parser.add_argument("--plan-model", default="deepseek-v4-flash")
    parser.add_argument("--plan-url", default="https://api.deepseek.com/chat/completions")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--plan-timeout", type=float, default=300.0)
    args = parser.parse_args()
    if args.all_chains:
        args.chain_index = None

    run_dir = os.path.abspath(args.run_dir)
    l0_json = os.path.join(run_dir, "l0", "state.json")
    l3_json = os.path.join(run_dir, "l3", "chains.json")
    output_dir = os.path.abspath(args.output_dir or os.path.join(run_dir, "l4_physical_task"))

    if not os.path.exists(l0_json):
        raise FileNotFoundError(f"L0 state not found: {l0_json}")
    if not os.path.exists(l3_json):
        raise FileNotFoundError(f"L3 chains not found: {l3_json}")

    l4_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l4.py")
    l4_scenario_language_script = os.path.join(repo_root, "carla_smoke", "pipeline", "l4_scenario_language.py")
    intervention_script = os.path.join(repo_root, "carla_smoke", "pipeline", "run_l4_intervention_from_workdir.py")
    carla_python = args.carla_python or sys.executable

    if args.l4_backend == "safebench-intervention":
        command = [
            carla_python,
            intervention_script,
            "--run-dir",
            run_dir,
            "--output-dir",
            output_dir,
            "--l0-json",
            l0_json,
            "--l3-json",
            l3_json,
            "--carla-root",
            args.carla_root,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--frames",
            str(args.frames),
            "--save-every",
            str(args.save_every),
            "--local-trigger-frame",
            str(args.local_trigger_frame),
            "--pre-trigger-seconds",
            str(args.pre_trigger_seconds),
            "--source-timestep",
            str(args.source_timestep),
            "--plan-model",
            args.plan_model,
            "--plan-url",
            args.plan_url,
            "--api-key-env",
            args.api_key_env,
            "--plan-timeout",
            str(args.plan_timeout),
            "--intervention-agent",
            "opencode" if args.code_agent == "opencode" else "template",
            "--opencode-bin",
            args.opencode_bin,
            "--opencode-model",
            args.opencode_model,
            "--opencode-repair-attempts",
            str(args.opencode_repair_attempts),
        ]
        if args.skip_plan_agent:
            command.append("--skip-plan-agent")
        if args.env_file:
            command.extend(["--env-file", args.env_file])
        if args.chain_index is None:
            command.append("--all-chains")
            if args.continue_on_chain_error and not args.stop_on_chain_error:
                command.append("--continue-on-chain-error")
        else:
            command.extend(["--chain-index", str(args.chain_index)])
        if not args.validate_event_trace or args.skip_event_trace_validation:
            command.append("--skip-event-trace-validation")
    elif args.l4_backend == "code-agent":
        command = [
            carla_python,
            l4_script,
            l3_json,
            "--output-dir",
            output_dir,
            "--l0-json",
            l0_json,
            "--carla-root",
            args.carla_root,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--town",
            derive_town(run_dir, args.town),
            "--frames",
            str(args.frames),
            "--save-every",
            str(args.save_every),
            "--local-trigger-frame",
            str(args.local_trigger_frame),
            "--pre-trigger-seconds",
            str(args.pre_trigger_seconds),
            "--source-timestep",
            str(args.source_timestep),
            "--code-agent",
            args.code_agent,
            "--opencode-bin",
            args.opencode_bin,
            "--opencode-model",
            args.opencode_model,
            "--opencode-repair-attempts",
            str(args.opencode_repair_attempts),
            "--plan-model",
            args.plan_model,
            "--plan-url",
            args.plan_url,
            "--api-key-env",
            args.api_key_env,
            "--plan-timeout",
            str(args.plan_timeout),
        ]
        if args.skip_plan_agent:
            command.append("--skip-plan-agent")
        if args.env_file:
            command.extend(["--env-file", args.env_file])
        if not args.no_execute and args.execute:
            command.append("--execute")
        if args.chain_index is None:
            command.append("--all-chains")
            if args.continue_on_chain_error and not args.stop_on_chain_error:
                command.append("--continue-on-chain-error")
        else:
            command.extend(["--chain-index", str(args.chain_index)])
        if args.validate_event_trace and not args.skip_event_trace_validation:
            command.append("--validate-event-trace")
    else:
        command = [
            carla_python,
            l4_scenario_language_script,
            l3_json,
            "--output-dir",
            output_dir,
            "--l0-json",
            l0_json,
            "--carla-root",
            args.carla_root,
            "--carla-python",
            carla_python,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--timeout",
            str(args.plan_timeout),
            "--frames",
            str(args.frames),
            "--save-every",
            str(args.save_every),
            "--local-trigger-frame",
            str(args.local_trigger_frame),
            "--pre-trigger-seconds",
            str(args.pre_trigger_seconds),
            "--source-timestep",
            str(args.source_timestep),
            "--code-agent",
            "opencode",
            "--opencode-bin",
            args.opencode_bin,
            "--opencode-model",
            args.opencode_model,
            "--opencode-repair-attempts",
            str(args.opencode_repair_attempts),
            "--plan-model",
            args.plan_model,
            "--plan-url",
            args.plan_url,
            "--api-key-env",
            args.api_key_env,
            "--plan-timeout",
            str(args.plan_timeout),
        ]
        if args.skip_plan_agent:
            command.append("--skip-plan-agent")
        if args.env_file:
            command.extend(["--env-file", args.env_file])
        if not args.no_execute and args.execute:
            command.append("--execute")
        if args.chain_index is None:
            command.append("--all-chains")
            if args.continue_on_chain_error and not args.stop_on_chain_error:
                command.append("--continue-on-chain-error")
        else:
            command.extend(["--chain-index", str(args.chain_index)])
        if args.validate_event_trace and not args.skip_event_trace_validation:
            command.append("--validate-event-trace")

    run_command(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
