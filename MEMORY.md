## Decisions

### 2026-05-21 - Task 1 Storage Dashboard Skeleton
- Decided: Build a local-only, read-only storage dashboard.
- Decided: Use a Python standard library backend and vanilla HTML/CSS/JS frontend.
- Decided: Keep safe default scan roots exactly `~/Downloads`, `~/Desktop`, `~/Documents`, `~/Movies`, and `~/Pictures`.
- Decided: Deny risky roots by default: `/System`, `/bin`, `/sbin`, `/usr`, `/private`, `/Library`, and `/Applications`.
- Decided: Skip symlinks by default.
- Decided: Persist exact local paths only in local user state.
- Decided: Do not include delete behavior in v1.
- Decided: Use a sparse, btop-style keyboard-only UI with a reduced palette.
- Why: The app is intended for local storage visibility with conservative filesystem safety and no external runtime dependencies.
- Rejected: External package dependencies, npm tooling, CDNs, external fonts, destructive file actions, and broad filesystem traversal defaults.

### 2026-05-21 - Task 4+6 Local API And Snapshots
- Decided: Expose only fixed local JSON endpoints under `/api/*`; unknown API paths return JSON 404 and static serving remains the fallback for non-API paths.
- Decided: Keep scans synchronous inside `ThreadingHTTPServer`; this preserves simple stdlib behavior while other requests can still be served by other threads.
- Decided: Persist latest local snapshots in `~/.storage-dashboard/dashboard.json`, bounded to 10 by default, with timestamp, roots, disk summary, entries, and logs.
- Decided: Guard snapshot load/latest/add with a store-level lock, fail closed on corrupt state JSON, and write snapshots through same-directory temp files followed by atomic replace.
- Decided: Generate exports only from the latest local snapshot; no arbitrary file reads or network/cloud behavior.
- Why: Meets local-only API/export needs without adding dependencies or expanding filesystem access.

### 2026-05-21 - Frontend Safety And Visual Rules
- Decided: Keep `Copy Path` and `Export Report` enabled because they are read-only local actions from the approved plan.
- Decided: Keep `Reveal in Finder` disabled in v1 because no explicit read-only reveal endpoint was approved.
- Decided: Escape API-backed and localStorage-backed strings before inserting into `innerHTML`; sanitize CSS class tokens derived from data.
- Decided: Use only local `styles.css` and `app.js`; no external assets, CDNs, fonts, npm, or build tooling.
- Decided: Keep the dashboard sparse with 2-3 panels per tab, row lists that scroll after a few rows, and single-hue light-to-dark meter gradients.
- Why: Preserves the safety-first local review model while matching the requested btop/TUI visual direction.
- Rejected: Purple palette in v1, destructive UI/API controls, broad browser automation side effects, and a Finder reveal endpoint without a follow-up approval.

### 2026-05-21 - Exact Loopback Binding
- Decided: `create_server()` accepts only the exact host `127.0.0.1`; it rejects `localhost`, `::1`, `0.0.0.0`, and other hosts.
- Why: The implementation plan required binding to `127.0.0.1` only, not broader loopback aliases.
- Rejected: Treating `localhost` or IPv6 loopback as equivalent in v1.

### 2026-05-21 - Lighthouse TUI Default
- Decided: Add `lighthouse = storage_dashboard.cli:main`; default command opens the curses TUI, while `lighthouse web` keeps the browser dashboard.
- Decided: Share scan/status/snapshot behavior through `ScanRuntime` with schema version, roots fingerprint, and an advisory scan lock.
- Decided: Keep the TUI stdlib-only, read-only, keyboard-first, and backed by compatible cached snapshots plus background refresh when stale.
- Why: Gives the requested btop-style terminal app without adding dependencies or duplicating scanner/store behavior.
- Rejected: Constant scanning, destructive file actions, Windows TUI support in v1, and a separate growth data store.

### 2026-05-21 - Btop-Style TUI Scan Debugger
- Decided: Evolve the curses TUI toward richer btop-inspired panels, color accents, responsive overview/debug layouts, and first-scan states while keeping `LIGHTHOUSE` as the title.
- Decided: Track configured runtime roots in `TuiState` and render a scan debugger using `completed_roots`, `active_root`, `active_path`, and `pending_roots`.
- Decided: Keep tests focused on text markers and bounds instead of terminal color escape behavior.
- Why: The live UI needs clearer scan progress without adding dependencies or changing scan/runtime public APIs.

### 2026-05-21 - Keyboard-Only TUI Input
- Decided: Disable curses mouse mode, remove mouse event routing, and keep tab/row navigation keyboard-only.
- Decided: Treat terminal mouse events as no-ops when surfaced by a test double.
- Why: Clicking in the terminal caused crashes/focus traps; keyboard controls are enough for v1 and safer across terminals.
- Rejected: Clickable tabs, mouse row selection, mouse scroll handling, and curses cursor/mouse setup.

### 2026-05-22 - Slate/Oxblood Btop-Style TUI
- Decided: Redesign the curses TUI around the second mockup: dark slate panels, muted steel-blue borders, pale gray text, dusty-blue highlights, and oxblood/coral risk accents.
- Decided: Keep `render(state, width, height) -> list[str]`, stdlib-only curses, read-only behavior, keyboard navigation, and existing CLI/API contracts unchanged.
- Decided: Use best-effort curses background/color pairs with fallback for limited terminals; tests should assert text markers/layout behavior, not exact terminal RGB output.
- Why: User wants the TUI to mimic btop’s dense panel style while matching the Lighthouse palette more closely.
- Rejected: New TUI dependencies, destructive actions, web dashboard changes, and relying on exact truecolor support.

### 2026-05-22 - Battery-Aware Scanner
- Decided: Use best-effort macOS FSEvents metadata to skip full scans when no watched roots changed, and fall back to the full safe scanner whenever FSEvents is unavailable, dropped, wrapped, or root-changed.
- Decided: Incremental scans rescan only changed subtrees, merge them into the previous snapshot, recompute affected ancestor totals, and keep one global scan lock.
- Decided: Cap parallel root/subtree scanning at a small worker count, default 2, and slow idle TUI runtime polling.
- Why: Reduces repeated disk traversal and timer wakeups while preserving read-only, stdlib-only, local-only safety.
- Rejected: Always-fast uncapped scanning, external watcher dependencies, and destructive filesystem actions.

### 2026-05-22 - Staged Scanner Strategy
- Decided: Add exact, summary, and lazy scan modes with `size_status` metadata so the UI can distinguish exact, cached, and partial byte totals.
- Decided: Publish a partial summary snapshot before long exact scans when the scanner supports options, but never treat partial snapshots as compatible/fresh for reuse.
- Decided: Keep expensive folders collapsed by default, reuse cached exact totals for shallow display, and rescan a collapsed folder when FSEvents reports a change inside it.
- Decided: Add `POST /api/scan/path` for read-only lazy subtree scans and throttle hot-path progress updates.
- Why: Improves perceived scan speed without switching languages or reading file contents.
- Rejected: C++ rewrite, destructive cleanup controls, and trusting cached/partial sizes as deletion-safe proof.
