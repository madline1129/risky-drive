#!/usr/bin/env python3
"""Analyze CARLA images with a Qwen-VL model served by Ollama."""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_PROMPT = """你是自动驾驶安全场景分析助手。请基于这张 CARLA 自车前视图，识别当前场景中最可能存在风险的薄弱环节，用于后续风险推演。

请严格按以下结构输出：

【场景简述】
用1-2句话描述自车前方的关键交通参与者、道路结构和可见障碍物。

【可见风险证据】
只列图片中能直接观察到的风险线索，例如：
- 前方是否有货车/大型车辆
- 是否存在掉落物、障碍物、施工物、行人、骑行者
- 车辆距离是否过近
- 是否有遮挡、盲区、车道变窄、路口、弯道
- 道路表面是否疑似湿滑或低附着

【潜在薄弱环节 L1】
从以下候选项中判断哪些最可能成立，并给出理由：
1. 货物固定不稳
2. 卡车刹车灯失效
3. 骑行者靠近机动车道
4. 道路湿滑
5. 自车A柱盲区
6. 前方大型车辆遮挡视野
7. 跟车距离不足
8. 车道空间不足或避让空间受限
9. 其他你认为重要的风险

每个薄弱环节请输出：
- 风险名称
- 是否可见：可见 / 部分可见 / 纯推测
- 风险等级：低 / 中 / 高
- 图像依据：引用画面中的具体线索
- 后续可能触发事件：一句话说明可能如何演化

【最值得推演的Top-3风险】
按优先级列出3个最值得进入下一步风险推演的薄弱环节，
并说明排序原因。

  注意：
- 不要把看不见的事情当成事实。
- 可以提出合理假设，但必须标注为“推测”。
- 重点是识别潜在危险薄弱环节，不是写普通图片描述。
  """



def iter_image_paths(path):
    if os.path.isdir(path):
        names = sorted(os.listdir(path))
        for name in names:
            if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                yield os.path.join(path, name)
    else:
        yield path


def encode_image(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def ask_ollama(url, model, prompt, image_path, timeout):
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
        body = resp.read().decode("utf-8")
    result = json.loads(body)
    message = result.get("message", {})
    return message.get("content", "")


def main():
    parser = argparse.ArgumentParser(description="Use Ollama Qwen-VL to analyze CARLA images.")
    parser.add_argument("path", help="Image file or directory of images.")
    parser.add_argument("--model", default="qwen3.5:0.8b")
    parser.add_argument("--url", default="http://127.0.0.1:11434/api/chat")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--limit", type=int, default=0, help="Max images to analyze; 0 means all.")
    parser.add_argument("--output", default=None, help="Optional JSONL output path.")
    args = parser.parse_args()

    image_paths = list(iter_image_paths(args.path))
    if args.limit > 0:
        image_paths = image_paths[: args.limit]
    if not image_paths:
        print(f"No image files found: {args.path}", file=sys.stderr)
        return 1

    out_f = open(args.output, "w", encoding="utf-8") if args.output else None
    try:
        for idx, image_path in enumerate(image_paths, start=1):
            print(f"\n[{idx}/{len(image_paths)}] {image_path}")
            try:
                response = ask_ollama(args.url, args.model, args.prompt, image_path, args.timeout)
            except urllib.error.URLError as exc:
                print(f"ERROR: failed to call Ollama at {args.url}: {exc}", file=sys.stderr)
                return 1

            print(response.strip())
            if out_f:
                out_f.write(json.dumps({"image": image_path, "response": response}, ensure_ascii=False) + "\n")
                out_f.flush()
    finally:
        if out_f:
            out_f.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
