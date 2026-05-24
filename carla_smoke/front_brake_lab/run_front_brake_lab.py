#!/usr/bin/env python3
"""Focused front-vehicle hard-brake lab.

This keeps the real L4 execution shape for one primitive only:

L0 optional input -> select nearest front vehicle -> instantiate the shared
front_vehicle_brake_after_trigger primitive -> l4_task_source/semantic_primitives
-> l4_task.json -> OpenCode generates Scenic -> CARLA capture
-> reconstruct/validate event_trace -> OpenCode repair feedback.
"""

import argparse
import json
import os
import sys


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def add_pipeline_path():
    pipeline_dir = os.path.join(repo_root_from_this_file(), "carla_smoke", "pipeline")
    if pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)


add_pipeline_path()

from risk_library import action_primitive_by_id, risk_type_by_id  # noqa: E402
from deepseek_client import DEFAULT_API_KEY_ENV, DEFAULT_DEEPSEEK_MODEL, DEFAULT_DEEPSEEK_URL  # noqa: E402
from l4_scenario_language import (  # noqa: E402
    build_semantic_primitives,
    prepare_workspace,
    run_opencode,
    run_spawn_check,
    run_scenic_validate_with_repair,
)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def as_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def actor_kind(actor):
    kind = str((actor or {}).get("kind") or "").lower()
    type_id = str((actor or {}).get("type_id") or "").lower()
    if kind:
        return kind
    if type_id.startswith("vehicle."):
        return "vehicle"
    return kind


def is_vehicle(actor):
    type_id = str((actor or {}).get("type_id") or "").lower()
    return actor_kind(actor) == "vehicle" or type_id.startswith("vehicle.")


def select_front_vehicle(l0_state, max_lateral_m=2.5):
    actors = l0_state.get("actors") or [] if isinstance(l0_state, dict) else []
    candidates = []
    for actor in actors:
        if not isinstance(actor, dict) or not is_vehicle(actor):
            continue
        rel_long = as_float(actor.get("relative_longitudinal_m"))
        rel_lat = as_float(actor.get("relative_lateral_m"), 0.0)
        if rel_long is None or rel_long <= 0.0:
            continue
        if rel_lat is not None and abs(rel_lat) > max_lateral_m:
            continue
        score = rel_long + 5.0 * abs(rel_lat or 0.0)
        if actor.get("same_lane_as_ego") is True:
            score -= 2.0
        candidates.append((score, actor))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    selected = dict(candidates[0][1])
    selected["source"] = "l0_actor"
    selected["actor_id"] = selected.get("actor_id", selected.get("id"))
    selected["role"] = "front_vehicle"
    selected["must_drive_primary_event"] = True
    return selected


def default_front_actor():
    return {
        "source": "synthetic_default",
        "actor_id": "front_vehicle_default",
        "kind": "vehicle",
        "type_id": "vehicle.nissan.micra",
        "role": "front_vehicle",
        "relative_position": "front",
        "relative_longitudinal_m": 14.0,
        "relative_lateral_m": 0.0,
        "distance_m": 14.0,
        "speed_mps": 8.0,
        "must_drive_primary_event": True,
    }


def scenic_map_path(town):
    return os.path.abspath(
        os.path.join(
            repo_root_from_this_file(),
            "safebench",
            "scenario",
            "scenario_data",
            "scenic_data",
            "maps",
            f"{town}.xodr",
        )
    )


def clamp(value, low, high):
    return max(low, min(high, value))


def build_config(args, l0_state, selected_actor):
    risk_type = risk_type_by_id("lead_vehicle_hard_brake") or {}
    primitive = action_primitive_by_id("front_vehicle_brake_after_trigger") or {}
    actor = selected_actor or default_front_actor()
    actor_type = actor.get("type_id") or args.front_model
    if not str(actor_type).startswith("vehicle."):
        actor_type = args.front_model

    selected_distance = as_float(actor.get("relative_longitudinal_m"), args.front_distance_m)
    front_distance_m = args.front_distance_m if args.front_distance_m is not None else clamp(selected_distance, 8.0, 22.0)
    selected_lateral = as_float(actor.get("relative_lateral_m"), 0.0)
    front_lateral_m = args.front_lateral_m if args.front_lateral_m is not None else clamp(selected_lateral, -0.5, 0.5)
    selected_speed = as_float(actor.get("speed_mps"), 0.0) or 0.0
    front_speed_mps = args.front_speed_mps if args.front_speed_mps is not None else max(selected_speed, 8.0)

    primary_actor = dict(actor)
    primary_actor.update(
        {
            "type_id": actor_type,
            "kind": "vehicle",
            "role": "front_vehicle",
            "relative_position": "front",
            "relative_longitudinal_m": front_distance_m,
            "relative_lateral_m": front_lateral_m,
            "distance_m": (front_distance_m**2 + front_lateral_m**2) ** 0.5,
            "speed_mps": front_speed_mps,
            "relative_to_ego": {
                "longitudinal_m": front_distance_m,
                "lateral_m": front_lateral_m,
                "side": "center" if abs(front_lateral_m) < 0.05 else ("right" if front_lateral_m > 0 else "left"),
                "relative_position": "front",
                "same_lane_as_ego": True,
            },
        }
    )

    trigger_frame = int(round(args.trigger_seconds / args.timestep))
    action_primitive = {
        "id": "front_vehicle_brake_after_trigger",
        "actor_role": "front_vehicle",
        "motion_frame": primitive.get("motion_frame", "lane_following"),
        "front_initial_speed_mps": front_speed_mps,
        "target_speed_mps": 0.0,
        "brake_intensity": args.brake_intensity,
        "trigger_frame": trigger_frame,
        "trigger_seconds": args.trigger_seconds,
        "direction": {
            "frame": "ego_local",
            "longitudinal_m": front_distance_m,
            "lateral_m": front_lateral_m,
            "heading_delta_deg": 0.0,
        },
        "acceptance_checks": primitive.get("acceptance_checks", ["front_vehicle_speed_drop", "front_vehicle_initially_ahead"]),
    }

    return {
        "level": "FrontBrakeLab",
        "source_l3_chain_id": "front_brake_lab",
        "chain_description": "Front vehicle hard-brake primitive lab: front actor follows lane, then brakes after trigger while ego approaches.",
        "direct_physical_outcome": "Ego closes distance to a braking front vehicle.",
        "expected_visual_result": "The selected front vehicle remains ahead of ego, follows its lane before the trigger, then visibly decelerates or stops.",
        "scenario_type": "front_vehicle_brake",
        "risk_family": risk_type.get("family", "lead_vehicle_risk"),
        "risk_type_id": "lead_vehicle_hard_brake",
        "primary_action_primitive_id": "front_vehicle_brake_after_trigger",
        "action_primitive": action_primitive,
        "source_l0_state_file": os.path.abspath(args.l0_json) if args.l0_json else None,
        "town": args.town,
        "map_absolute_path": scenic_map_path(args.town),
        "trigger_frame": trigger_frame,
        "trigger_seconds": args.trigger_seconds,
        "primary_actor": primary_actor,
        "success_criteria": {
            "front_actor_speed_drop_mps_min": args.min_speed_drop_mps,
            "front_distance_change_m_min": 0.5,
            "front_vehicle_initially_ahead": True,
            "must_match_scenario_type": "front_vehicle_brake",
            "must_use_primary_actor_from_config": True,
        },
        "l0_summary": {
            "ego": (l0_state or {}).get("ego"),
            "selected_actor_before_lab_override": selected_actor,
        },
    }


def main():
    default_carla_root = os.environ.get("CARLA_ROOT", "/mnt/data2/congfeng/CARLA")
    parser = argparse.ArgumentParser(description="Focused front vehicle hard-brake primitive lab.")
    parser.add_argument("--l0-json", default=None, help="Optional L0 state.json used only to select the front actor model/context.")
    parser.add_argument("--output-dir", default=os.path.join("carla_smoke", "workdir", "front_brake_lab", "manual"))
    parser.add_argument("--town", default="Town05")
    parser.add_argument("--front-model", default="vehicle.nissan.micra")
    parser.add_argument("--front-distance-m", type=float, default=None)
    parser.add_argument("--front-lateral-m", type=float, default=None)
    parser.add_argument("--front-speed-mps", type=float, default=None)
    parser.add_argument("--trigger-seconds", type=float, default=2.0)
    parser.add_argument("--brake-intensity", type=float, default=1.0)
    parser.add_argument("--min-speed-drop-mps", type=float, default=1.0)
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--opencode-repair-attempts", type=int, default=3)
    parser.add_argument("--spawn-check", dest="spawn_check", action="store_true", default=True)
    parser.add_argument("--no-spawn-check", dest="spawn_check", action="store_false")
    parser.add_argument("--spawn-semantic-check", dest="spawn_semantic_check", action="store_true", default=True)
    parser.add_argument("--no-spawn-semantic-check", dest="spawn_semantic_check", action="store_false")
    parser.add_argument("--spawn-check-frames", type=int, default=8)
    parser.add_argument("--spawn-check-save-every", type=int, default=1)
    parser.add_argument("--plan-model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--plan-url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--plan-timeout", type=float, default=300.0)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--api-key", default=None, help="Explicit API key. Prefer .env/API_KEY_ENV for shared runs.")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--validate-event-trace", action="store_true")
    parser.add_argument("--carla-root", default=default_carla_root)
    parser.add_argument("--carla-python", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2001)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--scene-sample-attempts", type=int, default=20)
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--warmup-ticks", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timestep", type=float, default=0.05)
    parser.add_argument("--ego-speed-difference", type=float, default=-5.0)
    parser.add_argument("--weather", default="ClearNoon")
    parser.add_argument("--camera-mode", choices=["front", "surround"], default="surround")
    args = parser.parse_args()

    l0_state = read_json(args.l0_json) if args.l0_json else {}
    selected_actor = select_front_vehicle(l0_state)
    config = build_config(args, l0_state, selected_actor)
    primitives = build_semantic_primitives(config, l0_state)

    os.makedirs(args.output_dir, exist_ok=True)
    task_source_path = os.path.join(args.output_dir, "l4_task_source.json")
    primitives_path = os.path.join(args.output_dir, "semantic_primitives.json")
    images_dir = os.path.join(args.output_dir, "risk_images")
    write_json(os.path.join(args.output_dir, "selected_front_actor.json"), selected_actor or default_front_actor())
    write_json(task_source_path, config)
    write_json(primitives_path, primitives)

    if args.execute:
        run_spawn_check(args, config, primitives, args.output_dir)

    workspace, workspace_task, workspace_primitives, output_scenic = prepare_workspace(args, task_source_path, primitives_path)
    run_opencode(args, workspace_task, workspace_primitives, output_scenic)

    print(f"Selected front actor: {(selected_actor or default_front_actor()).get('type_id')}")
    print(f"OpenCode workspace: {os.path.abspath(workspace)}")
    print(f"Generated Scenic: {os.path.abspath(output_scenic)}")
    if not args.execute:
        print("OpenCode generation only. Re-run with --execute to run CARLA capture and repair loop.")
        return 0

    run_scenic_validate_with_repair(args, workspace_task, workspace_primitives, output_scenic, images_dir, config, primitives)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
