#!/usr/bin/env python3
"""Phase 7 backup, transfer, recovery, and safe-update engine.

The module has no Gradio or NovelAI dependency. Every destructive operation is staged,
checksum-verified, journaled, and preceded by a restore point. Credentials, pairing
cookies/tokens, environment files, keystores, caches, and toolchains are never exported.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TRANSFER_SCHEMA_VERSION = 1
PORTABLE_MEDIA_REFERENCE_VERSION = 1
UPDATE_SCHEMA_VERSION = 1
MAX_ARCHIVE_MEMBERS = 600_000
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SINGLE_METADATA_BYTES = 2 * 1024 * 1024 * 1024

EXCLUDED_NAMES = {
    ".env", ".env.example", "local.properties", "keystore.properties",
    "gradle.properties", "phone_pairing.json", "bootstrap.json",
}
EXCLUDED_SUFFIXES = {".jks", ".keystore", ".p12", ".pfx", ".key", ".pem"}
EXCLUDED_PARTS = {
    "venv", ".git", "__pycache__", ".gradle", "gradle-cache", "toolchain",
    "diagnostics", "retention_quarantine", ".migration-staging",
}

RANKING_KEYS = (
    "elo_ratings_file", "comparison_history_file", "active_pool_file",
    "combination_ratings_file", "matchmaking_state_file", "top_search_ratings_file",
    "top50_entry_file",
)
SETTINGS_KEYS = (
    "storage_settings_file", "favorites_file", "classification_tags_file",
    "new_list_artists_file", "entity_notes_file", "artist_portraits_file",
    "generation_timing_stats_file", "bad_image_reports_file", "qol_runtime_state_file",
)
PROFILE_KEYS = ("saved_prompts_file", "saved_prompts_backup_file")
METADATA_KEYS = tuple(dict.fromkeys(RANKING_KEYS + SETTINGS_KEYS + PROFILE_KEYS + (
    "buffer_state_file",
)))
MEDIA_KEYS = ("artist_portraits_dir", "favorite_duel_archive_dir", "comparison_images_dir")

EXPORT_MODES = {
    "rankings": {"label": "Rankings only", "keys": RANKING_KEYS, "images": False},
    "settings": {"label": "Settings only", "keys": SETTINGS_KEYS, "images": False},
    "profiles": {"label": "Generation profiles / prompts only", "keys": PROFILE_KEYS, "images": False},
    "metadata": {"label": "All metadata without generated images", "keys": METADATA_KEYS + ("artist_portraits_dir",), "images": False},
    "complete": {"label": "Complete profile including images", "keys": METADATA_KEYS + MEDIA_KEYS, "images": True},
}

PROGRAM_BACKUP_NAMES = (
    "artist_elo_ranker_buffered.py", "ranker_data_layout.py", "generation_profiles.py",
    "storage_retention.py", "phone_pairing.py", "novelai_credential_store.py",
    "onboarding_guidance.py", "backup_transfer_recovery.py", "config.py",
)
PROGRAM_BACKUP_DIRS = ("android-builder/project",)


def _atomic_write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _format_bytes(value: int) -> str:
    amount = float(max(0, int(value or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024.0 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} TB"


def _version_tuple(value: Any) -> Tuple[int, ...]:
    parts: List[int] = []
    for token in str(value or "").strip().split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts or [0])


def _input_path(value: Any) -> Path:
    """Resolve a Gradio upload object, pathlib path, or ordinary path string."""
    if isinstance(value, Path):
        return value.expanduser().resolve()
    if isinstance(value, os.PathLike):
        return Path(value).expanduser().resolve()
    if isinstance(value, str):
        return Path(value).expanduser().resolve()
    name = getattr(value, "name", "")
    return Path(str(name or "")).expanduser().resolve()


def _safe_relative(name: str) -> Path:
    normalized = str(name or "").replace("\\", "/")
    if not normalized or normalized.startswith("/") or normalized.startswith("//"):
        raise ValueError(f"Unsafe archive path: {name!r}")
    path = Path(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe archive path: {name!r}")
    if path.parts and ":" in path.parts[0]:
        raise ValueError(f"Unsafe archive path: {name!r}")
    return path


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _is_sensitive(path: Path) -> bool:
    lowered = {part.casefold() for part in path.parts}
    if lowered & {part.casefold() for part in EXCLUDED_PARTS}:
        return True
    if path.name.casefold() in {name.casefold() for name in EXCLUDED_NAMES}:
        return True
    return path.suffix.casefold() in EXCLUDED_SUFFIXES


def _iter_files(path: Path) -> Iterable[Path]:
    path = Path(path)
    if not path.exists():
        return
    if path.is_file():
        if not _is_sensitive(path):
            yield path
        return
    for root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if name.casefold() not in {p.casefold() for p in EXCLUDED_PARTS}]
        for name in files:
            child = Path(root) / name
            if not _is_sensitive(child) and not child.is_symlink():
                yield child


def _merge_json(existing: Any, incoming: Any) -> Any:
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = dict(existing)
        for key, value in incoming.items():
            merged[key] = _merge_json(merged[key], value) if key in merged else value
        return merged
    return incoming


def _media_reference_relative(value: Any, folder_name: str) -> Path:
    """Reduce an absolute or portable media reference to a safe relative path."""
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        raise ValueError(f"Empty {folder_name} media reference.")
    folder = str(folder_name).strip("/")
    lowered = normalized.casefold()
    portable_prefix = folder.casefold() + "/"
    marker = "/" + portable_prefix
    if lowered.startswith(portable_prefix):
        relative_text = normalized[len(portable_prefix):]
    elif marker in lowered:
        offset = lowered.rfind(marker) + len(marker)
        relative_text = normalized[offset:]
    else:
        # Older exports did not identify their source data root. Confining an
        # unknown absolute value to its basename preserves the portrait while
        # preventing a transfer archive from selecting an arbitrary path.
        relative_text = normalized.rsplit("/", 1)[-1]
    return _safe_relative(relative_text)


def _rewrite_portrait_metadata_paths(
    payload: Any,
    *,
    portrait_root: Optional[Path] = None,
    comparison_root: Optional[Path] = None,
    portable: bool = False,
) -> Tuple[Any, int]:
    """Normalize portrait metadata for export or the active destination layout.

    Version 2.5.2 and older exports can contain absolute ``portrait_path`` and
    ``source_path`` values. New exports use portable, slash-separated media
    references; imports translate either representation to the active data root.
    """
    roots = {
        "portrait_path": ("artist_portraits", Path(portrait_root).resolve() if portrait_root else None),
        "source_path": ("comparison_images", Path(comparison_root).resolve() if comparison_root else None),
    }
    changed = 0

    def rewrite(value: Any) -> Any:
        nonlocal changed
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value
        output: Dict[Any, Any] = {}
        for key, item in value.items():
            field = str(key)
            if field not in roots or not isinstance(item, str) or not item.strip():
                output[key] = rewrite(item)
                continue
            folder, root = roots[field]
            relative = _media_reference_relative(item, folder)
            if portable:
                replacement = f"{folder}/{relative.as_posix()}"
            else:
                if root is None:
                    raise ValueError(f"An import destination is required for {field}.")
                destination = (root / relative).resolve()
                if destination != root and root not in destination.parents:
                    raise ValueError(f"Unsafe {field} destination in portrait metadata.")
                replacement = str(destination)
            changed += int(replacement != item)
            output[key] = replacement
        return output

    return rewrite(payload), changed


@dataclass
class ArchivePlan:
    plan_id: str
    source_archive: str
    source_sha256: str
    mode: str
    source_version: str
    entries: List[dict]
    conflicts: int
    files: int
    bytes: int
    created_at: float

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "source_archive": self.source_archive,
            "source_sha256": self.source_sha256,
            "mode": self.mode,
            "source_version": self.source_version,
            "entries": self.entries,
            "conflicts": self.conflicts,
            "files": self.files,
            "bytes": self.bytes,
            "created_at": self.created_at,
        }


class TransferRecoveryManager:
    def __init__(self, program_dir: Path, data_layout: Any, app_version: str):
        self.program_dir = Path(program_dir).resolve()
        self.data_layout = data_layout
        self.app_version = str(app_version)
        self.data_root = Path(data_layout.active_layout.root).resolve()
        self.root = self.data_root / "recovery"
        self.exports_dir = self.root / "exports"
        self.restore_points_dir = self.root / "restore_points"
        self.import_staging_dir = self.root / "import_staging"
        self.update_staging_dir = self.root / "update_staging"
        self.journal_file = self.root / "operation_journal.json"
        self.pending_update_file = self.program_dir / ".artist_ranker_pending_update.json"
        self.last_report_file = self.root / "last_recovery_report.json"
        self._lock = threading.RLock()
        for directory in (self.root, self.exports_dir, self.restore_points_dir, self.import_staging_dir, self.update_staging_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.startup_report = self._recover_interrupted_operation()

    def _path_for_key(self, key: str) -> Path:
        return Path(self.data_layout.path(key)).resolve()

    def _logical_sources(self, mode: str) -> List[Tuple[str, Path]]:
        spec = EXPORT_MODES.get(str(mode))
        if not spec:
            raise ValueError(f"Unsupported export mode: {mode}")
        result: List[Tuple[str, Path]] = []
        for key in spec["keys"]:
            try:
                path = self._path_for_key(key)
            except Exception:
                continue
            result.append((str(key), path))
        return result

    def _archive_manifest(self, *, kind: str, mode: str, entries: List[dict], label: str = "") -> dict:
        return {
            "schema_version": TRANSFER_SCHEMA_VERSION,
            "kind": kind,
            "mode": mode,
            "application": "NovelAI Artist Ranker",
            "application_version": self.app_version,
            "created_at": time.time(),
            "created_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
            "label": str(label or ""),
            "credentials_included": False,
            "phone_pairing_included": False,
            "portable_media_references": True,
            "portable_media_reference_version": PORTABLE_MEDIA_REFERENCE_VERSION,
            "entries": entries,
        }

    def create_export(self, mode: str, label: str = "") -> Tuple[str, Path]:
        mode = str(mode or "metadata")
        if mode not in EXPORT_MODES:
            raise ValueError("Choose a valid export mode.")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe_label = "_" + "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(label).strip())[:48] if str(label).strip() else ""
        destination = self.exports_dir / f"artist_ranker_{mode}_{stamp}{safe_label}.zip"
        entries: List[dict] = []
        seen: set[str] = set()
        portable_references = 0
        with self._lock, zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for key, source in self._logical_sources(mode):
                if not source.exists():
                    continue
                root = source if source.is_dir() else source.parent
                for child in _iter_files(source):
                    relative = child.relative_to(root) if source.is_dir() else Path(child.name)
                    arcname = Path("data") / key / relative
                    arc_text = arcname.as_posix()
                    if arc_text in seen:
                        continue
                    seen.add(arc_text)
                    if key == "artist_portraits_file":
                        portrait_data = json.loads(child.read_text(encoding="utf-8"))
                        portrait_data, rewritten = _rewrite_portrait_metadata_paths(
                            portrait_data,
                            portable=True,
                        )
                        payload = json.dumps(portrait_data, ensure_ascii=False, indent=2).encode("utf-8")
                        portable_references += rewritten
                        size = len(payload)
                        digest = _sha256_bytes(payload)
                        archive.writestr(arc_text, payload)
                    else:
                        size = int(child.stat().st_size)
                        digest = _sha256_path(child)
                        archive.write(child, arcname=arc_text)
                    entries.append({
                        "logical_key": key,
                        "relative_path": relative.as_posix(),
                        "archive_path": arc_text,
                        "size": size,
                        "sha256": digest,
                    })
            manifest = self._archive_manifest(kind="profile_export", mode=mode, entries=entries, label=label)
            manifest["portable_media_references_written"] = portable_references
            archive.writestr("artist_ranker_transfer_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        report = (
            f"Created **{EXPORT_MODES[mode]['label']}** export with **{len(entries):,} files** "
            f"and **{_format_bytes(sum(int(row['size']) for row in entries))}**.  \n"
            f"Credentials and pairing secrets were excluded.  \n`{destination}`"
        )
        return report, destination

    def _read_transfer_manifest(self, archive_path: Path) -> Tuple[dict, Dict[str, zipfile.ZipInfo]]:
        archive_path = Path(archive_path)
        if not archive_path.is_file():
            raise FileNotFoundError("Choose an Artist Ranker export ZIP.")
        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("The archive contains too many entries.")
            info_map: Dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                rel = _safe_relative(info.filename)
                if _zip_member_is_symlink(info):
                    raise ValueError(f"Symbolic links are not allowed: {info.filename}")
                if info.file_size < 0 or info.compress_size < 0:
                    raise ValueError("The archive has invalid member sizes.")
                info_map[rel.as_posix()] = info
            name = "artist_ranker_transfer_manifest.json"
            if name not in info_map:
                raise ValueError("This ZIP is not an Artist Ranker profile export.")
            if info_map[name].file_size > MAX_MANIFEST_BYTES:
                raise ValueError("The transfer manifest is unreasonably large.")
            raw = archive.read(name)
            manifest = json.loads(raw.decode("utf-8"))
        if not isinstance(manifest, dict) or int(manifest.get("schema_version", 0) or 0) != TRANSFER_SCHEMA_VERSION:
            raise ValueError("Unsupported transfer-manifest version.")
        if manifest.get("credentials_included"):
            raise ValueError("Archives claiming to contain credentials are refused.")
        return manifest, info_map

    def preview_import(self, archive_value: Any) -> Tuple[str, str]:
        archive_path = _input_path(archive_value)
        manifest, info_map = self._read_transfer_manifest(archive_path)
        rows: List[dict] = []
        conflicts = 0
        total_bytes = 0
        for row in manifest.get("entries", []):
            if not isinstance(row, dict):
                raise ValueError("The transfer manifest contains an invalid entry.")
            key = str(row.get("logical_key", ""))
            relative = _safe_relative(str(row.get("relative_path", "")))
            archive_name = _safe_relative(str(row.get("archive_path", ""))).as_posix()
            info = info_map.get(archive_name)
            if not info:
                raise ValueError(f"Archive member is missing: {archive_name}")
            expected_size = int(row.get("size", -1))
            if expected_size != int(info.file_size):
                raise ValueError(f"Size mismatch in manifest: {archive_name}")
            if expected_size > MAX_SINGLE_METADATA_BYTES and key != "comparison_images_dir":
                raise ValueError(f"Metadata member is too large: {archive_name}")
            try:
                base = self._path_for_key(key)
            except Exception as exc:
                raise ValueError(f"Unknown destination key: {key}") from exc
            destination = (base / relative).resolve() if base.is_dir() or key.endswith("_dir") else base.resolve()
            root = base.resolve() if base.is_dir() or key.endswith("_dir") else base.parent.resolve()
            if destination != root and root not in destination.parents:
                raise ValueError("Import destination escapes its data group.")
            conflict = destination.exists()
            conflicts += int(conflict)
            total_bytes += expected_size
            rows.append({
                "logical_key": key,
                "relative_path": relative.as_posix(),
                "archive_path": archive_name,
                "destination": str(destination),
                "size": expected_size,
                "sha256": str(row.get("sha256", "")),
                "conflict": conflict,
            })
        source_hash = _sha256_path(archive_path)
        payload = {
            "source_archive": str(archive_path),
            "source_sha256": source_hash,
            "mode": str(manifest.get("mode", "unknown")),
            "source_version": str(manifest.get("application_version", "unknown")),
            "entries": rows,
            "conflicts": conflicts,
            "files": len(rows),
            "bytes": total_bytes,
            "created_at": time.time(),
        }
        identity = {key: payload[key] for key in (
            "source_archive", "source_sha256", "mode", "source_version",
            "entries", "conflicts", "files", "bytes",
        )}
        payload_text = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["plan_id"] = _sha256_bytes(payload_text.encode("utf-8"))
        plan = ArchivePlan(**payload)
        preview_rows = [
            "### Import preview",
            f"**Source:** `{archive_path.name}` · version `{plan.source_version}` · mode `{plan.mode}`  ",
            f"**Files:** {plan.files:,} · **Size:** {_format_bytes(plan.bytes)} · **Conflicts:** {plan.conflicts:,}  ",
            f"**Plan:** `{plan.plan_id[:16]}`",
            "",
            "No files have been changed. Choose **Merge**, **Replace**, or **Skip existing**, confirm the preview, then apply.",
        ]
        for row in rows[:40]:
            marker = "replace/merge" if row["conflict"] else "new"
            preview_rows.append(f"- `{row['logical_key']}/{row['relative_path']}` — {marker} — {_format_bytes(row['size'])}")
        if len(rows) > 40:
            preview_rows.append(f"- …and {len(rows)-40:,} more files")
        if any(row["logical_key"] == "artist_portraits_file" for row in rows):
            preview_rows.append(
                "- Portrait and source-image references will be relocated to the active Data folder."
            )
        return "\n".join(preview_rows), json.dumps(plan.as_dict(), ensure_ascii=False)

    def _write_journal(self, operation: str, restore_point: Optional[Path], details: dict) -> None:
        _atomic_write_json(self.journal_file, {
            "schema_version": 1,
            "operation": operation,
            "state": "in_progress",
            "started_at": time.time(),
            "restore_point": str(restore_point or ""),
            "details": details,
        })

    def _complete_journal(self, report: dict) -> None:
        _atomic_write_json(self.last_report_file, report)
        self.journal_file.unlink(missing_ok=True)

    def create_restore_point(self, label: str, *, include_program: bool = True, include_images: bool = False) -> Path:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(label or "restore"))[:64]
        destination = self.restore_points_dir / f"restore_{stamp}_{safe}_{uuid.uuid4().hex[:6]}.zip"
        entries: List[dict] = []
        sources = self._logical_sources("complete" if include_images else "metadata")
        with self._lock, zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for key, source in sources:
                if not source.exists():
                    continue
                root = source if source.is_dir() else source.parent
                for child in _iter_files(source):
                    relative = child.relative_to(root) if source.is_dir() else Path(child.name)
                    arc = (Path("data") / key / relative).as_posix()
                    archive.write(child, arcname=arc)
                    entries.append({"scope":"data","logical_key":key,"relative_path":relative.as_posix(),"archive_path":arc,"size":int(child.stat().st_size),"sha256":_sha256_path(child)})
            if include_program:
                for name in PROGRAM_BACKUP_NAMES:
                    child = self.program_dir / name
                    if child.is_file() and not _is_sensitive(child):
                        arc = (Path("program") / name).as_posix()
                        archive.write(child, arcname=arc)
                        entries.append({"scope":"program","relative_path":name,"archive_path":arc,"size":int(child.stat().st_size),"sha256":_sha256_path(child)})
                for relative_dir in PROGRAM_BACKUP_DIRS:
                    directory = self.program_dir / relative_dir
                    for child in _iter_files(directory):
                        relative = child.relative_to(self.program_dir)
                        arc = (Path("program") / relative).as_posix()
                        archive.write(child, arcname=arc)
                        entries.append({"scope":"program","relative_path":relative.as_posix(),"archive_path":arc,"size":int(child.stat().st_size),"sha256":_sha256_path(child)})
            manifest = self._archive_manifest(kind="restore_point", mode="complete" if include_images else "metadata", entries=entries, label=label)
            manifest["includes_program"] = bool(include_program)
            manifest["includes_images"] = bool(include_images)
            manifest["logical_keys"] = [key for key, _source in sources]
            archive.writestr("artist_ranker_restore_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return destination

    def list_restore_points(self) -> List[dict]:
        rows: List[dict] = []
        for path in sorted(self.restore_points_dir.glob("restore_*.zip"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                with zipfile.ZipFile(path, "r") as archive:
                    raw = json.loads(archive.read("artist_ranker_restore_manifest.json").decode("utf-8"))
                rows.append({
                    "name": path.name,
                    "path": str(path),
                    "created": str(raw.get("created_at_text", "Unknown")),
                    "label": str(raw.get("label", "")),
                    "files": len(raw.get("entries", [])),
                    "size": int(path.stat().st_size),
                    "program": bool(raw.get("includes_program", False)),
                    "images": bool(raw.get("includes_images", False)),
                })
            except Exception:
                rows.append({"name":path.name,"path":str(path),"created":"Unreadable","label":"Corrupted","files":0,"size":int(path.stat().st_size),"program":False,"images":False})
        return rows

    def restore_points_markdown(self) -> str:
        rows = self.list_restore_points()
        if not rows:
            return "No restore points exist yet."
        lines = ["| Restore point | Created | Contents | Archive size |", "|---|---|---|---:|"]
        for row in rows[:50]:
            contents = "metadata" + (" + program" if row["program"] else "") + (" + images" if row["images"] else "")
            lines.append(f"| `{row['name']}` | {row['created']} | {contents} · {row['files']:,} files | {_format_bytes(row['size'])} |")
        return "\n".join(lines)

    def _destination_for_restore_entry(self, row: dict) -> Path:
        scope = str(row.get("scope", "data"))
        relative = _safe_relative(str(row.get("relative_path", "")))
        if scope == "program":
            destination = (self.program_dir / relative).resolve()
            if self.program_dir != destination and self.program_dir not in destination.parents:
                raise ValueError("Restore program path escapes the installation.")
            if _is_sensitive(destination):
                raise ValueError("Restore point attempts to write a protected file.")
            return destination
        key = str(row.get("logical_key", ""))
        base = self._path_for_key(key)
        return (base / relative).resolve() if key.endswith("_dir") or base.is_dir() else base.resolve()

    def _restore_archive(self, archive_path: Path, *, create_safety_point: bool) -> dict:
        archive_path = Path(archive_path).resolve()
        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
            raw = json.loads(archive.read("artist_ranker_restore_manifest.json").decode("utf-8"))
            entries = [row for row in raw.get("entries", []) if isinstance(row, dict)]
            # Validate every member before touching current data.
            validated: List[Tuple[dict, bytes, Path]] = []
            for row in entries:
                archive_name = _safe_relative(str(row.get("archive_path", ""))).as_posix()
                data = archive.read(archive_name)
                if len(data) != int(row.get("size", -1)) or _sha256_bytes(data) != str(row.get("sha256", "")):
                    raise ValueError(f"Restore checksum failed: {archive_name}")
                destination = self._destination_for_restore_entry(row)
                validated.append((row, data, destination))

            safety = self.create_restore_point("pre_restore", include_program=True, include_images=False) if create_safety_point else None
            new_destinations = [str(destination) for _row, _data, destination in validated if not destination.exists()]
            if create_safety_point:
                self._write_journal(
                    "restore", safety,
                    {"archive": str(archive_path), "new_destinations": new_destinations},
                )

            # A chosen restore point is an exact snapshot for the data-directory
            # groups it includes. Remove later files from those managed groups, but
            # never sweep the program folder or protected/private paths.
            expected_by_key: Dict[str, set[Path]] = {}
            for row, _data, destination in validated:
                if str(row.get("scope", "data")) == "data":
                    expected_by_key.setdefault(str(row.get("logical_key", "")), set()).add(destination.resolve())
            logical_keys = [str(key) for key in raw.get("logical_keys", [])]
            removed = 0
            for key in logical_keys:
                if not key.endswith("_dir"):
                    continue
                try:
                    base = self._path_for_key(key)
                except Exception:
                    continue
                expected = expected_by_key.get(key, set())
                for current in list(_iter_files(base)):
                    current = current.resolve()
                    if current not in expected:
                        current.unlink(missing_ok=True)
                        removed += 1

            restored = 0
            for _row, data, destination in validated:
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(prefix=destination.name+".", suffix=".restore.tmp", dir=str(destination.parent))
                temp = Path(temp_name)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, destination)
                finally:
                    temp.unlink(missing_ok=True)
                restored += 1
        report = {
            "ok": True, "operation": "restore", "archive": str(archive_path),
            "restored": restored, "removed_newer_files": removed,
            "finished_at": time.time(), "restart_required": True,
        }
        if create_safety_point:
            self._complete_journal(report)
        return report

    def restore_selected(self, name_or_path: str, confirmed: bool) -> str:
        if not confirmed:
            return "❌ Confirm restoration first."
        candidate = Path(str(name_or_path or ""))
        if not candidate.is_absolute():
            candidate = self.restore_points_dir / candidate.name
        if not candidate.is_file() or candidate.parent.resolve() != self.restore_points_dir.resolve():
            return "❌ Choose a valid restore point from the list."
        try:
            report = self._restore_archive(candidate, create_safety_point=True)
            return f"✅ Restored **{report['restored']:,} files**. Stop and restart the ranker before continuing. A pre-restore safety point was created."
        except Exception as exc:
            return f"❌ Restore failed: {type(exc).__name__}: {exc}. The recovery journal remains available for startup rollback."

    def apply_import(self, plan_text: str, strategy: str, confirmed: bool) -> str:
        if not confirmed:
            return "❌ Confirm that you reviewed the current import preview."
        try:
            submitted = json.loads(str(plan_text or ""))
            submitted_identity = {key: submitted.get(key) for key in (
                "source_archive", "source_sha256", "mode", "source_version",
                "entries", "conflicts", "files", "bytes",
            )}
            submitted_id = _sha256_bytes(json.dumps(
                submitted_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"))
            if submitted_id != submitted.get("plan_id"):
                raise ValueError("The import preview is stale or was modified; preview it again.")
            archive_path = Path(submitted["source_archive"]).resolve()
            if not archive_path.is_file() or _sha256_path(archive_path) != submitted.get("source_sha256"):
                raise ValueError("The selected archive changed after preview; preview it again.")
            # Never trust client-provided destinations. Rebuild the complete plan from
            # the archive and require the preview identity to still match.
            _preview, authoritative_text = self.preview_import(archive_path)
            authoritative = json.loads(authoritative_text)
            if authoritative.get("plan_id") != submitted.get("plan_id"):
                raise ValueError("The import preview is stale or was modified; preview it again.")
            strategy = str(strategy or "merge").strip().lower()
            if strategy not in {"merge", "replace", "skip"}:
                raise ValueError("Choose Merge, Replace, or Skip existing.")
            entries = authoritative.get("entries", [])
            include_images = any(str(row.get("logical_key")) in MEDIA_KEYS for row in entries)
            restore_point = self.create_restore_point(
                "pre_import", include_program=False, include_images=include_images
            )
            new_destinations = [
                str(Path(row["destination"]).resolve())
                for row in entries
                if not Path(row["destination"]).exists()
            ]
            self._write_journal(
                "import", restore_point,
                {
                    "plan_id": authoritative.get("plan_id"),
                    "archive": str(archive_path),
                    "strategy": strategy,
                    "new_destinations": new_destinations,
                },
            )
            written = skipped = merged = portrait_paths_rewritten = 0
            with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
                for row in entries:
                    destination = Path(row["destination"]).resolve()
                    if not self._allowed_data_destination(destination):
                        raise ValueError("Import destination is outside the active data layout.")
                    archive_name = _safe_relative(row["archive_path"]).as_posix()
                    data = archive.read(archive_name)
                    if len(data) != int(row["size"]) or _sha256_bytes(data) != row["sha256"]:
                        raise ValueError(f"Checksum failed: {archive_name}")
                    if destination.exists() and strategy == "skip":
                        skipped += 1
                        continue
                    if destination.exists() and strategy == "merge" and destination.suffix.casefold() == ".json":
                        existing = json.loads(destination.read_text(encoding="utf-8"))
                        incoming = json.loads(data.decode("utf-8"))
                        data = json.dumps(_merge_json(existing, incoming), ensure_ascii=False, indent=2).encode("utf-8")
                        merged += 1
                    if str(row.get("logical_key")) == "artist_portraits_file":
                        portrait_data = json.loads(data.decode("utf-8"))
                        portrait_data, rewritten = _rewrite_portrait_metadata_paths(
                            portrait_data,
                            portrait_root=self._path_for_key("artist_portraits_dir"),
                            comparison_root=self._path_for_key("comparison_images_dir"),
                        )
                        portrait_paths_rewritten += rewritten
                        data = json.dumps(portrait_data, ensure_ascii=False, indent=2).encode("utf-8")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    fd, temp_name = tempfile.mkstemp(prefix=destination.name+".", suffix=".import.tmp", dir=str(destination.parent))
                    temp = Path(temp_name)
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(data)
                            handle.flush()
                            os.fsync(handle.fileno())
                        if destination.suffix.casefold() == ".json":
                            json.loads(temp.read_text(encoding="utf-8"))
                        os.replace(temp, destination)
                    finally:
                        temp.unlink(missing_ok=True)
                    written += 1
            report = {
                "ok": True, "operation": "import", "written": written, "merged": merged,
                "skipped": skipped, "restore_point": str(restore_point),
                "portrait_paths_rewritten": portrait_paths_rewritten,
                "finished_at": time.time(), "restart_required": True,
            }
            self._complete_journal(report)
            return (
                f"✅ Import applied: **{written:,} written**, **{merged:,} JSON files merged**, "
                f"**{skipped:,} skipped**, **{portrait_paths_rewritten:,} portrait references relocated**. "
                f"Restart the ranker. "
                f"Pre-import restore point: `{restore_point.name}`."
            )
        except Exception as exc:
            rollback_note = ""
            if self.journal_file.exists():
                try:
                    journal = json.loads(self.journal_file.read_text(encoding="utf-8"))
                    report = self._rollback_journal(journal)
                    rollback_note = (
                        f" Automatic rollback restored {report.get('restored',0)} files and removed "
                        f"{report.get('new_files_removed',0)} partial new files."
                    )
                except Exception as rollback_exc:
                    rollback_note = (
                        f" Automatic rollback could not complete: {type(rollback_exc).__name__}: "
                        f"{rollback_exc}. Startup recovery will retry."
                    )
            return f"❌ Import failed: {type(exc).__name__}: {exc}.{rollback_note}".replace("..", ".")

    def validate_integrity(self) -> Tuple[str, dict]:
        issues: List[str] = []
        checked = 0
        for key in METADATA_KEYS:
            try:
                path = self._path_for_key(key)
            except Exception:
                continue
            if not path.exists() or not path.is_file():
                continue
            checked += 1
            if path.suffix.casefold() == ".json":
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except Exception as exc:
                    issues.append(f"{key}: invalid JSON — {exc}")
        pending = self.journal_file.exists()
        if pending:
            issues.append("An incomplete operation journal is present.")
        report = {"ok":not issues,"checked":checked,"issues":issues,"journal_present":pending,"time":time.time()}
        if issues:
            text = "### Integrity check found problems\n" + "\n".join(f"- {issue}" for issue in issues)
        else:
            text = f"✅ Integrity check passed for **{checked:,} metadata files**. No incomplete operation journal was found."
        return text, report

    def _allowed_data_destination(self, destination: Path) -> bool:
        destination = Path(destination).resolve()
        for key in tuple(dict.fromkeys(METADATA_KEYS + MEDIA_KEYS)):
            try:
                base = self._path_for_key(key)
            except Exception:
                continue
            root = base.resolve() if key.endswith("_dir") or base.is_dir() else base.parent.resolve()
            if destination == base.resolve() or destination == root or root in destination.parents:
                return True
        return False

    def _rollback_journal(self, journal: dict) -> dict:
        removed = 0
        for raw in journal.get("details", {}).get("new_destinations", []):
            try:
                destination = Path(str(raw)).resolve()
                if self._allowed_data_destination(destination) and destination.is_file():
                    destination.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                pass
        restore = Path(str(journal.get("restore_point", "")))
        if not restore.is_file() or restore.parent.resolve() != self.restore_points_dir.resolve():
            raise FileNotFoundError("The journaled restore point is unavailable.")
        report = self._restore_archive(restore, create_safety_point=False)
        report.update({
            "startup_recovery": True,
            "interrupted_operation": journal.get("operation"),
            "new_files_removed": removed,
        })
        self._complete_journal(report)
        return report

    def _recover_interrupted_operation(self) -> str:
        if not self.journal_file.exists():
            return ""
        try:
            journal = json.loads(self.journal_file.read_text(encoding="utf-8"))
            report = self._rollback_journal(journal)
            return (
                f"Recovered an interrupted {journal.get('operation','operation')} from "
                f"`{Path(str(journal.get('restore_point',''))).name}`; "
                f"{report.get('restored',0)} files restored and "
                f"{report.get('new_files_removed',0)} partial new files removed."
            )
        except Exception as exc:
            return f"Startup recovery could not complete: {type(exc).__name__}: {exc}"

    def _read_update_manifest(self, archive_path: Path) -> Tuple[dict, str]:
        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
            candidates = [name for name in archive.namelist() if name.endswith("phase_update_manifest.json")]
            if len(candidates) != 1:
                raise ValueError("The update ZIP must contain exactly one phase_update_manifest.json.")
            manifest_name = _safe_relative(candidates[0]).as_posix()
            raw = json.loads(archive.read(manifest_name).decode("utf-8"))
        if int(raw.get("schema_version",0) or 0) != UPDATE_SCHEMA_VERSION:
            raise ValueError("Unsupported update package schema.")
        return raw, str(Path(manifest_name).parent.as_posix())

    def preview_update(self, archive_value: Any) -> Tuple[str, str]:
        path = _input_path(archive_value)
        if not path.is_file():
            raise FileNotFoundError("Choose an Artist Ranker update ZIP.")
        manifest, root_prefix = self._read_update_manifest(path)
        files = manifest.get("files", [])
        if not isinstance(files, list) or not files:
            raise ValueError("The update package contains no file manifest.")
        minimum_version = str(manifest.get("minimum_version", "") or "").strip()
        if minimum_version and _version_tuple(self.app_version) < _version_tuple(minimum_version):
            raise ValueError(
                f"This update requires Artist Ranker {minimum_version} or newer; current version is {self.app_version}."
            )
        if len(files) > 20_000:
            raise ValueError("The update package contains too many program files.")
        seen_targets: set[str] = set()
        with zipfile.ZipFile(path, "r", allowZip64=True) as archive:
            names = set(archive.namelist())
            for row in files:
                source = _safe_relative(str(row.get("source", ""))).as_posix()
                target = _safe_relative(str(row.get("target", ""))).as_posix()
                if target in seen_targets:
                    raise ValueError(f"Duplicate update target: {target}")
                seen_targets.add(target)
                if _is_sensitive(Path(target)):
                    raise ValueError(f"Update attempts to replace a protected file: {target}")
                member = f"{root_prefix}/{source}" if root_prefix not in {"", "."} else source
                if member not in names:
                    raise ValueError(f"Update payload is missing: {source}")
                info = archive.getinfo(member)
                if int(info.file_size) != int(row.get("size", -1)):
                    raise ValueError(f"Update size mismatch: {source}")
                data = archive.read(member)
                if _sha256_bytes(data) != str(row.get("sha256", "")):
                    raise ValueError(f"Update checksum mismatch: {source}")
        payload = {
            "archive":str(path), "sha256":_sha256_path(path), "root_prefix":root_prefix,
            "version":str(manifest.get("version","unknown")), "minimum_version":str(manifest.get("minimum_version","")),
            "release_notes":str(manifest.get("release_notes","")), "files":files,
            "build_android":bool(manifest.get("build_android",False)), "created_at":time.time(),
        }
        payload["plan_id"] = _sha256_bytes(json.dumps(payload,sort_keys=True,separators=(",",":")).encode())
        text = (
            f"### Update preview\n**Version:** `{payload['version']}` · **Files:** {len(files):,} · "
            f"**Package:** `{path.name}`  \n**Plan:** `{payload['plan_id'][:16]}`\n\n"
            f"{payload['release_notes'] or 'No release notes were supplied.'}\n\n"
            "Applying schedules a checksum-verified update for the next restart and creates a full pre-update program/metadata restore point."
        )
        return text, json.dumps(payload, ensure_ascii=False)

    def schedule_update(self, plan_text: str, confirmed: bool) -> str:
        if not confirmed:
            return "❌ Confirm the update preview first."
        try:
            plan = json.loads(str(plan_text or ""))
            archive = Path(plan["archive"]).resolve()
            if not archive.is_file() or _sha256_path(archive) != plan.get("sha256"):
                raise ValueError("The update package changed after preview.")
            staged = self.update_staging_dir / f"{plan.get('version','update')}_{uuid.uuid4().hex[:8]}.zip"
            shutil.copy2(archive, staged)
            restore = self.create_restore_point(f"pre_update_{plan.get('version','unknown')}", include_program=True, include_images=False)
            request = dict(plan)
            request.update({"archive":str(staged),"pre_update_restore_point":str(restore),"scheduled_at":time.time()})
            _atomic_write_json(self.pending_update_file, request)
            return f"✅ Update `{plan.get('version')}` scheduled. Stop and restart the ranker. Pre-update restore point: `{restore.name}`."
        except Exception as exc:
            return f"❌ Could not schedule update: {type(exc).__name__}: {exc}"

    def check_release_feed(self, url: str) -> str:
        text = str(url or "").strip()
        if not text:
            return "Enter a JSON release-feed URL, or use the manual update ZIP controls below."
        if not text.lower().startswith(("https://", "http://")):
            return "❌ Release feed must use HTTP or HTTPS."
        try:
            request = urllib.request.Request(text, headers={"User-Agent":f"ArtistRanker/{self.app_version}"})
            with urllib.request.urlopen(request, timeout=8) as response:
                data = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
            releases = data.get("releases", []) if isinstance(data, dict) else []
            if not releases:
                return "No releases were listed by the feed."
            lines = [f"### Releases from feed (current `{self.app_version}`)"]
            for row in releases[:20]:
                lines.append(f"- **{row.get('version','?')}** — {row.get('notes','No notes')} — `{row.get('url','')}`")
            return "\n".join(lines)
        except Exception as exc:
            return f"❌ Release-feed check failed: {type(exc).__name__}: {exc}"


def process_pending_update_bootstrap(program_dir: Path) -> str:
    """Apply a scheduled update before importing mutable local modules.

    Program files and published APKs are restored byte-for-byte if checksum
    validation, file replacement, or the requested Android rebuild fails.
    """
    program_dir = Path(program_dir).resolve()
    pending = program_dir / ".artist_ranker_pending_update.json"
    if not pending.exists():
        return ""
    changed: List[Tuple[Path, Optional[bytes]]] = []
    protected_snapshots: set[Path] = set()

    def remember(path: Path) -> None:
        path = Path(path).resolve()
        if path in protected_snapshots:
            return
        protected_snapshots.add(path)
        changed.append((path, path.read_bytes() if path.is_file() else None))

    try:
        request = json.loads(pending.read_text(encoding="utf-8"))
        archive_path = Path(request["archive"]).resolve()
        if not archive_path.is_file() or _sha256_path(archive_path) != request.get("sha256"):
            raise ValueError("Scheduled update package is missing or changed.")
        root_prefix = str(request.get("root_prefix", "")).strip("/")
        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
            for row in request.get("files", []):
                source = _safe_relative(str(row.get("source", ""))).as_posix()
                target_rel = _safe_relative(str(row.get("target", "")))
                target = (program_dir / target_rel).resolve()
                if program_dir != target and program_dir not in target.parents:
                    raise ValueError("Update target escapes the program folder.")
                if _is_sensitive(target_rel):
                    raise ValueError(f"Update attempts to replace protected file: {target_rel}")
                member = f"{root_prefix}/{source}" if root_prefix else source
                data = archive.read(member)
                if len(data) != int(row.get("size", -1)) or _sha256_bytes(data) != str(row.get("sha256", "")):
                    raise ValueError(f"Update checksum failed: {source}")
                remember(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(prefix=target.name+".", suffix=".update.tmp", dir=str(target.parent))
                temp = Path(temp_name)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
        if bool(request.get("build_android", False)):
            builder = program_dir / "android-builder" / "build_artist_ranker.py"
            if not builder.is_file():
                raise FileNotFoundError("The Android builder required by this update is missing.")
            for apk in (
                program_dir / "android-builder" / "output" / "artist-ranker.apk",
                program_dir / "downloads" / "artist-ranker.apk",
            ):
                remember(apk)
            subprocess.run(
                [sys.executable, str(builder), "--mode", "fast"],
                cwd=str(builder.parent), check=True,
            )
        pending.unlink(missing_ok=True)
        return f"Scheduled update {request.get('version','')} applied successfully before startup."
    except Exception as exc:
        for target, previous in reversed(changed):
            try:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(previous)
            except Exception:
                pass
        failed = pending.with_name(f".artist_ranker_failed_update_{int(time.time())}.json")
        try:
            pending.replace(failed)
        except Exception:
            pass
        return (
            "Scheduled update failed and changed program/APK files were rolled back: "
            f"{type(exc).__name__}: {exc}"
        )
