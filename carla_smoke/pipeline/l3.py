#!/usr/bin/env python3
"""DeepSeek subagent for L3 initial accident chains from L2 triggers."""

import argparse
import json
import os
import sys

from deepseek_client import DEFAULT_DEEPSEEK_MODEL, DEFAULT_DEEPSEEK_URL, DeepSeekError, chat_json, get_api_key, parse_json_response


PROMPT_TEMPLATE = """你是自动驾驶风险推演系统中的 L3 子智能体。
输入是 L2 触发事件假设 JSON，以及可选的 L0 场景快照。

任务：
L3 初始事故链：
- 对每个 L2 触发事件，构思它导致的直接物理后果。
- L3 不是最终事故，也不是二次事故；只描述触发后第一段物理演化。
- L3 只写自然语言事故链和涉及物体清单，不生成 CARLA 代码，不需要生成 carla_plan。
- 如果 L2/L1 已经传入 primary_perturbation_object / perturbation_target，必须继承，不要换主风险物体。
- 对于事故链涉及多个物体的情况，用 chain_participants 列清楚：谁是主扰动物体，谁是 ego，谁只是背景/遮挡/受影响对象。
- 背景对象必须标注 must_not_drive_primary_event=true，避免后续 L4/code agent 把背景对象当成主风险。

例子：
- L2: 绳索断裂
- L3: 金属管失去约束，从货车后部向自车方向滑落/飞出，进入自车车道。

请只输出一个 JSON 对象，不要 Markdown，不要解释性前后缀。格式必须是：
{
  "level": "L3",
  "name": "初始事故链",
  "description": "触发事件导致的直接物理后果",
  "source_l2_file": "",
  "initial_accident_chains": [
    {
      "level": "L3",
      "id": "L3-1a",
      "parent_l2_id": "L2-1a",
      "parent_l2_trigger": "绳索断裂",
      "chain_description": "金属管失去约束并从货车后部向自车方向飞出",
      "direct_physical_outcome": "金属管进入自车前方车道，形成紧急避让/制动障碍",
      "primary_perturbation_object": {
        "source": "l0_actor/generated_object/l0_ego",
        "actor_id": 123,
        "kind": "vehicle/pedestrian/payload/obstacle/ego",
        "role": "front_vehicle/payload/vulnerable_actor/side_vehicle/road_obstacle/ego",
        "must_drive_primary_event": true,
        "selection_reason": "为什么它是主扰动物体"
      },
      "chain_participants": [
        {"source": "l0_ego", "actor_id": "ego", "kind": "ego", "role": "affected_actor", "must_drive_primary_event": false},
        {"source": "l0_actor", "actor_id": 123, "kind": "vehicle", "role": "primary_actor", "must_drive_primary_event": true},
        {"source": "l0_actor", "actor_id": 456, "kind": "pedestrian", "role": "background_or_occluder", "must_not_drive_primary_event": true}
      ]
    }
  ]
}

硬性要求：
- initial_accident_chains 最多 10 项，优先覆盖输入中的前 10 个 L2。
- 每项必须包含 chain_description、direct_physical_outcome、primary_perturbation_object、chain_participants。
- 不要输出 carla_plan；L4 会单独把自然语言事故链翻译成 CARLA plan。
- 不要为了“可视化明显”引入无关物体，例如非货物链条不要加入 metal_pipe。
- chain_participants 必须区分 primary_actor 和 background/occluder/affected_actor。
"""


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def trigger_events_from_data(data):
    if isinstance(data, dict) and isinstance(data.get("trigger_event_hypotheses"), list):
        return data["trigger_event_hypotheses"]
    if isinstance(data, list):
        return data
    return []


def cargo_drop_plan(trigger_frame=45, object_type="metal_pipe"):
    return {
        "scenario_type": "cargo_drop",
        "target_actor": "front_truck",
        "object_type": object_type,
        "object_count": 5,
        "trigger_frame": trigger_frame,
        "spawn_relative_to": "front_truck",
        "initial_position": {"x": -3.2, "y": 0.0, "z": 2.4},
        "motion": {
            "mode": "scripted_projectile",
            "direction": "toward_ego",
            "back_speed_mps": 8.0,
            "lateral_drift_mps": 0.2,
            "gravity": True,
        },
        "expected_visual_result": "货物/障碍物从前方车辆后部进入自车前方区域",
        "actor_motion_plan": {
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
        },
    }


def front_vehicle_brake_plan(trigger_frame=45):
    return {
        "scenario_type": "front_vehicle_brake",
        "target_actor": "front_vehicle",
        "trigger_frame": trigger_frame,
        "brake_intensity": 1.0,
        "deceleration_mps2": 6.0,
        "target_speed_mps": 0.0,
        "expected_visual_result": "前车在自车前方突然减速或接近停止，自车前向距离快速压缩",
        "actor_motion_plan": {
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
                "brake_intensity": 1.0,
                "target_speed_mps": 0.0,
            },
            "primary_actor": {"role": "front_actor", "behavior": "brake_after_trigger"},
            "background_actors": {"behavior": "preserve_l0_or_ignore_if_not_relevant"},
        },
    }


def vulnerable_actor_intrusion_plan(trigger_frame=45, actor_type="walker"):
    return {
        "scenario_type": "vulnerable_actor_intrusion",
        "actor_type": actor_type,
        "trigger_frame": trigger_frame,
        "spawn_relative_to": "ego_lane_right",
        "start_position": {"x": 18.0, "y": 4.0, "z": 0.2},
        "crossing_direction": "right_to_left",
        "speed_mps": 2.2 if actor_type == "walker" else 4.0,
        "expected_visual_result": "弱势交通参与者从侧前方侵入自车行驶空间",
        "actor_motion_plan": {
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
                "role": actor_type,
                "behavior": "cross_ego_lane_after_trigger",
                "trigger_frame": trigger_frame,
                "start": "from_occluded_side_near_front_actor",
                "end": "across_ego_lane_centerline",
                "speed_mps": 2.2 if actor_type == "walker" else 4.0,
                "must_enter_ego_lane": True,
            },
            "background_actors": {"behavior": "preserve_l0_or_ignore_if_not_relevant"},
        },
    }


def road_obstacle_intrusion_plan(trigger_frame=45):
    return {
        "scenario_type": "road_obstacle_intrusion",
        "object_type": "road_obstacle",
        "trigger_frame": trigger_frame,
        "spawn_relative_to": "front_of_ego",
        "initial_position": {"x": 14.0, "y": 0.0, "z": 0.4},
        "motion": {
            "mode": "static_or_slow_intrusion",
            "direction": "into_ego_lane",
            "lateral_drift_mps": 0.5,
            "gravity": False,
        },
        "expected_visual_result": "障碍物出现在自车前方车道内",
        "actor_motion_plan": {
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
        },
    }


def sanitize_carla_plan(plan):
    if not isinstance(plan, dict):
        plan = {}
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
        sanitized = front_vehicle_brake_plan(trigger_frame)
        for key in ["target_actor", "brake_intensity", "deceleration_mps2", "target_speed_mps", "expected_visual_result", "actor_motion_plan"]:
            if key in plan:
                sanitized[key] = plan[key]
        return sanitized

    if scenario_type == "vulnerable_actor_intrusion":
        sanitized = vulnerable_actor_intrusion_plan(trigger_frame, plan.get("actor_type", "walker"))
        for key in ["spawn_relative_to", "start_position", "crossing_direction", "speed_mps", "expected_visual_result", "actor_motion_plan"]:
            if key in plan:
                sanitized[key] = plan[key]
        return sanitized

    if scenario_type == "cargo_drop":
        sanitized = cargo_drop_plan(trigger_frame, plan.get("object_type", "metal_pipe"))
        for key in ["target_actor", "object_type", "object_count", "spawn_relative_to", "initial_position", "motion", "expected_visual_result", "actor_motion_plan"]:
            if key in plan:
                sanitized[key] = plan[key]
        return sanitized

    sanitized = road_obstacle_intrusion_plan(trigger_frame)
    for key in ["object_type", "spawn_relative_to", "initial_position", "motion", "expected_visual_result", "actor_motion_plan"]:
        if key in plan:
            sanitized[key] = plan[key]
    return sanitized


def fallback_plan_for_event(event, index):
    trigger = event.get("trigger_name", "待确认触发事件") if isinstance(event, dict) else str(event)
    event_id = event.get("id", f"L2-{index}") if isinstance(event, dict) else f"L2-{index}"
    text = f"{trigger} {event.get('parent_l1_name', '') if isinstance(event, dict) else ''}"

    if any(keyword in text for keyword in ["绳索", "货物", "固定", "滑移", "掉落"]):
        scenario_type = "cargo_drop"
        description = "货物失去约束后从前方货车区域向自车方向滑落/飞出"
        outcome = "掉落物进入自车前方车道，迫使自车紧急制动或避让"
        carla_plan = cargo_drop_plan()
    elif any(keyword in text for keyword in ["急刹", "减速", "停滞", "刹车"]):
        scenario_type = "front_vehicle_brake"
        description = "前车突然减速或停止，自车跟车距离被快速压缩"
        outcome = "自车前向安全距离不足，形成追尾风险"
        carla_plan = front_vehicle_brake_plan()
    elif any(keyword in text for keyword in ["骑行", "行人", "滑倒", "转向"]):
        scenario_type = "vulnerable_actor_intrusion"
        description = "弱势交通参与者轨迹突变并侵入自车行驶空间"
        outcome = "自车需要紧急制动或侧向避让"
        carla_plan = vulnerable_actor_intrusion_plan(actor_type="walker")
    else:
        scenario_type = "road_obstacle_intrusion"
        description = "前方对象状态突变，近距离障碍物进入自车行驶空间"
        outcome = "自车前方出现紧急障碍"
        carla_plan = road_obstacle_intrusion_plan()

    carla_plan["scenario_type"] = scenario_type

    return {
        "level": "L3",
        "id": f"L3-{index}",
        "parent_l2_id": event_id,
        "parent_l2_trigger": trigger,
        "chain_description": description,
        "direct_physical_outcome": outcome,
        "carla_plan": carla_plan,
    }


def build_prompt(l2_data, l0_data):
    context = {"l0_state_snapshot": l0_data, "l2_trigger_event_hypotheses": l2_data}
    return PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(context, ensure_ascii=False, indent=2)


def normalize_output(parsed, l2_data, source_l2_file):
    chains = parsed.get("initial_accident_chains", []) if isinstance(parsed, dict) else []
    normalized = []
    if isinstance(chains, list):
        for idx, chain in enumerate(chains[:10], start=1):
            if not isinstance(chain, dict):
                chain = {"chain_description": str(chain)}
            chain.setdefault("level", "L3")
            chain.setdefault("id", f"L3-{idx}")
            if isinstance(chain.get("carla_plan"), dict):
                chain["carla_plan"] = sanitize_carla_plan(chain.get("carla_plan"))
            normalized.append(chain)

    events = trigger_events_from_data(l2_data)[:10]
    for idx, event in enumerate(events, start=1):
        if len(normalized) >= idx:
            continue
        normalized.append(fallback_plan_for_event(event, idx))

    while len(normalized) < 1:
        normalized.append(fallback_plan_for_event({}, 1))

    return {
        "level": "L3",
        "name": "初始事故链",
        "description": "触发事件导致的直接物理后果",
        "source_l2_file": os.path.abspath(source_l2_file),
        "initial_accident_chains": normalized[:10],
    }


def main():
    parser = argparse.ArgumentParser(description="DeepSeek L3 subagent: initial accident chains from L2 triggers.")
    parser.add_argument("l2_json", help="Path to l2/triggers.json.")
    parser.add_argument("--l0-json", default=None, help="Optional l0/state.json for context.")
    parser.add_argument("--model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output-dir", default="carla_smoke/workdir/manual/l3")
    args = parser.parse_args()

    l2_data = read_json(args.l2_json)
    l0_data = read_json(args.l0_json) if args.l0_json else None
    prompt = build_prompt(l2_data, l0_data)

    print(f"L3 DeepSeek input: {args.l2_json}")
    raw_response = ""
    parsed = None
    try:
        api_key = get_api_key(args.api_key_env, args.env_file)
        raw_response = chat_json(args.url, args.model, api_key, prompt, args.timeout)
        parsed = parse_json_response(raw_response)
    except (DeepSeekError, json.JSONDecodeError) as exc:
        print(f"WARNING: DeepSeek L3 failed; using deterministic fallback chains: {exc}", file=sys.stderr)

    output = normalize_output(parsed, l2_data, args.l2_json)
    chains_path = os.path.join(args.output_dir, "chains.json")
    raw_path = os.path.join(args.output_dir, "deepseek_raw.json")
    write_json(chains_path, output)
    write_json(raw_path, {"raw_response": raw_response})
    print(f"Saved L3 chains: {os.path.abspath(chains_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
