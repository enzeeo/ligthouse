import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from storage_dashboard.runtime import (
    SNAPSHOT_SCHEMA_VERSION,
    ScanAlreadyRunningError,
    ScanRuntime,
    roots_fingerprint,
)
from storage_dashboard.store import SnapshotStore


class SlowScanner:
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def scan(self, progress=None):
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        return {"entries": [], "logs": []}


class CapturingScanner:
    def __init__(self) -> None:
        self.calls = 0
        self.options = None

    def scan(self, options=None, progress=None):
        self.calls += 1
        self.options = options
        return {"entries": [], "logs": []}


class FakeFSEvents:
    def __init__(self, *, changed_paths=(), current_event_id=101, must_full_scan=False, supported=True) -> None:
        self.changed_paths = tuple(Path(path) for path in changed_paths)
        self.current = current_event_id
        self.must_full_scan = must_full_scan
        self.supported = supported
        self.calls = 0

    def current_event_id(self):
        return self.current

    def changes_since(self, roots, since_event_id):
        self.calls += 1
        from storage_dashboard.fsevents import FSEventChanges

        return FSEventChanges(
            supported=self.supported,
            current_event_id=self.current,
            changed_paths=self.changed_paths,
            must_full_scan=self.must_full_scan,
        )


class RuntimeTests(unittest.TestCase):
    def runtime(self, temp_dir: str, **kwargs) -> ScanRuntime:
        root = Path(temp_dir) / "root"
        root.mkdir(exist_ok=True)
        return ScanRuntime(
            store=SnapshotStore(Path(temp_dir) / "state.json"),
            scanner=kwargs.pop("scanner", EmptyScanner()),
            disk_path=Path(temp_dir),
            roots=(root,),
            lock_path=Path(temp_dir) / ".scan.lock",
            **kwargs,
        )

    def test_missing_snapshot_is_not_fresh_and_starts_background_scan(self) -> None:
        with TemporaryDirectory() as temp_dir:
            scanner = SlowScanner()
            runtime = self.runtime(temp_dir, scanner=scanner)

            result = runtime.ensure_fresh_background()
            scanner.started.wait(timeout=1)
            scanner.release.set()
            result.thread.join(timeout=2)

        self.assertEqual(result.status, "started")
        self.assertEqual(scanner.calls, 1)

    def test_fresh_compatible_snapshot_skips_background_scan(self) -> None:
        with TemporaryDirectory() as temp_dir:
            runtime = self.runtime(temp_dir)
            add_snapshot(runtime, minutes_old=1)

            result = runtime.ensure_fresh_background()

        self.assertEqual(result.status, "fresh")

    def test_stale_compatible_snapshot_starts_background_scan(self) -> None:
        with TemporaryDirectory() as temp_dir:
            scanner = SlowScanner()
            runtime = self.runtime(temp_dir, scanner=scanner)
            add_snapshot(runtime, minutes_old=20)

            result = runtime.ensure_fresh_background()
            scanner.started.wait(timeout=1)
            scanner.release.set()
            result.thread.join(timeout=2)

        self.assertEqual(result.status, "started")
        self.assertEqual(scanner.calls, 1)

    def test_incompatible_schema_or_fingerprint_is_not_trusted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            runtime = self.runtime(temp_dir)
            snapshot = add_snapshot(runtime, minutes_old=1)
            snapshot["schema_version"] = 999
            write_snapshots(runtime.store.path, [snapshot])

            self.assertIsNone(runtime.latest_compatible_snapshot())

            snapshot["schema_version"] = SNAPSHOT_SCHEMA_VERSION
            snapshot["roots_fingerprint"] = "other"
            write_snapshots(runtime.store.path, [snapshot])

            self.assertIsNone(runtime.latest_compatible_snapshot())

    def test_second_scan_returns_running_or_locked_status(self) -> None:
        with TemporaryDirectory() as temp_dir:
            scanner = SlowScanner()
            runtime = self.runtime(temp_dir, scanner=scanner)
            thread = threading.Thread(target=runtime.run_scan)
            thread.start()
            scanner.started.wait(timeout=1)
            try:
                with self.assertRaises(ScanAlreadyRunningError):
                    runtime.run_scan()
                start = runtime.start_background_scan()
            finally:
                scanner.release.set()
                thread.join(timeout=2)

        self.assertEqual(start.status, "running")

    def test_corrupt_store_and_missing_snapshot_fail_closed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            runtime = self.runtime(temp_dir)
            runtime.store.path.write_text("{not json", encoding="utf-8")

            self.assertIsNone(runtime.latest_compatible_snapshot())
            self.assertFalse(runtime.has_fresh_compatible_snapshot())

    def test_no_fsevents_changes_reuses_latest_snapshot_without_scanning(self) -> None:
        with TemporaryDirectory() as temp_dir:
            scanner = CapturingScanner()
            fsevents = FakeFSEvents(changed_paths=(), current_event_id=101)
            runtime = self.runtime(temp_dir, scanner=scanner, fsevents=fsevents)
            snapshot = add_snapshot(runtime, minutes_old=1)
            snapshot["fsevents_event_id"] = 100
            snapshot["fsevents_roots_fingerprint"] = roots_fingerprint(runtime.roots)
            write_snapshots(runtime.store.path, [snapshot])

            result = runtime.run_scan()

        self.assertEqual(result["id"], snapshot["id"])
        self.assertEqual(scanner.calls, 0)
        self.assertEqual(fsevents.calls, 1)

    def test_fsevents_dirty_paths_run_incremental_scan_options(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            dirty = root / "dirty"
            scanner = CapturingScanner()
            runtime = self.runtime(
                temp_dir,
                scanner=scanner,
                fsevents=FakeFSEvents(changed_paths=(dirty,), current_event_id=102),
            )
            snapshot = add_snapshot(runtime, minutes_old=1)
            snapshot["entries"] = [{"path": str(root), "kind": "folder", "size_bytes": 0, "root": str(root)}]
            snapshot["fsevents_event_id"] = 100
            snapshot["fsevents_roots_fingerprint"] = roots_fingerprint(runtime.roots)
            write_snapshots(runtime.store.path, [snapshot])

            result = runtime.run_scan()

        self.assertEqual(scanner.calls, 1)
        self.assertEqual(scanner.options.changed_paths, (dirty,))
        self.assertEqual(scanner.options.previous_entries, tuple(snapshot["entries"]))
        self.assertEqual(result["fsevents_event_id"], 102)
        self.assertEqual(result["fsevents_roots_fingerprint"], roots_fingerprint(runtime.roots))

    def test_fsevents_dropped_events_fall_back_to_full_scan(self) -> None:
        with TemporaryDirectory() as temp_dir:
            scanner = CapturingScanner()
            runtime = self.runtime(
                temp_dir,
                scanner=scanner,
                fsevents=FakeFSEvents(changed_paths=(Path(temp_dir) / "root",), must_full_scan=True),
            )
            snapshot = add_snapshot(runtime, minutes_old=1)
            snapshot["fsevents_event_id"] = 100
            snapshot["fsevents_roots_fingerprint"] = roots_fingerprint(runtime.roots)
            write_snapshots(runtime.store.path, [snapshot])

            runtime.run_scan()

        self.assertEqual(scanner.calls, 1)
        self.assertEqual(scanner.options.changed_paths, ())
        self.assertEqual(scanner.options.previous_entries, ())


class EmptyScanner:
    def scan(self, progress=None):
        return {"entries": [], "logs": []}


def add_snapshot(runtime: ScanRuntime, minutes_old: int) -> dict[str, object]:
    timestamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
    snapshot = {
        "id": timestamp.isoformat(),
        "timestamp": timestamp.isoformat(),
        "roots": [str(root) for root in runtime.roots],
        "disk": {"total": 1, "used": 1, "free": 0, "percent": 100.0},
        "entries": [],
        "logs": [],
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "roots_fingerprint": roots_fingerprint(runtime.roots),
    }
    write_snapshots(runtime.store.path, [snapshot])
    return snapshot


def write_snapshots(path: Path, snapshots: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"snapshots": snapshots}), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
