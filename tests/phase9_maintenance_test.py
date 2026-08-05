from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if not (SOURCE_ROOT / "backup_transfer_recovery.py").is_file():
    SOURCE_ROOT = REPOSITORY_ROOT.parent
sys.path.insert(0, str(SOURCE_ROOT))

from backup_transfer_recovery import METADATA_KEYS, TransferRecoveryManager  # noqa: E402


class TestLayout:
    def __init__(self, root: Path) -> None:
        self.active_layout = type("ActiveLayout", (), {"root": root})()
        self.paths = {key: root / "config" / f"{key}.json" for key in METADATA_KEYS}
        self.paths.update({
            "comparison_history_file": root / "state" / "comparison_history.json",
            "favorites_file": root / "config" / "favorites.json",
            "artist_portraits_file": root / "config" / "artist_portraits.json",
            "artist_portraits_dir": root / "media" / "artist_portraits",
            "favorite_duel_archive_dir": root / "media" / "favorite_duels",
            "comparison_images_dir": root / "media" / "comparison_images",
        })

    def path(self, key: str) -> Path:
        return self.paths[key]


with tempfile.TemporaryDirectory(prefix="artist-ranker-phase9-") as temporary:
    root = Path(temporary)
    data = root / "Data"
    program = root / "Program"
    backups = root / "Backups"
    layout = TestLayout(data)
    program.mkdir()
    manager = TransferRecoveryManager(program, layout, "2.6.0", backup_root=backups)

    try:
        manager.configure_backup_root(data / "nested")
        raise AssertionError("A backup inside Data was accepted")
    except ValueError:
        pass
    try:
        manager.configure_backup_root(program / "nested")
        raise AssertionError("A backup inside Program was accepted")
    except ValueError:
        pass
    manager.configure_backup_root(backups)

    portrait = layout.path("artist_portraits_dir") / "artist_test.jpg"
    portrait.parent.mkdir(parents=True, exist_ok=True)
    portrait.write_bytes(b"portrait")
    portrait_metadata = {
        "test artist": {
            "artist": "test artist",
            "portrait_path": str(portrait),
            "source_path": str(layout.path("comparison_images_dir") / "retained_source_removed.png"),
            "source_status": "removed_by_retention",
            "portrait_sha256": "fixture",
        }
    }
    layout.path("artist_portraits_file").parent.mkdir(parents=True, exist_ok=True)
    layout.path("artist_portraits_file").write_text(json.dumps(portrait_metadata), encoding="utf-8")
    layout.path("favorites_file").write_text(json.dumps({"image": {}, "duel": {}}), encoding="utf-8")

    # Use a library larger than the owner's current 7,000-duel baseline. Missing
    # originals are explicitly retention-marked and must remain informational.
    history = [
        {
            "image_a_path": str(layout.path("comparison_images_dir") / f"compare_{index}_a.png"),
            "image_b_path": str(layout.path("comparison_images_dir") / f"compare_{index}_b.png"),
            "image_a_retained": False,
            "image_b_retained": False,
            "image_a_retention_status": "removed",
            "image_b_retention_status": "removed",
        }
        for index in range(7501)
    ]
    layout.path("comparison_history_file").parent.mkdir(parents=True, exist_ok=True)
    layout.path("comparison_history_file").write_text(json.dumps(history), encoding="utf-8")

    preview, estimate = manager.estimate_export("metadata", backups)
    assert estimate["enough_space"] is True
    assert estimate["files"] >= 3
    assert "All metadata without generated images" in preview

    started = time.perf_counter()
    report, details = manager.validate_integrity(False)
    elapsed = time.perf_counter() - started
    assert details["ok"] is True, report
    assert details["history_records"] == 7501
    assert details["history_images_removed_by_retention"] == 15002
    assert details["portrait_sources_removed_by_retention"] == 1
    assert elapsed < 10.0, elapsed

    manager.github_release = lambda _channel: {
        "tag_name": "v2.7.0",
        "_channel": "stable",
        "draft": False,
        "prerelease": False,
        "html_url": "https://github.com/SeaBassBand/NovelAI-Artist-Ranker/releases/tag/v2.7.0",
        "body": "Fixture release notes",
        "assets": [
            {"name": "NovelAI-Artist-Ranker-Update-v2.7.0.zip", "size": 123, "browser_download_url": "https://github.com/example/update.zip", "digest": "sha256:" + "a" * 64},
            {"name": "artist-ranker.apk", "size": 456, "browser_download_url": "https://github.com/example/app.apk", "digest": "sha256:" + "b" * 64},
        ],
    }
    update_report, payload = manager.github_release_status("stable")
    assert "update available" in update_report
    assert "Windows update package:** Ready" in update_report
    assert "Android APK:** Ready" in update_report
    assert json.loads(payload)["tag"] == "2.7.0"

print("PHASE9_MAINTENANCE_OK")
