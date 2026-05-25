#!/usr/bin/env python3
"""Minimal action-primitive -> Scenic generator using OpenCode.

This is intentionally isolated from the old ChatScene pipeline.
It prepares a small OpenCode workspace, asks OpenCode to edit one Scenic file,
and runs a lightweight Scenic API whitelist check on the result.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


DEFAULT_TOWN = "Town05"
DEFAULT_MAP = "/mnt/data2/whz/risky-drive/safebench/scenario/scenario_data/scenic_data/maps/Town05.xodr"
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def normalize_model(model: str) -> str:
    if "/" in model:
        return model
    if model.startswith("deepseek"):
        return f"deepseek/{model}"
    return model


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_degrees(degrees: float) -> float:
    value = float(degrees)
    while value <= -180.0:
        value += 360.0
    while value > 180.0:
        value -= 360.0
    return value


def carla_to_scenic_position(location: dict[str, Any] | None, fallback: str) -> str:
    if not isinstance(location, dict):
        return fallback
    x_value = as_float(location.get("x"))
    y_value = as_float(location.get("y"))
    if x_value is None or y_value is None:
        return fallback
    return f"({x_value:.3f} @ {-y_value:.3f})"


def carla_to_scenic_heading(rotation: dict[str, Any] | None, fallback: str) -> str:
    if not isinstance(rotation, dict):
        return fallback
    yaw_value = as_float(rotation.get("yaw"))
    if yaw_value is None:
        return fallback
    return f"{normalize_degrees(-(yaw_value + 90.0)):.3f} deg"


def actor_constructor(actor: dict[str, Any] | None) -> str:
    type_id = str((actor or {}).get("type_id") or "")
    kind = str((actor or {}).get("kind") or "").lower()
    if type_id.startswith("walker.") or kind in {"pedestrian", "walker", "vru"}:
        return "Pedestrian"
    if type_id.startswith("static.prop.") or kind in {"obstacle", "static", "prop", "object"}:
        return "Prop"
    return "Car"


def actor_blueprint(actor: dict[str, Any] | None, default: str) -> str:
    return str((actor or {}).get("type_id") or default)


def find_primitive(data: dict[str, Any], primitive_name: str) -> dict[str, Any] | None:
    for item in data.get("semantic_primitives") or []:
        if isinstance(item, dict) and item.get("primitive") == primitive_name:
            return item
    return None


def scene_context(data: dict[str, Any]) -> dict[str, Any]:
    context = find_primitive(data, "set_scene_context") or {}
    return {
        "town": context.get("town") or data.get("town") or DEFAULT_TOWN,
        "map_absolute_path": context.get("map_absolute_path") or data.get("map_absolute_path") or DEFAULT_MAP,
        "weather": context.get("weather") or data.get("weather") or {},
    }


def ego_actor(data: dict[str, Any]) -> dict[str, Any]:
    primitive = find_primitive(data, "spawn_ego") or {}
    actor = primitive.get("actor") if isinstance(primitive, dict) else None
    return actor if isinstance(actor, dict) else data.get("ego_actor") or {}


def primary_actor(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("primary_actor"), dict):
        return data["primary_actor"]
    for item in data.get("semantic_primitives") or []:
        if isinstance(item, dict) and item.get("role") == "primary_risk_actor":
            actor = item.get("actor")
            if isinstance(actor, dict):
                return actor
    return data.get("actor") if isinstance(data.get("actor"), dict) else {}


def action_primitive(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("action_primitive"), dict):
        return data["action_primitive"]
    if data.get("id"):
        return data
    return {}


def build_task(data: dict[str, Any]) -> dict[str, Any]:
    action = action_primitive(data)
    return {
        "level": "RiskyWeaverPrimitiveToScenicTask",
        "scene_context": scene_context(data),
        "risk": {
            "risk_family": data.get("risk_family"),
            "risk_type_id": data.get("risk_type_id"),
            "scenario_type": data.get("scenario_type"),
            "trigger_frame": data.get("trigger_frame") or action.get("trigger_frame") or 20,
        },
        "actors": {
            "ego": ego_actor(data),
            "primary_actor": primary_actor(data),
        },
        "action_primitive": action,
        "raw_input": data,
    }


def seed_scenic(task: dict[str, Any]) -> str:
    context = task["scene_context"]
    ego = task["actors"].get("ego") or {}
    primary = task["actors"].get("primary_actor") or {}
    action = task.get("action_primitive") or {}
    trigger_frame = int(as_float(task["risk"].get("trigger_frame"), 20) or 20)

    ego_pos = ego.get("scenic_position_expression") or carla_to_scenic_position(ego.get("location"), "(-184.000 @ -116.000)")
    ego_heading = ego.get("scenic_heading") or carla_to_scenic_heading(ego.get("rotation"), "0 deg")
    primary_pos = primary.get("scenic_position_expression") or carla_to_scenic_position(
        primary.get("location"), "(-188.000 @ -108.000)"
    )
    primary_heading = primary.get("scenic_heading") or carla_to_scenic_heading(primary.get("rotation"), "0 deg")
    primary_class = actor_constructor(primary)
    primary_default_bp = "walker.pedestrian.0001" if primary_class == "Pedestrian" else "vehicle.tesla.model3"

    lines = [
        "'''Seed Scenic file. OpenCode must edit this file in place.'''",
        f'Town = "{context["town"]}"',
        f'param map = localPath("{context["map_absolute_path"]}")',
        "param carla_map = Town",
        "model scenic.simulators.carla.model",
        "",
        f"TRIGGER_FRAME = {trigger_frame}",
        "",
        f"ego = Car at {ego_pos},",
        f"    with heading {ego_heading},",
        "    with regionContainedIn None,",
        f'    with blueprint "{actor_blueprint(ego, "vehicle.lincoln.mkz_2017")}"',
    ]

    if action.get("id") != "weather_visibility_change":
        lines.extend(
            [
                "",
                f"primary_actor = {primary_class} at {primary_pos},",
                f"    with heading {primary_heading},",
                "    with regionContainedIn None,",
                f'    with blueprint "{actor_blueprint(primary, primary_default_bp)}"',
            ]
        )

    lines.append("")
    return "\n".join(lines)


def write_opencode_config(workdir: Path, base_url: str) -> None:
    write_json(
        workdir / "opencode.json",
        {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "deepseek": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "DeepSeek",
                    "options": {
                        "baseURL": base_url,
                        "apiKey": "{env:DEEPSEEK_API_KEY}",
                    },
                    "models": {
                        "deepseek-chat": {"name": "DeepSeek Chat"},
                        "deepseek-reasoner": {"name": "DeepSeek Reasoner"},
                        "deepseek-v4-pro": {"name": "DeepSeek V4 Pro"},
                    },
                }
            },
        },
    )


def build_prompt(task_path: Path, output_path: Path) -> str:
    return f"""You are generating one Scenic scenario from one action primitive.

Inputs:
- Read task JSON: {task_path}
- Edit exactly this file in place: {output_path}

Goal:
- Convert task.action_primitive into executable Scenic code.
- Keep the ego and primary_actor declarations from the seed file unless the task is weather_visibility_change.
- Add only constants/behaviors/monitors/object behavior bindings needed for the primitive.
- Make the action dangerous/aggressive when the primitive asks for intrusion, cut-in, braking, reversing, crossing, or weather degradation.

Scenic API whitelist:
- Time is exactly simulation().currentTime. Do not use current_time, current_frame, frame_id, or time_step.
- wait has no argument. Use wait, never wait 0.05 or wait(...).
- Use do only for behaviors. Use take only for actions.
- Vehicle actions: SetThrottleAction, SetBrakeAction, SetSteerAction, SetReverseAction, SetHandBrakeAction, SetAutopilotAction, SetTrafficLightAction, SetSpeedAction, SetVelocityAction, OffsetAction.
- Walker actions: SetWalkingDirectionAction, SetWalkingSpeedAction, SetWalkAction.
- Built-in behaviors may be used with do: FollowLaneBehavior, LaneChangeBehavior, AutopilotBehavior, WalkForwardBehavior, CrossingBehavior.
- Bind behavior only in object declarations with with behavior BehaviorName(...).
- Never write require actor do Behavior().
- Constructor rule: vehicle.* -> Car, walker.* -> Pedestrian, static.prop.* -> Prop.
- Never use Point(x, y, z), carla.Location, or carla.Transform in Scenic object declarations.
- Weather change must use simulation().world.get_weather(), assign fields, then simulation().world.set_weather(weather).
- Define every behavior/monitor before the first object that references it.

Output contract:
- Edit only {output_path.name}.
- Do not write Markdown.
- Do not ask questions.
"""


def validate_scenic_api(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    checks = [
        (r"simulation\(\)\.current_time\b", "use simulation().currentTime, not current_time"),
        (r"\b(current_frame|frame_id|time_step)\b", "unknown simulation time field"),
        (r"^\s*wait\s+[^#\n]+", "Scenic wait takes no argument"),
        (r"\bdo\s+[A-Za-z_][A-Za-z0-9_]*Action\s*\(", "use take for Action calls, not do"),
        (r"\btake\s+(FollowLaneBehavior|LaneChangeBehavior|AutopilotBehavior|WalkForwardBehavior|CrossingBehavior)\s*\(", "use do for Behavior calls, not take"),
        (r"\brequire\s+\w+\s+do\b", "invalid require actor do behavior syntax"),
        (r"\bPoint\s*\(|\bcarla\.Location\s*\(|\bcarla\.Transform\s*\(", "do not use 3D CARLA constructors in Scenic"),
        (r"Car\s+at[\s\S]{0,240}with blueprint\s+\"static\.prop\.", "static.prop.* must use Prop, not Car"),
    ]
    errors = []
    for pattern, message in checks:
        if re.search(pattern, text, flags=re.MULTILINE):
            errors.append(message)
    if errors:
        raise RuntimeError("Scenic API whitelist violation: " + "; ".join(errors))


def run_opencode(args: argparse.Namespace, prompt: str, env: dict[str, str]) -> None:
    opencode_bin = shutil.which(args.opencode_bin)
    if not opencode_bin:
        raise RuntimeError(f"opencode binary not found: {args.opencode_bin}")
    command = [
        opencode_bin,
        "run",
        "--model",
        normalize_model(args.model),
        "--dir",
        str(args.workdir),
        prompt,
    ]
    subprocess.run(command, check=True, env=env)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal action primitive to Scenic generator using OpenCode.")
    parser.add_argument("input_json", nargs="?", default="action_primitive.json")
    parser.add_argument("--workdir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", default="generated_scene.scenic")
    parser.add_argument("--model", default=os.environ.get("OPENCODE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    parser.add_argument("--deepseek-base-url", default=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL))
    parser.add_argument("--no-opencode-config", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Write task, seed Scenic, and prompt without calling OpenCode.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.workdir = args.workdir.resolve()
    input_path = Path(args.input_json)
    if not input_path.is_absolute():
        input_path = args.workdir / input_path

    data = read_json(input_path)
    task = build_task(data)

    task_path = args.workdir / "opencode_task.json"
    output_path = args.workdir / args.output
    prompt_path = args.workdir / "opencode_prompt.txt"

    write_json(task_path, task)
    write_text(output_path, seed_scenic(task))
    prompt = build_prompt(task_path, output_path)
    write_text(prompt_path, prompt)
    if not args.no_opencode_config:
        write_opencode_config(args.workdir, args.deepseek_base_url)

    env = os.environ.copy()
    env.update(load_env_file(args.env_file))

    if args.dry_run:
        print(f"Wrote task: {task_path}")
        print(f"Wrote seed Scenic: {output_path}")
        print(f"Wrote prompt: {prompt_path}")
        return 0

    run_opencode(args, prompt, env)
    if not output_path.exists():
        raise RuntimeError(f"OpenCode completed but did not create {output_path}")
    validate_scenic_api(output_path)
    print(f"Generated Scenic: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
