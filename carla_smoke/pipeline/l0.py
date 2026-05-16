#!/usr/bin/env python3
"""DeepSeek subagent for L0 scene snapshot and L1 risk predictions."""

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


PROMPT_TEMPLATE = """你是自动驾驶风险推演系统中的 L0/L1 子智能体。

重要限制：
- 你现在使用的是 DeepSeek 文本 API，不能直接读取图片像素。
- 输入中会给出 CARLA 输出图片路径、可选 ego_log 车辆状态、以及可选场景提示。
- 不要声称自己“看到了图片”。如果某个信息只来自文件名、日志或提示，必须在 evidence 里写清来源。

任务一：L0 场景根节点
- 生成“当前时刻的场景结构化快照”。
- 结构化描述自车、道路、关键对象、相对距离、遮挡和不确定项。
- 示例风格：自车60km/h，卡车距12m，金属管绳索固定，骑行者距15m。
- 对未知数值使用 unknown；对推测数值使用 estimated，并降低 confidence。

任务二：L1 物理风险推测
- 识别场景中最可能存在风险的 5 个薄弱环节。
- 候选包括：货物固定不稳、卡车刹车灯失效、骑行者靠近机动车道、道路湿滑、自车A柱盲区、前方大型车辆遮挡视野、跟车距离不足、车道空间不足或避让空间受限、前车突然减速或静止。

请只输出一个 JSON 对象，不要 Markdown，不要解释性前后缀。格式必须是：
{
  "l0_state_snapshot": {
    "level": "L0",
    "name": "场景根节点",
    "description": "当前时刻的场景结构化快照",
    "source": {
      "image_path": "",
      "ego_log": {},
      "scenario_hint": ""
    },
    "ego": {
      "speed": {"value": "unknown/estimated number", "unit": "km/h", "confidence": "low/medium/high"},
      "lane_position": "unknown",
      "motion_state": "行驶/减速/停止/unknown"
    },
    "road": {
      "type": "城市道路/高速/路口/弯道/unknown",
      "surface": "干燥/湿滑/unknown",
      "lane_space": "充足/受限/unknown",
      "visibility": "良好/受遮挡/unknown"
    },
    "objects": [
      {
        "id": "obj_1",
        "type": "car/truck/cyclist/pedestrian/obstacle/other",
        "relative_position": "front/left/right/rear/front-left/front-right/unknown",
        "distance": {"value": "unknown/estimated number", "unit": "m", "confidence": "low/medium/high"},
        "state": "moving/stopped/unknown",
        "evidence": "来自 scenario_hint / ego_log / 文件路径 / 推测"
      }
    ],
    "scene_text": "一句话压缩场景"
  },
  "l1_risk_predictions": [
    {
      "level": "L1",
      "rank": 1,
      "name": "风险薄弱环节名称",
      "risk_type": "物理风险推测",
      "visibility": "可见/部分可见/纯推测",
      "risk_level": "低/中/高",
      "evidence": "证据来源，必须说明不是直接视觉识别时的依据",
      "trigger": "可能触发事件",
      "reason": "为什么它适合作为 L1 节点"
    }
  ]
}

硬性要求：
- l1_risk_predictions 必须正好 5 项，rank 从 1 到 5。
- 不要把不可见事实写成确定事实。
- 对缺少图像视觉证据的风险，用“纯推测”或“部分可见/部分已知”。
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


def read_ego_log(path, frame):
    if not path or not os.path.exists(path):
        return {}
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        return {}
    if frame is None:
        return rows[len(rows) // 2]
    closest = min(rows, key=lambda row: abs(int(float(row.get("frame", 0))) - frame))
    return closest


def infer_ego_log_path(image_path):
    candidate = os.path.join(os.path.dirname(image_path), "ego_log.csv")
    return candidate if os.path.exists(candidate) else None


def build_prompt(image_path, ego_log_row, scenario_hint):
    input_data = {
        "selected_image_path": os.path.abspath(image_path),
        "selected_frame": frame_from_image_name(image_path),
        "ego_log_row": ego_log_row,
        "scenario_hint": scenario_hint,
    }
    return PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(input_data, ensure_ascii=False, indent=2)


def normalize_output(parsed, image_path, ego_log_row, scenario_hint):
    if not isinstance(parsed, dict):
        parsed = {}

    l0 = parsed.get("l0_state_snapshot", {})
    if not isinstance(l0, dict):
        l0 = {}
    l0.setdefault("level", "L0")
    l0.setdefault("name", "场景根节点")
    l0.setdefault("description", "当前时刻的场景结构化快照")
    l0["source"] = {
        "image_path": os.path.abspath(image_path),
        "ego_log": ego_log_row,
        "scenario_hint": scenario_hint,
        "vision_note": "DeepSeek text API did not inspect image pixels; fields are inferred from metadata and hints.",
    }

    risks = parsed.get("l1_risk_predictions", [])
    if not isinstance(risks, list):
        risks = []

    normalized_risks = []
    for idx, risk in enumerate(risks[:5], start=1):
        if not isinstance(risk, dict):
            risk = {"name": str(risk)}
        risk.setdefault("level", "L1")
        risk.setdefault("risk_type", "物理风险推测")
        risk.setdefault("visibility", "纯推测")
        risk["rank"] = idx
        normalized_risks.append(risk)

    fallback_names = ["跟车距离不足", "前方车辆遮挡视野", "前车突然减速或静止", "车道空间不足或避让空间受限", "道路湿滑"]
    while len(normalized_risks) < 5:
        idx = len(normalized_risks) + 1
        normalized_risks.append(
            {
                "level": "L1",
                "rank": idx,
                "name": fallback_names[idx - 1],
                "risk_type": "物理风险推测",
                "visibility": "纯推测",
                "risk_level": "低",
                "evidence": "DeepSeek 未返回足够结构化结果；由默认候选补齐",
                "trigger": "需要后续 L2 生成具体触发事件",
                "reason": "保证 L1 固定输出 5 个可展开节点",
            }
        )

    return l0, normalized_risks


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description="DeepSeek L0/L1 subagent: state snapshot and five risk predictions.")
    parser.add_argument("path", help="Image file or directory from the CARLA scene output.")
    parser.add_argument("--model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--select", choices=["first", "middle", "last"], default="middle")
    parser.add_argument("--image-index", type=int, default=None)
    parser.add_argument("--ego-log", default=None)
    parser.add_argument("--scenario-hint", default="")
    parser.add_argument("--output-dir", default="carla_smoke/workdir/manual/l0")
    args = parser.parse_args()

    image_paths = list(iter_image_paths(args.path))
    try:
        image_path = select_image(image_paths, args.select, args.image_index)
    except (ValueError, IndexError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    ego_log_path = args.ego_log or infer_ego_log_path(image_path)
    ego_log_row = read_ego_log(ego_log_path, frame_from_image_name(image_path))
    prompt = build_prompt(image_path, ego_log_row, args.scenario_hint)

    print(f"L0/L1 DeepSeek input image path: {image_path}")
    print("NOTE: DeepSeek text API receives metadata and hints, not image pixels.")
    raw_response = ""
    parsed = None
    try:
        api_key = get_api_key(args.api_key_env)
        raw_response = chat_json(args.url, args.model, api_key, prompt, args.timeout)
        parsed = parse_json_response(raw_response)
    except (DeepSeekError, json.JSONDecodeError) as exc:
        print(f"WARNING: DeepSeek L0/L1 failed; using deterministic fallback fields: {exc}", file=sys.stderr)
    l0, risks = normalize_output(parsed, image_path, ego_log_row, args.scenario_hint)

    state_path = os.path.join(args.output_dir, "state.json")
    risks_path = os.path.join(args.output_dir, "risks.json")
    raw_path = os.path.join(args.output_dir, "deepseek_raw.json")

    write_json(state_path, l0)
    write_json(risks_path, {"source_state_file": os.path.abspath(state_path), "risks": risks})
    write_json(raw_path, {"raw_response": raw_response})

    print(f"Saved L0 state: {os.path.abspath(state_path)}")
    print(f"Saved L1 risks: {os.path.abspath(risks_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
