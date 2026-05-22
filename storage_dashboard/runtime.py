"""Shared scan runtime for web and terminal frontends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import threading

from storage_dashboard.fsevents import FSEventChanges, MacFSEvents
from storage_dashboard import scanner as filesystem_scanner
from storage_dashboard.scanner import SCAN_MODE_EXACT, SCAN_MODE_LAZY, SCAN_MODE_SUMMARY, ScanOptions
from storage_dashboard.store import SnapshotStore

SNAPSHOT_SCHEMA_VERSION = 3
DEFAULT_FRESHNESS = timedelta(minutes=15)


class ScanAlreadyRunningError(RuntimeError):
    """Raised when a scan is already active in this process or another one."""


class LocalScanner:
    """Default scanner adapter for app use."""

    def scan(self, options=None, progress=None) -> object:
        return filesystem_scanner.scan(options=options, progress=progress)


@dataclass(frozen=True)
class ScanStart:
    """Background scan start result."""

    status: str
    thread: threading.Thread | None = None


class AdvisoryScanLock:
    """Small Unix advisory lock around expensive filesystem scans."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file.close()
            return False
        self._file = file
        return True

    def release(self) -> None:
        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class ScanRuntime:
    """Shared status, freshness, locking, and snapshot persistence path."""

    def __init__(
        self,
        *,
        store: SnapshotStore | None = None,
        scanner: object | None = None,
        disk_path: Path | None = None,
        roots: tuple[Path, ...] = filesystem_scanner.DEFAULT_ROOTS,
        freshness: timedelta = DEFAULT_FRESHNESS,
        lock_path: Path | None = None,
        fsevents: object | None = None,
        max_scan_workers: int = 2,
    ) -> None:
        self.store = store or SnapshotStore()
        self.scanner = scanner or LocalScanner()
        self.fsevents = fsevents or MacFSEvents()
        self.disk_path = disk_path or Path.home()
        self.roots = tuple(Path(root).expanduser() for root in roots)
        self.freshness = freshness
        self.lock_path = lock_path or self.store.path.parent / ".scan.lock"
        self.max_scan_workers = max(1, max_scan_workers)
        self._scan_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status: dict[str, object] = self._base_status()

    @property
    def roots_fingerprint(self) -> str:
        return roots_fingerprint(self.roots)

    def disk_summary(self) -> dict[str, object]:
        usage = shutil.disk_usage(self.disk_path)
        percent = 0.0 if usage.total == 0 else round((usage.used / usage.total) * 100, 1)
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": percent,
        }

    def scan_status(self) -> dict[str, object]:
        with self._status_lock:
            status = dict(self._status)
        latest = self.store.latest()
        if latest is not None:
            status["latest_snapshot"] = {
                "id": latest.get("id"),
                "timestamp": latest.get("timestamp"),
            }
        return status

    def latest_compatible_snapshot(self) -> dict[str, object] | None:
        snapshots = self.store.load().get("snapshots", [])
        if not isinstance(snapshots, list):
            return None
        for snapshot in reversed(snapshots):
            if self.is_compatible(snapshot) and snapshot.get("snapshot_status") != "partial":
                return snapshot
        return None

    def is_compatible(self, snapshot: dict[str, object] | None) -> bool:
        if not isinstance(snapshot, dict):
            return False
        return (
            snapshot.get("schema_version") == SNAPSHOT_SCHEMA_VERSION
            and snapshot.get("roots_fingerprint") == self.roots_fingerprint
        )

    def snapshot_age(self, snapshot: dict[str, object] | None) -> timedelta | None:
        if not isinstance(snapshot, dict):
            return None
        timestamp = snapshot.get("timestamp")
        if not isinstance(timestamp, str):
            return None
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)

    def has_fresh_compatible_snapshot(self) -> bool:
        snapshot = self.latest_compatible_snapshot()
        age = self.snapshot_age(snapshot)
        return age is not None and age <= self.freshness

    def ensure_fresh_background(self) -> ScanStart:
        if self.has_fresh_compatible_snapshot():
            return ScanStart("fresh")
        return self.start_background_scan()

    def start_background_scan(self) -> ScanStart:
        if self.scan_status().get("status") == "running":
            return ScanStart("running")

        def target() -> None:
            try:
                self.run_scan()
            except ScanAlreadyRunningError:
                return
            except Exception:
                return

        thread = threading.Thread(target=target, name="lighthouse-scan", daemon=True)
        thread.start()
        return ScanStart("started", thread)

    def run_scan(self) -> dict[str, object]:
        if not self._scan_lock.acquire(blocking=False):
            raise ScanAlreadyRunningError("scan already running")
        advisory_lock = AdvisoryScanLock(self.lock_path)
        if not advisory_lock.acquire():
            self._scan_lock.release()
            raise ScanAlreadyRunningError("scan already running")

        self._set_status(
            status="running",
            phase="starting",
            active_root=None,
            active_path=None,
            completed_roots=[],
            pending_roots=[str(root) for root in self.roots],
            entries_seen=0,
            logs_seen=0,
            started_at=_utc_now(),
            finished_at=None,
            snapshot_id=None,
            error=None,
        )
        try:
            latest = self.latest_compatible_snapshot()
            changes = self._fsevents_changes(latest)
            if self._can_reuse_snapshot(latest, changes):
                entries = latest.get("entries", [])
                logs = latest.get("logs", [])
                self._set_status(
                    status="completed",
                    phase="complete",
                    active_root=None,
                    active_path=None,
                    pending_roots=[],
                    entries_seen=len(entries) if isinstance(entries, list) else 0,
                    logs_seen=len(logs) if isinstance(logs, list) else 0,
                    finished_at=_utc_now(),
                    snapshot_id=latest.get("id"),
                    error=None,
                )
                return latest

            scan_options = self._scan_options(latest, changes)
            fsevents_event_id = self._event_id_for_snapshot(changes)
            if self._should_run_summary_phase(scan_options):
                summary_options = self._summary_scan_options(latest)
                summary_result = self._run_scanner(summary_options)
                summary_snapshot = self._store_scan_result(
                    summary_result,
                    snapshot_status="partial",
                    fsevents_event_id=None,
                )
                self._set_status(
                    status="running",
                    phase="summary_complete",
                    snapshot_id=summary_snapshot["id"],
                    entries_seen=len(summary_snapshot.get("entries", [])),
                    logs_seen=len(summary_snapshot.get("logs", [])),
                    error=None,
                )

            result = self._run_scanner(scan_options)
            snapshot = self._store_scan_result(
                result,
                snapshot_status="exact",
                fsevents_event_id=fsevents_event_id,
            )
            self._set_status(
                status="completed",
                phase="complete",
                active_root=None,
                active_path=None,
                pending_roots=[],
                entries_seen=len(snapshot.get("entries", [])),
                logs_seen=len(snapshot.get("logs", [])),
                finished_at=_utc_now(),
                snapshot_id=snapshot["id"],
                error=None,
            )
            return snapshot
        except Exception:
            self._set_status(status="failed", phase="failed", error="scan_failed", finished_at=_utc_now())
            raise
        finally:
            advisory_lock.release()
            self._scan_lock.release()

    def run_path_scan(self, path: Path) -> dict[str, object]:
        target = self._validate_path_scan_target(path)
        if not self._scan_lock.acquire(blocking=False):
            raise ScanAlreadyRunningError("scan already running")
        advisory_lock = AdvisoryScanLock(self.lock_path)
        if not advisory_lock.acquire():
            self._scan_lock.release()
            raise ScanAlreadyRunningError("scan already running")

        self._set_status(
            status="running",
            phase="lazy",
            active_root=None,
            active_path=str(target),
            completed_roots=[],
            pending_roots=[str(target)],
            entries_seen=0,
            logs_seen=0,
            started_at=_utc_now(),
            finished_at=None,
            snapshot_id=None,
            error=None,
        )
        try:
            result = self._run_scanner(
                ScanOptions(
                    roots=self.roots,
                    mode=SCAN_MODE_LAZY,
                    target_path=target,
                    max_workers=self.max_scan_workers,
                )
            )
            entries = entries_from_result(result)
            logs = logs_from_result(result)
            self._set_status(
                status="completed",
                phase="complete",
                active_root=None,
                active_path=str(target),
                completed_roots=[str(target)],
                pending_roots=[],
                entries_seen=len(entries),
                logs_seen=len(logs),
                finished_at=_utc_now(),
                error=None,
            )
            return {"path": str(target), "entries": entries, "logs": logs}
        except Exception:
            self._set_status(status="failed", phase="failed", error="scan_failed", finished_at=_utc_now())
            raise
        finally:
            advisory_lock.release()
            self._scan_lock.release()

    def _validate_path_scan_target(self, path: Path) -> Path:
        target = Path(path).expanduser()
        if not any(target == root or target.is_relative_to(root) for root in self.roots):
            raise ValueError("scan path outside configured roots")
        if filesystem_scanner._is_denied(target, filesystem_scanner.DENIED_ROOTS):
            raise ValueError("scan path denied")
        if target.is_symlink() or not target.exists():
            raise ValueError("scan path unavailable")
        return target

    def _store_scan_result(
        self,
        result: object,
        *,
        snapshot_status: str,
        fsevents_event_id: int | None,
    ) -> dict[str, object]:
        entries = entries_from_result(result)
        logs = logs_from_result(result)
        snapshot_args = {
            "roots": roots_from_entries(entries, default_roots=self.roots),
            "disk": self.disk_summary(),
            "entries": entries,
            "logs": logs,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "roots_fingerprint": self.roots_fingerprint,
            "snapshot_status": snapshot_status,
        }
        if fsevents_event_id is not None and snapshot_status == "exact":
            snapshot_args["fsevents_event_id"] = fsevents_event_id
            snapshot_args["fsevents_roots_fingerprint"] = self.roots_fingerprint
        return self.store.add_snapshot(**snapshot_args)

    def _should_run_summary_phase(self, options: ScanOptions) -> bool:
        return (
            options.mode == SCAN_MODE_EXACT
            and not options.changed_paths
            and self._scanner_accepts("options")
        )

    def _summary_scan_options(self, latest: dict[str, object] | None) -> ScanOptions:
        previous_entries = ()
        if isinstance(latest, dict):
            previous_entries = tuple(item for item in latest.get("entries", []) if isinstance(item, dict))
        return ScanOptions(
            roots=self.roots,
            previous_entries=previous_entries,
            mode=SCAN_MODE_SUMMARY,
            max_workers=self.max_scan_workers,
        )

    def _scanner_accepts(self, parameter_name: str) -> bool:
        parameters = inspect.signature(self.scanner.scan).parameters
        return parameter_name in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )

    def _run_scanner(self, options: ScanOptions) -> object:
        scan = self.scanner.scan
        kwargs: dict[str, object] = {}
        if self._scanner_accepts("options"):
            kwargs["options"] = options
        if self._scanner_accepts("progress"):
            kwargs["progress"] = self._update_scan_progress
        return scan(**kwargs)

    def _fsevents_changes(self, latest: dict[str, object] | None) -> FSEventChanges | None:
        if not isinstance(latest, dict):
            return None
        if latest.get("fsevents_roots_fingerprint") != self.roots_fingerprint:
            return None
        event_id = latest.get("fsevents_event_id")
        if not isinstance(event_id, int):
            return None
        changes_since = getattr(self.fsevents, "changes_since", None)
        if not callable(changes_since):
            return None
        try:
            return changes_since(self.roots, event_id)
        except Exception:
            return FSEventChanges(supported=False, current_event_id=None, must_full_scan=True)

    def _can_reuse_snapshot(
        self,
        latest: dict[str, object] | None,
        changes: FSEventChanges | None,
    ) -> bool:
        return (
            isinstance(latest, dict)
            and changes is not None
            and changes.supported
            and not changes.must_full_scan
            and not changes.changed_paths
        )

    def _scan_options(self, latest: dict[str, object] | None, changes: FSEventChanges | None) -> ScanOptions:
        if (
            isinstance(latest, dict)
            and changes is not None
            and changes.supported
            and not changes.must_full_scan
            and changes.changed_paths
        ):
            previous_entries = tuple(item for item in latest.get("entries", []) if isinstance(item, dict))
            if previous_entries:
                return ScanOptions(
                    roots=self.roots,
                    changed_paths=tuple(Path(path).expanduser() for path in changes.changed_paths),
                    previous_entries=previous_entries,
                    mode=SCAN_MODE_EXACT,
                    max_workers=self.max_scan_workers,
                )
        return ScanOptions(roots=self.roots, mode=SCAN_MODE_EXACT, max_workers=self.max_scan_workers)

    def _event_id_for_snapshot(self, changes: FSEventChanges | None) -> int | None:
        if changes is not None and changes.current_event_id is not None:
            return changes.current_event_id
        current_event_id = getattr(self.fsevents, "current_event_id", None)
        if not callable(current_event_id):
            return None
        try:
            event_id = current_event_id()
        except Exception:
            return None
        return event_id if isinstance(event_id, int) else None

    def _update_scan_progress(self, progress: dict[str, object]) -> None:
        allowed = {
            "phase",
            "active_root",
            "active_path",
            "completed_roots",
            "pending_roots",
            "entries_seen",
            "logs_seen",
        }
        update = {key: progress[key] for key in allowed if key in progress}
        for key in ("completed_roots", "pending_roots"):
            if key in update and isinstance(update[key], list):
                update[key] = list(update[key])
        update["status"] = "running"
        self._set_status(**update)

    def _set_status(self, **updates: object) -> None:
        with self._status_lock:
            status = dict(self._status)
            status.update(updates)
            self._status = status

    def _base_status(self) -> dict[str, object]:
        return {
            "status": "idle",
            "phase": "idle",
            "active_root": None,
            "active_path": None,
            "completed_roots": [],
            "pending_roots": [],
            "entries_seen": 0,
            "logs_seen": 0,
            "started_at": None,
            "finished_at": None,
            "snapshot_id": None,
            "error": None,
        }


def roots_fingerprint(roots: tuple[Path, ...]) -> str:
    payload = json.dumps([str(Path(root).expanduser()) for root in roots], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def entries_from_result(result: object) -> list[dict[str, object]]:
    if isinstance(result, dict):
        entries = result.get("entries", [])
    else:
        entries = getattr(result, "entries", [])
    return [entry_to_dict(entry) for entry in entries]


def logs_from_result(result: object) -> list[dict[str, object]]:
    if isinstance(result, dict):
        logs = result.get("logs", [])
    else:
        logs = getattr(result, "logs", [])
    return [log_to_dict(log) for log in logs]


def entry_to_dict(entry: object) -> dict[str, object]:
    if isinstance(entry, dict):
        return dict(entry)
    to_dict = getattr(entry, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {}


def log_to_dict(log: object) -> dict[str, object]:
    if isinstance(log, dict):
        return dict(log)
    return {
        "path": str(getattr(log, "path", "")),
        "event": str(getattr(log, "event", "")),
        "message": str(getattr(log, "message", "")),
    }


def roots_from_entries(entries: list[dict[str, object]], *, default_roots: tuple[Path, ...]) -> list[str]:
    roots = []
    for entry in entries:
        root = entry.get("root")
        if root is not None and str(root) not in roots:
            roots.append(str(root))
    if roots:
        return roots
    return [str(root) for root in default_roots]
