#!/usr/bin/env python3
"""L4 scenario-language backend: semantic primitives -> OpenCode-generated Scenic -> CARLA images."""

import argparse
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys

try:
    from l4 import (
        DEFAULT_DEEPSEEK_MODEL,
        DEFAULT_DEEPSEEK_URL,
        DEFAULT_API_KEY_ENV,
        build_config,
        chain_output_dir,
        chains_from_data,
        chat_json,
        get_api_key,
        normalize_opencode_model_name,
        opencode_skills_dir,
        parse_json_response,
        read_json,
        select_chain,
        write_json,
    )
except ImportError:
    from .l4 import (
        DEFAULT_DEEPSEEK_MODEL,
        DEFAULT_DEEPSEEK_URL,
        DEFAULT_API_KEY_ENV,
        build_config,
        chain_output_dir,
        chains_from_data,
        chat_json,
        get_api_key,
        normalize_opencode_model_name,
        opencode_skills_dir,
        parse_json_response,
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


def format_float(value, digits=3):
    try:
        text = f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return None
    return text.rstrip("0").rstrip(".") if "." in text else text


def as_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_degrees(degrees):
    value = float(degrees)
    while value <= -180.0:
        value += 360.0
    while value > 180.0:
        value -= 360.0
    return value


def carla_to_scenic_position_2d(location):
    if not isinstance(location, dict):
        return None
    x_value = as_float(location.get("x"))
    y_value = as_float(location.get("y"))
    if x_value is None or y_value is None:
        return None
    x = format_float(x_value)
    y = format_float(-y_value)
    if x is None or y is None:
        return None
    return f"{x} @ {y}"


def carla_to_scenic_heading(rotation):
    if not isinstance(rotation, dict):
        return None
    yaw_value = as_float(rotation.get("yaw"))
    if yaw_value is None:
        return None
    scenic_heading_deg = normalize_degrees(-(yaw_value + 90.0))
    return f"{format_float(scenic_heading_deg)} deg"


def relative_carla_location_from_ego(ego, relative_longitudinal_m, relative_lateral_m, z_hint=None):
    ego_location = ego.get("location") if isinstance(ego, dict) else None
    ego_rotation = ego.get("rotation") if isinstance(ego, dict) else None
    if not isinstance(ego_location, dict) or not isinstance(ego_rotation, dict):
        return None
    ego_x = as_float(ego_location.get("x"))
    ego_y = as_float(ego_location.get("y"))
    ego_z = as_float(ego_location.get("z"), 0.0)
    ego_yaw = as_float(ego_rotation.get("yaw"))
    rel_long = as_float(relative_longitudinal_m)
    rel_lat = as_float(relative_lateral_m)
    if None in (ego_x, ego_y, ego_yaw, rel_long, rel_lat):
        return None
    yaw_rad = math.radians(ego_yaw)
    forward_x = math.cos(yaw_rad)
    forward_y = math.sin(yaw_rad)
    right_x = math.cos(yaw_rad + math.pi / 2.0)
    right_y = math.sin(yaw_rad + math.pi / 2.0)
    return {
        "x": round(ego_x + rel_long * forward_x + rel_lat * right_x, 3),
        "y": round(ego_y + rel_long * forward_y + rel_lat * right_y, 3),
        "z": round(as_float(z_hint, ego_z), 3),
    }


def generated_actor_with_pose(actor, ego):
    if not isinstance(actor, dict):
        return actor
    if actor.get("source") != "generated_object":
        return actor
    if isinstance(actor.get("location"), dict):
        return actor
    location = relative_carla_location_from_ego(
        ego,
        actor.get("relative_longitudinal_m"),
        actor.get("relative_lateral_m"),
        actor.get("z_hint_m", 0.7 if str(actor.get("kind") or "").lower() in {"pedestrian", "walker", "cyclist"} else None),
    )
    if not location:
        return actor
    ego_rotation = ego.get("rotation") if isinstance(ego, dict) else {}
    generated = dict(actor)
    generated["location"] = location
    generated.setdefault("rotation", {"pitch": 0.0, "yaw": as_float((ego_rotation or {}).get("yaw"), 0.0), "roll": 0.0})
    generated.setdefault("distance_m", math.sqrt((as_float(actor.get("relative_longitudinal_m"), 0.0) or 0.0) ** 2 + (as_float(actor.get("relative_lateral_m"), 0.0) or 0.0) ** 2))
    return generated


def lateral_side(relative_lateral_m):
    rel_lat = as_float(relative_lateral_m)
    if rel_lat is None:
        return None
    if rel_lat < -0.05:
        return "left"
    if rel_lat > 0.05:
        return "right"
    return "center"


def normalized_relative_position(relative_position, relative_longitudinal_m, relative_lateral_m):
    side = lateral_side(relative_lateral_m)
    if side not in {"left", "right"}:
        return relative_position
    rel_pos = str(relative_position or "").lower()
    if "front" in rel_pos:
        return f"front-{side}"
    if "rear" in rel_pos or "behind" in rel_pos or "back" in rel_pos:
        return f"rear-{side}"
    if rel_pos in {"left", "right", "side", ""}:
        rel_long = as_float(relative_longitudinal_m)
        if rel_long is not None and rel_long > 0.5:
            return f"front-{side}"
        if rel_long is not None and rel_long < -0.5:
            return f"rear-{side}"
        return side
    if "left" in rel_pos or "right" in rel_pos:
        return side
    return relative_position


def relative_to_ego_summary(actor):
    rel_long = as_float(
        actor.get("relative_longitudinal_m", actor.get("initial_relative_longitudinal_m")),
        None,
    )
    rel_lat = as_float(
        actor.get("relative_lateral_m", actor.get("initial_relative_lateral_m")),
        None,
    )
    distance = as_float(actor.get("distance_m", actor.get("initial_distance_m")), None)
    if rel_long is None and rel_lat is None:
        return None
    side = lateral_side(rel_lat)
    relative_position = normalized_relative_position(
        actor.get("relative_position") or actor.get("initial_relative_position"),
        rel_long,
        rel_lat,
    )
    summary = {
        "longitudinal_m": rel_long,
        "lateral_m": rel_lat,
        "lateral_distance_m": abs(rel_lat) if rel_lat is not None else None,
        "side": side,
        "relative_position": relative_position,
        "distance_m": distance,
        "same_lane_as_ego": actor.get("same_lane_as_ego"),
        "hard_constraints": {
            "preserve_side": side in ("left", "right"),
            "preserve_longitudinal_sign": rel_long is not None,
            "side_must_not_flip": side in ("left", "right"),
        },
        "same_side_search_policy": {
            "enabled": True,
            "why": "If the exact absolute Scenic position does not fit, preserve L0 ego-relative geometry instead of changing sides.",
            "allowed_lateral_m": same_side_lateral_candidates(rel_lat),
            "allowed_longitudinal_m": longitudinal_candidates(rel_long),
            "forbidden": [
                "changing left to right or right to left",
                "changing front to rear unless the original actor is rear",
                "dropping the original actor type/kind",
            ],
        },
    }
    return {key: value for key, value in summary.items() if value is not None}


def same_side_lateral_candidates(lateral_m):
    value = as_float(lateral_m)
    if value is None:
        return []
    sign = -1.0 if value < 0 else 1.0
    base = max(0.5, abs(value))
    candidates = [base, base - 0.5, base + 0.5, base - 1.0, base + 1.0, base + 1.5]
    cleaned = []
    for candidate in candidates:
        rounded = round(sign * max(0.5, candidate), 3)
        if rounded not in cleaned:
            cleaned.append(rounded)
    return cleaned


def longitudinal_candidates(longitudinal_m):
    value = as_float(longitudinal_m)
    if value is None:
        return []
    sign = -1.0 if value < 0 else 1.0
    base = abs(value)
    candidates = [base, base - 1.0, base + 1.0, base - 2.0, base + 2.0]
    cleaned = []
    for candidate in candidates:
        rounded = round(sign * max(0.5, candidate), 3)
        if rounded not in cleaned:
            cleaned.append(rounded)
    return cleaned


def normalize_town_name(source_map):
    if not source_map:
        return "Town05"
    name = os.path.basename(str(source_map))
    if name.endswith(".xodr"):
        name = name[:-5]
    return name or "Town05"


def scenic_map_absolute_path(source_map):
    if source_map and os.path.isabs(str(source_map)) and str(source_map).endswith(".xodr"):
        return os.path.abspath(str(source_map))
    town = normalize_town_name(source_map)
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


def actor_summary(actor):
    if not isinstance(actor, dict):
        return {}
    location = actor.get("location") or actor.get("initial_location")
    rotation = actor.get("rotation") or actor.get("initial_rotation")
    position_2d = carla_to_scenic_position_2d(location)
    heading = carla_to_scenic_heading(rotation)
    rel_long = actor.get("relative_longitudinal_m", actor.get("initial_relative_longitudinal_m"))
    rel_lat = actor.get("relative_lateral_m", actor.get("initial_relative_lateral_m"))
    distance = actor.get("distance_m", actor.get("initial_distance_m"))
    relative = relative_to_ego_summary(actor)
    relative_position = normalized_relative_position(
        actor.get("relative_position") or actor.get("initial_relative_position"),
        rel_long,
        rel_lat,
    )
    summary = {
        "source": actor.get("source"),
        "actor_id": actor.get("actor_id") or actor.get("id"),
        "type_id": actor.get("type_id"),
        "kind": actor.get("kind"),
        "role": actor.get("role") or actor.get("role_name"),
        "carla_location": location,
        "carla_rotation": rotation,
        "location": location,
        "rotation": rotation,
        "relative_position": relative_position,
        "relative_longitudinal_m": rel_long,
        "relative_lateral_m": rel_lat,
        "distance_m": distance,
    }
    if position_2d:
        summary["scenic_position_2d"] = position_2d
        summary["scenic_position_expression"] = f"({position_2d})"
    if heading:
        summary["scenic_heading"] = heading
    if relative:
        summary["relative_to_ego"] = relative
    if isinstance(location, dict) and location.get("z") is not None:
        summary["z_hint_m"] = location.get("z")
    return summary


def scenic_context_from_scene(scene):
    source_map = scene.get("source_map") or scene.get("preferred_town")
    town = normalize_town_name(source_map)
    map_path = scenic_map_absolute_path(source_map)
    return {
        "town": town,
        "map_absolute_path": map_path,
        "scenic_header": [
            f"Town = '{town}'",
            f"param map = localPath({json.dumps(map_path)})",
            "param carla_map = Town",
            "model scenic.simulators.carla.model",
        ],
        "coordinate_contract": {
            "source": "Scenic/src/scenic/simulators/carla/utils/utils.py",
            "carla_to_scenic_position": "scenic_x = carla_x; scenic_y = -carla_y",
            "carla_to_scenic_heading": "scenic_heading_deg = normalize_degrees(-(carla_yaw_deg + 90))",
            "world_position_format": "Use Scenic 2D coordinates: actor = Car at (x @ y).",
            "heading_format": "Use Scenic heading units: with heading yaw deg or facing yaw deg.",
            "z_policy": "Do not emit Point(x, y, z). Treat L0 z only as metadata/hint.",
            "relative_geometry": "Prefer actor.relative_to_ego over absolute coordinates when exact world points are not spawnable.",
            "same_side_policy": "If an actor has relative_to_ego.side left/right, repairs may adjust distances only within same_side_search_policy; never flip left/right.",
        },
    }


def scenic_context_from_l0(l0_state):
    source = l0_state.get("source", {}) if isinstance(l0_state, dict) else {}
    source_map = source.get("source_map") or source.get("map")
    return scenic_context_from_scene({"source_map": source_map})


def primitive(name, **fields):
    item = {"primitive": name}
    item.update({key: value for key, value in fields.items() if value is not None})
    return item


def nearest_front_actor_from_l0(l0_state):
    actors = l0_state.get("actors", []) if isinstance(l0_state, dict) else []
    candidates = []
    for actor in actors if isinstance(actors, list) else []:
        if not isinstance(actor, dict):
            continue
        type_id = str(actor.get("type_id", "")).lower()
        kind = str(actor.get("kind", "")).lower()
        if kind != "vehicle" and not type_id.startswith("vehicle."):
            continue
        rel_long = as_float(actor.get("relative_longitudinal_m"))
        rel_lat = as_float(actor.get("relative_lateral_m"), 0.0)
        if rel_long is None or rel_long < -0.5:
            continue
        if abs(rel_lat) > 4.5:
            continue
        candidates.append(actor)
    return min(candidates, key=lambda actor: as_float(actor.get("relative_longitudinal_m"), 9999.0)) if candidates else {}


def build_semantic_primitives(config, l0_state=None):
    l0_state = l0_state or {}
    scenario_type = config.get("scenario_type", "unknown")
    ego_source = (l0_state or {}).get("ego") or {}
    primary_source = generated_actor_with_pose(config.get("primary_actor") or {}, ego_source)
    primary_actor = actor_summary(primary_source)
    ego = actor_summary(ego_source)
    front_actor = actor_summary(nearest_front_actor_from_l0(l0_state))
    trigger_frame = int(config.get("trigger_frame", 20) or 20)
    scenic_context = scenic_context_from_l0(l0_state)
    action = config.get("action_primitive") or {}

    primitives = [
        primitive(
            "set_scene_context",
            town=scenic_context["town"],
            map_absolute_path=scenic_context["map_absolute_path"],
            scenic_header=scenic_context["scenic_header"],
            coordinate_contract=scenic_context["coordinate_contract"],
            weather=(l0_state or {}).get("weather"),
        ),
        primitive(
            "spawn_ego",
            actor=ego,
            behavior="follow_lane",
        ),
    ]

    if front_actor and scenario_type != "front_vehicle_brake":
        primitives.append(
            primitive(
                "spawn_actor_relative",
                role="front_or_occluder_actor",
                actor=front_actor,
                relative_to="ego",
                preserve_relative_geometry=True,
            )
        )

    if scenario_type != "weather_visibility_change":
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
                    front_initial_speed_mps=action.get("front_initial_speed_mps"),
                    target_speed_mps=action.get("target_speed_mps"),
                    reverse_speed_mps=action.get("reverse_speed_mps"),
                    brake_intensity=action.get("brake_intensity"),
                    stop_condition=action.get("stop_condition"),
                    velocity_vector=action.get("velocity_vector"),
                    trigger_seconds=action.get("trigger_seconds"),
                    direction=action.get("direction"),
                    must_be_visible=True,
                ),
                primitive("record_expectation", expectation="front vehicle performs configured hard-brake or low-speed reverse-toward-ego hazard while ego approaches"),
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
                    action=action,
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
                    action=action,
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
                    action=action,
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
                    trigger_frame=trigger_frame,
                    action=action,
                    target="ego_path",
                ),
            ]
        )
    elif scenario_type == "ego_action_risk":
        primitives.extend(
            [
                primitive(
                    "ego_action_risk",
                    actor="ego",
                    hazard_actor="primary_risk_actor",
                    trigger_frame=trigger_frame,
                    action=action,
                    preserve_hazard_actor=True,
                ),
                primitive("record_expectation", expectation="ego continues toward an existing hazard instead of braking or avoiding"),
            ]
        )
    elif scenario_type == "weather_visibility_change":
        primitives.extend(
            [
                primitive("follow_lane", actor="ego", until_frame=trigger_frame),
                primitive(
                    "weather_visibility_change",
                    actor="environment",
                    trigger_frame=trigger_frame,
                    action=action,
                    target_weather=action.get("weather"),
                ),
                primitive("record_expectation", expectation="environment visibility degrades to night conditions while ego keeps moving"),
            ]
        )
    else:
        primitives.append(
            primitive(
                "apply_configured_primary_action",
                actor="primary_risk_actor",
                trigger_frame=trigger_frame,
                action=action,
            )
        )

    return {
        "level": "L4SemanticPrimitives",
        "description": "Structured primitive graph used by OpenCode to generate Scenic scenario-language code.",
        "risk_family": config.get("risk_family"),
        "risk_type_id": config.get("risk_type_id"),
        "primary_action_primitive_id": config.get("primary_action_primitive_id"),
        "action_primitive": config.get("action_primitive"),
        "action_primitives": config.get("action_primitives") or [],
        "participant_actions": config.get("participant_actions") or [],
        "scenario_type": scenario_type,
        "source_l3_chain_id": config.get("source_l3_chain_id"),
        "trigger_frame": trigger_frame,
        "primary_actor": primary_actor,
        "semantic_primitives": primitives,
        "success_criteria": config.get("success_criteria"),
    }


def prepare_workspace(args, task_source_path, primitives_path, spawn_scenic_path=None):
    workspace = os.path.join(args.output_dir, "opencode_scenario_language_workspace")
    os.makedirs(workspace, exist_ok=True)
    workspace_task = os.path.join(workspace, "l4_task.json")
    workspace_primitives = os.path.join(workspace, "semantic_primitives.json")
    output_scenic = os.path.join(workspace, "generated_risk_scene.scenic")
    shutil.copy2(primitives_path, workspace_primitives)
    if args.l0_json:
        shutil.copy2(args.l0_json, os.path.join(workspace, "l0_state.json"))
    else:
        write_json(os.path.join(workspace, "l0_state.json"), {})
    write_opencode_provider_config(args, workspace)
    l0_state = read_json(os.path.join(workspace, "l0_state.json"))

    primitives = read_json(primitives_path)
    context = next(
        (
            item
            for item in primitives.get("semantic_primitives", [])
            if isinstance(item, dict) and item.get("primitive") == "set_scene_context"
        ),
        {},
    )
    town = context.get("town") or "Town05"
    map_path = context.get("map_absolute_path") or scenic_map_absolute_path(town)
    if spawn_scenic_path and os.path.exists(spawn_scenic_path):
        shutil.copy2(spawn_scenic_path, output_scenic)
        with open(output_scenic, "a", encoding="utf-8") as f:
            f.write(
                "\n# OpenCode: this file starts from the CARLA-validated spawn_check.scenic.\n"
                "# Preserve ego and primary_actor spawn declarations exactly; add only behavior, timing, and trace-compatible scenario logic.\n"
            )
    else:
        seed = (
            "'''OpenCode must replace this seed with a complete Scenic scenario generated from semantic_primitives.json.'''\n"
            f"Town = {json.dumps(town)}\n"
            f"param map = localPath({json.dumps(map_path)})\n"
            "param carla_map = Town\n"
            "model scenic.simulators.carla.model\n"
            'EGO_MODEL = "vehicle.lincoln.mkz_2017"\n\n'
            "# TODO: implement ego, primary risk actor, and behavior from semantic_primitives.json.\n"
            "# Coordinate rule: use converted Scenic 2D positions from semantic_primitives.json, never raw CARLA x/y or Point(x, y, z).\n"
        )
        with open(output_scenic, "w", encoding="utf-8") as f:
            f.write(seed)

    workspace_skills = os.path.join(workspace, ".opencode", "skills")
    if os.path.isdir(workspace_skills):
        shutil.rmtree(workspace_skills)
    os.makedirs(workspace_skills, exist_ok=True)
    scenario_language_skill = os.path.join(opencode_skills_dir(), "l4-scenario-language-codegen")
    shutil.copytree(
        scenario_language_skill,
        os.path.join(workspace_skills, "l4-scenario-language-codegen"),
    )
    with open(os.path.join(workspace, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write(
            "# OpenCode Workspace Instructions\n\n"
            "MANDATORY SKILL: l4-scenario-language-codegen.\n"
            "Read l4_task.json as the single business input; semantic_primitives.json is only a primitive trace.\n"
            "Generate Scenic scenario-language code only; do not generate CARLA Python code.\n"
            "Edit only generated_risk_scene.scenic.\n"
            "generated_risk_scene.scenic starts from a CARLA-validated spawn_check.scenic whenever available; preserve the existing ego/primary_actor spawn lines.\n"
            "The file must use the absolute map path from l4_task.scene_context.map_absolute_path.\n"
            "Use precomputed Scenic 2D coordinates from l4_task; never copy raw CARLA y/yaw directly.\n"
            "Never write tolerance shorthand like `12.352 +/- 1.0`; use Scenic `Range(11.352, 13.352)`.\n"
            "When absolute placement fails, preserve actor.relative_to_ego.side and use same_side_search_policy.\n"
            "Write event behavior so semantic_validation in event_trace.json can pass after execution.\n"
            "The file must be executable by Scenic/CARLA.\n"
        )
    config = read_json(task_source_path)
    write_json(
        workspace_task,
        {
            "level": "L4OpenCodeTask",
            "description": "Single self-contained task file for OpenCode Scenic generation.",
            "scene_context": {
                "town": town,
                "map_absolute_path": map_path,
                "coordinate_contract": context.get("coordinate_contract"),
                "weather": context.get("weather"),
            },
            "risk": {
                "risk_family": config.get("risk_family"),
                "risk_type_id": config.get("risk_type_id"),
                "scenario_type": config.get("scenario_type"),
                "chain_description": config.get("chain_description"),
                "direct_physical_outcome": config.get("direct_physical_outcome"),
                "expected_visual_result": config.get("expected_visual_result"),
            },
            "actors": {
                "ego": next(
                    (
                        item.get("actor")
                        for item in primitives.get("semantic_primitives", [])
                        if isinstance(item, dict) and item.get("primitive") == "spawn_ego"
                    ),
                    {},
                ),
                "primary_actor": primitives.get("primary_actor"),
                "background_actors": [
                    item.get("actor")
                    for item in primitives.get("semantic_primitives", [])
                    if isinstance(item, dict) and item.get("role") == "front_or_occluder_actor"
                ],
                "l0_state": l0_state,
            },
            "actions": {
                "action_primitive": config.get("action_primitive"),
                "action_primitives": config.get("action_primitives") or [],
                "participant_actions": config.get("participant_actions") or [],
            },
            "acceptance_criteria": config.get("success_criteria"),
            "output_contract": config.get("event_contract"),
        },
    )
    write_json(
        os.path.join(workspace, "opencode_inputs.json"),
        {
            "l4_task": workspace_task,
            "semantic_primitives": workspace_primitives,
            "l0_state": os.path.join(workspace, "l0_state.json"),
            "output_scenic": output_scenic,
        },
    )
    return workspace, workspace_task, workspace_primitives, output_scenic


def write_opencode_provider_config(args, workspace):
    model_name = normalize_opencode_model_name(getattr(args, "opencode_model", None))
    if model_name != "aihubmix/glm-5.1":
        return
    write_json(
        os.path.join(workspace, "opencode.json"),
        {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "aihubmix": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "AIHubMix",
                    "options": {
                        "baseURL": "https://aihubmix.com/v1",
                        "apiKey": "{env:AIHUBMIX_API_KEY}",
                    },
                    "models": {
                        "glm-5.1": {
                            "name": "GLM-5.1"
                        }
                    },
                }
            },
        },
    )


def opencode_prompt(task_path, primitives_path, output_scenic):
    task_path = os.path.join(os.path.dirname(output_scenic), "l4_task.json")
    return f"""MANDATORY SKILL: l4-scenario-language-codegen.

Task:
- Read l4_task.json as the single business input:
  {task_path}
- Read l0_state.json.
- Edit exactly this Scenic file in place:
  {output_scenic}

Requirements:
- Generate Scenario/Scenic language, not Python.
- generated_risk_scene.scenic already starts from the CARLA-validated spawn_check.scenic when available.
- Preserve existing `ego = ...` and `primary_actor = ...` spawn declarations exactly. Do not replace them with `ego offset by`, `left of ego`, `right of ego`, or a newly sampled intersection/lane spawn.
- Add or edit only behavior/timing declarations and scenario logic needed to realize the action primitive and pass validation.
- Follow l4_task.actions.action_primitive as the hard action primitive.
- Implement primary risk actions aggressively. Do not weaken high lateral/crossing speeds, hard braking or conditional reverse motion, target-lane intrusion depth, or no-braking ego behavior into gentle lane following.
- For front_vehicle_brake / front_vehicle_brake_after_trigger, default to hard braking. Only implement sudden reverse_toward_ego when action_primitive.front_action_variant == "reverse_toward_ego" or reverse_speed_mps is present.
- Use l4_task.scene_context.map_absolute_path exactly in `param map = localPath(...)`.
- Use the precomputed `actor.scenic_position_expression` and `actor.scenic_heading` from l4_task; never convert raw CARLA x/y/yaw yourself.
- Never write tolerance shorthand like `12.352 +/- 1.0`; Scenic ranges must be written as `Range(11.352, 13.352)`.
- In `following roadDirection from ego for ...`, use a numeric literal or `Range(lower, upper)`, never `a +/- b`.
- Prefer `actor.relative_to_ego` for L0 actors. If exact placement fails, use `same_side_search_policy` and keep the original left/right side.
- Do not over-constrain Scenic placement around one absolute point. Absolute L0 poses are hints; ego-relative same-side geometry is the acceptance target.
- Preserve l4_task.risk.scenario_type exactly.
- Preserve primary actor kind/type, ego-relative geometry, concrete action primitive, numeric direction, speed, brake intensity, and trigger timing.
- For weather_visibility_change, implement the environment/weather action directly; do not invent a physical primary actor.
- For weather_visibility_change, use the selected action_primitive.weather profile exactly; it is randomly chosen upstream from clear_night, hard_rain_night, hard_rain_sunset, and dust_storm.
- Define every `behavior`, `monitor`, helper function, and constant before the first object declaration or `with behavior ...` reference that uses it. Scenic does not allow forward references to behavior names.
- Never write `require <object> do <Behavior>()`; Scenic `require` is only for boolean constraints. Bind actor behavior in the object declaration with `with behavior Behavior(...)`. Do not attach a custom behavior to `ego` unless the scenario type is `ego_action_risk`; the SafeBench runtime normally controls ego through CARLA Traffic Manager.
- L0 absolute coordinates are hints; relative geometry is authoritative.
- The generated Scenic must run through carla_smoke/scenes/safebench_scenic_scene.py.
- Do not write Markdown. Do not ask questions. Edit only generated_risk_scene.scenic.
"""


def opencode_repair_prompt(task_path, primitives_path, output_scenic, repair_feedback):
    task_path = os.path.join(os.path.dirname(output_scenic), "l4_task.json")
    return f"""The generated Scenic scenario failed during Scenic/CARLA execution.

MANDATORY SKILL: l4-scenario-language-codegen.

Single task input:
  {task_path}

Scenic file to fix:
  {output_scenic}

Repair feedback:
{feedback_to_text(repair_feedback)}

Repair the Scenic file in place.
- Keep the same scenario_type and semantic primitive intent.
- Fix the concrete issue described in Repair feedback.
- Keep `param map = localPath(...)` on the absolute map path from l4_task.json.
- Replace any `Point(x, y, z)` / CARLA Python coordinate syntax with Scenic 2D `x @ y` syntax.
- Replace any tolerance shorthand like `12.352 +/- 1.0` with Scenic `Range(11.352, 13.352)`.
- In `following roadDirection from ego for ...`, use a numeric literal or `Range(lower, upper)`, never `a +/- b`.
- Do not flip actor.relative_to_ego.side. If a left-side actor does not fit, search only left-side distances; if a right-side actor does not fit, search only right-side distances.
- If exact absolute placement fails or Scenic cannot sample the scene, stop hard-coding the failed absolute point. Use ego-relative placement and same-side nearby search while preserving actor type, side, and front/rear relation.
- If this is a semantic validation failure, use the failed checks in Repair feedback as the repair target. Each failed check includes target, actual, and reason; edit the Scenic scenario so those checks pass.
- If a behavior name is undefined, move or add the corresponding `behavior ...` definition before the object declaration that uses `with behavior ...`; do not leave forward references.
- Never use `require <object> do <Behavior>()`; replace it with `with behavior Behavior(...)` in the relevant object declaration, or remove the ego behavior entirely when ego is Traffic-Manager controlled.
- Do not satisfy semantic validation by changing l4_task.json, semantic_primitives.json, event_trace.json, actor type, or scenario_type. Fix only generated_risk_scene.scenic.
- If a parameter used in range(...) can be float, cast it to int(...) or replace it with a fixed integer.
- Do not switch to Python code.
- Do not write Markdown. Edit only generated_risk_scene.scenic.
"""


def run_command(command, capture_output=False, env=None):
    print("\n$ " + " ".join(command))
    if capture_output:
        result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        if result.stdout:
            print(result.stdout)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, command, output=result.stdout)
        return result
    subprocess.run(command, check=True, env=env)
    return None


def error_text(exc):
    output = getattr(exc, "output", None)
    if output:
        return str(output)
    return repr(exc)


def feedback_to_text(feedback):
    if isinstance(feedback, (dict, list)):
        return json.dumps(feedback, ensure_ascii=False, indent=2)
    return str(feedback)


def extract_error_line(error_output):
    lines = [line.strip() for line in str(error_output or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if any(token in line for token in ("Error:", "Exception:", "InvalidScenarioError", "ScenicSyntaxError", "NameError")):
            return line
    return lines[-1] if lines else "unknown error"


def build_execution_repair_feedback(error_output):
    text = str(error_output or "")
    invalid_require_behavior = re.search(r"^\s*require\s+([A-Za-z_][A-Za-z0-9_]*)\s+do\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text, re.MULTILINE)
    feedback = {
        "kind": "execution_error",
        "issue": "Scenic/CARLA execution failed before a valid event_trace was produced.",
        "evidence": extract_error_line(text),
        "repair_requirements": [
            "Keep l4_task.risk.scenario_type, primary actor type/kind, trigger frame, and intended action unchanged.",
            "Fix generated_risk_scene.scenic only; do not edit JSON inputs.",
        ],
    }
    location_match = re.search(r"Object at \(([^)]+)\) does not fit in container", text)
    if invalid_require_behavior:
        feedback.update(
            {
                "issue": "Generated Scenic code used invalid behavior binding syntax: `require <object> do <Behavior>()`.",
                "invalid_actor": invalid_require_behavior.group(1),
                "invalid_behavior": invalid_require_behavior.group(2),
                "repair_requirements": [
                    "Scenic `require` is only for boolean constraints; never use it to run or bind behaviors.",
                    "Bind actor behavior in the object declaration with `with behavior Behavior(...)`.",
                    "If the invalid actor is `ego` and scenario_type is not `ego_action_risk`, remove the custom ego behavior and let the SafeBench runtime control ego through CARLA Traffic Manager.",
                    "Keep behavior definitions before their first `with behavior ...` use.",
                ],
            }
        )
    elif "does not fit in container" in text or location_match:
        failed_point = location_match.group(1) if location_match else "unknown"
        feedback.update(
            {
                "issue": "Scenic rejected an object placement because the absolute point is outside the allowed container.",
                "failed_point": failed_point,
                "repair_requirements": [
                    "Do not hard-code or retry the failed absolute point.",
                    "Use ego-relative placement from semantic_primitives.actor.relative_to_ego instead of strict absolute placement.",
                    "Preserve the original left/right side and front/rear relation; search only nearby same-side distances.",
                    "For pedestrians, place the actor at a Scenic/CARLA-valid nearby road or sidewalk-compatible position, then move it according to the requested intrusion action.",
                    "Keep scenario_type, actor type/kind, trigger frame, and primary action unchanged.",
                ],
            }
        )
    elif "failed to generate scenario" in text or "RejectionException" in text:
        feedback.update(
            {
                "issue": "Scenic could not sample a valid scene under the current constraints.",
                "repair_requirements": [
                    "Relax over-specific placement constraints in generated_risk_scene.scenic.",
                    "Prefer ego-relative placement over exact absolute coordinates.",
                    "Keep same-side geometry and actor type, but allow small nearby shifts that make Scenic sampling feasible.",
                    "Avoid combining incompatible region/container constraints.",
                ],
            }
        )
    elif "NameError" in text and "not defined" in text:
        missing_name_match = re.search(r"NameError:\s+name ['\"]([^'\"]+)['\"] is not defined", text)
        missing_name = missing_name_match.group(1) if missing_name_match else None
        feedback.update(
            {
                "issue": "Generated Scenic code references a name before defining it.",
                "missing_name": missing_name,
                "repair_requirements": [
                    "Define every behavior, monitor, helper function, and constant before the first use.",
                    "If the missing name is a behavior, move or add the full `behavior ...` block above the object declaration using `with behavior ...`.",
                    "Do not rename the behavior only at the call site; keep the definition and use site consistent.",
                    "For weather_visibility_change, no physical primary actor is required; the weather trigger behavior/monitor may be attached to an existing background actor or implemented as a monitor, but it must be defined before use.",
                ],
            }
        )
    elif "ScenicSyntaxError" in text:
        feedback.update(
            {
                "issue": "Generated Scenic code has a syntax error.",
                "repair_requirements": [
                    "Fix Scenic syntax in generated_risk_scene.scenic.",
                    "Replace tolerance shorthand like `12.352 +/- 1.0` with Scenic `Range(11.352, 13.352)`.",
                    "In `following roadDirection from ego for ...`, use a numeric literal or `Range(lower, upper)`, never `a +/- b`.",
                    "Avoid unsupported constructs such as timed `take ... for ... seconds` if Scenic rejects them.",
                    "Use valid Scenic behavior syntax for actions over time.",
                ],
            }
        )
    return feedback


def repair_requirement_for_check(check):
    name = check.get("name")
    if name == "ego_moving_near_trigger":
        return "Make ego keep moving near trigger_frame; avoid starting or stopping ego before the risk event."
    if name == "vulnerable_actor_approaches_ego":
        return "Adjust the vulnerable actor path so distance_to_ego_m decreases after trigger_frame."
    if name == "vulnerable_actor_moves_toward_ego_lane":
        return "Adjust the vulnerable actor path so abs(relative_lateral_m) decreases toward the ego lane after trigger_frame."
    if name == "initial_side_preserved":
        return "Preserve the original left/right side when placing the primary actor."
    if name in {"initial_lateral_tolerance", "initial_longitudinal_tolerance"}:
        return "Place the primary actor closer to the configured ego-relative longitudinal/lateral offset."
    if name == "front_vehicle_speed_drop":
        return "Make the front vehicle visibly decelerate or stop after trigger_frame."
    if name == "front_vehicle_initially_ahead":
        return "Place the front vehicle ahead of ego with positive relative_longitudinal_m."
    return "Modify generated_risk_scene.scenic so this failed check passes without changing JSON inputs."


def build_semantic_repair_feedback(trace):
    validation = (trace or {}).get("semantic_validation") or {}
    failed_checks = []
    for check in validation.get("checks") or []:
        if check.get("passed"):
            continue
        failed_checks.append(
            {
                "name": check.get("name"),
                "target": check.get("target"),
                "actual": check.get("actual"),
                "reason": check.get("reason") or check.get("name"),
                "repair_requirement": repair_requirement_for_check(check),
            }
        )
    return {
        "kind": "semantic_error",
        "issue": "Scenic executed and produced images/trace, but the physical event did not satisfy semantic validation.",
        "scenario_type": (trace or {}).get("scenario_type"),
        "trigger_frame": (trace or {}).get("trigger_frame"),
        "failed_checks": failed_checks,
        "repair_requirements": [
            "Fix generated_risk_scene.scenic so the failed target/actual checks pass.",
            "Do not change scenario_type, primary actor type/kind, trigger_frame, l4_task.json, semantic_primitives.json, or event_trace.json.",
            "If an absolute pose is hard to satisfy, use same-side ego-relative placement while preserving the intended risk semantics.",
        ],
    }


def build_repair_feedback(error_output, trace=None):
    if isinstance(trace, dict) and trace.get("semantic_validation"):
        return build_semantic_repair_feedback(trace)
    return build_execution_repair_feedback(error_output)


def opencode_env(args):
    env = os.environ.copy()
    if getattr(args, "api_key", None):
        env.setdefault("AIHUBMIX_API_KEY", args.api_key)
    return env


def run_opencode(args, task_path, primitives_path, output_scenic):
    opencode_bin = shutil.which(args.opencode_bin)
    if not opencode_bin:
        raise RuntimeError(f"opencode binary not found: {args.opencode_bin}")
    prompt = opencode_prompt(task_path, primitives_path, output_scenic)
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
        env=opencode_env(args),
    )
    if not os.path.exists(output_scenic):
        raise RuntimeError(f"opencode completed but did not create expected Scenic file: {output_scenic}")


def repair_opencode(args, task_path, primitives_path, output_scenic, repair_feedback):
    opencode_bin = shutil.which(args.opencode_bin)
    if not opencode_bin:
        raise RuntimeError(f"opencode binary not found: {args.opencode_bin}")
    prompt = opencode_repair_prompt(task_path, primitives_path, output_scenic, repair_feedback)
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
        env=opencode_env(args),
    )


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def infer_saved_frame(path, state):
    frame = state.get("frame") or safe_get(state, "source", "frame")
    if frame is not None:
        try:
            return int(frame)
        except (TypeError, ValueError):
            pass
    for text in (os.path.basename(path), str(safe_get(state, "source", "image_file") or "")):
        match = re.search(r"(?:state|rgb)_(\d+)", text)
        if match:
            return int(match.group(1))
    return None


def point_from_value(value):
    if not isinstance(value, dict):
        return None
    x = as_float(value.get("x"))
    y = as_float(value.get("y"))
    z = as_float(value.get("z"), 0.0)
    if x is None or y is None:
        return None
    return {"x": x, "y": y, "z": z}


def point_distance(a, b):
    if not a or not b:
        return None
    return math.sqrt(
        (float(a.get("x", 0.0)) - float(b.get("x", 0.0))) ** 2
        + (float(a.get("y", 0.0)) - float(b.get("y", 0.0))) ** 2
        + (float(a.get("z", 0.0)) - float(b.get("z", 0.0))) ** 2
    )


def signed_side(value):
    numeric = as_float(value)
    if numeric is None or abs(numeric) <= 0.05:
        return 0
    return -1 if numeric < 0 else 1


def primary_actor_candidates(actors, primary):
    if not isinstance(actors, list):
        return []
    expected_type = primary.get("type_id")
    expected_kind = primary.get("kind")
    candidates = []
    for actor in actors:
        if not isinstance(actor, dict):
            continue
        score = 0
        if expected_type and actor.get("type_id") == expected_type:
            score += 10
        if expected_kind and actor.get("kind") == expected_kind:
            score += 5
        if score:
            candidates.append((score, actor))
    if not candidates and expected_type:
        candidates = [(1, actor) for actor in actors if isinstance(actor, dict) and str(actor.get("type_id", "")).split(".")[0] == str(expected_type).split(".")[0]]
    candidates.sort(key=lambda item: (-item[0], abs(as_float(item[1].get("distance_m"), 9999.0))))
    return [actor for _, actor in candidates]


def select_primary_actor(state, primary):
    candidates = primary_actor_candidates(state.get("actors") or [], primary)
    if not candidates:
        return None
    relative = primary.get("relative_to_ego") or {}
    expected_long = as_float(relative.get("longitudinal_m"), as_float(primary.get("relative_longitudinal_m")))
    expected_lat = as_float(relative.get("lateral_m"), as_float(primary.get("relative_lateral_m")))
    if expected_long is None and expected_lat is None:
        return candidates[0]

    def score(actor):
        total = 0.0
        if expected_long is not None:
            total += abs(as_float(actor.get("relative_longitudinal_m"), 9999.0) - expected_long)
        if expected_lat is not None:
            total += abs(as_float(actor.get("relative_lateral_m"), 9999.0) - expected_lat)
        return total

    return min(candidates, key=score)


def frame_primary_fields(state, primary):
    actor = select_primary_actor(state, primary)
    if not actor:
        return {"primary_actor_found": False}
    location = point_from_value(actor.get("location"))
    fields = {
        "primary_actor_found": True,
        "primary_actor_id": actor.get("id") or actor.get("actor_id"),
        "primary_actor_type_id": actor.get("type_id"),
        "primary_actor_kind": actor.get("kind"),
        "primary_actor_speed_mps": actor.get("speed_mps"),
        "primary_actor_position": location,
        "relative_position": actor.get("relative_position"),
        "relative_longitudinal_m": actor.get("relative_longitudinal_m"),
        "relative_lateral_m": actor.get("relative_lateral_m"),
        "distance_to_ego_m": actor.get("distance_m"),
        "same_lane_as_ego": actor.get("same_lane_as_ego"),
    }
    primary_type = str(primary.get("type_id", ""))
    if primary.get("kind") in ("pedestrian", "walker", "cyclist") or primary_type.startswith("walker"):
        fields["vulnerable_actor_position"] = location
    if primary.get("kind") == "vehicle" or primary_type.startswith("vehicle"):
        fields["front_actor_speed_mps"] = actor.get("speed_mps")
        fields["front_distance_m"] = actor.get("relative_longitudinal_m") or actor.get("distance_m")
    return fields


def write_event_trace_from_states(images_dir, config, primitives):
    frames = []
    primary = primitives.get("primary_actor") or {}
    for path in sorted(glob.glob(os.path.join(images_dir, "state_*.json"))):
        state = read_json(path)
        ego = state.get("ego") or {}
        frame = {
            "frame": infer_saved_frame(path, state),
            "ego_speed_mps": ego.get("speed_mps"),
            "ego_location": ego.get("location"),
            "weather": state.get("weather"),
            "actor_count": len(state.get("actors") or []),
            "image_file": safe_get(state, "source", "image_file")
            or os.path.basename(path).replace("state_", "rgb_").replace(".json", ".png"),
        }
        frame.update(frame_primary_fields(state, primary))
        frames.append(frame)
    scenario_type = config.get("scenario_type")
    trace = {
        "scenario_type": scenario_type,
        "trigger_frame": config.get("trigger_frame", 20),
        "event_applied": f"{scenario_type} generated by scenario-language Scenic backend",
        "execution_backend": "scenario_language_opencode_scenic",
        "source_l3_chain_id": config.get("source_l3_chain_id"),
        "semantic_primitives_file": "semantic_primitives.json",
        "generated_scenic_file": "opencode_scenario_language_workspace/generated_risk_scene.scenic",
        "expected_primary_actor": primary,
        "frames": frames,
        "note": "Trace is reconstructed from Scenic capture state files and includes primary actor relative geometry for semantic validation.",
        "primitive_count": len(primitives.get("semantic_primitives") or []),
    }
    write_json(os.path.join(images_dir, "event_trace.json"), trace)
    return trace


def split_frames_by_trigger(frames, trigger_frame):
    before = []
    after = []
    for frame in frames:
        frame_index = frame.get("frame")
        if frame_index is None or frame_index >= trigger_frame:
            after.append(frame)
        else:
            before.append(frame)
    return before, after


def numeric_values(frames, key):
    values = []
    for frame in frames:
        value = as_float(frame.get(key))
        if value is not None:
            values.append(value)
    return values


def point_values(frames, key):
    return [point for point in (point_from_value(frame.get(key)) for frame in frames) if point]


def check_record(name, target, actual, passed, reason=None):
    record = {
        "name": name,
        "target": target,
        "actual": actual,
        "passed": bool(passed),
    }
    if reason:
        record["reason"] = reason
    return record


def append_check(checks, name, target, actual, passed, reason=None):
    checks.append(check_record(name, target, actual, passed, reason))
    return passed


def fail_reasons(checks):
    return [check.get("reason") or check.get("name") for check in checks if not check.get("passed")]


def validate_initial_relative_geometry(primary, frames, checks):
    relative = primary.get("relative_to_ego") or {}
    expected_lat = as_float(relative.get("lateral_m"), as_float(primary.get("relative_lateral_m")))
    expected_long = as_float(relative.get("longitudinal_m"), as_float(primary.get("relative_longitudinal_m")))
    if expected_lat is None and expected_long is None:
        return
    first = next((frame for frame in frames if frame.get("primary_actor_found")), None)
    if not append_check(
        checks,
        "primary_actor_found",
        "primary actor matching expected type/kind appears in captured states",
        bool(first),
        bool(first),
        "primary actor was not found in captured states",
    ):
        return
    actual_lat = as_float(first.get("relative_lateral_m"))
    actual_long = as_float(first.get("relative_longitudinal_m"))
    if expected_lat is not None:
        append_check(
            checks,
            "initial_lateral_present",
            "primary actor relative_lateral_m is present",
            actual_lat,
            actual_lat is not None,
            "primary actor relative_lateral_m is missing",
        )
        if actual_lat is not None:
            append_check(
                checks,
                "initial_side_preserved",
                {"expected_lateral_sign": signed_side(expected_lat), "expected_lateral_m": expected_lat},
                {"actual_lateral_sign": signed_side(actual_lat), "actual_lateral_m": actual_lat},
                signed_side(expected_lat) == 0 or signed_side(actual_lat) == signed_side(expected_lat),
                f"primary actor side flipped; expected lateral {expected_lat:.3f}, got {actual_lat:.3f}",
            )
            append_check(
                checks,
                "initial_lateral_tolerance",
                {"expected_lateral_m": expected_lat, "tolerance_m": 1.5},
                {"actual_lateral_m": actual_lat, "error_m": abs(actual_lat - expected_lat)},
                abs(actual_lat - expected_lat) <= 1.5,
                f"primary actor lateral offset drifted too far; expected {expected_lat:.3f}, got {actual_lat:.3f}",
            )
    if expected_long is not None:
        append_check(
            checks,
            "initial_longitudinal_present",
            "primary actor relative_longitudinal_m is present",
            actual_long,
            actual_long is not None,
            "primary actor relative_longitudinal_m is missing",
        )
        if actual_long is not None:
            append_check(
                checks,
                "initial_longitudinal_tolerance",
                {"expected_longitudinal_m": expected_long, "tolerance_m": 3.0},
                {"actual_longitudinal_m": actual_long, "error_m": abs(actual_long - expected_long)},
                abs(actual_long - expected_long) <= 3.0,
                f"primary actor longitudinal offset drifted too far; expected {expected_long:.3f}, got {actual_long:.3f}",
            )


def validate_scenario_language_event_trace(config, primitives, trace):
    checks = []
    frames = trace.get("frames") or []
    primary = primitives.get("primary_actor") or {}
    append_check(
        checks,
        "frames_present",
        "event_trace.frames is non-empty",
        len(frames),
        bool(frames),
        "event_trace.frames is empty",
    )
    if frames:
        validate_initial_relative_geometry(primary, frames, checks)

    scenario_type = trace.get("scenario_type")
    trigger_frame = int(trace.get("trigger_frame") or 20)
    before, after = split_frames_by_trigger(frames, trigger_frame)
    append_check(
        checks,
        "after_trigger_frames_present",
        {"trigger_frame": trigger_frame},
        len(after),
        bool(after),
        f"no frames at or after trigger_frame={trigger_frame}",
    )

    if frames and after and scenario_type == "vulnerable_actor_intrusion":
        nearby = [
            frame
            for frame in frames
            if frame.get("frame") is None or abs(int(frame.get("frame")) - trigger_frame) <= 10
        ]
        ego_trigger_speeds = numeric_values(nearby or before[-3:] or frames[:3], "ego_speed_mps")
        append_check(
            checks,
            "ego_moving_near_trigger",
            {"min_max_ego_speed_mps": 1.0, "trigger_frame": trigger_frame},
            {"max_ego_speed_mps": max(ego_trigger_speeds) if ego_trigger_speeds else None},
            bool(ego_trigger_speeds and max(ego_trigger_speeds) >= 1.0),
            "vulnerable_actor_intrusion trigger occurs while ego is stopped",
        )
        actor_points = point_values(after, "vulnerable_actor_position") or point_values(after, "primary_actor_position")
        append_check(
            checks,
            "vulnerable_actor_positions_present",
            "at least 2 vulnerable actor positions after trigger",
            len(actor_points),
            len(actor_points) >= 2,
            "vulnerable actor positions missing after trigger",
        )
        motion = max((point_distance(actor_points[0], point) or 0.0 for point in actor_points[1:]), default=0.0)
        append_check(
            checks,
            "vulnerable_actor_moves_after_trigger",
            {"min_motion_m": 1.0},
            {"motion_m": motion},
            motion >= 1.0,
            "vulnerable actor did not move enough after trigger",
        )
        distances = numeric_values(after, "distance_to_ego_m")
        append_check(
            checks,
            "distance_to_ego_present",
            "distance_to_ego_m exists after trigger",
            len(distances),
            bool(distances),
            "vulnerable actor distance_to_ego_m missing",
        )
        append_check(
            checks,
            "vulnerable_actor_close_enough",
            {"max_min_distance_to_ego_m": 8.0},
            {"min_distance_to_ego_m": min(distances) if distances else None},
            bool(distances and min(distances) <= 8.0),
            "vulnerable actor never got close enough to ego",
        )
        append_check(
            checks,
            "vulnerable_actor_approaches_ego",
            {"min_approach_delta_m": 0.5},
            {"approach_delta_m": distances[0] - min(distances) if distances else None},
            bool(distances and distances[0] - min(distances) >= 0.5),
            "vulnerable actor did not approach ego after trigger",
        )
        laterals = numeric_values(after, "relative_lateral_m")
        append_check(
            checks,
            "relative_lateral_present_after_trigger",
            "relative_lateral_m exists after trigger",
            len(laterals),
            bool(laterals),
            "vulnerable actor relative_lateral_m missing",
        )
        initial_lat = as_float((primary.get("relative_to_ego") or {}).get("lateral_m"), as_float(primary.get("relative_lateral_m")))
        if initial_lat is not None:
            append_check(
                checks,
                "vulnerable_actor_moves_toward_ego_lane",
                {"max_abs_lateral_m": max(2.2, abs(initial_lat) - 1.0), "initial_lateral_m": initial_lat},
                {"min_abs_lateral_m": min(abs(value) for value in laterals) if laterals else None},
                bool(laterals and min(abs(value) for value in laterals) <= max(2.2, abs(initial_lat) - 1.0)),
                "vulnerable actor did not move laterally toward ego lane",
            )

    if frames and after and scenario_type == "front_vehicle_brake":
        primary_type = str(primary.get("type_id", ""))
        action = config.get("action_primitive") if isinstance(config.get("action_primitive"), dict) else {}
        reverse_speed = as_float(action.get("reverse_speed_mps"))
        criteria = config.get("success_criteria") if isinstance(config.get("success_criteria"), dict) else {}
        min_distance_delta = as_float(criteria.get("front_distance_change_m_min"), 1.5)
        append_check(
            checks,
            "front_brake_primary_is_vehicle",
            "primary actor type/kind is vehicle",
            {"type_id": primary.get("type_id"), "kind": primary.get("kind")},
            primary_type.startswith("vehicle") or primary.get("kind") == "vehicle",
            "front_vehicle_brake primary actor is not a vehicle",
        )
        first = next((frame for frame in frames if frame.get("primary_actor_found")), None)
        first_long = as_float(first.get("relative_longitudinal_m")) if first else None
        append_check(
            checks,
            "front_vehicle_initially_ahead",
            "front vehicle initial relative_longitudinal_m > 0",
            first_long,
            bool(first and first_long is not None and first_long > 0.0),
            "front vehicle is not initially ahead of ego",
        )
        speeds_after = numeric_values(after, "front_actor_speed_mps")
        append_check(
            checks,
            "front_vehicle_speed_present_after_trigger",
            "at least 2 front_actor_speed_mps values after trigger",
            len(speeds_after),
            len(speeds_after) >= 2,
            "front vehicle speed missing after trigger",
        )
        front_distances = numeric_values(after, "front_distance_m")
        if reverse_speed is not None:
            distance_delta = front_distances[0] - min(front_distances) if front_distances else None
            append_check(
                checks,
                "front_vehicle_reverses_toward_ego",
                {"min_distance_decrease_m": min_distance_delta, "reverse_speed_mps": reverse_speed},
                {"distance_decrease_m": distance_delta, "max_speed_mps": max(speeds_after) if speeds_after else None},
                bool(front_distances and distance_delta is not None and distance_delta >= min_distance_delta),
                "front vehicle did not reverse toward ego enough after trigger",
            )
        else:
            append_check(
                checks,
                "front_vehicle_speed_drop",
                {"min_speed_drop_mps": 1.0},
                {"speed_range_mps": max(speeds_after) - min(speeds_after) if speeds_after else None},
                bool(speeds_after and max(speeds_after) - min(speeds_after) >= 1.0),
                "front vehicle did not show enough speed drop",
            )

    if frames and after and scenario_type == "side_vehicle_intrusion":
        laterals = numeric_values(after, "relative_lateral_m")
        success_criteria = config.get("success_criteria") if isinstance(config.get("success_criteria"), dict) else {}
        action = config.get("action_primitive") if isinstance(config.get("action_primitive"), dict) else {}
        initial_lat = as_float((primary.get("relative_to_ego") or {}).get("lateral_m"), as_float(primary.get("relative_lateral_m")))
        target_lat = as_float(action.get("target_relative_lateral_m"))
        min_delta = as_float(success_criteria.get("relative_lateral_delta_m_min"), 1.5)
        max_abs_lateral = as_float(success_criteria.get("min_abs_relative_lateral_m_max"), 0.8)
        append_check(
            checks,
            "side_vehicle_lateral_present",
            "relative_lateral_m exists after trigger",
            len(laterals),
            bool(laterals),
            "side vehicle relative_lateral_m missing",
        )
        append_check(
            checks,
            "side_vehicle_enters_ego_lane",
            {"max_abs_lateral_m": max_abs_lateral},
            {"min_abs_lateral_m": min(abs(value) for value in laterals) if laterals else None},
            bool(laterals and min(abs(value) for value in laterals) <= max_abs_lateral),
            "side vehicle did not enter ego lane laterally",
        )
        if initial_lat is not None:
            max_delta = max((abs(initial_lat - value) for value in laterals), default=0.0)
            append_check(
                checks,
                "side_vehicle_lateral_delta",
                {"min_lateral_delta_m": min_delta, "initial_lateral_m": initial_lat},
                {"max_lateral_delta_m": max_delta},
                max_delta >= min_delta,
                "side vehicle lateral movement was too small",
            )
        if target_lat is not None:
            min_target_error = min((abs(value - target_lat) for value in laterals), default=None)
            append_check(
                checks,
                "side_vehicle_reaches_target_lateral",
                {"target_relative_lateral_m": target_lat, "tolerance_m": 0.4},
                {"min_target_error_m": min_target_error},
                bool(min_target_error is not None and min_target_error <= 0.4),
                "side vehicle did not reach the target relative lateral position",
            )

    report = {
        "passed": all(check.get("passed") for check in checks),
        "scenario_type": scenario_type,
        "check_count": len(checks),
        "failed_count": sum(1 for check in checks if not check.get("passed")),
        "checks": checks,
    }
    trace["semantic_validation"] = report
    trace["semantic_validation_passed"] = report["passed"]
    trace["semantic_validation_failed_reasons"] = fail_reasons(checks)
    if not report["passed"]:
        raise RuntimeError("semantic validation failed: " + "; ".join(trace["semantic_validation_failed_reasons"]))
    return report


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
        "front" if getattr(args, "no_save_images", False) else "surround",
        "--output-dir",
        images_dir,
        "--clean-output",
        "--no-try-next-on-failure",
    ]
    if getattr(args, "no_save_images", False):
        command.append("--no-save-images")
    run_command(command, capture_output=True)


def scenic_actor_class(actor):
    kind = str((actor or {}).get("kind") or "").lower()
    type_id = str((actor or {}).get("type_id") or "").lower()
    if kind in {"pedestrian", "walker", "cyclist"} or type_id.startswith("walker."):
        return "Pedestrian"
    return "Car"


def scenic_actor_blueprint(actor, default_model):
    type_id = (actor or {}).get("type_id")
    if type_id:
        return str(type_id)
    return default_model


def require_spawn_pose(actor, label):
    position = (actor or {}).get("scenic_position_expression")
    heading = (actor or {}).get("scenic_heading") or "0 deg"
    if not position:
        raise RuntimeError(f"spawn check cannot run: {label} missing scenic_position_expression")
    return position, heading


def write_spawn_check_scenic(config, primitives, path):
    context = next(
        (
            item
            for item in primitives.get("semantic_primitives", [])
            if isinstance(item, dict) and item.get("primitive") == "set_scene_context"
        ),
        {},
    )
    ego = next(
        (
            item.get("actor")
            for item in primitives.get("semantic_primitives", [])
            if isinstance(item, dict) and item.get("primitive") == "spawn_ego"
        ),
        {},
    )
    primary = primitives.get("primary_actor") or {}
    ego_pos, ego_heading = require_spawn_pose(ego, "ego")
    town = context.get("town") or "Town05"
    map_path = context.get("map_absolute_path") or scenic_map_absolute_path(town)
    primary_is_environment = str(primary.get("kind") or "").lower() == "environment"
    if not primary_is_environment:
        primary_pos, primary_heading = require_spawn_pose(primary, "primary_actor")
        primary_class = scenic_actor_class(primary)
        primary_blueprint = scenic_actor_blueprint(primary, "vehicle.nissan.micra")
    ego_blueprint = scenic_actor_blueprint(ego, "vehicle.lincoln.mkz_2017")
    lines = [
        "'''Spawn-only validation generated before OpenCode codegen.'''",
        f"Town = {json.dumps(town)}",
        f"param map = localPath({json.dumps(map_path)})",
        "param carla_map = Town",
        "model scenic.simulators.carla.model",
        f"EGO_MODEL = {json.dumps(ego_blueprint)}",
        "",
        f"ego = Car at {ego_pos},",
        f"    with heading {ego_heading},",
        "    with regionContainedIn None,",
        "    with blueprint EGO_MODEL",
        "",
    ]
    if primary_is_environment:
        lines.extend(
            [
                "# No primary_actor spawn is needed for weather_visibility_change.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                f"primary_actor = {primary_class} at {primary_pos},",
                f"    with heading {primary_heading},",
                "    with regionContainedIn None,",
                f"    with blueprint {json.dumps(primary_blueprint)}",
                "",
            ]
        )
    write_text(path, "\n".join(lines))


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")


def spawn_check_args(args):
    copied = argparse.Namespace(**vars(args))
    copied.frames = int(getattr(args, "spawn_check_frames", 1) or 1)
    copied.save_every = int(getattr(args, "spawn_check_save_every", 1) or 1)
    copied.warmup_ticks = min(int(getattr(args, "warmup_ticks", 0) or 0), 1)
    copied.no_save_images = True
    return copied


def read_spawn_states(images_dir, limit=5):
    states = []
    for path in sorted(glob.glob(os.path.join(images_dir, "state_*.json")))[:limit]:
        state = read_json(path)
        state["_file"] = os.path.basename(path)
        states.append(state)
    return states


def run_spawn_semantic_check(args, config, primitives, images_dir, report_path):
    states = read_spawn_states(images_dir)
    payload = {
        "instruction": "判断出生地点语义是否合理。只检查出生相对关系、主对象类型、是否符合L4动作原语，不检查后续动态行为。",
        "scenario_type": config.get("scenario_type"),
        "chain_description": config.get("chain_description"),
        "direct_physical_outcome": config.get("direct_physical_outcome"),
        "planned_primary_actor": primitives.get("primary_actor"),
        "planned_action_primitive": config.get("action_primitive"),
        "captured_spawn_states": states,
        "required_output": {
            "passed": True,
            "reason": "简短原因",
            "checks": [{"name": "检查项", "passed": True, "target": "目标", "actual": "实际"}],
        },
    }
    prompt = (
        "你是 L4 spawn 语义检查 agent。请只输出 JSON。\n"
        "如果主对象没有按动作原语要求出现在合理相对位置，passed=false。\n\n输入 JSON：\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    api_key = get_api_key(args.api_key_env, args.env_file, getattr(args, "api_key", None))
    raw_response = chat_json(args.plan_url, args.plan_model, api_key, prompt, args.plan_timeout)
    parsed = parse_json_response(raw_response)
    report = {
        "kind": "spawn_semantic_check",
        "model": args.plan_model,
        "raw_response": raw_response,
        "result": parsed,
    }
    write_json(report_path, report)
    if not parsed.get("passed"):
        raise RuntimeError(f"spawn semantic check failed: {parsed.get('reason', parsed)}")
    return report


def run_spawn_check(args, config, primitives, output_dir):
    if not getattr(args, "spawn_check", True):
        return None
    check_dir = os.path.join(output_dir, "spawn_check")
    scenic_path = os.path.join(check_dir, "spawn_check.scenic")
    state_dir = os.path.join(check_dir, "state")
    report_path = os.path.join(check_dir, "spawn_check_report.json")
    parameter_report_path = os.path.join(check_dir, "spawn_parameter_check_report.json")
    os.makedirs(check_dir, exist_ok=True)
    parameter_check = config.get("spawn_parameter_check") or {
        "kind": "plan_spawn_parameter_check",
        "passed": True,
        "reason": "no spawn parameter check was attached",
    }
    write_json(parameter_report_path, parameter_check)
    if not parameter_check.get("passed"):
        report = {
            "kind": "spawn_check",
            "passed": False,
            "parameter_report": os.path.abspath(parameter_report_path),
            "carla_spawn_attempted": False,
            "note": "PlanAgent action-primitive spawn parameters failed before CARLA spawn validation.",
        }
        write_json(report_path, report)
        raise RuntimeError(f"spawn parameter check failed: {parameter_check.get('reason', parameter_check)}")

    write_spawn_check_scenic(config, primitives, scenic_path)
    run_scenic_capture(spawn_check_args(args), scenic_path, state_dir)
    report = {
        "kind": "spawn_check",
        "passed": True,
        "parameter_report": os.path.abspath(parameter_report_path),
        "carla_spawn_attempted": True,
        "spawn_scenic": os.path.abspath(scenic_path),
        "state_dir": os.path.abspath(state_dir),
        "note": "CARLA accepted the spawn-only Scenic file. No images are saved; this gate only checks spawn legality before OpenCode.",
    }
    write_json(report_path, report)
    return report


def spawn_scenic_from_report(spawn_report):
    if not isinstance(spawn_report, dict):
        return None
    path = spawn_report.get("spawn_scenic")
    return path if path and os.path.exists(path) else None


def run_scenic_with_repair(args, task_path, primitives_path, scenic_file, images_dir):
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
            repair_opencode(args, task_path, primitives_path, scenic_file, build_execution_repair_feedback(last_error))


def validation_summary(trace):
    if not isinstance(trace, dict):
        return "没有生成 event_trace，无法评估完成质量。"
    validation = trace.get("semantic_validation") or {}
    if not validation:
        return "没有启用语义验收，只有 Scenic/CARLA 执行结果。"
    check_count = validation.get("check_count", 0)
    failed_count = validation.get("failed_count", 0)
    status = "通过" if validation.get("passed") else "未通过"
    return f"语义验收{status}：共 {check_count} 项检查，失败 {failed_count} 项。"


def validation_errors(trace):
    if not isinstance(trace, dict):
        return ["没有生成 event_trace。"]
    reasons = trace.get("semantic_validation_failed_reasons")
    if reasons:
        return [str(reason) for reason in reasons]
    validation = trace.get("semantic_validation") or {}
    checks = validation.get("checks") or []
    return [str(check.get("reason") or check.get("name")) for check in checks if not check.get("passed")] or []


def feedback_issue_lines(feedback):
    if isinstance(feedback, list):
        lines = []
        for item in feedback:
            lines.extend(feedback_issue_lines(item))
        return lines
    if not isinstance(feedback, dict):
        return [str(feedback)] if feedback else []
    lines = []
    if feedback.get("issue"):
        lines.append(str(feedback["issue"]))
    if feedback.get("evidence"):
        lines.append(f"关键证据：{feedback['evidence']}")
    if feedback.get("failed_point"):
        lines.append(f"失败位置：{feedback['failed_point']}")
    for check in feedback.get("failed_checks") or []:
        reason = check.get("reason") or check.get("name")
        if reason:
            lines.append(str(reason))
    return lines


def format_feedback(feedback_text):
    def format_one(item):
        if not isinstance(item, dict):
            return str(item).strip()
        lines = []
        if item.get("kind"):
            lines.append(f"- 类型：{item['kind']}")
        if item.get("issue"):
            lines.append(f"- 问题：{item['issue']}")
        if item.get("evidence"):
            lines.append(f"- 关键证据：{item['evidence']}")
        if item.get("failed_point"):
            lines.append(f"- 失败位置：{item['failed_point']}")
        failed_checks = item.get("failed_checks") or []
        if failed_checks:
            lines.append("- 失败检查：")
            for check in failed_checks:
                lines.append(
                    "  - "
                    + json.dumps(
                        {
                            "name": check.get("name"),
                            "target": check.get("target"),
                            "actual": check.get("actual"),
                            "reason": check.get("reason"),
                            "repair_requirement": check.get("repair_requirement"),
                        },
                        ensure_ascii=False,
                    )
                )
        requirements = item.get("repair_requirements") or []
        if requirements:
            lines.append("- 修改要求：")
            lines.extend(f"  - {requirement}" for requirement in requirements)
        if item.get("raw_error_file"):
            lines.append(f"- 原始日志：{item['raw_error_file']}")
        return "\n".join(lines) if lines else json.dumps(item, ensure_ascii=False, indent=2)

    if isinstance(feedback_text, list):
        if not feedback_text:
            return "没有触发反馈修复。"
        chunks = []
        for index, item in enumerate(feedback_text, start=1):
            chunks.append(f"### 第 {index} 轮反馈\n{format_one(item)}")
        return "\n\n".join(chunks)
    return format_one(feedback_text) if feedback_text else "没有触发反馈修复。"


def has_feedback(feedback_text):
    return bool(feedback_text) if not isinstance(feedback_text, list) else bool(feedback_text)


def write_l4_feedback_report(path, config, before_trace=None, feedback_text="", final_trace=None, final_error=""):
    effective_before_trace = before_trace or final_trace
    errors = validation_errors(before_trace) if before_trace else feedback_issue_lines(final_error)
    event_desc = config.get("chain_description") or config.get("expected_visual_result") or "未提供事件描述"
    lines = [
        "# L4执行反馈报告",
        "",
        "## 1. 本次事件描述",
        f"- 场景类型：{config.get('scenario_type')}",
        f"- 触发帧：{config.get('trigger_frame')}",
        f"- 事件：{event_desc}",
        "",
        "## 2. 反馈之前的完成质量",
        validation_summary(effective_before_trace) if effective_before_trace else "首次执行没有形成可用验收结果。",
        "",
        "## 3. 错误具体出在哪（如果有错误）",
    ]
    if errors:
        lines.extend(f"- {reason}" for reason in errors)
    elif final_error:
        lines.extend(f"- {line}" for line in feedback_issue_lines(final_error))
    else:
        lines.append("- 没有发现语义验收错误。")
    lines.extend(
        [
            "",
            "## 4. 反馈内容",
            format_feedback(feedback_text),
            "",
            "## 5. 反馈后执行效果",
            (
                validation_summary(final_trace)
                if has_feedback(feedback_text) and final_trace
                else "没有触发反馈修复，首次执行结果就是最终效果。"
                if final_trace and not has_feedback(feedback_text)
                else "执行失败，未得到最终 event_trace。"
                if final_error
                else "尚未重新执行。"
            ),
        ]
    )
    if final_trace and validation_errors(final_trace):
        lines.append("")
        lines.append("反馈后仍未通过的原因：")
        lines.extend(f"- {reason}" for reason in validation_errors(final_trace))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def run_scenic_image_success(args, scenic_file, images_dir, config, primitives):
    report_path = os.path.join(os.path.dirname(images_dir), "l4_feedback_report.md")
    run_scenic_capture(args, scenic_file, images_dir)
    postprocess_images(images_dir)
    trace = write_event_trace_from_states(images_dir, config, primitives)
    trace["semantic_validation_skipped"] = True
    trace["success_policy"] = "opencode_success_requires_carla_images_only"
    write_json(os.path.join(images_dir, "event_trace.json"), trace)
    write_l4_feedback_report(report_path, config, None, [], trace)
    return trace


def run_scenic_validate_with_repair(args, task_path, primitives_path, scenic_file, images_dir, config, primitives):
    report_path = os.path.join(os.path.dirname(task_path), "..", "l4_feedback_report.md")
    report_dir = os.path.dirname(os.path.abspath(report_path))
    first_trace = None
    feedback_entries = []
    for attempt in range(args.opencode_repair_attempts + 1):
        trace = None
        try:
            run_scenic_capture(args, scenic_file, images_dir)
            postprocess_images(images_dir)
            trace = write_event_trace_from_states(images_dir, config, primitives)
            if args.validate_event_trace:
                try:
                    validate_scenario_language_event_trace(config, primitives, trace)
                finally:
                    write_json(os.path.join(images_dir, "event_trace.json"), trace)
            write_l4_feedback_report(report_path, config, first_trace, feedback_entries, trace)
            return trace
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            last_error = error_text(exc)
            if isinstance(trace, dict) and trace.get("semantic_validation"):
                if first_trace is None:
                    first_trace = trace
            raw_error_path = os.path.join(report_dir, f"repair_raw_error_attempt_{attempt + 1}.txt")
            with open(raw_error_path, "w", encoding="utf-8") as f:
                f.write(last_error)
                if isinstance(trace, dict) and trace.get("semantic_validation"):
                    f.write(
                        "\n\nSemantic validation report with target/actual/pass fields:\n"
                        + json.dumps(trace["semantic_validation"], ensure_ascii=False, indent=2)
                        + "\n\nExpected primary actor:\n"
                        + json.dumps(trace.get("expected_primary_actor", {}), ensure_ascii=False, indent=2)
                    )
            repair_feedback = build_repair_feedback(last_error, trace)
            repair_feedback["raw_error_file"] = raw_error_path
            feedback_entries.append(repair_feedback)
            if attempt >= args.opencode_repair_attempts:
                write_l4_feedback_report(report_path, config, first_trace, feedback_entries, trace, final_error=repair_feedback)
                raise
            print(f"\nAsking opencode to repair Scenic file ({attempt + 1}/{args.opencode_repair_attempts}).")
            repair_opencode(args, task_path, primitives_path, scenic_file, repair_feedback)


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
    plan_agent_raw = config.pop("_l4_plan_agent_raw", None)
    if plan_agent_raw is not None:
        write_json(os.path.join(output_dir, "l4_plan_agent_raw.json"), plan_agent_raw)
    config["execution_backend"] = "opencode_scenic"
    config["executor"] = "carla_smoke/scenes/safebench_scenic_scene.py"
    task_source_path = os.path.join(output_dir, "l4_task_source.json")
    write_json(task_source_path, config)

    primitives = build_semantic_primitives(config, l0_state)
    primitives_path = os.path.join(output_dir, "semantic_primitives.json")
    write_json(primitives_path, primitives)

    spawn_report = None
    if args.execute:
        spawn_report = run_spawn_check(args, config, primitives, output_dir)

    workspace, workspace_task, workspace_primitives, output_scenic = prepare_workspace(
        args,
        task_source_path,
        primitives_path,
        spawn_scenic_path=spawn_scenic_from_report(spawn_report),
    )
    run_opencode(args, workspace_task, workspace_primitives, output_scenic)

    images_dir = os.path.join(output_dir, "risk_images")
    if args.execute:
        if getattr(args, "validate_event_trace", False):
            run_scenic_validate_with_repair(args, workspace_task, workspace_primitives, output_scenic, images_dir, config, primitives)
        else:
            run_scenic_image_success(args, output_scenic, images_dir, config, primitives)
    else:
        print("Scenario-language execution skipped. Add --execute to run Scenic/CARLA.")

    return {
        "chain_id": chain.get("id"),
        "source_l2_id": chain.get("parent_l2_id"),
        "output_dir": os.path.abspath(output_dir),
        "l4_task_source": os.path.abspath(task_source_path),
        "semantic_primitives": os.path.abspath(primitives_path),
        "generated_scenic": os.path.abspath(output_scenic),
        "risk_images": os.path.abspath(images_dir),
        "scenario_type": config.get("scenario_type"),
        "spawn_check": spawn_report,
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
    parser.add_argument("--port", type=int, default=2001)
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
    parser.add_argument("--spawn-check", dest="spawn_check", action="store_true", default=True)
    parser.add_argument("--no-spawn-check", dest="spawn_check", action="store_false")
    parser.add_argument("--spawn-semantic-check", dest="spawn_semantic_check", action="store_true", default=True)
    parser.add_argument("--no-spawn-semantic-check", dest="spawn_semantic_check", action="store_false")
    parser.add_argument("--spawn-check-frames", type=int, default=1)
    parser.add_argument("--spawn-check-save-every", type=int, default=1)
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--opencode-repair-attempts", type=int, default=3)
    parser.add_argument("--plan-model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--plan-url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--api-key", default=None, help="Explicit API key. Prefer .env/API_KEY_ENV for shared runs.")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--plan-timeout", type=float, default=300.0)
    parser.add_argument("--plan-feedback-attempts", type=int, default=1)
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
