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
    side = None
    if rel_lat is not None:
        if rel_lat < -0.05:
            side = "left"
        elif rel_lat > 0.05:
            side = "right"
        else:
            side = "center"
    relative_position = actor.get("relative_position") or actor.get("initial_relative_position")
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
    base = max(0.5, abs(value))
    candidates = [base, base - 0.5, base + 0.5, base - 1.0, base + 1.0, base + 1.5]
    cleaned = []
    for candidate in candidates:
        rounded = round(max(0.5, candidate), 3)
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
        "relative_position": actor.get("relative_position") or actor.get("initial_relative_position"),
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
    scenic_context = scenic_context_from_scene(scene)

    primitives = [
        primitive(
            "set_scene_context",
            town=scenic_context["town"],
            map_absolute_path=scenic_context["map_absolute_path"],
            scenic_header=scenic_context["scenic_header"],
            coordinate_contract=scenic_context["coordinate_contract"],
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
    references_dir = os.path.join(opencode_skills_dir(), "l4-scenario-language-codegen", "references")
    context_dir = os.path.join(workspace, "context")
    if os.path.isdir(context_dir):
        shutil.rmtree(context_dir)
    if os.path.isdir(references_dir):
        copy_tree_contents(references_dir, context_dir)
    with open(os.path.join(workspace, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write(
            "# OpenCode Workspace Instructions\n\n"
            "MANDATORY SKILL: l4-scenario-language-codegen.\n"
            "Forbidden skills: l4-carla-codegen, l4-safebench-intervention-codegen.\n"
            "Generate Scenic scenario-language code only; do not generate CARLA Python code.\n"
            "Edit only generated_risk_scene.scenic.\n"
            "The file must use the absolute map path from semantic_primitives.json.\n"
            "Use precomputed Scenic 2D coordinates from semantic_primitives.json; never copy raw CARLA y/yaw directly.\n"
            "When absolute placement fails, preserve actor.relative_to_ego.side and use same_side_search_policy.\n"
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
    return f"""MANDATORY SKILL: l4-scenario-language-codegen.
Forbidden skills: l4-carla-codegen, l4-safebench-intervention-codegen.

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
- Use semantic_primitives.set_scene_context.map_absolute_path exactly in `param map = localPath(...)`.
- Use the precomputed `actor.scenic_position_expression` and `actor.scenic_heading`; never convert raw CARLA x/y/yaw yourself.
- Prefer `actor.relative_to_ego` for L0 actors. If exact placement fails, use `same_side_search_policy` and keep the original left/right side.
- Preserve scenario_config.carla_plan.scenario_type exactly.
- Preserve primary actor kind/type, ego-relative geometry, action, trigger timing, and forbidden substitutions.
- L0 absolute coordinates are hints; relative geometry is authoritative.
- The generated Scenic must run through carla_smoke/scenes/safebench_scenic_scene.py.
- Do not write Markdown. Do not ask questions. Edit only generated_risk_scene.scenic.
"""


def opencode_repair_prompt(config_path, primitives_path, output_scenic, error_output):
    return f"""The generated Scenic scenario failed during Scenic/CARLA execution.

MANDATORY SKILL: l4-scenario-language-codegen.
Forbidden skills: l4-carla-codegen, l4-safebench-intervention-codegen.

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
- Keep `param map = localPath(...)` on the absolute map path from semantic_primitives.json.
- Replace any `Point(x, y, z)` / CARLA Python coordinate syntax with Scenic 2D `x @ y` syntax.
- Do not flip actor.relative_to_ego.side. If a left-side actor does not fit, search only left-side distances; if a right-side actor does not fit, search only right-side distances.
- If this is a semantic validation failure, use the Semantic validation report below as the repair target. Each failed check includes target, actual, and reason; edit the Scenic scenario so those checks pass.
- Do not satisfy semantic validation by changing scenario_config.json, semantic_primitives.json, event_trace.json, actor type, or scenario_type. Fix only generated_risk_scene.scenic.
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
            "actor_count": len(state.get("actors") or []),
            "image_file": safe_get(state, "source", "image_file")
            or os.path.basename(path).replace("state_", "rgb_").replace(".json", ".png"),
        }
        frame.update(frame_primary_fields(state, primary))
        frames.append(frame)
    scenario_type = safe_get(config, "carla_plan", "scenario_type")
    trace = {
        "scenario_type": scenario_type,
        "trigger_frame": safe_get(config, "physical_task", "action", "trigger_frame", default=config.get("trigger_frame", 20)),
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
            {"max_abs_lateral_m": 2.5},
            {"min_abs_lateral_m": min(abs(value) for value in laterals) if laterals else None},
            bool(laterals and min(abs(value) for value in laterals) <= 2.5),
            "side vehicle did not enter ego lane laterally",
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


def run_scenic_validate_with_repair(args, config_path, primitives_path, scenic_file, images_dir, config, primitives):
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
            return trace
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            last_error = error_text(exc)
            if isinstance(trace, dict) and trace.get("semantic_validation"):
                last_error += (
                    "\n\nSemantic validation report with target/actual/pass fields:\n"
                    + json.dumps(trace["semantic_validation"], ensure_ascii=False, indent=2)
                    + "\n\nExpected primary actor:\n"
                    + json.dumps(trace.get("expected_primary_actor", {}), ensure_ascii=False, indent=2)
                )
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
        run_scenic_validate_with_repair(args, workspace_config, workspace_primitives, output_scenic, images_dir, config, primitives)
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
    parser.add_argument("--opencode-model", default=DEFAULT_DEEPSEEK_MODEL)
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
