#!/usr/bin/env python3
"""Qwen subagent for L0 scene snapshot and L1 risk predictions."""

import argparse
import base64
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


PROMPT_TEMPLATE = """你是自动驾驶风险推演系统中的 L0/L1 子智能体。
输入是一张 CARLA 自车前视图。你需要完成两个层次：

L0 场景根节点：
- 生成“当前时刻的场景结构化快照”。
- 目标是描述当前画面中的道路、车辆、行人/骑行者、障碍物、天气/路面、遮挡关系、相对距离等。
- 示例风格：自车60km/h，卡车距12m，金属管绳索固定，骑行者距15m。
- 但是注意：图片里看不出来的数值不要假装确定，只能写 estimated 或 unknown。

L1 物理风险推测：
- 识别场景中最可能存在风险的 5 个薄弱环节。
- 候选包括但不限于：
  1. 货物固定不稳
  2. 卡车刹车灯失效
  3. 骑行者靠近机动车道
  4. 道路湿滑
  5. 自车A柱盲区
  6. 前方大型车辆遮挡视野
  7. 跟车距离不足
  8. 车道空间不足或避让空间受限
  9. 前车突然减速或静止
  10. 其他画面中可见或合理推测的风险

请只输出一个 JSON 对象，不要输出 Markdown，不要输出解释性前后缀。
必须严格包含下面两个顶层字段：
{
  "l0_state_snapshot": {
    "level": "L0",
    "name": "场景根节点",
    "description": "当前时刻的场景结构化快照",
    "source_image": "由程序填充，模型可写空字符串",
    "ego": {
      "speed": {"value": "unknown/estimated value", "unit": "km/h", "confidence": "low/medium/high"},
      "lane_position": "自车所在车道或位置",
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
        "relative_position": "front/left/right/rear/front-left/front-right",
        "distance": {"value": "unknown/estimated value", "unit": "m", "confidence": "low/medium/high"},
        "state": "moving/stopped/unknown",
        "evidence": "画面依据"
      }
    ],
    "scene_text": "用一句话压缩成类似：自车xx，前车距xx，道路xx，潜在对象xx"
  },
  "l1_risk_predictions": [
    {
      "level": "L1",
      "name": "风险薄弱环节名称",
      "rank": 1,
      "risk_type": "物理风险推测",
      "visibility": "可见/部分可见/纯推测",
      "risk_level": "低/中/高",
      "evidence": "图像依据",
      "trigger": "可能触发事件",
      "reason": "为什么它是当前场景最可能的薄弱环节之一"
    }
  ]
}

硬性要求：
- l1_risk_predictions 必须正好 5 项，rank 从 1 到 5。
- 不要把不可见事实写成确定事实；不可见但合理的风险必须标成“纯推测”。
- 如果画面很普通，也要给出 5 个“可能风险”，但风险等级可以是低。
- 优先让 L1 对应后续风险推演可以展开的节点，而不是普通描述。
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


def encode_image(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_prompt(scenario_hint):
    if not scenario_hint:
        return PROMPT_TEMPLATE
    return PROMPT_TEMPLATE + "\n\n额外场景提示，可信度低于图像证据：\n" + scenario_hint.strip()


def call_ollama_chat(url, model, prompt, image_path, timeout):
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [encode_image(image_path)],
            }
        ],
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


def normalize_output(parsed, image_path, raw_response):
    if not isinstance(parsed, dict):
        return {
            "l0_state_snapshot": {
                "level": "L0",
                "name": "场景根节点",
                "description": "当前时刻的场景结构化快照",
                "source_image": os.path.abspath(image_path),
                "parse_error": True,
            },
            "l1_risk_predictions": [],
            "raw_response": raw_response,
        }

    l0 = parsed.get("l0_state_snapshot", {})
    if not isinstance(l0, dict):
        l0 = {}
    l0.setdefault("level", "L0")
    l0.setdefault("name", "场景根节点")
    l0.setdefault("description", "当前时刻的场景结构化快照")
    l0["source_image"] = os.path.abspath(image_path)

    risks = parsed.get("l1_risk_predictions", [])
    if not isinstance(risks, list):
        risks = []
    normalized_risks = []
    for idx, risk in enumerate(risks[:5], start=1):
        if not isinstance(risk, dict):
            risk = {"name": str(risk)}
        risk.setdefault("level", "L1")
        risk.setdefault("risk_type", "物理风险推测")
        risk["rank"] = idx
        normalized_risks.append(risk)

    while len(normalized_risks) < 5:
        idx = len(normalized_risks) + 1
        normalized_risks.append(
            {
                "level": "L1",
                "name": "待确认风险薄弱环节",
                "rank": idx,
                "risk_type": "物理风险推测",
                "visibility": "纯推测",
                "risk_level": "低",
                "evidence": "模型未给出足够结构化结果",
                "trigger": "需要后续帧或传感器信息确认",
                "reason": "占位，保证 L1 输出固定为 5 个候选节点",
            }
        )

    return {
        "l0_state_snapshot": l0,
        "l1_risk_predictions": normalized_risks,
    }


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description="Qwen L0/L1 subagent: scene snapshot and five risk predictions.")
    parser.add_argument("path", help="Image file or directory of CARLA front-view images.")
    parser.add_argument("--model", default="qwen3.5:0.8b")
    parser.add_argument("--url", default="http://127.0.0.1:11434/api/chat")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--select", choices=["first", "middle", "last"], default="middle")
    parser.add_argument("--image-index", type=int, default=None, help="Override --select with a specific sorted image index.")
    parser.add_argument("--scenario-hint", default="", help="Optional text hint, e.g. ego speed or known object distances.")
    parser.add_argument("--output-dir", default="carla_smoke/outputs/agent_pipeline/l0_l1")
    args = parser.parse_args()

    image_paths = list(iter_image_paths(args.path))
    try:
        image_path = select_image(image_paths, args.select, args.image_index)
    except (ValueError, IndexError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    prompt = build_prompt(args.scenario_hint)
    print(f"L0/L1 subagent image: {image_path}")
    try:
        raw_response = call_ollama_chat(args.url, args.model, prompt, image_path, args.timeout)
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        print(f"ERROR: failed to call Ollama at {args.url}: {exc}", file=sys.stderr)
        return 1

    parsed = parse_json_response(raw_response)
    output = normalize_output(parsed, image_path, raw_response)

    l0_path = os.path.join(args.output_dir, "L0_state_snapshot.json")
    l1_path = os.path.join(args.output_dir, "L1_risk_predictions.json")
    raw_path = os.path.join(args.output_dir, "qwen_raw_response.txt")

    write_json(l0_path, output["l0_state_snapshot"])
    write_json(l1_path, {"source_image": os.path.abspath(image_path), "risks": output["l1_risk_predictions"]})
    os.makedirs(args.output_dir, exist_ok=True)
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(raw_response)

    print(f"Saved L0 snapshot: {os.path.abspath(l0_path)}")
    print(f"Saved L1 risks: {os.path.abspath(l1_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
