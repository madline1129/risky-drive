#!/usr/bin/env python3
"""Minimal L4 helpers for the OpenCode + Scenic backend."""

import json
import os
import random
import shutil

try:
    from deepseek_client import (
        DEFAULT_DEEPSEEK_MODEL,
        DEFAULT_DEEPSEEK_URL,
        DEFAULT_API_KEY_ENV,
        chat_json,
        get_api_key,
        parse_json_response,
    )
    from risk_library import action_primitive_by_id, risk_type_by_id
except ImportError:
    from .deepseek_client import (
        DEFAULT_DEEPSEEK_MODEL,
        DEFAULT_DEEPSEEK_URL,
        DEFAULT_API_KEY_ENV,
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
- 你必须严格按 active_action_primitive_skill 填参数，尤其是 spawn_semantics、required_action_fields、parameter_rules。
- 生成风险场景时，主风险动作必须采用激进参数：更早触发、更快横向/纵向侵入、更深地进入 ego path；不要用慢速漂移或弱动作来表达风险。front_vehicle_brake_after_trigger 默认是前车急刹；只有当 L0 显示 ego 速度较低、单纯急刹风险不明显时，才可选择 front_action_variant=reverse_toward_ego 并填 reverse_speed_mps。
- 只有当 L0 actor 满足 active_action_primitive_skill.spawn_semantics 时，才能把 primary_object.source 写成 l0_actor。
- 如果没有 L0 actor 满足出生位置语义，必须使用 primary_object.source="generated_object"，并直接给出 kind、type_id、relative_position、relative_longitudinal_m、relative_lateral_m；不要硬选一个不合语义的 L0 actor。
- action_primitive.direction.longitudinal_m / lateral_m 必须和 primary_object 的 relative_longitudinal_m / relative_lateral_m 一致。
- 对 vru_cross_lateral_into_path 这类“进入自车前方车道”的动作，主对象不能在自车后方；relative_longitudinal_m 必须为正且满足 skill 范围。
- 对 weather_shift_to_night / weather_visibility_change，不要选择或生成物理 primary actor；primary_object 使用 kind="environment" 的占位对象即可；天气扰动从 clear_night、hard_rain_night、hard_rain_sunset、dust_storm 中选择一个。
- scenario_type 只能是：front_vehicle_brake, cargo_drop, vulnerable_actor_intrusion, road_obstacle_intrusion, side_vehicle_intrusion, ego_action_risk, weather_visibility_change。
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
    "kind": "vehicle/pedestrian/obstacle/payload/environment",
    "role": "front_vehicle",
    "type_id": "vehicle.nissan.micra",
    "relative_position": "front/front-left/front-right/left/right",
    "relative_longitudinal_m": 14.0,
    "relative_lateral_m": 0.0,
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
    "front_action_variant": "hard_brake 或 reverse_toward_ego（仅 ego 低速时）",
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


def action_primitive_skills_path():
    return os.path.join(repo_root_from_this_file(), "carla_smoke", "risk_library", "action_primitive_skills.json")


def load_action_primitive_skills():
    return read_json(action_primitive_skills_path())


def action_primitive_skill_by_id(action_primitive_id, skills_library=None):
    if not action_primitive_id:
        return {}
    skills_library = skills_library or load_action_primitive_skills()
    for item in skills_library.get("skills", []):
        if isinstance(item, dict) and item.get("id") == action_primitive_id:
            return item
    return {}


def action_primitive_skills_for_chain(chain, primary_primitive_id=None):
    skills_library = load_action_primitive_skills()
    wanted_ids = []
    if primary_primitive_id:
        wanted_ids.append(primary_primitive_id)
    for item in chain.get("action_primitives") or []:
        if not isinstance(item, dict):
            continue
        action_id = item.get("action_primitive_id") or item.get("action_id") or item.get("id")
        if action_id:
            wanted_ids.append(action_id)
    skills = []
    seen = set()
    for action_id in wanted_ids:
        if action_id in seen:
            continue
        seen.add(action_id)
        skill = action_primitive_skill_by_id(action_id, skills_library)
        if skill:
            skills.append(skill)
    return skills


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
    if model == "glm-5.1":
        return "aihubmix/glm-5.1"
    if model.startswith("glm-"):
        return f"aihubmix/{model}"
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


def max_float(value, minimum):
    numeric = as_float(value)
    if numeric is None:
        return minimum
    return max(numeric, minimum)


def signed_intrusion_target(relative_lateral_m, target_lateral_m=None, target_abs=0.5):
    rel = as_float(relative_lateral_m)
    target = as_float(target_lateral_m)
    if rel is None:
        return target if target is not None else 0.0
    if abs(rel) <= 0.05:
        return 0.0
    sign = -1.0 if rel < 0 else 1.0
    if target is not None and target * sign >= 0 and abs(target) <= target_abs:
        return target
    return round(sign * target_abs, 3)


WEATHER_VISIBILITY_PROFILES = [
    {
        "profile_id": "clear_night",
        "description": "晴天夜晚",
        "sun_altitude_angle": -35.0,
        "cloudiness": 5.0,
        "precipitation": 0.0,
        "precipitation_deposits": 0.0,
        "wetness": 0.0,
        "fog_density": 0.0,
        "wind_intensity": 10.0,
    },
    {
        "profile_id": "hard_rain_night",
        "description": "大雨夜晚",
        "sun_altitude_angle": -35.0,
        "cloudiness": 100.0,
        "precipitation": 100.0,
        "precipitation_deposits": 90.0,
        "wetness": 100.0,
        "fog_density": 20.0,
        "wind_intensity": 80.0,
    },
    {
        "profile_id": "hard_rain_sunset",
        "description": "大雨日落",
        "sun_altitude_angle": 3.0,
        "sun_azimuth_angle": 20.0,
        "cloudiness": 100.0,
        "precipitation": 100.0,
        "precipitation_deposits": 90.0,
        "wetness": 100.0,
        "fog_density": 15.0,
        "wind_intensity": 70.0,
    },
    {
        "profile_id": "dust_storm",
        "description": "沙尘暴",
        "sun_altitude_angle": 15.0,
        "cloudiness": 80.0,
        "precipitation": 0.0,
        "precipitation_deposits": 0.0,
        "wetness": 0.0,
        "fog_density": 55.0,
        "fog_distance": 20.0,
        "wind_intensity": 100.0,
        "dust_storm": 100.0,
    },
]


def random_weather_visibility_profile():
    return dict(random.choice(WEATHER_VISIBILITY_PROFILES))


def aggressivize_action_primitive(concrete, scenario_type, primitive_id, primary_actor):
    """Raise primary risk action intensity while preserving actor geometry."""
    if not isinstance(concrete, dict):
        return concrete
    primitive_id = primitive_id or concrete.get("id")
    rel = primary_actor.get("relative_to_ego") if isinstance(primary_actor, dict) else {}
    if not isinstance(rel, dict):
        rel = {}
    rel_lat = as_float(rel.get("lateral_m"), as_float(primary_actor.get("relative_lateral_m") if isinstance(primary_actor, dict) else None))

    if scenario_type == "front_vehicle_brake":
        variant = concrete.get("front_action_variant")
        if variant == "reverse_toward_ego" or concrete.get("reverse_speed_mps") is not None:
            concrete["front_action_variant"] = "reverse_toward_ego"
            concrete["front_initial_speed_mps"] = max_float(concrete.get("front_initial_speed_mps"), 4.0)
            concrete["reverse_speed_mps"] = max_float(concrete.get("reverse_speed_mps"), 6.0)
            concrete["target_speed_mps"] = None
            concrete["brake_intensity"] = 0.0
            concrete["stop_condition"] = "reverse_toward_ego_or_timeout"
            velocity = concrete.get("velocity_vector") if isinstance(concrete.get("velocity_vector"), dict) else {}
            velocity.update({"longitudinal_direction": "reverse_toward_ego", "speed_policy": "sudden_high_speed_reverse"})
            concrete["velocity_vector"] = velocity
        else:
            concrete["front_action_variant"] = "hard_brake"
            concrete["front_initial_speed_mps"] = max_float(concrete.get("front_initial_speed_mps"), 8.0)
            concrete["target_speed_mps"] = 0.0
            concrete["brake_intensity"] = 1.0
            concrete["stop_condition"] = "hard_stop_or_timeout"
        concrete["aggressiveness"] = "high"
    elif primitive_id in {"vru_cross_lateral_into_path", "vru_emerge_from_occlusion_into_path"}:
        speed_key = "crossing_speed_mps" if primitive_id == "vru_cross_lateral_into_path" else "emerge_speed_mps"
        concrete[speed_key] = max_float(concrete.get(speed_key), 2.8)
        concrete["target_relative_lateral_m"] = 0.0
        concrete["stop_condition"] = "reached_ego_lane_or_timeout"
        concrete["aggressiveness"] = "high"
    elif primitive_id == "vru_move_longitudinal_in_path":
        concrete["speed_mps"] = max_float(concrete.get("speed_mps"), 2.8)
        concrete["stop_condition"] = "min_distance_or_timeout"
        concrete["aggressiveness"] = "high"
    elif primitive_id in {"side_vehicle_cut_in_to_ego_lane", "side_vehicle_drift_toward_ego_lane"}:
        minimum_lateral_speed = 1.5 if primitive_id == "side_vehicle_cut_in_to_ego_lane" else 1.2
        concrete["lateral_speed_mps"] = max_float(concrete.get("lateral_speed_mps"), minimum_lateral_speed)
        concrete["target_relative_lateral_m"] = signed_intrusion_target(rel_lat, concrete.get("target_relative_lateral_m"), target_abs=0.5)
        concrete["stop_condition"] = "reached_target_lateral_or_timeout"
        concrete["aggressiveness"] = "high"
        velocity = concrete.get("velocity_vector") if isinstance(concrete.get("velocity_vector"), dict) else {}
        velocity.update({"lateral_direction": "toward_ego_lane", "lateral_speed_policy": "aggressive_intrusion"})
        concrete["velocity_vector"] = velocity
    elif primitive_id == "cargo_drop_or_slide_into_path":
        concrete["speed_mps"] = max_float(concrete.get("speed_mps"), 3.0)
        concrete["stop_condition"] = "path_blocked_or_timeout"
        concrete["aggressiveness"] = "high"
    elif primitive_id in {"ego_continue_without_braking", "ego_late_or_insufficient_braking"}:
        velocity = concrete.get("velocity_vector") if isinstance(concrete.get("velocity_vector"), dict) else {}
        velocity.update({"speed_policy": "maintain_or_accelerate_toward_hazard", "longitudinal_direction": "forward"})
        concrete["velocity_vector"] = velocity
        concrete["brake_intensity_max"] = 0.0 if primitive_id == "ego_continue_without_braking" else 0.25
        concrete["aggressiveness"] = "high"
    elif primitive_id == "weather_shift_to_night":
        concrete["weather_options"] = [dict(item) for item in WEATHER_VISIBILITY_PROFILES]
        concrete["weather"] = random_weather_visibility_profile()
        concrete["aggressiveness"] = "high"

    return concrete


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


def build_l4_plan_agent_prompt(chain, l0_state, feedback=None):
    risk_type = risk_type_by_id(chain.get("risk_type_id")) or {}
    primitive_id = chain.get("primary_trigger_action_id") or risk_type.get("primary_action_primitive_id")
    action_primitive = action_primitive_by_id(primitive_id) or {}
    action_skills = action_primitive_skills_for_chain(chain, primitive_id)
    active_skill = action_primitive_skill_by_id(primitive_id) or {}
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
        "active_action_primitive_skill": active_skill,
        "available_action_primitive_skills": action_skills,
    }
    if feedback:
        payload["previous_plan_feedback"] = feedback
    return PLAN_AGENT_PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def run_l4_plan_agent(args, chain, l0_state, feedback=None):
    prompt = build_l4_plan_agent_prompt(chain, l0_state, feedback=feedback)
    api_key = get_api_key(args.api_key_env, args.env_file, getattr(args, "api_key", None))
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
        reverse_speed = as_float(
            plan_primitive.get("reverse_speed_mps"),
            as_float(legacy_action.get("reverse_speed_mps"), as_float(library_primitive.get("reverse_speed_mps"))),
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
            "front_action_variant": plan_primitive.get("front_action_variant")
            or legacy_action.get("front_action_variant")
            or library_primitive.get("front_action_variant"),
            "front_initial_speed_mps": initial_speed,
            "target_speed_mps": target_speed,
            "reverse_speed_mps": reverse_speed,
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
        concrete = aggressivize_action_primitive(concrete, scenario_type, primitive_id, primary_actor)
        return {key: value for key, value in concrete.items() if value is not None}

    concrete = dict(library_primitive)
    concrete.update({key: value for key, value in plan_primitive.items() if value is not None})
    concrete.update({key: value for key, value in legacy_action.items() if key not in concrete and value is not None})
    if primitive_id:
        concrete["id"] = primitive_id
    concrete.setdefault("trigger_frame", int(trigger_frame))
    concrete = aggressivize_action_primitive(concrete, scenario_type, primitive_id, primary_actor)
    return concrete


def default_success_criteria(scenario_type, plan_output):
    criteria = dict(plan_output.get("success_criteria") or {})
    if scenario_type == "front_vehicle_brake":
        criteria.setdefault("front_actor_speed_drop_mps_min", 2.0)
        criteria.setdefault("front_distance_change_m_min", 1.5)
    elif scenario_type == "vulnerable_actor_intrusion":
        criteria.setdefault("actor_motion_m_min", 2.0)
        criteria.setdefault("min_distance_to_ego_m_max", 6.0)
        criteria.setdefault("min_abs_relative_lateral_m_max", 0.8)
        criteria.setdefault("relative_lateral_crosses_zero", True)
    elif scenario_type == "side_vehicle_intrusion":
        criteria.setdefault("relative_lateral_delta_m_min", 1.5)
        criteria.setdefault("min_abs_relative_lateral_m_max", 0.8)
        criteria.setdefault("min_distance_to_ego_m_max", 6.0)
    elif scenario_type == "cargo_drop":
        criteria.setdefault("payload_count_min", 1)
        criteria.setdefault("payload_motion_m_min", 1.5)
    elif scenario_type == "road_obstacle_intrusion":
        criteria.setdefault("obstacle_count_min", 1)
        criteria.setdefault("min_obstacle_distance_to_ego_m_max", 8.0)
    elif scenario_type == "ego_action_risk":
        criteria.setdefault("ego_speed_near_trigger_min", 2.0)
        criteria.setdefault("distance_to_hazard_decrease_min", 1.0)
        criteria.setdefault("min_distance_to_hazard_m_max", 3.0)
    elif scenario_type == "weather_visibility_change":
        criteria.setdefault("weather_profile_in_options", ["clear_night", "hard_rain_night", "hard_rain_sunset", "dust_storm"])
        criteria.setdefault("visibility_degraded", True)
    criteria.setdefault("must_match_scenario_type", scenario_type)
    criteria.setdefault("must_use_primary_actor_from_config", scenario_type != "weather_visibility_change")
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
    elif scenario_type == "weather_visibility_change":
        fields.extend(["weather"])
    return {
        "trace_file": "event_trace.json",
        "required_top_level_fields": ["scenario_type", "trigger_frame", "frames", "event_applied"],
        "required_frame_fields": fields,
        "numeric_acceptance": success_criteria,
    }


def normalized_actor_kind(actor):
    kind = str((actor or {}).get("kind") or "").lower()
    type_id = str((actor or {}).get("type_id") or "").lower()
    if kind:
        return kind
    if type_id.startswith("walker."):
        return "pedestrian"
    if type_id.startswith("vehicle."):
        return "vehicle"
    if type_id.startswith("static."):
        return "static"
    return ""


def kind_allowed(actual_kind, allowed_kinds):
    if not allowed_kinds:
        return True
    actual = str(actual_kind or "").lower()
    for item in allowed_kinds:
        item = str(item).lower()
        if actual == item:
            return True
        if item == "walker" and actual == "pedestrian":
            return True
        if item == "pedestrian" and actual == "walker":
            return True
        if item == "vehicle" and actual in {"car", "truck", "bus", "motorcycle"}:
            return True
        if item == "static" and actual in {"obstacle", "prop"}:
            return True
        if item == "obstacle" and actual in {"static", "prop"}:
            return True
    return False


def nested_value(data, path):
    current = data
    for part in str(path).split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def actor_relative_longitudinal(primary_actor, action_primitive):
    return as_float(
        primary_actor.get("relative_longitudinal_m") if isinstance(primary_actor, dict) else None,
        as_float(nested_value(action_primitive, "direction.longitudinal_m")),
    )


def actor_relative_lateral(primary_actor, action_primitive):
    return as_float(
        primary_actor.get("relative_lateral_m") if isinstance(primary_actor, dict) else None,
        as_float(nested_value(action_primitive, "direction.lateral_m")),
    )


def check_numeric_range(checks, name, value, spec):
    if not isinstance(spec, dict) or not spec:
        return
    passed = True
    reasons = []
    minimum = as_float(spec.get("min"))
    maximum = as_float(spec.get("max"))
    if value is None:
        passed = False
        reasons.append("missing")
    else:
        if minimum is not None and value < minimum:
            passed = False
            reasons.append(f"{value:.3f} < min {minimum:.3f}")
        if maximum is not None and value > maximum:
            passed = False
            reasons.append(f"{value:.3f} > max {maximum:.3f}")
    checks.append(
        {
            "name": name,
            "passed": passed,
            "target": spec,
            "actual": value,
            "reason": "; ".join(reasons) if reasons else "ok",
        }
    )


def validate_plan_spawn_parameters(config):
    primitive_id = config.get("primary_action_primitive_id")
    skill = action_primitive_skill_by_id(primitive_id)
    primary_actor = config.get("primary_actor") or {}
    action_primitive = config.get("action_primitive") or {}
    spawn_semantics = skill.get("spawn_semantics") if isinstance(skill, dict) else {}
    checks = []

    actual_kind = normalized_actor_kind(primary_actor)
    allowed_kinds = skill.get("primary_actor_kinds") or []
    checks.append(
        {
            "name": "primary_actor_kind",
            "passed": kind_allowed(actual_kind, allowed_kinds),
            "target": allowed_kinds,
            "actual": actual_kind,
            "reason": "ok" if kind_allowed(actual_kind, allowed_kinds) else "primary actor kind does not match action primitive skill",
        }
    )

    rel_long = actor_relative_longitudinal(primary_actor, action_primitive)
    rel_lat = actor_relative_lateral(primary_actor, action_primitive)
    if isinstance(spawn_semantics, dict):
        check_numeric_range(checks, "relative_longitudinal_m", rel_long, spawn_semantics.get("relative_longitudinal_m"))
        check_numeric_range(
            checks,
            "abs_relative_lateral_m",
            abs(rel_lat) if rel_lat is not None else None,
            spawn_semantics.get("abs_relative_lateral_m"),
        )
        if spawn_semantics.get("must_not_be_behind_ego"):
            checks.append(
                {
                    "name": "must_not_be_behind_ego",
                    "passed": rel_long is not None and rel_long >= 0.0,
                    "target": "relative_longitudinal_m >= 0",
                    "actual": rel_long,
                    "reason": "ok" if rel_long is not None and rel_long >= 0.0 else "primary actor is behind ego",
                }
            )
        if spawn_semantics.get("must_be_side_actor"):
            checks.append(
                {
                    "name": "must_be_side_actor",
                    "passed": rel_lat is not None and abs(rel_lat) >= 1.5,
                    "target": "abs(relative_lateral_m) >= 1.5",
                    "actual": rel_lat,
                    "reason": "ok" if rel_lat is not None and abs(rel_lat) >= 1.5 else "primary actor is not on a side lane",
                }
            )

    direction_long = as_float(nested_value(action_primitive, "direction.longitudinal_m"))
    direction_lat = as_float(nested_value(action_primitive, "direction.lateral_m"))
    if rel_long is not None and direction_long is not None:
        checks.append(
            {
                "name": "direction_longitudinal_matches_primary_actor",
                "passed": abs(rel_long - direction_long) <= 0.5,
                "target": rel_long,
                "actual": direction_long,
                "reason": "ok" if abs(rel_long - direction_long) <= 0.5 else "direction.longitudinal_m does not match primary actor",
            }
        )
    if rel_lat is not None and direction_lat is not None:
        checks.append(
            {
                "name": "direction_lateral_matches_primary_actor",
                "passed": abs(rel_lat - direction_lat) <= 0.5,
                "target": rel_lat,
                "actual": direction_lat,
                "reason": "ok" if abs(rel_lat - direction_lat) <= 0.5 else "direction.lateral_m does not match primary actor",
            }
        )

    for field in skill.get("required_action_fields") or []:
        value = nested_value(action_primitive, field)
        checks.append(
            {
                "name": f"required_action_field:{field}",
                "passed": value is not None,
                "target": "present",
                "actual": value,
                "reason": "ok" if value is not None else "missing required action primitive field",
            }
        )

    passed = all(check.get("passed") for check in checks)
    failed = [check for check in checks if not check.get("passed")]
    return {
        "kind": "plan_spawn_parameter_check",
        "passed": passed,
        "action_primitive_id": primitive_id,
        "skill": skill,
        "primary_actor": primary_actor,
        "action_primitive": action_primitive,
        "checks": checks,
        "failed_checks": failed,
        "reason": "ok" if passed else "; ".join(f"{item['name']}: {item.get('reason')}" for item in failed),
    }


def plan_feedback_from_spawn_check(check):
    skill = check.get("skill") or {}
    return {
        "kind": "plan_spawn_parameter_feedback",
        "message": "上一次 L4 PlanAgent 输出的动作原语出生地/参数语义检查失败。必须修正 primary_object 和 action_primitive 后重新输出完整 L4Plan JSON。",
        "failed_checks": check.get("failed_checks") or [],
        "active_action_primitive_skill": skill,
        "repair_rules": [
            "Do not keep an l0_actor if it violates spawn_semantics.",
            "If no L0 actor satisfies the skill, use primary_object.source='generated_object' and fill relative_longitudinal_m/relative_lateral_m from fallback_when_no_l0_actor_matches.",
            "Make action_primitive.direction.longitudinal_m/lateral_m match primary_object relative_longitudinal_m/relative_lateral_m.",
            "For front/crossing hazards, do not place the primary actor behind ego.",
        ],
    }


def config_from_plan_output(
    chain,
    plan_output,
    raw_response,
    l0_state,
    l0_json_path,
    l4_frames,
    local_trigger_frame,
    pre_trigger_seconds,
    source_timestep,
):
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

    return {
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

    max_feedback_attempts = max(0, int(getattr(plan_agent_args, "plan_feedback_attempts", 1) or 0))
    feedback = None
    attempts = []
    last_config = None
    last_check = None
    for attempt_index in range(max_feedback_attempts + 1):
        plan_output, raw_response = run_l4_plan_agent(plan_agent_args, chain, l0_state or {}, feedback=feedback)
        config = config_from_plan_output(
            chain,
            plan_output,
            raw_response,
            l0_state or {},
            l0_json_path,
            l4_frames,
            local_trigger_frame,
            pre_trigger_seconds,
            source_timestep,
        )
        check = validate_plan_spawn_parameters(config)
        attempts.append(
            {
                "attempt": attempt_index + 1,
                "raw_response": raw_response,
                "output": plan_output,
                "spawn_parameter_check": check,
            }
        )
        last_config = config
        last_check = check
        if check.get("passed"):
            config["spawn_parameter_check"] = check
            config["_l4_plan_agent_raw"] = {
                "model": getattr(plan_agent_args, "plan_model", None),
                "attempt_count": len(attempts),
                "raw_response": raw_response,
                "output": plan_output,
                "attempts": attempts,
            }
            return config
        if attempt_index < max_feedback_attempts:
            feedback = plan_feedback_from_spawn_check(check)

    if last_config is not None:
        last_config["spawn_parameter_check"] = last_check
        last_config["_l4_plan_agent_raw"] = {
            "model": getattr(plan_agent_args, "plan_model", None),
            "attempt_count": len(attempts),
            "attempts": attempts,
        }
    raise ValueError(f"L4 PlanAgent spawn parameter check failed after feedback: {last_check.get('reason') if last_check else 'unknown'}")
