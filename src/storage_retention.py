#!/usr/bin/env python3
"""Safe, restart-aware image retention for NovelAI Artist Ranker.

The manager is intentionally independent from Gradio and ranking callbacks. It only
operates on snapshots supplied by the ranker, re-checks protected paths immediately
before each move/delete, and records quarantine state atomically.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from historical_media import COMPARISON_KIND, HistoricalMediaResolver

RETENTION_SCHEMA_VERSION = 1
DEFAULT_RETENTION_SETTINGS: Dict[str, Any] = {
    "image_retention_policy": "keep_all",
    "retention_keep_latest_duels": 1000,
    "retention_keep_days": 90.0,
    "retention_max_storage_gb": 0.0,
    "retention_keep_thumbnails": True,
    "retention_quarantine_days": 7.0,
    "retention_auto_cleanup": False,
    "retention_auto_interval_hours": 24.0,
    "retention_last_auto_run": 0.0,
}


def _clamp_number(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def normalize_retention_settings(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    settings = dict(DEFAULT_RETENTION_SETTINGS)
    if isinstance(raw, dict):
        settings.update(raw)
    policy = str(settings.get("image_retention_policy", "keep_all") or "keep_all").strip().casefold()
    if policy not in {"keep_all", "managed_cleanup"}:
        policy = "keep_all"
    settings["image_retention_policy"] = policy
    settings["retention_keep_latest_duels"] = int(
        _clamp_number(settings.get("retention_keep_latest_duels"), 0, 1_000_000, 1000)
    )
    settings["retention_keep_days"] = _clamp_number(
        settings.get("retention_keep_days"), 0.0, 36500.0, 90.0
    )
    settings["retention_max_storage_gb"] = _clamp_number(
        settings.get("retention_max_storage_gb"), 0.0, 1_000_000.0, 0.0
    )
    settings["retention_keep_thumbnails"] = bool(settings.get("retention_keep_thumbnails", True))
    settings["retention_quarantine_days"] = _clamp_number(
        settings.get("retention_quarantine_days"), 0.0, 3650.0, 7.0
    )
    settings["retention_auto_cleanup"] = bool(settings.get("retention_auto_cleanup", False))
    settings["retention_auto_interval_hours"] = _clamp_number(
        settings.get("retention_auto_interval_hours"), 1.0, 8760.0, 24.0
    )
    settings["retention_last_auto_run"] = max(
        0.0, _clamp_number(settings.get("retention_last_auto_run"), 0.0, 10**12, 0.0)
    )
    return settings


def _atomic_write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _safe_stat(path: Path) -> Optional[os.stat_result]:
    try:
        if path.is_file():
            return path.stat()
    except (OSError, PermissionError):
        pass
    return None


def _normalized_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.abspath(text))
    except Exception:
        return str(Path(text))


@dataclass(frozen=True)
class RetentionCandidate:
    history_number: int
    duel_id: str
    side: str
    original_path: str
    size_bytes: int
    duel_timestamp: float
    reason: str


@dataclass
class RetentionPlan:
    plan_id: str
    created_at: float
    policy: Dict[str, Any]
    history_count: int
    original_file_count: int
    original_bytes: int
    protected_file_count: int
    protected_bytes: int
    selected: List[RetentionCandidate] = field(default_factory=list)
    projected_original_bytes: int = 0
    max_storage_bytes: int = 0
    cap_unreachable_bytes: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def selected_bytes(self) -> int:
        return sum(int(item.size_bytes) for item in self.selected)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["selected_bytes"] = self.selected_bytes
        return data


@dataclass
class RetentionAction:
    cleanup_id: str
    history_number: int
    duel_id: str
    side: str
    original_path: str
    thumbnail_path: str
    quarantine_path: str
    size_bytes: int
    status: str
    acted_at: float
    expires_at: float
    error: str = ""


@dataclass
class RetentionResult:
    cleanup_id: str
    started_at: float
    completed_at: float
    requested_count: int
    moved_count: int
    deleted_count: int
    skipped_count: int
    failed_count: int
    original_bytes_released: int
    disk_bytes_reclaimed_now: int
    actions: List[RetentionAction] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RetentionManager:
    """Build and execute safe retention plans with optional recovery quarantine."""

    def __init__(
        self,
        quarantine_dir: Path,
        state_file: Path,
        comparison_root: Optional[Path] = None,
    ):
        self.quarantine_dir = Path(quarantine_dir)
        self.state_file = Path(state_file)
        self.lock = threading.RLock()
        self.media_resolver = (
            HistoricalMediaResolver(comparison_root)
            if comparison_root is not None
            else None
        )
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    def _empty_state(self) -> Dict[str, Any]:
        return {
            "schema_version": RETENTION_SCHEMA_VERSION,
            "runs": [],
            "items": [],
            "updated_at": time.time(),
        }

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_file.is_file():
            return self._empty_state()
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return self._empty_state()
            raw.setdefault("schema_version", RETENTION_SCHEMA_VERSION)
            raw.setdefault("runs", [])
            raw.setdefault("items", [])
            return raw
        except Exception:
            return self._empty_state()

    def _save_state_locked(self) -> None:
        self.state["schema_version"] = RETENTION_SCHEMA_VERSION
        self.state["updated_at"] = time.time()
        self.state["runs"] = list(self.state.get("runs", []))[-200:]
        self.state["items"] = list(self.state.get("items", []))[-100000:]
        _atomic_write_json(self.state_file, self.state)

    @staticmethod
    def _record_duel_id(record: Dict[str, Any], history_number: int) -> str:
        explicit = str(record.get("duel_id", "") or "").strip()
        if explicit:
            return explicit
        payload = "|".join([
            str(history_number),
            str(record.get("timestamp", 0) or 0),
            str(record.get("image_a_path", "") or ""),
            str(record.get("image_b_path", "") or ""),
        ])
        return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()[:20]

    def build_plan(
        self,
        history_records: Sequence[Dict[str, Any]],
        protected_paths: Iterable[str],
        settings: Dict[str, Any],
        now: Optional[float] = None,
    ) -> RetentionPlan:
        now = float(now or time.time())
        policy = normalize_retention_settings(settings)
        history = [dict(record) for record in history_records]
        protected = {_normalized_path(path) for path in protected_paths if str(path or "").strip()}
        keep_latest = int(policy["retention_keep_latest_duels"])
        keep_days = float(policy["retention_keep_days"])
        cutoff = now - keep_days * 86400.0
        max_bytes = int(float(policy["retention_max_storage_gb"]) * (1024 ** 3))
        latest_start = max(0, len(history) - keep_latest) if keep_latest > 0 else len(history)

        inventory: List[Tuple[RetentionCandidate, bool, bool, bool]] = []
        seen_paths: Set[str] = set()
        original_bytes = 0
        original_count = 0
        protected_bytes = 0
        protected_count = 0

        if self.media_resolver is not None:
            self.media_resolver.prime_history(history)
        for index, record in enumerate(history):
            history_number = index + 1
            duel_id = self._record_duel_id(record, history_number)
            timestamp = float(record.get("timestamp", 0.0) or 0.0)
            latest = index >= latest_start
            age_kept = keep_days > 0 and (timestamp <= 0 or timestamp > cutoff)
            for side, field_name in (("A", "image_a_path"), ("B", "image_b_path")):
                raw_path = str(record.get(field_name, "") or "").strip()
                normalized = _normalized_path(raw_path)
                if not normalized:
                    continue
                if self.media_resolver is not None:
                    resolution = self.media_resolver.resolve(
                        raw_path,
                        record=record,
                        side=side,
                        default_kind=COMPARISON_KIND,
                    )
                    if not resolution.available:
                        continue
                    path = Path(resolution.resolved_path)
                    normalized = _normalized_path(path)
                else:
                    path = Path(raw_path)
                if normalized in seen_paths:
                    continue
                seen_paths.add(normalized)
                stat = _safe_stat(path)
                if stat is None:
                    continue
                original_count += 1
                original_bytes += int(stat.st_size)
                special_protected = normalized in protected
                if special_protected or latest:
                    protected_count += 1
                    protected_bytes += int(stat.st_size)
                candidate = RetentionCandidate(
                    history_number=history_number,
                    duel_id=duel_id,
                    side=side,
                    original_path=str(path),
                    size_bytes=int(stat.st_size),
                    duel_timestamp=timestamp,
                    reason="",
                )
                inventory.append((candidate, special_protected, latest, age_kept))

        seed = f"{now}|{len(history)}|{original_count}|{original_bytes}|{json.dumps(policy, sort_keys=True)}"
        plan = RetentionPlan(
            plan_id=hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20],
            created_at=now,
            policy=policy,
            history_count=len(history),
            original_file_count=original_count,
            original_bytes=original_bytes,
            protected_file_count=protected_count,
            protected_bytes=protected_bytes,
            projected_original_bytes=original_bytes,
            max_storage_bytes=max_bytes,
        )

        if policy["image_retention_policy"] != "managed_cleanup":
            plan.notes.append("Managed cleanup is disabled; all originals are retained.")
            return plan

        selected_paths: Set[str] = set()
        selected: List[RetentionCandidate] = []

        # Normal expiry: special protection and latest-duel retention are hard.
        # Age retention is honored unless a configured size cap needs additional relief.
        for candidate, special_protected, latest, age_kept in inventory:
            if special_protected or latest or age_kept:
                continue
            selected.append(RetentionCandidate(**{**asdict(candidate), "reason": "older than the configured age and outside the latest-duel window"}))
            selected_paths.add(_normalized_path(candidate.original_path))

        projected = original_bytes - sum(item.size_bytes for item in selected)

        # The size cap may override the age window, but never favorites/current/buffer
        # protection and never the latest-duel hard floor.
        if max_bytes > 0 and projected > max_bytes:
            additional = [
                candidate
                for candidate, special_protected, latest, _age_kept in inventory
                if not special_protected
                and not latest
                and _normalized_path(candidate.original_path) not in selected_paths
            ]
            additional.sort(key=lambda item: (item.duel_timestamp or 0.0, item.history_number, item.side))
            for candidate in additional:
                if projected <= max_bytes:
                    break
                selected.append(RetentionCandidate(**{**asdict(candidate), "reason": "needed to approach the configured storage-size cap"}))
                selected_paths.add(_normalized_path(candidate.original_path))
                projected -= candidate.size_bytes

        selected.sort(key=lambda item: (item.duel_timestamp or 0.0, item.history_number, item.side))
        plan.selected = selected
        plan.projected_original_bytes = max(0, original_bytes - plan.selected_bytes)
        if max_bytes > 0 and plan.projected_original_bytes > max_bytes:
            plan.cap_unreachable_bytes = plan.projected_original_bytes - max_bytes
            plan.notes.append(
                "The size target cannot be reached without touching protected or latest-duel originals."
            )

        unprotected_outside_latest = [
            candidate
            for candidate, special_protected, latest, _age_kept in inventory
            if not special_protected and not latest
        ]
        too_new_outside_latest = [
            candidate
            for candidate, special_protected, latest, age_kept in inventory
            if not special_protected and not latest and age_kept
        ]
        expired_outside_latest = [
            candidate
            for candidate, special_protected, latest, age_kept in inventory
            if not special_protected and not latest and not age_kept
        ]
        if not selected:
            plan.notes.append("No original images currently meet the cleanup rules.")
            if not unprotected_outside_latest:
                plan.notes.append(
                    "Every scanned original is protected by the latest-duel floor or another hard-protection rule."
                )
            else:
                plan.notes.append(
                    f"{len(unprotected_outside_latest):,} unprotected originals are outside the latest-duel floor."
                )
                if keep_days > 0 and not expired_outside_latest:
                    plan.notes.append(
                        f"All {len(too_new_outside_latest):,} of those originals are newer than the configured {keep_days:g}-day age limit."
                    )
                if max_bytes <= 0:
                    plan.notes.append(
                        "No storage-size target is configured, so the age rule is the only rule that can select them."
                    )
                elif original_bytes <= max_bytes:
                    plan.notes.append(
                        f"Current original storage is already at or below the configured {policy['retention_max_storage_gb']:g} GB target."
                    )
            plan.notes.append(
                "To test managed cleanup immediately, lower the age limit, lower the storage target, or reduce the latest-duel floor, then preview again."
            )
        return plan

    def execute_plan(
        self,
        plan: RetentionPlan,
        protected_paths_provider: Callable[[], Iterable[str]],
        thumbnail_factory: Optional[Callable[[str], Optional[str]]] = None,
        now: Optional[float] = None,
    ) -> RetentionResult:
        now = float(now or time.time())
        cleanup_id = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + "_" + plan.plan_id[:8]
        quarantine_days = float(plan.policy.get("retention_quarantine_days", 0.0) or 0.0)
        keep_thumbnails = bool(plan.policy.get("retention_keep_thumbnails", True))
        actions: List[RetentionAction] = []
        moved = deleted = skipped = failed = released = reclaimed = 0
        cleanup_root = self.quarantine_dir / cleanup_id

        for candidate in plan.selected:
            protected_now = {
                _normalized_path(path)
                for path in protected_paths_provider()
                if str(path or "").strip()
            }
            original = Path(candidate.original_path)
            normalized = _normalized_path(original)
            if normalized in protected_now:
                skipped += 1
                actions.append(RetentionAction(
                    cleanup_id, candidate.history_number, candidate.duel_id, candidate.side,
                    str(original), "", "", candidate.size_bytes, "skipped_protected", now, 0.0,
                ))
                continue
            stat = _safe_stat(original)
            if stat is None:
                skipped += 1
                actions.append(RetentionAction(
                    cleanup_id, candidate.history_number, candidate.duel_id, candidate.side,
                    str(original), "", "", candidate.size_bytes, "skipped_missing", now, 0.0,
                ))
                continue

            thumbnail_path = ""
            if keep_thumbnails and thumbnail_factory is not None:
                try:
                    thumbnail_path = str(thumbnail_factory(str(original)) or "")
                    if thumbnail_path and _normalized_path(thumbnail_path) == normalized:
                        # A fallback to the original is not a retained thumbnail.
                        thumbnail_path = ""
                except Exception:
                    thumbnail_path = ""

            try:
                size = int(stat.st_size)
                if quarantine_days > 0:
                    cleanup_root.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha1(str(original).encode("utf-8", errors="replace")).hexdigest()[:12]
                    destination = cleanup_root / f"h{candidate.history_number:08d}_{candidate.side}_{digest}_{original.name}"
                    if destination.exists():
                        destination = cleanup_root / f"{destination.stem}_{time.time_ns()}{destination.suffix}"
                    shutil.move(str(original), str(destination))
                    expires_at = now + quarantine_days * 86400.0
                    status = "quarantined"
                    quarantine_path = str(destination)
                    moved += 1
                else:
                    original.unlink()
                    expires_at = 0.0
                    status = "removed"
                    quarantine_path = ""
                    deleted += 1
                    reclaimed += size
                released += size
                actions.append(RetentionAction(
                    cleanup_id, candidate.history_number, candidate.duel_id, candidate.side,
                    str(original), thumbnail_path, quarantine_path, size, status, now, expires_at,
                ))
            except Exception as exc:
                failed += 1
                actions.append(RetentionAction(
                    cleanup_id, candidate.history_number, candidate.duel_id, candidate.side,
                    str(original), thumbnail_path, "", int(stat.st_size), "failed", now, 0.0,
                    error=f"{type(exc).__name__}: {exc}",
                ))

        completed = time.time()
        result = RetentionResult(
            cleanup_id=cleanup_id,
            started_at=now,
            completed_at=completed,
            requested_count=len(plan.selected),
            moved_count=moved,
            deleted_count=deleted,
            skipped_count=skipped,
            failed_count=failed,
            original_bytes_released=released,
            disk_bytes_reclaimed_now=reclaimed,
            actions=actions,
        )
        with self.lock:
            self.state.setdefault("runs", []).append({
                "cleanup_id": cleanup_id,
                "plan_id": plan.plan_id,
                "started_at": now,
                "completed_at": completed,
                "requested_count": len(plan.selected),
                "moved_count": moved,
                "deleted_count": deleted,
                "skipped_count": skipped,
                "failed_count": failed,
                "original_bytes_released": released,
                "disk_bytes_reclaimed_now": reclaimed,
            })
            for action in actions:
                if action.status == "quarantined":
                    self.state.setdefault("items", []).append(asdict(action))
            self._save_state_locked()
        return result

    def quarantine_summary(self) -> Dict[str, int]:
        with self.lock:
            items = list(self.state.get("items", []))
        count = 0
        total = 0
        for item in items:
            if str(item.get("status", "")) != "quarantined":
                continue
            path = Path(str(item.get("quarantine_path", "") or ""))
            stat = _safe_stat(path)
            if stat is None:
                continue
            count += 1
            total += int(stat.st_size)
        return {"count": count, "bytes": total}

    def purge_expired(self, now: Optional[float] = None, force_all: bool = False) -> List[RetentionAction]:
        now = float(now or time.time())
        purged: List[RetentionAction] = []
        with self.lock:
            for item in self.state.get("items", []):
                if str(item.get("status", "")) != "quarantined":
                    continue
                expires = float(item.get("expires_at", 0.0) or 0.0)
                if not force_all and (expires <= 0 or expires > now):
                    continue
                path = Path(str(item.get("quarantine_path", "") or ""))
                try:
                    if path.is_file():
                        path.unlink()
                    item["status"] = "purged"
                    item["purged_at"] = now
                    purged.append(RetentionAction(
                        cleanup_id=str(item.get("cleanup_id", "") or ""),
                        history_number=int(item.get("history_number", 0) or 0),
                        duel_id=str(item.get("duel_id", "") or ""),
                        side=str(item.get("side", "") or ""),
                        original_path=str(item.get("original_path", "") or ""),
                        thumbnail_path=str(item.get("thumbnail_path", "") or ""),
                        quarantine_path=str(item.get("quarantine_path", "") or ""),
                        size_bytes=int(item.get("size_bytes", 0) or 0),
                        status="purged",
                        acted_at=now,
                        expires_at=float(item.get("expires_at", 0.0) or 0.0),
                        error="",
                    ))
                except Exception as exc:
                    item["purge_error"] = f"{type(exc).__name__}: {exc}"
            self._save_state_locked()
        self._remove_empty_quarantine_dirs()
        return purged

    def restore_latest_cleanup(self, now: Optional[float] = None) -> List[RetentionAction]:
        now = float(now or time.time())
        restored: List[RetentionAction] = []
        with self.lock:
            available = [
                item for item in self.state.get("items", [])
                if str(item.get("status", "")) == "quarantined"
                and Path(str(item.get("quarantine_path", "") or "")).is_file()
            ]
            if not available:
                return []
            latest_cleanup = max(available, key=lambda item: float(item.get("acted_at", 0.0) or 0.0)).get("cleanup_id", "")
            for item in available:
                if str(item.get("cleanup_id", "")) != str(latest_cleanup):
                    continue
                source = Path(str(item.get("quarantine_path", "") or ""))
                destination = Path(str(item.get("original_path", "") or ""))
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        item["restore_error"] = "Original path already exists."
                        continue
                    shutil.move(str(source), str(destination))
                    item["status"] = "restored"
                    item["restored_at"] = now
                    restored.append(RetentionAction(
                        cleanup_id=str(item.get("cleanup_id", "") or ""),
                        history_number=int(item.get("history_number", 0) or 0),
                        duel_id=str(item.get("duel_id", "") or ""),
                        side=str(item.get("side", "") or ""),
                        original_path=str(item.get("original_path", "") or ""),
                        thumbnail_path=str(item.get("thumbnail_path", "") or ""),
                        quarantine_path=str(item.get("quarantine_path", "") or ""),
                        size_bytes=int(item.get("size_bytes", 0) or 0),
                        status="restored",
                        acted_at=now,
                        expires_at=float(item.get("expires_at", 0.0) or 0.0),
                        error="",
                    ))
                except Exception as exc:
                    item["restore_error"] = f"{type(exc).__name__}: {exc}"
            self._save_state_locked()
        self._remove_empty_quarantine_dirs()
        return restored

    def _remove_empty_quarantine_dirs(self) -> None:
        try:
            for directory in sorted(
                [path for path in self.quarantine_dir.rglob("*") if path.is_dir()],
                key=lambda path: len(path.parts), reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        except Exception:
            pass
