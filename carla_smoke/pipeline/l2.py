#!/usr/bin/env python3
"""DeepSeek subagent for L2 trigger-event hypotheses from L1 risks."""

import argparse
import json
import os
import sys

from deepseek_client import DEFAULT_DEEPSEEK_MODEL, DEFAULT_DEEPSEEK_URL, DeepSeekError, chat_json, get_api_key, parse_json_response
from risk_library import risk_types_for_family


PROMPT_TEMPLATE = """你是自动驾驶风险推演系统中的 L2 子智能体。
你的输入不是图像，而是上一步 L1 生成的风险薄弱环节 JSON，可能还包含精简的单帧 L0 场景快照。

任务：
L2 触发事件假设：
- 对每个 L1 脆弱点，构思具体的触发事件。
- 每个 L1 只带有 risk_family；L2 必须只在该风险家族内选择一个具体 risk_type_id。
- 这些触发事件是“反事实干预”：如果这个事件发生，当前风险薄弱点会被激活，并进入后续事故链。
- L2 只描述具体触发事件，不展开事故链，不写 CARLA 执行方案，不指定掉落物/轨迹/脚本参数；这些属于 L3/L4。
- L0 是单帧输入，不能声称有多帧趋势；如果触发事件需要后续确认，observability 写“需要仿真状态确认”。
- L2 不选择主风险对象，不输出完整 actor，不输出动作原语；L2 只负责给 L1 的风险家族和薄弱点补一个具体触发事件。
- 平均每个 L1 给出 2 个触发事件。
- 如果 L1 有 5 个风险，则总共输出 10 个 L2 触发事件。

示例：
- 对 L1「货物固定不稳」：a. 绳索断裂；b. 货物未被固定。
- 对 L1「骑行者靠近机动车道」：a. 骑行者突然滑倒；b. 骑行者为避让坑洼突然转向。

请只输出一个 JSON 对象，不要 Markdown，不要解释性前后缀。格式必须是：
{
  "level": "L2",
  "name": "触发事件假设",
  "description": "对每个脆弱点构思具体的触发事件（反事实干预）",
  "source_l1_file": "",
  "trigger_event_hypotheses": [
    {
      "level": "L2",
      "id": "L2-1a",
      "parent_l1_rank": 1,
      "parent_l1_name": "对应的L1风险薄弱环节",
      "trigger_name": "具体触发事件名称",
      "risk_family": "继承自L1的风险家族ID",
      "risk_type_id": "从该risk_family内选择的具体风险类型ID",
      "counterfactual_intervention": "如果人为设定这个事件发生，场景会发生什么变化",
      "mechanism": "为什么该事件会激活对应L1风险",
      "immediate_effect": "触发后最直接的状态变化，不要展开为事故链",
      "required_preconditions": ["该触发事件成立需要哪些前提"],
      "observability": "可由图像确认/需要仿真状态确认/纯假设",
      "plausibility": "低/中/高",
      "boundary": "L2只定义触发事件，L3再展开物理演化"
    }
  ]
}

硬性要求：
- trigger_event_hypotheses 必须正好 10 项。
- 每个 L1 风险默认生成 2 个 L2 触发事件，id 形如 L2-1a、L2-1b、L2-2a、L2-2b。
- 不要重新识别图像；只能基于输入 JSON 做推演。
- 必须继承 L1 的 risk_family，并且 risk_type_id 只能来自输入的 risk_type_options_by_family[risk_family]。
- 不要输出 primary_trigger_action_id、primary_action_primitive_id、action_primitive、risk_library_candidate、legacy_scenario_type、actor_list、selected_actor、primary_perturbation_object、matched_actor_id。
- 触发事件必须具体、可在 CARLA 或规则脚本中实现/近似实现。
- 触发事件只到“初始触发”，不要直接跳到 L3/L4 的事故链、对象轨迹、二次事故或代码参数。
- 不要把单帧风险写成“持续靠近/跨帧出现/速度逐渐变化”这类多帧事实。
"""


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_prompt(l1_data, l0_data):
    context = {
        "l0_state_snapshot": l0_data,
        "l1_risk_predictions": l1_data,
        "risk_type_options_by_family": risk_type_options_by_family(l1_data),
    }
    return PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(context, ensure_ascii=False, indent=2)


def risk_type_options_by_family(l1_data):
    families = []
    for risk in l1_risks_from_data(l1_data):
        if isinstance(risk, dict) and risk.get("risk_family") and risk.get("risk_family") not in families:
            families.append(risk.get("risk_family"))
    options = {}
    for family in families:
        options[family] = [
            {
                "risk_type_id": item.get("id"),
                "keywords": item.get("keywords", []),
                "actor_kinds": item.get("actor_kinds", []),
                "match": item.get("match", {}),
            }
            for item in risk_types_for_family(family)
        ]
    return options


def l1_risks_from_data(l1_data):
    if isinstance(l1_data, dict) and isinstance(l1_data.get("risks"), list):
        return l1_data["risks"]
    if isinstance(l1_data, dict) and isinstance(l1_data.get("l1_risk_predictions"), list):
        return l1_data["l1_risk_predictions"]
    if isinstance(l1_data, list):
        return l1_data
    return []


def fallback_events_for_risk(risk, rank):
    name = risk.get("name", "待确认风险薄弱环节") if isinstance(risk, dict) else str(risk)
    if "货物" in name or "固定" in name:
        pairs = [("绳索断裂", "货物约束突然失效"), ("货物未被固定", "车辆运动导致货物开始滑移")]
    elif "刹车灯" in name:
        pairs = [("前车减速但刹车灯不亮", "后车无法及时获得视觉提示"), ("刹车灯延迟亮起", "后车对减速时机判断滞后")]
    elif "骑行" in name or "自行车" in name:
        pairs = [("骑行者突然滑倒", "骑行者横向侵入机动车道"), ("骑行者避让坑洼突然转向", "骑行者轨迹发生突变")]
    elif "湿滑" in name or "路面" in name:
        pairs = [("车辆制动时轮胎打滑", "制动距离突然变长"), ("前方积水导致附着力下降", "车辆横向稳定性降低")]
    elif "A柱" in name or "盲区" in name:
        pairs = [("目标从A柱遮挡区出现", "自车感知目标时间被压缩"), ("自车转向时盲区扩大", "侧前方目标短时不可见")]
    elif "遮挡" in name or "大型车辆" in name:
        pairs = [("被遮挡车辆突然出现", "前方可通行空间骤减"), ("大型车辆突然变道", "遮挡解除后暴露近距离目标")]
    elif "跟车" in name or "距离" in name or ("前车" in name and "速度" in name):
        pairs = [("前车突然急刹", "自车剩余制动距离不足"), ("前车低速停滞", "自车需要紧急减速或变道")]
    else:
        pairs = [("目标运动状态突变", "当前薄弱环节被激活"), ("自车可用反应时间缩短", "避让或制动空间被压缩")]

    events = []
    for suffix, (trigger_name, outcome) in zip(["a", "b"], pairs):
        events.append(
            {
                "level": "L2",
                "id": f"L2-{rank}{suffix}",
                "parent_l1_rank": rank,
                "parent_l1_name": name,
                "trigger_name": trigger_name,
                "counterfactual_intervention": f"在仿真中强制发生：{trigger_name}",
                "mechanism": f"该事件会激活 L1「{name}」对应的薄弱环节。",
                "immediate_effect": outcome,
                "required_preconditions": ["L1 薄弱环节存在或部分存在", "自车与风险对象处于可相互影响范围内"],
                "observability": "需要仿真状态确认",
                "plausibility": "中",
                "boundary": "L2只定义触发事件，L3再展开物理演化",
            }
        )
    return events


def risk_by_rank(l1_data):
    risks = l1_risks_from_data(l1_data)[:5]
    mapping = {}
    for idx, risk in enumerate(risks, start=1):
        if not isinstance(risk, dict):
            continue
        mapping[idx] = risk
        try:
            mapping[int(risk.get("rank"))] = risk
        except (TypeError, ValueError):
            pass
    return mapping


def inherit_actor_context(event, risk):
    if not isinstance(event, dict) or not isinstance(risk, dict):
        return event
    if risk.get("risk_family") is not None:
        event["risk_family"] = risk["risk_family"]
    if not event.get("risk_type_id"):
        event["risk_type_id"] = choose_risk_type_id(event, risk)
    return event


def choose_risk_type_id(event, risk):
    family = (risk or {}).get("risk_family") or (event or {}).get("risk_family")
    options = risk_types_for_family(family)
    if not options:
        return None
    text = " ".join(
        str(value)
        for value in [
            (event or {}).get("trigger_name"),
            (event or {}).get("counterfactual_intervention"),
            (event or {}).get("mechanism"),
            (risk or {}).get("name"),
            (risk or {}).get("weakness_reason"),
        ]
        if value
    )
    best = None
    best_score = -1
    for item in options:
        score = sum(1 for keyword in item.get("keywords", []) if str(keyword) in text)
        if score > best_score:
            best = item
            best_score = score
    return best.get("id") if best else None


def strip_l2_event(event):
    allowed = {
        "level",
        "id",
        "parent_l1_rank",
        "parent_l1_name",
        "trigger_name",
        "risk_family",
        "risk_type_id",
        "counterfactual_intervention",
        "mechanism",
        "immediate_effect",
        "required_preconditions",
        "observability",
        "plausibility",
        "boundary",
    }
    return {key: event.get(key) for key in allowed if event.get(key) is not None}


def risk_for_event(risks_by_rank, event):
    rank = (event or {}).get("parent_l1_rank")
    if rank in risks_by_rank:
        return risks_by_rank[rank]
    try:
        return risks_by_rank.get(int(rank))
    except (TypeError, ValueError):
        return None


def normalize_output(parsed, l1_data, source_l1_file):
    events = parsed.get("trigger_event_hypotheses", []) if isinstance(parsed, dict) else []
    normalized = []
    risks_by_rank = risk_by_rank(l1_data)
    if isinstance(events, list):
        for idx, event in enumerate(events[:10], start=1):
            if not isinstance(event, dict):
                event = {"trigger_name": str(event)}
            event.setdefault("level", "L2")
            event.setdefault("id", f"L2-{((idx - 1) // 2) + 1}{'a' if idx % 2 == 1 else 'b'}")
            event.setdefault("parent_l1_rank", ((idx - 1) // 2) + 1)
            event.setdefault("parent_l1_name", "unknown")
            if "immediate_effect" not in event and "direct_physical_outcome" in event:
                event["immediate_effect"] = event.pop("direct_physical_outcome")
            event.setdefault("boundary", "L2只定义触发事件，L3再展开物理演化")
            inherit_actor_context(event, risk_for_event(risks_by_rank, event))
            normalized.append(strip_l2_event(event))

    if len(normalized) != 10:
        raise ValueError(f"L2 LLM output must contain exactly 10 trigger events, got {len(normalized)}")

    normalized = normalized[:10]
    for idx, event in enumerate(normalized, start=1):
        rank = ((idx - 1) // 2) + 1
        suffix = "a" if idx % 2 == 1 else "b"
        event["id"] = f"L2-{rank}{suffix}"
        event.setdefault("parent_l1_rank", rank)
        event.setdefault("boundary", "L2只定义触发事件，L3再展开物理演化")
        inherit_actor_context(event, risk_for_event(risks_by_rank, event))
        normalized[idx - 1] = strip_l2_event(event)

    return {
        "level": "L2",
        "name": "触发事件假设",
        "description": "对每个脆弱点构思具体的触发事件（反事实干预）",
        "source_l1_file": os.path.abspath(source_l1_file),
        "trigger_event_hypotheses": normalized,
    }


def main():
    parser = argparse.ArgumentParser(description="DeepSeek L2 subagent: trigger-event hypotheses from L1 risk JSON.")
    parser.add_argument("l1_json", help="Path to l0/risks.json.")
    parser.add_argument("--l0-json", default=None, help="Optional l0/state.json for context.")
    parser.add_argument("--model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--env-file", default=None, help="Optional .env path. Defaults to searching upward from cwd.")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output-dir", default="carla_smoke/workdir/manual/l2")
    args = parser.parse_args()

    l1_data = read_json(args.l1_json)
    l0_data = read_json(args.l0_json) if args.l0_json else None
    prompt = build_prompt(l1_data, l0_data)

    print(f"L2 DeepSeek input: {args.l1_json}")
    api_key = get_api_key(args.api_key_env, args.env_file)
    raw_response = chat_json(args.url, args.model, api_key, prompt, args.timeout)
    parsed = parse_json_response(raw_response)

    output = normalize_output(parsed, l1_data, args.l1_json)
    triggers_path = os.path.join(args.output_dir, "triggers.json")
    raw_path = os.path.join(args.output_dir, "deepseek_raw.json")

    write_json(triggers_path, output)
    write_json(raw_path, {"raw_response": raw_response})

    print(f"Saved L2 triggers: {os.path.abspath(triggers_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
