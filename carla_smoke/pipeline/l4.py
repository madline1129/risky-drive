#!/usr/bin/env python3
"""Code-agent stage for L4: turn an L3 CARLA plan into executable risk-scene images."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap

REPAIR_ATTEMPTS = 3


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def opencode_skills_dir():
    return os.path.join(repo_root_from_this_file(), "carla_smoke", "opencode_skills")


def reference_executor_path():
    return os.path.join(repo_root_from_this_file(), "carla_smoke", "scenes", "risk_event_scene.py")


def select_chain(chains_data, index):
    chains = chains_data.get("initial_accident_chains", [])
    if not chains:
        raise ValueError("No initial_accident_chains found in L3 JSON.")
    if index < 0 or index >= len(chains):
        raise ValueError(f"chain-index {index} out of range; available chains: {len(chains)}")
    return chains[index]


def chains_from_data(chains_data):
    chains = chains_data.get("initial_accident_chains", [])
    if not chains:
        raise ValueError("No initial_accident_chains found in L3 JSON.")
    return chains


def chain_output_dir(base_output_dir, chain, index, all_chains):
    if not all_chains:
        return base_output_dir
    chain_id = str(chain.get("id") or f"chain_{index:02d}")
    safe_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in chain_id)
    return os.path.join(base_output_dir, f"{index:02d}_{safe_id}")


def compact_l0_scene(l0_state):
    if not isinstance(l0_state, dict):
        return None
    source = l0_state.get("source", {})
    road = l0_state.get("road", {})
    ego = l0_state.get("ego", {})
    actors = l0_state.get("actors", [])
    nearest_front = l0_state.get("nearest_front_actor")
    source_map = source.get("map") or road.get("map")
    preferred_town = os.path.basename(str(source_map)) if source_map else None
    return {
        "source_frame": source.get("frame"),
        "source_image_file": source.get("image_file"),
        "source_map": source_map,
        "preferred_town": preferred_town,
        "ego": {
            "type_id": ego.get("type_id"),
            "speed_mps": ego.get("speed_mps"),
            "speed_kmh": ego.get("speed_kmh"),
            "location": ego.get("location"),
            "rotation": ego.get("rotation"),
            "road": ego.get("road"),
        },
        "weather": l0_state.get("weather", {}),
        "nearest_front_actor": nearest_front,
        "actors": actors[:20] if isinstance(actors, list) else [],
        "summary": l0_state.get("summary", {}),
    }


def normalize_l4_plan(plan):
    if not isinstance(plan, dict):
        plan = {}
    plan = dict(plan)
    scenario_type = plan.get("scenario_type")
    if scenario_type not in {
        "cargo_drop",
        "front_vehicle_brake",
        "vulnerable_actor_intrusion",
        "road_obstacle_intrusion",
    }:
        scenario_type = "road_obstacle_intrusion"
    trigger_frame = int(plan.get("trigger_frame", 45) or 45)

    if scenario_type == "front_vehicle_brake":
        normalized = {
            "scenario_type": scenario_type,
            "target_actor": plan.get("target_actor", "front_vehicle"),
            "trigger_frame": trigger_frame,
            "brake_intensity": float(plan.get("brake_intensity", 1.0)),
            "deceleration_mps2": float(plan.get("deceleration_mps2", 6.0)),
            "target_speed_mps": float(plan.get("target_speed_mps", 0.0)),
            "expected_visual_result": plan.get(
                "expected_visual_result",
                "前车在自车前方突然减速或接近停止，自车前向距离快速压缩",
            ),
        }
        normalized["actor_motion_plan"] = plan.get("actor_motion_plan") or default_actor_motion_plan(normalized)
        return normalized

    if scenario_type == "vulnerable_actor_intrusion":
        normalized = {
            "scenario_type": scenario_type,
            "actor_type": plan.get("actor_type", "walker"),
            "trigger_frame": trigger_frame,
            "spawn_relative_to": plan.get("spawn_relative_to", "ego_lane_right"),
            "start_position": plan.get("start_position", {"x": 18.0, "y": 4.0, "z": 0.2}),
            "crossing_direction": plan.get("crossing_direction", "right_to_left"),
            "speed_mps": float(plan.get("speed_mps", 2.2)),
            "expected_visual_result": plan.get(
                "expected_visual_result",
                "弱势交通参与者从侧前方侵入自车行驶空间",
            ),
        }
        normalized["actor_motion_plan"] = plan.get("actor_motion_plan") or default_actor_motion_plan(normalized)
        return normalized

    if scenario_type == "cargo_drop":
        normalized = {
            "scenario_type": scenario_type,
            "target_actor": plan.get("target_actor", "front_truck"),
            "object_type": plan.get("object_type", "metal_pipe"),
            "object_count": int(plan.get("object_count", 5)),
            "trigger_frame": trigger_frame,
            "spawn_relative_to": plan.get("spawn_relative_to", "front_truck"),
            "initial_position": plan.get("initial_position", {"x": -3.2, "y": 0.0, "z": 2.4}),
            "motion": plan.get(
                "motion",
                {
                    "mode": "scripted_projectile",
                    "direction": "toward_ego",
                    "back_speed_mps": 8.0,
                    "lateral_drift_mps": 0.2,
                    "gravity": True,
                },
            ),
            "expected_visual_result": plan.get(
                "expected_visual_result",
                "货物/障碍物从前方车辆后部进入自车前方区域",
            ),
        }
        normalized["actor_motion_plan"] = plan.get("actor_motion_plan") or default_actor_motion_plan(normalized)
        return normalized

    normalized = {
        "scenario_type": "road_obstacle_intrusion",
        "object_type": plan.get("object_type", "road_obstacle"),
        "trigger_frame": trigger_frame,
        "spawn_relative_to": plan.get("spawn_relative_to", "front_of_ego"),
        "initial_position": plan.get("initial_position", {"x": 14.0, "y": 0.0, "z": 0.4}),
        "motion": plan.get(
            "motion",
            {
                "mode": "static_or_slow_intrusion",
                "direction": "into_ego_lane",
                "lateral_drift_mps": 0.5,
                "gravity": False,
            },
        ),
        "expected_visual_result": plan.get("expected_visual_result", "障碍物出现在自车前方车道内"),
    }
    normalized["actor_motion_plan"] = plan.get("actor_motion_plan") or default_actor_motion_plan(normalized)
    return normalized


def default_actor_motion_plan(plan):
    scenario_type = plan.get("scenario_type")
    trigger_frame = int(plan.get("trigger_frame", 45) or 45)
    if scenario_type == "front_vehicle_brake":
        return {
            "ego": {
                "role": "following_observer_vehicle",
                "behavior": "follow_front_actor",
                "target_speed_mps": 4.0,
                "avoid_collision": True,
                "stop_if_distance_below_m": 4.0,
            },
            "front_actor": {
                "role": "primary_actor",
                "behavior": "brake_after_trigger",
                "trigger_frame": trigger_frame,
                "brake_intensity": plan.get("brake_intensity", 1.0),
                "target_speed_mps": plan.get("target_speed_mps", 0.0),
            },
            "primary_actor": {"role": "front_actor", "behavior": "brake_after_trigger"},
            "background_actors": {"behavior": "preserve_l0_or_ignore_if_not_relevant"},
        }
    if scenario_type == "vulnerable_actor_intrusion":
        return {
            "ego": {
                "role": "observer_vehicle",
                "behavior": "slow_approach",
                "target_speed_mps": 3.0,
                "avoid_collision": True,
                "stop_if_distance_below_m": 4.0,
                "must_remain_moving_until_trigger": True,
            },
            "front_actor": {
                "role": "occluder",
                "behavior": "stationary_or_slow",
                "must_not_be_primary_event": True,
            },
            "primary_actor": {
                "role": plan.get("actor_type", "walker"),
                "behavior": "cross_ego_lane_after_trigger",
                "trigger_frame": trigger_frame,
                "start": "from_occluded_side_near_front_actor",
                "end": "across_ego_lane_centerline",
                "speed_mps": plan.get("speed_mps", 2.2),
                "must_enter_ego_lane": True,
            },
            "background_actors": {"behavior": "preserve_l0_or_ignore_if_not_relevant"},
        }
    if scenario_type == "cargo_drop":
        return {
            "ego": {
                "role": "observer_vehicle",
                "behavior": "slow_approach",
                "target_speed_mps": 3.0,
                "avoid_collision": True,
                "stop_if_distance_below_m": 5.0,
            },
            "front_actor": {
                "role": "carrier_or_occluder",
                "behavior": "steady_or_slow",
                "must_not_be_primary_event": True,
            },
            "primary_actor": {
                "role": "payload",
                "behavior": "drop_or_slide_toward_ego_lane_after_trigger",
                "trigger_frame": trigger_frame,
                "must_enter_ego_path": True,
            },
            "background_actors": {"behavior": "preserve_l0_or_ignore_if_not_relevant"},
        }
    return {
        "ego": {
            "role": "observer_vehicle",
            "behavior": "slow_approach",
            "target_speed_mps": 3.0,
            "avoid_collision": True,
            "stop_if_distance_below_m": 4.0,
        },
        "front_actor": {
            "role": "background_or_occluder",
            "behavior": "preserve_l0_or_stationary",
            "must_not_be_primary_event": True,
        },
        "primary_actor": {
            "role": "road_obstacle",
            "behavior": "appear_or_move_into_ego_lane_after_trigger",
            "trigger_frame": trigger_frame,
            "must_enter_ego_lane": True,
        },
        "background_actors": {"behavior": "preserve_l0_or_ignore_if_not_relevant"},
    }


def build_event_contract(plan):
    scenario_type = plan.get("scenario_type", "unknown")
    base = {
        "scenario_type": scenario_type,
        "trace_file": "event_trace.json",
        "trace_location": "write to --output-dir/event_trace.json",
        "required_top_level_fields": ["scenario_type", "trigger_frame", "frames", "event_applied"],
        "purpose": "Prove that this L4 run executed the selected chain-specific physical event, not only the shared L0 reconstruction.",
    }
    if scenario_type == "front_vehicle_brake":
        base.update(
            {
                "primary_actor": "front_actor",
                "background_actors": ["ego", "nearby_actors"],
                "event_applied": "front actor braking/deceleration after trigger_frame",
                "required_frame_fields": ["frame", "ego_speed_mps", "front_actor_speed_mps", "front_distance_m"],
                "success_condition": "front_actor_speed_mps decreases after trigger_frame and front_distance_m changes over time",
                "forbidden": ["payload actors", "metal_pipe", "scripted projectile/drop unless scenario_type is cargo_drop"],
                "numeric_acceptance": {
                    "front_actor_speed_drop_mps_min": 1.0,
                    "front_distance_change_m_min": 1.0,
                },
            }
        )
    elif scenario_type == "cargo_drop":
        base.update(
            {
                "primary_actor": "payload",
                "background_actors": ["ego", "front_actor"],
                "event_applied": "configured payload/obstacle leaves the front actor and enters/approaches the ego lane",
                "required_frame_fields": ["frame", "ego_speed_mps", "payload_count", "payload_positions", "payload_distance_to_ego_m"],
                "success_condition": "payload_count > 0 and payload_positions change after trigger_frame",
                "forbidden": ["front-vehicle braking as the primary event unless explicitly configured"],
                "numeric_acceptance": {
                    "payload_count_min": 1,
                    "payload_motion_m_min": 0.5,
                    "payload_min_distance_to_ego_m_max": 15.0,
                },
            }
        )
    elif scenario_type == "vulnerable_actor_intrusion":
        base.update(
            {
                "primary_actor": "vulnerable_actor",
                "background_actors": ["ego", "front_actor_as_occluder", "nearby_actors"],
                "event_applied": "configured walker/cyclist moves into ego driving space",
                "required_frame_fields": [
                    "frame",
                    "ego_speed_mps",
                    "vulnerable_actor_position",
                    "distance_to_ego_m",
                    "relative_longitudinal_m",
                    "relative_lateral_m",
                ],
                "success_condition": "vulnerable actor position changes toward/through ego lane after trigger_frame",
                "forbidden": ["payload actors", "metal_pipe", "front-vehicle braking as the primary event"],
                "numeric_acceptance": {
                    "pre_trigger_ego_speed_mps_min": 1.0,
                    "actor_motion_m_min": 1.0,
                    "distance_drop_m_min": 1.0,
                    "min_distance_to_ego_m_max": 8.0,
                    "min_abs_relative_lateral_m_max": 2.2,
                    "relative_lateral_crosses_zero": True,
                },
            }
        )
    elif scenario_type == "road_obstacle_intrusion":
        base.update(
            {
                "primary_actor": "road_obstacle",
                "background_actors": ["ego", "nearby_actors"],
                "event_applied": "configured road obstacle appears or moves into ego lane",
                "required_frame_fields": [
                    "frame",
                    "ego_speed_mps",
                    "obstacle_positions",
                    "obstacle_distance_to_ego_m",
                    "obstacle_relative_lateral_m",
                ],
                "success_condition": "obstacle is visible/placed in ego path after trigger_frame",
                "forbidden": ["metal_pipe cargo drop unless object_type requests it", "front-vehicle braking as the primary event"],
                "numeric_acceptance": {
                    "obstacle_count_min": 1,
                    "min_obstacle_distance_to_ego_m_max": 12.0,
                    "min_abs_obstacle_relative_lateral_m_max": 2.2,
                },
            }
        )
    else:
        base.update(
            {
                "event_applied": "scenario-specific event from carla_plan",
                "required_frame_fields": ["frame", "ego_speed_mps"],
                "success_condition": "trace must show a chain-specific physical state change after trigger_frame",
                "forbidden": ["unrelated template event"],
            }
        )
    return base


def build_config(chain, l0_state=None, l0_json_path=None):
    plan = normalize_l4_plan(chain.get("carla_plan", {}))
    scene_reconstruction = compact_l0_scene(l0_state)
    return {
        "level": "L4",
        "name": "CARLA代码执行",
        "description": "在L0场景事实基础上，将L3初始事故链转为可执行CARLA风险场景",
        "source_l3_chain_id": chain.get("id"),
        "source_l2_id": chain.get("parent_l2_id"),
        "source_l0_state_file": os.path.abspath(l0_json_path) if l0_json_path else None,
        "chain_description": chain.get("chain_description"),
        "direct_physical_outcome": chain.get("direct_physical_outcome"),
        "truck_distance": 18.0,
        "trigger_frame": plan.get("trigger_frame", 45),
        "carla_plan": plan,
        "event_contract": build_event_contract(plan),
        "scene_reconstruction": scene_reconstruction,
        "reconstruction_policy": {
            "base_scene": "Reconstruct the L4 risk scene from L0 state instead of choosing unrelated spawn points.",
            "town": "Use source_map from L0 when available; otherwise use --town.",
            "weather": "Apply L0 weather when available.",
            "ego": "Spawn ego near L0 ego.location/rotation and match ego type when possible.",
            "front_actor": "For front-vehicle scenarios, spawn the nearest_front_actor at its L0 relative position when available.",
            "nearby_actors": "Optionally recreate nearby L0 actors when they materially affect the selected risk event.",
        },
        "executor": "carla_smoke/scenes/risk_event_scene.py",
    }


def run_command(command, capture_output=False):
    print("\n$ " + " ".join(command))
    if capture_output:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.stdout:
            print(result.stdout, end="")
        result.check_returncode()
        return result.stdout or ""
    subprocess.run(command, check=True)
    return ""


def normalize_opencode_model_name(model):
    if not model:
        return model
    if model == "ds-v4-pro":
        return "deepseek/deepseek-v4-pro"
    if "/" in model:
        return model
    if model.startswith("deepseek"):
        return f"deepseek/{model}"
    return model


def copy_tree_contents(src_dir, dst_dir):
    if not os.path.isdir(src_dir):
        raise RuntimeError(f"Required directory not found: {src_dir}")
    os.makedirs(dst_dir, exist_ok=True)
    for name in os.listdir(src_dir):
        src = os.path.join(src_dir, name)
        dst = os.path.join(dst_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def seed_generated_script(reference_path, output_script):
    if not os.path.exists(reference_path):
        raise RuntimeError(f"Reference executor not found: {reference_path}")
    script = textwrap.dedent(
        '''\
        #!/usr/bin/env python3
        """Neutral seed for an opencode-generated CARLA risk scene script."""

        import argparse
        import glob
        import json
        import os
        import queue
        import sys
        import time


        def add_carla_python_api(carla_root):
            candidates = [
                os.path.join(carla_root, "PythonAPI", "carla"),
                os.path.join(carla_root, "PythonAPI", "carla", "agents"),
            ]
            candidates.extend(glob.glob(os.path.join(carla_root, "PythonAPI", "carla", "dist", "carla-*.egg")))
            candidates.extend(glob.glob(os.path.join(carla_root, "PythonAPI", "carla", "dist", "carla-*.whl")))
            for path in candidates:
                if os.path.exists(path) and path not in sys.path:
                    sys.path.insert(0, path)


        def import_carla(carla_root):
            try:
                import carla
                return carla
            except ImportError:
                add_carla_python_api(carla_root)
                import carla
                return carla


        def load_config(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)


        def first_blueprint(blueprints, patterns):
            for pattern in patterns:
                matches = list(blueprints.filter(pattern))
                if matches:
                    return matches[0]
            raise RuntimeError(f"No blueprint found for patterns: {patterns}")


        def parse_args():
            default_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenario_config.json")
            parser = argparse.ArgumentParser(description="Execute a generated CARLA risk event and save front-camera images.")
            parser.add_argument("--config", default=default_config)
            parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
            parser.add_argument("--host", default="127.0.0.1")
            parser.add_argument("--port", type=int, default=2000)
            parser.add_argument("--timeout", type=float, default=60.0)
            parser.add_argument("--town", default="Town03")
            parser.add_argument("--output-dir", required=True)
            parser.add_argument("--frames", type=int, default=140)
            parser.add_argument("--save-every", type=int, default=5)
            parser.add_argument("--target-speed", type=float, default=5.0)
            return parser.parse_args()


        def main():
            args = parse_args()
            config = load_config(args.config)
            plan = config.get("carla_plan", {})
            scenario_type = plan.get("scenario_type", "unknown")

            os.makedirs(args.output_dir, exist_ok=True)
            carla = import_carla(args.carla_root)
            client = carla.Client(args.host, args.port)
            client.set_timeout(args.timeout)

            world = None
            original_settings = None
            actors = []
            image_queue = queue.Queue()

            try:
                world = client.load_world(args.town) if args.town else client.get_world()
                original_settings = world.get_settings()
                settings = world.get_settings()
                settings.synchronous_mode = True
                settings.fixed_delta_seconds = 0.05
                world.apply_settings(settings)

                raise NotImplementedError(
                    f"OpenCode must implement scenario_type={scenario_type!r} according to scenario_config.json"
                )
            finally:
                for actor in reversed(actors):
                    try:
                        if actor.is_alive:
                            actor.destroy()
                    except RuntimeError:
                        pass
                if world is not None and original_settings is not None:
                    world.apply_settings(original_settings)
                time.sleep(0.5)


        if __name__ == "__main__":
            raise SystemExit(main())
        '''
    )
    with open(output_script, "w", encoding="utf-8") as f:
        f.write(script)


def prepare_opencode_workspace(args, config_path):
    workspace = os.path.join(args.output_dir, "opencode_workspace")
    os.makedirs(workspace, exist_ok=True)

    workspace_config = os.path.join(workspace, "scenario_config.json")
    shutil.copyfile(config_path, workspace_config)
    if args.l0_json:
        shutil.copyfile(args.l0_json, os.path.join(workspace, "l0_state.json"))

    output_script = os.path.join(workspace, "generated_risk_scene.py")
    reference_path = reference_executor_path()
    shutil.copyfile(reference_path, os.path.join(workspace, "reference_executor.py"))
    seed_generated_script(reference_path, output_script)

    workspace_skills = os.path.join(workspace, ".opencode", "skills")
    copy_tree_contents(opencode_skills_dir(), workspace_skills)

    skill_references = os.path.join(workspace_skills, "l4-carla-codegen", "references")
    context_dir = os.path.join(workspace, "context")
    copy_tree_contents(skill_references, context_dir)

    agents_path = os.path.join(workspace, "AGENTS.md")
    with open(agents_path, "w", encoding="utf-8") as f:
        f.write(
            "# OpenCode Workspace Instructions\n\n"
            "Use the `l4-carla-codegen` skill for this workspace.\n"
            "Edit only `generated_risk_scene.py` unless explicitly asked otherwise.\n"
            "Read `scenario_config.json`, optional `l0_state.json`, `reference_executor.py`, and the files under `context/` before editing.\n"
            "Keep the generated script self-contained and compatible with the L4 pipeline CLI.\n"
            "Reconstruct the risk scene from L0 state when available instead of using unrelated default spawn points.\n"
        )

    write_json(
        os.path.join(workspace, "opencode_inputs.json"),
        {
            "config_path": os.path.abspath(config_path),
            "workspace_config": workspace_config,
            "output_script": output_script,
            "skill": "l4-carla-codegen",
            "l0_state": os.path.join(workspace, "l0_state.json") if args.l0_json else None,
        },
    )
    return workspace, workspace_config, output_script


def opencode_prompt(config_path, output_script):
    return f"""Use the l4-carla-codegen skill.

Task:
- Read the L4 scenario config at:
  {config_path}
- Read l0_state.json if it exists.
- Read reference_executor.py and the files under context/. Use reference_executor.py for CARLA mechanics only, not as an event template.
- Edit the neutral seeded Python script in place at exactly:
  {output_script}
- Replace the NotImplementedError with scenario-specific behavior from scenario_config.json.
- Reconstruct the scene from L0 scene_reconstruction/source state: preserve town/map, weather, ego pose, nearest front actor, and relevant nearby actors as much as CARLA spawn constraints allow.
- Do not choose an unrelated spawn point when L0 ego.location/rotation is available.
- Follow carla_plan.actor_motion_plan exactly. L0 gives the initial picture; actor_motion_plan gives what every actor should do after L0.
- Do not invent actor behavior that contradicts actor_motion_plan.
- The script must connect to the configured CARLA server using the installed CARLA Python API, reconstruct the L0 ego/front/relevant actors, and execute the requested risk event from carla_plan.
- Save front-camera images into the --output-dir argument as risk_rgb_XXXX.png.
- Write --output-dir/event_trace.json exactly as required by scenario_config.event_contract. The trace must prove that this chain's physical event was applied.
- Keep the script self-contained. Do not require project imports.
- Support these CLI arguments: --carla-root, --host, --port, --town, --output-dir, --frames, --save-every.
- Use synchronous mode and restore original world settings in finally.
- Respect carla_plan.scenario_type exactly. Do not combine unrelated actions across scenario types.
- Respect scenario_config.event_contract.primary_actor. The primary actor must drive the visible risk event; background actors must not become the main event.
- For front_vehicle_brake, implement only front-vehicle braking/deceleration. Do not spawn payloads or metal pipes unless the config explicitly uses cargo_drop.
- For cargo_drop, implement payload/drop motion from the configured object and motion fields.
- For vulnerable_actor_intrusion, implement a walker/cyclist intrusion using actor_type and crossing fields.
- For road_obstacle_intrusion, implement a static or slow obstacle entering the ego lane.
- Import CARLA safely: add CARLA PythonAPI paths first, then import carla inside main or a helper and return the module.
- Do not reference a global carla variable before importing it. Avoid patterns like "if carla is None" inside a function that also imports carla.
- Before finishing, make sure the script would pass "python -m py_compile".
- Use deterministic code. Do not ask questions. Do not write Markdown. Edit only the requested Python file.

The generated script will be executed by this pipeline after opencode exits.
"""


def opencode_repair_prompt(config_path, output_script, error_output):
    return f"""The generated CARLA script failed when executed.

Use the l4-carla-codegen skill.

Scenario config:
  {config_path}

Script to fix:
  {output_script}

Execution error:
{error_output}

Edit the existing script in place. Keep the same CLI arguments and output behavior.
Fix the root cause, especially CARLA import/scope errors such as UnboundLocalError from referencing carla before import.
If the failure mentions event_trace, implement or fix --output-dir/event_trace.json according to scenario_config.event_contract.
If the failure mentions semantic validation, change the physical scene so the primary actor satisfies event_contract.numeric_acceptance; do not fake trace values.
Read reference_executor.py, context/known_failures.md, and the current generated_risk_scene.py before editing.
Do not write Markdown. Do not ask questions. Only modify the Python script.
"""


def run_opencode(args, config_path):
    opencode_bin = shutil.which(args.opencode_bin)
    if not opencode_bin:
        raise RuntimeError(
            f"opencode binary not found: {args.opencode_bin}. Install/configure opencode first, "
            "then rerun with --code-agent opencode."
        )

    workspace, workspace_config, output_script = prepare_opencode_workspace(args, config_path)
    prompt = opencode_prompt(workspace_config, output_script)
    prompt_path = os.path.join(workspace, "opencode_prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)

    opencode_model = normalize_opencode_model_name(args.opencode_model)
    command = [
        opencode_bin,
        "run",
        "--model",
        opencode_model,
        "--dir",
        workspace,
        prompt,
    ]
    run_command(command)

    if not os.path.exists(output_script):
        raise RuntimeError(f"opencode completed but did not create expected script: {output_script}")
    return output_script


def repair_generated_script(args, config_path, script_path, error_output):
    opencode_bin = shutil.which(args.opencode_bin)
    if not opencode_bin:
        raise RuntimeError(f"opencode binary not found: {args.opencode_bin}")

    workspace = os.path.dirname(script_path)
    repair_prompt = opencode_repair_prompt(
        os.path.join(workspace, "scenario_config.json"),
        script_path,
        error_output[-8000:],
    )
    repair_prompt_path = os.path.join(workspace, "opencode_repair_prompt.txt")
    with open(repair_prompt_path, "w", encoding="utf-8") as f:
        f.write(repair_prompt)

    opencode_model = normalize_opencode_model_name(args.opencode_model)
    command = [
        opencode_bin,
        "run",
        "--model",
        opencode_model,
        "--dir",
        workspace,
        repair_prompt,
    ]
    run_command(command)


def run_generated_script(args, script_path, images_dir):
    command = [
        sys.executable,
        script_path,
        "--carla-root",
        args.carla_root,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--town",
        args.town,
        "--output-dir",
        images_dir,
        "--frames",
        str(args.frames),
        "--save-every",
        str(args.save_every),
    ]
    return run_command(command, capture_output=True)


def clean_l4_outputs(images_dir):
    os.makedirs(images_dir, exist_ok=True)
    for name in os.listdir(images_dir):
        if name.startswith("risk_rgb_") and name.lower().endswith(".png"):
            os.remove(os.path.join(images_dir, name))
    trace_path = os.path.join(images_dir, "event_trace.json")
    if os.path.exists(trace_path):
        os.remove(trace_path)


def error_output_from_exception(exc):
    return getattr(exc, "output", None) or getattr(exc, "stdout", None) or getattr(exc, "stderr", None) or str(exc)


def frame_value(frame, key):
    value = frame.get(key)
    if isinstance(value, list) and value:
        value = value[0]
    return value


def numeric_values(frames, key):
    values = []
    for frame in frames:
        value = frame_value(frame, key)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def point_from_value(value):
    if isinstance(value, dict):
        if all(axis in value for axis in ("x", "y", "z")):
            return (float(value["x"]), float(value["y"]), float(value["z"]))
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (float(value[0]), float(value[1]), float(value[2]))
    return None


def point_values(frames, key):
    points = []
    for frame in frames:
        value = frame_value(frame, key)
        point = point_from_value(value)
        if point:
            points.append(point)
    return points


def point_distance(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def split_frames_by_trigger(frames, trigger_frame):
    before = [frame for frame in frames if int(frame.get("frame", -1)) < trigger_frame]
    after = [frame for frame in frames if int(frame.get("frame", -1)) >= trigger_frame]
    return before, after


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def validate_generated_script(script_path):
    run_command([sys.executable, "-m", "py_compile", script_path], capture_output=True)
    run_command([sys.executable, script_path, "--help"], capture_output=True)
    with open(script_path, "r", encoding="utf-8") as f:
        script = f.read()
    if "NotImplementedError" in script:
        raise RuntimeError("generated script still contains the neutral seed NotImplementedError")


def validate_event_trace(config_path, images_dir):
    config = read_json(config_path)
    contract = config.get("event_contract", {})
    trace_path = os.path.join(images_dir, contract.get("trace_file", "event_trace.json"))
    if not os.path.exists(trace_path):
        raise RuntimeError(
            f"generated script did not write required event trace: {trace_path}. "
            "Write --output-dir/event_trace.json with scenario_type, trigger_frame, frames, and event_applied."
        )
    trace = read_json(trace_path)
    expected_type = config.get("carla_plan", {}).get("scenario_type")
    if trace.get("scenario_type") != expected_type:
        raise RuntimeError(
            f"event_trace scenario_type mismatch: expected {expected_type!r}, got {trace.get('scenario_type')!r}"
        )
    frames = trace.get("frames")
    if not isinstance(frames, list) or not frames:
        raise RuntimeError("event_trace.frames must be a non-empty list of per-frame event state.")
    if not trace.get("event_applied"):
        raise RuntimeError("event_trace.event_applied must describe the chain-specific event that was executed.")
    validate_event_trace_semantics(config, trace, frames)
    return trace_path


def validate_risk_images(images_dir):
    if not os.path.isdir(images_dir):
        raise RuntimeError(f"generated script did not create risk image directory: {images_dir}")
    images = [
        name
        for name in os.listdir(images_dir)
        if name.startswith("risk_rgb_") and name.lower().endswith(".png")
    ]
    if not images:
        raise RuntimeError(f"generated script did not save any risk_rgb_*.png images under: {images_dir}")
    return len(images)


def validate_event_trace_semantics(config, trace, frames):
    plan = config.get("carla_plan", {})
    scenario_type = plan.get("scenario_type")
    trigger_frame = int(trace.get("trigger_frame", plan.get("trigger_frame", config.get("trigger_frame", 45))) or 45)
    before, after = split_frames_by_trigger(frames, trigger_frame)
    require(after, f"event_trace has no frames at or after trigger_frame={trigger_frame}")

    if scenario_type == "front_vehicle_brake":
        speeds_after = numeric_values(after, "front_actor_speed_mps")
        distances = numeric_values(frames, "front_distance_m")
        require(len(speeds_after) >= 2, "front_vehicle_brake trace must include front_actor_speed_mps after trigger.")
        require(max(speeds_after) - min(speeds_after) >= 1.0, "front_vehicle_brake did not show enough front actor speed drop.")
        require(distances and max(distances) - min(distances) >= 1.0, "front_vehicle_brake did not show enough ego-front distance change.")
        forbidden_text = json.dumps(trace, ensure_ascii=False).lower()
        require("payload" not in forbidden_text and "metal_pipe" not in forbidden_text, "front_vehicle_brake trace includes payload/metal_pipe template artifacts.")
        return

    if scenario_type == "cargo_drop":
        payload_counts = numeric_values(after, "payload_count")
        require(payload_counts and max(payload_counts) >= 1, "cargo_drop trace must show payload_count >= 1 after trigger.")
        payload_points = point_values(after, "payload_positions")
        if len(payload_points) >= 2:
            motion = max(point_distance(payload_points[0], point) for point in payload_points[1:])
            require(motion >= 0.5, "cargo_drop payload positions did not move enough after trigger.")
        payload_distances = numeric_values(after, "payload_distance_to_ego_m")
        if payload_distances:
            require(min(payload_distances) <= 15.0, "cargo_drop payload never approached ego path closely enough.")
        return

    if scenario_type == "vulnerable_actor_intrusion":
        ego_before = numeric_values(before[-10:] if before else [], "ego_speed_mps")
        require(ego_before and max(ego_before) >= 1.0, "vulnerable_actor_intrusion trigger occurs after ego has already stopped.")
        actor_points = point_values(after, "vulnerable_actor_position")
        require(len(actor_points) >= 2, "vulnerable_actor_intrusion trace must include actor positions after trigger.")
        actor_motion = max(point_distance(actor_points[0], point) for point in actor_points[1:])
        require(actor_motion >= 1.0, "vulnerable_actor_intrusion actor did not move enough after trigger.")
        distances = numeric_values(after, "distance_to_ego_m")
        require(distances, "vulnerable_actor_intrusion trace must include distance_to_ego_m after trigger.")
        require(min(distances) <= 8.0, "vulnerable_actor_intrusion actor never got close enough to ego.")
        require(distances[0] - min(distances) >= 1.0, "vulnerable_actor_intrusion actor did not approach ego enough after trigger.")
        laterals = numeric_values(after, "relative_lateral_m")
        require(laterals, "vulnerable_actor_intrusion trace must include relative_lateral_m after trigger.")
        require(min(abs(value) for value in laterals) <= 2.2, "vulnerable_actor_intrusion actor never entered ego lane laterally.")
        require(min(laterals) <= 0.0 <= max(laterals), "vulnerable_actor_intrusion actor did not cross ego lane centerline.")
        forbidden_text = json.dumps(trace, ensure_ascii=False).lower()
        require("payload" not in forbidden_text and "metal_pipe" not in forbidden_text, "vulnerable_actor_intrusion trace includes cargo template artifacts.")
        return

    if scenario_type == "road_obstacle_intrusion":
        positions = point_values(after, "obstacle_positions")
        require(positions, "road_obstacle_intrusion trace must include obstacle_positions after trigger.")
        distances = numeric_values(after, "obstacle_distance_to_ego_m")
        if distances:
            require(min(distances) <= 12.0, "road_obstacle_intrusion obstacle never got close enough to ego.")
        laterals = numeric_values(after, "obstacle_relative_lateral_m")
        if laterals:
            require(min(abs(value) for value in laterals) <= 2.2, "road_obstacle_intrusion obstacle never entered ego lane laterally.")
        return


def repair_then_validate(args, config_path, script_path, error_output):
    if args.opencode_repair_attempts <= 0:
        raise RuntimeError(error_output)
    last_error = error_output
    for attempt in range(1, args.opencode_repair_attempts + 1):
        print(f"\nAsking opencode to repair generated script ({attempt}/{args.opencode_repair_attempts}).")
        repair_generated_script(args, config_path, script_path, last_error)
        try:
            validate_generated_script(script_path)
            return
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            last_error = error_output_from_exception(exc)
            if attempt == args.opencode_repair_attempts:
                raise


def run_generated_script_with_repair(args, config_path, script_path, images_dir):
    last_error = ""
    for attempt in range(0, args.opencode_repair_attempts + 1):
        try:
            run_generated_script(args, script_path, images_dir)
            image_count = validate_risk_images(images_dir)
            print(f"Validated risk images: {image_count} files under {os.path.abspath(images_dir)}")
            if args.validate_event_trace:
                trace_path = validate_event_trace(config_path, images_dir)
                print(f"Validated event trace: {os.path.abspath(trace_path)}")
            return
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            last_error = error_output_from_exception(exc)
            if attempt == args.opencode_repair_attempts:
                raise
            print("\nGenerated script failed during CARLA execution.")
            repair_then_validate(args, config_path, script_path, last_error)
    raise RuntimeError(last_error)


def execute_chain(args, chain, l0_state, chain_dir):
    config = build_config(chain, l0_state, args.l0_json)
    config["code_agent"] = args.code_agent

    os.makedirs(chain_dir, exist_ok=True)
    config_path = os.path.join(chain_dir, "scenario_config.json")
    images_dir = os.path.join(chain_dir, "risk_images")
    clean_l4_outputs(images_dir)
    write_json(config_path, config)
    print(f"Saved L4 scenario config: {os.path.abspath(config_path)}")

    if args.execute and args.code_agent == "template":
        repo_root = repo_root_from_this_file()
        executor = os.path.join(repo_root, "carla_smoke", "scenes", "risk_event_scene.py")
        command = [
            sys.executable,
            executor,
            "--config",
            config_path,
            "--carla-root",
            args.carla_root,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--town",
            args.town,
            "--output-dir",
            images_dir,
            "--frames",
            str(args.frames),
            "--save-every",
            str(args.save_every),
        ]
        run_command(command)
    elif args.execute and args.code_agent == "opencode":
        generated_script = run_opencode(args, config_path)
        config["generated_script"] = generated_script
        write_json(config_path, config)
        try:
            validate_generated_script(generated_script)
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            print("\nGenerated script failed local validation.")
            repair_then_validate(args, config_path, generated_script, error_output_from_exception(exc))
        run_generated_script_with_repair(args, config_path, generated_script, images_dir)
    else:
        print("L4 execution skipped. Add --execute to run CARLA and save risk images.")

    return {
        "chain_id": chain.get("id"),
        "source_l2_id": chain.get("parent_l2_id"),
        "output_dir": os.path.abspath(chain_dir),
        "scenario_config": os.path.abspath(config_path),
        "risk_images": os.path.abspath(images_dir),
        "scenario_type": config.get("carla_plan", {}).get("scenario_type"),
    }


def main():
    parser = argparse.ArgumentParser(description="L4 code-agent: generate and optionally execute CARLA risk-scene code.")
    parser.add_argument("l3_json", help="Path to l3/chains.json.")
    parser.add_argument("--chain-index", type=int, default=0)
    parser.add_argument("--all-chains", action="store_true", help="Generate L4 outputs for every chain in l3_json.")
    parser.add_argument("--continue-on-chain-error", action="store_true", help="In --all-chains mode, keep running later chains if one chain fails.")
    parser.add_argument("--output-dir", default="carla_smoke/workdir/manual/l4")
    parser.add_argument("--l0-json", default=None, help="Optional L0 state.json used to reconstruct the original scene.")
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--town", default="Town03")
    parser.add_argument("--frames", type=int, default=140)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--execute", action="store_true", help="Run CARLA executor to produce risk images.")
    parser.add_argument("--code-agent", choices=["template", "opencode"], default="opencode")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default="deepseek-v4-pro")
    parser.add_argument("--opencode-repair-attempts", type=int, default=REPAIR_ATTEMPTS)
    parser.add_argument(
        "--validate-event-trace",
        action="store_true",
        help="Also validate event_trace.json structure and scenario-specific numeric semantics after execution.",
    )
    args = parser.parse_args()

    chains_data = read_json(args.l3_json)
    l0_state = read_json(args.l0_json) if args.l0_json else None

    if args.all_chains:
        chains = chains_from_data(chains_data)
        results = []
        os.makedirs(args.output_dir, exist_ok=True)
        for index, chain in enumerate(chains, start=1):
            print(f"\n=== L4 chain {index}/{len(chains)}: {chain.get('id', index)} ===")
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
                        "scenario_type": (chain.get("carla_plan") or {}).get("scenario_type"),
                        "error": repr(exc),
                    }
                )
                print(f"WARNING: L4 chain failed, continuing: {exc}", file=sys.stderr)
            finally:
                args.output_dir = original_output_dir
        manifest_path = os.path.join(args.output_dir, "l4_manifest.json")
        write_json(
            manifest_path,
            {
                "mode": "all_chains",
                "source_l3_file": os.path.abspath(args.l3_json),
                "chain_count": len(results),
                "results": results,
            },
        )
        print(f"Saved L4 manifest: {os.path.abspath(manifest_path)}")
    else:
        chain = select_chain(chains_data, args.chain_index)
        execute_chain(args, chain, l0_state, args.output_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
