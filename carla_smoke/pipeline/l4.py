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
    from risk_library import action_primitive_by_id, risk_type_by_id
except ImportError:
    from .deepseek_client import (
        DEFAULT_DEEPSEEK_MODEL,
        DEFAULT_DEEPSEEK_URL,
        chat_json,
        get_api_key,
        parse_json_response,
    )
    from .risk_library import action_primitive_by_id, risk_type_by_id


PLAN_AGENT_PROMPT_TEMPLATE = """你是 L4 PlanAgent。

你的任务是把一条 L3 事故链翻译成最小 L4 执行任务，后续会由 OpenCode 生成 Scenic 脚本。

硬性要求：
- 不改写 L3 的核心事件。
- 如果 L3 已经给出 risk_type_id / primary_trigger_action_id / action_primitives，必须优先沿用；你的任务是把这些动作原语实例化为具体参数，不要自由发明 action.mode。
- L3 不负责选择完整物体或出生地点；你必须根据 L0 actors、L3 chain_participants 和 action_primitives 选择 primary_object，并保留所选 L0 actor 的 actor_id、type_id、kind、location、rotation、relative_longitudinal_m、relative_lateral_m。
- scenario_type 只能是：front_vehicle_brake, cargo_drop, vulnerable_actor_intrusion, road_obstacle_intrusion, side_vehicle_intrusion, ego_action_risk。
- 只输出 JSON，不要 Markdown。

输出格式：
{
  "level": "L4Plan",
  "scenario_type": "front_vehicle_brake",
  "risk_family": "lead_vehicle_risk",
  "risk_type_id": "lead_vehicle_hard_brake",
  "primary_action_primitive_id": "front_vehicle_brake_after_trigger",
  "translation_reason": "为什么这样翻译",
  "primary_object": {
    "source": "l0_actor/generated_object",
    "actor_id": 123,
    "kind": "vehicle/pedestrian/obstacle/payload",
    "role": "front_vehicle",
    "must_drive_primary_event": true,
    "selection_reason": "引用 L0/L3 字段说明"
  },
  "action_primitive": {
    "id": "front_vehicle_brake_after_trigger",
    "actor_role": "front_vehicle",
    "motion_frame": "lane_following",
    "front_initial_speed_mps": 8.0,
    "target_speed_mps": 0.0,
    "brake_intensity": 1.0,
    "trigger_frame": 40,
    "direction": {
      "frame": "ego_local",
      "longitudinal_m": 14.0,
      "lateral_m": 0.0,
      "heading_delta_deg": 0.0
    }
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
    if model == "deepseek-v4-pro":
        return "deepseek/deepseek-v4-pro"
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


def as_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


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
    risk_type = risk_type_by_id(chain.get("risk_type_id")) or {}
    primitive_id = chain.get("primary_trigger_action_id") or risk_type.get("primary_action_primitive_id")
    action_primitive = action_primitive_by_id(primitive_id) or {}
    payload = {
        "l3_chain": chain,
        "l0_state": compact_l0_for_prompt(l0_state),
        "inherited_primary_object": chain.get("primary_perturbation_object") or chain.get("selected_actor"),
        "actor_list": chain.get("actor_list") or [],
        "risk_library_selection": {
            "risk_family": chain.get("risk_family") or risk_type.get("family"),
            "risk_type": risk_type,
            "primary_action_primitive": action_primitive,
            "l3_action_primitives": chain.get("action_primitives") or [],
            "participant_actions": chain.get("participant_actions") or [],
        },
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


def concrete_action_primitive(
    scenario_type,
    primitive_id,
    library_primitive,
    plan_output,
    primary_actor,
    trigger_frame,
    pre_trigger_seconds=2.0,
    source_timestep=0.05,
):
    plan_primitive = dict(plan_output.get("action_primitive") or {})
    legacy_action = dict(plan_output.get("action") or {})
    library_primitive = dict(library_primitive or {})
    base = {}
    if primitive_id:
        base["id"] = primitive_id
    base["actor_role"] = (
        plan_primitive.get("actor_role")
        or library_primitive.get("actor_role")
        or library_primitive.get("controlled_actor_role")
        or legacy_action.get("controlled_actor")
        or "primary_risk_actor"
    )
    base["motion_frame"] = (
        plan_primitive.get("motion_frame")
        or legacy_action.get("motion_frame")
        or library_primitive.get("motion_frame")
    )
    if scenario_type == "front_vehicle_brake":
        rel = primary_actor.get("relative_to_ego") if isinstance(primary_actor, dict) else {}
        if not isinstance(rel, dict):
            rel = {}
        actor_longitudinal = primary_actor.get("relative_longitudinal_m") if isinstance(primary_actor, dict) else None
        actor_lateral = primary_actor.get("relative_lateral_m") if isinstance(primary_actor, dict) else None
        actor_speed = primary_actor.get("speed_mps") if isinstance(primary_actor, dict) else None
        library_direction = library_primitive.get("direction") if isinstance(library_primitive.get("direction"), dict) else {}
        plan_direction = plan_primitive.get("direction") if isinstance(plan_primitive.get("direction"), dict) else {}
        longitudinal_m = as_float(
            plan_direction.get("longitudinal_m", plan_primitive.get("longitudinal_m")),
            as_float(
                rel.get("longitudinal_m"),
                as_float(actor_longitudinal, as_float(library_direction.get("longitudinal_m"), 14.0)),
            ),
        )
        lateral_m = as_float(
            plan_direction.get("lateral_m", plan_primitive.get("lateral_m")),
            as_float(rel.get("lateral_m"), as_float(actor_lateral, as_float(library_direction.get("lateral_m"), 0.0))),
        )
        initial_speed = as_float(
            plan_primitive.get("front_initial_speed_mps"),
            as_float(
                legacy_action.get("front_initial_speed_mps"),
                as_float(legacy_action.get("speed_mps"), as_float(actor_speed, as_float(library_primitive.get("front_initial_speed_mps"), 8.0))),
            ),
        )
        target_speed = as_float(
            plan_primitive.get("target_speed_mps"),
            as_float(legacy_action.get("target_speed_mps"), as_float(library_primitive.get("target_speed_mps"), 0.0)),
        )
        brake_intensity = as_float(
            plan_primitive.get("brake_intensity"),
            as_float(legacy_action.get("brake_intensity"), as_float(library_primitive.get("brake_intensity"), 1.0)),
        )
        concrete_trigger_frame = int(
            plan_primitive.get("trigger_frame")
            or legacy_action.get("trigger_frame")
            or library_primitive.get("trigger_frame")
            or trigger_frame
        )
        concrete = {
            **base,
            "front_initial_speed_mps": initial_speed,
            "target_speed_mps": target_speed,
            "brake_intensity": brake_intensity,
            "trigger_frame": concrete_trigger_frame,
            "trigger_seconds": as_float(
                plan_primitive.get("trigger_seconds"),
                as_float(
                    library_primitive.get("trigger_seconds"),
                    round(float(concrete_trigger_frame) * float(source_timestep or 0.05), 3),
                ),
            ),
            "direction": {
                "frame": "ego_local",
                "longitudinal_m": longitudinal_m,
                "lateral_m": lateral_m,
                "heading_delta_deg": as_float(plan_direction.get("heading_delta_deg", plan_primitive.get("heading_delta_deg")), 0.0),
            },
            "acceptance_checks": library_primitive.get("acceptance_checks") or [
                "front_vehicle_speed_drop",
                "front_vehicle_initially_ahead",
            ],
        }
        return {key: value for key, value in concrete.items() if value is not None}

    concrete = dict(library_primitive)
    concrete.update({key: value for key, value in plan_primitive.items() if value is not None})
    concrete.update({key: value for key, value in legacy_action.items() if key not in concrete and value is not None})
    if primitive_id:
        concrete["id"] = primitive_id
    concrete.setdefault("trigger_frame", int(trigger_frame))
    return concrete


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
    elif scenario_type == "ego_action_risk":
        criteria.setdefault("ego_speed_near_trigger_min", 1.0)
        criteria.setdefault("distance_to_hazard_decrease_min", 0.5)
        criteria.setdefault("min_distance_to_hazard_m_max", 4.0)
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
    elif scenario_type == "ego_action_risk":
        fields.extend(["primary_actor_position", "distance_to_ego_m", "relative_longitudinal_m"])
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
    chain_risk_type = risk_type_by_id(chain.get("risk_type_id")) or {}
    risk_family = plan_output.get("risk_family") or chain.get("risk_family") or chain_risk_type.get("family")
    risk_type_id = plan_output.get("risk_type_id") or chain.get("risk_type_id")
    action_primitive_id = (
        plan_output.get("primary_action_primitive_id")
        or chain.get("primary_trigger_action_id")
        or chain_risk_type.get("primary_action_primitive_id")
    )
    action_primitive = action_primitive_by_id(action_primitive_id) or {}
    scenario_type = plan_output.get("scenario_type") or chain_risk_type.get("legacy_scenario_type")
    if not scenario_type:
        raise ValueError("L4 PlanAgent output missing scenario_type.")

    primary_actor = primary_from_chain_or_plan(chain, plan_output, l0_state or {})
    trigger_frame = int(local_trigger_frame or 20)
    action_primitive = concrete_action_primitive(
        scenario_type,
        action_primitive_id,
        action_primitive,
        plan_output,
        primary_actor,
        trigger_frame,
        pre_trigger_seconds=pre_trigger_seconds,
        source_timestep=source_timestep,
    )
    success_criteria = default_success_criteria(scenario_type, plan_output)
    for key, value in (chain_risk_type.get("acceptance") or {}).items():
        success_criteria.setdefault(key, value)

    config = {
        "level": "L4",
        "source_l3_chain_id": chain.get("id"),
        "source_l2_id": chain.get("parent_l2_id"),
        "source_l0_state_file": os.path.abspath(l0_json_path) if l0_json_path else None,
        "risk_family": risk_family,
        "risk_type_id": risk_type_id,
        "primary_action_primitive_id": action_primitive_id,
        "action_primitive": action_primitive,
        "action_primitives": chain.get("action_primitives") or [],
        "participant_actions": chain.get("participant_actions") or [],
        "scenario_type": scenario_type,
        "trigger_frame": action_primitive.get("trigger_frame", trigger_frame),
        "chain_description": chain.get("chain_description"),
        "direct_physical_outcome": chain.get("direct_physical_outcome"),
        "expected_visual_result": plan_output.get("expected_visual_result"),
        "translation_reason": plan_output.get("translation_reason"),
        "primary_actor": primary_actor,
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
