"""Read-only filesystem scanner for local storage metadata."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import time
from typing import Callable, Iterable

from storage_dashboard.classifier import SOURCE_MARKERS, Classification, classify_path

ProgressCallback = Callable[[dict[str, object]], None]

SCAN_MODE_EXACT = "exact"
SCAN_MODE_SUMMARY = "summary"
SCAN_MODE_LAZY = "lazy"
SIZE_STATUS_EXACT = "exact"
SIZE_STATUS_CACHED = "cached"
SIZE_STATUS_PARTIAL = "partial"
SCAN_DEPTH_RECURSIVE = "recursive"
SCAN_DEPTH_SHALLOW = "shallow"
SCAN_MODES = frozenset({SCAN_MODE_EXACT, SCAN_MODE_SUMMARY, SCAN_MODE_LAZY})
ALWAYS_EMIT_PHASES = frozenset({"starting", "root", "root_complete", "incremental", "complete", "failed"})

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
    changed_paths: tuple[Path, ...] = ()
    previous_entries: tuple[dict[str, object], ...] = ()
    mode: str = SCAN_MODE_EXACT
    target_path: Path | None = None
    reuse_cached_totals: bool = True
    progress_interval_seconds: float = 0.1
    progress_interval_entries: int = 250
    max_workers: int = 2
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
    size_status: str = SIZE_STATUS_EXACT
    scan_depth: str = SCAN_DEPTH_RECURSIVE

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
            "size_status": self.size_status,
            "scan_depth": self.scan_depth,
        }


@dataclass(frozen=True)
class ScanResult:
    """Scanner output and non-fatal logs."""

    entries: tuple[ScanEntry, ...]
    logs: tuple[ScanLog, ...]


def normalize_roots(roots: tuple[str | Path, ...]) -> tuple[Path, ...]:
    """Return exact local root paths as expanded Path objects without resolving symlinks."""

    return tuple(Path(root).expanduser() for root in roots)


class _ProgressThrottle:
    """Limit hot path progress updates while preserving phase transitions."""

    def __init__(self, callback: ProgressCallback, options: ScanOptions) -> None:
        self.callback = callback
        self.interval_seconds = max(0.0, options.progress_interval_seconds)
        self.interval_entries = max(1, options.progress_interval_entries)
        self.seen = 0
        self.last_emitted_seen = 0
        self.last_emitted_at = 0.0
        self.emitted_phases: set[str] = set()
        self.emitted_nonroot_phases: set[str] = set()

    def __call__(self, payload: dict[str, object]) -> None:
        self.seen += 1
        phase = str(payload.get("phase", ""))
        active_path = payload.get("active_path")
        active_root = payload.get("active_root")
        is_nonroot_path = active_path is not None and active_path != active_root
        now = time.monotonic()
        should_emit = (
            phase in ALWAYS_EMIT_PHASES
            or phase not in self.emitted_phases
            or (is_nonroot_path and phase not in self.emitted_nonroot_phases)
            or self.seen - self.last_emitted_seen >= self.interval_entries
            or now - self.last_emitted_at >= self.interval_seconds
        )
        if not should_emit:
            return
        self.callback(payload)
        self.last_emitted_seen = self.seen
        self.last_emitted_at = now
        self.emitted_phases.add(phase)
        if is_nonroot_path:
            self.emitted_nonroot_phases.add(phase)


def _throttled_progress(options: ScanOptions, progress: ProgressCallback | None) -> ProgressCallback | None:
    if progress is None:
        return None
    return _ProgressThrottle(progress, options)


def scan(options: ScanOptions | None = None, progress: ProgressCallback | None = None) -> ScanResult:
    """Scan configured roots without modifying filesystem state."""

    options = options or ScanOptions()
    if not options.read_only:
        raise ValueError("Scanner only supports read-only operation.")
    if options.mode not in SCAN_MODES:
        raise ValueError(f"Unsupported scan mode: {options.mode}")

    progress = _throttled_progress(options, progress)
    if options.mode == SCAN_MODE_LAZY:
        return _scan_lazy(options, progress)
    if options.mode == SCAN_MODE_SUMMARY:
        return _scan_summary(options, progress)
    if options.changed_paths and options.previous_entries:
        return _scan_incremental(options, progress)
    return _scan_full(options, progress)


def _scan_full(options: ScanOptions, progress: ProgressCallback | None = None) -> ScanResult:
    """Run a full root scan, optionally across a small root worker pool."""

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

    if len(roots) > 1 and options.max_workers > 1:
        return _scan_roots_parallel(options, roots, progress, completed_roots)

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


def _scan_roots_parallel(
    options: ScanOptions,
    roots: tuple[Path, ...],
    progress: ProgressCallback | None,
    completed_roots: list[str],
) -> ScanResult:
    entries_by_index: dict[int, list[ScanEntry]] = {}
    logs_by_index: dict[int, list[ScanLog]] = {}
    max_workers = max(1, min(options.max_workers, len(roots)))

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="lighthouse-root-scan") as executor:
        future_to_index = {
            executor.submit(_scan_root_worker, root, options, progress): index for index, root in enumerate(roots)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            root_entries, root_logs = future.result()
            entries_by_index[index] = root_entries
            logs_by_index[index] = root_logs
            completed_roots.append(str(roots[index]))
            _emit_progress(
                progress,
                phase="root_complete",
                active_root=str(roots[index]),
                active_path=str(roots[index]),
                completed_roots=list(completed_roots),
                pending_roots=[str(root) for root in roots if str(root) not in completed_roots],
                entries_seen=sum(len(items) for items in entries_by_index.values()),
                logs_seen=sum(len(items) for items in logs_by_index.values()),
            )

    entries: list[ScanEntry] = []
    logs: list[ScanLog] = []
    for index in range(len(roots)):
        entries.extend(entries_by_index.get(index, []))
        logs.extend(logs_by_index.get(index, []))

    _emit_progress(
        progress,
        phase="complete",
        active_root=None,
        active_path=None,
        completed_roots=[str(root) for root in roots],
        pending_roots=[],
        entries_seen=len(entries),
        logs_seen=len(logs),
    )
    return ScanResult(tuple(entries), tuple(logs))


def _scan_root_worker(
    root: Path,
    options: ScanOptions,
    progress: ProgressCallback | None = None,
) -> tuple[list[ScanEntry], list[ScanLog]]:
    logs: list[ScanLog] = []
    _emit_progress(progress, phase="root", active_root=str(root), active_path=str(root), logs_seen=0)
    if _is_denied(root, options.denied_roots):
        logs.append(ScanLog(root, "denied_root", "Skipped denied root."))
        return [_denied_entry(root)], logs

    entries = _scan_path(root, root, options, logs, progress)
    return list(entries or []), logs


def _scan_summary(options: ScanOptions, progress: ProgressCallback | None = None) -> ScanResult:
    """Run a shallow scan that returns roots and direct children only."""

    entries: list[ScanEntry] = []
    logs: list[ScanLog] = []
    roots = normalize_roots(tuple(options.roots))
    completed_roots: list[str] = []
    cached_entries = _cached_entries_by_path(options.previous_entries)

    _emit_progress(
        progress,
        phase="starting",
        active_root=None,
        active_path=None,
        completed_roots=[],
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
        else:
            scanned = _scan_summary_path(root, root, options, logs, progress, cached_entries)
            if scanned is not None:
                entries.extend(scanned)
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


def _scan_summary_path(
    path: Path,
    root: Path,
    options: ScanOptions,
    logs: list[ScanLog],
    progress: ProgressCallback | None,
    cached_entries: dict[Path, ScanEntry],
) -> list[ScanEntry] | None:
    _emit_path_progress(progress, "checking", path, root, logs)
    stat_result = _stat_path(path, logs)
    if stat_result is None:
        return None
    if stat.S_ISLNK(stat_result.st_mode):
        logs.append(ScanLog(path, "symlink_skipped", "Skipped symlink."))
        return None
    if stat.S_ISDIR(stat_result.st_mode):
        directory_fd = _open_directory_path(path, logs)
        if directory_fd is None:
            return None
        try:
            return _scan_summary_directory(
                path,
                root,
                stat_result.st_mtime,
                directory_fd,
                options,
                logs,
                progress,
                cached_entries,
            )
        finally:
            os.close(directory_fd)

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
            scan_depth=SCAN_DEPTH_SHALLOW,
        )
    ]


def _scan_summary_directory(
    path: Path,
    root: Path,
    modified_timestamp: float,
    directory_fd: int,
    options: ScanOptions,
    logs: list[ScanLog],
    progress: ProgressCallback | None,
    cached_entries: dict[Path, ScanEntry],
) -> list[ScanEntry]:
    _emit_path_progress(progress, "directory", path, root, logs)
    child_entries: list[ScanEntry] = []
    size_bytes = 0
    file_count = 0
    unknown_file_count = False
    child_names = _list_directory_names(directory_fd, path, logs)

    for child_name in child_names:
        child_path = path / child_name
        _emit_path_progress(progress, "checking", child_path, root, logs)
        if _is_denied(child_path, options.denied_roots):
            logs.append(ScanLog(child_path, "denied_root", "Skipped denied path."))
            child_entries.append(_denied_entry(child_path, root=root))
            continue

        stat_result = _stat_child(child_name, child_path, directory_fd, logs)
        if stat_result is None:
            unknown_file_count = True
            continue
        if stat.S_ISLNK(stat_result.st_mode):
            logs.append(ScanLog(child_path, "symlink_skipped", "Skipped symlink."))
            continue

        if stat.S_ISDIR(stat_result.st_mode):
            cached = _cached_summary_entry(child_path, cached_entries, options)
            entry = cached or _partial_folder_entry(child_path, root, stat_result.st_mtime)
            child_entries.append(entry)
            size_bytes += entry.size_bytes
            if entry.file_count is None:
                unknown_file_count = True
            else:
                file_count += entry.file_count
            continue

        modified_at = _datetime_modified(stat_result.st_mtime)
        classification = classify_path(
            child_path,
            root=root,
            is_dir=False,
            size_bytes=stat_result.st_size,
            modified_at=modified_at,
        )
        entry = _entry_from_classification(
            path=child_path,
            kind="file",
            size_bytes=stat_result.st_size,
            modified_at=modified_at.isoformat(),
            file_count=None,
            root=root,
            classification=classification,
            scan_depth=SCAN_DEPTH_SHALLOW,
        )
        child_entries.append(entry)
        size_bytes += entry.size_bytes
        file_count += 1

    modified_at = _datetime_modified(modified_timestamp)
    classification = classify_path(
        path,
        root=root,
        is_dir=True,
        size_bytes=size_bytes,
        modified_at=modified_at,
        source_marker_present=bool(SOURCE_MARKERS.intersection(child_names)),
    )
    current = _entry_from_classification(
        path=path,
        kind="folder",
        size_bytes=size_bytes,
        modified_at=modified_at.isoformat(),
        file_count=None if unknown_file_count else file_count,
        root=root,
        classification=classification,
        size_status=SIZE_STATUS_PARTIAL,
        scan_depth=SCAN_DEPTH_SHALLOW,
    )
    return [current, *child_entries]


def _cached_entries_by_path(items: tuple[dict[str, object], ...]) -> dict[Path, ScanEntry]:
    entries = [_entry_from_mapping(item) for item in items]
    return {entry.path: entry for entry in entries if entry is not None}


def _cached_summary_entry(
    path: Path,
    cached_entries: dict[Path, ScanEntry],
    options: ScanOptions,
) -> ScanEntry | None:
    if not options.reuse_cached_totals:
        return None
    cached = cached_entries.get(path)
    if cached is None or cached.kind != "folder":
        return None
    if cached.size_status != SIZE_STATUS_EXACT:
        return None
    return ScanEntry(
        path=cached.path,
        kind=cached.kind,
        size_bytes=cached.size_bytes,
        modified_at=cached.modified_at,
        file_count=cached.file_count,
        root=cached.root,
        classification=cached.classification,
        reason=cached.reason,
        risk=cached.risk,
        size_status=SIZE_STATUS_CACHED,
        scan_depth=SCAN_DEPTH_SHALLOW,
    )


def _partial_folder_entry(path: Path, root: Path, modified_timestamp: float) -> ScanEntry:
    modified_at = _datetime_modified(modified_timestamp)
    classification = classify_path(
        path,
        root=root,
        is_dir=True,
        size_bytes=0,
        modified_at=modified_at,
    )
    return _entry_from_classification(
        path=path,
        kind="folder",
        size_bytes=0,
        modified_at=modified_at.isoformat(),
        file_count=None,
        root=root,
        classification=classification,
        size_status=SIZE_STATUS_PARTIAL,
        scan_depth=SCAN_DEPTH_SHALLOW,
    )


def _scan_lazy(options: ScanOptions, progress: ProgressCallback | None = None) -> ScanResult:
    roots = normalize_roots(tuple(options.roots))
    target, root = _validated_lazy_target(options, roots)
    logs: list[ScanLog] = []
    _emit_progress(
        progress,
        phase="starting",
        active_root=str(root),
        active_path=str(target),
        completed_roots=[],
        pending_roots=[str(target)],
        entries_seen=0,
        logs_seen=0,
    )
    entries = list(_scan_path(target, root, options, logs, progress) or [])
    _emit_progress(
        progress,
        phase="complete",
        active_root=str(root),
        active_path=str(target),
        completed_roots=[str(target)],
        pending_roots=[],
        entries_seen=len(entries),
        logs_seen=len(logs),
    )
    return ScanResult(tuple(entries), tuple(logs))


def _validated_lazy_target(options: ScanOptions, roots: tuple[Path, ...]) -> tuple[Path, Path]:
    if options.target_path is None:
        raise ValueError("Lazy scan requires target_path.")
    target = Path(options.target_path).expanduser()
    root = _root_for_path(target, roots)
    if root is None:
        raise ValueError("Lazy scan target is outside configured roots.")
    if _is_denied(target, options.denied_roots):
        raise ValueError("Lazy scan target is denied.")
    stat_result = _stat_path(target, [])
    if stat_result is None:
        raise ValueError("Lazy scan target is not readable.")
    if stat.S_ISLNK(stat_result.st_mode):
        raise ValueError("Lazy scan target is a symlink.")
    return target, root


def _scan_incremental(options: ScanOptions, progress: ProgressCallback | None = None) -> ScanResult:
    roots = normalize_roots(tuple(options.roots))
    changed_paths = _collapse_changed_paths(
        tuple(Path(path).expanduser() for path in options.changed_paths),
        roots,
        options.excluded_names,
    )
    previous_entries = [
        entry for entry in (_entry_from_mapping(item) for item in options.previous_entries) if entry is not None
    ]

    _emit_progress(
        progress,
        phase="incremental",
        active_root=None,
        active_path=None,
        completed_roots=[],
        pending_roots=[str(path) for path in changed_paths],
        entries_seen=len(previous_entries),
        logs_seen=0,
    )

    scanned_entries: list[ScanEntry] = []
    logs: list[ScanLog] = []
    if changed_paths:
        if len(changed_paths) > 1 and options.max_workers > 1:
            scanned_entries, logs = _scan_changed_paths_parallel(changed_paths, roots, options, progress)
        else:
            for path in changed_paths:
                entries, path_logs = _scan_changed_path(path, roots, options, progress)
                scanned_entries.extend(entries)
                logs.extend(path_logs)

    merged_entries = _merge_incremental_entries(previous_entries, scanned_entries, changed_paths, roots)
    _emit_progress(
        progress,
        phase="complete",
        active_root=None,
        active_path=None,
        completed_roots=[str(path) for path in changed_paths],
        pending_roots=[],
        entries_seen=len(merged_entries),
        logs_seen=len(logs),
    )
    return ScanResult(tuple(merged_entries), tuple(logs))


def _scan_changed_paths_parallel(
    changed_paths: tuple[Path, ...],
    roots: tuple[Path, ...],
    options: ScanOptions,
    progress: ProgressCallback | None,
) -> tuple[list[ScanEntry], list[ScanLog]]:
    entries_by_index: dict[int, list[ScanEntry]] = {}
    logs_by_index: dict[int, list[ScanLog]] = {}
    max_workers = max(1, min(options.max_workers, len(changed_paths)))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="lighthouse-incremental-scan") as executor:
        future_to_index = {
            executor.submit(_scan_changed_path, path, roots, options, progress): index
            for index, path in enumerate(changed_paths)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            entries, logs = future.result()
            entries_by_index[index] = entries
            logs_by_index[index] = logs

    entries: list[ScanEntry] = []
    logs: list[ScanLog] = []
    for index in range(len(changed_paths)):
        entries.extend(entries_by_index.get(index, []))
        logs.extend(logs_by_index.get(index, []))
    return entries, logs


def _scan_changed_path(
    path: Path,
    roots: tuple[Path, ...],
    options: ScanOptions,
    progress: ProgressCallback | None = None,
) -> tuple[list[ScanEntry], list[ScanLog]]:
    logs: list[ScanLog] = []
    root = _root_for_path(path, roots)
    if root is None or not os.path.lexists(path):
        return [], logs
    scanned = _scan_path(path, root, options, logs, progress)
    return list(scanned or []), logs


def _collapse_changed_paths(
    paths: tuple[Path, ...],
    roots: tuple[Path, ...],
    excluded_names: frozenset[str],
) -> tuple[Path, ...]:
    in_scope = sorted(
        {_collapsed_scan_path(path, roots, excluded_names) for path in paths if _root_for_path(path, roots) is not None},
        key=lambda item: (len(item.parts), str(item)),
    )
    collapsed: list[Path] = []
    for path in in_scope:
        if any(_same_or_child(path, existing) for existing in collapsed):
            continue
        collapsed.append(path)
    return tuple(collapsed)


def _collapsed_scan_path(path: Path, roots: tuple[Path, ...], excluded_names: frozenset[str]) -> Path:
    root = _root_for_path(path, roots)
    if root is None:
        return path
    current = path
    while current != root:
        if current.name in excluded_names:
            return current
        current = current.parent
    return root if root.name in excluded_names else path


def _merge_incremental_entries(
    previous_entries: list[ScanEntry],
    scanned_entries: list[ScanEntry],
    changed_paths: tuple[Path, ...],
    roots: tuple[Path, ...],
) -> list[ScanEntry]:
    retained = [
        entry for entry in previous_entries if not any(_same_or_child(entry.path, path) for path in changed_paths)
    ]
    entries_by_path = {entry.path: entry for entry in retained}
    for entry in scanned_entries:
        entries_by_path[entry.path] = entry

    for folder_path in _affected_ancestor_paths(changed_paths, roots):
        if folder_path in changed_paths:
            continue
        entry = entries_by_path.get(folder_path)
        if entry is None or entry.kind != "folder":
            continue
        size_bytes, file_count = _direct_child_totals(entries_by_path.values(), folder_path)
        entries_by_path[folder_path] = ScanEntry(
            path=entry.path,
            kind=entry.kind,
            size_bytes=size_bytes,
            modified_at=entry.modified_at,
            file_count=file_count,
            root=entry.root,
            classification=entry.classification,
            reason=entry.reason,
            risk=entry.risk,
        )

    return sorted(entries_by_path.values(), key=lambda entry: _entry_sort_key(entry, roots))


def _affected_ancestor_paths(changed_paths: tuple[Path, ...], roots: tuple[Path, ...]) -> list[Path]:
    ancestors: set[Path] = set()
    for path in changed_paths:
        root = _root_for_path(path, roots)
        if root is None:
            continue
        current = path
        while True:
            ancestors.add(current)
            if current == root:
                break
            current = current.parent
    return sorted(ancestors, key=lambda item: len(item.parts), reverse=True)


def _direct_child_totals(entries: Iterable[ScanEntry], folder_path: Path) -> tuple[int, int]:
    size_bytes = 0
    file_count = 0
    for entry in entries:
        if entry.path.parent != folder_path:
            continue
        size_bytes += entry.size_bytes
        if entry.kind == "folder":
            file_count += entry.file_count or 0
        else:
            file_count += 1
    return size_bytes, file_count


def _entry_sort_key(entry: ScanEntry, roots: tuple[Path, ...]) -> tuple[int, int, str]:
    root_lookup = {root: index for index, root in enumerate(roots)}
    return (root_lookup.get(entry.root, len(roots)), len(entry.path.parts), str(entry.path))


def _root_for_path(path: Path, roots: tuple[Path, ...]) -> Path | None:
    matches = [root for root in roots if path == root or path.is_relative_to(root)]
    if not matches:
        return None
    return max(matches, key=lambda root: len(root.parts))


def _same_or_child(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _entry_from_mapping(data: dict[str, object]) -> ScanEntry | None:
    try:
        path = Path(str(data["path"]))
        root = Path(str(data.get("root") or path))
        file_count = data.get("file_count")
        return ScanEntry(
            path=path,
            kind=str(data["kind"]),
            size_bytes=int(data.get("size_bytes", 0)),
            modified_at=data.get("modified_at") if isinstance(data.get("modified_at"), str) else None,
            file_count=int(file_count) if isinstance(file_count, int) else None,
            root=root,
            classification=str(data.get("classification", "do_not_touch")),
            reason=str(data.get("reason", "unclassified")),
            risk=str(data.get("risk", "high")),
            size_status=str(data.get("size_status", SIZE_STATUS_EXACT)),
            scan_depth=str(data.get("scan_depth", SCAN_DEPTH_RECURSIVE)),
        )
    except (KeyError, TypeError, ValueError):
        return None


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
    size_status: str = SIZE_STATUS_EXACT,
    scan_depth: str = SCAN_DEPTH_RECURSIVE,
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
        size_status=size_status,
        scan_depth=scan_depth,
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
