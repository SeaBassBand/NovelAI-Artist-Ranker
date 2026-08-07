#!/usr/bin/env python3
"""Portable transfer bundles containing both isolated generation archives."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Tuple

from backup_transfer_recovery import (
    EXPORT_MODES,
    TransferRecoveryManager,
    _atomic_zip_writer,
    _format_bytes,
    _iter_files,
)


DUAL_TRANSFER_SCHEMA_VERSION = 1
DUAL_MANIFEST_NAME = "artist_ranker_dual_archive_manifest.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class MirroredDataLayout:
    """Reuse one DataLayoutManager's logical mapping under another archive root."""

    def __init__(self, source_layout: Any, root: Path):
        self.source_layout = source_layout
        self.source_root = Path(source_layout.active_layout.root).resolve()
        self.active_layout = SimpleNamespace(root=Path(root).resolve(), mode="generation_archive")

    def path(self, key: str) -> Path:
        source = Path(self.source_layout.path(key)).resolve()
        try:
            relative = source.relative_to(self.source_root)
        except ValueError as exc:
            raise ValueError(f"Logical data path {key!r} is outside the isolated archive root") from exc
        return self.active_layout.root / relative


class DualArchiveTransferManager:
    """Delegate normal recovery operations, but make profile transfers dual-mode."""

    def __init__(
        self,
        active_manager: TransferRecoveryManager,
        source_layout: Any,
        program_dir: Path,
        app_version: str,
        control_path: Path,
    ):
        self.active = active_manager
        self.source_layout = source_layout
        self.program_dir = Path(program_dir).resolve()
        self.app_version = str(app_version)
        self.control_path = Path(control_path).resolve()
        self.archive_container = self.control_path.parent
        self.archives_root = self.archive_container / "archives"
        self.archive_roots = {
            "novelai": self.archives_root / "novelai",
            "local": self.archives_root / "local",
        }
        for root in self.archive_roots.values():
            root.mkdir(parents=True, exist_ok=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.active, name)

    def _manager(self, archive_name: str, backup_root: Path | None = None) -> TransferRecoveryManager:
        layout = MirroredDataLayout(self.source_layout, self.archive_roots[archive_name])
        return TransferRecoveryManager(
            self.program_dir,
            layout,
            self.app_version,
            backup_root=backup_root or self.active.backup_root,
            github_repository=self.active.github_repository,
        )

    def _control_mode(self) -> str:
        try:
            value = json.loads(self.control_path.read_text(encoding="utf-8-sig"))
            mode = str(value.get("selected_mode", "novelai") or "novelai").casefold()
        except Exception:
            mode = "novelai"
        return mode if mode in self.archive_roots else "novelai"

    def estimate_export(self, mode: str, destination_root: Any = None) -> Tuple[str, dict]:
        mode = str(mode or "metadata")
        if mode not in EXPORT_MODES:
            raise ValueError("Choose a valid export mode.")
        root = self.active.configure_backup_root(destination_root) if destination_root else self.active.backup_root
        files = 0
        total = 0
        seen: set[Path] = set()
        for archive_name in ("novelai", "local"):
            manager = self._manager(archive_name, root)
            for _key, source in manager._logical_sources(mode):
                for child in _iter_files(source):
                    resolved = child.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    try:
                        total += child.stat().st_size
                        files += 1
                    except OSError:
                        continue
        free = int(shutil.disk_usage(root).free)
        required = total + min(max(512 * 1024 * 1024, total // 20), 4 * 1024 * 1024 * 1024)
        enough = free >= required
        report = {"mode": mode, "files": files, "source_bytes": total, "free_bytes": free, "required_bytes": required, "enough_space": enough, "destination": str(root), "dual_archive": True}
        text = (
            f"### Dual-archive backup preview\n**Contents:** NovelAI + Local/Anima · {EXPORT_MODES[mode]['label']}  \n"
            f"**Files:** {files:,}  \n**Source size:** {_format_bytes(total)}  \n"
            f"**Destination:** `{root}`  \n**Free space:** {_format_bytes(free)}  \n"
            f"**Space check:** {'Ready' if enough else 'Not enough free space for a safe export'}"
        )
        return text, report

    def create_export(self, mode: str, label: str = "") -> Tuple[str, Path]:
        mode = str(mode or "metadata")
        if mode not in EXPORT_MODES:
            raise ValueError("Choose a valid export mode.")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe_label = "_" + "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(label).strip())[:48] if str(label).strip() else ""
        destination = self.active.exports_dir / f"artist_ranker_dual_{mode}_{stamp}{safe_label}.zip"
        entries = []
        with tempfile.TemporaryDirectory(prefix="artist-ranker-dual-export-") as temp_name:
            temp_root = Path(temp_name)
            nested_paths = {}
            for archive_name in ("novelai", "local"):
                manager = self._manager(archive_name, temp_root / archive_name)
                _report, nested = manager.create_export(mode, archive_name)
                nested_paths[archive_name] = nested
            with _atomic_zip_writer(destination) as archive:
                for archive_name, nested in nested_paths.items():
                    data = nested.read_bytes()
                    arcname = f"archives/{archive_name}.zip"
                    archive.writestr(arcname, data)
                    entries.append({"archive": archive_name, "archive_path": arcname, "size": len(data), "sha256": _sha256_bytes(data)})
                manifest = {
                    "schema_version": DUAL_TRANSFER_SCHEMA_VERSION,
                    "kind": "dual_archive_export",
                    "application": "NovelAI Artist Ranker",
                    "application_version": self.app_version,
                    "mode": mode,
                    "selected_mode": self._control_mode(),
                    "created_at": time.time(),
                    "label": str(label or ""),
                    "credentials_included": False,
                    "phone_pairing_included": False,
                    "archives": entries,
                }
                archive.writestr(DUAL_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        total = sum(int(row["size"]) for row in entries)
        return (
            f"Created a dual-archive **{EXPORT_MODES[mode]['label']}** transfer with NovelAI and Local/Anima data "
            f"({_format_bytes(total)} nested data). Credentials and pairing secrets were excluded.  \n`{destination}`",
            destination,
        )

    def _read_dual(self, archive_path: Path) -> Tuple[Dict[str, Any], Dict[str, bytes]]:
        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
            if DUAL_MANIFEST_NAME not in archive.namelist():
                raise KeyError(DUAL_MANIFEST_NAME)
            manifest = json.loads(archive.read(DUAL_MANIFEST_NAME).decode("utf-8"))
            if int(manifest.get("schema_version", 0) or 0) != DUAL_TRANSFER_SCHEMA_VERSION:
                raise ValueError("Unsupported dual-archive transfer version.")
            if manifest.get("credentials_included") or manifest.get("phone_pairing_included"):
                raise ValueError("A transfer claiming to contain private credentials is refused.")
            nested = {}
            for row in manifest.get("archives", []):
                archive_name = str(row.get("archive", "") or "")
                member = str(row.get("archive_path", "") or "")
                if archive_name not in self.archive_roots or member != f"archives/{archive_name}.zip":
                    raise ValueError("Invalid archive routing in dual transfer.")
                data = archive.read(member)
                if len(data) != int(row.get("size", -1)) or _sha256_bytes(data) != str(row.get("sha256", "")):
                    raise ValueError(f"Checksum failed for {archive_name} archive.")
                nested[archive_name] = data
        if set(nested) != set(self.archive_roots):
            raise ValueError("A dual transfer must contain both NovelAI and Local/Anima archives.")
        return manifest, nested

    def preview_import(self, archive_value: Any) -> Tuple[str, str]:
        if isinstance(archive_value, (str, os.PathLike)):
            archive_path = Path(archive_value).expanduser().resolve()
        else:
            archive_path = Path(str(getattr(archive_value, "name", "") or "")).expanduser().resolve()
        try:
            manifest, nested = self._read_dual(archive_path)
        except KeyError:
            return self.active.preview_import(archive_value)
        entries = [{"archive": name, "size": len(data), "sha256": _sha256_bytes(data)} for name, data in sorted(nested.items())]
        identity = {"source_archive": str(archive_path), "source_sha256": _sha256_path(archive_path), "dual_archive": True, "mode": str(manifest.get("mode", "metadata")), "source_version": str(manifest.get("application_version", "unknown")), "selected_mode": str(manifest.get("selected_mode", "novelai")), "entries": entries}
        identity["plan_id"] = _sha256_bytes(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        text = (
            "### Dual-archive import preview\n"
            f"**Source:** `{archive_path.name}` · version `{identity['source_version']}`  \n"
            f"**Contents:** NovelAI + Local/Anima · mode `{identity['mode']}` · {_format_bytes(sum(row['size'] for row in entries))}  \n"
            f"**Restored selected mode:** `{identity['selected_mode']}`  \n"
            "Both archives are checksum-validated independently and restored to separate data roots. No files have changed."
        )
        return text, json.dumps(identity, ensure_ascii=False)

    def apply_import(self, plan_text: str, strategy: str, confirmed: bool) -> str:
        try:
            submitted = json.loads(str(plan_text or ""))
        except json.JSONDecodeError:
            return self.active.apply_import(plan_text, strategy, confirmed)
        if not submitted.get("dual_archive"):
            return self.active.apply_import(plan_text, strategy, confirmed)
        if not confirmed:
            return "Confirm that you reviewed the current dual-archive import preview."
        archive_path = Path(str(submitted.get("source_archive", ""))).resolve()
        if not archive_path.is_file() or _sha256_path(archive_path) != submitted.get("source_sha256"):
            return "Dual-archive import failed: the selected ZIP changed after preview."
        _preview, authoritative_text = self.preview_import(archive_path)
        authoritative = json.loads(authoritative_text)
        if authoritative.get("plan_id") != submitted.get("plan_id"):
            return "Dual-archive import failed: the preview is stale or was modified."
        try:
            manifest, nested = self._read_dual(archive_path)
            results = []
            with tempfile.TemporaryDirectory(prefix="artist-ranker-dual-import-") as temp_name:
                temp_root = Path(temp_name)
                for archive_name in ("novelai", "local"):
                    nested_path = temp_root / f"{archive_name}.zip"
                    nested_path.write_bytes(nested[archive_name])
                    manager = self._manager(archive_name)
                    _nested_preview, nested_plan = manager.preview_import(nested_path)
                    result = manager.apply_import(nested_plan, strategy, True)
                    if "Import applied:" not in result:
                        raise ValueError(f"{archive_name}: {result}")
                    results.append(archive_name)
            selected_mode = str(manifest.get("selected_mode", "novelai") or "novelai").casefold()
            if selected_mode not in self.archive_roots:
                selected_mode = "novelai"
            _atomic_json(self.control_path, {"schema_version": 1, "selected_mode": selected_mode, "restart_requested": False, "request_id": "", "requested_at": 0.0})
            return f"Import applied: restored {', '.join(results)} archives independently. Restart the ranker to activate the transferred selected mode ({selected_mode})."
        except Exception as exc:
            return f"Dual-archive import failed: {type(exc).__name__}: {exc}"
