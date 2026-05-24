#!/usr/bin/env python3
"""DeepSeek subagent for L3 initial accident chains from L2 triggers."""

import argparse
import json
import os
import sys

from deepseek_client import DEFAULT_DEEPSEEK_MODEL, DEFAULT_DEEPSEEK_URL, DeepSeekError, chat_json, get_api_key, parse_json_response
from risk_library import risk_type_by_id


PROMPT_TEMPLATE = """你是自动驾驶风险推演系统中的 L3 子智能体。
输入是 L2 触发事件假设 JSON，以及可选的精简单帧 L0 场景快照。

任务：
L3 初始事故链：
- 对每个 L2 触发事件，构思它导致的直接物理后果。
- L2 会携带 risk_family、risk_type_id；L3 必须继承它们，并根据 risk_type_id 选择主动作原语。
- L3 可以补充 participant_actions / accompanying_actions 描述其他参与者的响应或不响应；这是第一层真正开始组织动作原语的地方。
- L3 不是最终事故，也不是二次事故；只描述触发后第一段物理演化。
- L3 只写自然语言事故链和涉及物体清单，不生成 CARLA/Scenic 执行计划。
- L3 不输出完整 actor 快照，不输出出生地点参数；只允许用 actor_ref/role 说明事故链涉及哪些角色。具体物体选择、出生地点和动作参数由 L4 PlanAgent 根据 L0+L3 完成。
- 对于事故链涉及多个物体的情况，用 chain_participants 列清楚：谁是主扰动物体角色，谁是 ego，谁只是背景/遮挡/受影响对象。
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
      "risk_family": "继承自L2",
      "risk_type_id": "继承自L2",
      "primary_trigger_action_id": "由risk_type_id对应的主动作原语ID",
      "chain_description": "金属管失去约束并从货车后部向自车方向飞出",
      "direct_physical_outcome": "金属管进入自车前方车道，形成紧急避让/制动障碍",
      "action_primitives": [
        {"role": "primary", "action_primitive_id": "cargo_drop_or_slide_into_path", "actor_role": "payload", "description": "主扰动物体动作"},
        {"role": "accompanying", "action_primitive_id": "ego_continue_without_braking", "actor_role": "ego", "description": "伴随触发动作"}
      ],
      "participant_actions": [
        {"actor_role": "ego", "action_id": "ego_continue_without_braking", "description": "其他参与者动作或不动作"}
      ],
      "chain_participants": [
        {"actor_role": "ego", "role": "affected_actor", "must_drive_primary_event": false},
        {"actor_role": "front_vehicle/payload/vulnerable_actor/side_vehicle/road_obstacle", "role": "primary_actor", "must_drive_primary_event": true},
        {"actor_role": "background_or_occluder", "role": "background_or_occluder", "must_not_drive_primary_event": true}
      ]
    }
  ]
}

硬性要求：
- initial_accident_chains 最多 10 项，优先覆盖输入中的前 10 个 L2。
- 每项必须包含 chain_description、direct_physical_outcome、action_primitives、chain_participants。
- 每项必须继承 risk_family、risk_type_id，并根据 risk_type_id 写出 primary_trigger_action_id；participant_actions 可以跨 family 引用动作，但必须服务于主事故链。
- 不要输出执行计划字段；L4 会单独把自然语言事故链翻译成 Scenic 执行任务。
- 不要为了“可视化明显”引入无关物体，例如非货物链条不要加入 metal_pipe。
- chain_participants 必须区分 primary_actor 和 background/occluder/affected_actor。
- 不要输出 primary_perturbation_object、risk_library_candidate、legacy_scenario_type 或完整 L0 actor 快照。
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


def inherit_event_context(chain, event):
    if not isinstance(chain, dict) or not isinstance(event, dict):
        return chain
    for key in ("risk_family", "risk_type_id"):
        if event.get(key) is not None and key not in chain:
            chain[key] = event[key]
    if not chain.get("primary_trigger_action_id"):
        risk_type = risk_type_by_id(chain.get("risk_type_id") or event.get("risk_type_id")) or {}
        if risk_type.get("primary_action_primitive_id"):
            chain["primary_trigger_action_id"] = risk_type["primary_action_primitive_id"]
    if "action_primitives" not in chain:
        chain["action_primitives"] = build_action_primitives(chain)
    if "chain_participants" not in chain:
        chain["chain_participants"] = default_chain_participants(chain)
    if "participant_actions" not in chain:
        chain["participant_actions"] = []
    return chain


def build_prompt(l2_data, l0_data):
    context = {
        "l0_state_snapshot": l0_data,
        "l2_trigger_event_hypotheses": l2_data,
        "primary_action_options_by_risk_type": primary_action_options_by_risk_type(l2_data),
    }
    return PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(context, ensure_ascii=False, indent=2)


def primary_action_options_by_risk_type(l2_data):
    options = {}
    for event in trigger_events_from_data(l2_data):
        if not isinstance(event, dict) or not event.get("risk_type_id"):
            continue
        risk_type = risk_type_by_id(event.get("risk_type_id")) or {}
        options[event["risk_type_id"]] = {
            "primary_action_primitive_id": risk_type.get("primary_action_primitive_id"),
            "actor_kinds": risk_type.get("actor_kinds", []),
            "match": risk_type.get("match", {}),
        }
    return options


def actor_role_for_primary_action(action_id):
    if not action_id:
        return "primary_actor"
    if action_id.startswith("front_vehicle"):
        return "front_vehicle"
    if action_id.startswith("vru"):
        return "vulnerable_actor"
    if action_id.startswith("side_vehicle"):
        return "side_vehicle"
    if action_id.startswith("static_obstacle"):
        return "road_obstacle"
    if action_id.startswith("cargo"):
        return "payload"
    if action_id.startswith("ego"):
        return "ego"
    return "primary_actor"


def build_action_primitives(chain):
    primary_action = chain.get("primary_trigger_action_id")
    primitives = []
    if primary_action:
        primitives.append(
            {
                "role": "primary",
                "action_primitive_id": primary_action,
                "actor_role": actor_role_for_primary_action(primary_action),
                "description": "主触发动作原语",
            }
        )
    for item in chain.get("participant_actions") or []:
        if not isinstance(item, dict):
            continue
        action_id = item.get("action_primitive_id") or item.get("action_id")
        if not action_id:
            continue
        primitives.append(
            {
                "role": "accompanying",
                "action_primitive_id": action_id,
                "actor_role": item.get("actor_role", actor_role_for_primary_action(action_id)),
                "description": item.get("description"),
            }
        )
    return primitives


def default_chain_participants(chain):
    primary_role = actor_role_for_primary_action(chain.get("primary_trigger_action_id"))
    return [
        {"actor_role": "ego", "role": "affected_actor", "must_drive_primary_event": False},
        {"actor_role": primary_role, "role": "primary_actor", "must_drive_primary_event": True},
    ]


def strip_l3_chain(chain):
    forbidden = {
        "primary_perturbation_object",
        "risk_library_candidate",
        "legacy_scenario_type",
        "actor_list",
        "selected_actor",
        "matched_actor_id",
        "matched_actor_kind",
    }
    for key in forbidden:
        chain.pop(key, None)
    if "chain_participants" in chain:
        chain["chain_participants"] = sanitize_chain_participants(chain.get("chain_participants"))
    if "action_primitives" in chain:
        chain["action_primitives"] = sanitize_action_primitives(chain.get("action_primitives"))
    allowed = {
        "level",
        "id",
        "parent_l2_id",
        "parent_l2_trigger",
        "risk_family",
        "risk_type_id",
        "primary_trigger_action_id",
        "chain_description",
        "direct_physical_outcome",
        "action_primitives",
        "participant_actions",
        "chain_participants",
    }
    return {key: chain.get(key) for key in allowed if chain.get(key) is not None}


def sanitize_chain_participants(participants):
    cleaned = []
    for item in participants or []:
        if not isinstance(item, dict):
            continue
        cleaned.append(
            {
                key: item.get(key)
                for key in ("actor_role", "role", "must_drive_primary_event", "must_not_drive_primary_event")
                if item.get(key) is not None
            }
        )
    return cleaned


def sanitize_action_primitives(primitives):
    cleaned = []
    for item in primitives or []:
        if not isinstance(item, dict):
            continue
        cleaned.append(
            {
                key: item.get(key)
                for key in ("role", "action_primitive_id", "actor_role", "description")
                if item.get(key) is not None
            }
        )
    return cleaned

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
            chain["action_primitives"] = build_action_primitives(chain)
            normalized.append(strip_l3_chain(chain))

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
