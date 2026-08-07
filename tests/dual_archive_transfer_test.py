from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from backup_transfer_recovery import METADATA_KEYS, TransferRecoveryManager  # noqa: E402
from dual_archive_transfer import DUAL_MANIFEST_NAME, DualArchiveTransferManager  # noqa: E402


class TestLayout:
    def __init__(self, root: Path):
        self.active_layout = type("ActiveLayout", (), {"root": root})()
        self.paths = {key: root / "config" / f"{key}.json" for key in METADATA_KEYS}
        self.paths.update({
            "artist_portraits_dir": root / "media" / "artist_portraits",
            "favorite_duel_archive_dir": root / "media" / "favorite_duels",
            "comparison_images_dir": root / "media" / "comparison_images",
        })

    def path(self, key: str) -> Path:
        return self.paths[key]


def write_archive_fixture(container: Path, archive: str, value: str) -> None:
    path = container / "archives" / archive / "config" / "elo_ratings_file.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"archive": value}), encoding="utf-8")


with tempfile.TemporaryDirectory(prefix="artist-ranker-dual-transfer-") as temp_name:
    root = Path(temp_name)
    source_container = root / "source"
    source_control = source_container / "generation_mode_control.json"
    source_control.parent.mkdir(parents=True, exist_ok=True)
    source_control.write_text(json.dumps({"selected_mode": "local"}), encoding="utf-8")
    write_archive_fixture(source_container, "novelai", "nai-source")
    write_archive_fixture(source_container, "local", "anima-source")
    source_layout = TestLayout(source_container / "archives" / "local")
    active = TransferRecoveryManager(root / "program", source_layout, "test", backup_root=root / "backups")
    dual = DualArchiveTransferManager(active, source_layout, root / "program", "test", source_control)

    report, export_path = dual.create_export("metadata", "dual-test")
    assert "NovelAI and Local/Anima" in report
    with zipfile.ZipFile(export_path) as archive:
        manifest = json.loads(archive.read(DUAL_MANIFEST_NAME))
        assert manifest["selected_mode"] == "local"
        assert {row["archive"] for row in manifest["archives"]} == {"novelai", "local"}

    destination_container = root / "destination"
    destination_control = destination_container / "generation_mode_control.json"
    destination_control.parent.mkdir(parents=True, exist_ok=True)
    destination_control.write_text(json.dumps({"selected_mode": "novelai"}), encoding="utf-8")
    destination_layout = TestLayout(destination_container / "archives" / "novelai")
    destination_active = TransferRecoveryManager(root / "program", destination_layout, "test", backup_root=root / "destination-backups")
    destination_dual = DualArchiveTransferManager(destination_active, destination_layout, root / "program", "test", destination_control)
    preview, plan = destination_dual.preview_import(export_path)
    assert "NovelAI + Local/Anima" in preview
    result = destination_dual.apply_import(plan, "replace", True)
    assert result.startswith("Import applied:"), result
    assert json.loads((destination_container / "archives" / "novelai" / "config" / "elo_ratings_file.json").read_text())["archive"] == "nai-source"
    assert json.loads((destination_container / "archives" / "local" / "config" / "elo_ratings_file.json").read_text())["archive"] == "anima-source"
    assert json.loads(destination_control.read_text())["selected_mode"] == "local"

print("DUAL_ARCHIVE_TRANSFER_OK")
