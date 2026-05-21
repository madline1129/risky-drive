#!/usr/bin/env python3
"""Minimal L4 helpers for the OpenCode + Scenic backend."""

import json
import os
import shutil

try:
    from deepseek_client import (
        DEFAULT_DEEPSEEK_MODEL,
        DEFAULT_DEEPSEEK_URL,
        chat_json,
        get_api_key,
        parse_json_response,
    )
except ImportError:
    from .deepseek_client import (
        DEFAULT_DEEPSEEK_MODEL,
        DEFAULT_DEEPSEEK_URL,
        chat_json,
        get_api_key,
        parse_json_response,
    )


PLAN_AGENT_PROMPT_TEMPLATE = """你是 L4 PlanAgent。

你的任务是把一条 L3 事故链翻译成最小 L4 执行任务，后续会由 OpenCode 生成 Scenic 脚本。

硬性要求：
- 不改写 L3 的核心事件。
- 如果 L3 / L2 / L1 已经指定 primary_perturbation_object 或 selected_actor，必须沿用这个对象。
- source="l0_actor" 的对象必须保留 actor_id、type_id、kind、location、rotation、relative_longitudinal_m、relative_lateral_m。
- scenario_type 只能是：front_vehicle_brake, cargo_drop, vulnerable_actor_intrusion, road_obstacle_intrusion, side_vehicle_intrusion。
- 只输出 JSON，不要 Markdown。

输出格式：
{
  "level": "L4Plan",
  "scenario_type": "front_vehicle_brake",
  "translation_reason": "为什么这样翻译",
  "primary_object": {
    "source": "l0_actor/generated_object",
    "actor_id": 123,
    "kind": "vehicle/pedestrian/obstacle/payload",
    "role": "front_vehicle",
    "must_drive_primary_event": true,
    "selection_reason": "引用 L0/L3 字段说明"
  },
  "action": {
    "mode": "brake_or_decelerate_after_trigger",
    "speed_mps": 2.2,
    "brake_intensity": 1.0,
    "target_speed_mps": 0.0,
    "deceleration_mps2": 6.0
  },
  "expected_visual_result": "一句话说明生成出来应该看到什么",
  "success_criteria": {
    "关键验收字段": "目标"
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


def normalize_opencode_model_name(model):
    if not model:
        return model
    if model == "deepseek-v4-flash":
        return "deepseek/deepseek-v4-flash"
    if "/" in model:
        return model
    if model.startswith("deepseek"):
        return f"deepseek/{model}"
    return model


def actor_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def actor_by_id(l0_state, wanted_id):
    wanted = actor_id(wanted_id)
    for actor in l0_state.get("actors", []) if isinstance(l0_state, dict) else []:
        if not isinstance(actor, dict):
            continue
        if actor_id(actor.get("id", actor.get("actor_id"))) == wanted:
            return actor
    return None


def compact_l0_for_prompt(l0_state):
    if not isinstance(l0_state, dict):
        return {}
    return {
        "ego": l0_state.get("ego", {}),
        "weather": l0_state.get("weather", {}),
        "actors": l0_state.get("actors", []),
        "source": l0_state.get("source", {}),
    }


def build_l4_plan_agent_prompt(chain, l0_state):
    payload = {
        "l3_chain": chain,
        "l0_state": compact_l0_for_prompt(l0_state),
        "inherited_primary_object": chain.get("primary_perturbation_object") or chain.get("selected_actor"),
        "actor_list": chain.get("actor_list") or [],
    }
    return PLAN_AGENT_PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def run_l4_plan_agent(args, chain, l0_state):
    prompt = build_l4_plan_agent_prompt(chain, l0_state)
    api_key = get_api_key(args.api_key_env, args.env_file)
    raw_response = chat_json(args.plan_url, args.plan_model, api_key, prompt, args.plan_timeout)
    return parse_json_response(raw_response), raw_response


def merge_actor_snapshot(primary, l0_state):
    if not isinstance(primary, dict):
        primary = {}
    merged = dict(primary)
    source = merged.get("source")
    l0_actor = actor_by_id(l0_state, merged.get("actor_id"))
    if source == "l0_actor" and isinstance(l0_actor, dict):
        merged = dict(l0_actor)
        merged["source"] = "l0_actor"
        merged["actor_id"] = l0_actor.get("id", l0_actor.get("actor_id"))
        merged["role"] = primary.get("role", merged.get("role"))
        merged["must_drive_primary_event"] = True
        merged["selection_reason"] = primary.get("selection_reason")
    return merged


def primary_from_chain_or_plan(chain, plan_output, l0_state):
    for candidate in (
        chain.get("primary_perturbation_object"),
        chain.get("selected_actor"),
        plan_output.get("primary_object"),
    ):
        if isinstance(candidate, dict) and candidate:
            return merge_actor_snapshot(candidate, l0_state)
    return {}


def default_action(scenario_type, plan_output):
    action = dict(plan_output.get("action") or {})
    if scenario_type == "front_vehicle_brake":
        action.setdefault("mode", "brake_or_decelerate_after_trigger")
        action.setdefault("brake_intensity", 1.0)
        action.setdefault("target_speed_mps", 0.0)
        action.setdefault("deceleration_mps2", 6.0)
    elif scenario_type == "vulnerable_actor_intrusion":
        action.setdefault("mode", "move_vulnerable_actor_into_ego_lane")
        action.setdefault("speed_mps", 2.2)
        action.setdefault("must_approach_ego", True)
        action.setdefault("must_enter_ego_lane", True)
    elif scenario_type == "side_vehicle_intrusion":
        action.setdefault("mode", "move_side_vehicle_toward_ego_lane")
        action.setdefault("minimum_lateral_shift_m", 1.2)
        action.setdefault("must_keep_same_l0_actor", True)
    elif scenario_type == "cargo_drop":
        action.setdefault("mode", "drop_or_move_payload_toward_ego_path")
        action.setdefault("payload_count_min", 1)
    elif scenario_type == "road_obstacle_intrusion":
        action.setdefault("mode", "place_or_move_obstacle_into_ego_path")
        action.setdefault("obstacle_count_min", 1)
    return action


def default_success_criteria(scenario_type, plan_output):
    criteria = dict(plan_output.get("success_criteria") or {})
    if scenario_type == "front_vehicle_brake":
        criteria.setdefault("front_actor_speed_drop_mps_min", 1.0)
        criteria.setdefault("front_distance_change_m_min", 1.0)
    elif scenario_type == "vulnerable_actor_intrusion":
        criteria.setdefault("actor_motion_m_min", 1.0)
        criteria.setdefault("min_distance_to_ego_m_max", 8.0)
        criteria.setdefault("min_abs_relative_lateral_m_max", 2.2)
        criteria.setdefault("relative_lateral_crosses_zero", True)
    elif scenario_type == "side_vehicle_intrusion":
        criteria.setdefault("relative_lateral_delta_m_min", 1.2)
        criteria.setdefault("min_abs_relative_lateral_m_max", 2.2)
        criteria.setdefault("min_distance_to_ego_m_max", 8.0)
    elif scenario_type == "cargo_drop":
        criteria.setdefault("payload_count_min", 1)
        criteria.setdefault("payload_motion_m_min", 0.5)
    elif scenario_type == "road_obstacle_intrusion":
        criteria.setdefault("obstacle_count_min", 1)
        criteria.setdefault("min_obstacle_distance_to_ego_m_max", 12.0)
    criteria.setdefault("must_match_scenario_type", scenario_type)
    criteria.setdefault("must_use_primary_actor_from_config", True)
    return criteria


def event_contract(scenario_type, success_criteria):
    fields = ["frame", "ego_speed_mps"]
    if scenario_type == "front_vehicle_brake":
        fields.extend(["front_actor_speed_mps", "front_distance_m"])
    elif scenario_type == "vulnerable_actor_intrusion":
        fields.extend(["vulnerable_actor_position", "distance_to_ego_m", "relative_lateral_m"])
    elif scenario_type == "side_vehicle_intrusion":
        fields.extend(["primary_actor_position", "distance_to_ego_m", "relative_lateral_m"])
    elif scenario_type in {"cargo_drop", "road_obstacle_intrusion"}:
        fields.extend(["primary_actor_position", "distance_to_ego_m"])
    return {
        "trace_file": "event_trace.json",
        "required_top_level_fields": ["scenario_type", "trigger_frame", "frames", "event_applied"],
        "required_frame_fields": fields,
        "numeric_acceptance": success_criteria,
    }


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
    if plan_agent_args is None:
        raise ValueError("L4 requires PlanAgent args; deterministic fallback is disabled.")

    plan_output, raw_response = run_l4_plan_agent(plan_agent_args, chain, l0_state or {})
    scenario_type = plan_output.get("scenario_type")
    if not scenario_type:
        raise ValueError("L4 PlanAgent output missing scenario_type.")

    primary_actor = primary_from_chain_or_plan(chain, plan_output, l0_state or {})
    action = default_action(scenario_type, plan_output)
    trigger_frame = int(local_trigger_frame or 20)
    action["trigger_frame"] = trigger_frame
    success_criteria = default_success_criteria(scenario_type, plan_output)

    config = {
        "level": "L4",
        "source_l3_chain_id": chain.get("id"),
        "source_l2_id": chain.get("parent_l2_id"),
        "source_l0_state_file": os.path.abspath(l0_json_path) if l0_json_path else None,
        "scenario_type": scenario_type,
        "trigger_frame": trigger_frame,
        "chain_description": chain.get("chain_description"),
        "direct_physical_outcome": chain.get("direct_physical_outcome"),
        "expected_visual_result": plan_output.get("expected_visual_result"),
        "translation_reason": plan_output.get("translation_reason"),
        "primary_actor": primary_actor,
        "action": action,
        "success_criteria": success_criteria,
        "event_contract": event_contract(scenario_type, success_criteria),
        "execution_backend": "opencode_scenic",
        "_l4_plan_agent_raw": {
            "model": getattr(plan_agent_args, "plan_model", None),
            "raw_response": raw_response,
            "output": plan_output,
        },
    }
    return config
