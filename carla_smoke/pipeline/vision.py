#!/usr/bin/env python3
"""Local Qwen/Ollama vision observer for CARLA images or sampled multi-frame montages."""

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


VISION_PROMPT = """你是自动驾驶风险推演系统中的视觉观测子模块。
输入是 1 张或多张 CARLA 图像；在 SafeBench 多视角模式下，每张图是同一时刻 6 个自车相机视角拼接成的 2x3 图，顺序固定为第一行 CAM_FRONT / CAM_FRONT_LEFT / CAM_FRONT_RIGHT，第二行 CAM_BACK / CAM_BACK_LEFT / CAM_BACK_RIGHT。请只基于图像可见内容输出结构化 JSON。

请只输出一个 JSON 对象，不要 Markdown，不要解释性前后缀。格式必须是：
{
  "observer": "qwen_local_vision",
  "scene_summary": "1-2句话描述多帧中可见道路、交通参与者和风险变化",
  "key_frames": [
    {
      "frame_hint": "第几张输入图或图像文件名",
      "reason": "为什么这一帧值得后续风险推理关注"
    }
  ],
  "visible_objects": [
    {
      "type": "car/truck/bus/cyclist/pedestrian/traffic_light/obstacle/road_marking/other",
      "position": "front/left/right/front-left/front-right/unknown",
      "apparent_distance": "near/medium/far/unknown",
      "description": "可见外观",
      "confidence": "low/medium/high"
    }
  ],
  "visual_risks": [
    {
      "name": "视觉上可见或疑似的风险",
      "evidence": "图像依据",
      "risk_level": "低/中/高",
      "confidence": "low/medium/high"
    }
  ],
  "occlusions": [
    {
      "source": "造成遮挡的对象或结构",
      "area": "front/left/right/front-left/front-right/unknown",
      "possible_hidden_target": "可能被遮挡的对象类型",
      "confidence": "low/medium/high"
    }
  ],
  "uncertain_observations": ["不确定但值得后续用CARLA API核验的视觉线索"]
}

约束：
- 不要估计精确米数；只能用 near/medium/far。
- 不要把看不见的对象写成事实。
- 多图输入时要综合时间变化，优先指出新出现、靠近、横穿、遮挡后出现、从侧后方进入的对象。
- 如果画面很普通，也要说明“未见明显异常”。
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


def select_images(image_paths, select, image_index, sample_count):
    if sample_count is None or sample_count <= 1:
        return [select_image(image_paths, select, image_index)]
    if not image_paths:
        raise ValueError("No image files found.")
    if image_index is not None:
        start = max(0, min(image_index, len(image_paths) - 1))
        return image_paths[start : start + sample_count]
    if sample_count >= len(image_paths):
        return image_paths
    if sample_count == 1:
        return [select_image(image_paths, select, None)]
    indexes = []
    for idx in range(sample_count):
        pos = round(idx * (len(image_paths) - 1) / (sample_count - 1))
        indexes.append(int(pos))
    return [image_paths[idx] for idx in sorted(set(indexes))]


def frame_from_image_name(path):
    match = re.search(r"rgb_(\d+)", os.path.basename(path))
    return int(match.group(1)) if match else None


def encode_image(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def call_ollama_vision(url, model, prompt, image_paths, timeout):
    if isinstance(image_paths, str):
        image_paths = [image_paths]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt, "images": [encode_image(path) for path in image_paths]}],
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
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        stripped = stripped[start : end + 1]
    return json.loads(stripped)


def normalize_observation(parsed, image_paths, model, error=None):
    if isinstance(image_paths, str):
        image_paths = [image_paths]
    if not isinstance(parsed, dict):
        parsed = {}
    parsed.setdefault("observer", "qwen_local_vision")
    parsed["source_image"] = os.path.abspath(image_paths[0]) if image_paths else None
    parsed["source_frame"] = frame_from_image_name(image_paths[0]) if image_paths else None
    parsed["source_images"] = [os.path.abspath(path) for path in image_paths]
    parsed["source_frames"] = [frame_from_image_name(path) for path in image_paths]
    parsed["sample_count"] = len(image_paths)
    parsed["model"] = model
    if error:
        parsed["error"] = error
        parsed.setdefault("scene_summary", "Qwen vision call failed; no visual observation available.")
        parsed.setdefault("visible_objects", [])
        parsed.setdefault("visual_risks", [])
        parsed.setdefault("occlusions", [])
        parsed.setdefault("uncertain_observations", [])
    return parsed


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description="Run local Qwen/Ollama vision observation on one CARLA image.")
    parser.add_argument("path", help="Image file or directory.")
    parser.add_argument("--model", default="qwen3.5:0.8b")
    parser.add_argument("--url", default="http://127.0.0.1:11434/api/chat")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--select", choices=["first", "middle", "last"], default="middle")
    parser.add_argument("--image-index", type=int, default=None)
    parser.add_argument("--sample-count", type=int, default=1, help="Number of evenly sampled images to send to the vision model.")
    parser.add_argument("--output-dir", default="carla_smoke/workdir/manual/vision")
    parser.add_argument("--fail-on-error", action="store_true")
    args = parser.parse_args()

    image_paths = list(iter_image_paths(args.path))
    try:
        selected_images = select_images(image_paths, args.select, args.image_index, args.sample_count)
    except (ValueError, IndexError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Qwen vision images: " + ", ".join(selected_images))
    raw_response = ""
    parsed = None
    error = None
    try:
        raw_response = call_ollama_vision(args.url, args.model, VISION_PROMPT, selected_images, args.timeout)
        parsed = parse_json_response(raw_response)
    except (urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
        error = str(exc)
        print(f"WARNING: Qwen vision failed: {error}", file=sys.stderr)
        if args.fail_on_error:
            return 1

    observation = normalize_observation(parsed, selected_images, args.model, error=error)
    observations_path = os.path.join(args.output_dir, "observations.json")
    raw_path = os.path.join(args.output_dir, "qwen_raw.json")
    write_json(observations_path, observation)
    write_json(raw_path, {"raw_response": raw_response})
    print(f"Saved vision observations: {os.path.abspath(observations_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
