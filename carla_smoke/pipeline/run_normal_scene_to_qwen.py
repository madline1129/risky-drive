#!/usr/bin/env python3
"""Run the minimal pipeline: CARLA normal scene -> saved images -> Qwen risk labels."""

import argparse
import os
import subprocess
import sys


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def run_command(command):
    print("\n$ " + " ".join(command))
    subprocess.run(command, check=True)


def main():
    repo_root = repo_root_from_this_file()
    default_image_dir = os.path.join(repo_root, "carla_smoke", "outputs", "normal_driving")
    default_label_path = os.path.join(
        repo_root,
        "carla_smoke",
        "outputs",
        "risk_labels",
        "normal_driving_step1_qwen.jsonl",
    )

    parser = argparse.ArgumentParser(description="Generate a normal CARLA scene, save frames, and annotate risk with Qwen.")
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--tm-port", type=int, default=8000)
    parser.add_argument("--town", default="Town03")
    parser.add_argument("--frames", type=int, default=160)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--vehicles", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--image-dir", default=default_image_dir)
    parser.add_argument("--label-output", default=default_label_path)
    parser.add_argument("--qwen-limit", type=int, default=8, help="How many saved images Qwen should annotate; 0 means all.")
    parser.add_argument("--model", default="qwen3.5:0.8b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/chat")
    parser.add_argument("--skip-scene", action="store_true", help="Only run Qwen annotation on an existing image directory.")
    parser.add_argument("--skip-qwen", action="store_true", help="Only generate CARLA images.")
    parser.add_argument("--clean-output", action="store_true", help="Clean old scene images before generating new ones.")
    args = parser.parse_args()

    scene_script = os.path.join(repo_root, "carla_smoke", "scenes", "normal_driving_scene.py")
    qwen_script = os.path.join(repo_root, "carla_smoke", "pipeline", "step1_qwen_risk_annotation.py")

    if not args.skip_scene:
        scene_command = [
            sys.executable,
            scene_script,
            "--carla-root",
            args.carla_root,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--tm-port",
            str(args.tm_port),
            "--town",
            args.town,
            "--frames",
            str(args.frames),
            "--save-every",
            str(args.save_every),
            "--vehicles",
            str(args.vehicles),
            "--seed",
            str(args.seed),
            "--output-dir",
            args.image_dir,
        ]
        if args.clean_output:
            scene_command.append("--clean-output")
        run_command(scene_command)

    if not args.skip_qwen:
        qwen_command = [
            sys.executable,
            qwen_script,
            args.image_dir,
            "--model",
            args.model,
            "--url",
            args.ollama_url,
            "--limit",
            str(args.qwen_limit),
            "--output",
            args.label_output,
        ]
        run_command(qwen_command)

    print("\nPipeline finished.")
    print(f"Images: {os.path.abspath(args.image_dir)}")
    if not args.skip_qwen:
        print(f"Labels: {os.path.abspath(args.label_output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
