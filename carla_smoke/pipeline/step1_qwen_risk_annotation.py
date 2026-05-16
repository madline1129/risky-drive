#!/usr/bin/env python3
"""Decision-tree pipeline step 1: annotate CARLA images with Qwen-VL risk labels."""

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request


DEFAULT_PROMPT = """你是自动驾驶风险推演系统的第一步标注器。
输入是一张 CARLA 自车前视图。你的任务不是普通看图描述，而是识别后续决策树推演的 L1 风险薄弱环节。

候选薄弱环节包括：
1. 货物固定不稳
2. 卡车刹车灯失效
3. 骑行者靠近机动车道
4. 道路湿滑
5. 自车A柱盲区
6. 前方大型车辆遮挡视野
7. 跟车距离不足
8. 车道空间不足或避让空间受限
9. 其他可见风险

请只输出一个 JSON 对象，不要输出 Markdown，不要输出解释性前后缀。
JSON 字段格式如下：
{
  "scene_summary": "1-2句话概括场景",
  "visible_evidence": ["图片中能直接观察到的证据"],
  "risk_weaknesses": [
    {
      "name": "薄弱环节名称",
      "visibility": "可见/部分可见/纯推测",
      "risk_level": "低/中/高",
      "image_evidence": "画面依据；如果不可见，写明缺少证据",
      "possible_trigger": "后续可能如何演化",
      "why_l1": "为什么它适合作为决策树第一层风险节点"
    }
  ],
  "top3_for_rollout": ["按优先级排序的最多3个薄弱环节名称"]
}

约束：
- 不要把看不见的事情当成事实。
- 可以提出合理假设，但必须把 visibility 标成“纯推测”或“部分可见”。
- 如果画面里没有某个候选风险，不要强行加入。
"""


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def iter_image_paths(path, recursive=False):
    if os.path.isdir(path):
        if recursive:
            for root, _, names in os.walk(path):
                for name in sorted(names):
                    if name.lower().endswith(IMAGE_EXTENSIONS):
                        yield os.path.join(root, name)
        else:
            for name in sorted(os.listdir(path)):
                if name.lower().endswith(IMAGE_EXTENSIONS):
                    yield os.path.join(path, name)
    else:
        yield path


def encode_image(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def load_prompt(path):
    if not path:
        return DEFAULT_PROMPT
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


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
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
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


def write_jsonl(path, rows):
    output_dir = os.path.dirname(os.path.abspath(path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Step 1 of the risk decision-tree pipeline: Qwen risk annotation.")
    parser.add_argument("path", help="Image file or directory, for example carla_smoke/outputs/approach_truck.")
    parser.add_argument("--model", default="qwen3.5:0.8b")
    parser.add_argument("--url", default="http://127.0.0.1:11434/api/chat")
    parser.add_argument("--prompt-file", default=None, help="Optional custom prompt text file.")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--limit", type=int, default=0, help="Max images to analyze; 0 means all.")
    parser.add_argument("--recursive", action="store_true", help="Scan images recursively when path is a directory.")
    parser.add_argument(
        "--output",
        default="carla_smoke/outputs/risk_labels/step1_qwen_risk_annotations.jsonl",
        help="JSONL file for annotations.",
    )
    args = parser.parse_args()

    prompt = load_prompt(args.prompt_file)
    image_paths = list(iter_image_paths(args.path, recursive=args.recursive))
    if args.limit > 0:
        image_paths = image_paths[: args.limit]
    if not image_paths:
        print(f"No image files found: {args.path}", file=sys.stderr)
        return 1

    rows = []
    for idx, image_path in enumerate(image_paths, start=1):
        print(f"[{idx}/{len(image_paths)}] annotate {image_path}")
        try:
            raw_response = call_ollama_chat(args.url, args.model, prompt, image_path, args.timeout)
        except urllib.error.URLError as exc:
            print(f"ERROR: failed to call Ollama at {args.url}: {exc}", file=sys.stderr)
            return 1

        parsed = parse_json_response(raw_response)
        if parsed is None:
            print("  WARNING: model response was not valid JSON; saved raw_response only.")
        else:
            top3 = parsed.get("top3_for_rollout", [])
            print(f"  top3_for_rollout: {top3}")

        rows.append(
            {
                "image": os.path.abspath(image_path),
                "model": args.model,
                "step": "L1_QWEN_RISK_WEAKNESS_ANNOTATION",
                "parsed": parsed,
                "raw_response": raw_response,
            }
        )

    write_jsonl(args.output, rows)
    print(f"Saved annotations: {os.path.abspath(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
