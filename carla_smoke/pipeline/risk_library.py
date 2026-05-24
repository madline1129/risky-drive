#!/usr/bin/env python3
"""Local structured traffic-risk library helpers.

The first version deliberately avoids vector search. It scores a small local
library using actor type, ego-relative geometry, speed, and simple text cues so
the L1-L4 agents can inherit stable risk/action IDs.
"""

import json
import os
import re


def repo_root_from_this_file():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def default_library_path():
    return os.path.join(repo_root_from_this_file(), "carla_smoke", "risk_library", "traffic_risk_library.json")


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_library(path=None):
    return read_json(path or default_library_path())


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def actor_id(actor):
    if not isinstance(actor, dict):
        return None
    return actor.get("actor_id", actor.get("id"))


def actor_kind(actor):
    if not isinstance(actor, dict):
        return ""
    kind = str(actor.get("kind") or "").lower()
    type_id = str(actor.get("type_id") or "").lower()
    if kind:
        return kind
    if type_id.startswith("walker."):
        return "pedestrian"
    if type_id.startswith("vehicle."):
        return "vehicle"
    return kind


def kind_matches(actor, allowed):
    if not allowed:
        return True
    kind = actor_kind(actor)
    type_id = str(actor.get("type_id") or "").lower() if isinstance(actor, dict) else ""
    for item in allowed:
        item = str(item).lower()
        if item == kind:
            return True
        if item == "walker" and type_id.startswith("walker."):
            return True
        if item == "vehicle" and type_id.startswith("vehicle."):
            return True
        if item in {"pedestrian", "cyclist"} and kind in {"pedestrian", "walker", "cyclist"}:
            return True
        if item == "ego" and kind == "ego":
            return True
    return False


def text_blob(*items):
    return " ".join(str(item) for item in items if item)


def keyword_score(text, keywords):
    if not text or not keywords:
        return 0
    return sum(1 for keyword in keywords if str(keyword) in text)


def relative_position_matches(actor, allowed):
    if not allowed:
        return True
    return str(actor.get("relative_position") or "") in set(allowed)


def geometry_score(actor, match):
    if not isinstance(actor, dict):
        return 0
    score = 0
    if relative_position_matches(actor, match.get("relative_positions") or match.get("hazard_relative_positions")):
        score += 2
    longitudinal = safe_float(actor.get("relative_longitudinal_m"))
    if longitudinal is not None:
        max_long = safe_float(match.get("max_longitudinal_m"))
        if max_long is None or (longitudinal >= -1.0 and longitudinal <= max_long):
            score += 1
    lateral = safe_float(actor.get("relative_lateral_m"))
    if lateral is not None:
        abs_lat = abs(lateral)
        min_lat = safe_float(match.get("min_abs_lateral_m"))
        max_lat = safe_float(match.get("max_abs_lateral_m"))
        if min_lat is None or abs_lat >= min_lat:
            score += 1
        if max_lat is None or abs_lat <= max_lat:
            score += 1
    distance = safe_float(actor.get("distance_m"))
    if distance is not None and distance <= safe_float(match.get("max_distance_m"), 35.0):
        score += 1
    return score


def candidate_actor_score(risk_type, actor, text=""):
    if not isinstance(actor, dict):
        return 0
    if not kind_matches(actor, risk_type.get("actor_kinds") or risk_type.get("hazard_actor_kinds")):
        return 0
    score = 2
    score += geometry_score(actor, risk_type.get("match") or {})
    score += min(keyword_score(text, risk_type.get("keywords") or []), 3)
    return score


def best_actor_for_risk_type(risk_type, l0_state, text=""):
    actors = (l0_state or {}).get("actors") or []
    best = None
    best_score = 0
    for actor in actors:
        score = candidate_actor_score(risk_type, actor, text=text)
        if score > best_score:
            best = actor
            best_score = score
    return best, best_score


def ego_score_for_risk_type(risk_type, l0_state, text=""):
    if "ego" not in risk_type.get("actor_kinds", []):
        return None, 0
    ego = dict((l0_state or {}).get("ego") or {})
    ego["kind"] = "ego"
    hazard, hazard_score = best_actor_for_risk_type(
        {"actor_kinds": risk_type.get("hazard_actor_kinds", []), "match": risk_type.get("match", {})},
        l0_state,
        text=text,
    )
    ego_speed = safe_float(ego.get("speed_mps"), 0.0) or 0.0
    kw_score = keyword_score(text, risk_type.get("keywords") or [])
    score = hazard_score + (2 if ego_speed >= 0.5 else 0) + min(kw_score, 3)
    if kw_score == 0:
        score -= 5
    if hazard:
        ego["hazard_actor"] = hazard
    return ego, score


def summarize_candidate(risk_type, action_primitive, actor, score, reason_bits):
    candidate = {
        "risk_family": risk_type.get("family"),
        "risk_type_id": risk_type.get("id"),
        "legacy_scenario_type": risk_type.get("legacy_scenario_type"),
        "primary_action_primitive_id": risk_type.get("primary_action_primitive_id"),
        "score": score,
        "reason": "; ".join(bit for bit in reason_bits if bit),
    }
    if action_primitive:
        candidate["action_primitive"] = action_primitive
    if isinstance(actor, dict):
        candidate["matched_actor_id"] = actor_id(actor)
        candidate["matched_actor_kind"] = actor_kind(actor)
        if isinstance(actor.get("hazard_actor"), dict):
            candidate["hazard_actor_id"] = actor_id(actor["hazard_actor"])
            candidate["hazard_actor_kind"] = actor_kind(actor["hazard_actor"])
    return candidate


def retrieve_scene_risk_candidates(l0_state, text="", top_k=6, library=None):
    library = library or load_library()
    action_by_id = {item.get("id"): item for item in library.get("action_primitives", []) if isinstance(item, dict)}
    candidates = []
    for risk_type in library.get("risk_types", []):
        if not isinstance(risk_type, dict):
            continue
        if "ego" in risk_type.get("actor_kinds", []):
            actor, score = ego_score_for_risk_type(risk_type, l0_state, text=text)
        else:
            actor, score = best_actor_for_risk_type(risk_type, l0_state, text=text)
        if score <= 0:
            continue
        action = action_by_id.get(risk_type.get("primary_action_primitive_id"))
        reason = [
            f"matched {actor_kind(actor)} actor {actor_id(actor)}" if actor else "",
            f"keywords={keyword_score(text, risk_type.get('keywords') or [])}" if text else "",
        ]
        candidates.append(summarize_candidate(risk_type, action, actor, score, reason))
    candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
    return candidates[:top_k]


def _candidate_text(candidate):
    return text_blob(
        candidate.get("risk_family"),
        candidate.get("risk_type_id"),
        candidate.get("legacy_scenario_type"),
        candidate.get("primary_action_primitive_id"),
        candidate.get("reason"),
    )


def select_candidate_for_text(text, candidates):
    if not candidates:
        return None
    normalized = str(text or "")
    best = None
    best_score = -1
    for candidate in candidates:
        score = candidate.get("score", 0)
        score += keyword_score(normalized, re.split(r"[_\s;:,-]+", _candidate_text(candidate)))
        if score > best_score:
            best = candidate
            best_score = score
    return best


def attach_candidate_fields(target, candidate):
    if not isinstance(target, dict) or not isinstance(candidate, dict):
        return target
    for key in (
        "risk_family",
        "risk_type_id",
        "legacy_scenario_type",
        "primary_action_primitive_id",
        "matched_actor_id",
        "hazard_actor_id",
    ):
        if candidate.get(key) is not None and key not in target:
            target[key] = candidate[key]
    if "risk_library_candidate" not in target:
        target["risk_library_candidate"] = candidate
    return target


def action_primitive_by_id(action_primitive_id, library=None):
    if not action_primitive_id:
        return None
    library = library or load_library()
    for item in library.get("action_primitives", []):
        if isinstance(item, dict) and item.get("id") == action_primitive_id:
            return item
    return None


def risk_type_by_id(risk_type_id, library=None):
    if not risk_type_id:
        return None
    library = library or load_library()
    for item in library.get("risk_types", []):
        if isinstance(item, dict) and item.get("id") == risk_type_id:
            return item
    return None


def risk_types_for_family(risk_family, library=None):
    if not risk_family:
        return []
    library = library or load_library()
    return [
        item
        for item in library.get("risk_types", [])
        if isinstance(item, dict) and item.get("family") == risk_family
    ]
