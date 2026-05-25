#!/usr/bin/env python3
"""Batch-generate L4 Scenic scenes and evaluate them with SafeBench metrics."""

import argparse
import csv
import glob
import json
import os
import pickle
import shutil
import subprocess
import sys
from datetime import datetime

try:
    from deepseek_client import DEFAULT_API_KEY_ENV, DEFAULT_DEEPSEEK_MODEL, DEFAULT_DEEPSEEK_URL, DeepSeekError, get_api_key
except ImportError:
    from .deepseek_client import DEFAULT_API_KEY_ENV, DEFAULT_DEEPSEEK_MODEL, DEFAULT_DEEPSEEK_URL, DeepSeekError, get_api_key


METRIC_ALIASES = {
    "CR": "collision_rate",
    "RR": "avg_red_light_freq",
    "SS": "avg_stop_sign_freq",
    "OR": "out_of_road_length",
    "RF": "route_following_stability",
    "Comp": "route_completion",
    "TS": "avg_time_spent",
    "ACC": "avg_acceleration",
    "YV": "avg_yaw_velocity",
    "LI": "avg_lane_invasion_freq",
}

METRIC_DIRECTIONS = {
    "CR": "up",
    "RR": "up",
    "SS": "up",
    "OR": "up",
    "RF": "down",
    "Comp": "down",
    "TS": "up",
    "ACC": "up",
    "YV": "up",
    "LI": "up",
}


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def redact_command(command):
    redacted = []
    hide_next = False
    for item in command:
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        redacted.append(str(item))
        if item == "--api-key":
            hide_next = True
    return redacted


def run_command(command, continue_on_error=False):
    print("\n$ " + " ".join(redact_command(command)))
    completed = subprocess.run(command)
    if completed.returncode and not continue_on_error:
        completed.check_returncode()
    return completed.returncode


def validate_generation_api_key(args):
    if args.skip_generation:
        return
    try:
        get_api_key(args.api_key_env, args.env_file, args.api_key)
    except DeepSeekError as exc:
        raise SystemExit(
            f"{exc}\n"
            "L4 generation needs the API key and cannot produce Scenic scripts without it. "
            "Set the key before launching, e.g. `export AIHUBMIX_API_KEY=...`, "
            "put it in `.env`, or pass `--api-key`."
        ) from exc


def parse_scenario_indices(args):
    if args.scenario_indices:
        indexes = []
        for item in args.scenario_indices.split(","):
            item = item.strip()
            if item:
                indexes.append(int(item))
        return indexes
    return list(range(args.start_scenario_index, args.start_scenario_index + args.num_source_scenes))


def safe_scene_name(source_index, result_index, chain_id):
    raw = f"sb{source_index:03d}_{result_index:02d}_{chain_id or 'chain'}"
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw)


def copy_generated_scenics(generation_runs_dir, eval_scenic_dir):
    os.makedirs(eval_scenic_dir, exist_ok=True)
    copied = []
    failures = []
    manifests = sorted(glob.glob(os.path.join(generation_runs_dir, "source_*", "l4", "l4_scenario_language_manifest.json")))
    for manifest_path in manifests:
        source_dir = os.path.dirname(os.path.dirname(manifest_path))
        source_name = os.path.basename(source_dir)
        try:
            source_index = int(source_name.split("_", 1)[1])
        except (IndexError, ValueError):
            source_index = len(copied)
        manifest = read_json(manifest_path)
        for result_index, result in enumerate(manifest.get("results") or [], start=1):
            scenic_path = result.get("generated_scenic")
            if result.get("error") or not scenic_path or not os.path.exists(scenic_path):
                failures.append(
                    {
                        "source_index": source_index,
                        "chain_id": result.get("chain_id"),
                        "error": result.get("error") or f"missing generated_scenic: {scenic_path}",
                    }
                )
                continue
            stem = safe_scene_name(source_index, result_index, result.get("chain_id"))
            destination = os.path.join(eval_scenic_dir, f"{stem}.scenic")
            shutil.copy2(scenic_path, destination)
            copied.append(
                {
                    "source_index": source_index,
                    "chain_id": result.get("chain_id"),
                    "scenario_type": result.get("scenario_type"),
                    "source_scenic": scenic_path,
                    "eval_scenic": destination,
                    "behavior": stem,
                }
            )
    return copied, failures


def write_dynamic_scenario_params(eval_scenic_dir, copied_scenes):
    params = {}
    for scene in copied_scenes:
        params[f"OPT_{scene['behavior']}"] = {
            "select_id": [0],
            "opt_time_0": {},
        }
    write_json(os.path.join(eval_scenic_dir, "dynamic_scenario.json"), params)


def load_pickle(path):
    try:
        import joblib

        return joblib.load(path)
    except Exception:
        with open(path, "rb") as f:
            return pickle.load(f)


def collect_eval_outputs(eval_output_root):
    result_paths = sorted(glob.glob(os.path.join(eval_output_root, "**", "eval_results", "OPT_*_results.pkl"), recursive=True))
    record_paths = sorted(glob.glob(os.path.join(eval_output_root, "**", "eval_results", "OPT_*_records.pkl"), recursive=True))
    per_scene = []
    for result_path in result_paths:
        behavior = os.path.basename(result_path)
        behavior = behavior[len("OPT_") : -len("_results.pkl")]
        scores = load_pickle(result_path)
        records_path = result_path[:-len("_results.pkl")] + "_records.pkl"
        record_load_error = None
        records = {}
        if os.path.exists(records_path):
            try:
                records = load_pickle(records_path)
            except Exception as exc:
                record_load_error = repr(exc)
        row = {
            "behavior": behavior,
            "records": len(records) if records else 1,
            "record_load_error": record_load_error,
        }
        for alias, field in METRIC_ALIASES.items():
            row[alias] = scores.get(field)
            row[field] = scores.get(field)
        per_scene.append(row)
    return result_paths, record_paths, per_scene


def summarize_scores(per_scene):
    if not per_scene:
        return {alias: None for alias in METRIC_ALIASES}
    summary = {}
    for alias, field in METRIC_ALIASES.items():
        weighted_total = 0.0
        weight_sum = 0
        for row in per_scene:
            value = row.get(alias)
            weight = int(row.get("records") or 1)
            if value is None:
                continue
            weighted_total += float(value) * weight
            weight_sum += weight
        value = weighted_total / weight_sum if weight_sum else None
        summary[alias] = value
        summary[field] = value
    return summary


def write_summary_csv(path, per_scene):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fields = ["behavior", "records"] + list(METRIC_ALIASES.keys()) + list(METRIC_ALIASES.values())
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in per_scene:
            writer.writerow({field: row.get(field) for field in fields})


def generation_command(args, source_index, run_dir):
    repo_root = repo_root_from_this_file()
    run_safebench = os.path.join(repo_root, "carla_smoke", "pipeline", "run_safebench.py")
    carla_python = args.carla_python or sys.executable
    command = [
        sys.executable,
        run_safebench,
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
        "--scenic-dir",
        args.scenic_dir,
        "--scenario-index",
        str(source_index),
        "--scene-sample-attempts",
        str(args.scene_sample_attempts),
        "--frames",
        str(args.frames),
        "--save-every",
        str(args.save_every),
        "--sample-count",
        str(args.sample_count),
        "--single-random-frame",
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
        os.path.dirname(run_dir),
        "--run-id",
        os.path.basename(run_dir),
        "--model",
        args.model,
        "--deepseek-url",
        args.deepseek_url,
        "--api-key-env",
        args.api_key_env,
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
        "--l4-local-trigger-frame",
        str(args.l4_local_trigger_frame),
        "--l4-pre-trigger-seconds",
        str(args.l4_pre_trigger_seconds),
        "--l4-all-chains",
        "--continue-on-chain-error",
    ]
    if args.env_file:
        command.extend(["--env-file", args.env_file])
    if args.api_key:
        command.extend(["--api-key", args.api_key])
    if args.validate_event_trace:
        command.append("--validate-event-trace")
    return command


def run_generation(args, generation_runs_dir, source_indices):
    failures = []
    for source_index in source_indices:
        run_dir = os.path.join(generation_runs_dir, f"source_{source_index:03d}")
        returncode = run_command(
            generation_command(args, source_index, run_dir),
            continue_on_error=args.continue_on_generation_error,
        )
        if returncode:
            failures.append({"source_index": source_index, "returncode": returncode, "run_dir": run_dir})
    return failures


def run_eval_phase(args):
    from safebench.util.run_util import load_config
    from safebench.util.torch_util import set_seed, set_torch_variable
    from safebench.scenic_runner_dynamic import ScenicRunner
    import torch

    repo_root = repo_root_from_this_file()
    if args.device == "auto":
        args.device = "cuda:0" if torch.cuda.is_available() else "cpu"
    set_torch_variable(args.device)
    torch.set_num_threads(args.threads)
    set_seed(args.seed)

    agent_config = load_config(os.path.join(repo_root, "safebench", "agent", "config", args.agent_cfg))
    agent_config["policy_name"] = args.test_policy
    agent_config["load_dir"] = os.path.join(agent_config["load_dir"], "dynamic_scenario")

    scenario_config = load_config(os.path.join(repo_root, "safebench", "scenario", "config", args.scenario_cfg))
    scenario_config.update(vars(args))
    scenario_config["scenic_dir"] = args.eval_scenic_dir
    scenario_config["num_scenario"] = 1
    scenario_config["sample_num"] = 1
    scenario_config["opt_step"] = 1
    scenario_config["select_num"] = 1
    scenario_config["mode"] = "eval"
    scenario_config["continue_agent_training"] = False
    scenario_config["continue_scenario_training"] = False

    agent_config.update(vars(args))
    output_dir = os.path.relpath(args.eval_output_dir, repo_root)
    scenario_config["output_dir"] = output_dir
    agent_config["output_dir"] = output_dir
    scenario_config["exp_name"] = args.eval_exp_name
    agent_config["exp_name"] = args.eval_exp_name

    runner = ScenicRunner(agent_config, scenario_config)
    runner.run(args.test_epoch)


def eval_command(args, eval_scenic_dir, eval_output_dir, eval_exp_name):
    carla_python = args.carla_python or sys.executable
    command = [
        carla_python,
        os.path.abspath(__file__),
        "--_eval-only",
        "--eval-scenic-dir",
        eval_scenic_dir,
        "--eval-output-dir",
        eval_output_dir,
        "--eval-exp-name",
        eval_exp_name,
        "--agent-cfg",
        args.agent_cfg,
        "--scenario-cfg",
        args.scenario_cfg,
        "--test-policy",
        args.test_policy,
        "--test-epoch",
        str(args.test_epoch),
        "--carla-root",
        args.carla_root,
        "--port",
        str(args.port),
        "--tm-port",
        str(args.tm_port),
        "--seed",
        str(args.seed),
        "--threads",
        str(args.threads),
        "--device",
        args.device,
        "--max-episode-step",
        str(args.max_episode_step),
        "--fixed-delta-seconds",
        str(args.fixed_delta_seconds),
    ]
    if args.auto_ego:
        command.append("--auto-ego")
    if not args.render:
        command.append("--no-render")
    if args.save_video:
        command.append("--save-video")
    return command


def main():
    repo_root = repo_root_from_this_file()
    default_carla_root = os.environ.get("CARLA_ROOT", "/mnt/data2/congfeng/CARLA")
    default_workdir_root = os.path.join(repo_root, "carla_smoke", "workdir", "safebench_l4_experiment")
    default_scenic_dir = os.path.join(repo_root, "safebench", "scenario", "scenario_data", "scenic_data", "dynamic_scenario")

    parser = argparse.ArgumentParser(description="Generate L4 Scenic scenes from SafeBench sources and evaluate them with SafeBench.")
    parser.add_argument("--_eval-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--carla-root", default=default_carla_root)
    parser.add_argument("--carla-python", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2001)
    parser.add_argument("--tm-port", type=int, default=8002)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--scenic-dir", default=default_scenic_dir)
    parser.add_argument("--start-scenario-index", type=int, default=0)
    parser.add_argument("--num-source-scenes", type=int, default=10)
    parser.add_argument("--scenario-indices", default=None, help="Comma-separated SafeBench scenario indexes. Overrides range selection.")
    parser.add_argument("--scene-sample-attempts", type=int, default=20)
    parser.add_argument("--frames", type=int, default=600)
    parser.add_argument("--save-every", type=int, default=20)
    parser.add_argument("--sample-count", type=int, default=1)
    parser.add_argument("--camera-mode", choices=["front", "surround"], default="surround")
    parser.add_argument("--timestep", type=float, default=0.05)
    parser.add_argument("--warmup-ticks", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--ego-speed-difference", type=float, default=-5.0)
    parser.add_argument("--weather", default="ClearNoon")
    parser.add_argument("--workdir-root", default=default_workdir_root)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--deepseek-url", "--llm-url", dest="deepseek_url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--opencode-repair-attempts", type=int, default=3)
    parser.add_argument("--l4-frames", type=int, default=180)
    parser.add_argument("--l4-save-every", type=int, default=5)
    parser.add_argument("--l4-local-trigger-frame", type=int, default=20)
    parser.add_argument("--l4-pre-trigger-seconds", type=float, default=2.0)
    parser.add_argument("--validate-event-trace", action="store_true", default=True)
    parser.add_argument("--skip-event-trace-validation", dest="validate_event_trace", action="store_false")
    parser.add_argument("--continue-on-generation-error", action="store_true", default=True)
    parser.add_argument("--stop-on-generation-error", dest="continue_on_generation_error", action="store_false")
    parser.add_argument("--skip-generation", action="store_true")
    parser.add_argument("--skip-eval", action="store_true")

    parser.add_argument("--agent-cfg", default="adv_scenic.yaml")
    parser.add_argument("--scenario-cfg", default="dynamic_scenic.yaml")
    parser.add_argument("--test-policy", default="sac")
    parser.add_argument("--test-epoch", type=int, default=None)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-episode-step", type=int, default=300)
    parser.add_argument("--fixed-delta-seconds", type=float, default=0.1)
    parser.add_argument("--render", dest="render", action="store_true", default=True)
    parser.add_argument("--no-render", dest="render", action="store_false")
    parser.add_argument("--save-video", action="store_true")
    parser.add_argument("--auto-ego", action="store_true")
    parser.add_argument("--eval-scenic-dir", default=None)
    parser.add_argument("--eval-output-dir", default=None)
    parser.add_argument("--eval-exp-name", default=None)
    args = parser.parse_args()

    if args._eval_only:
        run_eval_phase(args)
        return 0

    if not args.skip_eval and args.agent_cfg == "adv_scenic.yaml" and args.test_policy == "sac" and args.test_epoch is None:
        raise ValueError("--test-epoch is required when evaluating adv_scenic.yaml with sac.")
    validate_generation_api_key(args)

    run_id = args.run_id or timestamp()
    experiment_dir = os.path.abspath(os.path.join(args.workdir_root, run_id))
    generation_runs_dir = os.path.join(experiment_dir, "generation_runs")
    eval_scenic_dir = os.path.join(experiment_dir, "safebench_dynamic_scenic")
    eval_output_dir = os.path.join(experiment_dir, "safebench_eval_log")
    eval_exp_name = f"l4_batch_{run_id}"
    os.makedirs(experiment_dir, exist_ok=True)

    source_indices = parse_scenario_indices(args)
    generation_failures = []
    if not args.skip_generation:
        generation_failures = run_generation(args, generation_runs_dir, source_indices)

    copied_scenes, l4_failures = copy_generated_scenics(generation_runs_dir, eval_scenic_dir)
    write_dynamic_scenario_params(eval_scenic_dir, copied_scenes)

    eval_returncode = None
    if copied_scenes and not args.skip_eval:
        eval_returncode = run_command(eval_command(args, eval_scenic_dir, eval_output_dir, eval_exp_name))
    elif not copied_scenes:
        print("No generated Scenic scripts were available for SafeBench evaluation.", file=sys.stderr)

    result_paths, record_paths, per_scene = collect_eval_outputs(eval_output_dir)
    metric_summary = summarize_scores(per_scene)
    source_with_success = sorted({scene["source_index"] for scene in copied_scenes})
    evaluated_scene_count = sum(int(row.get("records") or 1) for row in per_scene)
    collision_count = sum(float(row.get("CR") or 0.0) * int(row.get("records") or 1) for row in per_scene)
    sources_with_collision = set()
    behavior_to_source = {scene["behavior"]: scene["source_index"] for scene in copied_scenes}
    for row in per_scene:
        if float(row.get("CR") or 0.0) > 0.0 and row.get("behavior") in behavior_to_source:
            sources_with_collision.add(behavior_to_source[row["behavior"]])

    summary = {
        "run_id": run_id,
        "experiment_dir": experiment_dir,
        "source_indices": source_indices,
        "source_scenario_count": len(source_indices),
        "source_scenario_success_count": len(source_with_success),
        "successful_generated_scenic_count": len(copied_scenes),
        "evaluated_scene_count": evaluated_scene_count,
        "collision_count": collision_count,
        "source_level_collision_rate": len(sources_with_collision) / len(source_indices) if source_indices else None,
        "safe_bench_result_file_count": len(result_paths),
        "safe_bench_record_file_count": len(record_paths),
        "metrics": metric_summary,
        "metric_aliases": METRIC_ALIASES,
        "metric_directions": METRIC_DIRECTIONS,
        "generated_scenes": copied_scenes,
        "per_scene_results": per_scene,
        "generation_failures": generation_failures,
        "l4_chain_failures": l4_failures,
        "eval_returncode": eval_returncode,
    }
    write_json(os.path.join(experiment_dir, "summary.json"), summary)
    write_summary_csv(os.path.join(experiment_dir, "summary.csv"), per_scene)

    print("\nSafeBench L4 experiment finished.")
    print(f"Experiment dir: {experiment_dir}")
    print(f"Successful generated Scenic scripts: {len(copied_scenes)}")
    print(f"Evaluated SafeBench scenes: {evaluated_scene_count}")
    for alias in ["CR", "RR", "SS", "OR", "RF", "Comp", "TS", "ACC", "YV", "LI"]:
        print(f"{alias}: {metric_summary.get(alias)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
