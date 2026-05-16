#!/usr/bin/env python3
"""Code-agent stage for L4: turn an L3 CARLA plan into executable risk-scene images."""

import argparse
import json
import os
import shutil
import subprocess
import sys


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def select_chain(chains_data, index):
    chains = chains_data.get("initial_accident_chains", [])
    if not chains:
        raise ValueError("No initial_accident_chains found in L3 JSON.")
    if index < 0 or index >= len(chains):
        raise ValueError(f"chain-index {index} out of range; available chains: {len(chains)}")
    return chains[index]


def build_config(chain):
    plan = chain.get("carla_plan", {})
    plan.setdefault("scenario_type", "cargo_drop")
    plan.setdefault("object_type", "metal_pipe")
    plan.setdefault("trigger_frame", 45)
    plan.setdefault("initial_position", {"x": -3.2, "y": 0.0, "z": 2.4})
    plan.setdefault(
        "motion",
        {
            "mode": "scripted_projectile",
            "direction": "toward_ego",
            "back_speed_mps": 8.0,
            "lateral_drift_mps": 0.2,
            "gravity": True,
        },
    )
    return {
        "level": "L4",
        "name": "CARLA代码执行",
        "description": "将L3初始事故链转为可执行CARLA风险场景",
        "source_l3_chain_id": chain.get("id"),
        "source_l2_id": chain.get("parent_l2_id"),
        "chain_description": chain.get("chain_description"),
        "direct_physical_outcome": chain.get("direct_physical_outcome"),
        "truck_distance": 18.0,
        "trigger_frame": plan.get("trigger_frame", 45),
        "carla_plan": plan,
        "executor": "carla_smoke/scenes/risk_event_scene.py",
    }


def run_command(command):
    print("\n$ " + " ".join(command))
    subprocess.run(command, check=True)


def opencode_prompt(config_path, output_script):
    return f"""You are the L4 code agent for a CARLA risk-scenario pipeline.

Task:
- Read the L4 scenario config at:
  {config_path}
- Generate a complete executable Python script at exactly:
  {output_script}
- The script must connect to CARLA 0.9.15, spawn an ego vehicle, a front truck, and execute the requested risk event from carla_plan.
- Save front-camera images into the --output-dir argument as risk_rgb_XXXX.png.
- Keep the script self-contained. Do not require project imports.
- Support these CLI arguments: --carla-root, --host, --port, --town, --output-dir, --frames, --save-every.
- Use synchronous mode and restore original world settings in finally.
- For cargo_drop or unknown scenario types, implement a scripted projectile/drop from the rear of the front truck toward the ego lane.
- Use deterministic code. Do not ask questions. Do not write Markdown. Edit/create only the requested Python file.

The generated script will be executed by this pipeline after opencode exits.
"""


def run_opencode(args, config_path):
    opencode_bin = shutil.which(args.opencode_bin)
    if not opencode_bin:
        raise RuntimeError(
            f"opencode binary not found: {args.opencode_bin}. Install/configure opencode first, "
            "then rerun with --code-agent opencode."
        )

    workspace = os.path.join(args.output_dir, "opencode_workspace")
    os.makedirs(workspace, exist_ok=True)
    workspace_config = os.path.join(workspace, "scenario_config.json")
    shutil.copyfile(config_path, workspace_config)
    output_script = os.path.join(workspace, "generated_risk_scene.py")
    prompt = opencode_prompt(workspace_config, output_script)
    prompt_path = os.path.join(workspace, "opencode_prompt.txt")
    write_json(
        os.path.join(workspace, "opencode_inputs.json"),
        {
            "config_path": os.path.abspath(config_path),
            "workspace_config": workspace_config,
            "output_script": output_script,
        },
    )
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)

    command = [
        opencode_bin,
        "run",
        "--model",
        args.opencode_model,
        "--dir",
        workspace,
        prompt,
    ]
    run_command(command)

    if not os.path.exists(output_script):
        raise RuntimeError(f"opencode completed but did not create expected script: {output_script}")
    return output_script


def run_generated_script(args, script_path, images_dir):
    command = [
        sys.executable,
        script_path,
        "--carla-root",
        args.carla_root,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--town",
        args.town,
        "--output-dir",
        images_dir,
        "--frames",
        str(args.frames),
        "--save-every",
        str(args.save_every),
    ]
    run_command(command)


def main():
    parser = argparse.ArgumentParser(description="L4 code-agent: generate and optionally execute CARLA risk-scene code.")
    parser.add_argument("l3_json", help="Path to l3/chains.json.")
    parser.add_argument("--chain-index", type=int, default=0)
    parser.add_argument("--output-dir", default="carla_smoke/workdir/manual/l4")
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--town", default="Town03")
    parser.add_argument("--frames", type=int, default=140)
    parser.add_argument("--save-every", type=int, default=5)
    parser.add_argument("--execute", action="store_true", help="Run CARLA executor to produce risk images.")
    parser.add_argument("--code-agent", choices=["template", "opencode"], default="opencode")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-model", default="deepseek/deepseek-v4-pro")
    args = parser.parse_args()

    chains_data = read_json(args.l3_json)
    chain = select_chain(chains_data, args.chain_index)
    config = build_config(chain)
    config["code_agent"] = args.code_agent

    os.makedirs(args.output_dir, exist_ok=True)
    config_path = os.path.join(args.output_dir, "scenario_config.json")
    images_dir = os.path.join(args.output_dir, "risk_images")
    write_json(config_path, config)
    print(f"Saved L4 scenario config: {os.path.abspath(config_path)}")

    if args.execute and args.code_agent == "template":
        repo_root = repo_root_from_this_file()
        executor = os.path.join(repo_root, "carla_smoke", "scenes", "risk_event_scene.py")
        command = [
            sys.executable,
            executor,
            "--config",
            config_path,
            "--carla-root",
            args.carla_root,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--town",
            args.town,
            "--output-dir",
            images_dir,
            "--frames",
            str(args.frames),
            "--save-every",
            str(args.save_every),
        ]
        run_command(command)
    elif args.execute and args.code_agent == "opencode":
        generated_script = run_opencode(args, config_path)
        config["generated_script"] = generated_script
        write_json(config_path, config)
        run_generated_script(args, generated_script, images_dir)
    else:
        print("L4 execution skipped. Add --execute to run CARLA and save risk images.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
