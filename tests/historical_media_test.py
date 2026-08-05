from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if not (SOURCE_ROOT / "historical_media.py").is_file():
    SOURCE_ROOT = REPOSITORY_ROOT.parent
sys.path.insert(0, str(SOURCE_ROOT))

import historical_media as media_module  # noqa: E402
from historical_media import HistoricalMediaResolver  # noqa: E402


with tempfile.TemporaryDirectory(prefix="artist-ranker-history-media-") as temp_name:
    root = Path(temp_name)
    comparison = root / "active" / "media" / "comparison_images"
    favorites = root / "active" / "media" / "favorite_duels"
    comparison.mkdir(parents=True)
    favorites.mkdir(parents=True)

    current = comparison / "current.webp"
    current.write_bytes(b"current")
    nested = comparison / "2026" / "08" / "nested.webp"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"nested")
    unique = comparison / "library" / "unique.webp"
    unique.parent.mkdir(parents=True)
    unique.write_bytes(b"unique")
    archived = favorites / "20260805" / "images" / "favorite.webp"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(b"favorite")

    resolver = HistoricalMediaResolver(comparison, favorites)

    result = resolver.resolve(str(current))
    assert result.status == "valid" and result.method == "stored_path"
    assert Path(result.resolved_path) == current.resolve()

    result = resolver.resolve(r"D:\Storage\Programs\ArtistEloImages\current.webp")
    assert result.status == "relocated" and Path(result.resolved_path) == current.resolve()

    result = resolver.resolve(r"D:\old\root\comparison_images\2026\08\nested.webp")
    assert result.status == "relocated" and result.method == "relative_suffix"
    assert Path(result.resolved_path) == nested.resolve()

    result = resolver.resolve("comparison_images/2026/08/nested.webp")
    assert result.status == "relocated" and result.method == "portable_reference"

    result = resolver.resolve(r"D:\unrecognized\unique.webp")
    assert result.status == "relocated" and result.method == "basename_unique"
    assert Path(result.resolved_path) == unique.resolve()

    collision_a = comparison / "one" / "collision.webp"
    collision_b = comparison / "two" / "collision.webp"
    collision_a.parent.mkdir(parents=True)
    collision_b.parent.mkdir(parents=True)
    collision_a.write_bytes(b"a")
    collision_b.write_bytes(b"b")
    resolver.invalidate()
    result = resolver.resolve(r"D:\unrecognized\collision.webp")
    assert result.status == "ambiguous" and len(result.candidates) == 2
    assert not result.resolved_path

    for status in ("removed", "quarantined", "purged"):
        record = {
            "image_a_path": r"D:\old\comparison_images\current.webp",
            "image_a_retained": False,
            "image_a_retention_status": status,
        }
        result = resolver.resolve(record["image_a_path"], record=record, side="A")
        assert result.status == "retention_removed" and not result.available

    assert resolver.resolve(r"D:\old\comparison_images\absent.webp").status == "missing"
    assert resolver.resolve("comparison_images/../current.webp").status in {"missing", "invalid"}

    result = resolver.resolve(
        r"D:\old\Data\media\favorite_duels\20260805\images\favorite.webp"
    )
    assert result.status == "relocated" and Path(result.resolved_path) == archived.resolve()

    assert resolver.rewrite_reference(str(nested), portable=True) == "comparison_images/2026/08/nested.webp"
    relocated = resolver.rewrite_reference(
        "comparison_images/2026/08/nested.webp", portable=False
    )
    assert Path(relocated) == nested.resolve()

    # The basename index is built once and reused. This fixture is deliberately
    # large enough to catch accidental per-row rescans without slowing CI.
    bulk = comparison / "bulk"
    bulk.mkdir()
    for index in range(3000):
        (bulk / f"item_{index:04d}.webp").write_bytes(b"x")
    resolver.invalidate()
    original_walk = media_module.os.walk
    walk_calls = 0

    def counted_walk(*args, **kwargs):
        nonlocal_walk_calls[0] += 1
        return original_walk(*args, **kwargs)

    nonlocal_walk_calls = [0]
    media_module.os.walk = counted_walk
    try:
        started = time.perf_counter()
        first = resolver.resolve(r"D:\unknown\item_2999.webp")
        for _ in range(100):
            repeated = resolver.resolve(r"D:\unknown\item_2999.webp")
        elapsed = time.perf_counter() - started
    finally:
        media_module.os.walk = original_walk
    assert first.available and repeated.available
    assert nonlocal_walk_calls[0] == 1
    assert elapsed < 10.0, elapsed

print("HISTORICAL_MEDIA_OK")
