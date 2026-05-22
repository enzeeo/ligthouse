from dataclasses import replace
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

        self.assertIn("LIGHTHOUSE", rows[0])
        self.assertIn("[1 Overview]", rows[1])
        self.assertTrue(any("Disk" in row for row in rows))

    def test_wide_overview_uses_btop_style_dashboard_panels(self) -> None:
        rows = tui.render(self.state("overview"), 120, 30)

        self.assertTrue(any("┐ Disk ┌" in row for row in rows))
        self.assertTrue(any("Review Queue" in row for row in rows))
        self.assertTrue(any("Scan Debugger" in row for row in rows))
        self.assertTrue(any("Growth History" in row for row in rows))
        self.assertTrue(any("Log Stream" in row for row in rows))
        self.assertFalse(any("�" in row for row in rows))
        self.assertFalse(any("/Users/enzeeo/Documents/HRI/misty-helping-study1/venv/lib/python3.11/site-packages" in row for row in rows))

    def test_panel_uses_btop_title_cradle(self) -> None:
        rows = tui.panel("Disk", ["body"], 24, 4)

        self.assertIn("┐ Disk ┌", rows[0])
        self.assertEqual(rows[0][0], "╭")
        self.assertEqual(rows[0][-1], "╮")
        self.assertEqual(rows[-1][0], "╰")
        self.assertEqual(rows[-1][-1], "╯")

    def test_renderer_golden_review(self) -> None:
        rows = tui.render(self.state("review"), 72, 12)

        self.assertIn("[2 Review]", rows[1])
        self.assertTrue(any("SAFE" in row and "file.bin" in row for row in rows))

    def test_selected_review_row_remains_visible_after_scroll(self) -> None:
        state = replace(self.state("review"), selected_index=1, scroll=1)

        rows = tui.render(state, 110, 14)

        self.assertTrue(any("▶" in row and "WARN" in row and "archive.zip" in row for row in rows))

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

        self.assertTrue(any("DONE /Users/me/Downloads" in row for row in rows))
        self.assertTrue(any("SCAN /Users/me/Desktop" in row for row in rows))
        self.assertTrue(any("WAIT /Users/me/Documents" in row for row in rows))

    def test_merge_runtime_tracks_runtime_roots(self) -> None:
        state = tui.merge_runtime(tui.TuiState(), FakeRuntime())

        self.assertEqual(state.scan_roots, ("/tmp/root", "/tmp/other"))

    def test_input_mapping_keys(self) -> None:
        curses = FakeCurses()

        self.assertEqual(tui.map_key(ord("q"), curses).name, "quit")
        self.assertEqual(tui.map_key(ord("r"), curses).name, "refresh")
        self.assertEqual(tui.map_key(ord("3"), curses), tui.InputAction("tab", "files"))
        self.assertEqual(tui.map_key(9, curses).name, "next_tab")
        self.assertEqual(tui.map_key(curses.KEY_RIGHT, curses).name, "next_tab")
        self.assertEqual(tui.map_key(curses.KEY_LEFT, curses).name, "previous_tab")
        self.assertEqual(tui.map_key(curses.KEY_BTAB, curses).name, "previous_tab")
        self.assertEqual(tui.map_key(curses.KEY_DOWN, curses).name, "down")
        self.assertEqual(tui.map_key(curses.KEY_UP, curses).name, "up")
        self.assertEqual(tui.map_key(curses.KEY_NPAGE, curses).name, "page_down")
        self.assertEqual(tui.map_key(curses.KEY_PPAGE, curses).name, "page_up")
        self.assertEqual(tui.map_key(curses.KEY_MOUSE, curses).name, "noop")

    def test_tab_cycle_actions_wrap_and_reset_position(self) -> None:
        state = tui.TuiState(active_tab="overview", scroll=4, selected_index=4, snapshot=SNAPSHOT)

        next_state = tui.reduce_state(state, tui.InputAction("next_tab"))
        self.assertEqual(next_state.active_tab, "review")
        self.assertEqual(next_state.scroll, 0)
        self.assertEqual(next_state.selected_index, 0)

        self.assertEqual(tui.reduce_state(tui.TuiState(active_tab="log"), tui.InputAction("next_tab")).active_tab, "overview")
        self.assertEqual(
            tui.reduce_state(tui.TuiState(active_tab="overview"), tui.InputAction("previous_tab")).active_tab,
            "log",
        )
        self.assertEqual(
            tui.reduce_state(tui.TuiState(active_tab="files"), tui.InputAction("previous_tab")).active_tab,
            "review",
        )

    def test_tab_key_during_first_scan_persists_for_snapshot_render(self) -> None:
        loading = tui.TuiState(status={"status": "running", "phase": "scan"})

        state = tui.reduce_state(loading, tui.map_key(9, FakeCurses()))
        state = replace(state, snapshot=SNAPSHOT, disk=SNAPSHOT["disk"])
        rows = tui.render(state, 72, 12)

        self.assertEqual(state.active_tab, "review")
        self.assertIn("[2 Review]", rows[1])

    def test_ascii_bar_fallback(self) -> None:
        self.assertEqual(tui.bar(50, 100, ascii_only=True, cells=4), "##..")

    def test_unicode_bar_uses_btop_meter_symbol(self) -> None:
        self.assertEqual(tui.bar(50, 100, cells=4), "■■··")

    def test_compact_path_shortens_long_home_paths(self) -> None:
        path = "/Users/enzeeo/Documents/HRI/misty-helping-study1/venv/lib/python3.11/site-packages/langchain/agents/foo.py"

        self.assertEqual(tui.compact_path(path, 43), "~/Documents/HRI/.../langchain/agents/foo.py")

    def test_setup_colors_tolerates_limited_color_terminal(self) -> None:
        tui.setup_colors(FakeLimitedColorCurses())

    def test_draw_applies_palette_background_when_available(self) -> None:
        curses = FakeColorCurses()
        screen = FakeScreen()

        tui.setup_colors(curses)
        tui.draw(screen, ["LIGHTHOUSE"], curses)

        self.assertTrue(screen.backgrounds)
        self.assertEqual(len(screen.added), len("LIGHTHOUSE"))
        self.assertTrue(all(len(text) == 1 for _y, _x, text, _attrs in screen.added))

    def test_256_color_palette_uses_non_basic_background(self) -> None:
        curses = FakeColor256Curses()

        tui.setup_colors(curses)

        background_pairs = [pair for pair in curses.pairs if pair[0] == tui.COLOR_PAIRS["background"]]
        self.assertTrue(background_pairs)
        self.assertGreaterEqual(background_pairs[0][2], 16)

    def test_terminal_cleanup_on_exception_and_sigint_handler_restore(self) -> None:
        curses = FakeCurses()
        old_handler = signal.getsignal(signal.SIGINT)
        with self.assertRaises(RuntimeError):
            with tui.CursesTerminal(curses):
                raise RuntimeError("boom")

        self.assertTrue(curses.nocbreak_called)
        self.assertTrue(curses.echo_called)
        self.assertTrue(curses.endwin_called)
        self.assertFalse(curses.mousemask_called)
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

    def test_idle_loop_does_not_reload_runtime_state_every_tick(self) -> None:
        screen = FakeScreen(keys=[-1, ord("q")])
        runtime = CountingRuntime()
        with patch("storage_dashboard.tui.time.sleep"), patch("storage_dashboard.tui.time.monotonic", side_effect=[0.0, 0.1]):
            tui._loop(screen, FakeCurses(), runtime)

        self.assertEqual(runtime.latest_calls, 1)


class FakeCurses:
    KEY_DOWN = 258
    KEY_UP = 259
    KEY_LEFT = 260
    KEY_RIGHT = 261
    KEY_NPAGE = 338
    KEY_PPAGE = 339
    KEY_BTAB = 353
    KEY_MOUSE = 409
    ALL_MOUSE_EVENTS = 1

    def __init__(self) -> None:
        self.screen = FakeScreen()
        self.nocbreak_called = False
        self.echo_called = False
        self.endwin_called = False
        self.mousemask_called = False

    def initscr(self):
        return self.screen

    def noecho(self):
        pass

    def cbreak(self):
        pass

    def curs_set(self, _value):
        pass

    def mousemask(self, _value):
        self.mousemask_called = True

    def nocbreak(self):
        self.nocbreak_called = True

    def echo(self):
        self.echo_called = True

    def endwin(self):
        self.endwin_called = True


class FakeLimitedColorCurses(FakeCurses):
    COLOR_BLACK = 0
    COLOR_BLUE = 4
    COLOR_CYAN = 6
    COLOR_GREEN = 2
    COLOR_MAGENTA = 5
    COLOR_RED = 1
    COLOR_WHITE = 7
    COLOR_YELLOW = 3

    def has_colors(self):
        return True

    def start_color(self):
        pass

    def use_default_colors(self):
        pass

    def init_pair(self, _pair, _fg, _bg):
        raise RuntimeError("limited color table")


class FakeColorCurses(FakeLimitedColorCurses):
    A_BOLD = 1 << 8

    def __init__(self) -> None:
        super().__init__()
        self.pairs = []

    def init_pair(self, pair, fg, bg):
        self.pairs.append((pair, fg, bg))

    def color_pair(self, pair):
        return pair << 16


class FakeColor256Curses(FakeColorCurses):
    COLORS = 256

    def can_change_color(self):
        return False


class FakeScreen:
    def __init__(self, keys=None) -> None:
        self.keys = list(keys or [])
        self.refreshed = False
        self.backgrounds = []
        self.added = []

    def keypad(self, _value):
        pass

    def nodelay(self, _value):
        pass

    def getmaxyx(self):
        return (12, 72)

    def erase(self):
        pass

    def bkgd(self, *args):
        self.backgrounds.append(args)

    def addstr(self, _y, _x, _text, *_attrs):
        self.added.append((_y, _x, _text, _attrs))

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


class CountingRuntime(FakeRuntime):
    def __init__(self) -> None:
        self.latest_calls = 0
        self._store = FakeStore()

    def latest_compatible_snapshot(self):
        self.latest_calls += 1
        return SNAPSHOT

    @property
    def store(self):
        return self._store


class FakeStore:
    def latest(self):
        return SNAPSHOT

    def load(self):
        return {"snapshots": [SNAPSHOT]}


if __name__ == "__main__":
    unittest.main()
