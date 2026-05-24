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
    DEFAULT_API_KEY_ENV,
    DeepSeekError,
    chat_json,
    get_api_key,
    parse_json_response,
)
from risk_library import retrieve_scene_risk_candidates


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


PROMPT_TEMPLATE = """你是自动驾驶风险推演系统中的 L1 子智能体。

输入包括：
1. CARLA API 已经导出的单帧 L0 场景结构化快照。L0 是几何/物理事实来源。
2. SafeBench 原始场景描述和 source 元信息。你不能声称自己直接看过图像，只能使用结构化 L0 字段。

任务：
- 结合 L0 中的 ego、weather、actors、source，推断 5 个最可能的 L1 物理风险薄弱环节。
- 输入可能包含 risk_family_candidates；这是本地交通风险库基于 L0 几何召回出的风险家族候选。L1 只能在风险家族层面选择，不选择具体 risk_type 或动作原语。
- L0 是单帧输入，不能声称有“持续靠近、跨帧出现、速度变化趋势”等多帧证据；只能说“当前单帧状态显示”。
- 如果需要最近前车、侧车、弱势交通参与者，必须从 actors 中根据 relative_position、relative_longitudinal_m、relative_lateral_m、distance_m、kind/type_id 自己判断。
- L1 只描述当前场景中的“物理风险薄弱环节”：哪里脆弱、为什么脆弱、证据是什么。
- L1 不生成具体触发事件，不写“突然急刹/绳索断裂/行人闯入”等事件；这些属于 L2。
- 候选包括但不限于：货物固定不稳、卡车刹车灯可见性不足、骑行者靠近机动车道、道路湿滑、自车A柱盲区、前方大型车辆遮挡视野、跟车距离不足、车道空间不足或避让空间受限、前车速度状态不确定。
- evidence 必须引用精简 L0 字段，例如 ego.speed_mps、weather.*、actors[i].relative_*、actors[i].type_id、source.frame。

请只输出一个 JSON 对象，不要 Markdown，不要解释性前后缀。格式必须是：
{
  "l1_risk_predictions": [
    {
      "level": "L1",
      "rank": 1,
      "name": "风险薄弱环节名称",
      "risk_type": "物理风险推测",
      "risk_family": "候选风险家族ID",
      "visibility": "由CARLA状态支持/部分由CARLA状态支持/纯推测",
      "risk_level": "低/中/高",
      "evidence": "必须引用 L0 字段",
      "weakness_reason": "为什么当前状态是薄弱环节，不要写具体触发事件",
      "boundary": "L1只识别风险家族和薄弱环节，不生成触发事件、risk_type、动作原语或具体扰动物体"
    }
  ]
}

硬性要求：
- l1_risk_predictions 必须正好 5 项，rank 从 1 到 5。
- 优先使用 L0 中有数据支撑的风险，不要胡编不可见对象。
- risk_family 必须优先来自 risk_family_candidates；不要输出 risk_type_id、primary_action_primitive_id、risk_library_candidate、actor_list、selected_actor、primary_perturbation_object、legacy_scenario_type、matched_actor_id。
- 如果风险缺少直接状态证据，visibility 必须写“纯推测”，risk_level 通常为低。
- L1 的 name 必须是状态/脆弱点，例如“跟车距离不足”，不能是事件，例如“前车突然急刹”。
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
    indexes = []
    for idx in range(sample_count):
        pos = round(idx * (len(image_paths) - 1) / (sample_count - 1))
        indexes.append(int(pos))
    return [image_paths[idx] for idx in sorted(set(indexes))]


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


def actor_min_distance(state):
    summary = state.get("summary", {}) if isinstance(state, dict) else {}
    candidates = [
        summary.get("nearest_actor_distance_m"),
        summary.get("nearest_front_distance_m"),
    ]
    actors = state.get("actors", []) if isinstance(state, dict) else []
    for actor in actors:
        if isinstance(actor, dict):
            candidates.append(actor.get("distance_m"))
    numeric = []
    for value in candidates:
        try:
            if value is not None:
                numeric.append(float(value))
        except (TypeError, ValueError):
            pass
    return min(numeric) if numeric else float("inf")


def source_for_reconstruction(state, selected_image_path=None, selected_images=None):
    source = dict(state.get("source", {}) if isinstance(state, dict) else {})
    road = state.get("road", {}) if isinstance(state, dict) and isinstance(state.get("road"), dict) else {}
    keep = {
        "frame",
        "image_file",
        "scenario_source",
        "safebench_scenic_file",
        "safebench_scenario_index",
        "scenario_description",
        "camera_mode",
        "camera_images",
        "montage_layout",
        "map",
        "source_map",
    }
    compact = {key: source.get(key) for key in keep if source.get(key) is not None}
    source_map = compact.get("source_map") or compact.get("map") or road.get("map")
    if source_map is not None:
        compact["source_map"] = source_map
        compact.setdefault("map", source_map)
    if selected_image_path:
        compact["selected_image_path"] = os.path.abspath(selected_image_path)
    return compact


def compact_l0_state(state, selected_image_path=None, selected_images=None):
    if not isinstance(state, dict):
        state = {}
    return {
        "level": "L0",
        "ego": state.get("ego", {}),
        "weather": state.get("weather", {}),
        "actors": state.get("actors", []) if isinstance(state.get("actors"), list) else [],
        "source": source_for_reconstruction(state, selected_image_path, selected_images),
    }


def is_vehicle(actor):
    kind = actor_kind(actor)
    type_id = str((actor or {}).get("type_id", "")).lower()
    return kind == "vehicle" or type_id.startswith("vehicle.")


def is_vulnerable_actor(actor):
    kind = actor_kind(actor)
    type_id = str((actor or {}).get("type_id", "")).lower()
    return kind in {"pedestrian", "walker", "cyclist", "bicycle"} or type_id.startswith("walker.")


def nearest_front_vehicle(l0_state):
    actors = l0_state.get("actors", []) if isinstance(l0_state, dict) and isinstance(l0_state.get("actors"), list) else []
    front = []
    for actor in actors:
        if not isinstance(actor, dict) or not is_vehicle(actor):
            continue
        rel_pos = str(actor.get("relative_position", "")).lower()
        rel_long = safe_float(actor.get("relative_longitudinal_m"), None)
        rel_lat = safe_float(actor.get("relative_lateral_m"), None)
        if rel_long is not None and rel_long < -1.0:
            continue
        if rel_lat is not None and abs(rel_lat) > 4.0 and "front" not in rel_pos:
            continue
        if rel_pos and "front" not in rel_pos and rel_long is None:
            continue
        front.append(actor)
    return min(front, key=actor_distance_score) if front else None


def compact_sampled_state(state):
    if not isinstance(state, dict):
        return {}
    return {
        "source": state.get("source", {}),
        "ego": state.get("ego", {}),
        "weather": state.get("weather", {}),
        "actors": state.get("actors", []),
    }


def load_l0_sequence(image_paths, state_json, ego_log, all_image_paths=None):
    if state_json:
        state = read_json(state_json)
        return state, [state]
    states = [load_l0_state(image_path, None, ego_log) for image_path in image_paths]
    representative = min(states, key=actor_min_distance) if states else fallback_state_from_ego_log(image_paths[0], ego_log)
    representative = dict(representative)
    representative.setdefault("source", {})
    return representative, states


def build_prompt(l0_state, scenario_hint, scene_risk_candidates=None):
    payload = {
        "l0_state_snapshot": l0_state,
        "scenario_hint": scenario_hint,
        "risk_family_candidates": scene_risk_candidates or [],
    }
    return PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def fallback_risks_from_state(l0_state):
    ego_speed = float(l0_state.get("ego", {}).get("speed_kmh", 0.0) or 0.0)
    nearest_front = nearest_front_vehicle(l0_state)
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
                "risk_family": "lead_vehicle_risk",
                "visibility": "由CARLA状态支持",
                "risk_level": level,
                "evidence": f"ego.speed_kmh={ego_speed}; selected front actor distance_m={distance}",
                "weakness_reason": "自车与前方目标距离较近时，制动和避让余量下降。",
                "boundary": "L1只识别薄弱环节，不生成触发事件",
            }
        )
        risks.append(
            {
                "level": "L1",
                "rank": 2,
                "name": "前方车辆遮挡视野",
                "risk_type": "物理风险推测",
                "risk_family": "lead_vehicle_risk",
                "visibility": "由CARLA状态支持",
                "risk_level": "中",
                "evidence": f"front actor type_id={nearest_front.get('type_id')}; relative_position={nearest_front.get('relative_position')}",
                "weakness_reason": "前方车辆处于自车前向区域，会压缩对更远处目标的观察空间。",
                "boundary": "L1只识别薄弱环节，不生成触发事件",
            }
        )

    if float(weather.get("wetness", 0.0) or 0.0) > 20 or float(weather.get("precipitation", 0.0) or 0.0) > 0:
        risks.append(
            {
                "level": "L1",
                "rank": len(risks) + 1,
                "name": "道路湿滑",
                "risk_type": "物理风险推测",
                "risk_family": "ego_action_risk",
                "visibility": "由CARLA状态支持",
                "risk_level": "中",
                "evidence": f"weather.wetness={weather.get('wetness')}; weather.precipitation={weather.get('precipitation')}",
                "weakness_reason": "低附着会增大制动距离并降低横向稳定性。",
                "boundary": "L1只识别薄弱环节，不生成触发事件",
            }
        )

    if any(is_vulnerable_actor(actor) for actor in actors if isinstance(actor, dict)):
        risks.append(
            {
                "level": "L1",
                "rank": len(risks) + 1,
                "name": "行人或骑行者靠近机动车道",
                "risk_type": "物理风险推测",
                "risk_family": "vru_risk",
                "visibility": "由CARLA状态支持",
                "risk_level": "中",
                "evidence": "actors 中存在 pedestrian 类型参与者",
                "weakness_reason": "弱势交通参与者轨迹不确定性高。",
                "boundary": "L1只识别薄弱环节，不生成触发事件",
            }
        )

    fallback = [
        ("前车速度状态不确定", "lead_vehicle_risk", "前方车辆速度状态可能变化，作为低置信候选"),
        ("车道空间不足或避让空间受限", "side_vehicle_risk", "附近 actor 数量和车道关系可能压缩避让空间"),
        ("自车A柱盲区", "vru_risk", "CARLA 状态未直接给出视觉遮挡，作为低置信候选"),
        ("卡车刹车灯可见性不足", "lead_vehicle_risk", "CARLA 状态未直接给出刹车灯观测，作为低置信候选"),
        ("货物固定不稳", "cargo_payload_risk", "CARLA 状态未直接给出货物约束，作为低置信候选"),
    ]
    for name, family, evidence in fallback:
        if len(risks) >= 5:
            break
        risks.append(
            {
                "level": "L1",
                "rank": len(risks) + 1,
                "name": name,
                "risk_type": "物理风险推测",
                "risk_family": family,
                "visibility": "纯推测",
                "risk_level": "低",
                "evidence": evidence,
                "weakness_reason": "补齐为可展开的 L1 候选节点。",
                "boundary": "L1只识别薄弱环节，不生成触发事件",
            }
        )

    for idx, risk in enumerate(risks[:5], start=1):
        risk["rank"] = idx
    return risks[:5]


def actor_kind(actor):
    kind = str((actor or {}).get("kind", "")).lower()
    type_id = str((actor or {}).get("type_id", "")).lower()
    if kind:
        return kind
    if type_id.startswith("vehicle."):
        return "vehicle"
    if type_id.startswith("walker."):
        return "pedestrian"
    return type_id


def actor_distance_score(actor):
    for key in ("distance_m", "relative_longitudinal_m"):
        try:
            return abs(float(actor.get(key)))
        except (TypeError, ValueError):
            continue
    return 9999.0


def safe_float(value, default=99.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def risk_text(risk):
    if not isinstance(risk, dict):
        return str(risk)
    fields = [
        risk.get("name"),
        risk.get("evidence"),
        risk.get("weakness_reason"),
        risk.get("risk_type"),
        risk.get("risk_family"),
    ]
    return " ".join(str(value) for value in fields if value)


def family_candidates_from_scene_candidates(candidates):
    families = {}
    for candidate in candidates or []:
        if not isinstance(candidate, dict) or not candidate.get("risk_family"):
            continue
        family = candidate["risk_family"]
        item = families.setdefault(
            family,
            {
                "risk_family": family,
                "score": 0,
                "reason": [],
            },
        )
        item["score"] = max(item["score"], candidate.get("score", 0))
        reason = candidate.get("reason")
        if reason and reason not in item["reason"]:
            item["reason"].append(reason)
    return sorted(families.values(), key=lambda item: item.get("score", 0), reverse=True)


def choose_family_for_text(text, family_candidates):
    allowed = [item.get("risk_family") for item in family_candidates or [] if isinstance(item, dict)]
    if not allowed:
        return None
    text = str(text or "")
    family_keywords = {
        "lead_vehicle_risk": ["前车", "跟车", "前方车辆", "刹车", "减速", "停滞", "遮挡"],
        "vru_risk": ["行人", "骑行", "自行车", "弱势", "A柱", "盲区"],
        "side_vehicle_risk": ["侧方", "侧车", "变道", "车道空间", "避让空间"],
        "obstacle_risk": ["障碍", "静止", "阻挡", "路径"],
        "cargo_payload_risk": ["货物", "掉落", "固定", "载荷"],
        "ego_action_risk": ["自车", "湿滑", "反应", "制动不足"],
    }
    best = None
    best_score = -1
    for family in allowed:
        score = sum(1 for keyword in family_keywords.get(family, []) if keyword in text)
        if score > best_score:
            best = family
            best_score = score
    return best or allowed[0]


def strip_l1_for_output(risk, family_candidates):
    allowed_keys = {
        "level",
        "rank",
        "name",
        "risk_type",
        "risk_family",
        "visibility",
        "risk_level",
        "evidence",
        "weakness_reason",
        "boundary",
    }
    cleaned = {key: risk.get(key) for key in allowed_keys if risk.get(key) is not None}
    if cleaned.get("risk_family") not in {item.get("risk_family") for item in family_candidates or [] if isinstance(item, dict)}:
        cleaned["risk_family"] = choose_family_for_text(risk_text(risk), family_candidates) or cleaned.get("risk_family")
    return cleaned


def normalize_risks(parsed, l0_state, scene_risk_candidates=None):
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
        if "weakness_reason" not in risk and "reason" in risk:
            risk["weakness_reason"] = risk.pop("reason")
        risk.pop("trigger", None)
        risk.setdefault("boundary", "L1只识别风险家族和薄弱环节，不生成触发事件、risk_type、动作原语或具体扰动物体")
        risk["rank"] = idx
        normalized.append(strip_l1_for_output(risk, scene_risk_candidates or []))

    if len(normalized) != 5:
        raise ValueError(f"L1 LLM output must contain exactly 5 risks, got {len(normalized)}")

    for idx, risk in enumerate(normalized[:5], start=1):
        risk["rank"] = idx
    return normalized[:5]


def main():
    parser = argparse.ArgumentParser(description="CARLA API L0 state + DeepSeek L1 risk agent.")
    parser.add_argument("path", help="Image file or directory from the CARLA scene output.")
    parser.add_argument("--model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--api-key", default=None, help="Explicit API key. Prefer .env/API_KEY_ENV for shared runs.")
    parser.add_argument("--env-file", default=None, help="Optional .env path. Defaults to searching upward from cwd.")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--select", choices=["first", "middle", "last"], default="middle")
    parser.add_argument("--image-index", type=int, default=None)
    parser.add_argument("--sample-count", type=int, default=1, help="Number of evenly sampled frames used for L1 risk inference.")
    parser.add_argument("--state-json", default=None, help="Optional explicit CARLA API state JSON.")
    parser.add_argument("--ego-log", default=None, help="Fallback only when no state JSON exists.")
    parser.add_argument("--scenario-hint", default="")
    parser.add_argument("--output-dir", default="carla_smoke/workdir/manual/l0")
    args = parser.parse_args()

    image_paths = list(iter_image_paths(args.path))
    try:
        selected_images = select_images(image_paths, args.select, args.image_index, args.sample_count)
    except (ValueError, IndexError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    raw_l0_state, _ = load_l0_sequence(selected_images, args.state_json, args.ego_log, image_paths)
    raw_l0_state.setdefault("source", {})
    representative_frame = raw_l0_state.get("source", {}).get("frame")
    image_path = next(
        (path for path in selected_images if frame_from_image_name(path) == representative_frame),
        selected_images[0],
    )
    l0_state = compact_l0_state(
        raw_l0_state,
        selected_image_path=image_path,
        selected_images=selected_images,
    )
    internal_scene_risk_candidates = retrieve_scene_risk_candidates(
        l0_state,
        text=args.scenario_hint,
        top_k=8,
    )
    risk_family_candidates = family_candidates_from_scene_candidates(internal_scene_risk_candidates)
    prompt = build_prompt(l0_state, args.scenario_hint, risk_family_candidates)

    print(f"L0 state source: {l0_state.get('source', {}).get('sensor', 'unknown')}")
    print("Selected images: " + ", ".join(selected_images))

    state_path = os.path.join(args.output_dir, "state.json")
    risks_path = os.path.join(args.output_dir, "risks.json")
    raw_path = os.path.join(args.output_dir, "deepseek_raw.json")
    error_path = os.path.join(args.output_dir, "deepseek_error.json")

    raw_response = None
    fallback_used = False
    fallback_reason = None
    try:
        api_key = get_api_key(args.api_key_env, args.env_file, args.api_key)
        raw_response = chat_json(args.url, args.model, api_key, prompt, args.timeout)
        parsed = parse_json_response(raw_response)
        risks = normalize_risks(parsed, l0_state, risk_family_candidates)
    except (DeepSeekError, ValueError, json.JSONDecodeError) as exc:
        fallback_used = True
        fallback_reason = repr(exc)
        print(f"WARNING: L0 DeepSeek failed; using local risk-family fallback: {exc}", file=sys.stderr)
        risks = fallback_risks_from_state(l0_state)

    write_json(state_path, l0_state)
    write_json(
        risks_path,
        {
            "source_state_file": os.path.abspath(state_path),
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "risks": risks,
        },
    )
    if raw_response is not None:
        write_json(raw_path, {"raw_response": raw_response, "risk_family_candidates": risk_family_candidates})
    if fallback_used:
        write_json(
            error_path,
            {
                "model": args.model,
                "url": args.url,
                "error": fallback_reason,
                "risk_family_candidates": risk_family_candidates,
                "fallback": "fallback_risks_from_state",
            },
        )

    print(f"Saved L0 state: {os.path.abspath(state_path)}")
    print(f"Saved L1 risks: {os.path.abspath(risks_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
