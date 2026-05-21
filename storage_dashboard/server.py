"""Small stdlib HTTP server for the local dashboard shell."""

from __future__ import annotations

import argparse
import errno
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from urllib.parse import urlparse

from storage_dashboard.runtime import LocalScanner, ScanAlreadyRunningError, ScanRuntime
from storage_dashboard.store import SnapshotStore

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
ALLOWED_HOST = "127.0.0.1"
SUGGESTION_CLASSES = {"safe_to_review": 0, "caution": 1}


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
        runtime: ScanRuntime | None = None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.runtime = runtime or ScanRuntime(store=store, scanner=scanner, disk_path=disk_path)
        self.store = self.runtime.store
        self.scanner = self.runtime.scanner
        self.disk_path = self.runtime.disk_path
        self._scan_lock = self.runtime._scan_lock

    def disk_summary(self) -> dict[str, object]:
        return self.runtime.disk_summary()

    def scan_status(self) -> dict[str, object]:
        return self.runtime.scan_status()

    def run_scan(self) -> dict[str, object]:
        return self.runtime.run_scan()


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    store: SnapshotStore | None = None,
    scanner: object | None = None,
    disk_path: Path | None = None,
    runtime: ScanRuntime | None = None,
) -> DashboardServer:
    """Create a dashboard server bound to the approved IPv4 loopback host."""

    if host != ALLOWED_HOST:
        raise ValueError("Storage dashboard server only supports 127.0.0.1.")

    return DashboardServer(
        (host, port),
        DashboardRequestHandler,
        store=store or SnapshotStore(),
        scanner=scanner or LocalScanner(),
        disk_path=disk_path or Path.home(),
        runtime=runtime,
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


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the local-only dashboard server."""

    server = create_server(host, port)
    print(f"Serving storage dashboard at http://{host}:{port}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local storage dashboard.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        run(port=args.port)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        next_port = args.port + 1
        print(
            f"Port {args.port} is already in use. "
            f"Stop the process using it or run: python3 app.py --port {next_port}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
