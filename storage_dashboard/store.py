"""Local persistence boundaries for exact path metadata."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import tempfile
import threading
from pathlib import Path

APP_STATE_DIR = Path.home() / ".storage-dashboard"
APP_STATE_FILE = APP_STATE_DIR / "dashboard.json"
DEFAULT_SNAPSHOT_LIMIT = 10


def state_file_path() -> Path:
    """Return the local-only state file path."""

    return APP_STATE_FILE


class SnapshotStore:
    """Bounded local JSON snapshot store."""

    def __init__(self, path: Path | None = None, limit: int = DEFAULT_SNAPSHOT_LIMIT) -> None:
        self.path = path or APP_STATE_FILE
        self.limit = limit
        self._lock = threading.RLock()

    def load(self) -> dict[str, object]:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, object]:
        if not self.path.exists():
            return {"snapshots": []}

        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {"snapshots": []}

        if not isinstance(data, dict):
            return {"snapshots": []}

        snapshots = data.get("snapshots", [])
        if not isinstance(snapshots, list):
            snapshots = []
        snapshots = [snapshot for snapshot in snapshots if isinstance(snapshot, dict)]
        return {"snapshots": snapshots[-self.limit :]}

    def latest(self) -> dict[str, object] | None:
        with self._lock:
            snapshots = self._load_unlocked()["snapshots"]
            if not snapshots:
                return None
            return snapshots[-1]

    def add_snapshot(
        self,
        *,
        roots: list[str],
        disk: dict[str, object],
        entries: list[dict[str, object]],
        logs: list[dict[str, object]],
        schema_version: int | None = None,
        roots_fingerprint: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            data = self._load_unlocked()
            snapshots = list(data["snapshots"])
            timestamp = datetime.now(timezone.utc).isoformat()
            snapshot = {
                "id": timestamp,
                "timestamp": timestamp,
                "roots": roots,
                "disk": disk,
                "entries": entries,
                "logs": logs,
            }
            if schema_version is not None:
                snapshot["schema_version"] = schema_version
            if roots_fingerprint is not None:
                snapshot["roots_fingerprint"] = roots_fingerprint
            snapshots.append(snapshot)
            data["snapshots"] = snapshots[-self.limit :]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._write_unlocked(data)
            return snapshot

    def _write_unlocked(self, data: dict[str, object]) -> None:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_path = Path(file.name)
                json.dump(data, file, indent=2)
                file.flush()
            temp_path.replace(self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
