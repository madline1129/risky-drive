#!/usr/bin/env python3
"""DeepSeek subagent for L3 initial accident chains from L2 triggers."""

import argparse
import json
import os
import sys

from deepseek_client import DEFAULT_DEEPSEEK_MODEL, DEFAULT_DEEPSEEK_URL, DeepSeekError, chat_json, get_api_key, parse_json_response


PROMPT_TEMPLATE = """你是自动驾驶风险推演系统中的 L3 子智能体。
输入是 L2 触发事件假设 JSON，以及可选的精简单帧 L0 场景快照。

任务：
L3 初始事故链：
- 对每个 L2 触发事件，构思它导致的直接物理后果。
- L3 不是最终事故，也不是二次事故；只描述触发后第一段物理演化。
- L3 只写自然语言事故链和涉及物体清单，不生成 CARLA/Scenic 执行计划。
- 如果 L2/L1 已经传入 primary_perturbation_object / perturbation_target，必须继承，不要换主风险物体。
- 如果上游对象 source="l0_actor"，必须保留 actor_id、type_id、location、rotation、relative_longitudinal_m、relative_lateral_m 等原始字段；不能改成 generated_object。
- 对于事故链涉及多个物体的情况，用 chain_participants 列清楚：谁是主扰动物体，谁是 ego，谁只是背景/遮挡/受影响对象。
- 背景对象必须标注 must_not_drive_primary_event=true，避免后续 L4/code agent 把背景对象当成主风险。
- L0 是单帧输入，不要把事故链写成已经观测到的多帧趋势；只能基于当前单帧距离、相对方位、速度、天气解释触发后的第一段物理演化。

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
- 不要输出执行计划字段；L4 会单独把自然语言事故链翻译成 Scenic 执行任务。
- 不要为了“可视化明显”引入无关物体，例如非货物链条不要加入 metal_pipe。
- chain_participants 必须区分 primary_actor 和 background/occluder/affected_actor。
- 单帧 L0 中不存在的对象只能在上游已经明确为 generated_object/generated_actor 时出现。
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


def event_by_l2_id(events):
    mapping = {}
    for idx, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            continue
        mapping[idx] = event
        if event.get("id") is not None:
            mapping[str(event.get("id"))] = event
    return mapping


def event_primary_object(event, scenario_type=None):
    if not isinstance(event, dict):
        return None
    primary = event.get("primary_perturbation_object") or event.get("selected_actor")
    if not isinstance(primary, dict):
        return None
    primary = dict(primary)
    role_by_scenario = {
        "front_vehicle_brake": "front_vehicle",
        "vulnerable_actor_intrusion": "vulnerable_actor",
        "road_obstacle_intrusion": "road_obstacle",
        "cargo_drop": "payload",
    }
    primary.setdefault("role", role_by_scenario.get(scenario_type, "primary_actor"))
    primary["must_drive_primary_event"] = True
    return primary


def chain_participants_from_event(event, primary):
    participants = [
        {"source": "l0_ego", "actor_id": "ego", "kind": "ego", "role": "affected_actor", "must_drive_primary_event": False}
    ]
    if isinstance(primary, dict):
        participants.append(dict(primary))
    for actor in event.get("actor_list", []) if isinstance(event, dict) and isinstance(event.get("actor_list"), list) else []:
        if not isinstance(actor, dict):
            continue
        actor_id = actor.get("actor_id", actor.get("id"))
        if isinstance(primary, dict) and actor_id == primary.get("actor_id", primary.get("id")):
            continue
        background = dict(actor)
        background.setdefault("role", "background_or_context")
        background["must_not_drive_primary_event"] = True
        participants.append(background)
    return participants


def inherit_event_context(chain, event):
    if not isinstance(chain, dict) or not isinstance(event, dict):
        return chain
    primary = event_primary_object(event)
    if primary and "primary_perturbation_object" not in chain:
        chain["primary_perturbation_object"] = primary
    if isinstance(event.get("actor_list"), list) and "actor_list" not in chain:
        chain["actor_list"] = event["actor_list"]
    if primary and "chain_participants" not in chain:
        chain["chain_participants"] = chain_participants_from_event(event, primary)
    return chain


def build_prompt(l2_data, l0_data):
    context = {"l0_state_snapshot": l0_data, "l2_trigger_event_hypotheses": l2_data}
    return PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(context, ensure_ascii=False, indent=2)


def normalize_output(parsed, l2_data, source_l2_file):
    chains = parsed.get("initial_accident_chains", []) if isinstance(parsed, dict) else []
    normalized = []
    events = trigger_events_from_data(l2_data)[:10]
    events_by_id = event_by_l2_id(events)
    if isinstance(chains, list):
        for idx, chain in enumerate(chains[:10], start=1):
            if not isinstance(chain, dict):
                chain = {"chain_description": str(chain)}
            chain.setdefault("level", "L3")
            chain.setdefault("id", f"L3-{idx}")
            chain.pop("carla" + "_plan", None)
            inherit_event_context(chain, events_by_id.get(chain.get("parent_l2_id")) or events_by_id.get(idx))
            normalized.append(chain)

    if not normalized:
        raise ValueError("L3 LLM output must contain at least one initial accident chain")

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
    api_key = get_api_key(args.api_key_env, args.env_file)
    raw_response = chat_json(args.url, args.model, api_key, prompt, args.timeout)
    parsed = parse_json_response(raw_response)

    output = normalize_output(parsed, l2_data, args.l2_json)
    chains_path = os.path.join(args.output_dir, "chains.json")
    raw_path = os.path.join(args.output_dir, "deepseek_raw.json")
    write_json(chains_path, output)
    write_json(raw_path, {"raw_response": raw_response})
    print(f"Saved L3 chains: {os.path.abspath(chains_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
