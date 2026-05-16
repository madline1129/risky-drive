#!/usr/bin/env python3
"""Analyze CARLA images with a Qwen-VL model served by Ollama."""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_PROMPT = """请分析这张 CARLA 自车前视图，只关注交通风险：
1. 前方是否有货车或大型车辆？
2. 道路上是否有掉落物、障碍物或异常物体？
3. 自车是否正在接近危险物体？
4. 是否出现碰撞、侧翻、视角异常或失控迹象？
5. 用一句话给出风险等级：低/中/高。
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
        "prompt": prompt,
        "images": [encode_image(image_path)],
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
    return result.get("response", "")


def main():
    parser = argparse.ArgumentParser(description="Use Ollama Qwen-VL to analyze CARLA images.")
    parser.add_argument("path", help="Image file or directory of images.")
    parser.add_argument("--model", default="qwen2.5vl:7b")
    parser.add_argument("--url", default="http://127.0.0.1:11434/api/generate")
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
