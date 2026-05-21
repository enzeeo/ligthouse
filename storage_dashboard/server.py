"""Small stdlib HTTP server for the local dashboard shell."""

from __future__ import annotations

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import threading
from urllib.parse import urlparse

from storage_dashboard import scanner as filesystem_scanner
from storage_dashboard.store import SnapshotStore

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SUGGESTION_CLASSES = {"safe_to_review": 0, "caution": 1}


class ScanAlreadyRunningError(RuntimeError):
    """Raised when a second scan starts before the current scan finishes."""


class LocalScanner:
    """Default scanner adapter for API use."""

    def scan(self) -> object:
        return filesystem_scanner.scan()


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """Serve the vanilla frontend from the local web directory."""

    def __init__(self, *args: object, directory: str | None = None, **kwargs: object) -> None:
        super().__init__(*args, directory=directory or str(WEB_ROOT), **kwargs)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/disk":
            self._send_json(self.server.disk_summary())
            return
        if path == "/api/scan/status":
            self._send_json(self.server.scan_status())
            return
        if path == "/api/scan/results":
            self._send_latest_snapshot()
            return
        if path == "/api/suggestions":
            snapshot = self.server.store.latest()
            self._send_json({"suggestions": suggestions_from_snapshot(snapshot)})
            return
        if path == "/api/export":
            self._send_export()
            return
        if path == "/api" or path.startswith("/api/"):
            self._send_json({"error": "not_found"}, status=404)
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/scan":
            self._send_json({"error": "not_found"}, status=404)
            return

        try:
            snapshot = self.server.run_scan()
        except ScanAlreadyRunningError:
            self._send_json({"error": "scan_already_running", "status": "running"}, status=409)
            return
        except Exception:
            self._send_json({"error": "scan_failed", "status": "failed"}, status=500)
            return
        self._send_json({"status": "completed", "snapshot": snapshot}, status=202)

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path == "/api" or path.startswith("/api/"):
            self._send_json({"error": "method_not_allowed"}, status=405, include_body=False)
            return
        super().do_HEAD()

    def do_PUT(self) -> None:
        self._send_unsupported_api_method()

    def do_DELETE(self) -> None:
        self._send_unsupported_api_method()

    def do_PATCH(self) -> None:
        self._send_unsupported_api_method()

    def do_OPTIONS(self) -> None:
        self._send_unsupported_api_method()

    def _send_unsupported_api_method(self) -> None:
        path = urlparse(self.path).path
        if path == "/api" or path.startswith("/api/"):
            self._send_json({"error": "method_not_allowed"}, status=405)
            return
        self.send_error(501, "Unsupported method")

    def _send_latest_snapshot(self) -> None:
        snapshot = self.server.store.latest()
        if snapshot is None:
            self._send_json({"error": "no_scan_results"}, status=404)
            return
        self._send_json(
            {
                "snapshot": snapshot,
                "entries": snapshot.get("entries", []),
                "logs": snapshot.get("logs", []),
            }
        )

    def _send_export(self) -> None:
        snapshot = self.server.store.latest()
        if snapshot is None:
            self._send_json({"error": "no_scan_results"}, status=404)
            return
        report = {
            "generated_from": "local_snapshot",
            "snapshot": snapshot,
            "suggestions": suggestions_from_snapshot(snapshot),
        }
        self._send_json({"report": report})

    def _send_json(self, payload: dict[str, object], status: int = 200, *, include_body: bool = True) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class DashboardServer(ThreadingHTTPServer):
    """Threaded local HTTP server with API dependencies."""

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[DashboardRequestHandler],
        *,
        store: SnapshotStore,
        scanner: object,
        disk_path: Path,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.store = store
        self.scanner = scanner
        self.disk_path = disk_path
        self._scan_lock = threading.Lock()
        self._status: dict[str, object] = {"status": "idle"}

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
        status = dict(self._status)
        latest = self.store.latest()
        if latest is not None:
            status["latest_snapshot"] = {
                "id": latest.get("id"),
                "timestamp": latest.get("timestamp"),
            }
        return status

    def run_scan(self) -> dict[str, object]:
        if not self._scan_lock.acquire(blocking=False):
            raise ScanAlreadyRunningError("scan already running")
        self._status = {"status": "running"}
        try:
            result = self.scanner.scan()
            entries = _entries_from_result(result)
            logs = _logs_from_result(result)
            roots = _roots_from_entries(entries)
            snapshot = self.store.add_snapshot(
                roots=roots,
                disk=self.disk_summary(),
                entries=entries,
                logs=logs,
            )
            self._status = {"status": "completed", "snapshot_id": snapshot["id"]}
            return snapshot
        except Exception:
            self._status = {"status": "failed", "error": "scan_failed"}
            raise
        finally:
            self._scan_lock.release()


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    store: SnapshotStore | None = None,
    scanner: object | None = None,
    disk_path: Path | None = None,
) -> DashboardServer:
    """Create a loopback-only dashboard server."""

    if host not in LOOPBACK_HOSTS:
        raise ValueError("Storage dashboard server only supports loopback hosts.")

    return DashboardServer(
        (host, port),
        DashboardRequestHandler,
        store=store or SnapshotStore(),
        scanner=scanner or LocalScanner(),
        disk_path=disk_path or Path.home(),
    )


def suggestions_from_snapshot(snapshot: dict[str, object] | None) -> list[dict[str, object]]:
    if not isinstance(snapshot, dict):
        return []

    entries = snapshot.get("entries", [])
    if not isinstance(entries, list):
        return []

    suggestions = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        classification = entry.get("classification")
        if classification not in SUGGESTION_CLASSES:
            continue
        size_bytes = _coerce_size_bytes(entry.get("size_bytes", 0))
        suggestions.append(
            {
                "path": entry.get("path"),
                "kind": entry.get("kind"),
                "size": size_bytes,
                "size_bytes": size_bytes,
                "reason": entry.get("reason"),
                "risk": entry.get("risk"),
                "classification": classification,
            }
        )

    return sorted(
        suggestions,
        key=lambda item: (
            SUGGESTION_CLASSES.get(str(item["classification"]), 99),
            -int(item["size_bytes"]),
        ),
    )


def _coerce_size_bytes(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _entries_from_result(result: object) -> list[dict[str, object]]:
    if isinstance(result, dict):
        entries = result.get("entries", [])
    else:
        entries = getattr(result, "entries", [])
    return [_entry_to_dict(entry) for entry in entries]


def _logs_from_result(result: object) -> list[dict[str, object]]:
    if isinstance(result, dict):
        logs = result.get("logs", [])
    else:
        logs = getattr(result, "logs", [])
    return [_log_to_dict(log) for log in logs]


def _entry_to_dict(entry: object) -> dict[str, object]:
    if isinstance(entry, dict):
        return dict(entry)
    to_dict = getattr(entry, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {}


def _log_to_dict(log: object) -> dict[str, object]:
    if isinstance(log, dict):
        return dict(log)
    return {
        "path": str(getattr(log, "path", "")),
        "event": str(getattr(log, "event", "")),
        "message": str(getattr(log, "message", "")),
    }


def _roots_from_entries(entries: list[dict[str, object]]) -> list[str]:
    roots = []
    for entry in entries:
        root = entry.get("root")
        if root is not None and str(root) not in roots:
            roots.append(str(root))
    if roots:
        return roots
    return [str(root) for root in filesystem_scanner.DEFAULT_ROOTS]


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the local-only dashboard server."""

    server = create_server(host, port)
    print(f"Serving storage dashboard at http://{host}:{port}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local storage dashboard.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    run(port=args.port)
