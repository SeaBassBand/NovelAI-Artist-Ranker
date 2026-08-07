#!/usr/bin/env python3
"""Small installation-level controller for isolated generation archives.

The ranker itself is intentionally launched against exactly one data root.  A
mode change records the desired archive and asks the supervisor to restart the
same URL with the other root, keeping every mutable ranking artifact separate.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


CONTROL_ENV = "ARTIST_RANKER_MODE_CONTROL"
MODE_ENV = "ARTIST_RANKER_GENERATION_MODE"
MODES = {"novelai", "local"}


def _normalize_mode(value: Any) -> str:
    mode = str(value or "").strip().casefold()
    return mode if mode in MODES else "novelai"


def _read(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class GenerationModeControl:
    """Read and update the supervisor contract without touching either archive."""

    SCHEMA_VERSION = 1

    def __init__(self, program_dir: Path, path: Optional[Path] = None):
        explicit = str(os.environ.get(CONTROL_ENV, "") or "").strip()
        root = Path(program_dir).resolve().parent
        self.path = Path(path or explicit or (root / "generation_mode_control.json")).resolve()
        self.active_mode = _normalize_mode(os.environ.get(MODE_ENV) or self.state().get("selected_mode"))

    def state(self) -> Dict[str, Any]:
        raw = _read(self.path)
        return {
            "schema_version": self.SCHEMA_VERSION,
            "selected_mode": _normalize_mode(raw.get("selected_mode")),
            "restart_requested": bool(raw.get("restart_requested", False)),
            "request_id": str(raw.get("request_id", "") or ""),
            "requested_at": float(raw.get("requested_at", 0.0) or 0.0),
        }

    def ensure(self, default_mode: str = "novelai") -> Dict[str, Any]:
        if self.path.exists():
            return self.state()
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "selected_mode": _normalize_mode(default_mode),
            "restart_requested": False,
            "request_id": "",
            "requested_at": 0.0,
        }
        _atomic_json(self.path, payload)
        return self.state()

    def request_switch(self, target_mode: str) -> Dict[str, Any]:
        target = _normalize_mode(target_mode)
        if target == self.active_mode:
            return {**self.state(), "active_mode": self.active_mode, "changed": False}
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "selected_mode": target,
            "restart_requested": True,
            "request_id": uuid.uuid4().hex,
            "requested_at": time.time(),
        }
        _atomic_json(self.path, payload)
        return {**payload, "active_mode": self.active_mode, "changed": True}

    def public_status(self) -> Dict[str, Any]:
        state = self.state()
        return {
            "active_mode": self.active_mode,
            "active_label": "NovelAI" if self.active_mode == "novelai" else "Local diffuser",
            "selected_mode": state["selected_mode"],
            "restart_requested": state["restart_requested"],
            "request_id": state["request_id"],
            "control_path": str(self.path),
        }

