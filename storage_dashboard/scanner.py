"""Read-only filesystem scanner for local storage metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from typing import Callable

from storage_dashboard.classifier import SOURCE_MARKERS, Classification, classify_path

ProgressCallback = Callable[[dict[str, object]], None]

DEFAULT_ROOTS = (
    Path.home() / "Downloads",
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Movies",
    Path.home() / "Pictures",
)
DENIED_ROOTS = (
    Path("/System"),
    Path("/bin"),
    Path("/sbin"),
    Path("/usr"),
    Path("/private"),
    Path("/Library"),
    Path("/Applications"),
)


@dataclass(frozen=True)
class ScanOptions:
    """Configuration for read-only filesystem scanning."""

    roots: tuple[Path, ...] = DEFAULT_ROOTS
    denied_roots: tuple[Path, ...] = DENIED_ROOTS
    read_only: bool = True
    excluded_names: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                ".Trash",
                ".git",
                "dist",
                "build",
                ".next",
                "target",
                ".pytest_cache",
                ".mypy_cache",
                "__pycache__",
                "node_modules",
                ".venv",
                "venv",
            }
        )
    )


@dataclass(frozen=True)
class ScanLog:
    """Non-fatal scanner event."""

    path: Path
    event: str
    message: str


@dataclass(frozen=True)
class ScanEntry:
    """Metadata for one file or folder."""

    path: Path
    kind: str
    size_bytes: int
    modified_at: str | None
    file_count: int | None
    root: Path
    classification: str
    reason: str
    risk: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "file_count": self.file_count,
            "root": str(self.root),
            "classification": self.classification,
            "reason": self.reason,
            "risk": self.risk,
        }


@dataclass(frozen=True)
class ScanResult:
    """Scanner output and non-fatal logs."""

    entries: tuple[ScanEntry, ...]
    logs: tuple[ScanLog, ...]


def normalize_roots(roots: tuple[str | Path, ...]) -> tuple[Path, ...]:
    """Return exact local root paths as expanded Path objects without resolving symlinks."""

    return tuple(Path(root).expanduser() for root in roots)


def scan(options: ScanOptions | None = None, progress: ProgressCallback | None = None) -> ScanResult:
    """Scan configured roots without modifying filesystem state."""

    options = options or ScanOptions()
    if not options.read_only:
        raise ValueError("Scanner only supports read-only operation.")

    entries: list[ScanEntry] = []
    logs: list[ScanLog] = []
    roots = normalize_roots(tuple(options.roots))
    completed_roots: list[str] = []

    _emit_progress(
        progress,
        phase="starting",
        active_root=None,
        active_path=None,
        completed_roots=list(completed_roots),
        pending_roots=[str(root) for root in roots],
        entries_seen=0,
        logs_seen=0,
    )

    for index, root in enumerate(roots):
        _emit_progress(
            progress,
            phase="root",
            active_root=str(root),
            active_path=str(root),
            completed_roots=list(completed_roots),
            pending_roots=[str(item) for item in roots[index:]],
            entries_seen=len(entries),
            logs_seen=len(logs),
        )
        if _is_denied(root, options.denied_roots):
            entries.append(_denied_entry(root))
            logs.append(ScanLog(root, "denied_root", "Skipped denied root."))
            completed_roots.append(str(root))
            _emit_progress(
                progress,
                phase="root_complete",
                active_root=str(root),
                active_path=str(root),
                completed_roots=list(completed_roots),
                pending_roots=[str(item) for item in roots[index + 1 :]],
                entries_seen=len(entries),
                logs_seen=len(logs),
            )
            continue

        entry = _scan_path(root, root, options, logs, progress)
        if entry is not None:
            entries.extend(entry)
        completed_roots.append(str(root))
        _emit_progress(
            progress,
            phase="root_complete",
            active_root=str(root),
            active_path=str(root),
            completed_roots=list(completed_roots),
            pending_roots=[str(item) for item in roots[index + 1 :]],
            entries_seen=len(entries),
            logs_seen=len(logs),
        )

    _emit_progress(
        progress,
        phase="complete",
        active_root=None,
        active_path=None,
        completed_roots=list(completed_roots),
        pending_roots=[],
        entries_seen=len(entries),
        logs_seen=len(logs),
    )

    return ScanResult(tuple(entries), tuple(logs))


def _scan_path(
    path: Path,
    root: Path,
    options: ScanOptions,
    logs: list[ScanLog],
    progress: ProgressCallback | None = None,
) -> list[ScanEntry] | None:
    _emit_path_progress(progress, "checking", path, root, logs)
    if _is_denied(path, options.denied_roots):
        logs.append(ScanLog(path, "denied_root", "Skipped denied path."))
        return [_denied_entry(path, root=root)]

    stat_result = _stat_path(path, logs)
    if stat_result is None:
        return None

    if stat.S_ISLNK(stat_result.st_mode):
        logs.append(ScanLog(path, "symlink_skipped", "Skipped symlink."))
        return None

    if stat.S_ISDIR(stat_result.st_mode):
        _emit_path_progress(progress, "directory", path, root, logs)
        directory_fd = _open_directory_path(path, logs)
        if directory_fd is None:
            return None
        try:
            return _scan_directory(path, root, stat_result.st_mtime, directory_fd, options, logs, progress)
        finally:
            os.close(directory_fd)

    _emit_path_progress(progress, "file", path, root, logs)
    modified_at = _datetime_modified(stat_result.st_mtime)
    classification = classify_path(
        path,
        root=root,
        is_dir=False,
        size_bytes=stat_result.st_size,
        modified_at=modified_at,
    )
    return [
        _entry_from_classification(
            path=path,
            kind="file",
            size_bytes=stat_result.st_size,
            modified_at=modified_at.isoformat(),
            file_count=None,
            root=root,
            classification=classification,
        )
    ]


def _scan_directory(
    path: Path,
    root: Path,
    modified_timestamp: float,
    directory_fd: int,
    options: ScanOptions,
    logs: list[ScanLog],
    progress: ProgressCallback | None = None,
) -> list[ScanEntry]:
    _emit_path_progress(progress, "directory", path, root, logs)
    child_entries: list[ScanEntry] = []
    size_bytes = 0
    file_count = 0

    if path.name in options.excluded_names:
        size_bytes, file_count = _measure_directory(path, directory_fd, options, logs, progress, root)
        modified_at = _datetime_modified(modified_timestamp)
        classification = classify_path(
            path,
            root=root,
            is_dir=True,
            size_bytes=size_bytes,
            modified_at=modified_at,
            source_marker_present=_has_source_marker(directory_fd, logs, path),
        )
        return [
            _entry_from_classification(
                path=path,
                kind="folder",
                size_bytes=size_bytes,
                modified_at=modified_at.isoformat(),
                file_count=file_count,
                root=root,
                classification=classification,
            )
        ]

    children = _list_directory_names(directory_fd, path, logs)

    for child_name in children:
        child_path = path / child_name
        scanned = _scan_child(child_name, child_path, root, directory_fd, options, logs, progress)
        if scanned is None:
            continue
        child_entries.extend(scanned)
        direct_entry = scanned[0]
        size_bytes += direct_entry.size_bytes
        if direct_entry.kind == "folder":
            file_count += direct_entry.file_count or 0
        else:
            file_count += 1

    modified_at = _datetime_modified(modified_timestamp)
    classification = classify_path(
        path,
        root=root,
        is_dir=True,
        size_bytes=size_bytes,
        modified_at=modified_at,
        source_marker_present=_has_source_marker(directory_fd, logs, path),
    )
    current = _entry_from_classification(
        path=path,
        kind="folder",
        size_bytes=size_bytes,
        modified_at=modified_at.isoformat(),
        file_count=file_count,
        root=root,
        classification=classification,
    )
    return [current, *child_entries]


def _entry_from_classification(
    *,
    path: Path,
    kind: str,
    size_bytes: int,
    modified_at: str | None,
    file_count: int | None,
    root: Path,
    classification: Classification,
) -> ScanEntry:
    return ScanEntry(
        path=path,
        kind=kind,
        size_bytes=size_bytes,
        modified_at=modified_at,
        file_count=file_count,
        root=root,
        classification=classification.classification,
        reason=classification.reason,
        risk=classification.risk,
    )


def _denied_entry(path: Path, root: Path | None = None) -> ScanEntry:
    classification = classify_path(path, root=root or path, denied=True)
    return _entry_from_classification(
        path=path,
        kind="folder",
        size_bytes=0,
        modified_at=None,
        file_count=None,
        root=root or path,
        classification=classification,
    )


def _is_denied(path: Path, denied_roots: tuple[Path, ...]) -> bool:
    normalized = _normalize_without_resolving_symlinks(path)
    for denied_root in denied_roots:
        denied = _normalize_without_resolving_symlinks(denied_root)
        if normalized == denied or normalized.is_relative_to(denied):
            return True
    return False


def _normalize_without_resolving_symlinks(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _scan_child(
    child_name: str,
    child_path: Path,
    root: Path,
    parent_fd: int,
    options: ScanOptions,
    logs: list[ScanLog],
    progress: ProgressCallback | None = None,
) -> list[ScanEntry] | None:
    _emit_path_progress(progress, "checking", child_path, root, logs)
    if _is_denied(child_path, options.denied_roots):
        logs.append(ScanLog(child_path, "denied_root", "Skipped denied path."))
        return [_denied_entry(child_path, root=root)]

    stat_result = _stat_child(child_name, child_path, parent_fd, logs)
    if stat_result is None:
        return None

    if stat.S_ISLNK(stat_result.st_mode):
        logs.append(ScanLog(child_path, "symlink_skipped", "Skipped symlink."))
        return None

    if stat.S_ISDIR(stat_result.st_mode):
        _emit_path_progress(progress, "directory", child_path, root, logs)
        directory_fd = _open_directory_child(child_name, child_path, parent_fd, logs)
        if directory_fd is None:
            return None
        try:
            return _scan_directory(child_path, root, stat_result.st_mtime, directory_fd, options, logs, progress)
        finally:
            os.close(directory_fd)

    _emit_path_progress(progress, "file", child_path, root, logs)
    modified_at = _datetime_modified(stat_result.st_mtime)
    classification = classify_path(
        child_path,
        root=root,
        is_dir=False,
        size_bytes=stat_result.st_size,
        modified_at=modified_at,
    )
    return [
        _entry_from_classification(
            path=child_path,
            kind="file",
            size_bytes=stat_result.st_size,
            modified_at=modified_at.isoformat(),
            file_count=None,
            root=root,
            classification=classification,
        )
    ]


def _measure_directory(
    path: Path,
    directory_fd: int,
    options: ScanOptions,
    logs: list[ScanLog],
    progress: ProgressCallback | None = None,
    root: Path | None = None,
) -> tuple[int, int]:
    _emit_path_progress(progress, "directory", path, root or path, logs)
    size_bytes = 0
    file_count = 0

    for child_name in _list_directory_names(directory_fd, path, logs):
        child_path = path / child_name
        _emit_path_progress(progress, "checking", child_path, root or path, logs)
        stat_result = _stat_child(child_name, child_path, directory_fd, logs)
        if stat_result is None:
            continue

        if stat.S_ISLNK(stat_result.st_mode):
            logs.append(ScanLog(child_path, "symlink_skipped", "Skipped symlink."))
            continue

        if _is_denied(child_path, options.denied_roots):
            logs.append(ScanLog(child_path, "denied_root", "Skipped denied path."))
            continue

        if stat.S_ISDIR(stat_result.st_mode):
            _emit_path_progress(progress, "directory", child_path, root or path, logs)
            child_fd = _open_directory_child(child_name, child_path, directory_fd, logs)
            if child_fd is None:
                continue
            try:
                child_size, child_count = _measure_directory(child_path, child_fd, options, logs, progress, root or path)
            finally:
                os.close(child_fd)
            size_bytes += child_size
            file_count += child_count
            continue

        _emit_path_progress(progress, "file", child_path, root or path, logs)
        size_bytes += stat_result.st_size
        file_count += 1

    return size_bytes, file_count


def _emit_path_progress(
    progress: ProgressCallback | None,
    phase: str,
    path: Path,
    root: Path,
    logs: list[ScanLog],
) -> None:
    _emit_progress(
        progress,
        phase=phase,
        active_root=str(root),
        active_path=str(path),
        logs_seen=len(logs),
    )


def _emit_progress(progress: ProgressCallback | None, **payload: object) -> None:
    if progress is not None:
        progress(payload)


def _stat_path(path: Path, logs: list[ScanLog]) -> os.stat_result | None:
    try:
        return path.stat(follow_symlinks=False)
    except PermissionError as exc:
        logs.append(ScanLog(path, "permission_denied", str(exc)))
        return None
    except OSError as exc:
        logs.append(ScanLog(path, "stat_error", str(exc)))
        return None


def _stat_child(
    name: str,
    path: Path,
    directory_fd: int,
    logs: list[ScanLog],
) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except PermissionError as exc:
        logs.append(ScanLog(path, "permission_denied", str(exc)))
        return None
    except OSError as exc:
        logs.append(ScanLog(path, "stat_error", str(exc)))
        return None


def _open_directory_path(path: Path, logs: list[ScanLog]) -> int | None:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except PermissionError as exc:
        logs.append(ScanLog(path, "permission_denied", str(exc)))
        return None
    except OSError as exc:
        logs.append(ScanLog(path, "open_dir_error", str(exc)))
        return None


def _open_directory_child(name: str, path: Path, directory_fd: int, logs: list[ScanLog]) -> int | None:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except PermissionError as exc:
        logs.append(ScanLog(path, "permission_denied", str(exc)))
        return None
    except OSError as exc:
        logs.append(ScanLog(path, "open_dir_error", str(exc)))
        return None


def _list_directory_names(directory_fd: int, path: Path, logs: list[ScanLog]) -> tuple[str, ...]:
    try:
        return tuple(os.listdir(directory_fd))
    except PermissionError as exc:
        logs.append(ScanLog(path, "permission_denied", str(exc)))
    except OSError as exc:
        logs.append(ScanLog(path, "listdir_error", str(exc)))
    return ()


def _has_source_marker(directory_fd: int, logs: list[ScanLog], path: Path) -> bool:
    return bool(SOURCE_MARKERS.intersection(_list_directory_names(directory_fd, path, logs)))


def _datetime_modified(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)
