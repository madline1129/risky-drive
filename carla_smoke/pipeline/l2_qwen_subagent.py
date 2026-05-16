#!/usr/bin/env python3
"""Qwen subagent for L2 trigger-event hypotheses from L1 risks."""

import argparse
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request


PROMPT_TEMPLATE = """你是自动驾驶风险推演系统中的 L2 子智能体。
你的输入不是图像，而是上一步 L1 生成的风险薄弱环节 JSON，可能还包含 L0 场景快照。

任务：
L2 触发事件假设：
- 对每个 L1 脆弱点，构思具体的触发事件。
- 这些触发事件是“反事实干预”：也就是如果这个事件发生，当前风险薄弱点会被激活，并进入后续事故链。
- 平均每个 L1 给出 2 个触发事件。
- 如果 L1 有 5 个风险，则总共输出 10 个 L2 触发事件。

示例：
- 对 L1「货物固定不稳」：
  a. 绳索断裂
  b. 货物未被固定
- 对 L1「骑行者靠近机动车道」：
  a. 骑行者突然滑倒
  b. 骑行者为避让坑洼突然转向

请只输出一个 JSON 对象，不要输出 Markdown，不要输出解释性前后缀。
必须严格包含下面字段：
{
  "level": "L2",
  "name": "触发事件假设",
  "description": "对每个脆弱点构思具体的触发事件（反事实干预）",
  "source_l1_file": "由程序填充，模型可写空字符串",
  "trigger_event_hypotheses": [
    {
      "level": "L2",
      "id": "L2-1a",
      "parent_l1_rank": 1,
      "parent_l1_name": "对应的L1风险薄弱环节",
      "trigger_name": "具体触发事件名称",
      "counterfactual_intervention": "如果人为设定这个事件发生，场景会发生什么变化",
      "mechanism": "为什么该事件会激活对应L1风险",
      "direct_physical_outcome": "直接物理后果，不要写太远的最终事故",
      "required_preconditions": ["该触发事件成立需要哪些前提"],
      "observability": "可由图像确认/需要多帧确认/需要仿真状态确认/纯假设",
      "plausibility": "低/中/高"
    }
  ]
}

硬性要求：
- trigger_event_hypotheses 必须正好 10 项。
- 每个 L1 风险默认生成 2 个 L2 触发事件，id 形如 L2-1a、L2-1b、L2-2a、L2-2b。
- 不要重新识别图像；只能基于输入 JSON 做推演。
- 触发事件必须是具体、可在 CARLA 或规则脚本中实现/近似实现的事件。
- 触发事件只到“初始触发”，不要直接跳到 L3/L4 的事故链和二次事故。
"""


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def call_ollama_chat(url, model, prompt, timeout):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result.get("message", {}).get("content", "")


def parse_json_response(text):
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def build_prompt(l1_data, l0_data):
    context = {
        "l0_state_snapshot": l0_data,
        "l1_risk_predictions": l1_data,
    }
    return PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(context, ensure_ascii=False, indent=2)


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
    elif "跟车" in name or "距离" in name:
        pairs = [("前车突然急刹", "自车剩余制动距离不足"), ("前车低速停滞", "自车需要紧急减速或变道")]
    else:
        pairs = [("目标运动状态突变", "当前薄弱环节被激活"), ("自车可用反应时间缩短", "避让或制动空间被压缩")]

    events = []
    suffixes = ["a", "b"]
    for idx, (trigger_name, outcome) in enumerate(pairs, start=0):
        events.append(
            {
                "level": "L2",
                "id": f"L2-{rank}{suffixes[idx]}",
                "parent_l1_rank": rank,
                "parent_l1_name": name,
                "trigger_name": trigger_name,
                "counterfactual_intervention": f"在仿真中强制发生：{trigger_name}",
                "mechanism": f"该事件会激活 L1「{name}」对应的薄弱环节。",
                "direct_physical_outcome": outcome,
                "required_preconditions": ["L1 薄弱环节存在或部分存在", "自车与风险对象处于可相互影响范围内"],
                "observability": "需要仿真状态确认",
                "plausibility": "中",
            }
        )
    return events


def normalize_output(parsed, l1_data, source_l1_file):
    if isinstance(parsed, dict):
        events = parsed.get("trigger_event_hypotheses", [])
    else:
        events = []

    normalized = []
    if isinstance(events, list):
        for idx, event in enumerate(events[:10], start=1):
            if not isinstance(event, dict):
                event = {"trigger_name": str(event)}
            event.setdefault("level", "L2")
            event.setdefault("id", f"L2-{idx}")
            event.setdefault("parent_l1_rank", ((idx - 1) // 2) + 1)
            event.setdefault("parent_l1_name", "unknown")
            normalized.append(event)

    risks = l1_risks_from_data(l1_data)[:5]
    while len(normalized) < 10 and risks:
        risk_index = len(normalized) // 2
        risk = risks[min(risk_index, len(risks) - 1)]
        rank = risk.get("rank", min(risk_index + 1, 5)) if isinstance(risk, dict) else min(risk_index + 1, 5)
        for event in fallback_events_for_risk(risk, rank):
            if len(normalized) >= 10:
                break
            event["id"] = f"L2-{rank}{'a' if len([e for e in normalized if e.get('parent_l1_rank') == rank]) == 0 else 'b'}"
            normalized.append(event)

    while len(normalized) < 10:
        idx = len(normalized) + 1
        rank = ((idx - 1) // 2) + 1
        normalized.append(
            {
                "level": "L2",
                "id": f"L2-{rank}{'a' if idx % 2 == 1 else 'b'}",
                "parent_l1_rank": rank,
                "parent_l1_name": "待确认风险薄弱环节",
                "trigger_name": "待确认触发事件",
                "counterfactual_intervention": "需要更完整的 L1 输入后生成",
                "mechanism": "模型未返回足够结构化结果",
                "direct_physical_outcome": "unknown",
                "required_preconditions": [],
                "observability": "纯假设",
                "plausibility": "低",
            }
        )

    return {
        "level": "L2",
        "name": "触发事件假设",
        "description": "对每个脆弱点构思具体的触发事件（反事实干预）",
        "source_l1_file": os.path.abspath(source_l1_file),
        "trigger_event_hypotheses": normalized[:10],
    }


def main():
    parser = argparse.ArgumentParser(description="Qwen L2 subagent: trigger-event hypotheses from L1 risk JSON.")
    parser.add_argument("l1_json", help="Path to L1_risk_predictions.json.")
    parser.add_argument("--l0-json", default=None, help="Optional L0_state_snapshot.json for context.")
    parser.add_argument("--model", default="qwen3.5:0.8b")
    parser.add_argument("--url", default="http://127.0.0.1:11434/api/chat")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output-dir", default="carla_smoke/outputs/agent_pipeline/l2")
    args = parser.parse_args()

    l1_data = read_json(args.l1_json)
    l0_data = read_json(args.l0_json) if args.l0_json else None
    prompt = build_prompt(l1_data, l0_data)

    print(f"L2 subagent L1 input: {args.l1_json}")
    try:
        raw_response = call_ollama_chat(args.url, args.model, prompt, args.timeout)
        parsed = parse_json_response(raw_response)
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        print(f"ERROR: failed to call Ollama at {args.url}: {exc}", file=sys.stderr)
        raw_response = ""
        parsed = None

    output = normalize_output(parsed, l1_data, args.l1_json)
    output_path = os.path.join(args.output_dir, "L2_trigger_event_hypotheses.json")
    raw_path = os.path.join(args.output_dir, "qwen_raw_response.txt")

    write_json(output_path, output)
    os.makedirs(args.output_dir, exist_ok=True)
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(raw_response)

    print(f"Saved L2 triggers: {os.path.abspath(output_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
