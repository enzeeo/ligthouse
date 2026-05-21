import signal
import unittest
from unittest.mock import patch

from storage_dashboard import tui


SNAPSHOT = {
    "id": "snap-1",
    "timestamp": "2026-05-21T12:00:00+00:00",
    "roots": ["/tmp/root"],
    "disk": {"total": 1000, "used": 500, "free": 500, "percent": 50.0},
    "entries": [
        {
            "path": "/tmp/root",
            "kind": "folder",
            "size_bytes": 900,
            "file_count": 2,
            "classification": "do_not_touch",
            "reason": "root",
            "risk": "high",
        },
        {
            "path": "/tmp/root/file.bin",
            "kind": "file",
            "size_bytes": 700,
            "classification": "safe_to_review",
            "reason": "large file",
            "risk": "low",
        },
        {
            "path": "/tmp/root/archive.zip",
            "kind": "file",
            "size_bytes": 200,
            "classification": "caution",
            "reason": "archive",
            "risk": "medium",
        },
    ],
    "logs": [{"path": "/tmp/root/link", "event": "symlink_skipped", "message": "Skipped symlink."}],
}


class TuiTests(unittest.TestCase):
    def state(self, tab: str) -> tui.TuiState:
        return tui.TuiState(
            active_tab=tab,
            snapshot=SNAPSHOT,
            disk=SNAPSHOT["disk"],
            status={"status": "completed", "phase": "complete"},
            history=(SNAPSHOT, {**SNAPSHOT, "id": "snap-2", "timestamp": "2026-05-21T12:10:00+00:00"}),
            scan_roots=("/tmp/root",),
        )

    def test_renderer_golden_overview(self) -> None:
        rows = tui.render(self.state("overview"), 72, 12)

        self.assertIn("LIGHTHOUSE  completed:complete", rows[0])
        self.assertIn("[1 Overview]", rows[1])
        self.assertTrue(any("Snapshot rows 3" in row for row in rows))

    def test_renderer_golden_review(self) -> None:
        rows = tui.render(self.state("review"), 72, 12)

        self.assertIn("[2 Review]", rows[1])
        self.assertTrue(any("safe_to_review" in row and "file.bin" in row for row in rows))

    def test_renderer_golden_files(self) -> None:
        rows = tui.render(self.state("files"), 72, 12)

        self.assertIn("[3 Files]", rows[1])
        self.assertTrue(any("file.bin" in row for row in rows))

    def test_renderer_golden_folders(self) -> None:
        rows = tui.render(self.state("folders"), 72, 12)

        self.assertIn("[4 Folders]", rows[1])
        self.assertTrue(any("/tmp/root" in row for row in rows))

    def test_renderer_golden_growth(self) -> None:
        rows = tui.render(self.state("growth"), 72, 12)

        self.assertIn("[5 Growth]", rows[1])
        self.assertTrue(any("2026-05-21" in row for row in rows))

    def test_renderer_golden_log(self) -> None:
        rows = tui.render(self.state("log"), 72, 12)

        self.assertIn("[6 Log]", rows[1])
        self.assertTrue(any("symlink_skipped" in row for row in rows))

    def test_tiny_terminal_fallback(self) -> None:
        rows = tui.render(self.state("overview"), 24, 5)

        self.assertEqual(len(rows), 5)
        self.assertTrue(any("r refresh" in row for row in rows))
        self.assertTrue(all(len(row) == 24 for row in rows))

    def test_first_scan_idle_minimal_screen(self) -> None:
        rows = tui.render(tui.TuiState(status={"status": "idle", "phase": "idle"}), 40, 8)

        self.assertEqual(len(rows), 8)
        self.assertTrue(any(row.strip() == "LIGHTHOUSE" for row in rows))
        self.assertTrue(any("Press r to scan" in row for row in rows))
        self.assertFalse(any("Overview" in row for row in rows))

    def test_first_scan_running_shows_active_path(self) -> None:
        state = tui.TuiState(
            status={
                "status": "running",
                "phase": "scan",
                "active_root": "/tmp/root",
                "active_path": "/tmp/root/project",
                "completed_roots": [],
                "pending_roots": ["/tmp/root"],
            },
            scan_roots=("/tmp/root",),
        )

        rows = tui.render(state, 72, 10)

        self.assertTrue(any("Checking /tmp/root/project" in row for row in rows))

    def test_debugger_lists_configured_top_level_roots(self) -> None:
        state = tui.TuiState(
            active_tab="overview",
            snapshot=SNAPSHOT,
            disk=SNAPSHOT["disk"],
            status={
                "status": "running",
                "phase": "scan",
                "active_root": "/Users/me/Desktop",
                "active_path": "/Users/me/Desktop/file.txt",
                "completed_roots": ["/Users/me/Downloads"],
                "pending_roots": ["/Users/me/Desktop", "/Users/me/Documents"],
            },
            scan_roots=("/Users/me/Downloads", "/Users/me/Desktop", "/Users/me/Documents"),
        )

        rows = tui.render(state, 100, 12)

        self.assertTrue(any("[done] /Users/me/Downloads" in row for row in rows))
        self.assertTrue(any("[scan] /Users/me/Desktop" in row for row in rows))
        self.assertTrue(any("[wait] /Users/me/Documents" in row for row in rows))

    def test_merge_runtime_tracks_runtime_roots(self) -> None:
        state = tui.merge_runtime(tui.TuiState(), FakeRuntime())

        self.assertEqual(state.scan_roots, ("/tmp/root", "/tmp/other"))

    def test_input_mapping_keys(self) -> None:
        curses = FakeCurses()

        self.assertEqual(tui.map_key(ord("q"), curses).name, "quit")
        self.assertEqual(tui.map_key(ord("r"), curses).name, "refresh")
        self.assertEqual(tui.map_key(ord("3"), curses), tui.InputAction("tab", "files"))
        self.assertEqual(tui.map_key(curses.KEY_DOWN, curses).name, "down")
        self.assertEqual(tui.map_key(curses.KEY_UP, curses).name, "up")
        self.assertEqual(tui.map_key(curses.KEY_NPAGE, curses).name, "page_down")
        self.assertEqual(tui.map_key(curses.KEY_PPAGE, curses).name, "page_up")
        self.assertEqual(tui.map_key(curses.KEY_MOUSE, curses).name, "mouse")

    def test_mouse_mapping_fallback(self) -> None:
        self.assertEqual(tui.map_mouse((0, 16, 1, 0, 0), 60), tui.InputAction("mouse_tab", "review"))
        self.assertEqual(tui.map_mouse((0, 2, 6, 0, 0), 60), tui.InputAction("mouse_row", 2))

    def test_ascii_bar_fallback(self) -> None:
        self.assertEqual(tui.bar(50, 100, ascii_only=True, cells=4), "##..")

    def test_terminal_cleanup_on_exception_and_sigint_handler_restore(self) -> None:
        curses = FakeCurses()
        old_handler = signal.getsignal(signal.SIGINT)
        with self.assertRaises(RuntimeError):
            with tui.CursesTerminal(curses):
                raise RuntimeError("boom")

        self.assertTrue(curses.nocbreak_called)
        self.assertTrue(curses.echo_called)
        self.assertTrue(curses.endwin_called)
        self.assertEqual(signal.getsignal(signal.SIGINT), old_handler)

    def test_sigint_handler_raises_keyboard_interrupt(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            tui._raise_keyboard_interrupt(signal.SIGINT, None)

    def test_quit_key_exits_loop_and_draws_once(self) -> None:
        screen = FakeScreen(keys=[ord("q")])
        runtime = FakeRuntime()
        with patch("storage_dashboard.tui.time.sleep"):
            tui._loop(screen, FakeCurses(), runtime)

        self.assertTrue(screen.refreshed)


class FakeCurses:
    KEY_DOWN = 258
    KEY_UP = 259
    KEY_NPAGE = 338
    KEY_PPAGE = 339
    KEY_MOUSE = 409
    ALL_MOUSE_EVENTS = 1

    def __init__(self) -> None:
        self.screen = FakeScreen()
        self.nocbreak_called = False
        self.echo_called = False
        self.endwin_called = False

    def initscr(self):
        return self.screen

    def noecho(self):
        pass

    def cbreak(self):
        pass

    def curs_set(self, _value):
        pass

    def mousemask(self, _value):
        pass

    def nocbreak(self):
        self.nocbreak_called = True

    def echo(self):
        self.echo_called = True

    def endwin(self):
        self.endwin_called = True


class FakeScreen:
    def __init__(self, keys=None) -> None:
        self.keys = list(keys or [])
        self.refreshed = False

    def keypad(self, _value):
        pass

    def nodelay(self, _value):
        pass

    def getmaxyx(self):
        return (12, 72)

    def erase(self):
        pass

    def addstr(self, _y, _x, _text):
        pass

    def refresh(self):
        self.refreshed = True

    def getch(self):
        if self.keys:
            return self.keys.pop(0)
        return ord("q")


class FakeRuntime:
    store = None
    roots = ("/tmp/root", "/tmp/other")

    def latest_compatible_snapshot(self):
        return SNAPSHOT

    def scan_status(self):
        return {"status": "idle", "phase": "idle"}

    def disk_summary(self):
        return SNAPSHOT["disk"]

    def start_background_scan(self):
        return object()

    def ensure_fresh_background(self):
        return object()

    @property
    def store(self):
        return FakeStore()


class FakeStore:
    def latest(self):
        return SNAPSHOT

    def load(self):
        return {"snapshots": [SNAPSHOT]}


if __name__ == "__main__":
    unittest.main()
