from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if not (SOURCE_ROOT / "backup_transfer_recovery.py").is_file():
    SOURCE_ROOT = REPOSITORY_ROOT.parent
sys.path.insert(0, str(SOURCE_ROOT))

from backup_transfer_recovery import (  # noqa: E402
    MEDIA_KEYS,
    METADATA_KEYS,
    TransferRecoveryManager,
    _rewrite_portrait_metadata_paths,
)


class TestLayout:
    def __init__(self, root: Path) -> None:
        self.active_layout = type("ActiveLayout", (), {"root": root})()
        self.paths = {
            key: root / "config" / f"{key}.json"
            for key in METADATA_KEYS
        }
        self.paths.update({
            "artist_portraits_file": root / "config" / "artist_portraits.json",
            "artist_portraits_dir": root / "media" / "artist_portraits",
            "favorite_duel_archive_dir": root / "media" / "favorite_duels",
            "comparison_images_dir": root / "media" / "comparison_images",
        })

    def path(self, key: str) -> Path:
        return self.paths[key]


def write_fixture(layout: TestLayout) -> tuple[Path, Path]:
    portrait = layout.path("artist_portraits_dir") / "artist_test.jpg"
    comparison = layout.path("comparison_images_dir") / "compare_test.png"
    portrait.parent.mkdir(parents=True, exist_ok=True)
    comparison.parent.mkdir(parents=True, exist_ok=True)
    portrait.write_bytes(b"portrait")
    comparison.write_bytes(b"comparison")
    metadata = {
        "test artist": {
            "artist": "test artist",
            "portrait_path": str(portrait),
            "source_path": str(comparison),
            "history_number": 1,
        }
    }
    metadata_path = layout.path("artist_portraits_file")
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return portrait, comparison


with tempfile.TemporaryDirectory(prefix="artist-ranker-portability-") as temp_name:
    root = Path(temp_name)
    source_layout = TestLayout(root / "old-data-location")
    old_portrait, old_comparison = write_fixture(source_layout)
    source_manager = TransferRecoveryManager(root / "program", source_layout, "2.5.3")

    _report, export_path = source_manager.create_export("complete", "portability-test")
    with zipfile.ZipFile(export_path) as archive:
        manifest = json.loads(archive.read("artist_ranker_transfer_manifest.json"))
        portrait_metadata = json.loads(
            archive.read("data/artist_portraits_file/artist_portraits.json")
        )
    exported = portrait_metadata["test artist"]
    assert manifest["portable_media_references"] is True
    assert manifest["portable_media_reference_version"] == 1
    assert exported["portrait_path"] == "artist_portraits/artist_test.jpg"
    assert exported["source_path"] == "comparison_images/compare_test.png"
    assert str(source_layout.active_layout.root) not in json.dumps(portrait_metadata)

    destination_layout = TestLayout(root / "new-data-location")
    destination_manager = TransferRecoveryManager(root / "program", destination_layout, "2.5.3")
    _preview, plan = destination_manager.preview_import(export_path)
    result = destination_manager.apply_import(plan, "replace", True)
    assert result.startswith("✅ Import applied:"), result

    restored_metadata = json.loads(
        destination_layout.path("artist_portraits_file").read_text(encoding="utf-8")
    )["test artist"]
    expected_portrait = destination_layout.path("artist_portraits_dir") / old_portrait.name
    expected_comparison = destination_layout.path("comparison_images_dir") / old_comparison.name
    assert restored_metadata["portrait_path"] == str(expected_portrait.resolve())
    assert restored_metadata["source_path"] == str(expected_comparison.resolve())
    assert expected_portrait.read_bytes() == b"portrait"
    assert expected_comparison.read_bytes() == b"comparison"

    # A 2.5.2-style absolute-path payload is also relocated safely.
    legacy_payload = {
        "legacy": {
            "portrait_path": r"D:\old\Data\media\artist_portraits\legacy.jpg",
            "source_path": r"D:\old\Data\media\comparison_images\legacy.png",
        }
    }
    rewritten, count = _rewrite_portrait_metadata_paths(
        legacy_payload,
        portrait_root=destination_layout.path("artist_portraits_dir"),
        comparison_root=destination_layout.path("comparison_images_dir"),
    )
    assert count == 2
    assert rewritten["legacy"]["portrait_path"] == str(
        (destination_layout.path("artist_portraits_dir") / "legacy.jpg").resolve()
    )
    assert rewritten["legacy"]["source_path"] == str(
        (destination_layout.path("comparison_images_dir") / "legacy.png").resolve()
    )

print("TRANSFER_PORTABILITY_OK")
