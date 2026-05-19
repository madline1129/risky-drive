#!/usr/bin/env python3
"""Code-agent stage for L4: turn an L3 CARLA plan into executable risk-scene images."""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import textwrap
import struct

try:
    from deepseek_client import (
        DEFAULT_DEEPSEEK_MODEL,
        DEFAULT_DEEPSEEK_URL,
        DeepSeekError,
        chat_json,
        get_api_key,
        parse_json_response,
    )
except ImportError:
    from .deepseek_client import (
        DEFAULT_DEEPSEEK_MODEL,
        DEFAULT_DEEPSEEK_URL,
        DeepSeekError,
        chat_json,
        get_api_key,
        parse_json_response,
    )

REPAIR_ATTEMPTS = 3


PLAN_AGENT_PROMPT_TEMPLATE = """你是 L4 PlanAgent：把自然语言事故链翻译成可执行 CARLA 物理计划。

你不是事故链生成器，不要编新故事；你只负责把输入的 L1/L2/L3 语义、L0 场景事实、物体清单翻译成严格的执行计划和物体约束。

硬性原则：
- 不要改变 L3 的核心事故链。
- 如果输入里已有 primary_perturbation_object / perturbation_target / object_registry，必须优先继承。
- scenario_type 必须是以下之一：front_vehicle_brake, cargo_drop, vulnerable_actor_intrusion, road_obstacle_intrusion, side_vehicle_intrusion。
- front_vehicle_brake 的 primary_object 必须是 vehicle，优先 same_lane_as_ego=true 且 relative_longitudinal_m>0 的 L0 actor；禁止 pedestrian/walker 作为 primary。
- vulnerable_actor_intrusion 的 primary_object 必须是 pedestrian/walker/cyclist 或 generated vulnerable actor。
- cargo_drop 的 primary_object 必须是 generated payload；carrier 可以是前方 vehicle，但 primary 不是前车本身。
- side_vehicle_intrusion 的 primary_object 必须是 L0 侧方 vehicle。
- road_obstacle_intrusion 的 primary_object 必须是 obstacle/generated object，不能退化成前车刹车。
- chain_participants 可以列多个物体，但只有 primary_object 能驱动主风险事件；背景物体不得抢主事件。

只输出 JSON 对象，不要 Markdown。格式：
{
  "level": "L4Plan",
  "scenario_type": "front_vehicle_brake",
  "translation_reason": "为什么这样翻译",
  "object_registry": {
    "primary_object": {
      "source": "l0_actor/generated_object/l0_ego",
      "actor_id": 123,
      "kind": "vehicle/pedestrian/payload/obstacle/ego",
      "role": "front_vehicle",
      "must_drive_primary_event": true,
      "selection_reason": "引用 L0 字段说明为什么选它"
    },
    "participants": [
      {
        "source": "l0_actor/l0_ego/generated_object",
        "actor_id": "ego",
        "kind": "ego",
        "role": "affected_actor",
        "must_drive_primary_event": false
      }
    ]
  },
  "carla_plan": {
    "scenario_type": "front_vehicle_brake",
    "primary_actor_id": 123,
    "primary_actor_source": "l0_actor",
    "target_actor": "front_vehicle",
    "trigger_frame": 45,
    "expected_visual_result": "简短说明",
    "actor_motion_plan": {
      "ego": {"role": "observer/follower", "behavior": "具体行为"},
      "primary_actor": {"role": "front_actor/payload/walker/side_vehicle/obstacle", "behavior": "具体行为", "trigger_frame": 45},
      "background_actors": {"behavior": "preserve_or_ignore", "must_not_drive_primary_event": true}
    }
  },
  "success_criteria": {
    "一句话或字段": "关键验收约束"
  }
}
"""


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


def frame_from_state(state):
    try:
        frame = (state.get("source") or {}).get("frame")
        return int(frame) if frame is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def actor_min_distance(state):
    if not isinstance(state, dict):
        return float("inf")
    summary = state.get("summary", {}) if isinstance(state.get("summary"), dict) else {}
    candidates = [summary.get("nearest_actor_distance_m"), summary.get("nearest_front_distance_m")]
    nearest_front = state.get("nearest_front_actor")
    if isinstance(nearest_front, dict):
        candidates.extend(
            [
                nearest_front.get("distance_m"),
                nearest_front.get("relative_longitudinal_m"),
            ]
        )
    actors = state.get("actors", []) if isinstance(state.get("actors"), list) else []
    for actor in actors:
        if isinstance(actor, dict):
            candidates.append(actor.get("distance_m"))
    numeric = []
    for value in candidates:
        try:
            if value is not None:
                numeric.append(abs(float(value)))
        except (TypeError, ValueError):
            pass
    return min(numeric) if numeric else float("inf")


def timeline_states_from_l0(l0_state):
    if not isinstance(l0_state, dict):
        return []
    for key in ("l4_timeline_states", "sampled_l0_states"):
        states = l0_state.get(key)
        if isinstance(states, list) and states:
            return [state for state in states if isinstance(state, dict)]
    return [l0_state]


def select_reconstruction_state(l0_state, pre_trigger_seconds=2.0, source_timestep=0.05):
    states = timeline_states_from_l0(l0_state)
    if not states:
        return l0_state, {
            "selection_reason": "no_l0_timeline_available",
            "risk_peak_frame": None,
            "reconstruction_frame": frame_from_state(l0_state) if isinstance(l0_state, dict) else None,
        }

    states = sorted(states, key=lambda state: frame_from_state(state) if frame_from_state(state) is not None else 10**12)
    peak_state = min(states, key=actor_min_distance)
    peak_frame = frame_from_state(peak_state)
    peak_distance = actor_min_distance(peak_state)

    selected = peak_state
    target_frame = None
    if peak_frame is not None:
        retreat_ticks = max(1, int(round(float(pre_trigger_seconds) / float(source_timestep or 0.05))))
        target_frame = peak_frame - retreat_ticks
        earlier = [state for state in states if frame_from_state(state) is not None and frame_from_state(state) <= target_frame]
        if earlier:
            selected = max(earlier, key=lambda state: frame_from_state(state))
        else:
            before_peak = [state for state in states if frame_from_state(state) is not None and frame_from_state(state) < peak_frame]
            if before_peak:
                selected = before_peak[0]

    return selected, {
        "selection_reason": "pre_event_frame_before_min_actor_distance",
        "risk_peak_frame": peak_frame,
        "risk_peak_distance_m": None if peak_distance == float("inf") else round(peak_distance, 3),
        "target_pre_event_frame": target_frame,
        "reconstruction_frame": frame_from_state(selected),
        "available_timeline_frames": [frame_from_state(state) for state in states if frame_from_state(state) is not None],
        "pre_trigger_seconds": pre_trigger_seconds,
        "source_timestep": source_timestep,
    }


def replace_nested_trigger_frames(value, trigger_frame, original_trigger_frame=None):
    if isinstance(value, dict):
        updated = {}
        for key, item in value.items():
            if key == "trigger_frame":
                updated[key] = trigger_frame
            else:
                updated[key] = replace_nested_trigger_frames(item, trigger_frame, original_trigger_frame)
        return updated
    if isinstance(value, list):
        return [replace_nested_trigger_frames(item, trigger_frame, original_trigger_frame) for item in value]
    if isinstance(value, str) and original_trigger_frame is not None:
        return value.replace(f"frame_{original_trigger_frame}", f"frame_{trigger_frame}")
    return value


def clamp_local_trigger_frame(requested_trigger_frame, l4_frames):
    frame_count = max(1, int(l4_frames or 1))
    trigger = max(1, int(requested_trigger_frame or 20))
    if trigger >= frame_count:
        trigger = max(1, frame_count // 4)
    return trigger


def ego_speed_mps_from_state(state):
    try:
        return float(((state or {}).get("ego") or {}).get("speed_mps") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def adapt_actor_motion_plan_to_reconstruction(plan, reconstruction_state):
    plan = dict(plan)
    motion_plan = plan.get("actor_motion_plan")
    if not isinstance(motion_plan, dict):
        return plan

    scenario_type = plan.get("scenario_type")
    if scenario_type not in {"vulnerable_actor_intrusion", "road_obstacle_intrusion", "cargo_drop", "front_vehicle_brake", "side_vehicle_intrusion"}:
        return plan

    ego_plan = motion_plan.get("ego")
    if not isinstance(ego_plan, dict):
        return plan

    source_speed = ego_speed_mps_from_state(reconstruction_state)
    target_speed = max(2.0, min(5.0, source_speed if source_speed > 0.5 else 3.0))
    behavior = str(ego_plan.get("behavior", "")).lower()
    try:
        planned_speed = float(ego_plan.get("target_speed_mps", target_speed) or 0.0)
    except (TypeError, ValueError):
        planned_speed = 0.0

    should_fix_stale_stop = "stay_stopped" in behavior or "stopped" in behavior or planned_speed <= 0.1
    if should_fix_stale_stop:
        ego_plan = dict(ego_plan)
        ego_plan.update(
            {
                "behavior": "slow_approach_until_trigger_then_react",
                "target_speed_mps": round(target_speed, 3),
                "avoid_collision": True,
                "must_remain_moving_until_trigger": True,
                "derived_from_reconstruction_frame_speed_mps": round(source_speed, 3),
            }
        )
        motion_plan = dict(motion_plan)
        motion_plan["ego"] = ego_plan
        plan["actor_motion_plan"] = motion_plan
    return plan


def actor_id_from_plan(plan):
    motion_plan = plan.get("actor_motion_plan") if isinstance(plan, dict) else {}
    primary = motion_plan.get("primary_actor") if isinstance(motion_plan, dict) else {}
    actor_id = primary.get("actor_id") if isinstance(primary, dict) else None
    try:
        return int(actor_id)
    except (TypeError, ValueError):
        return None


def actor_by_id(l0_state, actor_id):
    if actor_id is None or not isinstance(l0_state, dict):
        return None
    actors = l0_state.get("actors", [])
    if not isinstance(actors, list):
        return None
    for actor in actors:
        if not isinstance(actor, dict):
            continue
        try:
            if int(actor.get("id")) == int(actor_id):
                return actor
        except (TypeError, ValueError):
            continue
    return None


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def xyz_dict(value):
    if not isinstance(value, dict):
        return None
    if not all(key in value for key in ("x", "y", "z")):
        return None
    return {
        "x": round(as_float(value.get("x")), 3),
        "y": round(as_float(value.get("y")), 3),
        "z": round(as_float(value.get("z")), 3),
    }


def actor_location(actor):
    return xyz_dict((actor or {}).get("location"))


def actor_rotation(actor):
    rotation = (actor or {}).get("rotation")
    if not isinstance(rotation, dict):
        return None
    return {
        "pitch": round(as_float(rotation.get("pitch")), 3),
        "yaw": round(as_float(rotation.get("yaw")), 3),
        "roll": round(as_float(rotation.get("roll")), 3),
    }


def ego_anchor(scene_reconstruction):
    ego = (scene_reconstruction or {}).get("ego") or {}
    return actor_location(ego), actor_rotation(ego)


def local_offset_to_world(anchor_location, anchor_rotation, offset):
    anchor = xyz_dict(anchor_location)
    local = xyz_dict(offset)
    if not anchor or not local:
        return None
    yaw = math.radians(as_float((anchor_rotation or {}).get("yaw")))
    forward_x = math.cos(yaw)
    forward_y = math.sin(yaw)
    right_x = -math.sin(yaw)
    right_y = math.cos(yaw)
    return {
        "x": round(anchor["x"] + local["x"] * forward_x + local["y"] * right_x, 3),
        "y": round(anchor["y"] + local["x"] * forward_y + local["y"] * right_y, 3),
        "z": round(anchor["z"] + local["z"], 3),
    }


def lateral_side_from_plan(plan):
    spawn_hint = str(plan.get("spawn_relative_to", "")).lower()
    direction = str(plan.get("crossing_direction", "")).lower()
    if "left" in spawn_hint:
        return "left"
    if "right" in spawn_hint:
        return "right"
    if direction.startswith("left"):
        return "left"
    if direction.startswith("right"):
        return "right"
    start = plan.get("start_position") or plan.get("initial_position") or {}
    y_value = as_float((start or {}).get("y"), 0.0)
    return "left" if y_value < 0 else "right"


def signed_lateral_for_side(side, magnitude):
    value = abs(as_float(magnitude, 4.0))
    return -value if side == "left" else value


def build_generated_path_from_ego(plan, scene_reconstruction, start_key="start_position", default_x=18.0, default_y=4.0, end_x=None):
    ego_location, ego_rotation = ego_anchor(scene_reconstruction)
    start = dict(plan.get(start_key) or {"x": default_x, "y": default_y, "z": 0.2})
    side = lateral_side_from_plan(plan)
    start["y"] = signed_lateral_for_side(side, start.get("y", default_y))
    start.setdefault("x", default_x)
    start.setdefault("z", 0.2)

    end = dict(start)
    end["y"] = -signed_lateral_for_side(side, max(abs(as_float(start.get("y"), default_y)), 3.2))
    end["x"] = as_float(end_x, as_float(start.get("x"), default_x) - 2.0) if end_x is not None else as_float(start.get("x"), default_x) - 2.0

    return {
        "side": side,
        "start_local": xyz_dict(start),
        "end_local": xyz_dict(end),
        "start_world": local_offset_to_world(ego_location, ego_rotation, start),
        "end_world": local_offset_to_world(ego_location, ego_rotation, end),
    }


def nearest_front_actor(scene_reconstruction):
    actor = (scene_reconstruction or {}).get("nearest_front_actor")
    return actor if isinstance(actor, dict) else None


def is_vehicle_actor(actor):
    if not isinstance(actor, dict):
        return False
    kind = str(actor.get("kind", "")).lower()
    type_id = str(actor.get("type_id", "")).lower()
    return kind == "vehicle" or type_id.startswith("vehicle.")


def is_front_actor(actor):
    if not isinstance(actor, dict):
        return False
    try:
        rel_long = float(actor.get("relative_longitudinal_m"))
    except (TypeError, ValueError):
        return "front" in str(actor.get("relative_position", "")).lower()
    return rel_long > 0.0


def find_front_vehicle(scene_reconstruction):
    actors = (scene_reconstruction or {}).get("actors", [])
    if not isinstance(actors, list):
        actors = []
    candidates = [actor for actor in actors if is_vehicle_actor(actor) and is_front_actor(actor)]
    if not candidates:
        nearest = nearest_front_actor(scene_reconstruction)
        if is_vehicle_actor(nearest) and is_front_actor(nearest):
            return nearest
        return None

    def score(actor):
        same_lane = 0 if actor.get("same_lane_as_ego") else 1
        lateral = abs(as_float(actor.get("relative_lateral_m"), 99.0))
        longitudinal = abs(as_float(actor.get("relative_longitudinal_m"), actor.get("distance_m") or 99.0))
        distance = abs(as_float(actor.get("distance_m"), longitudinal))
        return (same_lane, lateral, longitudinal, distance)

    return min(candidates, key=score)


def chain_object_registry(chain):
    for key in ("object_registry", "chain_participants"):
        value = chain.get(key) if isinstance(chain, dict) else None
        if isinstance(value, (dict, list)):
            return value
    collected = {}
    for key in ("primary_perturbation_object", "perturbation_target", "risk_object", "primary_object"):
        value = chain.get(key) if isinstance(chain, dict) else None
        if isinstance(value, dict):
            collected[key] = value
    return collected or None


def build_l4_plan_agent_prompt(chain, l0_state, reconstruction_state):
    context = {
        "l3_chain": chain,
        "l0_state_snapshot": compact_l0_scene(l0_state) if isinstance(l0_state, dict) else None,
        "selected_reconstruction_state": compact_l0_scene(reconstruction_state) if isinstance(reconstruction_state, dict) else None,
        "inherited_object_registry": chain_object_registry(chain),
    }
    return PLAN_AGENT_PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(context, ensure_ascii=False, indent=2)


def run_l4_plan_agent(args, chain, l0_state, reconstruction_state):
    prompt = build_l4_plan_agent_prompt(chain, l0_state, reconstruction_state)
    api_key = get_api_key(args.api_key_env, args.env_file)
    raw_response = chat_json(args.plan_url, args.plan_model, api_key, prompt, args.plan_timeout)
    return parse_json_response(raw_response), raw_response


def plan_from_l4_plan_agent_output(output):
    if not isinstance(output, dict):
        return {}
    plan = output.get("carla_plan")
    if isinstance(plan, dict):
        return plan
    nested = output.get("l4_plan")
    if isinstance(nested, dict) and isinstance(nested.get("carla_plan"), dict):
        return nested["carla_plan"]
    return {}


def merge_plan_with_agent_output(base_plan, agent_output):
    agent_plan = plan_from_l4_plan_agent_output(agent_output)
    if not agent_plan:
        return base_plan
    merged = dict(base_plan or {})
    merged.update(agent_plan)
    if agent_output.get("scenario_type") and "scenario_type" not in agent_plan:
        merged["scenario_type"] = agent_output["scenario_type"]
    registry = agent_output.get("object_registry")
    if isinstance(registry, dict):
        primary = registry.get("primary_object")
        if isinstance(primary, dict):
            if primary.get("actor_id") is not None:
                merged.setdefault("primary_actor_id", primary.get("actor_id"))
            if primary.get("source") is not None:
                merged.setdefault("primary_actor_source", primary.get("source"))
            if primary.get("kind") is not None:
                merged.setdefault("primary_actor_kind", primary.get("kind"))
            if primary.get("role") is not None:
                merged.setdefault("primary_actor_role", primary.get("role"))
    return merged


def fallback_raw_plan_from_chain_text(chain):
    text = " ".join(
        str((chain or {}).get(key, ""))
        for key in ("chain_description", "direct_physical_outcome", "parent_l2_trigger", "parent_l1_name")
    )
    if any(keyword in text for keyword in ["急刹", "减速", "停滞", "刹车", "追尾", "跟车"]):
        return {
            "scenario_type": "front_vehicle_brake",
            "target_actor": "front_vehicle",
            "trigger_frame": 45,
            "brake_intensity": 1.0,
            "deceleration_mps2": 6.0,
            "target_speed_mps": 0.0,
            "expected_visual_result": "前车在自车前方突然减速或接近停止，自车前向距离快速压缩",
        }
    if any(keyword in text for keyword in ["行人", "骑行", "自行车", "弱势", "横穿", "闯入", "滑倒"]):
        return {
            "scenario_type": "vulnerable_actor_intrusion",
            "actor_type": "walker",
            "trigger_frame": 45,
            "spawn_relative_to": "ego_lane_right",
            "start_position": {"x": 18.0, "y": 4.0, "z": 0.2},
            "crossing_direction": "right_to_left",
            "speed_mps": 2.2,
        }
    if any(keyword in text for keyword in ["货物", "钢筋", "金属管", "掉落", "滑落", "绳索", "固定"]):
        return {
            "scenario_type": "cargo_drop",
            "target_actor": "front_vehicle",
            "object_type": "metal_pipe",
            "object_count": 5,
            "trigger_frame": 45,
            "spawn_relative_to": "front_vehicle",
            "initial_position": {"x": -3.2, "y": 0.0, "z": 2.4},
            "motion": {
                "mode": "scripted_projectile",
                "direction": "toward_ego",
                "back_speed_mps": 8.0,
                "lateral_drift_mps": 0.2,
                "gravity": True,
            },
        }
    if any(keyword in text for keyword in ["侧方", "变道", "横向", "侵入", "并线"]):
        return {
            "scenario_type": "side_vehicle_intrusion",
            "trigger_frame": 45,
            "motion": {"mode": "lateral_shift_toward_ego_lane", "duration_s": 1.0, "distance_m": 1.5},
        }
    return {
        "scenario_type": "road_obstacle_intrusion",
        "object_type": "road_obstacle",
        "trigger_frame": 45,
        "spawn_relative_to": "front_of_ego",
        "initial_position": {"x": 14.0, "y": 0.0, "z": 0.4},
    }


def validate_l4_plan_against_l0(plan, scene_reconstruction):
    scenario_type = plan.get("scenario_type")
    actor_id = actor_id_from_plan(plan) or plan.get("primary_actor_id")
    actor = actor_by_id(scene_reconstruction, actor_id)
    if scenario_type == "front_vehicle_brake":
        if actor_id is None or (actor and not is_vehicle_actor(actor)):
            front_vehicle = find_front_vehicle(scene_reconstruction)
            if front_vehicle:
                plan["plan_validation_note"] = (
                    "front_vehicle_brake primary actor was missing or not a vehicle; "
                    f"overrode with L0 front vehicle actor_id={front_vehicle.get('id')}."
                )
            else:
                raise ValueError("front_vehicle_brake requires a front vehicle, but no suitable L0 vehicle was found.")
            plan["primary_actor_id"] = front_vehicle.get("id")
            plan["primary_actor_source"] = "l0_actor"
            plan["primary_actor_kind"] = "vehicle"
            plan["primary_actor_role"] = "front_vehicle"
    return plan


def build_risk_object_spec(plan, scene_reconstruction):
    """Translate the selected scenario into a concrete primary risk object order."""
    scenario_type = plan.get("scenario_type", "unknown")
    trigger_frame = int(plan.get("trigger_frame", 20) or 20)
    ego_location, ego_rotation = ego_anchor(scene_reconstruction)
    actor_id = actor_id_from_plan(plan) or plan.get("primary_actor_id")
    l0_actor = actor_by_id(scene_reconstruction, actor_id)
    front_actor = nearest_front_actor(scene_reconstruction)

    base = {
        "version": "primary_risk_object_v1",
        "hard_rule": (
            "This spec describes only the object that receives the risk perturbation. "
            "The generated script must implement this object/action and must not replace it with a familiar template."
        ),
        "scenario_type": scenario_type,
        "trigger_frame": trigger_frame,
        "coordinate_frame": "world_coordinates_are_precomputed_when_available",
        "ego_anchor": {"location": ego_location, "rotation": ego_rotation},
    }

    if scenario_type == "front_vehicle_brake":
        actor = l0_actor if is_vehicle_actor(l0_actor) else None
        actor = actor or find_front_vehicle(scene_reconstruction)
        actor = actor or (front_actor if is_vehicle_actor(front_actor) else None)
        base.update(
            {
                "primary_object": {
                    "role": "front_braking_vehicle",
                    "kind": "vehicle",
                    "source": "l0_actor" if actor else "generated_actor",
                    "actor_id": (actor or {}).get("id"),
                    "type_id": (actor or {}).get("type_id"),
                    "initial_location": actor_location(actor),
                    "initial_rotation": actor_rotation(actor),
                },
                "action": {
                    "mode": "brake_or_decelerate_after_trigger",
                    "brake_intensity": plan.get("brake_intensity", 1.0),
                    "target_speed_mps": plan.get("target_speed_mps", 0.0),
                    "deceleration_mps2": plan.get("deceleration_mps2", 6.0),
                },
                "success_criteria": {
                    "front_actor_speed_drop_mps_min": 1.0,
                    "front_distance_change_m_min": 1.0,
                },
                "forbidden_substitutions": ["cargo_drop", "payload", "metal_pipe", "pedestrian_intrusion"],
            }
        )
        return base

    if scenario_type == "side_vehicle_intrusion":
        actor = l0_actor
        initial_lateral = (actor or {}).get("relative_lateral_m")
        motion = plan.get("motion", {}) if isinstance(plan.get("motion"), dict) else {}
        requested_shift = abs(as_float(motion.get("distance_m", motion.get("lateral_shift_m", 1.5)), 1.5))
        try:
            needed_shift = max(requested_shift, abs(float(initial_lateral)) - 2.0)
        except (TypeError, ValueError):
            needed_shift = max(requested_shift, 1.5)
        base.update(
            {
                "primary_object": {
                    "role": "existing_side_vehicle",
                    "kind": "vehicle",
                    "source": "l0_actor",
                    "actor_id": actor_id,
                    "type_id": (actor or {}).get("type_id"),
                    "initial_location": actor_location(actor),
                    "initial_rotation": actor_rotation(actor),
                    "initial_relative_lateral_m": initial_lateral,
                    "initial_distance_m": (actor or {}).get("distance_m"),
                },
                "action": {
                    "mode": "lateral_shift_toward_ego_lane",
                    "minimum_lateral_shift_m": round(max(needed_shift, 1.2), 3),
                    "target_abs_relative_lateral_m_max": 2.2,
                    "do_not_spawn_replacement": True,
                },
                "success_criteria": {
                    "primary_actor_id_must_match": actor_id,
                    "relative_lateral_delta_m_min": round(max(needed_shift, 1.2), 3),
                    "min_abs_relative_lateral_m_max": 2.2,
                    "min_distance_to_ego_m_max": 8.0,
                },
                "forbidden_substitutions": ["road_obstacle_intrusion", "cargo_drop", "front_vehicle_brake"],
            }
        )
        return base

    if scenario_type == "vulnerable_actor_intrusion":
        path = build_generated_path_from_ego(plan, scene_reconstruction, "start_position", 18.0, 4.0, end_x=6.0)
        base.update(
            {
                "primary_object": {
                    "role": "vulnerable_actor",
                    "kind": plan.get("actor_type", "walker"),
                    "source": "generated_actor",
                    "actor_id": "generated_vulnerable_actor",
                    "type_id": "walker.*" if plan.get("actor_type", "walker") == "walker" else plan.get("actor_type", "walker"),
                    "initial_location": path.get("start_world"),
                    "initial_rotation": ego_rotation,
                },
                "geometry": {
                    "spawn_side": path.get("side"),
                    "start_local": path.get("start_local"),
                    "end_local": path.get("end_local"),
                    "start_world": path.get("start_world"),
                    "end_world": path.get("end_world"),
                    "path_world": [point for point in (path.get("start_world"), path.get("end_world")) if point],
                    "lane_crossing_required": True,
                    "world_origin_is_forbidden": True,
                },
                "action": {
                    "mode": "walk_or_cycle_across_ego_lane_after_trigger",
                    "speed_mps": plan.get("speed_mps", 2.2),
                    "must_cross_ego_lane_centerline": True,
                    "must_approach_ego": True,
                },
                "success_criteria": {
                    "actor_motion_m_min": 1.0,
                    "min_distance_to_ego_m_max": 8.0,
                    "min_abs_relative_lateral_m_max": 2.2,
                    "relative_lateral_crosses_zero": True,
                    "max_single_frame_displacement_m": 3.0,
                },
                "forbidden_substitutions": ["cargo_drop", "front_vehicle_brake", "road_obstacle_intrusion"],
            }
        )
        return base

    if scenario_type == "cargo_drop":
        carrier = l0_actor or front_actor
        initial = plan.get("initial_position", {"x": -3.2, "y": 0.0, "z": 2.4})
        carrier_location = actor_location(carrier) or ego_location
        carrier_rotation = actor_rotation(carrier) or ego_rotation
        spawn_world = local_offset_to_world(carrier_location, carrier_rotation, initial)
        base.update(
            {
                "primary_object": {
                    "role": "payload",
                    "kind": "payload",
                    "source": "generated_actor",
                    "actor_id": "generated_payload",
                    "type_id": plan.get("object_type", "metal_pipe"),
                    "initial_location": spawn_world,
                    "initial_rotation": carrier_rotation,
                    "carrier_actor_id": (carrier or {}).get("id"),
                    "carrier_type_id": (carrier or {}).get("type_id"),
                },
                "geometry": {
                    "spawn_relative_to": "front_carrier_actor",
                    "initial_local_offset_from_carrier": xyz_dict(initial),
                    "initial_world_location": spawn_world,
                    "target_zone": "ego_lane_ahead_or_between_carrier_and_ego",
                },
                "action": {
                    "mode": "payload_drop_or_slide_after_trigger",
                    "object_type": plan.get("object_type", "metal_pipe"),
                    "object_count": plan.get("object_count", 5),
                    "motion": plan.get("motion", {}),
                },
                "success_criteria": {
                    "payload_count_min": 1,
                    "payload_motion_m_min": 0.5,
                    "payload_min_distance_to_ego_m_max": 15.0,
                },
                "forbidden_substitutions": ["front_vehicle_brake", "pedestrian_intrusion"],
            }
        )
        return base

    path = build_generated_path_from_ego(plan, scene_reconstruction, "initial_position", 14.0, 3.0)
    base.update(
        {
            "primary_object": {
                "role": "road_obstacle",
                "kind": "obstacle",
                "source": "generated_actor",
                "actor_id": "generated_road_obstacle",
                "type_id": plan.get("object_type", "road_obstacle"),
                "initial_location": path.get("start_world"),
                "initial_rotation": ego_rotation,
            },
            "geometry": {
                "start_local": path.get("start_local"),
                "target_local": path.get("end_local"),
                "start_world": path.get("start_world"),
                "target_world": path.get("end_world"),
                "must_enter_ego_lane": True,
            },
            "action": {
                "mode": "place_or_move_obstacle_into_ego_lane_after_trigger",
                "motion": plan.get("motion", {}),
            },
            "success_criteria": {
                "obstacle_count_min": 1,
                "min_obstacle_distance_to_ego_m_max": 12.0,
                "min_abs_obstacle_relative_lateral_m_max": 2.2,
            },
            "forbidden_substitutions": ["front_vehicle_brake", "cargo_drop"],
        }
    )
    return base


def is_lateral_vehicle_motion(plan):
    motion = plan.get("motion") if isinstance(plan, dict) else {}
    motion_mode = str((motion or {}).get("mode", "")).lower()
    text = json.dumps(plan.get("actor_motion_plan", {}), ensure_ascii=False).lower()
    indicators = [
        "lateral",
        "lane_change",
        "shift",
        "intruder_vehicle",
        "accelerating_vehicle",
    ]
    return any(indicator in motion_mode or indicator in text for indicator in indicators)


def refine_plan_against_l0(plan, reconstruction_state):
    plan = dict(plan)
    actor_id = actor_id_from_plan(plan)
    actor = actor_by_id(reconstruction_state, actor_id)
    if actor and actor.get("kind") == "vehicle" and is_lateral_vehicle_motion(plan):
        plan["scenario_type"] = "side_vehicle_intrusion"
        plan["primary_actor_source"] = "l0_actor"
        plan["primary_actor_id"] = actor_id
        plan["primary_actor_type_id"] = actor.get("type_id")
        plan["primary_actor_initial_relative_lateral_m"] = actor.get("relative_lateral_m")
        plan["primary_actor_initial_distance_m"] = actor.get("distance_m")
        plan.setdefault("expected_visual_result", "L0侧方车辆向自车车道方向横向侵入")
    return plan


def build_physical_task(plan, scene_reconstruction, risk_object_spec=None):
    scenario_type = plan.get("scenario_type", "unknown")
    trigger_frame = int(plan.get("trigger_frame", 20) or 20)
    motion = plan.get("motion", {}) if isinstance(plan.get("motion"), dict) else {}
    risk_object_spec = risk_object_spec or {}
    risk_primary = risk_object_spec.get("primary_object") if isinstance(risk_object_spec, dict) else {}
    risk_primary = risk_primary if isinstance(risk_primary, dict) else {}
    actor_id = actor_id_from_plan(plan) or plan.get("primary_actor_id")
    primary_actor = actor_by_id(scene_reconstruction, actor_id)
    if scenario_type == "front_vehicle_brake" and not primary_actor:
        primary_actor = nearest_front_actor(scene_reconstruction)
        actor_id = (primary_actor or {}).get("id") or actor_id
    initial_location = primary_actor.get("location") if primary_actor else risk_primary.get("initial_location")
    initial_rotation = primary_actor.get("rotation") if primary_actor else risk_primary.get("initial_rotation")

    task = {
        "version": "l4_physical_task_v1",
        "scenario_type": scenario_type,
        "hard_rule": "The generated CARLA scene must implement this physical_task exactly; do not substitute a different actor, object, or event type.",
        "risk_object_spec": risk_object_spec,
        "primary_actor": {
            "source": "l0_actor" if primary_actor else "generated_actor",
            "actor_id": actor_id if primary_actor else risk_primary.get("actor_id"),
            "kind": primary_actor.get("kind") if primary_actor else risk_primary.get("kind"),
            "type_id": primary_actor.get("type_id") if primary_actor else risk_primary.get("type_id"),
            "initial_location": initial_location,
            "initial_rotation": initial_rotation,
            "initial_relative_lateral_m": primary_actor.get("relative_lateral_m") if primary_actor else None,
            "initial_relative_longitudinal_m": primary_actor.get("relative_longitudinal_m") if primary_actor else None,
            "initial_distance_m": primary_actor.get("distance_m") if primary_actor else None,
        },
        "ego_actor": {
            "source": "l0_ego",
            "initial_location": (scene_reconstruction.get("ego") or {}).get("location") if isinstance(scene_reconstruction, dict) else None,
            "initial_rotation": (scene_reconstruction.get("ego") or {}).get("rotation") if isinstance(scene_reconstruction, dict) else None,
        },
        "action": {
            "trigger_frame": trigger_frame,
            "duration_frames": max(10, int(round(float(motion.get("duration_s", 1.0) or 1.0) / 0.05))),
        },
        "trace_schema": {
            "top_level_frames_key": "frames",
            "forbidden_top_level_frame_data_key": "frame_data",
            "required_common_frame_fields": ["frame", "ego_speed_mps"],
        },
        "visualization": {
            "output_pattern": "risk_rgb_XXXX.png",
            "primary_actor_must_be_visible": True,
            "default_mode": "ego_surround_montage",
            "required_layout": "2x3",
            "tile_order": [
                "CAM_FRONT",
                "CAM_FRONT_LEFT",
                "CAM_FRONT_RIGHT",
                "CAM_BACK",
                "CAM_BACK_LEFT",
                "CAM_BACK_RIGHT",
            ],
            "camera_specs": [
                {"name": "CAM_FRONT", "x": 1.5, "y": 0.0, "z": 1.6, "pitch": 0.0, "yaw": 0.0, "roll": 0.0, "fov": 90.0},
                {"name": "CAM_FRONT_LEFT", "x": 1.2, "y": -0.4, "z": 1.6, "pitch": 0.0, "yaw": -55.0, "roll": 0.0, "fov": 90.0},
                {"name": "CAM_FRONT_RIGHT", "x": 1.2, "y": 0.4, "z": 1.6, "pitch": 0.0, "yaw": 55.0, "roll": 0.0, "fov": 90.0},
                {"name": "CAM_BACK", "x": -1.5, "y": 0.0, "z": 1.6, "pitch": 0.0, "yaw": 180.0, "roll": 0.0, "fov": 90.0},
                {"name": "CAM_BACK_LEFT", "x": -1.2, "y": -0.4, "z": 1.6, "pitch": 0.0, "yaw": -125.0, "roll": 0.0, "fov": 90.0},
                {"name": "CAM_BACK_RIGHT", "x": -1.2, "y": 0.4, "z": 1.6, "pitch": 0.0, "yaw": 125.0, "roll": 0.0, "fov": 90.0},
            ],
            "notes": "Each saved risk_rgb image must be a six-view 2x3 ego-camera montage using the same layout as the source SafeBench capture.",
        },
    }

    if scenario_type == "side_vehicle_intrusion":
        initial_lateral = primary_actor.get("relative_lateral_m") if primary_actor else None
        requested_shift = abs(float(motion.get("distance_m", motion.get("lateral_shift_m", 0.5)) or 0.5))
        try:
            needed_shift = max(requested_shift, abs(float(initial_lateral)) - 2.0)
        except (TypeError, ValueError):
            needed_shift = max(requested_shift, 1.5)
        task["action"].update(
            {
                "mode": "move_existing_side_vehicle_toward_ego_lane",
                "direction": "toward_ego_lane_center",
                "minimum_lateral_shift_m": round(max(needed_shift, 1.2), 3),
                "target_abs_relative_lateral_m_max": 2.2,
                "keep_primary_actor_visible": True,
                "do_not_spawn_replacement_primary_actor": True,
            }
        )
        task["success_criteria"] = {
            "primary_actor_id_must_match": actor_id,
            "relative_lateral_delta_m_min": round(max(needed_shift, 1.2), 3),
            "min_abs_relative_lateral_m_max": 2.2,
            "min_distance_to_ego_m_max": 8.0,
            "saved_images_must_show_primary_actor": True,
        }
        task["trace_schema"]["required_common_frame_fields"].extend(
            [
                "primary_actor_id",
                "primary_actor_type_id",
                "primary_actor_position",
                "distance_to_ego_m",
                "relative_lateral_m",
            ]
        )
        task["visualization"]["forbidden"] = "Do not save only a front camera view when the primary actor is side-left or side-right."
    else:
        task["action"].update(
            {
                "mode": motion.get("mode", scenario_type),
                "raw_motion": motion,
                "primary_risk_object_action": risk_object_spec.get("action"),
                "primary_risk_object_geometry": risk_object_spec.get("geometry"),
            }
        )
        task["success_criteria"] = dict((risk_object_spec.get("success_criteria") or {}))
        task["success_criteria"].update({
            "must_match_scenario_type": scenario_type,
            "must_use_primary_actor_from_event_contract": True,
            "saved_images_must_show_primary_event": True,
        })
    return task


def preserve_plan_identity(normalized, plan):
    for key in (
        "primary_actor_id",
        "primary_actor_source",
        "primary_actor_kind",
        "primary_actor_role",
        "primary_actor_type_id",
        "target_actor_id",
        "target_actor_source",
        "plan_validation_note",
    ):
        if key in plan:
            normalized[key] = plan[key]
    if isinstance(plan.get("object_registry"), dict):
        normalized["object_registry"] = plan["object_registry"]
    return normalized


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
        "side_vehicle_intrusion",
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
        return preserve_plan_identity(normalized, plan)

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
        return preserve_plan_identity(normalized, plan)

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
        return preserve_plan_identity(normalized, plan)

    if scenario_type == "side_vehicle_intrusion":
        normalized = {
            "scenario_type": scenario_type,
            "object_type": plan.get("object_type", "existing_side_vehicle"),
            "trigger_frame": trigger_frame,
            "motion": plan.get(
                "motion",
                {
                    "mode": "lateral_shift_toward_ego_lane",
                    "duration_s": 1.0,
                    "distance_m": 1.5,
                },
            ),
            "expected_visual_result": plan.get("expected_visual_result", "L0侧方车辆向自车车道方向横向侵入"),
        }
        normalized["actor_motion_plan"] = plan.get("actor_motion_plan") or default_actor_motion_plan(normalized)
        return preserve_plan_identity(normalized, plan)

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
    return preserve_plan_identity(normalized, plan)


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
    if scenario_type == "side_vehicle_intrusion":
        return {
            "ego": {
                "role": "observer_vehicle",
                "behavior": "maintain_current_speed",
                "avoid_collision": True,
            },
            "primary_actor": {
                "role": "existing_l0_side_vehicle",
                "behavior": "move_laterally_toward_ego_lane_after_trigger",
                "trigger_frame": trigger_frame,
                "must_enter_ego_lane_lateral_band": True,
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
    elif scenario_type == "side_vehicle_intrusion":
        base.update(
            {
                "primary_actor": "existing_l0_side_vehicle",
                "background_actors": ["ego", "nearby_actors"],
                "event_applied": "configured existing side vehicle moves laterally toward the ego lane after trigger_frame",
                "required_frame_fields": [
                    "frame",
                    "ego_speed_mps",
                    "primary_actor_id",
                    "primary_actor_type_id",
                    "primary_actor_position",
                    "distance_to_ego_m",
                    "relative_lateral_m",
                ],
                "success_condition": "the same L0 side vehicle actor moves laterally toward the ego lane and gets close enough to be a visible side intrusion",
                "forbidden": [
                    "spawning a new obstacle as the primary event",
                    "payload actors",
                    "metal_pipe",
                    "front-vehicle braking as the primary event",
                    "using a different actor id as the primary actor",
                ],
                "numeric_acceptance": {
                    "relative_lateral_delta_m_min": 1.2,
                    "min_abs_relative_lateral_m_max": 2.2,
                    "min_distance_to_ego_m_max": 8.0,
                    "primary_actor_id_must_match_config": True,
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


def build_config(
    chain,
    l0_state=None,
    l0_json_path=None,
    l4_frames=140,
    local_trigger_frame=20,
    pre_trigger_seconds=2.0,
    source_timestep=0.05,
    plan_agent_args=None,
):
    reconstruction_state, time_axis_policy = select_reconstruction_state(
        l0_state,
        pre_trigger_seconds=pre_trigger_seconds,
        source_timestep=source_timestep,
    )
    l4_plan_agent = {
        "enabled": bool(plan_agent_args and not getattr(plan_agent_args, "skip_plan_agent", False)),
        "used": False,
        "error": None,
        "raw_response": "",
        "output": None,
    }

    raw_plan = chain.get("carla_plan", {}) if isinstance(chain.get("carla_plan"), dict) else {}
    if not raw_plan:
        raw_plan = fallback_raw_plan_from_chain_text(chain)
    if l4_plan_agent["enabled"]:
        try:
            agent_output, raw_response = run_l4_plan_agent(plan_agent_args, chain, l0_state, reconstruction_state)
            l4_plan_agent.update(
                {
                    "used": True,
                    "raw_response": raw_response,
                    "output": agent_output,
                    "model": getattr(plan_agent_args, "plan_model", None),
                }
            )
            raw_plan = merge_plan_with_agent_output(raw_plan, agent_output)
        except (DeepSeekError, json.JSONDecodeError, ValueError) as exc:
            l4_plan_agent["error"] = repr(exc)
            print(f"WARNING: L4 PlanAgent failed; using L3 carla_plan/fallback rules: {exc}", file=sys.stderr)

    original_trigger_frame = int((raw_plan or {}).get("trigger_frame", 45) or 45)
    plan = normalize_l4_plan(raw_plan)
    local_trigger_frame = clamp_local_trigger_frame(local_trigger_frame, l4_frames)
    plan = replace_nested_trigger_frames(plan, local_trigger_frame, original_trigger_frame)
    plan["original_l3_trigger_frame"] = original_trigger_frame
    plan["trigger_frame_semantics"] = "local_l4_frame_after_scene_reconstruction"
    plan = adapt_actor_motion_plan_to_reconstruction(plan, reconstruction_state)
    plan = refine_plan_against_l0(plan, reconstruction_state)
    scene_reconstruction = compact_l0_scene(reconstruction_state)
    try:
        plan = validate_l4_plan_against_l0(plan, scene_reconstruction or {})
    except ValueError as exc:
        l4_plan_agent["validation_error"] = str(exc)
        print(f"WARNING: L4 plan validation failed; continuing with normalized plan: {exc}", file=sys.stderr)
    time_axis_policy.update(
        {
            "local_trigger_frame": local_trigger_frame,
            "l4_frames": l4_frames,
            "rule": (
                "Reconstruct from reconstruction_frame, then apply carla_plan.trigger_frame "
                "as a local L4 frame. Do not treat original_l3_trigger_frame as a local frame."
            ),
        }
    )
    risk_object_spec = build_risk_object_spec(plan, scene_reconstruction or {})
    physical_task = build_physical_task(plan, scene_reconstruction or {}, risk_object_spec)
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
        "original_l3_trigger_frame": original_trigger_frame,
        "carla_plan": plan,
        "l4_plan_agent": l4_plan_agent,
        "object_registry": (l4_plan_agent.get("output") or {}).get("object_registry") or chain_object_registry(chain),
        "risk_object_spec": risk_object_spec,
        "physical_task": physical_task,
        "event_contract": build_event_contract(plan),
        "scene_reconstruction": scene_reconstruction,
        "time_axis_policy": time_axis_policy,
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
            "Read `scenario_config.json`, optional `l0_state.json`, `reference_executor.py`, `context/failure_history.md`, and the files under `context/` before editing.\n"
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
- Read reference_executor.py and the files under context/, especially context/failure_history.md. Use reference_executor.py for CARLA mechanics only, not as an event template.
- Edit the neutral seeded Python script in place at exactly:
  {output_script}
- Replace the NotImplementedError with scenario-specific behavior from scenario_config.json.
- Read scenario_config.risk_object_spec before writing the event logic. It is the concrete translation of the main object that receives the risk perturbation.
- Read scenario_config.object_registry and scenario_config.l4_plan_agent.output when present. They explain which objects are primary participants and which objects must remain background.
- Treat risk_object_spec.primary_object as the only primary risk object. Strengthen that object/action; do not substitute a different actor, payload, pedestrian, or braking template.
- Treat scenario_config.physical_task as the hard physical task order. It defines the primary actor, the action, timing, trace schema, and success criteria.
- Before designing the scene, apply the checklist in context/failure_history.md. Do not repeat any listed failure mode.
- If physical_task conflicts with free-form text such as chain_description or expected_visual_result, follow physical_task.
- If physical_task.primary_actor.source is "l0_actor", the primary event must use that same L0 actor id/type/initial pose as closely as CARLA spawn constraints allow. Do not replace it with a generic obstacle or newly invented actor.
- After spawning any actor from physical_task, immediately verify actor.get_location() is close to the requested initial_location. If the actor appears near world origin or a random spawn point, destroy it and retry near the requested L0 pose or fail clearly.
- For ego and vehicle primary actors, raw L0 poses may not be directly spawnable. Project to a nearby driving-lane waypoint with world.get_map().get_waypoint(project_to_road=True, lane_type=carla.LaneType.Driving), try small z offsets and nearby lane shifts, then verify the live location. Never accept a spawn near (0,0,0).
- Reconstruct the scene from L0 scene_reconstruction/source state: preserve town/map, weather, ego pose, nearest front actor, and relevant nearby actors as much as CARLA spawn constraints allow.
- Treat carla_plan.trigger_frame as a local frame in the generated L4 simulation, not as an original SafeBench global frame.
- Follow scenario_config.time_axis_policy: start from scene_reconstruction.source_frame, trigger at local_trigger_frame, then continue until --frames.
- Do not choose an unrelated spawn point when L0 ego.location/rotation is available.
- Follow carla_plan.actor_motion_plan exactly. L0 gives the initial picture; actor_motion_plan gives what every actor should do after L0.
- Do not invent actor behavior that contradicts actor_motion_plan.
- The script must connect to the configured CARLA server using the installed CARLA Python API, reconstruct the L0 ego/front/relevant actors, and execute the requested risk event from carla_plan.
- Save risk images into the --output-dir argument as risk_rgb_XXXX.png.
- Follow physical_task.visualization. By default each risk_rgb_XXXX.png must be a six-view 2x3 ego-camera montage.
- The required six-view tile order is CAM_FRONT, CAM_FRONT_LEFT, CAM_FRONT_RIGHT, CAM_BACK, CAM_BACK_LEFT, CAM_BACK_RIGHT.
- Save per-camera images in optional subdirectories if useful, but the top-level risk_rgb_XXXX.png must be the six-view montage used for review.
- The saved images must make the primary actor/event visible when physical_task.visualization.primary_actor_must_be_visible is true.
- Write --output-dir/event_trace.json exactly as required by scenario_config.event_contract. The trace must prove that this chain's physical event was applied.
- Keep the script self-contained. Do not require project imports.
- Support these CLI arguments: --carla-root, --host, --port, --town, --output-dir, --frames, --save-every.
- Use synchronous mode and restore original world settings in finally.
- Respect carla_plan.scenario_type exactly. Do not combine unrelated actions across scenario types.
- Respect scenario_config.event_contract.primary_actor. The primary actor must drive the visible risk event; background actors must not become the main event.
- Write event_trace.json with top-level "frames" as a non-empty list of per-frame states. Do not write per-frame states under "frame_data".
- For front_vehicle_brake, implement only front-vehicle braking/deceleration. Do not spawn payloads or metal pipes unless the config explicitly uses cargo_drop.
- For cargo_drop, implement payload/drop motion from the configured object and motion fields.
- For vulnerable_actor_intrusion, implement a walker/cyclist intrusion using actor_type and crossing fields.
- For road_obstacle_intrusion, implement a static or slow obstacle entering the ego lane.
- For side_vehicle_intrusion, use the existing L0 side vehicle from physical_task.primary_actor. Move that vehicle laterally toward the ego lane after physical_task.action.trigger_frame until physical_task.success_criteria is satisfied. Do not implement this as a spawned road obstacle.
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
Read scenario_config.risk_object_spec first and repair the script so that exact primary risk object/action is implemented.
Treat scenario_config.physical_task as the hard execution contract. If the script does not satisfy physical_task.success_criteria, change the physical scene, not just the trace.
If ego or a vehicle primary actor spawned at `(0,0,0)` or far from the requested L0 pose, repair the spawn logic: use waypoint projection near the requested L0 location, small z offsets, and nearby lane shifts, then verify the live actor location. Do not switch to an unrelated spawn point.
Fix the root cause, especially CARLA import/scope errors such as UnboundLocalError from referencing carla before import.
If the failure mentions event_trace, implement or fix --output-dir/event_trace.json according to scenario_config.event_contract.
If the failure mentions semantic validation, change the physical scene so the primary actor satisfies event_contract.numeric_acceptance; do not fake trace values.
Read reference_executor.py, context/known_failures.md, context/failure_history.md, and the current generated_risk_scene.py before editing.
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
    if "frame_data" in trace:
        raise RuntimeError("event_trace must put per-frame state in top-level frames, not frame_data.")
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


def png_size(path):
    with open(path, "rb") as f:
        header = f.read(24)
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return struct.unpack(">II", header[16:24])


def validate_risk_image_layout(config_path, images_dir):
    config = read_json(config_path)
    visualization = (config.get("physical_task") or {}).get("visualization") or {}
    if visualization.get("default_mode") != "ego_surround_montage":
        return
    images = sorted(
        name
        for name in os.listdir(images_dir)
        if name.startswith("risk_rgb_") and name.lower().endswith(".png")
    )
    if not images:
        return
    size = png_size(os.path.join(images_dir, images[0]))
    if not size:
        return
    width, height = size
    require(width > height * 2, f"risk image {images[0]} does not look like a 2x3 six-view montage: {width}x{height}")


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
        risk_spec = config.get("risk_object_spec", {})
        configured_start = point_from_value(
            ((risk_spec.get("primary_object") or {}).get("initial_location"))
            or ((risk_spec.get("geometry") or {}).get("start_world"))
        )
        all_actor_points = point_values(frames, "vulnerable_actor_position")
        if configured_start and all_actor_points:
            initial_error = point_distance(configured_start, all_actor_points[0])
            require(
                initial_error <= 5.0,
                "vulnerable_actor_intrusion primary actor spawned far from risk_object_spec start location "
                f"({initial_error:.3f}m). This usually means local/world coordinates were mixed.",
            )
        if len(all_actor_points) >= 2:
            max_step = max(point_distance(a, b) for a, b in zip(all_actor_points, all_actor_points[1:]))
            step_limit = as_float(
                ((risk_spec.get("success_criteria") or {}).get("max_single_frame_displacement_m")),
                3.0,
            )
            require(
                max_step <= step_limit,
                "vulnerable_actor_intrusion primary actor teleported or jumped too far between frames "
                f"({max_step:.3f}m > {step_limit:.3f}m).",
            )
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

    if scenario_type == "side_vehicle_intrusion":
        physical_task = config.get("physical_task", {})
        expected_actor_id = (
            physical_task.get("primary_actor", {}).get("actor_id")
            or plan.get("primary_actor_id")
            or actor_id_from_plan(plan)
        )
        observed_ids = [frame.get("primary_actor_id") for frame in frames if frame.get("primary_actor_id") is not None]
        require(observed_ids, "side_vehicle_intrusion trace must include primary_actor_id in frames.")
        if expected_actor_id is not None:
            require(
                all(str(actor_id) == str(expected_actor_id) for actor_id in observed_ids),
                f"side_vehicle_intrusion primary_actor_id must stay {expected_actor_id}; got {sorted(set(map(str, observed_ids)))}.",
            )
        actor_points = point_values(after, "primary_actor_position")
        require(len(actor_points) >= 2, "side_vehicle_intrusion trace must include primary_actor_position after trigger.")
        all_actor_points = point_values(frames, "primary_actor_position")
        configured_location = (physical_task.get("primary_actor") or {}).get("initial_location")
        configured_point = point_from_value(configured_location)
        if configured_point and all_actor_points:
            initial_error = point_distance(configured_point, all_actor_points[0])
            require(
                initial_error <= 3.0,
                "side_vehicle_intrusion primary actor spawned far from physical_task.primary_actor.initial_location "
                f"({initial_error:.3f}m). This usually means it fell back to world origin or an unrelated spawn point.",
            )
        laterals = numeric_values(frames, "relative_lateral_m")
        require(laterals, "side_vehicle_intrusion trace must include relative_lateral_m.")
        action = physical_task.get("action", {})
        criteria = physical_task.get("success_criteria", {})
        delta_min = float(criteria.get("relative_lateral_delta_m_min", action.get("minimum_lateral_shift_m", 1.2)) or 1.2)
        lane_max = float(criteria.get("min_abs_relative_lateral_m_max", action.get("target_abs_relative_lateral_m_max", 2.2)) or 2.2)
        lateral_delta = max(abs(laterals[0] - value) for value in laterals[1:]) if len(laterals) >= 2 else 0.0
        require(lateral_delta >= delta_min, f"side_vehicle_intrusion lateral motion too small: {lateral_delta:.3f}m < {delta_min:.3f}m.")
        require(min(abs(value) for value in laterals) <= lane_max, "side_vehicle_intrusion actor never entered the required ego-lane lateral band.")
        distances = numeric_values(frames, "distance_to_ego_m")
        require(distances, "side_vehicle_intrusion trace must include distance_to_ego_m.")
        distance_max = float(criteria.get("min_distance_to_ego_m_max", 8.0) or 8.0)
        require(min(distances) <= distance_max, "side_vehicle_intrusion actor never got close enough to ego.")
        forbidden_text = json.dumps(trace, ensure_ascii=False).lower()
        require("payload" not in forbidden_text and "metal_pipe" not in forbidden_text, "side_vehicle_intrusion trace includes unrelated cargo artifacts.")
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
            validate_risk_image_layout(config_path, images_dir)
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
    config = build_config(
        chain,
        l0_state,
        args.l0_json,
        l4_frames=args.frames,
        local_trigger_frame=args.local_trigger_frame,
        pre_trigger_seconds=args.pre_trigger_seconds,
        source_timestep=args.source_timestep,
        plan_agent_args=args,
    )
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
    parser.add_argument("--local-trigger-frame", type=int, default=20, help="Local L4 frame at which the generated risk event should begin.")
    parser.add_argument("--pre-trigger-seconds", type=float, default=2.0, help="Choose an L0 reconstruction frame this many seconds before the closest-risk frame when possible.")
    parser.add_argument("--source-timestep", type=float, default=0.05, help="Timestep used by the source SafeBench capture for frame-to-second conversion.")
    parser.add_argument("--execute", action="store_true", help="Run CARLA executor to produce risk images.")
    parser.add_argument("--code-agent", choices=["template", "opencode"], default="opencode")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default="deepseek-v4-pro")
    parser.add_argument("--opencode-repair-attempts", type=int, default=REPAIR_ATTEMPTS)
    parser.add_argument("--skip-plan-agent", action="store_true", help="Skip the L4 PlanAgent and use the L3 carla_plan/fallback rules directly.")
    parser.add_argument("--plan-model", default=DEFAULT_DEEPSEEK_MODEL, help="DeepSeek model used by the L4 PlanAgent.")
    parser.add_argument("--plan-url", default=DEFAULT_DEEPSEEK_URL, help="DeepSeek chat-completions URL used by the L4 PlanAgent.")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY", help="Environment variable containing the DeepSeek API key for the L4 PlanAgent.")
    parser.add_argument("--env-file", default=None, help="Optional .env file for the L4 PlanAgent API key.")
    parser.add_argument("--plan-timeout", type=float, default=300.0)
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
