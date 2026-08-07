"""Generation-profile schema and validation for NovelAI Artist Ranker Phase 3.

This module intentionally contains no API credentials and performs no network I/O.
It centralizes profile ownership, standard resolutions, sampler/scheduler choices,
and backwards-compatible migration of the former saved-prompt format.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Mapping, Tuple

PROFILE_SCHEMA_VERSION = 3

OWNERSHIP_PROMPT = "prompt_only"
OWNERSHIP_PROMPT_RESOLUTION = "prompt_resolution"
OWNERSHIP_FULL = "full"
OWNERSHIP_CHOICES = (
    ("Prompt only", OWNERSHIP_PROMPT),
    ("Prompt + resolution", OWNERSHIP_PROMPT_RESOLUTION),
    ("Complete configuration", OWNERSHIP_FULL),
)
OWNERSHIP_LABELS = {value: label for label, value in OWNERSHIP_CHOICES}

# These are the standard NovelAI resolution presets used by the ranker. Keep all
# dimensions here so desktop, Android, profile migration, and generation agree.
RESOLUTION_PRESETS: Dict[str, Dict[str, Any]] = {
    "portrait": {"label": "Portrait — 832 × 1216", "width": 832, "height": 1216},
    "landscape": {"label": "Landscape — 1216 × 832", "width": 1216, "height": 832},
    "square": {"label": "Square — 1024 × 1024", "width": 1024, "height": 1024},
    "custom": {"label": "Custom", "width": None, "height": None},
}
RESOLUTION_CHOICES = tuple(
    (value["label"], key) for key, value in RESOLUTION_PRESETS.items()
)

SAMPLER_CHOICES = (
    ("Euler Ancestral", "k_euler_ancestral"),
    ("DPM++ 2M", "k_dpmpp_2m"),
    ("Euler", "k_euler"),
    ("DPM2", "k_dpm_2"),
    ("DPM++ 2S Ancestral", "k_dpmpp_2s_ancestral"),
    ("DPM++ SDE", "k_dpmpp_sde"),
    ("DPM Fast", "k_dpm_fast"),
    ("DDIM", "ddim"),
)
SAMPLER_LABELS = {value: label for label, value in SAMPLER_CHOICES}
VALID_SAMPLERS = set(SAMPLER_LABELS)

SCHEDULER_CHOICES = (
    ("Automatic / sampler default", "default"),
    ("Karras", "karras"),
    ("Exponential", "exponential"),
    ("Polyexponential", "polyexponential"),
)
SCHEDULER_LABELS = {value: label for label, value in SCHEDULER_CHOICES}
VALID_SCHEDULERS = set(SCHEDULER_LABELS)

_SCHEDULERS_BY_SAMPLER = {
    "k_euler_ancestral": {"karras", "exponential", "polyexponential"},
    "k_dpmpp_2s_ancestral": {"karras", "exponential", "polyexponential"},
    "k_dpmpp_2m": {"karras", "exponential", "polyexponential"},
    "k_dpmpp_sde": {"karras", "exponential", "polyexponential"},
    "k_euler": {"karras", "exponential", "polyexponential"},
    "k_dpm_2": {"exponential", "polyexponential"},
}


def enum_value(value: Any, fallback: str = "") -> str:
    raw = getattr(value, "value", value)
    text = str(raw if raw is not None else fallback).strip()
    return text or fallback


def clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(fallback)
    return max(minimum, min(maximum, result))


def clamp_float(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(fallback)
    # Both NovelAI and the common local backends accept tenths for CFG. Keeping
    # one decimal place also avoids Pydantic rejecting float artifacts.
    return round(max(minimum, min(maximum, result)), 1)


def normalize_ownership(value: Any) -> str:
    text = str(value or OWNERSHIP_PROMPT).strip().casefold()
    aliases = {
        "prompt": OWNERSHIP_PROMPT,
        "prompt only": OWNERSHIP_PROMPT,
        "prompt_only": OWNERSHIP_PROMPT,
        "prompt+resolution": OWNERSHIP_PROMPT_RESOLUTION,
        "prompt + resolution": OWNERSHIP_PROMPT_RESOLUTION,
        "prompt_resolution": OWNERSHIP_PROMPT_RESOLUTION,
        "complete": OWNERSHIP_FULL,
        "complete configuration": OWNERSHIP_FULL,
        "full": OWNERSHIP_FULL,
    }
    return aliases.get(text, OWNERSHIP_PROMPT)


def infer_resolution_preset(width: Any, height: Any) -> str:
    try:
        pair = (int(width), int(height))
    except (TypeError, ValueError):
        return "custom"
    for key, preset in RESOLUTION_PRESETS.items():
        if preset["width"] is not None and pair == (preset["width"], preset["height"]):
            return key
    return "custom"


def resolve_resolution(preset: Any, width: Any, height: Any, *, fallback_width: int, fallback_height: int) -> Tuple[str, int, int]:
    key = str(preset or "").strip().casefold()
    if key not in RESOLUTION_PRESETS:
        key = infer_resolution_preset(width, height)
    if key != "custom":
        entry = RESOLUTION_PRESETS[key]
        return key, int(entry["width"]), int(entry["height"])
    normalized_width = clamp_int(width, 64, 4096, fallback_width)
    normalized_height = clamp_int(height, 64, 4096, fallback_height)
    # NovelAI dimensions are normally multiples of 64. Snap custom values to the
    # nearest valid multiple to prevent accidental API validation failures.
    normalized_width = max(64, min(4096, int(round(normalized_width / 64.0)) * 64))
    normalized_height = max(64, min(4096, int(round(normalized_height / 64.0)) * 64))
    inferred = infer_resolution_preset(normalized_width, normalized_height)
    return inferred if inferred != "custom" else "custom", normalized_width, normalized_height


def normalize_sampler(value: Any, fallback: str = "k_euler_ancestral") -> str:
    text = enum_value(value, fallback).strip().casefold()
    return text if text in VALID_SAMPLERS else fallback


def normalize_scheduler(value: Any, sampler: Any) -> str:
    sampler_id = normalize_sampler(sampler)
    text = str(value or "default").strip().casefold()
    if text not in VALID_SCHEDULERS:
        text = "default"
    if text != "default" and text not in _SCHEDULERS_BY_SAMPLER.get(sampler_id, set()):
        return "default"
    return text


def default_generation_settings(
    *, width: int, height: int, steps: int, sampler: Any, cfg_scale: float = 6.0,
) -> Dict[str, Any]:
    sampler_id = normalize_sampler(sampler)
    preset, width, height = resolve_resolution(
        infer_resolution_preset(width, height), width, height,
        fallback_width=width, fallback_height=height,
    )
    return {
        "positive_prompt_text": "",
        "negative_prompt_text": "",
        "quality_toggle": True,
        "uc_preset": 0,
        "resolution_preset": preset,
        "width": width,
        "height": height,
        "steps": clamp_int(steps, 1, 50, 28),
        "cfg_scale": clamp_float(cfg_scale, 0.0, 10.0, 6.0),
        "sampler": sampler_id,
        "scheduler": normalize_scheduler("default", sampler_id),
        "decrisp_mode": False,
        "variety_boost": False,
        "rotation_enabled": False,
        "rotation_names": [],
    }


def normalize_generation_settings(raw: Any, defaults: Mapping[str, Any]) -> Dict[str, Any]:
    data = dict(defaults)
    if isinstance(raw, Mapping):
        data.update(raw)
    preset, width, height = resolve_resolution(
        data.get("resolution_preset"), data.get("width"), data.get("height"),
        fallback_width=int(defaults.get("width", 832)),
        fallback_height=int(defaults.get("height", 1216)),
    )
    sampler = normalize_sampler(data.get("sampler"), normalize_sampler(defaults.get("sampler")))
    scheduler = normalize_scheduler(data.get("scheduler"), sampler)
    rotation_names = data.get("rotation_names", [])
    if not isinstance(rotation_names, list):
        rotation_names = []
    return {
        **data,
        "positive_prompt_text": str(data.get("positive_prompt_text", "") or ""),
        "negative_prompt_text": str(data.get("negative_prompt_text", "") or ""),
        "quality_toggle": bool(data.get("quality_toggle", True)),
        "uc_preset": clamp_int(data.get("uc_preset", 0), -1, 3, 0),
        "resolution_preset": preset,
        "width": width,
        "height": height,
        "steps": clamp_int(data.get("steps", defaults.get("steps", 28)), 1, 50, int(defaults.get("steps", 28))),
        "cfg_scale": clamp_float(
            data.get("cfg_scale", defaults.get("cfg_scale", 6.0)),
            0.0,
            10.0,
            float(defaults.get("cfg_scale", 6.0)),
        ),
        "sampler": sampler,
        "scheduler": scheduler,
        "decrisp_mode": bool(data.get("decrisp_mode", False)),
        "variety_boost": bool(data.get("variety_boost", False)),
        "rotation_enabled": bool(data.get("rotation_enabled", False)),
        "rotation_names": [str(name) for name in rotation_names if str(name).strip()],
    }


def normalize_profile(value: Any, defaults: Mapping[str, Any]) -> Tuple[Dict[str, Any], bool]:
    source = dict(value) if isinstance(value, Mapping) else {}
    migrated = not bool(source.get("schema_version")) or "ownership" not in source
    ownership = normalize_ownership(source.get("ownership", OWNERSHIP_PROMPT))
    settings = normalize_generation_settings(source, defaults)
    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "ownership": ownership,
        "positive": str(source.get("positive", source.get("positive_prompt_text", "")) or ""),
        "negative": str(source.get("negative", source.get("negative_prompt_text", "")) or ""),
        "resolution_preset": settings["resolution_preset"],
        "width": settings["width"],
        "height": settings["height"],
        "steps": settings["steps"],
        "cfg_scale": settings["cfg_scale"],
        "sampler": settings["sampler"],
        "scheduler": settings["scheduler"],
        "uc_preset": settings["uc_preset"],
        "quality_toggle": settings["quality_toggle"],
        "decrisp_mode": settings["decrisp_mode"],
        "variety_boost": settings["variety_boost"],
        "notes": str(source.get("notes", "") or "").strip(),
    }
    if source != profile:
        migrated = True
    return profile, migrated


def clean_profile_payload(data: Any, defaults: Mapping[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    raw = data.get("profiles", data.get("presets", data)) if isinstance(data, Mapping) else {}
    if not isinstance(raw, Mapping):
        return {}, False
    profiles: Dict[str, Dict[str, Any]] = {}
    migrated = bool(isinstance(data, Mapping) and "profiles" not in data)
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, Mapping):
            continue
        clean_name = name.strip()
        if not clean_name:
            continue
        profile, changed = normalize_profile(value, defaults)
        profiles[clean_name] = profile
        migrated = migrated or changed
    return profiles, migrated


def build_profile(
    *, ownership: Any, positive: Any, negative: Any, resolution_preset: Any,
    width: Any, height: Any, steps: Any, cfg_scale: Any, sampler: Any, scheduler: Any,
    uc_preset: Any, quality_toggle: Any, decrisp_mode: Any,
    variety_boost: Any, notes: Any, defaults: Mapping[str, Any],
) -> Dict[str, Any]:
    raw = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "ownership": ownership,
        "positive": positive,
        "negative": negative,
        "resolution_preset": resolution_preset,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg_scale": cfg_scale,
        "sampler": sampler,
        "scheduler": scheduler,
        "uc_preset": uc_preset,
        "quality_toggle": quality_toggle,
        "decrisp_mode": decrisp_mode,
        "variety_boost": variety_boost,
        "notes": notes,
    }
    profile, _ = normalize_profile(raw, defaults)
    return profile


def apply_profile(profile: Mapping[str, Any], current: Mapping[str, Any], defaults: Mapping[str, Any]) -> Dict[str, Any]:
    normalized, _ = normalize_profile(profile, defaults)
    resolved = normalize_generation_settings(current, defaults)
    resolved["positive_prompt_text"] = normalized["positive"]
    resolved["negative_prompt_text"] = normalized["negative"]
    ownership = normalized["ownership"]
    if ownership in {OWNERSHIP_PROMPT_RESOLUTION, OWNERSHIP_FULL}:
        for key in ("resolution_preset", "width", "height"):
            resolved[key] = normalized[key]
    if ownership == OWNERSHIP_FULL:
        for key in (
            "steps", "cfg_scale", "sampler", "scheduler", "uc_preset", "quality_toggle",
            "decrisp_mode", "variety_boost",
        ):
            resolved[key] = normalized[key]
    return normalize_generation_settings(resolved, defaults)


def profile_summary(name: str, profile: Mapping[str, Any], defaults: Mapping[str, Any]) -> str:
    normalized, _ = normalize_profile(profile, defaults)
    ownership = OWNERSHIP_LABELS.get(normalized["ownership"], "Prompt only")
    dimensions = f"{normalized['width']}×{normalized['height']}"
    if normalized["ownership"] == OWNERSHIP_PROMPT:
        details = "prompt only"
    elif normalized["ownership"] == OWNERSHIP_PROMPT_RESOLUTION:
        details = f"prompt + {dimensions}"
    else:
        sampler = SAMPLER_LABELS.get(normalized["sampler"], normalized["sampler"])
        details = f"{dimensions}, {normalized['steps']} steps, CFG {normalized['cfg_scale']:g}, {sampler}"
    return f"{name} — {ownership} ({details})"


def snapshot_profile(name: str | None, profile: Mapping[str, Any] | None, resolved: Mapping[str, Any], defaults: Mapping[str, Any]) -> Dict[str, Any]:
    settings = normalize_generation_settings(resolved, defaults)
    ownership = normalize_ownership(profile.get("ownership")) if isinstance(profile, Mapping) else "manual"
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_name": str(name or ""),
        "ownership": ownership,
        "positive_prompt_text": str(settings.get("positive_prompt_text", "") or ""),
        "negative_prompt_text": str(settings.get("negative_prompt_text", "") or ""),
        "resolution_preset": settings["resolution_preset"],
        "width": settings["width"],
        "height": settings["height"],
        "steps": settings["steps"],
        "cfg_scale": settings["cfg_scale"],
        "sampler": settings["sampler"],
        "scheduler": settings["scheduler"],
        "uc_preset": settings["uc_preset"],
        "quality_toggle": settings["quality_toggle"],
        "decrisp_mode": settings["decrisp_mode"],
        "variety_boost": settings["variety_boost"],
        "notes": str(profile.get("notes", "") or "") if isinstance(profile, Mapping) else "",
    }


def copy_profiles(profiles: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return deepcopy({str(name): dict(value) for name, value in profiles.items()})
