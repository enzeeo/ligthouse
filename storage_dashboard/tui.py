"""Curses terminal dashboard for Lighthouse."""

from __future__ import annotations

from dataclasses import dataclass, replace
import locale
import os
import signal
import sys
import time
from typing import Sequence

from storage_dashboard.runtime import ScanRuntime
from storage_dashboard.server import suggestions_from_snapshot

TAB_ORDER = ("overview", "review", "files", "folders", "growth", "log")
TAB_LABELS = {
    "overview": "Overview",
    "review": "Review",
    "files": "Files",
    "folders": "Folders",
    "growth": "Growth",
    "log": "Log",
}

COLOR_PAIRS = {
    "title": 1,
    "accent": 2,
    "ok": 3,
    "warn": 4,
    "muted": 5,
    "border": 6,
    "danger": 7,
    "background": 8,
    "panel": 9,
}
RUNNING_REFRESH_SECONDS = 0.2
IDLE_REFRESH_SECONDS = 1.0
RUNNING_SLEEP_SECONDS = 0.05
IDLE_SLEEP_SECONDS = 0.2

PALETTE = {
    "background": "#111c2a",
    "panel": "#253954",
    "danger": "#882139",
    "accent": "#bfc5ca",
    "border": "#7c9cb3",
    "text": "#dce4e9",
}

SYMBOLS = {
    "h": "─",
    "v": "│",
    "lu": "╭",
    "ru": "╮",
    "ld": "╰",
    "rd": "╯",
    "title_left": "┐",
    "title_right": "┌",
    "meter": "■",
    "meter_bg": "·",
}

BRAILLE_UP = (
    " ",
    "⢀",
    "⢠",
    "⢰",
    "⢸",
    "⡀",
    "⣀",
    "⣠",
    "⣰",
    "⣸",
    "⡄",
    "⣄",
    "⣤",
    "⣴",
    "⣼",
    "⡆",
    "⣆",
    "⣦",
    "⣶",
    "⣾",
    "⡇",
    "⣇",
    "⣧",
    "⣷",
    "⣿",
)

BORDER_CHARS = frozenset("╭╮╰╯─│├┤┬┴┐┌")
METER_CHARS = frozenset((SYMBOLS["meter"], *BRAILLE_UP[1:]))

CUSTOM_COLORS = {
    "background": 16,
    "panel": 17,
    "danger": 18,
    "accent": 19,
    "border": 20,
    "text": 21,
}


@dataclass(frozen=True)
class TuiState:
    active_tab: str = "overview"
    scroll: int = 0
    selected_index: int = 0
    notice: str = ""
    use_ascii: bool = False
    snapshot: dict[str, object] | None = None
    status: dict[str, object] | None = None
    disk: dict[str, object] | None = None
    history: tuple[dict[str, object], ...] = ()
    scan_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class InputAction:
    name: str
    value: object = None


def run(runtime: ScanRuntime | None = None) -> None:
    if os.name == "nt":
        print("Lighthouse TUI is unsupported on Windows in v1.", file=sys.stderr)
        raise SystemExit(1)

    runtime = runtime or ScanRuntime()
    runtime.ensure_fresh_background()
    try:
        import curses
    except ImportError:
        print("Lighthouse TUI requires Python curses support.", file=sys.stderr)
        raise SystemExit(1) from None

    with CursesTerminal(curses) as screen:
        _loop(screen, curses, runtime)


class CursesTerminal:
    """Curses setup/cleanup wrapper kept small for testability."""

    def __init__(self, curses_module) -> None:
        self.curses = curses_module
        self.screen = None
        self._old_sigint = None

    def __enter__(self):
        setup_locale()
        self.screen = self.curses.initscr()
        self.curses.noecho()
        self.curses.cbreak()
        try:
            self.curses.curs_set(0)
        except Exception:
            pass
        self.screen.keypad(True)
        self.screen.nodelay(True)
        setup_colors(self.curses)
        self._old_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _raise_keyboard_interrupt)
        return self.screen

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.screen is not None:
            try:
                self.screen.keypad(False)
            except Exception:
                pass
        try:
            self.curses.curs_set(1)
        except Exception:
            pass
        self.curses.nocbreak()
        self.curses.echo()
        self.curses.endwin()
        if self._old_sigint is not None:
            signal.signal(signal.SIGINT, self._old_sigint)
        return False


def setup_locale() -> None:
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass


def _raise_keyboard_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def _loop(screen, curses, runtime: ScanRuntime) -> None:
    state = load_state(runtime)
    next_runtime_refresh = time.monotonic() + runtime_refresh_interval(state.status)
    while True:
        height, width = screen.getmaxyx()
        draw(screen, render(state, width, height), curses)
        key = screen.getch()
        if key != -1:
            action = map_key(key, curses)
            if action.name == "quit":
                return
            if action.name == "refresh":
                start = runtime.start_background_scan()
                state = replace(state, notice="Scan already running." if start.status == "running" else "Scan started.")
                state = merge_runtime(state, runtime)
                next_runtime_refresh = time.monotonic() + runtime_refresh_interval(state.status)
            else:
                state = reduce_state(state, action, width, height)
        now = time.monotonic()
        if now >= next_runtime_refresh:
            state = merge_runtime(state, runtime)
            next_runtime_refresh = now + runtime_refresh_interval(state.status)
        time.sleep(loop_sleep_interval(state.status))


def runtime_refresh_interval(status: dict[str, object] | None) -> float:
    if (status or {}).get("status") == "running":
        return RUNNING_REFRESH_SECONDS
    return IDLE_REFRESH_SECONDS


def loop_sleep_interval(status: dict[str, object] | None) -> float:
    if (status or {}).get("status") == "running":
        return RUNNING_SLEEP_SECONDS
    return IDLE_SLEEP_SECONDS


def load_state(runtime: ScanRuntime) -> TuiState:
    return merge_runtime(TuiState(), runtime)


def merge_runtime(state: TuiState, runtime: ScanRuntime) -> TuiState:
    snapshot = runtime.latest_compatible_snapshot()
    history = tuple(item for item in runtime.store.load().get("snapshots", []) if isinstance(item, dict))
    scan_roots = tuple(str(root) for root in getattr(runtime, "roots", ()))
    return replace(
        state,
        snapshot=snapshot,
        status=runtime.scan_status(),
        disk=(snapshot.get("disk") if isinstance(snapshot, dict) else None) or runtime.disk_summary(),
        history=history[-10:],
        scan_roots=scan_roots,
    )


def reduce_state(state: TuiState, action: InputAction, width: int = 80, height: int = 24) -> TuiState:
    if action.name == "tab" and action.value in TAB_ORDER:
        return replace(state, active_tab=str(action.value), scroll=0, selected_index=0)
    if action.name in ("next_tab", "previous_tab"):
        direction = 1 if action.name == "next_tab" else -1
        index = TAB_ORDER.index(state.active_tab) if state.active_tab in TAB_ORDER else 0
        return replace(state, active_tab=TAB_ORDER[(index + direction) % len(TAB_ORDER)], scroll=0, selected_index=0)
    if action.name == "down":
        return replace(state, selected_index=state.selected_index + 1, scroll=state.scroll + 1)
    if action.name == "up":
        return replace(state, selected_index=max(0, state.selected_index - 1), scroll=max(0, state.scroll - 1))
    if action.name == "page_down":
        amount = max(1, height - 6)
        return replace(state, selected_index=state.selected_index + amount, scroll=state.scroll + amount)
    if action.name == "page_up":
        amount = max(1, height - 6)
        return replace(state, selected_index=max(0, state.selected_index - amount), scroll=max(0, state.scroll - amount))
    if action.name == "select":
        return replace(state, notice="Selected row.")
    return state


def map_key(key: int, curses_module) -> InputAction:
    if key in (ord("q"), ord("Q")):
        return InputAction("quit")
    if key in (ord("r"), ord("R")):
        return InputAction("refresh")
    if ord("1") <= key <= ord("6"):
        return InputAction("tab", TAB_ORDER[key - ord("1")])
    if key in (9, ord("l"), ord("L")) or key == getattr(curses_module, "KEY_RIGHT", -999):
        return InputAction("next_tab")
    if key in (ord("h"), ord("H")) or key in (
        getattr(curses_module, "KEY_LEFT", -999),
        getattr(curses_module, "KEY_BTAB", -998),
    ):
        return InputAction("previous_tab")
    if key in (10, 13):
        return InputAction("select")
    if key == curses_module.KEY_DOWN:
        return InputAction("down")
    if key == curses_module.KEY_UP:
        return InputAction("up")
    if key == curses_module.KEY_NPAGE:
        return InputAction("page_down")
    if key == curses_module.KEY_PPAGE:
        return InputAction("page_up")
    return InputAction("noop")


def render(state: TuiState, width: int, height: int) -> list[str]:
    width = max(20, width)
    height = max(5, height)
    if not isinstance(state.snapshot, dict):
        rows = render_first_scan(state, width, height)
        return pad_rows(rows, width, height)

    rows = [
        chrome_bar(f" LIGHTHOUSE  {status_text(state).upper()}  {snapshot_text(state)} ", width, "top"),
        render_tabs(state.active_tab, width),
        chrome_bar(f" {state.notice or 'read-only storage dashboard'}  R refresh  Q quit  1-6 tabs  TAB/H/L move ", width, "mid"),
    ]

    if height <= 8:
        rows.extend(render_tiny(state, width))
    elif state.active_tab == "overview":
        rows.extend(render_overview_dashboard(state, width, height - len(rows)))
    else:
        rows.extend(render_active_tab(state, width, height - len(rows)))
    return pad_rows(rows, width, height)


def render_first_scan(state: TuiState, width: int, height: int) -> list[str]:
    status = state.status or {}
    if status.get("status") == "running":
        rows = [
            center("LIGHTHOUSE", width),
            center(status_text(state), width),
            fit(running_line(state), width),
        ]
        if height > 6:
            rows.extend(panel("Scan Debugger", debugger_rows(state), width, max(3, height - len(rows))))
        return rows

    top_pad = max(0, (height - 4) // 2)
    return [" " * width for _ in range(top_pad)] + [
        center("LIGHTHOUSE", width),
        center("Press r to scan", width),
        center("q quit", width),
    ]


def render_tabs(active_tab: str, width: int) -> str:
    pieces = []
    for index, tab in enumerate(TAB_ORDER, start=1):
        label = f"{index} {TAB_LABELS[tab]}"
        pieces.append(f"[{label}]" if tab == active_tab else f" {label} ")
    return chrome_bar(" " + " ".join(pieces) + " ", width, "mid")


def render_tiny(state: TuiState, width: int) -> list[str]:
    entries = entries_from_state(state)
    logs = logs_from_state(state)
    rows = [
        fit(f"{TAB_LABELS[state.active_tab]} | rows {len(entries)} | logs {len(logs)}", width),
        fit("r refresh  q quit", width),
    ]
    if (state.status or {}).get("status") == "running":
        rows.insert(1, fit(running_line(state), width))
    return rows


def render_overview_dashboard(state: TuiState, width: int, available: int) -> list[str]:
    if available <= 6:
        return render_tiny(state, width)[:available]
    if width >= 108 and available >= 15:
        top_height = max(5, min(9, available // 3))
        middle_height = max(5, min(9, (available - top_height) // 2))
        bottom_height = max(3, available - top_height - middle_height)
        left_width, right_width = split_width(width, 0.48)
        rows = join_columns(
            (
                panel("Disk", disk_rows(state, left_width), left_width, top_height),
                panel("Scan Debugger", debugger_rows(state), right_width, top_height),
            ),
            width,
        )
        rows.extend(
            join_columns(
                (
                    panel("Review Queue", review_queue_rows(state), left_width, middle_height),
                    panel("Folder Meters", folder_meter_rows(state, right_width), right_width, middle_height),
                ),
                width,
            )
        )
        rows.extend(
            join_columns(
                (
                    panel("Growth History", growth_rows(state), left_width, bottom_height),
                    panel("Log Stream", log_stream_rows(state), right_width, bottom_height),
                ),
                width,
            )
        )
        return rows[:available]

    if width >= 96 and available >= 8:
        left_width, right_width = split_width(width, 0.48)
        return join_columns(
            (
                panel("Disk", disk_rows(state, left_width), left_width, available),
                panel("Scan Debugger", debugger_rows(state), right_width, available),
            ),
            width,
        )

    disk_height = min(available, max(5, available // 2))
    rows = panel("Disk", disk_rows(state, width), width, disk_height)
    remaining = available - len(rows)
    if remaining > 0:
        rows.extend(panel("Scan Debugger", debugger_rows(state), width, remaining))
    return rows[:available]


def render_active_tab(state: TuiState, width: int, available: int) -> list[str]:
    title, rows, items = tab_content(state)
    if items:
        header, data_rows = rows[:1], rows[1:]
        visible_count = max(1, available - 3)
        visible = header + mark_selected(data_rows[state.scroll : state.scroll + visible_count], state.selected_index - state.scroll, width)
        selected = items[max(0, min(state.selected_index, len(items) - 1))]
    else:
        visible = rows[state.scroll : state.scroll + max(1, available - 2)]
        selected = None

    if width >= 104 and available >= 8 and state.active_tab in ("review", "files", "folders"):
        left_width, right_width = split_width(width, 0.66)
        return join_columns(
            (
                panel(title, visible, left_width, available),
                panel("Details", detail_rows(selected), right_width, available),
            ),
            width,
        )
    return panel(title, visible, width, available)


def tab_content(state: TuiState) -> tuple[str, list[str], list[dict[str, object]]]:
    if state.active_tab == "review":
        items = suggestions_from_snapshot(state.snapshot)
        return "Review Queue", table_rows(items, ("classification", "size_bytes", "path")), items
    if state.active_tab == "files":
        items = sorted((entry for entry in entries_from_state(state) if entry.get("kind") == "file"), key=size_of, reverse=True)
        return "Files", table_rows(items, ("size_bytes", "classification", "path")), items
    if state.active_tab == "folders":
        items = sorted((entry for entry in entries_from_state(state) if entry.get("kind") == "folder"), key=size_of, reverse=True)
        return "Folders", table_rows(items, ("size_bytes", "file_count", "path")), items
    if state.active_tab == "growth":
        return "Growth History", growth_rows(state), []
    if state.active_tab == "log":
        items = logs_from_state(state)
        return "Log Stream", table_rows(items, ("event", "message", "path")), []
    return "Overview", overview_rows(state, 80), []


def disk_rows(state: TuiState, width: int) -> list[str]:
    disk = state.disk or {}
    entries = entries_from_state(state)
    suggestions = suggestions_from_snapshot(state.snapshot)
    logs = logs_from_state(state)
    percent = number(disk.get("percent"))
    return [
        f"Used {format_bytes(disk.get('used'))} / {format_bytes(disk.get('total'))}  Free {format_bytes(disk.get('free'))}",
        framed_meter("DISK", percent, width=max(12, min(30, width - 16)), ascii_only=state.use_ascii),
        f"Rows {len(entries)}  Review {len(suggestions)}  Logs {len(logs)}",
        f"Trend {sparkline([total_bytes(snapshot) for snapshot in state.history], max(8, width - 8), state.use_ascii)}",
    ]


def review_queue_rows(state: TuiState) -> list[str]:
    suggestions = suggestions_from_snapshot(state.snapshot)
    if not suggestions:
        return ["No safe review candidates yet."]
    rows = []
    for item in suggestions[:6]:
        rows.append(f"{risk_label(item)} {format_bytes(item.get('size_bytes')):>7} {compact_path(item.get('path'), 64)}")
    return rows


def folder_meter_rows(state: TuiState, width: int) -> list[str]:
    roots = root_summaries(state)
    if not roots:
        return ["No scan roots yet."]
    max_size = max(1, *(int(root["size"]) for root in roots))
    rows = []
    for root in roots[:5]:
        rows.append(
            f"{format_bytes(root['size']):>7} {bar(root['size'], max_size, state.use_ascii, max(4, min(18, width // 4)))} {compact_path(root['path'], 42)}"
        )
    return rows


def log_stream_rows(state: TuiState) -> list[str]:
    logs = logs_from_state(state)
    if not logs:
        return ["No scanner events."]
    rows = []
    for item in logs[-6:]:
        rows.append(f"{item.get('event', '')} {item.get('message', '')} {compact_path(item.get('path'), 46)}")
    return rows


def overview_rows(state: TuiState, width: int) -> list[str]:
    disk = state.disk or {}
    entries = entries_from_state(state)
    logs = logs_from_state(state)
    suggestions = suggestions_from_snapshot(state.snapshot)
    percent = number(disk.get("percent"))
    rows = [
        f"Disk      {format_bytes(disk.get('used'))} / {format_bytes(disk.get('total'))}  {percent:.1f}%",
        meter(percent, state.use_ascii),
        f"Snapshot rows {len(entries)}  review {len(suggestions)}  log {len(logs)}",
    ]
    for root in root_summaries(state)[: max(1, min(5, width // 20))]:
        rows.append(f"{format_bytes(root['size'])}  {root['path']}")
    return rows


def debugger_rows(state: TuiState) -> list[str]:
    roots = debugger_roots(state)
    if not roots:
        return ["No scan roots configured."]

    completed = set(string_list((state.status or {}).get("completed_roots")))
    pending = set(string_list((state.status or {}).get("pending_roots")))
    active = str((state.status or {}).get("active_root") or "")
    rows = [running_line(state)]
    for root in roots:
        if root in completed:
            marker = "DONE"
        elif root == active:
            marker = "SCAN"
        elif root in pending:
            marker = "WAIT"
        else:
            marker = "IDLE"
        rows.append(f"{marker} {compact_path(root, 54)}")
    return rows


def debugger_roots(state: TuiState) -> list[str]:
    if state.scan_roots:
        return list(state.scan_roots)
    if isinstance(state.snapshot, dict):
        roots = state.snapshot.get("roots", [])
        if isinstance(roots, list):
            return [str(root) for root in roots]
    status = state.status or {}
    roots = string_list(status.get("completed_roots")) + string_list(status.get("pending_roots"))
    active = status.get("active_root")
    if active is not None:
        roots.append(str(active))
    return dedupe(roots)


def running_line(state: TuiState) -> str:
    status = state.status or {}
    active_path = status.get("active_path")
    active_root = status.get("active_root")
    pending = string_list(status.get("pending_roots"))
    target = active_path or active_root or (pending[0] if pending else None)
    if status.get("status") == "running" and target:
        return f"Checking {compact_path(target, 64)}"
    return "Scan idle"


def growth_rows(state: TuiState) -> list[str]:
    if len(state.history) < 2:
        return ["Not enough local snapshot history yet."]
    values = [(snapshot.get("timestamp", ""), total_bytes(snapshot)) for snapshot in state.history]
    max_value = max(1, *(value for _time, value in values))
    trend = sparkline([value for _time, value in values], 12, state.use_ascii)
    return [f"{short_time(time_value)} {bar(value, max_value, state.use_ascii)} {format_bytes(value)}  {trend}" for time_value, value in values]


def table_rows(items: Sequence[dict[str, object]], fields: tuple[str, ...]) -> list[str]:
    if not items:
        return ["No rows."]
    rows = ["  ".join(field.upper() for field in fields)]
    for item in items:
        cells = []
        for field in fields:
            cells.append(format_cell(item, field))
        rows.append("  ".join(cells))
    return rows


def format_cell(item: dict[str, object], field: str) -> str:
    value = item.get(field)
    if field == "size_bytes":
        return format_bytes(value)
    if field == "classification":
        return risk_label(item)
    if field == "path":
        return compact_path(value, 76)
    return str(value or "")


def detail_rows(item: dict[str, object] | None) -> list[str]:
    if not item:
        return ["No row selected."]
    rows = [
        f"Status {risk_label(item)}",
        f"Size   {format_bytes(item.get('size_bytes'))}",
        f"Kind   {item.get('kind', '')}",
    ]
    if item.get("file_count") is not None:
        rows.append(f"Files  {item.get('file_count')}")
    if item.get("reason"):
        rows.append(f"Reason {item.get('reason')}")
    rows.extend(
        [
            f"Path   {compact_path(item.get('path'), 72)}",
            "Mode   read-only",
        ]
    )
    return rows


def risk_label(item: dict[str, object] | object) -> str:
    if isinstance(item, dict):
        value = str(item.get("classification") or item.get("risk") or "").lower()
    else:
        value = str(item or "").lower()
    if value in ("safe_to_review", "safe", "low"):
        return "SAFE"
    if value in ("caution", "medium", "warn", "warning"):
        return "WARN"
    if value in ("do_not_touch", "high", "critical", "keep"):
        return "KEEP"
    return value.upper() if value else "ITEM"


def mark_selected(rows: list[str], selected: int, width: int) -> list[str]:
    marked = []
    for index, row in enumerate(rows):
        prefix = "▶ " if index == selected else "  "
        marked.append(fit(prefix + row, width))
    return marked


def draw(screen, rows: list[str], curses_module=None) -> None:
    if curses_module is not None:
        try:
            screen.bkgd(" ", curses_module.color_pair(COLOR_PAIRS["background"]))
        except Exception:
            pass
    screen.erase()
    for y, row in enumerate(rows):
        draw_row(screen, y, row, curses_module)
    screen.refresh()


def draw_row(screen, y: int, row: str, curses_module) -> None:
    if curses_module is None:
        try:
            screen.addstr(y, 0, row)
        except Exception:
            pass
        return
    base_attr = row_attr(row, curses_module) or safe_color_pair(curses_module, "background")
    for x, char in enumerate(row):
        try:
            screen.addstr(y, x, char, char_attr(row, char, base_attr, curses_module))
        except Exception:
            pass


def char_attr(row: str, char: str, base_attr: int, curses_module) -> int:
    if char in BORDER_CHARS:
        return safe_color_pair(curses_module, "border")
    if char in METER_CHARS:
        return safe_color_pair(curses_module, "title") | getattr(curses_module, "A_BOLD", 0)
    if "KEEP" in row and char.strip():
        return safe_color_pair(curses_module, "danger") | getattr(curses_module, "A_BOLD", 0)
    if "WARN" in row and char.strip():
        return safe_color_pair(curses_module, "warn")
    if ("SAFE" in row or "DONE" in row) and char.strip():
        return safe_color_pair(curses_module, "ok")
    if ("WAIT" in row or "IDLE" in row) and char.strip():
        return safe_color_pair(curses_module, "muted")
    return base_attr


def safe_color_pair(curses_module, name: str) -> int:
    try:
        return curses_module.color_pair(COLOR_PAIRS[name])
    except Exception:
        return 0


def setup_colors(curses_module) -> None:
    try:
        if not curses_module.has_colors():
            return
        curses_module.start_color()
        if hasattr(curses_module, "use_default_colors"):
            curses_module.use_default_colors()
        colors = color_ids(curses_module)
        curses_module.init_pair(COLOR_PAIRS["title"], colors["accent"], colors["background"])
        curses_module.init_pair(COLOR_PAIRS["accent"], colors["text"], colors["panel"])
        curses_module.init_pair(COLOR_PAIRS["ok"], getattr(curses_module, "COLOR_GREEN", colors["accent"]), colors["background"])
        curses_module.init_pair(COLOR_PAIRS["warn"], getattr(curses_module, "COLOR_YELLOW", colors["accent"]), colors["background"])
        curses_module.init_pair(COLOR_PAIRS["muted"], colors["border"], colors["background"])
        curses_module.init_pair(COLOR_PAIRS["border"], colors["border"], colors["background"])
        curses_module.init_pair(COLOR_PAIRS["danger"], colors["danger"], colors["background"])
        curses_module.init_pair(COLOR_PAIRS["background"], colors["text"], colors["background"])
        curses_module.init_pair(COLOR_PAIRS["panel"], colors["text"], colors["panel"])
    except Exception:
        return


def color_ids(curses_module) -> dict[str, int]:
    colors = {
        "background": getattr(curses_module, "COLOR_BLACK", -1),
        "panel": getattr(curses_module, "COLOR_BLUE", -1),
        "danger": getattr(curses_module, "COLOR_RED", getattr(curses_module, "COLOR_MAGENTA", -1)),
        "accent": getattr(curses_module, "COLOR_CYAN", getattr(curses_module, "COLOR_BLUE", -1)),
        "border": getattr(curses_module, "COLOR_CYAN", getattr(curses_module, "COLOR_BLUE", -1)),
        "text": getattr(curses_module, "COLOR_WHITE", -1),
    }
    try:
        can_change = bool(curses_module.can_change_color())
        color_count = int(getattr(curses_module, "COLORS", 0))
    except Exception:
        return colors
    if color_count >= 256 and not can_change:
        return {name: rgb_256(PALETTE[name]) for name in colors}
    if not can_change or color_count <= max(CUSTOM_COLORS.values()):
        return colors
    try:
        for name, index in CUSTOM_COLORS.items():
            curses_module.init_color(index, *rgb_1000(PALETTE[name]))
            colors[name] = index
    except Exception:
        return colors
    return colors


def rgb_256(hex_color: str) -> int:
    value = hex_color.lstrip("#")
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    if red == green == blue:
        if red < 8:
            return 16
        if red > 248:
            return 231
        return round(((red - 8) / 247) * 24) + 232
    return 16 + (36 * round(red / 255 * 5)) + (6 * round(green / 255 * 5)) + round(blue / 255 * 5)


def rgb_1000(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    return round(red / 255 * 1000), round(green / 255 * 1000), round(blue / 255 * 1000)


def row_attr(row: str, curses_module) -> int:
    if curses_module is None:
        return 0
    try:
        if "LIGHTHOUSE" in row:
            return curses_module.color_pair(COLOR_PAIRS["title"]) | getattr(curses_module, "A_BOLD", 0)
        if "▶" in row:
            return curses_module.color_pair(COLOR_PAIRS["accent"]) | getattr(curses_module, "A_BOLD", 0)
        if "KEEP" in row:
            return curses_module.color_pair(COLOR_PAIRS["danger"]) | getattr(curses_module, "A_BOLD", 0)
        if "WARN" in row:
            return curses_module.color_pair(COLOR_PAIRS["warn"])
        if "SAFE" in row or "DONE" in row:
            return curses_module.color_pair(COLOR_PAIRS["ok"])
        if "SCAN" in row or "Checking " in row:
            return curses_module.color_pair(COLOR_PAIRS["title"])
        if "WAIT" in row or "IDLE" in row:
            return curses_module.color_pair(COLOR_PAIRS["muted"])
        if row.startswith("╭") or row.startswith("├") or row.startswith("╰"):
            return curses_module.color_pair(COLOR_PAIRS["border"])
        if row.startswith("+") or row.startswith("|") or row.startswith("│") or row.startswith("╳"):
            return curses_module.color_pair(COLOR_PAIRS["panel"])
    except Exception:
        return 0
    return 0


def status_text(state: TuiState) -> str:
    status = state.status or {}
    return f"{status.get('status', 'idle')}:{status.get('phase', 'idle')}"


def snapshot_text(state: TuiState) -> str:
    if not isinstance(state.snapshot, dict):
        return "No snapshot yet. Press r to scan."
    return f"Snapshot {short_time(state.snapshot.get('timestamp'))}"


def entries_from_state(state: TuiState) -> list[dict[str, object]]:
    if not isinstance(state.snapshot, dict):
        return []
    entries = state.snapshot.get("entries", [])
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def logs_from_state(state: TuiState) -> list[dict[str, object]]:
    if not isinstance(state.snapshot, dict):
        return []
    logs = state.snapshot.get("logs", [])
    return [log for log in logs if isinstance(log, dict)] if isinstance(logs, list) else []


def root_summaries(state: TuiState) -> list[dict[str, object]]:
    if not isinstance(state.snapshot, dict):
        return []
    roots = state.snapshot.get("roots", [])
    if not isinstance(roots, list):
        return []
    entries = entries_from_state(state)
    summaries = []
    for root in roots:
        root_text = str(root)
        root_entry = next((entry for entry in entries if entry.get("path") == root_text and entry.get("kind") == "folder"), None)
        size = size_of(root_entry) if root_entry else sum(size_of(entry) for entry in entries if str(entry.get("path", "")).startswith(root_text))
        summaries.append({"path": root_text, "size": size})
    return sorted(summaries, key=lambda item: int(item["size"]), reverse=True)


def total_bytes(snapshot: dict[str, object]) -> int:
    entries = snapshot.get("entries", [])
    if not isinstance(entries, list):
        return 0
    roots = snapshot.get("roots", [])
    if isinstance(roots, list):
        root_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("path") in roots and entry.get("kind") == "folder"]
        if root_entries:
            return sum(size_of(entry) for entry in root_entries)
    return sum(size_of(entry) for entry in entries if isinstance(entry, dict) and entry.get("kind") == "file")


def meter(percent: float, ascii_only: bool = False) -> str:
    return framed_meter("DISK", percent, 24, ascii_only)


def framed_meter(label: str, percent: float, width: int = 24, ascii_only: bool = False) -> str:
    cells = max(4, width)
    return f"{label} [{bar(percent, 100, ascii_only, cells)}] {percent:5.1f}%"


def bar(value: float, max_value: float, ascii_only: bool = False, cells: int = 16) -> str:
    active = max(0, min(cells, round((value / max_value) * cells) if max_value else 0))
    on = "#" if ascii_only else SYMBOLS["meter"]
    off = "." if ascii_only else SYMBOLS["meter_bg"]
    return on * active + off * (cells - active)


def sparkline(values: Sequence[int], width: int, ascii_only: bool = False) -> str:
    clean = [max(0, int(value)) for value in values if value is not None]
    if not clean:
        return "-" * max(1, width) if ascii_only else "·" * max(1, width)
    glyphs = "._-*#@" if ascii_only else BRAILLE_UP
    max_value = max(1, max(clean))
    chars = [glyphs[min(len(glyphs) - 1, round((value / max_value) * (len(glyphs) - 1)))] for value in clean]
    text = "".join(chars)
    if len(text) >= width:
        return text[-width:]
    pad = "." if ascii_only else "·"
    return text + pad * (width - len(text))


def split_width(width: int, left_ratio: float) -> tuple[int, int]:
    left = max(24, min(width - 25, round(width * left_ratio)))
    return left, max(1, width - left - 1)


def join_columns(columns: tuple[list[str], ...], width: int) -> list[str]:
    height = max((len(column) for column in columns), default=0)
    rows = []
    for index in range(height):
        parts = []
        for column in columns:
            column_width = len(column[0]) if column else 0
            parts.append(column[index] if index < len(column) else " " * column_width)
        rows.append(fit(" ".join(parts), width))
    return rows


def size_of(entry: dict[str, object] | None) -> int:
    if not isinstance(entry, dict):
        return 0
    try:
        return int(entry.get("size_bytes") or 0)
    except (TypeError, ValueError):
        return 0


def number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def format_bytes(value: object) -> str:
    size = number(value)
    units = ("B", "KB", "MB", "GB", "TB")
    unit = 0
    while abs(size) >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    precision = 0 if unit == 0 or abs(size) >= 10 else 1
    return f"{size:.{precision}f}{units[unit]}"


def compact_path(value: object, width: int) -> str:
    text = str(value or "")
    if not text:
        return ""
    home = os.path.expanduser("~")
    if home and text.startswith(home):
        text = "~" + text[len(home) :]
    if len(text) <= width:
        return text

    parts = text.split("/")
    if len(parts) >= 6:
        head = "/".join(parts[:3])
        tail = "/".join(parts[-3:])
        candidate = f"{head}/.../{tail}"
        if len(candidate) <= width:
            return candidate
    if width <= 4:
        return text[:width]
    head_width = max(1, width // 2 - 2)
    tail_width = max(1, width - head_width - 3)
    return f"{text[:head_width]}...{text[-tail_width:]}"


def short_time(value: object) -> str:
    text = str(value or "")
    if "T" in text:
        date, rest = text.split("T", 1)
        return f"{date} {rest[:5]}"
    return text or "never"


def fit(text: str, width: int) -> str:
    if len(text) <= width:
        return text.ljust(width)
    return text[: max(0, width - 1)] + "~"


def pad_rows(rows: list[str], width: int, height: int) -> list[str]:
    fitted = [fit(row, width) for row in rows[:height]]
    fitted.extend(" " * width for _ in range(height - len(fitted)))
    return fitted


def center(text: str, width: int) -> str:
    return fit(text.center(width), width)


def panel(title: str, rows: list[str], width: int, height: int) -> list[str]:
    if height <= 0:
        return []
    if width < 8 or height < 3:
        return [fit(row, width) for row in rows[:height]]
    top = titled_border(width, title, top=True)
    bottom = titled_border(width, "", top=False)
    body_height = max(0, height - 2)
    body = [f"{SYMBOLS['v']}{fit(row, width - 2)}{SYMBOLS['v']}" for row in rows[:body_height]]
    body.extend(SYMBOLS["v"] + " " * (width - 2) + SYMBOLS["v"] for _ in range(body_height - len(body)))
    return [top] + body + ([bottom] if height > 1 else [])


def chrome_bar(text: str, width: int, kind: str) -> str:
    if width < 8:
        return fit(text, width)
    if kind == "top":
        return titled_border(width, text.strip(), top=True)
    body = fit(f" {text.strip()} ", width - 2)
    return SYMBOLS["v"] + body + SYMBOLS["v"]


def titled_border(width: int, title: str, top: bool) -> str:
    if width < 8:
        return " " * max(0, width)
    left = SYMBOLS["lu"] if top else SYMBOLS["ld"]
    right = SYMBOLS["ru"] if top else SYMBOLS["rd"]
    line = [left] + [SYMBOLS["h"]] * (width - 2) + [right]
    if title and width >= 12:
        title_text = f"{SYMBOLS['title_left']} {fit(title, max(1, width - 8)).rstrip()} {SYMBOLS['title_right']}"
        if len(title_text) > width - 4:
            title_text = title_text[: max(0, width - 5)] + SYMBOLS["title_right"]
        start = 2
        for index, char in enumerate(title_text[: width - start - 1]):
            line[start + index] = char
    return "".join(line)


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def dedupe(values: list[str]) -> list[str]:
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
