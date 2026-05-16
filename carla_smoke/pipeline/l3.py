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
- 必须把语义事件变成 CARLA 可执行/可近似执行的计划。

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
      "carla_plan": {
        "scenario_type": "cargo_drop",
        "target_actor": "front_truck",
        "object_type": "metal_pipe",
        "trigger_frame": 45,
        "spawn_relative_to": "front_truck",
        "initial_position": {"x": -3.2, "y": 0.0, "z": 2.4},
        "motion": {
          "mode": "scripted_projectile",
          "direction": "toward_ego",
          "back_speed_mps": 8.0,
          "lateral_drift_mps": 0.2,
          "gravity": true
        },
        "expected_visual_result": "掉落物出现在自车前方近距离区域"
      }
    }
  ]
}

硬性要求：
- initial_accident_chains 最多 10 项，优先覆盖输入中的前 10 个 L2。
- 每项必须包含 carla_plan。
- carla_plan 要能被规则代码近似执行，不要输出抽象话。
- 如果 L2 与货物/绳索/固定/掉落有关，优先生成 cargo_drop 计划。
- 如果 L2 与前车急刹/低速停滞有关，生成 front_vehicle_brake 计划。
- 如果 L2 与骑行者/行人有关，生成 vulnerable_actor_intrusion 计划。
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


def fallback_plan_for_event(event, index):
    trigger = event.get("trigger_name", "待确认触发事件") if isinstance(event, dict) else str(event)
    event_id = event.get("id", f"L2-{index}") if isinstance(event, dict) else f"L2-{index}"
    text = f"{trigger} {event.get('parent_l1_name', '') if isinstance(event, dict) else ''}"

    if any(keyword in text for keyword in ["绳索", "货物", "固定", "滑移", "掉落"]):
        scenario_type = "cargo_drop"
        description = "货物失去约束后从前方货车区域向自车方向滑落/飞出"
        outcome = "掉落物进入自车前方车道，迫使自车紧急制动或避让"
        object_type = "metal_pipe"
    elif any(keyword in text for keyword in ["急刹", "减速", "停滞", "刹车"]):
        scenario_type = "front_vehicle_brake"
        description = "前车突然减速或停止，自车跟车距离被快速压缩"
        outcome = "自车前向安全距离不足，形成追尾风险"
        object_type = "front_vehicle"
    elif any(keyword in text for keyword in ["骑行", "行人", "滑倒", "转向"]):
        scenario_type = "vulnerable_actor_intrusion"
        description = "弱势交通参与者轨迹突变并侵入自车行驶空间"
        outcome = "自车需要紧急制动或侧向避让"
        object_type = "walker"
    else:
        scenario_type = "cargo_drop"
        description = "前方对象状态突变，近距离障碍物进入自车行驶空间"
        outcome = "自车前方出现紧急障碍"
        object_type = "road_obstacle"

    return {
        "level": "L3",
        "id": f"L3-{index}",
        "parent_l2_id": event_id,
        "parent_l2_trigger": trigger,
        "chain_description": description,
        "direct_physical_outcome": outcome,
        "carla_plan": {
            "scenario_type": scenario_type,
            "target_actor": "front_truck",
            "object_type": object_type,
            "trigger_frame": 45,
            "spawn_relative_to": "front_truck",
            "initial_position": {"x": -3.2, "y": 0.0, "z": 2.4},
            "motion": {
                "mode": "scripted_projectile",
                "direction": "toward_ego",
                "back_speed_mps": 8.0,
                "lateral_drift_mps": 0.2,
                "gravity": True,
            },
            "expected_visual_result": "风险物体或前车动作出现在自车前方近距离区域",
        },
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
            chain.setdefault("carla_plan", fallback_plan_for_event({}, idx)["carla_plan"])
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
