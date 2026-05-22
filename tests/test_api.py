import contextlib
import errno
import io
import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from storage_dashboard.server import DashboardRequestHandler, create_server, main
from storage_dashboard.runtime import ScanRuntime
from storage_dashboard.store import SnapshotStore


class FakeScanner:
    def __init__(self) -> None:
        self.calls = 0

    def scan(self):
        self.calls += 1
        return {
            "entries": [
                {
                    "path": "/tmp/safe.bin",
                    "kind": "file",
                    "size_bytes": 30,
                    "classification": "safe_to_review",
                    "reason": "large file",
                    "risk": "low",
                },
                {
                    "path": "/tmp/caution.db",
                    "kind": "file",
                    "size_bytes": 40,
                    "classification": "caution",
                    "reason": "database file",
                    "risk": "medium",
                },
                {
                    "path": "/tmp/unknown.txt",
                    "kind": "file",
                    "size_bytes": 50,
                    "classification": "do_not_touch",
                    "reason": "unclassified",
                    "risk": "high",
                },
            ],
            "logs": [{"path": "/tmp", "event": "ok", "message": "done"}],
        }


class FailingScanner:
    def scan(self):
        raise RuntimeError("secret local path /Users/example")


class ProgressScanner:
    def __init__(self) -> None:
        self.server = None
        self.observed_status = None

    def scan(self, progress=None):
        if progress is not None:
            progress(
                {
                    "phase": "directory",
                    "active_root": "/tmp/root",
                    "active_path": "/tmp/root/project",
                    "completed_roots": [],
                    "pending_roots": ["/tmp/root"],
                    "entries_seen": 4,
                    "logs_seen": 1,
                }
            )
        self.observed_status = self.server.scan_status()
        return {
            "entries": [
                {
                    "path": "/tmp/root/project",
                    "kind": "folder",
                    "size_bytes": 30,
                    "file_count": 2,
                    "classification": "safe_to_review",
                    "reason": "large folder",
                    "risk": "low",
                }
            ],
            "logs": [{"path": "/tmp/root", "event": "ok", "message": "done"}],
        }


class ApiTests(unittest.TestCase):
    def request(
        self,
        server,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        connection = HTTPConnection(server.server_address[0], server.server_address[1], timeout=2)
        try:
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"} if body is not None else {}
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)
        finally:
            connection.close()

    def raw_request(self, server, method: str, path: str):
        connection = HTTPConnection(server.server_address[0], server.server_address[1], timeout=2)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            body = response.read()
            return response.status, response.getheader("Content-Type"), body
        finally:
            connection.close()

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        return thread

    def test_main_reports_port_conflict_without_traceback(self) -> None:
        error = OSError(errno.EADDRINUSE, "Address already in use")

        with (
            patch("sys.argv", ["app.py"]),
            patch("storage_dashboard.server.run", side_effect=error),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit) as exit_error:
                main()

        self.assertEqual(exit_error.exception.code, 1)
        self.assertIn("Port 8765 is already in use.", stderr.getvalue())
        self.assertIn("python3 app.py --port 8766", stderr.getvalue())

    def test_store_prunes_snapshots_to_limit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "state.json", limit=2)
            for index in range(3):
                store.add_snapshot(
                    roots=[f"/tmp/root-{index}"],
                    disk={"total": 1, "used": 1, "free": 0, "percent": 100.0},
                    entries=[],
                    logs=[],
                )

            snapshots = store.load()["snapshots"]

        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0]["roots"], ["/tmp/root-1"])
        self.assertEqual(snapshots[1]["roots"], ["/tmp/root-2"])

    def test_store_invalid_json_fails_closed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text("{not json", encoding="utf-8")
            store = SnapshotStore(state_path)

            self.assertEqual(store.load(), {"snapshots": []})
            self.assertIsNone(store.latest())

    def test_store_non_object_or_non_list_snapshots_fail_closed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = SnapshotStore(state_path)

            state_path.write_text("[]", encoding="utf-8")
            self.assertEqual(store.load(), {"snapshots": []})

            state_path.write_text('{"snapshots": {}}', encoding="utf-8")
            self.assertEqual(store.load(), {"snapshots": []})

    def test_store_concurrent_adds_do_not_corrupt_json(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "state.json", limit=20)

            def add_snapshot(index: int) -> None:
                store.add_snapshot(
                    roots=[f"/tmp/root-{index}"],
                    disk={"total": 1, "used": 1, "free": 0, "percent": 100.0},
                    entries=[],
                    logs=[],
                )

            threads = [threading.Thread(target=add_snapshot, args=(index,)) for index in range(10)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            snapshots = store.load()["snapshots"]

        self.assertEqual(len(snapshots), 10)

    def test_api_scan_status_results_suggestions_and_export(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "state.json")
            server = create_server("127.0.0.1", 0, store=store, scanner=FakeScanner(), disk_path=Path(temp_dir))
            thread = self.serve(server)
            try:
                disk_status, disk_payload = self.request(server, "GET", "/api/disk")
                scan_status, scan_payload = self.request(server, "POST", "/api/scan")
                status_status, status_payload = self.request(server, "GET", "/api/scan/status")
                results_status, results_payload = self.request(server, "GET", "/api/scan/results")
                suggestions_status, suggestions_payload = self.request(server, "GET", "/api/suggestions")
                export_status, export_payload = self.request(server, "GET", "/api/export")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(disk_status, 200)
        self.assertEqual(set(disk_payload), {"total", "used", "free", "percent"})
        self.assertEqual(scan_status, 202)
        self.assertEqual(scan_payload["status"], "completed")
        self.assertEqual(status_status, 200)
        self.assertEqual(status_payload["status"], "completed")
        self.assertIn("phase", status_payload)
        self.assertIn("active_path", status_payload)
        self.assertIn("pending_roots", status_payload)
        self.assertEqual(results_status, 200)
        self.assertEqual(len(results_payload["entries"]), 3)
        self.assertEqual(suggestions_status, 200)
        self.assertEqual(
            [item["path"] for item in suggestions_payload["suggestions"]],
            ["/tmp/safe.bin", "/tmp/caution.db"],
        )
        self.assertEqual(export_status, 200)
        self.assertEqual(export_payload["report"]["snapshot"]["entries"][0]["path"], "/tmp/safe.bin")

    def test_missing_results_and_export_return_404_json(self) -> None:
        with TemporaryDirectory() as temp_dir:
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(Path(temp_dir) / "state.json"),
                scanner=FakeScanner(),
                disk_path=Path(temp_dir),
            )
            thread = self.serve(server)
            try:
                results_status, results_payload = self.request(server, "GET", "/api/scan/results")
                export_status, export_payload = self.request(server, "GET", "/api/export")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(results_status, 404)
        self.assertEqual(results_payload["error"], "no_scan_results")
        self.assertEqual(export_status, 404)
        self.assertEqual(export_payload["error"], "no_scan_results")

    def test_lazy_scan_path_rejects_out_of_root_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            outside = Path(temp_dir) / "outside"
            root.mkdir()
            outside.mkdir()
            store = SnapshotStore(Path(temp_dir) / "state.json")
            runtime = ScanRuntime(store=store, scanner=FakeScanner(), disk_path=Path(temp_dir), roots=(root,))
            server = create_server("127.0.0.1", 0, store=store, runtime=runtime)
            thread = self.serve(server)
            try:
                status, payload = self.request(server, "POST", "/api/scan/path", {"path": str(outside)})
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_scan_path")

    def test_lazy_scan_path_returns_requested_subtree_entries(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            project = root / "project"
            project.mkdir(parents=True)
            (project / "data.bin").write_bytes(b"123")
            store = SnapshotStore(Path(temp_dir) / "state.json")
            runtime = ScanRuntime(store=store, disk_path=Path(temp_dir), roots=(root,))
            server = create_server("127.0.0.1", 0, store=store, runtime=runtime)
            thread = self.serve(server)
            try:
                status, payload = self.request(server, "POST", "/api/scan/path", {"path": str(project)})
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["path"], str(project))
        self.assertEqual({entry["path"] for entry in payload["entries"]}, {str(project), str(project / "data.bin")})

    def test_unknown_api_path_returns_json_404(self) -> None:
        with TemporaryDirectory() as temp_dir:
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(Path(temp_dir) / "state.json"),
                scanner=FakeScanner(),
                disk_path=Path(temp_dir),
            )
            thread = self.serve(server)
            try:
                bare_status, bare_payload = self.request(server, "GET", "/api")
                status, payload = self.request(server, "GET", "/api/nope")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(bare_status, 404)
        self.assertEqual(bare_payload["error"], "not_found")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")

    def test_unsupported_api_methods_return_json_errors(self) -> None:
        with TemporaryDirectory() as temp_dir:
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(Path(temp_dir) / "state.json"),
                scanner=FakeScanner(),
                disk_path=Path(temp_dir),
            )
            thread = self.serve(server)
            try:
                put_status, put_content_type, put_body = self.raw_request(server, "PUT", "/api/nope")
                head_status, head_content_type, head_body = self.raw_request(server, "HEAD", "/api/nope")
                options_status, options_content_type, options_body = self.raw_request(server, "OPTIONS", "/api/nope")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(put_status, 405)
        self.assertIn("application/json", put_content_type)
        self.assertEqual(json.loads(put_body.decode("utf-8")), {"error": "method_not_allowed"})
        self.assertEqual(head_status, 405)
        self.assertIn("application/json", head_content_type)
        self.assertEqual(head_body, b"")
        self.assertEqual(options_status, 405)
        self.assertIn("application/json", options_content_type)
        self.assertEqual(json.loads(options_body.decode("utf-8")), {"error": "method_not_allowed"})

    def test_static_root_still_serves_frontend(self) -> None:
        with TemporaryDirectory() as temp_dir:
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(Path(temp_dir) / "state.json"),
                scanner=FakeScanner(),
                disk_path=Path(temp_dir),
            )
            thread = self.serve(server)
            connection = HTTPConnection(server.server_address[0], server.server_address[1], timeout=2)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                body = response.read().decode("utf-8")
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(response.status, 200)
        self.assertIn("LIGHTHOUSE", body)

    def test_frontend_uses_only_approved_api_paths_and_no_external_assets(self) -> None:
        web_root = Path(__file__).resolve().parent.parent / "web"
        frontend = "\n".join(
            (web_root / name).read_text(encoding="utf-8")
            for name in ("index.html", "styles.css", "app.js")
        )
        approved_paths = {
            "/api/disk",
            "/api/scan",
            "/api/scan/status",
            "/api/scan/results",
            "/api/suggestions",
            "/api/export",
        }

        self.assertNotIn("http://", frontend)
        self.assertNotIn("https://", frontend)
        self.assertNotIn("//cdn", frontend)
        self.assertNotIn("@import", frontend)

        api_paths = set()
        for token in frontend.replace('"', "'").split("'"):
            if token.startswith("/api/"):
                api_paths.add(token)
        self.assertEqual(api_paths, approved_paths)

    def test_corrupt_store_does_not_crash_api(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text("{not json", encoding="utf-8")
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(state_path),
                scanner=FakeScanner(),
                disk_path=Path(temp_dir),
            )
            thread = self.serve(server)
            try:
                status, payload = self.request(server, "GET", "/api/scan/results")
                scan_status, scan_payload = self.request(server, "POST", "/api/scan")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "no_scan_results")
        self.assertEqual(scan_status, 202)
        self.assertEqual(scan_payload["status"], "completed")

    def test_non_dict_snapshots_do_not_crash_api(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps({"snapshots": ["bad"]}), encoding="utf-8")
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(state_path),
                scanner=FakeScanner(),
                disk_path=Path(temp_dir),
            )
            thread = self.serve(server)
            try:
                suggestions_status, suggestions_payload = self.request(server, "GET", "/api/suggestions")
                export_status, export_payload = self.request(server, "GET", "/api/export")
                status_status, status_payload = self.request(server, "GET", "/api/scan/status")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(suggestions_status, 200)
        self.assertEqual(suggestions_payload, {"suggestions": []})
        self.assertEqual(export_status, 404)
        self.assertEqual(export_payload, {"error": "no_scan_results"})
        self.assertEqual(status_status, 200)
        self.assertEqual(status_payload["status"], "idle")
        self.assertEqual(status_payload["phase"], "idle")

    def test_scan_progress_updates_status_during_scan_and_finishes_cleanly(self) -> None:
        with TemporaryDirectory() as temp_dir:
            scanner = ProgressScanner()
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(Path(temp_dir) / "state.json"),
                scanner=scanner,
                disk_path=Path(temp_dir),
            )
            scanner.server = server

            snapshot = server.run_scan()
            running_status = scanner.observed_status
            final_status = server.scan_status()

        self.assertEqual(snapshot["entries"][0]["path"], "/tmp/root/project")
        self.assertEqual(running_status["status"], "running")
        self.assertEqual(running_status["phase"], "directory")
        self.assertEqual(running_status["active_root"], "/tmp/root")
        self.assertEqual(running_status["active_path"], "/tmp/root/project")
        self.assertEqual(running_status["pending_roots"], ["/tmp/root"])
        self.assertEqual(running_status["entries_seen"], 4)
        self.assertEqual(running_status["logs_seen"], 1)
        self.assertEqual(final_status["status"], "completed")
        self.assertEqual(final_status["phase"], "complete")
        self.assertIsNone(final_status["active_path"])
        self.assertEqual(final_status["pending_roots"], [])
        self.assertEqual(final_status["entries_seen"], 1)
        self.assertEqual(final_status["logs_seen"], 1)
        self.assertIsNotNone(final_status["finished_at"])

    def test_malformed_snapshot_size_does_not_crash_suggestions_or_export(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "snapshots": [
                            {
                                "id": "snapshot-1",
                                "timestamp": "2026-05-21T00:00:00+00:00",
                                "roots": [temp_dir],
                                "disk": {"total": 1, "used": 1, "free": 0, "percent": 100.0},
                                "entries": [
                                    {
                                        "path": "/tmp/bad-size.bin",
                                        "kind": "file",
                                        "size_bytes": "oops",
                                        "classification": "safe_to_review",
                                        "reason": "bad local state",
                                        "risk": "low",
                                    },
                                    {
                                        "path": "/tmp/caution.db",
                                        "kind": "file",
                                        "size_bytes": 5,
                                        "classification": "caution",
                                        "reason": "database file",
                                        "risk": "medium",
                                    },
                                ],
                                "logs": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(state_path),
                scanner=FakeScanner(),
                disk_path=Path(temp_dir),
            )
            thread = self.serve(server)
            try:
                suggestions_status, suggestions_payload = self.request(server, "GET", "/api/suggestions")
                export_status, export_payload = self.request(server, "GET", "/api/export")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(suggestions_status, 200)
        self.assertEqual(export_status, 200)
        self.assertEqual(suggestions_payload["suggestions"][0]["path"], "/tmp/bad-size.bin")
        self.assertEqual(suggestions_payload["suggestions"][0]["size_bytes"], 0)
        self.assertEqual(export_payload["report"]["suggestions"][0]["size_bytes"], 0)

    def test_scan_already_running_returns_consistent_error_envelope(self) -> None:
        with TemporaryDirectory() as temp_dir:
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(Path(temp_dir) / "state.json"),
                scanner=FakeScanner(),
                disk_path=Path(temp_dir),
            )
            server._scan_lock.acquire()
            thread = self.serve(server)
            try:
                status, payload = self.request(server, "POST", "/api/scan")
            finally:
                server._scan_lock.release()
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(status, 409)
        self.assertEqual(payload, {"error": "scan_already_running", "status": "running"})

    def test_scan_failure_returns_safe_error_envelope(self) -> None:
        with TemporaryDirectory() as temp_dir:
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(Path(temp_dir) / "state.json"),
                scanner=FailingScanner(),
                disk_path=Path(temp_dir),
            )
            thread = self.serve(server)
            try:
                status, payload = self.request(server, "POST", "/api/scan")
                status_status, status_payload = self.request(server, "GET", "/api/scan/status")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(status, 500)
        self.assertEqual(payload, {"error": "scan_failed", "status": "failed"})
        self.assertEqual(status_status, 200)
        self.assertEqual(status_payload["status"], "failed")
        self.assertEqual(status_payload["error"], "scan_failed")
        self.assertNotIn("secret", json.dumps(status_payload))

    def test_access_logs_are_quiet_by_default(self) -> None:
        with TemporaryDirectory() as temp_dir:
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(Path(temp_dir) / "state.json"),
                scanner=FakeScanner(),
                disk_path=Path(temp_dir),
            )
            thread = self.serve(server)
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    status, payload = self.request(server, "GET", "/api")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")
        self.assertEqual(stderr.getvalue(), "")

    def test_api_query_paths_do_not_read_arbitrary_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            secret_path = Path(temp_dir) / "secret.txt"
            secret_path.write_text("do-not-return-this", encoding="utf-8")
            server = create_server(
                "127.0.0.1",
                0,
                store=SnapshotStore(Path(temp_dir) / "state.json"),
                scanner=FakeScanner(),
                disk_path=Path(temp_dir),
            )
            thread = self.serve(server)
            try:
                export_status, export_payload = self.request(
                    server,
                    "GET",
                    f"/api/export?path={secret_path}",
                )
                unknown_status, unknown_payload = self.request(
                    server,
                    "GET",
                    f"/api/../../secret.txt?path={secret_path}",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(export_status, 404)
        self.assertEqual(export_payload, {"error": "no_scan_results"})
        self.assertNotIn("do-not-return-this", json.dumps(export_payload))
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_payload, {"error": "not_found"})
        self.assertNotIn("do-not-return-this", json.dumps(unknown_payload))

    def test_create_server_rejects_hosts_other_than_127_0_0_1(self) -> None:
        for host in ("0.0.0.0", "localhost", "::1"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    create_server(host, 0)


if __name__ == "__main__":
    unittest.main()
