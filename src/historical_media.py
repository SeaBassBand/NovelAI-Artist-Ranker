#!/usr/bin/env python3
"""Safe compatibility resolution for moved Artist Ranker media files.

Historical metadata can outlive the data root that originally held its images.
This module resolves those references without mutating metadata and without ever
guessing between duplicate filenames.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Optional, Tuple


COMPARISON_KIND = "comparison_images"
FAVORITE_KIND = "favorite_duels"
_RETENTION_STATUSES = frozenset({"removed", "quarantined", "purged", "deleted"})
_KIND_MARKERS = {
    "comparison_images": COMPARISON_KIND,
    "artisteloimages": COMPARISON_KIND,
    "favorite_duels": FAVORITE_KIND,
}


@dataclass(frozen=True)
class HistoricalMediaResolution:
    stored_path: str
    resolved_path: str
    status: str
    method: str
    candidates: Tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.status in {"valid", "relocated"} and bool(self.resolved_path)


def _contained_path(root: Path, relative: PurePosixPath) -> Optional[Path]:
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    try:
        root = Path(os.path.abspath(str(root)))
        candidate = Path(os.path.abspath(str(root.joinpath(*relative.parts))))
        if os.path.commonpath((str(root), str(candidate))) != str(root):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _reference_parts(value: Any) -> Tuple[str, ...]:
    normalized = str(value or "").strip().replace("\\", "/")
    return tuple(part for part in normalized.split("/") if part not in {"", "."})


def _reference_kind_and_relative(
    value: Any,
    default_kind: str = COMPARISON_KIND,
) -> Tuple[str, Optional[PurePosixPath], str]:
    parts = _reference_parts(value)
    if not parts or ".." in parts:
        return default_kind, None, "none"
    marker_index = -1
    kind = default_kind
    for index, part in enumerate(parts):
        mapped = _KIND_MARKERS.get(part.casefold())
        if mapped:
            marker_index = index
            kind = mapped
    if marker_index >= 0 and marker_index + 1 < len(parts):
        method = "portable_reference" if marker_index == 0 else "relative_suffix"
        return kind, PurePosixPath(*parts[marker_index + 1 :]), method
    name = parts[-1]
    if not name or ":" in name:
        return default_kind, None, "none"
    return default_kind, PurePosixPath(name), "basename"


def _intentionally_removed(record: Optional[dict], side: Optional[str]) -> bool:
    if not isinstance(record, dict) or not side:
        return False
    normalized = "b" if str(side).strip().casefold() == "b" else "a"
    for field in (f"image_{normalized}_retained", f"image_{normalized}_original_retained"):
        if record.get(field) is False:
            return True
    for field in (f"image_{normalized}_retention_status", f"image_{normalized}_status"):
        if str(record.get(field, "") or "").strip().casefold() in _RETENTION_STATUSES:
            return True
    return False


class _LazyMediaIndex:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._lock = threading.RLock()
        self._by_name: Optional[Dict[str, Tuple[Path, ...]]] = None
        self._top_level: Optional[Dict[str, Path]] = None

    def invalidate(self) -> None:
        with self._lock:
            self._by_name = None
            self._top_level = None

    def top_level(self, filename: str) -> Optional[Path]:
        with self._lock:
            if self._top_level is None:
                found: Dict[str, Path] = {}
                if self.root.is_dir():
                    with os.scandir(self.root) as entries:
                        for entry in entries:
                            if entry.is_file(follow_symlinks=False):
                                found[entry.name.casefold()] = Path(entry.path)
                self._top_level = found
            return self._top_level.get(str(filename or "").casefold())

    def _ensure(self) -> Dict[str, Tuple[Path, ...]]:
        with self._lock:
            if self._by_name is not None:
                return self._by_name
            pending: Dict[str, list[Path]] = {}
            if self.root.is_dir():
                for folder, _dirs, files in os.walk(self.root):
                    parent = Path(folder)
                    for name in files:
                        path = parent / name
                        pending.setdefault(name.casefold(), []).append(path)
            self._by_name = {
                name: tuple(sorted(paths, key=lambda path: str(path).casefold()))
                for name, paths in pending.items()
            }
            return self._by_name

    def matches(self, filename: str) -> Tuple[Path, ...]:
        return self._ensure().get(str(filename or "").casefold(), ())

class HistoricalMediaResolver:
    """Resolve current, legacy, and portable managed-media references safely."""

    def __init__(self, comparison_root: Path, favorite_root: Optional[Path] = None):
        roots = {COMPARISON_KIND: Path(comparison_root).resolve()}
        if favorite_root is not None:
            roots[FAVORITE_KIND] = Path(favorite_root).resolve()
        self.roots = roots
        self._indexes = {kind: _LazyMediaIndex(root) for kind, root in roots.items()}
        self._source_root_lock = threading.RLock()
        self._source_root_exists: Dict[str, bool] = {}
        self._path_cache_lock = threading.RLock()
        self._path_file_cache: Dict[str, bool] = {}

    @staticmethod
    def _path_cache_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def _is_file(self, path: Path) -> bool:
        key = self._path_cache_key(path)
        with self._path_cache_lock:
            cached = self._path_file_cache.get(key)
        if cached is not None:
            return cached
        try:
            exists = Path(path).is_file()
        except (OSError, RuntimeError, ValueError):
            exists = False
        with self._path_cache_lock:
            self._path_file_cache[key] = exists
        return exists

    def prime_references(
        self,
        references: Iterable[Any],
        *,
        default_kind: str = COMPARISON_KIND,
        workers: int = 32,
    ) -> None:
        """Batch existence checks so large Gallery indexes do not serialize disk latency."""
        pending: Dict[str, Path] = {}
        for value in references:
            stored = str(value or "").strip()
            if not stored:
                continue
            kind, relative, method = _reference_kind_and_relative(stored, default_kind)
            root = self.roots.get(kind)
            candidate: Optional[Path] = None
            if relative is not None and root is not None and method != "basename":
                candidate = _contained_path(root, relative)
            elif self._direct_might_exist(stored, kind):
                candidate = Path(stored).expanduser()
            if candidate is None:
                continue
            key = self._path_cache_key(candidate)
            with self._path_cache_lock:
                if key in self._path_file_cache:
                    continue
            pending[key] = candidate
        if not pending:
            return

        def check(item: Tuple[str, Path]) -> Tuple[str, bool]:
            key, path = item
            try:
                return key, path.is_file()
            except (OSError, RuntimeError, ValueError):
                return key, False

        max_workers = max(1, min(int(workers or 1), 64, len(pending)))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="media-audit") as executor:
            results = executor.map(check, pending.items())
            with self._path_cache_lock:
                self._path_file_cache.update(results)

    def prime_history(self, records: Iterable[dict], *, workers: int = 32) -> None:
        references = []
        for record in records:
            if isinstance(record, dict):
                references.extend((record.get("image_a_path", ""), record.get("image_b_path", "")))
        self.prime_references(references, default_kind=COMPARISON_KIND, workers=workers)

    def _direct_might_exist(self, stored: str, kind: str) -> bool:
        normalized = str(stored or "").strip().replace("\\", "/")
        lowered = normalized.casefold()
        marker_position = -1
        marker_text = ""
        for marker in ("comparison_images", "artisteloimages", "favorite_duels"):
            token = f"/{marker}/"
            position = lowered.rfind(token)
            if position > marker_position:
                marker_position = position
                marker_text = marker
        if marker_position < 0:
            prefix = f"{kind.casefold()}/"
            if lowered.startswith(prefix):
                return False
            return True
        source_root_text = normalized[: marker_position + len(marker_text) + 1]
        active_root = self.roots.get(kind)
        if active_root is not None:
            active_text = str(active_root).replace("\\", "/").rstrip("/").casefold()
            if source_root_text.rstrip("/").casefold() == active_text:
                return True
        cache_key = source_root_text.casefold()
        with self._source_root_lock:
            if cache_key not in self._source_root_exists:
                self._source_root_exists[cache_key] = Path(source_root_text).is_dir()
            return self._source_root_exists[cache_key]

    def invalidate(self, kind: Optional[str] = None) -> None:
        if kind:
            index = self._indexes.get(str(kind))
            if index:
                index.invalidate()
            with self._path_cache_lock:
                self._path_file_cache.clear()
            return
        for index in self._indexes.values():
            index.invalidate()
        with self._path_cache_lock:
            self._path_file_cache.clear()

    def resolve(
        self,
        stored_path: Any,
        *,
        record: Optional[dict] = None,
        side: Optional[str] = None,
        default_kind: str = COMPARISON_KIND,
    ) -> HistoricalMediaResolution:
        stored = str(stored_path or "").strip()
        if _intentionally_removed(record, side):
            return HistoricalMediaResolution(stored, "", "retention_removed", "none")
        if not stored:
            return HistoricalMediaResolution(stored, "", "invalid", "none")

        kind, relative, suffix_method = _reference_kind_and_relative(stored, default_kind)
        direct = Path(stored).expanduser()
        if self._direct_might_exist(stored, kind):
            try:
                if self._is_file(direct):
                    return HistoricalMediaResolution(stored, str(direct.resolve()), "valid", "stored_path")
            except (OSError, RuntimeError, ValueError):
                pass

        root = self.roots.get(kind)
        if relative is not None and root is not None and suffix_method != "basename":
            candidate = _contained_path(root, relative)
            if candidate is not None and self._is_file(candidate):
                return HistoricalMediaResolution(stored, str(candidate), "relocated", suffix_method)

        filename = relative.name if relative is not None else ""
        index = self._indexes.get(kind) or self._indexes.get(default_kind)
        matches = index.matches(filename) if index is not None and filename else ()
        if len(matches) == 1:
            return HistoricalMediaResolution(
                stored, str(matches[0].resolve()), "relocated", "basename_unique"
            )
        if len(matches) > 1:
            return HistoricalMediaResolution(
                stored,
                "",
                "ambiguous",
                "basename_collision",
                tuple(str(path.resolve()) for path in matches),
            )
        return HistoricalMediaResolution(stored, "", "missing", "none")

    def rewrite_reference(
        self,
        stored_path: Any,
        *,
        portable: bool,
        default_kind: str = COMPARISON_KIND,
    ) -> str:
        stored = str(stored_path or "").strip()
        if not stored:
            return stored
        kind, relative, _method = _reference_kind_and_relative(stored, default_kind)
        resolution = self.resolve(stored, default_kind=kind)
        root = self.roots.get(kind)
        if resolution.available and root is not None:
            try:
                relative = Path(resolution.resolved_path).resolve().relative_to(root)
            except (OSError, RuntimeError, ValueError):
                pass
        if relative is None:
            return stored
        safe = _contained_path(root, relative) if root is not None else None
        if safe is None:
            return stored
        if portable:
            return f"{kind}/{PurePosixPath(*relative.parts).as_posix()}"
        return str(safe)


def rewrite_history_media_paths(
    payload: Any,
    resolver: HistoricalMediaResolver,
    *,
    portable: bool,
) -> Tuple[Any, int]:
    if not isinstance(payload, list):
        return payload, 0
    changed = 0
    output = []
    for value in payload:
        if not isinstance(value, dict):
            output.append(value)
            continue
        record = dict(value)
        for field in ("image_a_path", "image_b_path"):
            current = record.get(field)
            if not isinstance(current, str) or not current.strip():
                continue
            replacement = resolver.rewrite_reference(
                current, portable=portable, default_kind=COMPARISON_KIND
            )
            changed += int(replacement != current)
            record[field] = replacement
        output.append(record)
    return output, changed


def rewrite_favorite_media_paths(
    payload: Any,
    resolver: HistoricalMediaResolver,
    *,
    portable: bool,
) -> Tuple[Any, int]:
    if not isinstance(payload, dict):
        return payload, 0
    changed = 0

    def rewrite(value: Any, field: str = "") -> Any:
        nonlocal changed
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value
        result: Dict[Any, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in {"image_a_path", "image_b_path", "path", "favorite_archive_manifest"} and isinstance(item, str) and item.strip():
                default_kind = FAVORITE_KIND if name == "favorite_archive_manifest" else COMPARISON_KIND
                replacement = resolver.rewrite_reference(
                    item, portable=portable, default_kind=default_kind
                )
                changed += int(replacement != item)
                result[key] = replacement
            else:
                result[key] = rewrite(item, name)
        return result

    output = rewrite(payload)
    image_bucket = output.get("image", {}) if isinstance(output, dict) else {}
    if isinstance(image_bucket, dict):
        rewritten_bucket: Dict[str, Any] = {}
        for old_key, entry in image_bucket.items():
            new_key = resolver.rewrite_reference(
                old_key, portable=portable, default_kind=COMPARISON_KIND
            )
            changed += int(new_key != old_key)
            if isinstance(entry, dict):
                entry = dict(entry)
                if str(entry.get("key", "")) == str(old_key):
                    entry["key"] = new_key
            rewritten_bucket[new_key] = entry
        output["image"] = rewritten_bucket
    return output, changed
