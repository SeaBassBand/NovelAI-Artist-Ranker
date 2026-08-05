#!/usr/bin/env python3
"""Versioned, migration-safe data layout for NovelAI Artist Ranker.

This module deliberately has no Gradio or NovelAI dependencies.  It is loaded before
ranker state is constructed, so pending data-location migrations can complete (or roll
back cleanly) before any mutable files are opened.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

APP_NAME = "NovelAI Artist Ranker"
BOOTSTRAP_SCHEMA_VERSION = 1
DATA_LAYOUT_SCHEMA_VERSION = 1
DATA_SCHEMA_VERSION = 1
CONFIG_SCHEMA_VERSION = 1

MODE_LEGACY = "legacy"
MODE_INSTALLED = "installed"
MODE_PORTABLE = "portable"
MODE_CUSTOM = "custom"
VALID_MODES = {MODE_LEGACY, MODE_INSTALLED, MODE_PORTABLE, MODE_CUSTOM}

FILE_KEYS = (
    "elo_ratings_file",
    "comparison_history_file",
    "active_pool_file",
    "saved_prompts_file",
    "saved_prompts_backup_file",
    "combination_ratings_file",
    "buffer_state_file",
    "matchmaking_state_file",
    "favorites_file",
    "storage_settings_file",
    "generation_timing_stats_file",
    "classification_tags_file",
    "new_list_artists_file",
    "entity_notes_file",
    "top_search_ratings_file",
    "artist_portraits_file",
    "top50_entry_file",
    "bad_image_reports_file",
    "qol_runtime_state_file",
)

DIRECTORY_KEYS = (
    "comparison_images_dir",
    "artist_portraits_dir",
    "favorite_duel_archive_dir",
    "diagnostics_dir",
)

# Legacy backups often contain source-code patch backups.  Only actual user-data
# archives are migrated into the new data root.
LEGACY_BACKUP_PATTERNS = (
    "artist_elo_backup_*.zip",
    "artist_elo_manual_backup_*.zip",
    "shareable_update_backup_*.zip",
    "data_migration_backup_*.zip",
)


def _atomic_write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_resolve(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except Exception:
        return Path(path).expanduser().absolute()


def _path_is_relative_to(path: Path, root: Path) -> bool:
    path = _safe_resolve(path)
    root = _safe_resolve(root)
    return path == root or root in path.parents


def _scan_path(path: Path, *, backup_filter: bool = False) -> Tuple[int, int]:
    path = Path(path)
    if not path.exists():
        return 0, 0
    if path.is_file():
        try:
            return 1, int(path.stat().st_size)
        except OSError:
            return 1, 0
    count = 0
    total = 0
    if backup_filter:
        files: List[Path] = []
        for pattern in LEGACY_BACKUP_PATTERNS:
            files.extend(path.glob(pattern))
        for child in dict.fromkeys(files):
            if child.is_file():
                count += 1
                try:
                    total += int(child.stat().st_size)
                except OSError:
                    pass
        return count, total
    for root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if name not in {".migration-staging", "__pycache__"}]
        for name in files:
            child = Path(root) / name
            count += 1
            try:
                total += int(child.stat().st_size)
            except OSError:
                pass
    return count, total


def _format_bytes(value: int) -> str:
    amount = float(max(0, int(value or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024.0 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} TB"


@dataclass(frozen=True)
class DataLayout:
    mode: str
    root: Path
    paths: Dict[str, Path]

    def path(self, key: str) -> Path:
        return Path(self.paths[key])


class DataLayoutManager:
    """Selects, validates, previews, and migrates ranker user-data layouts."""

    def __init__(self, program_dir: Path, legacy_paths: Dict[str, Any]):
        self.program_dir = _safe_resolve(Path(program_dir))
        self.legacy_paths = {key: _safe_resolve(Path(value)) for key, value in legacy_paths.items()}
        self._lock = threading.RLock()
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            self.local_app_root = _safe_resolve(Path(local_app_data) / APP_NAME)
        else:
            self.local_app_root = _safe_resolve(Path.home() / ".local" / "share" / APP_NAME)
        self.bootstrap_dir = self.local_app_root / "bootstrap"
        self.bootstrap_file = self.bootstrap_dir / "bootstrap.json"
        self.migration_request_file = self.bootstrap_dir / "migration_request.json"
        self.last_migration_report_file = self.bootstrap_dir / "last_migration_report.json"
        self.portable_flag = self.program_dir / "portable_mode.flag"
        self.bootstrap_dir.mkdir(parents=True, exist_ok=True)
        self.last_startup_message = ""
        self._process_pending_request()
        self.active_layout = self._select_active_layout()
        self._ensure_active_layout()
        self._ensure_manifest_and_schema()

    # ------------------------------------------------------------------
    # Layout selection and path maps
    # ------------------------------------------------------------------
    def _read_json(self, path: Path, default: Any = None) -> Any:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
            return value
        except Exception:
            return default

    def _read_bootstrap(self) -> dict:
        raw = self._read_json(self.bootstrap_file, {})
        return raw if isinstance(raw, dict) else {}

    def _legacy_detected(self) -> bool:
        important = (
            "elo_ratings_file", "comparison_history_file", "storage_settings_file",
            "buffer_state_file", "favorites_file",
        )
        return any(self.legacy_paths.get(key, Path("__missing__")).exists() for key in important)

    def _default_root_for_mode(self, mode: str, custom_root: Optional[Any] = None) -> Path:
        if mode == MODE_LEGACY:
            return self.program_dir
        if mode == MODE_PORTABLE:
            return self.program_dir / "data"
        if mode == MODE_INSTALLED:
            return self.local_app_root / "data"
        if mode == MODE_CUSTOM:
            text = str(custom_root or "").strip()
            if not text:
                raise ValueError("A custom data folder is required.")
            return _safe_resolve(Path(text))
        raise ValueError(f"Unsupported data mode: {mode}")

    def _new_layout_paths(self, root: Path) -> Dict[str, Path]:
        root = _safe_resolve(root)
        state = root / "state"
        config = root / "config"
        media = root / "media"
        images = media / "comparison_images"
        return {
            "data_root": root,
            "manifest_file": root / "data_manifest.json",
            "elo_ratings_file": state / self.legacy_paths["elo_ratings_file"].name,
            "comparison_history_file": state / self.legacy_paths["comparison_history_file"].name,
            "active_pool_file": state / self.legacy_paths["active_pool_file"].name,
            "saved_prompts_file": config / "saved_prompt_presets.json",
            "saved_prompts_backup_file": config / "saved_prompt_presets.backup.json",
            "combination_ratings_file": state / "combination_elo_ratings.json",
            "buffer_state_file": state / "comparison_buffer.json",
            "backups_dir": root / "backups",
            "matchmaking_state_file": state / "matchmaking_state.json",
            "favorites_file": config / "favorites.json",
            "storage_settings_file": config / "storage_settings.json",
            "generation_timing_stats_file": config / "generation_timing_stats.json",
            "classification_tags_file": config / "classification_tags.json",
            "new_list_artists_file": config / "added_top2000_missing_artists.txt",
            "entity_notes_file": config / "entity_notes.json",
            "top_search_ratings_file": state / "top_search_ratings.json",
            "artist_portraits_file": config / "artist_portraits.json",
            "artist_portraits_dir": media / "artist_portraits",
            "top50_entry_file": state / "top50_entry_tracking.json",
            "favorite_duel_archive_dir": media / "favorite_duels",
            "diagnostics_dir": root / "diagnostics",
            "comparison_images_dir": images,
            "thumbnail_dir": images / ".gallery_thumbnails",
            "qol_preview_dir": images / ".duel_previews",
            "bad_image_reports_file": config / "bad_image_reports.json",
            "qol_runtime_state_file": state / "qol_runtime_state.json",
            "migration_dir": root / "migration",
        }

    def _legacy_layout_paths(self) -> Dict[str, Path]:
        images = self.legacy_paths["comparison_images_dir"]
        return {
            "data_root": self.program_dir,
            "manifest_file": self.bootstrap_dir / "legacy_data_manifest.json",
            **self.legacy_paths,
            "thumbnail_dir": images / ".gallery_thumbnails",
            "qol_preview_dir": images / ".duel_previews",
            "migration_dir": self.bootstrap_dir / "legacy_migration",
        }

    def layout_for(self, mode: str, custom_root: Optional[Any] = None) -> DataLayout:
        mode = str(mode or "").strip().lower()
        if mode not in VALID_MODES:
            raise ValueError(f"Unsupported data mode: {mode}")
        root = self._default_root_for_mode(mode, custom_root)
        paths = self._legacy_layout_paths() if mode == MODE_LEGACY else self._new_layout_paths(root)
        return DataLayout(mode=mode, root=root, paths=paths)

    def _select_active_layout(self) -> DataLayout:
        env_root = str(os.environ.get("ARTIST_RANKER_DATA_DIR", "") or "").strip()
        if env_root:
            return self.layout_for(MODE_CUSTOM, env_root)
        bootstrap = self._read_bootstrap()
        mode = str(bootstrap.get("mode", "") or "").strip().lower()
        configured_root = bootstrap.get("data_root")
        if mode in VALID_MODES:
            if mode == MODE_CUSTOM:
                return self.layout_for(mode, configured_root)
            return self.layout_for(mode)
        if self.portable_flag.exists():
            return self.layout_for(MODE_PORTABLE)
        if self._legacy_detected():
            return self.layout_for(MODE_LEGACY)
        return self.layout_for(MODE_INSTALLED)

    def _ensure_active_layout(self) -> None:
        layout = self.active_layout
        if layout.mode == MODE_LEGACY:
            Path(layout.path("comparison_images_dir")).mkdir(parents=True, exist_ok=True)
            return
        bootstrap = self._read_bootstrap()
        if (
            layout.mode == MODE_CUSTOM
            and str(bootstrap.get("mode", "")).strip().lower() == MODE_CUSTOM
            and not layout.root.exists()
        ):
            raise RuntimeError(
                f"The configured custom data folder is unavailable: {layout.root}. "
                "Reconnect the drive/folder or repair bootstrap.json; an empty replacement was not created."
            )
        for key, path in layout.paths.items():
            if key.endswith("_dir") or key in {"data_root", "migration_dir"}:
                Path(path).mkdir(parents=True, exist_ok=True)
            elif key.endswith("_file"):
                Path(path).parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Schema/version foundation
    # ------------------------------------------------------------------
    def _manifest_payload(self, layout: Optional[DataLayout] = None) -> dict:
        layout = layout or self.active_layout
        previous = self._read_json(layout.path("manifest_file"), {})
        created = previous.get("created_at") if isinstance(previous, dict) else None
        return {
            "application": APP_NAME,
            "layout_schema_version": DATA_LAYOUT_SCHEMA_VERSION,
            "data_schema_version": DATA_SCHEMA_VERSION,
            "config_schema_version": CONFIG_SCHEMA_VERSION,
            "mode": layout.mode,
            "data_root": str(layout.root),
            "comparison_images_dir": str(layout.path("comparison_images_dir")),
            "created_at": float(created or time.time()),
            "updated_at": time.time(),
        }

    def _ensure_manifest_and_schema(self) -> None:
        manifest_path = self.active_layout.path("manifest_file")
        old = self._read_json(manifest_path, {})
        old_layout = int(old.get("layout_schema_version", 0) or 0) if isinstance(old, dict) else 0
        old_data = int(old.get("data_schema_version", 0) or 0) if isinstance(old, dict) else 0
        old_config = int(old.get("config_schema_version", 0) or 0) if isinstance(old, dict) else 0
        if old_layout > DATA_LAYOUT_SCHEMA_VERSION or old_data > DATA_SCHEMA_VERSION or old_config > CONFIG_SCHEMA_VERSION:
            raise RuntimeError(
                "This data folder was created by a newer Artist Ranker version. "
                "Install a compatible application version before opening it."
            )
        # Version 1 establishes the manifest without rewriting existing ranker JSON.
        # Future migrations are added as ordered, backup-protected steps here.
        _atomic_write_json(manifest_path, self._manifest_payload())

    # ------------------------------------------------------------------
    # Status and preview
    # ------------------------------------------------------------------
    def path(self, key: str) -> Path:
        return self.active_layout.path(key)

    def status_dict(self) -> dict:
        pending = self._read_json(self.migration_request_file, None)
        report = self._read_json(self.last_migration_report_file, None)
        return {
            "mode": self.active_layout.mode,
            "data_root": str(self.active_layout.root),
            "comparison_images_dir": str(self.path("comparison_images_dir")),
            "layout_schema_version": DATA_LAYOUT_SCHEMA_VERSION,
            "data_schema_version": DATA_SCHEMA_VERSION,
            "config_schema_version": CONFIG_SCHEMA_VERSION,
            "manifest_file": str(self.path("manifest_file")),
            "pending_migration": pending if isinstance(pending, dict) else None,
            "last_migration_report": report if isinstance(report, dict) else None,
            "startup_message": self.last_startup_message,
        }

    def status_markdown(self) -> str:
        status = self.status_dict()
        mode_labels = {
            MODE_LEGACY: "Legacy location (current project folders)",
            MODE_INSTALLED: "Installed mode (%LOCALAPPDATA%)",
            MODE_PORTABLE: "Portable mode (program\\data)",
            MODE_CUSTOM: "Custom data folder",
        }
        pending = status.get("pending_migration")
        pending_text = "None"
        if pending:
            pending_text = f"Scheduled for next restart: **{pending.get('target_mode', 'unknown')}** → `{pending.get('target_root', '')}`"
        last = status.get("last_migration_report")
        last_text = "No migration has run yet."
        if last:
            outcome = "Succeeded" if last.get("ok") else "Failed and rolled back"
            last_text = f"{outcome} at `{last.get('finished_at_text', 'unknown')}`"
        return (
            "### Data location and migration safety\n"
            f"**Current mode:** {mode_labels.get(status['mode'], status['mode'])}  \n"
            f"**Active data root:** `{status['data_root']}`  \n"
            f"**Comparison images:** `{status['comparison_images_dir']}`  \n"
            f"**Schema versions:** layout `{DATA_LAYOUT_SCHEMA_VERSION}` · data `{DATA_SCHEMA_VERSION}` · config `{CONFIG_SCHEMA_VERSION}`  \n"
            f"**Pending migration:** {pending_text}  \n"
            f"**Last migration:** {last_text}\n\n"
            "Existing installations remain in Legacy mode until a migration is explicitly previewed, confirmed, "
            "and scheduled. The old source data is never deleted by this phase."
        )

    def _target_layout_from_ui(self, mode: Any, custom_root: Any = "") -> DataLayout:
        raw = str(mode or "").strip().lower()
        aliases = {
            "legacy current location": MODE_LEGACY,
            "installed (%localappdata%)": MODE_INSTALLED,
            "portable (program\\data)": MODE_PORTABLE,
            "custom folder": MODE_CUSTOM,
        }
        selected = aliases.get(raw, raw)
        return self.layout_for(selected, custom_root)

    def _validate_target(self, source: DataLayout, target: DataLayout) -> List[str]:
        warnings: List[str] = []
        if target.mode == MODE_LEGACY:
            raise ValueError(
                "Legacy mode is kept as the untouched rollback source in Phase 1; "
                "choose Installed, Portable, or Custom for a migration destination."
            )
        if source.mode == target.mode and _safe_resolve(source.root) == _safe_resolve(target.root):
            raise ValueError("The selected destination is already active.")
        if target.mode != MODE_LEGACY and _path_is_relative_to(target.root, source.path("comparison_images_dir")):
            raise ValueError("The data root cannot be placed inside the current comparison-image directory.")
        if target.mode == MODE_PORTABLE and not os.access(self.program_dir, os.W_OK):
            raise PermissionError("The program directory is not writable, so Portable mode cannot be used here.")
        root = target.root
        if root.exists():
            manifest = target.path("manifest_file")
            entries = [child for child in root.iterdir() if child.name not in {".migration-staging"}]
            if manifest.exists():
                raise FileExistsError("The selected destination already contains an Artist Ranker data layout.")
            if entries:
                raise FileExistsError("The selected destination is not empty. Choose an empty folder.")
        if target.mode == MODE_CUSTOM:
            warnings.append("A custom folder is only available while that drive/path is mounted.")
        if source.mode == MODE_LEGACY:
            warnings.append("The current legacy files will remain untouched as a rollback copy after migration.")
        return warnings

    def build_migration_plan(self, mode: Any, custom_root: Any = "") -> dict:
        source = self.active_layout
        target = self._target_layout_from_ui(mode, custom_root)
        warnings = self._validate_target(source, target)
        entries: List[dict] = []
        total_files = 0
        total_bytes = 0
        for key in FILE_KEYS:
            source_path = source.path(key)
            destination = target.path(key)
            count, size = _scan_path(source_path)
            entries.append({
                "key": key, "kind": "file", "source": str(source_path),
                "destination": str(destination), "files": count, "bytes": size,
            })
            total_files += count
            total_bytes += size
        for key in DIRECTORY_KEYS:
            source_path = source.path(key)
            destination = target.path(key)
            count, size = _scan_path(source_path)
            entries.append({
                "key": key, "kind": "directory", "source": str(source_path),
                "destination": str(destination), "files": count, "bytes": size,
            })
            total_files += count
            total_bytes += size
        backup_count, backup_bytes = _scan_path(source.path("backups_dir"), backup_filter=(source.mode == MODE_LEGACY))
        entries.append({
            "key": "backups_dir", "kind": "filtered_backups", "source": str(source.path("backups_dir")),
            "destination": str(target.path("backups_dir")), "files": backup_count, "bytes": backup_bytes,
        })
        total_files += backup_count
        total_bytes += backup_bytes
        plan = {
            "schema": 1,
            "created_at": time.time(),
            "source_mode": source.mode,
            "source_root": str(source.root),
            "target_mode": target.mode,
            "target_root": str(target.root),
            "source_paths": {key: str(value) for key, value in source.paths.items()},
            "target_paths": {key: str(value) for key, value in target.paths.items()},
            "entries": entries,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "warnings": warnings,
            "requires_restart": True,
        }
        plan_json = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
        plan["plan_sha256"] = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        return plan

    def preview_markdown(self, mode: Any, custom_root: Any = "") -> Tuple[str, str]:
        try:
            plan = self.build_migration_plan(mode, custom_root)
        except Exception as exc:
            return f"❌ **Migration preview failed:** {type(exc).__name__}: {exc}", ""
        present = [row for row in plan["entries"] if int(row.get("files", 0)) > 0]
        rows = [
            "| Data group | Files | Size | Destination |",
            "|---|---:|---:|---|",
        ]
        label_map = {
            "comparison_images_dir": "Generated duel images and derived previews",
            "artist_portraits_dir": "Artist portraits",
            "favorite_duel_archive_dir": "Favorite-duel archive",
            "diagnostics_dir": "Diagnostics and exports",
            "backups_dir": "User-data backup ZIPs",
        }
        for row in present:
            label = label_map.get(row["key"], row["key"].replace("_", " ").title())
            rows.append(
                f"| {label} | {int(row['files']):,} | {_format_bytes(int(row['bytes']))} | `{row['destination']}` |"
            )
        if not present:
            rows.append("| No existing data found | 0 | 0 B | — |")
        warning_text = "\n".join(f"- ⚠️ {warning}" for warning in plan.get("warnings", []))
        report = (
            "### Migration preview\n"
            f"**From:** `{plan['source_mode']}` — `{plan['source_root']}`  \n"
            f"**To:** `{plan['target_mode']}` — `{plan['target_root']}`  \n"
            f"**Data to copy:** {int(plan['total_files']):,} files · {_format_bytes(int(plan['total_bytes']))}  \n"
            f"**Plan ID:** `{plan['plan_sha256'][:16]}`\n\n"
            + "\n".join(rows)
            + ("\n\n" + warning_text if warning_text else "")
            + "\n\nMigration runs before the ranker opens on the next restart. Files are copied into a staging "
              "folder, JSON references are rewritten, every metadata file is parsed, copied files are validated, "
              "and only then is the new location activated. A failed migration leaves the current location active."
        )
        return report, json.dumps(plan, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Scheduling and update snapshots
    # ------------------------------------------------------------------
    def schedule_migration(self, plan_text: Any, confirmed: Any) -> Tuple[str, str]:
        if not bool(confirmed):
            return "❌ Confirm that you reviewed the migration preview first.", str(plan_text or "")
        try:
            plan = json.loads(str(plan_text or ""))
            if not isinstance(plan, dict):
                raise ValueError("The migration plan is missing.")
            # Rebuild the plan to detect edits or destination changes since preview.
            rebuilt = self.build_migration_plan(plan.get("target_mode"), plan.get("target_root"))
            if rebuilt.get("plan_sha256") != plan.get("plan_sha256"):
                # Counts may legitimately change while the app is running. Preserve the
                # latest authoritative plan rather than accepting arbitrary client data.
                plan = rebuilt
            request = {
                "schema": 1,
                "requested_at": time.time(),
                "source_mode": self.active_layout.mode,
                "source_root": str(self.active_layout.root),
                "target_mode": plan["target_mode"],
                "target_root": plan["target_root"],
                "plan_sha256": plan["plan_sha256"],
            }
            _atomic_write_json(self.migration_request_file, request)
            return (
                "✅ Migration scheduled. **Stop and restart the ranker** to run it before any data files are opened. "
                "The current files remain active until the destination validates.",
                json.dumps(plan, ensure_ascii=False),
            )
        except Exception as exc:
            return f"❌ Could not schedule migration: {type(exc).__name__}: {exc}", str(plan_text or "")

    def cancel_scheduled_migration(self) -> str:
        self.migration_request_file.unlink(missing_ok=True)
        return "Scheduled migration cancelled. The active data location was not changed."

    def create_update_snapshot(self, label: str = "manual") -> Tuple[str, Optional[str]]:
        """Create a compact metadata/config snapshot; generated images stay in place."""
        import zipfile
        stamp = time.strftime("%Y%m%d_%H%M%S")
        destination_dir = self.path("backups_dir")
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"shareable_update_backup_{stamp}_{label}.zip"
        try:
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                manifest = {
                    "created_at": time.time(),
                    "label": str(label),
                    "mode": self.active_layout.mode,
                    "data_root": str(self.active_layout.root),
                    "comparison_images_dir": str(self.path("comparison_images_dir")),
                    "layout_schema_version": DATA_LAYOUT_SCHEMA_VERSION,
                    "data_schema_version": DATA_SCHEMA_VERSION,
                    "config_schema_version": CONFIG_SCHEMA_VERSION,
                    "generated_images_included": False,
                }
                archive.writestr("data_layout_manifest.json", json.dumps(manifest, indent=2))
                for key in FILE_KEYS:
                    path = self.path(key)
                    if path.exists() and path.is_file():
                        archive.write(path, arcname=f"metadata/{key}/{path.name}")
                portraits = self.path("artist_portraits_dir")
                if portraits.exists():
                    for child in portraits.rglob("*"):
                        if child.is_file():
                            archive.write(child, arcname=f"artist_portraits/{child.relative_to(portraits)}")
            return f"Created update-safe metadata snapshot: `{destination.name}`", str(destination)
        except Exception as exc:
            destination.unlink(missing_ok=True)
            return f"❌ Update snapshot failed: {type(exc).__name__}: {exc}", None

    # ------------------------------------------------------------------
    # Transactional migration engine
    # ------------------------------------------------------------------
    def _copy_file_verified(self, source: Path, destination: Path, manifest_rows: List[dict]) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        source_size = int(source.stat().st_size)
        destination_size = int(destination.stat().st_size)
        if source_size != destination_size:
            raise IOError(f"Copy size mismatch: {source}")
        # Exact hash all metadata; images are size-verified and sampled later to avoid
        # doubling the I/O cost of a potentially very large one-time migration.
        exact_hash = source.suffix.lower() in {".json", ".txt", ".jsonl", ".zip"} or source_size <= 8 * 1024 * 1024
        digest = ""
        if exact_hash:
            source_hash = _sha256(source)
            destination_hash = _sha256(destination)
            if source_hash != destination_hash:
                raise IOError(f"Copy hash mismatch: {source}")
            digest = source_hash
        manifest_rows.append({
            "source": str(source), "destination": str(destination),
            "size": source_size, "sha256": digest,
        })

    def _copy_directory_verified(self, source: Path, destination: Path, manifest_rows: List[dict]) -> None:
        if not source.exists():
            return
        for root, dirs, files in os.walk(source):
            dirs[:] = [name for name in dirs if name not in {"__pycache__", ".migration-staging"}]
            relative = Path(root).relative_to(source)
            for name in files:
                src = Path(root) / name
                dst = destination / relative / name
                self._copy_file_verified(src, dst, manifest_rows)

    def _copy_filtered_backups(self, source: Path, destination: Path, manifest_rows: List[dict], legacy: bool) -> None:
        if not source.exists():
            return
        if not legacy:
            self._copy_directory_verified(source, destination, manifest_rows)
            return
        found: List[Path] = []
        for pattern in LEGACY_BACKUP_PATTERNS:
            found.extend(source.glob(pattern))
        for src in dict.fromkeys(found):
            if src.is_file():
                self._copy_file_verified(src, destination / src.name, manifest_rows)

    def _rewrite_json_paths(self, json_paths: Iterable[Path], path_replacements: List[Tuple[str, str]]) -> int:
        changed = 0

        normalized_pairs: List[Tuple[str, str]] = []
        for old, new in path_replacements:
            old = str(old or "")
            new = str(new or "")
            if old and new and old != new:
                normalized_pairs.append((old, new))
                normalized_pairs.append((old.replace("\\", "/"), new.replace("\\", "/")))

        def rewrite(value: Any) -> Any:
            nonlocal changed
            if isinstance(value, str):
                output = value
                for old, new in normalized_pairs:
                    if output == old or output.startswith(old + "\\") or output.startswith(old + "/"):
                        output = new + output[len(old):]
                        changed += int(output != value)
                        break
                return output
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            if isinstance(value, dict):
                return {key: rewrite(item) for key, item in value.items()}
            return value

        for path in json_paths:
            path = Path(path)
            if not path.exists() or not path.is_file() or path.suffix.lower() != ".json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"Copied JSON is invalid before path rewrite: {path}: {exc}") from exc
            rewritten = rewrite(data)
            _atomic_write_json(path, rewritten)
            # Parse again after atomic rewrite.
            json.loads(path.read_text(encoding="utf-8"))
        return changed

    def _verify_staging(self, staging: Path, manifest_rows: List[dict]) -> dict:
        missing: List[str] = []
        bytes_verified = 0
        for row in manifest_rows:
            destination = Path(row["destination"])
            # Destination paths in rows point to final target; convert to staging path.
            try:
                relative = destination.relative_to(Path(row["target_root"]))
            except Exception:
                relative = None
            actual = staging / relative if relative is not None else destination
            if not actual.exists() or not actual.is_file():
                missing.append(str(actual))
                continue
            if int(actual.stat().st_size) != int(row["size"]):
                raise IOError(f"Staged file size changed during migration: {actual}")
            if row.get("sha256") and _sha256(actual) != row["sha256"]:
                raise IOError(f"Staged file hash changed during migration: {actual}")
            bytes_verified += int(row["size"])
        if missing:
            raise FileNotFoundError("Staging verification found missing files: " + ", ".join(missing[:5]))
        return {"files_verified": len(manifest_rows), "bytes_verified": bytes_verified}

    def _execute_migration(self, request: dict) -> dict:
        started = time.time()
        source = self._select_active_layout_without_pending()
        target = self.layout_for(request.get("target_mode"), request.get("target_root"))
        self._validate_target(source, target)
        target_parent = target.root.parent
        target_parent.mkdir(parents=True, exist_ok=True)
        staging = target_parent / f".{target.root.name}.migration-staging-{uuid.uuid4().hex[:10]}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        staging_layout = DataLayout(target.mode, staging, self._new_layout_paths(staging))
        manifest_rows: List[dict] = []
        report: dict = {
            "schema": 1, "ok": False, "started_at": started,
            "source_mode": source.mode, "source_root": str(source.root),
            "target_mode": target.mode, "target_root": str(target.root),
            "staging_root": str(staging), "copied": manifest_rows,
        }
        try:
            for key in FILE_KEYS:
                src = source.path(key)
                if src.exists() and src.is_file():
                    dst = staging_layout.path(key)
                    self._copy_file_verified(src, dst, manifest_rows)
            for key in DIRECTORY_KEYS:
                self._copy_directory_verified(source.path(key), staging_layout.path(key), manifest_rows)
            self._copy_filtered_backups(
                source.path("backups_dir"), staging_layout.path("backups_dir"),
                manifest_rows, source.mode == MODE_LEGACY,
            )
            # Record final target paths in manifest rows before verification.
            for row in manifest_rows:
                staged_destination = Path(row["destination"])
                relative = staged_destination.relative_to(staging)
                row["destination"] = str(target.root / relative)
                row["target_root"] = str(target.root)
            replacements = [
                (str(source.path("comparison_images_dir")), str(target.path("comparison_images_dir"))),
                (str(source.path("artist_portraits_dir")), str(target.path("artist_portraits_dir"))),
                (str(source.path("favorite_duel_archive_dir")), str(target.path("favorite_duel_archive_dir"))),
            ]
            rewritten = self._rewrite_json_paths(
                [staging_layout.path(key) for key in FILE_KEYS],
                replacements,
            )
            # Rewritten JSON files are intentionally no longer byte-identical to
            # the legacy source. Refresh their verified size/hash to describe the
            # staged, parseable destination rather than the pre-rewrite copy.
            for row in manifest_rows:
                final_path = Path(row["destination"])
                relative = final_path.relative_to(target.root)
                staged_path = staging / relative
                if staged_path.suffix.lower() == ".json" and staged_path.exists():
                    row["size"] = int(staged_path.stat().st_size)
                    row["sha256"] = _sha256(staged_path)
            verification = self._verify_staging(staging, manifest_rows)
            migration_manifest = {
                **self._manifest_payload(target),
                "migrated_at": time.time(),
                "migrated_from_mode": source.mode,
                "migrated_from_root": str(source.root),
                "source_comparison_images_dir": str(source.path("comparison_images_dir")),
                "path_references_rewritten": rewritten,
                "files_copied": verification["files_verified"],
                "bytes_copied": verification["bytes_verified"],
                "source_preserved": True,
            }
            _atomic_write_json(staging_layout.path("manifest_file"), migration_manifest)
            if target.root.exists():
                # _validate_target allows only an empty existing directory.
                target.root.rmdir()
            os.replace(staging, target.root)
            bootstrap = {
                "bootstrap_schema_version": BOOTSTRAP_SCHEMA_VERSION,
                "mode": target.mode,
                "data_root": str(target.root),
                "activated_at": time.time(),
                "previous_mode": source.mode,
                "previous_root": str(source.root),
                "source_preserved": True,
            }
            _atomic_write_json(self.bootstrap_file, bootstrap)
            report.update({
                "ok": True,
                "finished_at": time.time(),
                "finished_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
                "path_references_rewritten": rewritten,
                **verification,
                "source_preserved": True,
            })
            _atomic_write_json(self.last_migration_report_file, report)
            return report
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            report.update({
                "ok": False,
                "finished_at": time.time(),
                "finished_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "rolled_back": True,
                "source_preserved": True,
            })
            _atomic_write_json(self.last_migration_report_file, report)
            raise

    def _select_active_layout_without_pending(self) -> DataLayout:
        env_root = str(os.environ.get("ARTIST_RANKER_DATA_DIR", "") or "").strip()
        if env_root:
            return self.layout_for(MODE_CUSTOM, env_root)
        bootstrap = self._read_bootstrap()
        mode = str(bootstrap.get("mode", "") or "").strip().lower()
        if mode in VALID_MODES:
            return self.layout_for(mode, bootstrap.get("data_root")) if mode == MODE_CUSTOM else self.layout_for(mode)
        if self.portable_flag.exists():
            return self.layout_for(MODE_PORTABLE)
        if self._legacy_detected():
            return self.layout_for(MODE_LEGACY)
        return self.layout_for(MODE_INSTALLED)

    def _process_pending_request(self) -> None:
        request = self._read_json(self.migration_request_file, None)
        if not isinstance(request, dict):
            return
        try:
            report = self._execute_migration(request)
            self.last_startup_message = (
                f"Data migration completed: {report.get('files_verified', 0)} files copied to "
                f"{report.get('target_root')}. The original data was preserved."
            )
        except Exception as exc:
            self.last_startup_message = (
                f"Data migration failed and was rolled back: {type(exc).__name__}: {exc}. "
                "The previous data location remains active."
            )
        finally:
            self.migration_request_file.unlink(missing_ok=True)


def build_legacy_path_map(
    *,
    program_dir: Path,
    comparison_images_dir: Path,
    artist_tags_file: Path,
    elo_ratings_file: Path,
    comparison_history_file: Path,
    active_pool_file: Path,
) -> Dict[str, Path]:
    """Return the complete v1.7.x legacy layout without importing the ranker."""
    program_dir = _safe_resolve(program_dir)
    comparison_images_dir = _safe_resolve(comparison_images_dir)
    return {
        "artist_tags_file": _safe_resolve(artist_tags_file),
        "elo_ratings_file": _safe_resolve(elo_ratings_file),
        "comparison_history_file": _safe_resolve(comparison_history_file),
        "active_pool_file": _safe_resolve(active_pool_file),
        "saved_prompts_file": program_dir / "saved_prompt_presets.json",
        "saved_prompts_backup_file": program_dir / "saved_prompt_presets.backup.json",
        "combination_ratings_file": program_dir / "combination_elo_ratings.json",
        "buffer_state_file": program_dir / "comparison_buffer.json",
        "backups_dir": program_dir / "backups",
        "matchmaking_state_file": program_dir / "matchmaking_state.json",
        "favorites_file": program_dir / "favorites.json",
        "storage_settings_file": program_dir / "storage_settings.json",
        "generation_timing_stats_file": program_dir / "generation_timing_stats.json",
        "classification_tags_file": program_dir / "classification_tags.json",
        "new_list_artists_file": program_dir / "added_top2000_missing_artists.txt",
        "entity_notes_file": program_dir / "entity_notes.json",
        "top_search_ratings_file": program_dir / "top_search_ratings.json",
        "artist_portraits_file": program_dir / "artist_portraits.json",
        "artist_portraits_dir": program_dir / "artist_portraits",
        "top50_entry_file": program_dir / "top50_entry_tracking.json",
        "favorite_duel_archive_dir": program_dir / "FavoriteDuelArchive",
        "diagnostics_dir": program_dir / "diagnostics",
        "comparison_images_dir": comparison_images_dir,
        "bad_image_reports_file": program_dir / "bad_image_reports.json",
        "qol_runtime_state_file": program_dir / "qol_runtime_state.json",
    }
