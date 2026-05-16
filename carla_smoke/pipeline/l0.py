#!/usr/bin/env python3
"""Build L0 from CARLA API state and use DeepSeek to infer L1 risks."""

import argparse
import csv
import json
import os
import re
import sys

from deepseek_client import (
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_DEEPSEEK_URL,
    DeepSeekError,
    chat_json,
    get_api_key,
    parse_json_response,
)


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


PROMPT_TEMPLATE = """你是自动驾驶风险推演系统中的 L1 子智能体。

输入是 CARLA API 已经导出的 L0 场景结构化快照。L0 是事实来源，你不要重新生成 L0，也不要声称看过图像。

任务：
- 基于 L0 中的自车速度、附近 actor、相对距离、相对方位、车道、路口、天气和最近前方目标，推断 5 个最可能的 L1 物理风险薄弱环节。
- L1 是“物理风险推测”，用于后续 L2 触发事件假设。
- 候选包括但不限于：货物固定不稳、卡车刹车灯失效、骑行者靠近机动车道、道路湿滑、自车A柱盲区、前方大型车辆遮挡视野、跟车距离不足、车道空间不足或避让空间受限、前车突然减速或静止。

请只输出一个 JSON 对象，不要 Markdown，不要解释性前后缀。格式必须是：
{
  "l1_risk_predictions": [
    {
      "level": "L1",
      "rank": 1,
      "name": "风险薄弱环节名称",
      "risk_type": "物理风险推测",
      "visibility": "由CARLA状态支持/部分由CARLA状态支持/纯推测",
      "risk_level": "低/中/高",
      "evidence": "必须引用 L0 字段，例如 ego.speed_kmh、nearest_front_actor.distance_m、weather.wetness",
      "trigger": "后续可能触发事件的一句话概括",
      "reason": "为什么它适合作为 L1 节点"
    }
  ]
}

硬性要求：
- l1_risk_predictions 必须正好 5 项，rank 从 1 到 5。
- 优先使用 L0 中有数据支撑的风险，不要胡编不可见对象。
- 如果风险缺少直接状态证据，visibility 必须写“纯推测”，risk_level 通常为低。
- L1 要适合继续展开到 L2 触发事件假设。
"""


def iter_image_paths(path):
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.lower().endswith(IMAGE_EXTENSIONS):
                yield os.path.join(path, name)
    else:
        yield path


def select_image(image_paths, select, image_index):
    if not image_paths:
        raise ValueError("No image files found.")
    if image_index is not None:
        return image_paths[image_index]
    if select == "first":
        return image_paths[0]
    if select == "last":
        return image_paths[-1]
    return image_paths[len(image_paths) // 2]


def frame_from_image_name(path):
    match = re.search(r"rgb_(\d+)", os.path.basename(path))
    return int(match.group(1)) if match else None


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def infer_state_path(image_path):
    frame = frame_from_image_name(image_path)
    if frame is None:
        return None
    candidate = os.path.join(os.path.dirname(image_path), f"state_{frame:04d}.json")
    return candidate if os.path.exists(candidate) else None


def read_state_from_jsonl(path, frame):
    if frame is None or not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("source", {}).get("frame") == frame:
                return item
    return None


def read_ego_log(path, frame):
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {}
    if frame is None:
        return rows[len(rows) // 2]
    return min(rows, key=lambda row: abs(int(float(row.get("frame", 0))) - frame))


def fallback_state_from_ego_log(image_path, ego_log_path):
    frame = frame_from_image_name(image_path)
    row = read_ego_log(ego_log_path, frame)
    speed_mps = float(row.get("speed_mps", 0.0)) if row else 0.0
    return {
        "level": "L0",
        "name": "场景根节点",
        "description": "当前时刻的场景结构化快照",
        "source": {
            "sensor": "ego_log_fallback",
            "image_file": os.path.basename(image_path),
            "frame": frame,
            "warning": "No CARLA API state JSON found; only ego_log.csv was available.",
        },
        "ego": {
            "speed_mps": round(speed_mps, 3),
            "speed_kmh": round(speed_mps * 3.6, 3),
            "location": {
                "x": float(row.get("x", 0.0)) if row else 0.0,
                "y": float(row.get("y", 0.0)) if row else 0.0,
                "z": float(row.get("z", 0.0)) if row else 0.0,
            },
            "rotation": {"yaw": float(row.get("yaw", 0.0)) if row else 0.0},
        },
        "road": {},
        "weather": {},
        "actors": [],
        "nearest_front_actor": None,
        "summary": {"nearby_actor_count": 0, "front_actor_count": 0},
    }


def load_l0_state(image_path, state_json, ego_log):
    if state_json:
        return read_json(state_json)

    inferred = infer_state_path(image_path)
    if inferred:
        return read_json(inferred)

    jsonl_path = os.path.join(os.path.dirname(image_path), "scene_states.jsonl")
    state = read_state_from_jsonl(jsonl_path, frame_from_image_name(image_path))
    if state:
        return state

    ego_log_path = ego_log or os.path.join(os.path.dirname(image_path), "ego_log.csv")
    return fallback_state_from_ego_log(image_path, ego_log_path)


def build_prompt(l0_state, scenario_hint):
    payload = {"l0_state_snapshot": l0_state, "scenario_hint": scenario_hint}
    return PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def fallback_risks_from_state(l0_state):
    ego_speed = float(l0_state.get("ego", {}).get("speed_kmh", 0.0) or 0.0)
    nearest_front = l0_state.get("nearest_front_actor")
    weather = l0_state.get("weather", {})
    actors = l0_state.get("actors", [])

    risks = []
    if nearest_front:
        distance = nearest_front.get("relative_longitudinal_m") or nearest_front.get("distance_m")
        level = "高" if ego_speed > 40 and distance is not None and distance < 15 else "中"
        risks.append(
            {
                "level": "L1",
                "rank": 1,
                "name": "跟车距离不足",
                "risk_type": "物理风险推测",
                "visibility": "由CARLA状态支持",
                "risk_level": level,
                "evidence": f"ego.speed_kmh={ego_speed}; nearest_front_actor.distance_m={distance}",
                "trigger": "前车突然急刹或低速停滞",
                "reason": "自车与前方目标距离较近时，制动和避让余量下降。",
            }
        )
        risks.append(
            {
                "level": "L1",
                "rank": 2,
                "name": "前方车辆遮挡视野",
                "risk_type": "物理风险推测",
                "visibility": "由CARLA状态支持",
                "risk_level": "中",
                "evidence": f"nearest_front_actor.type_id={nearest_front.get('type_id')}; relative_position={nearest_front.get('relative_position')}",
                "trigger": "被遮挡目标突然出现",
                "reason": "前方车辆处于自车前向区域，会压缩对更远处目标的观察空间。",
            }
        )

    if float(weather.get("wetness", 0.0) or 0.0) > 20 or float(weather.get("precipitation", 0.0) or 0.0) > 0:
        risks.append(
            {
                "level": "L1",
                "rank": len(risks) + 1,
                "name": "道路湿滑",
                "risk_type": "物理风险推测",
                "visibility": "由CARLA状态支持",
                "risk_level": "中",
                "evidence": f"weather.wetness={weather.get('wetness')}; weather.precipitation={weather.get('precipitation')}",
                "trigger": "车辆制动时轮胎打滑",
                "reason": "低附着会增大制动距离并降低横向稳定性。",
            }
        )

    if any(actor.get("kind") == "pedestrian" for actor in actors):
        risks.append(
            {
                "level": "L1",
                "rank": len(risks) + 1,
                "name": "行人或骑行者靠近机动车道",
                "risk_type": "物理风险推测",
                "visibility": "由CARLA状态支持",
                "risk_level": "中",
                "evidence": "actors 中存在 pedestrian 类型参与者",
                "trigger": "行人或骑行者突然进入车道",
                "reason": "弱势交通参与者轨迹不确定性高。",
            }
        )

    fallback = [
        ("前车突然减速或静止", "前方车辆速度状态可能变化", "前车急刹或停止"),
        ("车道空间不足或避让空间受限", "附近 actor 数量和车道关系可能压缩避让空间", "自车需要紧急变道但侧向空间不足"),
        ("自车A柱盲区", "CARLA 状态未直接给出视觉遮挡，作为低置信候选", "侧前方目标从盲区出现"),
        ("卡车刹车灯失效", "CARLA 状态未直接给出刹车灯观测，作为低置信候选", "前方大型车辆减速但灯光提示不足"),
        ("货物固定不稳", "CARLA 状态未直接给出货物约束，作为低置信候选", "货物从前车掉落"),
    ]
    for name, evidence, trigger in fallback:
        if len(risks) >= 5:
            break
        risks.append(
            {
                "level": "L1",
                "rank": len(risks) + 1,
                "name": name,
                "risk_type": "物理风险推测",
                "visibility": "纯推测",
                "risk_level": "低",
                "evidence": evidence,
                "trigger": trigger,
                "reason": "补齐为可展开的 L1 候选节点。",
            }
        )

    for idx, risk in enumerate(risks[:5], start=1):
        risk["rank"] = idx
    return risks[:5]


def normalize_risks(parsed, l0_state):
    risks = parsed.get("l1_risk_predictions", []) if isinstance(parsed, dict) else []
    if not isinstance(risks, list):
        risks = []

    normalized = []
    for idx, risk in enumerate(risks[:5], start=1):
        if not isinstance(risk, dict):
            risk = {"name": str(risk)}
        risk.setdefault("level", "L1")
        risk.setdefault("risk_type", "物理风险推测")
        risk.setdefault("visibility", "纯推测")
        risk["rank"] = idx
        normalized.append(risk)

    if len(normalized) < 5:
        fallback = fallback_risks_from_state(l0_state)
        for risk in fallback:
            if len(normalized) >= 5:
                break
            if risk["name"] not in {item.get("name") for item in normalized}:
                normalized.append(risk)

    for idx, risk in enumerate(normalized[:5], start=1):
        risk["rank"] = idx
    return normalized[:5]


def main():
    parser = argparse.ArgumentParser(description="CARLA API L0 state + DeepSeek L1 risk agent.")
    parser.add_argument("path", help="Image file or directory from the CARLA scene output.")
    parser.add_argument("--model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--env-file", default=None, help="Optional .env path. Defaults to searching upward from cwd.")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--select", choices=["first", "middle", "last"], default="middle")
    parser.add_argument("--image-index", type=int, default=None)
    parser.add_argument("--state-json", default=None, help="Optional explicit CARLA API state JSON.")
    parser.add_argument("--ego-log", default=None, help="Fallback only when no state JSON exists.")
    parser.add_argument("--scenario-hint", default="")
    parser.add_argument("--output-dir", default="carla_smoke/workdir/manual/l0")
    args = parser.parse_args()

    image_paths = list(iter_image_paths(args.path))
    try:
        image_path = select_image(image_paths, args.select, args.image_index)
    except (ValueError, IndexError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    l0_state = load_l0_state(image_path, args.state_json, args.ego_log)
    l0_state.setdefault("source", {})
    l0_state["source"]["selected_image_path"] = os.path.abspath(image_path)
    prompt = build_prompt(l0_state, args.scenario_hint)

    print(f"L0 state source: {l0_state.get('source', {}).get('sensor', 'unknown')}")
    print(f"Selected image: {image_path}")

    raw_response = ""
    parsed = None
    try:
        api_key = get_api_key(args.api_key_env, args.env_file)
        raw_response = chat_json(args.url, args.model, api_key, prompt, args.timeout)
        parsed = parse_json_response(raw_response)
    except (DeepSeekError, json.JSONDecodeError) as exc:
        print(f"WARNING: DeepSeek L1 failed; using CARLA-state fallback risks: {exc}", file=sys.stderr)

    risks = normalize_risks(parsed, l0_state)

    state_path = os.path.join(args.output_dir, "state.json")
    risks_path = os.path.join(args.output_dir, "risks.json")
    raw_path = os.path.join(args.output_dir, "deepseek_raw.json")

    write_json(state_path, l0_state)
    write_json(risks_path, {"source_state_file": os.path.abspath(state_path), "risks": risks})
    write_json(raw_path, {"raw_response": raw_response})

    print(f"Saved L0 state: {os.path.abspath(state_path)}")
    print(f"Saved L1 risks: {os.path.abspath(risks_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
