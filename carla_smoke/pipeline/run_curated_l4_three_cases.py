#!/usr/bin/env python3
"""Build three curated L3 chains and run L4 only on those chains.

The curated cases are:
1. pedestrian intrusion
2. front vehicle sudden brake
3. front vehicle turns left and a pedestrian appears ahead of it
"""

import argparse
import json
import os
import subprocess
import sys


CASE_IDS = [
    "pedestrian_intrusion",
    "front_vehicle_brake",
    "front_left_turn_pedestrian_appear",
]


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def run_command(command):
    print("\n$ " + " ".join(command))
    subprocess.run(command, check=True)


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def actor_kind(actor):
    kind = str((actor or {}).get("kind", "")).lower()
    type_id = str((actor or {}).get("type_id", "")).lower()
    if kind:
        return kind
    if type_id.startswith("vehicle."):
        return "vehicle"
    if type_id.startswith("walker."):
        return "pedestrian"
    return kind or "unknown"


def is_vehicle(actor):
    return actor_kind(actor) == "vehicle" or str((actor or {}).get("type_id", "")).startswith("vehicle.")


def is_vulnerable(actor):
    kind = actor_kind(actor)
    type_id = str((actor or {}).get("type_id", "")).lower()
    return kind in {"pedestrian", "walker", "cyclist", "bicycle"} or type_id.startswith("walker.")


def actor_distance(actor):
    return abs(as_float((actor or {}).get("distance_m"), as_float((actor or {}).get("relative_longitudinal_m"), 9999.0)))


def actor_lateral(actor):
    return abs(as_float((actor or {}).get("relative_lateral_m"), 9999.0))


def all_actors(l0_state):
    actors = l0_state.get("actors", []) if isinstance(l0_state, dict) else []
    return [actor for actor in actors if isinstance(actor, dict)]


def choose_pedestrian(l0_state):
    candidates = [actor for actor in all_actors(l0_state) if is_vulnerable(actor)]
    if not candidates:
        nearest = (l0_state or {}).get("nearest_front_actor")
        if isinstance(nearest, dict) and is_vulnerable(nearest):
            candidates.append(nearest)
    return min(candidates, key=lambda actor: (actor_distance(actor), actor_lateral(actor))) if candidates else None


def choose_front_vehicle(l0_state):
    nearest = (l0_state or {}).get("nearest_front_actor")
    if isinstance(nearest, dict) and is_vehicle(nearest):
        return nearest
    candidates = []
    for actor in all_actors(l0_state):
        if not is_vehicle(actor):
            continue
        rel_long = as_float(actor.get("relative_longitudinal_m"), None)
        if rel_long is None or rel_long >= -1.0:
            candidates.append(actor)
    return min(candidates, key=lambda actor: (actor_lateral(actor), actor_distance(actor))) if candidates else None


def actor_identity(actor, role, must_drive):
    if not isinstance(actor, dict):
        return None
    identity = dict(actor)
    actor_id = identity.get("id", identity.get("actor_id"))
    identity["source"] = "l0_actor"
    identity["actor_id"] = actor_id
    identity.setdefault("id", actor_id)
    identity["kind"] = actor_kind(identity)
    identity["role"] = role
    identity["must_drive_primary_event"] = bool(must_drive)
    if not must_drive:
        identity["must_not_drive_primary_event"] = True
    return identity


def generated_pedestrian(role="vulnerable_actor"):
    return {
        "source": "generated_actor",
        "actor_id": "generated_vulnerable_actor",
        "kind": "pedestrian",
        "type_id": "walker.pedestrian.*",
        "role": role,
        "must_drive_primary_event": True,
        "selection_reason": "No original L0 pedestrian is available for this curated scenario.",
    }


def generated_front_vehicle(role="front_vehicle"):
    return {
        "source": "generated_actor",
        "actor_id": "generated_front_vehicle",
        "kind": "vehicle",
        "type_id": "vehicle.*",
        "role": role,
        "must_drive_primary_event": True,
        "selection_reason": "No original L0 front vehicle is available for this curated scenario.",
    }


def ego_participant():
    return {
        "source": "l0_ego",
        "actor_id": "ego",
        "kind": "ego",
        "role": "affected_actor",
        "must_drive_primary_event": False,
    }


def plan_identity(primary):
    if not isinstance(primary, dict):
        return {}
    actor_id = primary.get("actor_id", primary.get("id"))
    if primary.get("source") != "l0_actor":
        return {}
    return {
        "primary_actor_id": actor_id,
        "primary_actor_source": "l0_actor",
        "primary_actor_kind": primary.get("kind"),
        "primary_actor_type_id": primary.get("type_id"),
        "primary_actor_role": primary.get("role"),
    }


def relevant_actor_list(*actors):
    result = []
    seen = set()
    for actor in actors:
        if not isinstance(actor, dict):
            continue
        actor_id = actor.get("actor_id", actor.get("id"))
        key = (actor.get("source"), actor_id, actor.get("type_id"))
        if key in seen:
            continue
        seen.add(key)
        result.append(actor)
    return result


def pedestrian_intrusion_chain(l0_state, trigger_frame):
    pedestrian = actor_identity(choose_pedestrian(l0_state), "vulnerable_actor", True) or generated_pedestrian()
    front_vehicle = actor_identity(choose_front_vehicle(l0_state), "background_or_occluder", False)
    participants = [ego_participant(), pedestrian]
    if front_vehicle:
        participants.append(front_vehicle)
    plan = {
        "scenario_type": "vulnerable_actor_intrusion",
        "actor_type": "walker",
        "trigger_frame": trigger_frame,
        "spawn_relative_to": "ego_lane_right",
        "start_position": {"x": 18.0, "y": 4.0, "z": 0.2},
        "crossing_direction": "right_to_left",
        "speed_mps": 2.4,
        "expected_visual_result": "主行人从侧前方突然插入自车行驶空间，自车需要紧急制动或避让。",
        "actor_motion_plan": {
            "ego": {"role": "observer_vehicle", "behavior": "slow_approach_until_trigger_then_react"},
            "primary_actor": {
                "role": "vulnerable_actor",
                "behavior": "cross_ego_lane_after_trigger",
                "trigger_frame": trigger_frame,
                "must_enter_ego_lane": True,
            },
            "background_actors": {"behavior": "preserve_or_ignore", "must_not_drive_primary_event": True},
        },
    }
    plan.update(plan_identity(pedestrian))
    return {
        "level": "L3",
        "id": "L3-curated-01-pedestrian-intrusion",
        "parent_l2_id": "L2-curated-01",
        "parent_l2_trigger": "行人突然从侧前方插入自车车道",
        "parent_l1_name": "弱势交通参与者靠近自车行驶空间",
        "chain_description": "主行人突然从侧前方进入自车行驶空间，横向侵入自车车道并与自车距离快速缩小。",
        "direct_physical_outcome": "自车前方出现近距离弱势交通参与者，需要紧急制动或避让。",
        "primary_perturbation_object": pedestrian,
        "selected_actor": pedestrian,
        "actor_list": relevant_actor_list(pedestrian, front_vehicle),
        "chain_participants": participants,
        "object_registry": {"primary_object": pedestrian, "participants": participants},
        "carla_plan": plan,
    }


def front_vehicle_brake_chain(l0_state, trigger_frame):
    front_vehicle = actor_identity(choose_front_vehicle(l0_state), "front_vehicle", True) or generated_front_vehicle()
    pedestrian = actor_identity(choose_pedestrian(l0_state), "background_vulnerable_actor", False)
    participants = [ego_participant(), front_vehicle]
    if pedestrian:
        participants.append(pedestrian)
    plan = {
        "scenario_type": "front_vehicle_brake",
        "target_actor": "front_vehicle",
        "trigger_frame": trigger_frame,
        "brake_intensity": 1.0,
        "deceleration_mps2": 7.0,
        "target_speed_mps": 0.0,
        "expected_visual_result": "前车在自车前方突然急刹，自车与前车距离快速压缩。",
        "actor_motion_plan": {
            "ego": {"role": "following_observer_vehicle", "behavior": "follow_front_actor_until_trigger_then_react"},
            "primary_actor": {
                "role": "front_vehicle",
                "behavior": "brake_after_trigger",
                "trigger_frame": trigger_frame,
                "brake_intensity": 1.0,
            },
            "background_actors": {"behavior": "preserve_or_ignore", "must_not_drive_primary_event": True},
        },
    }
    plan.update(plan_identity(front_vehicle))
    return {
        "level": "L3",
        "id": "L3-curated-02-front-brake",
        "parent_l2_id": "L2-curated-02",
        "parent_l2_trigger": "前车突然急刹",
        "parent_l1_name": "跟车距离和前车速度状态不确定",
        "chain_description": "前车在自车前方突然施加强制制动，速度快速下降，自车前向安全距离被压缩。",
        "direct_physical_outcome": "自车需要紧急制动以避免追尾。",
        "primary_perturbation_object": front_vehicle,
        "selected_actor": front_vehicle,
        "actor_list": relevant_actor_list(front_vehicle, pedestrian),
        "chain_participants": participants,
        "object_registry": {"primary_object": front_vehicle, "participants": participants},
        "carla_plan": plan,
    }


def front_left_turn_pedestrian_appear_chain(l0_state, trigger_frame):
    pedestrian = actor_identity(choose_pedestrian(l0_state), "vulnerable_actor", True) or generated_pedestrian()
    front_vehicle = actor_identity(choose_front_vehicle(l0_state), "turning_front_occluder", False)
    participants = [ego_participant(), pedestrian]
    if front_vehicle:
        participants.append(front_vehicle)
    plan = {
        "scenario_type": "vulnerable_actor_intrusion",
        "actor_type": "walker",
        "trigger_frame": trigger_frame,
        "spawn_relative_to": "front_vehicle_occluded_area",
        "start_position": {"x": 16.0, "y": 2.8, "z": 0.2},
        "crossing_direction": "left_to_right",
        "speed_mps": 2.0,
        "expected_visual_result": "前车突然左转/偏离后，前车前方遮挡区域暴露出一个行人，行人进入自车前方路径。",
        "actor_motion_plan": {
            "ego": {"role": "observer_vehicle", "behavior": "slow_approach_until_occlusion_breaks"},
            "front_actor": {
                "role": "turning_front_occluder",
                "behavior": "turn_or_shift_left_after_trigger_to_reveal_pedestrian",
                "trigger_frame": trigger_frame,
                "must_not_drive_primary_event": True,
            },
            "primary_actor": {
                "role": "vulnerable_actor",
                "behavior": "appear_from_front_vehicle_occlusion_and_enter_ego_path",
                "trigger_frame": trigger_frame,
                "must_enter_ego_lane": True,
            },
            "background_actors": {"behavior": "preserve_or_ignore", "must_not_drive_primary_event": True},
        },
    }
    plan.update(plan_identity(pedestrian))
    chain_description = "前车突然左转或向左偏离，遮挡解除后，前车前方出现行人并进入自车行驶路径。"
    direct_outcome = "自车前方突然暴露近距离行人目标，感知和制动时间被压缩。"
    return {
        "level": "L3",
        "id": "L3-curated-03-front-left-turn-pedestrian-appear",
        "parent_l2_id": "L2-curated-03",
        "parent_l2_trigger": "前车突然左转后前方出现行人",
        "parent_l1_name": "前车遮挡导致前方弱势交通参与者不可见",
        "chain_description": chain_description,
        "direct_physical_outcome": direct_outcome,
        "primary_perturbation_object": pedestrian,
        "selected_actor": pedestrian,
        "actor_list": relevant_actor_list(pedestrian, front_vehicle),
        "chain_participants": participants,
        "object_registry": {"primary_object": pedestrian, "participants": participants},
        "carla_plan": plan,
        "l4_note": (
            "This is a curated composite case. The pedestrian is the primary risk object; "
            "the front vehicle is an occluder/context actor and should not replace the pedestrian as the primary event."
        ),
    }


def build_curated_chains(l0_state, cases, trigger_frame):
    builders = {
        "pedestrian_intrusion": pedestrian_intrusion_chain,
        "front_vehicle_brake": front_vehicle_brake_chain,
        "front_left_turn_pedestrian_appear": front_left_turn_pedestrian_appear_chain,
    }
    chains = []
    for case_id in cases:
        if case_id not in builders:
            raise ValueError(f"Unknown curated case {case_id!r}; choose from {', '.join(CASE_IDS)}")
        chains.append(builders[case_id](l0_state, trigger_frame))
    return {
        "level": "L3",
        "name": "Curated L3 chains for L4-only evaluation",
        "description": "Hand-authored L3 chains that mimic the L0-L3 outputs and restrict L4 to three selected risk pictures.",
        "curated": True,
        "initial_accident_chains": chains,
    }


def run_capture_if_requested(args, repo_root):
    l0_path = os.path.join(os.path.abspath(args.run_dir), "l0", "state.json")
    if os.path.exists(l0_path) or not args.capture_if_missing:
        return
    run_safebench = os.path.join(repo_root, "carla_smoke", "pipeline", "run_safebench.py")
    command = [
        args.carla_python or sys.executable,
        run_safebench,
        "--carla-root",
        args.carla_root,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--timeout",
        str(args.timeout),
        "--scenario-index",
        str(args.scenario_index),
        "--frames",
        str(args.capture_frames),
        "--save-every",
        str(args.capture_save_every),
        "--sample-count",
        str(args.sample_count),
        "--camera-mode",
        "surround",
        "--workdir-root",
        os.path.dirname(os.path.abspath(args.run_dir)),
        "--run-id",
        os.path.basename(os.path.abspath(args.run_dir)),
        "--model",
        args.plan_model,
        "--qwen-model",
        args.qwen_model,
        "--api-key-env",
        args.api_key_env,
        "--skip-l2",
        "--skip-l4",
    ]
    if args.env_file:
        command.extend(["--env-file", args.env_file])
    if args.scenic_file:
        command.extend(["--scenic-file", args.scenic_file])
    run_command(command)


def build_l4_command(args, repo_root, curated_l3_path):
    if args.l4_backend == "code-agent":
        runner = os.path.join(repo_root, "carla_smoke", "pipeline", "l4.py")
        command = [
            args.carla_python or sys.executable,
            runner,
            os.path.abspath(curated_l3_path),
            "--output-dir",
            os.path.abspath(args.output_dir),
            "--l0-json",
            os.path.abspath(args.l0_json),
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
            "--local-trigger-frame",
            str(args.trigger_frame),
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
            str(args.timeout),
            "--execute",
            "--all-chains",
            "--continue-on-chain-error",
        ]
        if args.skip_plan_agent:
            command.append("--skip-plan-agent")
        if args.env_file:
            command.extend(["--env-file", args.env_file])
        if args.validate_event_trace:
            command.append("--validate-event-trace")
        return command

    runner = os.path.join(repo_root, "carla_smoke", "pipeline", "run_l4_intervention_from_workdir.py")
    command = [
        args.carla_python or sys.executable,
        runner,
        "--run-dir",
        os.path.abspath(args.run_dir),
        "--output-dir",
        os.path.abspath(args.output_dir),
        "--l0-json",
        os.path.abspath(args.l0_json),
        "--l3-json",
        os.path.abspath(curated_l3_path),
        "--carla-root",
        args.carla_root,
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
        str(args.trigger_frame),
        "--pre-trigger-seconds",
        str(args.pre_trigger_seconds),
        "--source-timestep",
        str(args.source_timestep),
        "--warmup-ticks",
        str(args.warmup_ticks),
        "--seed",
        str(args.seed),
        "--timestep",
        str(args.source_timestep),
        "--ego-speed-difference",
        str(args.ego_speed_difference),
        "--weather",
        args.weather,
        "--all-chains",
        "--continue-on-chain-error",
        "--intervention-agent",
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
        str(args.timeout),
    ]
    if args.skip_plan_agent:
        command.append("--skip-plan-agent")
    if args.env_file:
        command.extend(["--env-file", args.env_file])
    if not args.validate_event_trace:
        command.append("--skip-event-trace-validation")
    if args.scenic_file:
        command.extend(["--scenic-file", os.path.abspath(args.scenic_file)])
    return command


def main():
    repo_root = repo_root_from_this_file()
    default_carla_root = os.environ.get("CARLA_ROOT", "/mnt/data2/congfeng/CARLA")
    parser = argparse.ArgumentParser(description="Run L4 on exactly three curated risk pictures.")
    parser.add_argument("--run-dir", required=True, help="Existing or target workdir containing l0/state.json and images/safebench_scene.json.")
    parser.add_argument("--output-dir", default=None, help="Defaults to <run-dir>/l4_curated_three_cases.")
    parser.add_argument("--l0-json", default=None, help="Defaults to <run-dir>/l0/state.json.")
    parser.add_argument("--curated-l3-json", default=None, help="Defaults to <run-dir>/curated_l3/chains.json.")
    parser.add_argument("--cases", nargs="+", default=CASE_IDS, choices=CASE_IDS)
    parser.add_argument("--only-build-inputs", action="store_true")
    parser.add_argument("--capture-if-missing", action="store_true", help="If l0/state.json is missing, first capture a SafeBench scene and run L0/L1 only.")
    parser.add_argument("--scenario-index", type=int, default=0)
    parser.add_argument("--scenic-file", default=None)
    parser.add_argument("--capture-frames", type=int, default=600)
    parser.add_argument("--capture-save-every", type=int, default=20)
    parser.add_argument("--sample-count", type=int, default=5)
    parser.add_argument("--carla-root", default=default_carla_root)
    parser.add_argument("--carla-python", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--scene-sample-attempts", type=int, default=20)
    parser.add_argument("--l4-frames", type=int, default=180)
    parser.add_argument("--l4-save-every", type=int, default=5)
    parser.add_argument("--trigger-frame", type=int, default=20)
    parser.add_argument("--pre-trigger-seconds", type=float, default=2.0)
    parser.add_argument("--source-timestep", type=float, default=0.05)
    parser.add_argument("--warmup-ticks", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--ego-speed-difference", type=float, default=-5.0)
    parser.add_argument("--weather", default="ClearNoon")
    parser.add_argument(
        "--l4-backend",
        choices=["code-agent", "safebench-intervention"],
        default="code-agent",
        help="Default is code-agent because this curated script is intended to make OpenCode generate L4 risk scenes.",
    )
    parser.add_argument("--code-agent", choices=["opencode", "template"], default="opencode")
    parser.add_argument("--town", default="Town05")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default="deepseek-v4-pro")
    parser.add_argument("--opencode-repair-attempts", type=int, default=3)
    parser.add_argument("--skip-plan-agent", action="store_true")
    parser.add_argument("--plan-model", default="deepseek-v4-pro")
    parser.add_argument("--plan-url", default="https://api.deepseek.com/chat/completions")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--qwen-model", default="qwen3.5:0.8b")
    parser.add_argument("--validate-event-trace", action="store_true", help="By default validation is skipped so images are kept even if a curated composite case is imperfect.")
    args = parser.parse_args()

    args.run_dir = os.path.abspath(args.run_dir)
    args.output_dir = args.output_dir or os.path.join(args.run_dir, "l4_curated_three_cases")
    args.l0_json = os.path.abspath(args.l0_json or os.path.join(args.run_dir, "l0", "state.json"))
    curated_l3_path = os.path.abspath(args.curated_l3_json or os.path.join(args.run_dir, "curated_l3", "chains.json"))

    run_capture_if_requested(args, repo_root)
    if not os.path.exists(args.l0_json):
        raise FileNotFoundError(f"L0 state not found: {args.l0_json}. Use --capture-if-missing or pass --l0-json.")

    l0_state = read_json(args.l0_json)
    curated = build_curated_chains(l0_state, args.cases, args.trigger_frame)
    write_json(curated_l3_path, curated)
    write_json(
        os.path.join(os.path.dirname(curated_l3_path), "curated_inputs_manifest.json"),
        {
            "run_dir": args.run_dir,
            "l0_json": args.l0_json,
            "curated_l3_json": curated_l3_path,
            "cases": args.cases,
            "note": "L4 should use this curated L3 file instead of the model-generated l3/chains.json.",
        },
    )
    print(f"Saved curated L3 chains: {curated_l3_path}")

    if args.only_build_inputs:
        return 0

    command = build_l4_command(args, repo_root, curated_l3_path)
    run_command(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
