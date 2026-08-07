from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from generation_mode_control import CONTROL_ENV, MODE_ENV, GenerationModeControl  # noqa: E402


saved = {name: os.environ.get(name) for name in (CONTROL_ENV, MODE_ENV)}
try:
    os.environ.pop(MODE_ENV, None)
    with tempfile.TemporaryDirectory(prefix="artist-ranker-mode-") as temp_name:
        root = Path(temp_name)
        path = root / "control.json"
        os.environ[CONTROL_ENV] = str(path)
        control = GenerationModeControl(root / "src")
        assert control.ensure("local")["selected_mode"] == "local"
        assert control.active_mode == "novelai"  # active mode is fixed at process construction
        switched = control.request_switch("local")
        assert switched["changed"] is True
        assert switched["restart_requested"] is True
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["selected_mode"] == "local"
        assert persisted["request_id"]
        os.environ[MODE_ENV] = "local"
        restarted = GenerationModeControl(root / "src")
        assert restarted.active_mode == "local"
        assert restarted.request_switch("local")["changed"] is False
finally:
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

print("GENERATION_MODE_CONTROL_OK")
