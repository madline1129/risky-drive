#!/usr/bin/env python3
"""Run L4 only by replaying the original SafeBench scene and intervening in-place."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

try:
    from l4 import (
        build_config,
        chain_output_dir,
        chains_from_data,
        copy_tree_contents,
        normalize_opencode_model_name,
        opencode_skills_dir,
        read_json,
        select_chain,
        validate_event_trace,
        validate_risk_image_layout,
        validate_risk_images,
        write_json,
    )
except ImportError:
    from .l4 import (
        build_config,
        chain_output_dir,
        chains_from_data,
        copy_tree_contents,
        normalize_opencode_model_name,
        opencode_skills_dir,
        read_json,
        select_chain,
        validate_event_trace,
        validate_risk_image_layout,
        validate_risk_images,
        write_json,
    )


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def run_command(command):
    print("\n$ " + " ".join(command))
    subprocess.run(command, check=True)


def run_command_capture(command):
    print("\n$ " + " ".join(command))
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.stdout:
        print(result.stdout, end="")
    return result.stdout or ""


def error_output(exc):
    output = getattr(exc, "output", None) or getattr(exc, "stdout", None)
    if output:
        return str(output)
    return repr(exc)


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


def prepare_opencode_intervention_workspace(args, repo_root, chain_dir, config_path, l0_json, scenic_file):
    workspace = os.path.join(chain_dir, "opencode_safebench_workspace")
    os.makedirs(workspace, exist_ok=True)

    workspace_config = os.path.join(workspace, "scenario_config.json")
    workspace_l0 = os.path.join(workspace, "l0_state.json")
    output_script = os.path.join(workspace, "generated_safebench_intervention.py")
    seed_script = os.path.join(repo_root, "carla_smoke", "scenes", "safebench_intervention_scene.py")

    shutil.copyfile(config_path, workspace_config)
    shutil.copyfile(l0_json, workspace_l0)
    shutil.copyfile(seed_script, output_script)
    shutil.copyfile(seed_script, os.path.join(workspace, "reference_safebench_intervention.py"))
    with open(os.path.join(workspace, "scenic_file.txt"), "w", encoding="utf-8") as f:
        f.write(os.path.abspath(scenic_file) + "\n")

    workspace_skills = os.path.join(workspace, ".opencode", "skills")
    copy_tree_contents(opencode_skills_dir(), workspace_skills)

    old_refs = os.path.join(workspace_skills, "l4-carla-codegen", "references")
    context_dir = os.path.join(workspace, "context")
    if os.path.isdir(old_refs):
        copy_tree_contents(old_refs, context_dir)

    with open(os.path.join(workspace, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write(
            "# OpenCode Workspace Instructions\n\n"
            "Use the `l4-safebench-intervention-codegen` skill for this workspace.\n"
            "Edit only `generated_safebench_intervention.py`.\n"
            "This backend must replay the original SafeBench/Scenic scene and perturb live actors in-place.\n"
            "Do not write a fresh-world L4 script, do not spawn a replacement ego, and do not call client.load_world.\n"
        )

    write_json(
        os.path.join(workspace, "opencode_inputs.json"),
        {
            "workspace_config": workspace_config,
            "l0_state": workspace_l0,
            "output_script": output_script,
            "seed_script": seed_script,
            "scenic_file": os.path.abspath(scenic_file),
            "skill": "l4-safebench-intervention-codegen",
        },
    )
    return workspace, workspace_config, output_script


def opencode_intervention_prompt(config_path, output_script, scenic_file):
    return f"""Use the l4-safebench-intervention-codegen skill.

Task:
- Read scenario_config.json at:
  {config_path}
- Read l0_state.json if it exists.
- Read scenic_file.txt. The SafeBench Scenic file to replay is:
  {scenic_file}
- Edit the SafeBench replay intervention script in place at exactly:
  {output_script}

Hard requirement:
- The generated script must perturb the original SafeBench/Scenic replayed scene.
- Do not create a fresh CARLA L4 world.
- Do not implement spawn_ego_near_l0.
- Do not call client.load_world.
- Do not spawn a replacement ego vehicle.
- Keep the ScenicSimulator replay flow intact.

What to edit:
- Improve actor matching for the primary risk object from scenario_config.risk_object_spec.
- Improve generated actor setup only when the scenario requires a generated primary object.
- Improve apply_intervention so the primary object physically performs the requested risk event.
- Improve event_trace fields so validation can prove the physical event happened.

Scenario rules:
- front_vehicle_brake: match the live SafeBench front vehicle and brake/decelerate it. No payloads, no walkers.
- side_vehicle_intrusion: match the live SafeBench side vehicle and move it laterally toward the ego lane.
- vulnerable_actor_intrusion: if risk_object_spec.primary_object.source is "l0_actor", match and perturb that original live vulnerable actor; only spawn a vulnerable actor when the primary object is explicitly generated. Follow risk_object_spec.geometry.path_world/start/end points.
- road_obstacle_intrusion: move/place the obstacle into the ego lane according to risk_object_spec.geometry.
- cargo_drop: make the payload the primary event.

Output requirements:
- Save top-level six-view montage images as risk_rgb_XXXX.png in --output-dir.
- Write --output-dir/event_trace.json with top-level frames list.
- Preserve the existing CLI.
- Keep the script self-contained enough to run from this workspace.
- Before finishing, ensure it would pass python -m py_compile and --help.
- Do not write Markdown. Edit only the requested Python file.
"""


def opencode_intervention_repair_prompt(config_path, output_script, error_output):
    return f"""The SafeBench intervention script failed.

Use the l4-safebench-intervention-codegen skill.

Scenario config:
  {config_path}

Script to fix:
  {output_script}

Execution or validation error:
{error_output}

Repair the existing SafeBench replay intervention script in place.
Do not switch back to a fresh-world generated_risk_scene.py design.
Do not add spawn_ego_near_l0, client.load_world, or replacement ego spawning.
Keep the ScenicSimulator replay flow and fix only actor matching, intervention logic, trace fields, or camera/output behavior.
Do not write Markdown. Edit only the requested Python file.
"""


def run_opencode_intervention(args, repo_root, chain_dir, config_path, l0_json, scenic_file):
    opencode_bin = shutil.which(args.opencode_bin)
    if not opencode_bin:
        raise RuntimeError(f"opencode binary not found: {args.opencode_bin}")

    workspace, workspace_config, output_script = prepare_opencode_intervention_workspace(
        args,
        repo_root,
        chain_dir,
        config_path,
        l0_json,
        scenic_file,
    )
    prompt = opencode_intervention_prompt(workspace_config, output_script, scenic_file)
    with open(os.path.join(workspace, "opencode_prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)
    command = [
        opencode_bin,
        "run",
        "--model",
        normalize_opencode_model_name(args.opencode_model),
        "--dir",
        workspace,
        prompt,
    ]
    run_command(command)
    if not os.path.exists(output_script):
        raise RuntimeError(f"opencode did not create expected script: {output_script}")
    return output_script


def validate_intervention_script(script_path):
    run_command_capture([sys.executable, "-m", "py_compile", script_path])
    run_command_capture([sys.executable, script_path, "--help"])


def run_intervention_script(args, script_path, config_path, l0_json, scenic_file, images_dir):
    command = [
        args.carla_python or sys.executable,
        script_path,
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
    run_command_capture(command)


def repair_opencode_intervention(args, config_path, script_path, error_output):
    opencode_bin = shutil.which(args.opencode_bin)
    if not opencode_bin:
        raise RuntimeError(f"opencode binary not found: {args.opencode_bin}")
    workspace = os.path.dirname(script_path)
    prompt = opencode_intervention_repair_prompt(
        os.path.join(workspace, "scenario_config.json"),
        script_path,
        error_output,
    )
    with open(os.path.join(workspace, "opencode_repair_prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)
    command = [
        opencode_bin,
        "run",
        "--model",
        normalize_opencode_model_name(args.opencode_model),
        "--dir",
        workspace,
        prompt,
    ]
    run_command(command)


def run_script_with_repair(args, config_path, script_path, l0_json, scenic_file, images_dir):
    last_error = ""
    for attempt in range(args.opencode_repair_attempts + 1):
        try:
            validate_intervention_script(script_path)
            run_intervention_script(args, script_path, config_path, l0_json, scenic_file, images_dir)
            image_count = validate_risk_images(images_dir)
            validate_risk_image_layout(config_path, images_dir)
            print(f"Validated risk images: {image_count} files under {os.path.abspath(images_dir)}")
            if args.validate_event_trace:
                trace_path = validate_event_trace(config_path, images_dir)
                print(f"Validated event trace: {os.path.abspath(trace_path)}")
            return
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            last_error = error_output(exc)
            if attempt >= args.opencode_repair_attempts:
                raise
            print(f"\nSafeBench intervention script failed; asking opencode to repair ({attempt + 1}/{args.opencode_repair_attempts}).")
            repair_opencode_intervention(args, config_path, script_path, last_error)


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
    if args.intervention_agent == "opencode":
        intervention_script = run_opencode_intervention(args, repo_root, chain_dir, config_path, l0_json, scenic_file)
        run_script_with_repair(args, config_path, intervention_script, l0_json, scenic_file, images_dir)
    else:
        intervention_script = os.path.join(repo_root, "carla_smoke", "scenes", "safebench_intervention_scene.py")
        run_intervention_script(args, intervention_script, config_path, l0_json, scenic_file, images_dir)
        image_count = validate_risk_images(images_dir)
        validate_risk_image_layout(config_path, images_dir)
        print(f"Validated risk images: {image_count} files under {os.path.abspath(images_dir)}")
        if args.validate_event_trace:
            validate_event_trace(config_path, images_dir)
    return {
        "chain_id": chain.get("id"),
        "output_dir": os.path.abspath(chain_dir),
        "scenario_config": os.path.abspath(config_path),
        "risk_images": os.path.abspath(images_dir),
        "intervention_script": os.path.abspath(intervention_script),
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
    parser.add_argument(
        "--intervention-agent",
        choices=["opencode", "template"],
        default="opencode",
        help="opencode edits the SafeBench replay intervention template; template runs the fixed fallback executor.",
    )
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default="deepseek-v4-pro")
    parser.add_argument("--opencode-repair-attempts", type=int, default=3)
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
