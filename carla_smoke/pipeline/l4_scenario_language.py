#!/usr/bin/env python3
"""L4 scenario-language backend: semantic primitives -> OpenCode-generated Scenic -> CARLA images."""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

try:
    from l4 import (
        DEFAULT_DEEPSEEK_MODEL,
        DEFAULT_DEEPSEEK_URL,
        build_config,
        chain_output_dir,
        chains_from_data,
        copy_tree_contents,
        normalize_opencode_model_name,
        opencode_skills_dir,
        read_json,
        select_chain,
        write_json,
    )
except ImportError:
    from .l4 import (
        DEFAULT_DEEPSEEK_MODEL,
        DEFAULT_DEEPSEEK_URL,
        build_config,
        chain_output_dir,
        chains_from_data,
        copy_tree_contents,
        normalize_opencode_model_name,
        opencode_skills_dir,
        read_json,
        select_chain,
        write_json,
    )


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def safe_get(data, *keys, default=None):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return current if current is not None else default


def actor_summary(actor):
    if not isinstance(actor, dict):
        return {}
    return {
        "source": actor.get("source"),
        "actor_id": actor.get("actor_id") or actor.get("id"),
        "type_id": actor.get("type_id"),
        "kind": actor.get("kind"),
        "role": actor.get("role") or actor.get("role_name"),
        "location": actor.get("location") or actor.get("initial_location"),
        "rotation": actor.get("rotation") or actor.get("initial_rotation"),
        "relative_position": actor.get("relative_position"),
        "relative_longitudinal_m": actor.get("relative_longitudinal_m"),
        "relative_lateral_m": actor.get("relative_lateral_m"),
        "distance_m": actor.get("distance_m"),
    }


def primitive(name, **fields):
    item = {"primitive": name}
    item.update({key: value for key, value in fields.items() if value is not None})
    return item


def build_semantic_primitives(config):
    scenario_type = safe_get(config, "carla_plan", "scenario_type", default="unknown")
    physical_task = config.get("physical_task") or {}
    risk_object_spec = config.get("risk_object_spec") or {}
    primary_actor = actor_summary(physical_task.get("primary_actor") or safe_get(risk_object_spec, "primary_object", default={}))
    scene = config.get("scene_reconstruction") or {}
    ego = actor_summary(scene.get("ego") or {})
    front_actor = actor_summary(scene.get("nearest_front_actor") or {})
    trigger_frame = safe_get(config, "physical_task", "action", "trigger_frame", default=config.get("trigger_frame", 20))

    primitives = [
        primitive(
            "set_scene_context",
            town=scene.get("preferred_town") or scene.get("source_map"),
            weather=scene.get("weather"),
            reconstruction_policy=config.get("reconstruction_policy"),
            spawn_policy=config.get("spawn_policy"),
        ),
        primitive(
            "spawn_ego",
            actor=ego,
            behavior="follow_lane",
            target_speed_mps=safe_get(config, "carla_plan", "actor_motion_plan", "ego", "target_speed_mps"),
        ),
    ]

    if front_actor:
        primitives.append(
            primitive(
                "spawn_actor_relative",
                role="front_or_occluder_actor",
                actor=front_actor,
                relative_to="ego",
                preserve_relative_geometry=True,
            )
        )

    primitives.append(
        primitive(
            "spawn_actor_relative",
            role="primary_risk_actor",
            actor=primary_actor,
            relative_to="ego",
            preserve_relative_geometry=True,
            absolute_pose_is_hint_only=True,
        )
    )

    if scenario_type == "front_vehicle_brake":
        primitives.extend(
            [
                primitive("follow_lane", actor="ego", until_frame=trigger_frame),
                primitive(
                    "front_vehicle_brake",
                    actor="primary_risk_actor",
                    trigger_frame=trigger_frame,
                    target_speed_mps=0.0,
                    must_be_visible=True,
                ),
                primitive("record_expectation", expectation="front vehicle decelerates or stops while ego is still approaching"),
            ]
        )
    elif scenario_type == "vulnerable_actor_intrusion":
        primitives.extend(
            [
                primitive("follow_lane", actor="ego", until_frame=trigger_frame),
                primitive(
                    "vulnerable_actor_intrusion",
                    actor="primary_risk_actor",
                    trigger_frame=trigger_frame,
                    path=safe_get(risk_object_spec, "geometry", default={}),
                    target="ego_lane",
                    must_cross_or_enter_ego_lane=True,
                ),
                primitive("record_expectation", expectation="walker/cyclist moves into ego driving space while ego is moving"),
            ]
        )
    elif scenario_type == "side_vehicle_intrusion":
        primitives.extend(
            [
                primitive("follow_lane", actor="ego", until_frame=trigger_frame),
                primitive(
                    "side_vehicle_intrusion",
                    actor="primary_risk_actor",
                    trigger_frame=trigger_frame,
                    target="ego_lane",
                    preserve_same_l0_actor=True,
                ),
            ]
        )
    elif scenario_type == "road_obstacle_intrusion":
        primitives.extend(
            [
                primitive("follow_lane", actor="ego", until_frame=trigger_frame),
                primitive(
                    "road_obstacle_intrusion",
                    actor="primary_risk_actor",
                    trigger_frame=trigger_frame,
                    target="ego_path",
                ),
            ]
        )
    elif scenario_type == "cargo_drop":
        primitives.extend(
            [
                primitive("follow_lane", actor="ego", until_frame=trigger_frame),
                primitive(
                    "cargo_drop",
                    actor="primary_risk_actor",
                    carrier=safe_get(risk_object_spec, "carrier_actor"),
                    trigger_frame=trigger_frame,
                    target="ego_path",
                ),
            ]
        )
    else:
        primitives.append(
            primitive(
                "apply_configured_primary_action",
                actor="primary_risk_actor",
                trigger_frame=trigger_frame,
                action=safe_get(physical_task, "action", default=safe_get(risk_object_spec, "action", default={})),
            )
        )

    return {
        "level": "L4SemanticPrimitives",
        "description": "Structured primitive graph used by OpenCode to generate Scenic scenario-language code.",
        "scenario_type": scenario_type,
        "source_l3_chain_id": config.get("source_l3_chain_id"),
        "trigger_frame": trigger_frame,
        "primary_actor": primary_actor,
        "semantic_primitives": primitives,
        "success_criteria": physical_task.get("success_criteria") or risk_object_spec.get("success_criteria"),
        "forbidden_substitutions": safe_get(risk_object_spec, "forbidden_substitutions", default=[]),
    }


def prepare_workspace(args, config_path, primitives_path):
    workspace = os.path.join(args.output_dir, "opencode_scenario_language_workspace")
    os.makedirs(workspace, exist_ok=True)
    workspace_config = os.path.join(workspace, "scenario_config.json")
    workspace_primitives = os.path.join(workspace, "semantic_primitives.json")
    output_scenic = os.path.join(workspace, "generated_risk_scene.scenic")
    shutil.copy2(config_path, workspace_config)
    shutil.copy2(primitives_path, workspace_primitives)
    if args.l0_json:
        shutil.copy2(args.l0_json, os.path.join(workspace, "l0_state.json"))
    else:
        write_json(os.path.join(workspace, "l0_state.json"), {})

    seed = """'''OpenCode must replace this seed with a complete Scenic scenario generated from semantic_primitives.json.'''\nTown = 'Town05'\nparam map = localPath(f'../maps/{Town}.xodr')\nparam carla_map = Town\nmodel scenic.simulators.carla.model\nEGO_MODEL = \"vehicle.lincoln.mkz_2017\"\n\n# TODO: implement ego, primary risk actor, and behavior from semantic_primitives.json.\n"""
    with open(output_scenic, "w", encoding="utf-8") as f:
        f.write(seed)

    workspace_skills = os.path.join(workspace, ".opencode", "skills")
    copy_tree_contents(opencode_skills_dir(), workspace_skills)
    references_dir = os.path.join(opencode_skills_dir(), "l4-scenario-language-codegen", "references")
    context_dir = os.path.join(workspace, "context")
    if os.path.isdir(references_dir):
        copy_tree_contents(references_dir, context_dir)
    with open(os.path.join(workspace, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write(
            "# OpenCode Workspace Instructions\n\n"
            "Use the l4-scenario-language-codegen skill.\n"
            "Edit only generated_risk_scene.scenic.\n"
            "The file must be executable by Scenic/CARLA.\n"
        )
    write_json(
        os.path.join(workspace, "opencode_inputs.json"),
        {
            "scenario_config": workspace_config,
            "semantic_primitives": workspace_primitives,
            "l0_state": os.path.join(workspace, "l0_state.json"),
            "output_scenic": output_scenic,
        },
    )
    return workspace, workspace_config, workspace_primitives, output_scenic


def opencode_prompt(config_path, primitives_path, output_scenic):
    return f"""Use the l4-scenario-language-codegen skill.

Task:
- Read scenario_config.json:
  {config_path}
- Read semantic_primitives.json:
  {primitives_path}
- Read l0_state.json and context/scenic_examples.md if present.
- Edit exactly this Scenic file in place:
  {output_scenic}

Requirements:
- Generate Scenario/Scenic language, not Python.
- Follow semantic_primitives.json as the hard primitive graph.
- Preserve scenario_config.carla_plan.scenario_type exactly.
- Preserve primary actor kind/type, ego-relative geometry, action, trigger timing, and forbidden substitutions.
- L0 absolute coordinates are hints; relative geometry is authoritative.
- The generated Scenic must run through carla_smoke/scenes/safebench_scenic_scene.py.
- Do not write Markdown. Do not ask questions. Edit only generated_risk_scene.scenic.
"""


def opencode_repair_prompt(config_path, primitives_path, output_scenic, error_output):
    return f"""The generated Scenic scenario failed during Scenic/CARLA execution.

Use the l4-scenario-language-codegen skill.

Scenario config:
  {config_path}

Semantic primitives:
  {primitives_path}

Scenic file to fix:
  {output_scenic}

Execution error:
{error_output}

Repair the Scenic file in place.
- Keep the same scenario_type and semantic primitive intent.
- Fix Scenic syntax/runtime issues.
- If a parameter used in range(...) can be float, cast it to int(...) or replace it with a fixed integer.
- Do not switch to Python code.
- Do not write Markdown. Edit only generated_risk_scene.scenic.
"""


def run_command(command, capture_output=False):
    print("\n$ " + " ".join(command))
    if capture_output:
        result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.stdout:
            print(result.stdout)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, command, output=result.stdout)
        return result
    subprocess.run(command, check=True)
    return None


def error_text(exc):
    output = getattr(exc, "output", None)
    if output:
        return str(output)
    return repr(exc)


def run_opencode(args, config_path, primitives_path, output_scenic):
    opencode_bin = shutil.which(args.opencode_bin)
    if not opencode_bin:
        raise RuntimeError(f"opencode binary not found: {args.opencode_bin}")
    prompt = opencode_prompt(config_path, primitives_path, output_scenic)
    prompt_path = os.path.join(os.path.dirname(output_scenic), "opencode_prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    run_command(
        [
            opencode_bin,
            "run",
            "--model",
            normalize_opencode_model_name(args.opencode_model),
            "--dir",
            os.path.dirname(output_scenic),
            prompt,
        ],
        capture_output=True,
    )
    if not os.path.exists(output_scenic):
        raise RuntimeError(f"opencode completed but did not create expected Scenic file: {output_scenic}")


def repair_opencode(args, config_path, primitives_path, output_scenic, error_output):
    opencode_bin = shutil.which(args.opencode_bin)
    if not opencode_bin:
        raise RuntimeError(f"opencode binary not found: {args.opencode_bin}")
    prompt = opencode_repair_prompt(config_path, primitives_path, output_scenic, error_output)
    prompt_path = os.path.join(os.path.dirname(output_scenic), "opencode_repair_prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    run_command(
        [
            opencode_bin,
            "run",
            "--model",
            normalize_opencode_model_name(args.opencode_model),
            "--dir",
            os.path.dirname(output_scenic),
            prompt,
        ],
        capture_output=True,
    )


def write_event_trace_from_states(images_dir, config, primitives):
    frames = []
    for path in sorted(glob.glob(os.path.join(images_dir, "state_*.json"))):
        state = read_json(path)
        ego = state.get("ego") or {}
        frames.append(
            {
                "frame": state.get("frame"),
                "ego_speed_mps": ego.get("speed_mps"),
                "ego_location": ego.get("location"),
                "actor_count": len(state.get("actors") or []),
                "image_file": safe_get(state, "source", "image_file"),
            }
        )
    trace = {
        "scenario_type": safe_get(config, "carla_plan", "scenario_type"),
        "execution_backend": "scenario_language_opencode_scenic",
        "source_l3_chain_id": config.get("source_l3_chain_id"),
        "semantic_primitives_file": "semantic_primitives.json",
        "generated_scenic_file": "opencode_scenario_language_workspace/generated_risk_scene.scenic",
        "frames": frames,
        "note": "Trace is reconstructed from Scenic capture state files; semantic validation is intentionally lightweight for the scenario-language prototype.",
        "primitive_count": len(primitives.get("semantic_primitives") or []),
    }
    write_json(os.path.join(images_dir, "event_trace.json"), trace)
    return trace


def postprocess_images(images_dir):
    for path in sorted(glob.glob(os.path.join(images_dir, "rgb_*.png"))):
        target = os.path.join(images_dir, "risk_" + os.path.basename(path))
        if not os.path.exists(target):
            shutil.copy2(path, target)


def run_scenic_capture(args, scenic_file, images_dir):
    scene_script = os.path.join(repo_root_from_this_file(), "carla_smoke", "scenes", "safebench_scenic_scene.py")
    command = [
        args.carla_python or sys.executable,
        scene_script,
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
        "surround",
        "--output-dir",
        images_dir,
        "--clean-output",
        "--no-try-next-on-failure",
    ]
    run_command(command, capture_output=True)


def run_scenic_with_repair(args, config_path, primitives_path, scenic_file, images_dir):
    last_error = ""
    for attempt in range(args.opencode_repair_attempts + 1):
        try:
            run_scenic_capture(args, scenic_file, images_dir)
            return
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            last_error = error_text(exc)
            if attempt >= args.opencode_repair_attempts:
                raise
            print(f"\nAsking opencode to repair Scenic file ({attempt + 1}/{args.opencode_repair_attempts}).")
            repair_opencode(args, config_path, primitives_path, scenic_file, last_error)


def execute_chain(args, chain, l0_state, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    config = build_config(
        chain,
        l0_state=l0_state,
        l0_json_path=args.l0_json,
        l4_frames=args.frames,
        local_trigger_frame=args.local_trigger_frame,
        pre_trigger_seconds=args.pre_trigger_seconds,
        source_timestep=args.source_timestep,
        plan_agent_args=args,
    )
    config["execution_backend"] = "scenario_language"
    config["executor"] = "carla_smoke/scenes/safebench_scenic_scene.py"
    config_path = os.path.join(output_dir, "scenario_config.json")
    write_json(config_path, config)

    primitives = build_semantic_primitives(config)
    primitives_path = os.path.join(output_dir, "semantic_primitives.json")
    write_json(primitives_path, primitives)

    workspace, workspace_config, workspace_primitives, output_scenic = prepare_workspace(args, config_path, primitives_path)
    run_opencode(args, workspace_config, workspace_primitives, output_scenic)

    images_dir = os.path.join(output_dir, "risk_images")
    if args.execute:
        run_scenic_with_repair(args, workspace_config, workspace_primitives, output_scenic, images_dir)
        postprocess_images(images_dir)
        trace = write_event_trace_from_states(images_dir, config, primitives)
        if args.validate_event_trace and not trace["frames"]:
            raise RuntimeError("Scenario-language execution produced no event_trace frames.")
    else:
        print("Scenario-language execution skipped. Add --execute to run Scenic/CARLA.")

    return {
        "chain_id": chain.get("id"),
        "source_l2_id": chain.get("parent_l2_id"),
        "output_dir": os.path.abspath(output_dir),
        "scenario_config": os.path.abspath(config_path),
        "semantic_primitives": os.path.abspath(primitives_path),
        "generated_scenic": os.path.abspath(output_scenic),
        "risk_images": os.path.abspath(images_dir),
        "scenario_type": safe_get(config, "carla_plan", "scenario_type"),
    }


def main():
    parser = argparse.ArgumentParser(
        description="L4 semantic-primitives backend: use OpenCode to generate Scenic scenario-language code and run it in CARLA."
    )
    parser.add_argument("l3_json", help="Path to l3/chains.json.")
    parser.add_argument("--chain-index", type=int, default=0)
    parser.add_argument("--all-chains", action="store_true")
    parser.add_argument("--continue-on-chain-error", action="store_true")
    parser.add_argument("--output-dir", default="carla_smoke/workdir/manual/l4_scenario_language")
    parser.add_argument("--l0-json", default=None)
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/CARLA")
    parser.add_argument("--carla-python", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--scene-sample-attempts", type=int, default=20)
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--local-trigger-frame", type=int, default=20)
    parser.add_argument("--pre-trigger-seconds", type=float, default=2.0)
    parser.add_argument("--source-timestep", type=float, default=0.05)
    parser.add_argument("--warmup-ticks", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timestep", type=float, default=0.05)
    parser.add_argument("--ego-speed-difference", type=float, default=-5.0)
    parser.add_argument("--weather", default="ClearNoon")
    parser.add_argument("--execute", dest="execute", action="store_true", default=True)
    parser.add_argument("--no-execute", dest="execute", action="store_false")
    parser.add_argument("--code-agent", choices=["opencode"], default="opencode")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default="deepseek-v4-pro")
    parser.add_argument("--opencode-repair-attempts", type=int, default=3)
    parser.add_argument("--skip-plan-agent", action="store_true")
    parser.add_argument("--plan-model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--plan-url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--plan-timeout", type=float, default=300.0)
    parser.add_argument("--validate-event-trace", action="store_true")
    args = parser.parse_args()

    chains_data = read_json(args.l3_json)
    l0_state = read_json(args.l0_json) if args.l0_json else None

    if args.all_chains:
        chains = chains_from_data(chains_data)
        os.makedirs(args.output_dir, exist_ok=True)
        results = []
        for index, chain in enumerate(chains, start=1):
            print(f"\n=== L4 scenario-language chain {index}/{len(chains)}: {chain.get('id', index)} ===")
            original_output_dir = args.output_dir
            args.output_dir = chain_output_dir(original_output_dir, chain, index, True)
            try:
                results.append(execute_chain(args, chain, l0_state, args.output_dir))
            except Exception as exc:
                if not args.continue_on_chain_error:
                    raise
                results.append(
                    {
                        "chain_id": chain.get("id"),
                        "source_l2_id": chain.get("parent_l2_id"),
                        "output_dir": os.path.abspath(args.output_dir),
                        "scenario_type": safe_get(chain, "carla_plan", "scenario_type"),
                        "error": repr(exc),
                    }
                )
                print(f"WARNING: L4 scenario-language chain failed, continuing: {exc}", file=sys.stderr)
            finally:
                args.output_dir = original_output_dir
        write_json(
            os.path.join(args.output_dir, "l4_scenario_language_manifest.json"),
            {
                "mode": "all_chains",
                "source_l3_file": os.path.abspath(args.l3_json),
                "chain_count": len(results),
                "results": results,
            },
        )
    else:
        chain = select_chain(chains_data, args.chain_index)
        execute_chain(args, chain, l0_state, args.output_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
