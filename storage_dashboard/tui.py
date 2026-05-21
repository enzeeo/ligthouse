"""Curses terminal dashboard for Lighthouse."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
        self.screen = self.curses.initscr()
        self.curses.noecho()
        self.curses.cbreak()
        self.screen.keypad(True)
        self.screen.nodelay(True)
        self.curses.curs_set(0)
        setup_colors(self.curses)
        try:
            self.curses.mousemask(self.curses.ALL_MOUSE_EVENTS)
        except Exception:
            pass
        self._old_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _raise_keyboard_interrupt)
        return self.screen

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.screen is not None:
            try:
                self.screen.keypad(False)
            except Exception:
                pass
        self.curses.nocbreak()
        self.curses.echo()
        self.curses.endwin()
        if self._old_sigint is not None:
            signal.signal(signal.SIGINT, self._old_sigint)
        return False


def _raise_keyboard_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def _loop(screen, curses, runtime: ScanRuntime) -> None:
    state = load_state(runtime)
    while True:
        height, width = screen.getmaxyx()
        draw(screen, render(state, width, height), curses)
        key = screen.getch()
        if key != -1:
            action = map_key(key, curses)
            if action.name == "mouse":
                action = map_mouse(curses.getmouse(), width)
            if action.name == "quit":
                return
            if action.name == "refresh":
                start = runtime.start_background_scan()
                state = replace(state, notice="Scan already running." if start.status == "running" else "Scan started.")
            else:
                state = reduce_state(state, action, width, height)
        state = merge_runtime(state, runtime)
        time.sleep(0.05)


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
    if action.name == "mouse_tab" and action.value in TAB_ORDER:
        return replace(state, active_tab=str(action.value), scroll=0, selected_index=0)
    if action.name == "mouse_row":
        index = max(0, int(action.value or 0) + state.scroll)
        return replace(state, selected_index=index)
    if action.name == "mouse_scroll_down":
        return reduce_state(state, InputAction("down"), width, height)
    if action.name == "mouse_scroll_up":
        return reduce_state(state, InputAction("up"), width, height)
    return state


def map_key(key: int, curses_module) -> InputAction:
    if key in (ord("q"), ord("Q")):
        return InputAction("quit")
    if key in (ord("r"), ord("R")):
        return InputAction("refresh")
    if ord("1") <= key <= ord("6"):
        return InputAction("tab", TAB_ORDER[key - ord("1")])
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
    if key == getattr(curses_module, "KEY_MOUSE", -999):
        return InputAction("mouse")
    return InputAction("noop")


def map_mouse(mouse_event: tuple[int, int, int, int, int], width: int) -> InputAction:
    _id, x, y, _z, button = mouse_event
    if y == 1:
        tab_width = max(8, width // len(TAB_ORDER))
        index = min(len(TAB_ORDER) - 1, max(0, x // tab_width))
        return InputAction("mouse_tab", TAB_ORDER[index])
    if button & 0x80000:
        return InputAction("mouse_scroll_down")
    if button & 0x100000:
        return InputAction("mouse_scroll_up")
    if y >= 4:
        return InputAction("mouse_row", y - 4)
    return InputAction("noop")


def render(state: TuiState, width: int, height: int) -> list[str]:
    width = max(20, width)
    height = max(5, height)
    if not isinstance(state.snapshot, dict):
        rows = render_first_scan(state, width, height)
        return pad_rows(rows, width, height)

    rows = [
        fit("LIGHTHOUSE  " + status_text(state), width),
        render_tabs(state.active_tab, width),
        fit(state.notice or snapshot_text(state), width),
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
    return fit(" ".join(pieces), width)


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
    overview = overview_rows(state, width)
    debugger = debugger_rows(state)
    if available <= 6:
        return render_tiny(state, width)[:available]
    if width >= 96 and available >= 8:
        left_width = max(40, width // 2)
        right_width = width - left_width - 1
        left = panel("Overview", overview, left_width, available)
        right = panel("Scan Debugger", debugger, right_width, available)
        return [fit(f"{left_row} {right_row}", width) for left_row, right_row in zip(left, right)]
    rows = panel("Overview", overview, width, min(available, max(6, available // 2)))
    remaining = available - len(rows)
    if remaining > 0:
        rows.extend(panel("Scan Debugger", debugger, width, remaining))
    return rows


def render_active_tab(state: TuiState, width: int, available: int) -> list[str]:
    if state.active_tab == "overview":
        rows = overview_rows(state, width)
    elif state.active_tab == "review":
        rows = table_rows(suggestions_from_snapshot(state.snapshot), ("classification", "size_bytes", "path"))
    elif state.active_tab == "files":
        files = sorted((entry for entry in entries_from_state(state) if entry.get("kind") == "file"), key=size_of, reverse=True)
        rows = table_rows(files, ("size_bytes", "classification", "path"))
    elif state.active_tab == "folders":
        folders = sorted((entry for entry in entries_from_state(state) if entry.get("kind") == "folder"), key=size_of, reverse=True)
        rows = table_rows(folders, ("size_bytes", "file_count", "path"))
    elif state.active_tab == "growth":
        rows = growth_rows(state)
    else:
        rows = table_rows(logs_from_state(state), ("event", "message", "path"))
    visible = rows[state.scroll : state.scroll + max(1, available)]
    return mark_selected(visible, state.selected_index - state.scroll, width)


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
            marker = "done"
        elif root == active:
            marker = "scan"
        elif root in pending:
            marker = "wait"
        else:
            marker = "idle"
        rows.append(f"[{marker}] {root}")
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
        return f"Checking {target}"
    return "Scan idle"


def growth_rows(state: TuiState) -> list[str]:
    if len(state.history) < 2:
        return ["Not enough local snapshot history yet."]
    values = [(snapshot.get("timestamp", ""), total_bytes(snapshot)) for snapshot in state.history]
    max_value = max(1, *(value for _time, value in values))
    return [f"{short_time(time_value)} {bar(value, max_value, state.use_ascii)} {format_bytes(value)}" for time_value, value in values]


def table_rows(items: Sequence[dict[str, object]], fields: tuple[str, ...]) -> list[str]:
    if not items:
        return ["No rows."]
    rows = []
    for item in items:
        cells = []
        for field in fields:
            value = item.get(field)
            if field == "size_bytes":
                value = format_bytes(value)
            cells.append(str(value or ""))
        rows.append("  ".join(cells))
    return rows


def mark_selected(rows: list[str], selected: int, width: int) -> list[str]:
    marked = []
    for index, row in enumerate(rows):
        prefix = "> " if index == selected else "  "
        marked.append(fit(prefix + row, width))
    return marked


def draw(screen, rows: list[str], curses_module=None) -> None:
    screen.erase()
    for y, row in enumerate(rows):
        try:
            attr = row_attr(row, curses_module)
            if attr:
                screen.addstr(y, 0, row, attr)
            else:
                screen.addstr(y, 0, row)
        except Exception:
            try:
                screen.addstr(y, 0, row)
            except Exception:
                pass
    screen.refresh()


def setup_colors(curses_module) -> None:
    try:
        if not curses_module.has_colors():
            return
        curses_module.start_color()
        if hasattr(curses_module, "use_default_colors"):
            curses_module.use_default_colors()
        curses_module.init_pair(COLOR_PAIRS["title"], curses_module.COLOR_CYAN, -1)
        curses_module.init_pair(COLOR_PAIRS["accent"], curses_module.COLOR_BLUE, -1)
        curses_module.init_pair(COLOR_PAIRS["ok"], curses_module.COLOR_GREEN, -1)
        curses_module.init_pair(COLOR_PAIRS["warn"], curses_module.COLOR_YELLOW, -1)
        curses_module.init_pair(COLOR_PAIRS["muted"], curses_module.COLOR_WHITE, -1)
        curses_module.init_pair(COLOR_PAIRS["border"], curses_module.COLOR_MAGENTA, -1)
    except Exception:
        return


def row_attr(row: str, curses_module) -> int:
    if curses_module is None:
        return 0
    try:
        if row.startswith("LIGHTHOUSE") or row.strip() == "LIGHTHOUSE":
            return curses_module.color_pair(COLOR_PAIRS["title"]) | getattr(curses_module, "A_BOLD", 0)
        if row.startswith("+") or row.startswith("|"):
            return curses_module.color_pair(COLOR_PAIRS["border"])
        if "[done]" in row:
            return curses_module.color_pair(COLOR_PAIRS["ok"])
        if "[scan]" in row or "Checking " in row:
            return curses_module.color_pair(COLOR_PAIRS["title"])
        if "[wait]" in row:
            return curses_module.color_pair(COLOR_PAIRS["muted"])
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
    return bar(percent, 100, ascii_only, cells=24)


def bar(value: float, max_value: float, ascii_only: bool = False, cells: int = 16) -> str:
    active = max(0, min(cells, round((value / max_value) * cells) if max_value else 0))
    on = "#" if ascii_only else "█"
    off = "." if ascii_only else "·"
    return on * active + off * (cells - active)


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
    title_text = f" {title} "
    top = "+" + title_text[: max(0, width - 2)].ljust(width - 2, "-") + "+"
    bottom = "+" + "-" * (width - 2) + "+"
    body_height = max(0, height - 2)
    body = [f"|{fit(row, width - 2)}|" for row in rows[:body_height]]
    body.extend("|" + " " * (width - 2) + "|" for _ in range(body_height - len(body)))
    return [top] + body + ([bottom] if height > 1 else [])


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
