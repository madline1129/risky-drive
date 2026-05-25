#!/usr/bin/env python3
"""DeepSeek subagent for L3 initial accident chains from L2 triggers."""

import argparse
import json
import os
import sys

from deepseek_client import DEFAULT_API_KEY_ENV, DEFAULT_DEEPSEEK_MODEL, DEFAULT_DEEPSEEK_URL, DeepSeekError, chat_json, get_api_key, parse_json_response
from risk_library import risk_type_by_id


L3_CHAIN_LIMIT = 4


PROMPT_TEMPLATE = """你是自动驾驶风险推演系统中的 L3 子智能体。
输入是 L2 触发事件假设 JSON，以及可选的精简单帧 L0 场景快照。

任务：
L3 初始事故链：
- 对每个 L2 触发事件，构思它导致的直接物理后果。
- L2 会携带 risk_family、risk_type_id；L3 必须继承它们，并根据 risk_type_id 选择主动作原语。
- L3 可以补充 participant_actions / accompanying_actions 描述其他参与者的响应或不响应；这是第一层真正开始组织动作原语的地方。
- L3 必须为事故链中每一个参与物体生成动作原语：主触发物体、ego、背景车、可见行人、遮挡物/静态物都要有自己的 action_primitives 项。
- L3 不是最终事故，也不是二次事故；只描述触发后第一段物理演化。
- L3 只写自然语言事故链和涉及物体清单，不生成 CARLA/Scenic 执行计划。
- L3 不输出完整 actor 快照，不输出出生地点参数；只允许用 actor_ref/role 说明事故链涉及哪些角色。具体物体选择、出生地点和动作参数由 L4 PlanAgent 根据 L0+L3 完成。
- 对于事故链涉及多个物体的情况，用 chain_participants 列清楚：谁是主扰动物体角色，谁是 ego，谁只是背景/遮挡/受影响对象。
- 如果 L0 actors 中还有可见前车、侧车、行人、障碍物等对象，即使它不是主触发对象，也必须作为 background/occluder/affected_actor 写入 chain_participants，并在 action_primitives 中说明它的动作。
- 背景对象必须标注 must_not_drive_primary_event=true，避免后续 L4/code agent 把背景对象当成主风险。
- 背景车辆默认动作原语是 vehicle_maintain_current_speed；静止/遮挡行人默认动作原语是 vru_remain_stationary；静态遮挡物/障碍物默认动作原语是 actor_remain_stationary；ego 默认动作原语是 ego_continue_without_braking。
- 对 intersection_signal_risk，事故链必须保留“路口/信号灯/让行冲突”语义：横向来车闯红灯、对向左转抢行、支路车辆未让行等，主触发角色通常是 side_vehicle/cross_vehicle/oncoming_vehicle，不要退化成普通相邻车道变道。
- 对 oncoming_vehicle_risk、merge_yield_risk、wrong_way_u_turn_risk，主动作仍按 risk_type 的 primary_action_primitive_id 组织，但 chain_description 必须明确对向/汇入/掉头/逆行的来源语义。
- L0 是单帧输入，不要把事故链写成已经观测到的多帧趋势；只能基于当前单帧距离、相对方位、速度、天气解释触发后的第一段物理演化。

例子：
- L2: 绳索断裂
- L3: 金属管失去约束，从货车后部向自车方向滑落/飞出，进入自车车道。

请只输出一个 JSON 对象，不要 Markdown，不要解释性前后缀。格式必须是：
{
  "level": "L3",
  "name": "初始事故链",
  "description": "触发事件导致的直接物理后果",
  "source_l2_file": "",
  "initial_accident_chains": [
    {
      "level": "L3",
      "id": "L3-1a",
      "parent_l2_id": "L2-1a",
      "parent_l2_trigger": "绳索断裂",
      "risk_family": "继承自L2",
      "risk_type_id": "继承自L2",
      "primary_trigger_action_id": "由risk_type_id对应的主动作原语ID",
      "chain_description": "金属管失去约束并从货车后部向自车方向飞出",
      "direct_physical_outcome": "金属管进入自车前方车道，形成紧急避让/制动障碍",
      "action_primitives": [
        {"role": "primary", "action_primitive_id": "cargo_drop_or_slide_into_path", "actor_role": "payload", "description": "主扰动物体动作"},
        {"role": "accompanying", "action_primitive_id": "ego_continue_without_braking", "actor_role": "ego", "description": "自车继续沿原车道运动"},
        {"role": "background", "action_primitive_id": "vehicle_maintain_current_speed", "actor_role": "front_vehicle", "actor_ref": "l0_actor:512", "description": "非主触发前车保持当前速度沿原车道行驶"}
      ],
      "participant_actions": [
        {"actor_role": "ego", "action_id": "ego_continue_without_braking", "description": "其他参与者动作或不动作"}
      ],
      "chain_participants": [
        {"actor_role": "ego", "role": "affected_actor", "must_drive_primary_event": false},
        {"actor_role": "front_vehicle/payload/vulnerable_actor/side_vehicle/road_obstacle", "role": "primary_actor", "must_drive_primary_event": true},
        {"actor_role": "background_or_occluder", "role": "background_or_occluder", "actor_ref": "l0_actor:512", "must_not_drive_primary_event": true}
      ]
    }
  ]
}

硬性要求：
- initial_accident_chains 最多 4 项，优先覆盖输入中的前 4 个 L2。
- 每项必须包含 chain_description、direct_physical_outcome、action_primitives、chain_participants。
- 每项必须继承 risk_family、risk_type_id，并根据 risk_type_id 写出 primary_trigger_action_id；participant_actions 可以跨 family 引用动作，但必须服务于主事故链。
- action_primitives 必须覆盖 chain_participants 中每一个 actor_role/actor_ref；不要只写主触发动作。
- 不要输出执行计划字段；L4 会单独把自然语言事故链翻译成 Scenic 执行任务。
- 不要为了“可视化明显”引入无关物体，例如非货物链条不要加入 metal_pipe。
- chain_participants 必须区分 primary_actor 和 background/occluder/affected_actor。
- 不要输出 primary_perturbation_object、risk_library_candidate、legacy_scenario_type 或完整 L0 actor 快照。
"""


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def trigger_events_from_data(data):
    if isinstance(data, dict) and isinstance(data.get("trigger_event_hypotheses"), list):
        return data["trigger_event_hypotheses"]
    if isinstance(data, list):
        return data
    return []


def event_by_l2_id(events):
    mapping = {}
    for idx, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            continue
        mapping[idx] = event
        if event.get("id") is not None:
            mapping[str(event.get("id"))] = event
    return mapping


def text_mentions_vru(*items):
    text = " ".join(str(item or "") for item in items)
    return any(token in text for token in ("行人", "pedestrian", "walker", "骑行", "自行车", "cyclist", "vru"))


def chain_mentions_vru(chain, event):
    action_text = " ".join(
        " ".join(str(item.get(key, "")) for key in ("actor_role", "description"))
        for item in (chain.get("action_primitives") or [])
        if isinstance(item, dict)
    )
    participant_text = " ".join(
        str(item.get("actor_role", ""))
        for item in (chain.get("chain_participants") or [])
        if isinstance(item, dict)
    )
    return text_mentions_vru(
        chain.get("chain_description"),
        chain.get("direct_physical_outcome"),
        chain.get("parent_l2_trigger"),
        action_text,
        participant_text,
        (event or {}).get("trigger_name"),
        (event or {}).get("immediate_effect"),
    )


def force_vru_risk_if_needed(chain, event):
    if not chain_mentions_vru(chain, event):
        return
    risk_type = risk_type_by_id(chain.get("risk_type_id") or (event or {}).get("risk_type_id")) or {}
    if any(kind in risk_type.get("actor_kinds", []) for kind in ("pedestrian", "walker", "cyclist")):
        return
    chain["risk_family"] = "vru_risk"
    text = " ".join(
        str(value or "")
        for value in (
            chain.get("chain_description"),
            chain.get("direct_physical_outcome"),
            (event or {}).get("trigger_name"),
            (event or {}).get("immediate_effect"),
        )
    )
    if any(token in text for token in ("遮挡", "盲区", "突然出现")):
        chain["risk_type_id"] = "vru_emerge_from_occlusion"
        chain["primary_trigger_action_id"] = "vru_emerge_from_occlusion_into_path"
    elif any(token in text for token in ("纵向", "向前", "沿")):
        chain["risk_type_id"] = "vru_longitudinal_intrusion"
        chain["primary_trigger_action_id"] = "vru_move_longitudinal_in_path"
    else:
        chain["risk_type_id"] = "vru_lateral_crossing"
        chain["primary_trigger_action_id"] = "vru_cross_lateral_into_path"


def inherit_event_context(chain, event, l0_data=None):
    if not isinstance(chain, dict):
        return chain
    if not isinstance(event, dict):
        event = {}
    for key in ("risk_family", "risk_type_id"):
        if event.get(key) is not None and key not in chain:
            chain[key] = event[key]
    if not chain.get("primary_trigger_action_id"):
        risk_type = risk_type_by_id(chain.get("risk_type_id") or event.get("risk_type_id")) or {}
        if risk_type.get("primary_action_primitive_id"):
            chain["primary_trigger_action_id"] = risk_type["primary_action_primitive_id"]
    force_vru_risk_if_needed(chain, event)
    if "chain_participants" not in chain:
        chain["chain_participants"] = default_chain_participants(chain)
    if "participant_actions" not in chain:
        chain["participant_actions"] = []
    chain["chain_participants"] = complete_chain_participants(chain, l0_data)
    chain["action_primitives"] = build_action_primitives(chain)
    return chain


def build_prompt(l2_data, l0_data):
    context = {
        "l0_state_snapshot": l0_data,
        "l2_trigger_event_hypotheses": l2_data,
        "primary_action_options_by_risk_type": primary_action_options_by_risk_type(l2_data),
    }
    return PROMPT_TEMPLATE + "\n\n输入 JSON：\n" + json.dumps(context, ensure_ascii=False, indent=2)


def primary_action_options_by_risk_type(l2_data):
    options = {}
    for event in trigger_events_from_data(l2_data):
        if not isinstance(event, dict) or not event.get("risk_type_id"):
            continue
        risk_type = risk_type_by_id(event.get("risk_type_id")) or {}
        options[event["risk_type_id"]] = {
            "primary_action_primitive_id": risk_type.get("primary_action_primitive_id"),
            "actor_kinds": risk_type.get("actor_kinds", []),
            "match": risk_type.get("match", {}),
        }
    return options


def actor_role_for_primary_action(action_id):
    if not action_id:
        return "primary_actor"
    if action_id.startswith("front_vehicle"):
        return "front_vehicle"
    if action_id.startswith("vru"):
        return "vulnerable_actor"
    if action_id.startswith("side_vehicle"):
        return "side_vehicle"
    if action_id.startswith("cross_vehicle"):
        return "side_vehicle"
    if action_id.startswith("oncoming_vehicle"):
        return "side_vehicle"
    if action_id.startswith("merge_vehicle"):
        return "side_vehicle"
    if action_id.startswith("driveway_vehicle"):
        return "side_vehicle"
    if action_id.startswith("vehicle_illegal_u_turn"):
        return "side_vehicle"
    if action_id.startswith("static_obstacle"):
        return "road_obstacle"
    if action_id.startswith("cargo"):
        return "payload"
    if action_id.startswith("ego"):
        return "ego"
    if action_id.startswith("weather"):
        return "environment"
    return "primary_actor"


def boolish(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def actor_kind_from_l0(actor):
    if not isinstance(actor, dict):
        return ""
    kind = str(actor.get("kind") or "").lower()
    type_id = str(actor.get("type_id") or "").lower()
    if kind:
        return kind
    if type_id.startswith("vehicle."):
        return "vehicle"
    if type_id.startswith("walker."):
        return "pedestrian"
    return ""


def is_vehicle_kind(kind):
    return kind in {"vehicle", "car", "truck", "bus", "motorcycle", "bike"}


def is_vru_kind(kind):
    return kind in {"pedestrian", "walker", "cyclist", "bicycle", "vru"}


def l0_actor_ref(actor):
    actor_id = actor.get("actor_id", actor.get("id")) if isinstance(actor, dict) else None
    if actor_id is None:
        return None
    return f"l0_actor:{actor_id}"


def role_for_l0_actor(actor):
    kind = actor_kind_from_l0(actor)
    relative_position = str((actor or {}).get("relative_position") or "").lower()
    if is_vehicle_kind(kind):
        if "front" in relative_position:
            return "front_vehicle"
        if "left" in relative_position or "right" in relative_position or "side" in relative_position:
            return "side_vehicle"
        return "background_vehicle"
    if is_vru_kind(kind):
        return "visible_pedestrian"
    if kind in {"obstacle", "static", "prop"}:
        return "road_obstacle"
    return "background_actor"


def participant_key(participant):
    if not isinstance(participant, dict):
        return None
    return participant.get("actor_ref") or participant.get("actor_role")


def participant_is_primary(participant):
    if not isinstance(participant, dict):
        return False
    role = str(participant.get("role") or "").lower()
    return role == "primary_actor" or boolish(participant.get("must_drive_primary_event"))


def observed_background_participants(l0_data, primary_role):
    actors = l0_data.get("actors") if isinstance(l0_data, dict) else []
    if not isinstance(actors, list):
        return []
    participants = []
    for actor in actors[:20]:
        if not isinstance(actor, dict):
            continue
        actor_role = role_for_l0_actor(actor)
        if actor_role == primary_role:
            continue
        actor_ref = l0_actor_ref(actor)
        if not actor_ref:
            continue
        participants.append(
            {
                "actor_ref": actor_ref,
                "actor_role": actor_role,
                "role": "background_actor",
                "must_drive_primary_event": False,
                "must_not_drive_primary_event": True,
            }
        )
    return participants


def complete_chain_participants(chain, l0_data=None):
    participants = list(chain.get("chain_participants") or [])
    if not participants:
        participants = default_chain_participants(chain)

    primary_role = actor_role_for_primary_action(chain.get("primary_trigger_action_id"))
    participants.extend(observed_background_participants(l0_data or {}, primary_role))

    cleaned = []
    seen = set()
    for item in participants:
        if not isinstance(item, dict):
            continue
        key = participant_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    return cleaned


def is_vehicle_role(actor_role):
    role = str(actor_role or "").lower()
    return "vehicle" in role or role in {"car", "truck", "bus", "background_vehicle"}


def is_vru_role(actor_role):
    role = str(actor_role or "").lower()
    return any(token in role for token in ("vru", "pedestrian", "walker", "cyclist", "bicycle"))


def default_action_for_participant(participant, chain):
    actor_role = participant.get("actor_role")
    primary_action = chain.get("primary_trigger_action_id")
    primary_role = actor_role_for_primary_action(primary_action)

    if participant_is_primary(participant) or actor_role == primary_role:
        return primary_action, "primary", "主触发物体执行主动作原语"
    if actor_role == "environment":
        return "weather_shift_to_night", "primary", "环境光照/可见度执行天气扰动动作原语"
    if actor_role == "ego":
        return "ego_continue_without_braking", "accompanying", "自车维持当前速度沿原车道继续运动"
    if is_vehicle_role(actor_role):
        return "vehicle_maintain_current_speed", "background", "背景车辆保持当前速度沿原车道行驶"
    if is_vru_role(actor_role):
        return "vru_remain_stationary", "background", "非主触发弱势交通参与者保持当前位置或当前静止状态"
    return "actor_remain_stationary", "background", "非主触发背景物体保持原位置"


def append_primitive_once(primitives, primitive):
    action_id = primitive.get("action_primitive_id")
    actor_key = primitive.get("actor_ref") or primitive.get("actor_role")
    if not action_id or not actor_key:
        return
    key = (actor_key, action_id)
    for existing in primitives:
        existing_key = (existing.get("actor_ref") or existing.get("actor_role"), existing.get("action_primitive_id"))
        if existing_key == key:
            for field in ("role", "actor_role", "actor_ref", "description"):
                if primitive.get(field) is not None and existing.get(field) is None:
                    existing[field] = primitive[field]
            return
    primitives.append(primitive)


def build_action_primitives(chain):
    primary_action = chain.get("primary_trigger_action_id")
    primitives = []
    for item in chain.get("action_primitives") or []:
        if not isinstance(item, dict):
            continue
        action_id = item.get("action_primitive_id") or item.get("action_id") or item.get("id")
        if not action_id:
            continue
        if item.get("role") == "primary" and primary_action and action_id != primary_action:
            continue
        append_primitive_once(
            primitives,
            {
                "role": item.get("role"),
                "action_primitive_id": action_id,
                "actor_role": item.get("actor_role", actor_role_for_primary_action(action_id)),
                "actor_ref": item.get("actor_ref"),
                "description": item.get("description"),
            },
        )
    if primary_action:
        append_primitive_once(
            primitives,
            {
                "role": "primary",
                "action_primitive_id": primary_action,
                "actor_role": actor_role_for_primary_action(primary_action),
                "description": "主触发动作原语",
            }
        )
    for item in chain.get("participant_actions") or []:
        if not isinstance(item, dict):
            continue
        action_id = item.get("action_primitive_id") or item.get("action_id")
        if not action_id:
            continue
        append_primitive_once(
            primitives,
            {
                "role": "accompanying",
                "action_primitive_id": action_id,
                "actor_role": item.get("actor_role", actor_role_for_primary_action(action_id)),
                "actor_ref": item.get("actor_ref"),
                "description": item.get("description"),
            }
        )
    for participant in chain.get("chain_participants") or []:
        if not isinstance(participant, dict):
            continue
        action_id, primitive_role, description = default_action_for_participant(participant, chain)
        if not action_id:
            continue
        append_primitive_once(
            primitives,
            {
                "role": primitive_role,
                "action_primitive_id": action_id,
                "actor_role": participant.get("actor_role", actor_role_for_primary_action(action_id)),
                "actor_ref": participant.get("actor_ref"),
                "description": description,
            },
        )
    return primitives


def default_chain_participants(chain):
    primary_role = actor_role_for_primary_action(chain.get("primary_trigger_action_id"))
    return [
        {"actor_role": "ego", "role": "affected_actor", "must_drive_primary_event": False},
        {"actor_role": primary_role, "role": "primary_actor", "must_drive_primary_event": True},
    ]


def strip_l3_chain(chain):
    forbidden = {
        "primary_perturbation_object",
        "risk_library_candidate",
        "legacy_scenario_type",
        "actor_list",
        "selected_actor",
        "matched_actor_id",
        "matched_actor_kind",
    }
    for key in forbidden:
        chain.pop(key, None)
    if "chain_participants" in chain:
        chain["chain_participants"] = sanitize_chain_participants(chain.get("chain_participants"))
    if "action_primitives" in chain:
        chain["action_primitives"] = sanitize_action_primitives(chain.get("action_primitives"))
    allowed = {
        "level",
        "id",
        "parent_l2_id",
        "parent_l2_trigger",
        "risk_family",
        "risk_type_id",
        "primary_trigger_action_id",
        "chain_description",
        "direct_physical_outcome",
        "action_primitives",
        "participant_actions",
        "chain_participants",
    }
    return {key: chain.get(key) for key in allowed if chain.get(key) is not None}


def sanitize_chain_participants(participants):
    cleaned = []
    for item in participants or []:
        if not isinstance(item, dict):
            continue
        cleaned.append(
            {
                key: item.get(key)
                for key in ("actor_ref", "actor_role", "role", "must_drive_primary_event", "must_not_drive_primary_event")
                if item.get(key) is not None
            }
        )
    return cleaned


def sanitize_action_primitives(primitives):
    cleaned = []
    for item in primitives or []:
        if not isinstance(item, dict):
            continue
        cleaned.append(
            {
                key: item.get(key)
                for key in ("role", "action_primitive_id", "actor_role", "actor_ref", "description")
                if item.get(key) is not None
            }
        )
    return cleaned


def sanitize_primary_action_roles(chain):
    primary_action = chain.get("primary_trigger_action_id")
    primary_role = actor_role_for_primary_action(primary_action)
    for participant in chain.get("chain_participants") or []:
        if not isinstance(participant, dict):
            continue
        if participant_is_primary(participant):
            participant["actor_role"] = primary_role
    for primitive in chain.get("action_primitives") or []:
        if not isinstance(primitive, dict):
            continue
        if primitive.get("action_primitive_id") == primary_action and primitive.get("role") == "primary":
            primitive["actor_role"] = primary_role


def normalize_output(parsed, l2_data, source_l2_file, l0_data=None):
    chains = parsed.get("initial_accident_chains", []) if isinstance(parsed, dict) else []
    normalized = []
    events = trigger_events_from_data(l2_data)[:L3_CHAIN_LIMIT]
    events_by_id = event_by_l2_id(events)
    if isinstance(chains, list):
        for idx, chain in enumerate(chains[:L3_CHAIN_LIMIT], start=1):
            if not isinstance(chain, dict):
                chain = {"chain_description": str(chain)}
            chain.setdefault("level", "L3")
            chain.setdefault("id", f"L3-{idx}")
            chain.pop("carla" + "_plan", None)
            inherit_event_context(chain, events_by_id.get(chain.get("parent_l2_id")) or events_by_id.get(idx), l0_data)
            sanitize_primary_action_roles(chain)
            normalized.append(strip_l3_chain(chain))

    if not normalized:
        raise ValueError("L3 LLM output must contain at least one initial accident chain")

    return {
        "level": "L3",
        "name": "初始事故链",
        "description": "触发事件导致的直接物理后果",
        "source_l2_file": os.path.abspath(source_l2_file),
        "initial_accident_chains": normalized[:L3_CHAIN_LIMIT],
    }


def fallback_chains_from_l2(l2_data):
    chains = []
    for idx, event in enumerate(trigger_events_from_data(l2_data)[:L3_CHAIN_LIMIT], start=1):
        if not isinstance(event, dict):
            continue
        chains.append(
            {
                "level": "L3",
                "id": f"L3-{idx}",
                "parent_l2_id": event.get("id"),
                "parent_l2_trigger": event.get("trigger_name"),
                "risk_family": event.get("risk_family"),
                "risk_type_id": event.get("risk_type_id"),
                "chain_description": f"{event.get('trigger_name', '触发事件')}发生后，风险对象进入与自车发生冲突的初始运动阶段。",
                "direct_physical_outcome": event.get("immediate_effect") or "自车与风险对象的安全距离或可避让空间被压缩。",
                "participant_actions": [],
            }
        )
    return chains


def fallback_output(l2_data, source_l2_file, l0_data=None):
    return normalize_output({"initial_accident_chains": fallback_chains_from_l2(l2_data)}, l2_data, source_l2_file, l0_data)


def main():
    parser = argparse.ArgumentParser(description="DeepSeek L3 subagent: initial accident chains from L2 triggers.")
    parser.add_argument("l2_json", help="Path to l2/triggers.json.")
    parser.add_argument("--l0-json", default=None, help="Optional l0/state.json for context.")
    parser.add_argument("--model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--url", default=DEFAULT_DEEPSEEK_URL)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--api-key", default=None, help="Explicit API key. Prefer .env/API_KEY_ENV for shared runs.")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output-dir", default="carla_smoke/workdir/manual/l3")
    args = parser.parse_args()

    l2_data = read_json(args.l2_json)
    l0_data = read_json(args.l0_json) if args.l0_json else None
    prompt = build_prompt(l2_data, l0_data)

    print(f"L3 DeepSeek input: {args.l2_json}")
    chains_path = os.path.join(args.output_dir, "chains.json")
    raw_path = os.path.join(args.output_dir, "deepseek_raw.json")
    error_path = os.path.join(args.output_dir, "deepseek_error.json")

    raw_response = None
    try:
        api_key = get_api_key(args.api_key_env, args.env_file, args.api_key)
        raw_response = chat_json(args.url, args.model, api_key, prompt, args.timeout)
        parsed = parse_json_response(raw_response)
        output = normalize_output(parsed, l2_data, args.l2_json, l0_data)
        output["fallback_used"] = False
    except (DeepSeekError, ValueError, json.JSONDecodeError) as exc:
        print(f"WARNING: L3 DeepSeek failed; using local chain fallback: {exc}", file=sys.stderr)
        output = fallback_output(l2_data, args.l2_json, l0_data)
        output["fallback_used"] = True
        output["fallback_reason"] = repr(exc)
        write_json(
            error_path,
            {
                "model": args.model,
                "url": args.url,
                "error": repr(exc),
                "fallback": "fallback_chains_from_l2",
            },
        )

    write_json(chains_path, output)
    if raw_response is not None:
        write_json(raw_path, {"raw_response": raw_response})
    print(f"Saved L3 chains: {os.path.abspath(chains_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
