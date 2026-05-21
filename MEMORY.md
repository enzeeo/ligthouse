## Decisions

### 2026-05-21 - Task 1 Storage Dashboard Skeleton
- Decided: Build a local-only, read-only storage dashboard.
- Decided: Use a Python standard library backend and vanilla HTML/CSS/JS frontend.
- Decided: Keep safe default scan roots exactly `~/Downloads`, `~/Desktop`, `~/Documents`, `~/Movies`, and `~/Pictures`.
- Decided: Deny risky roots by default: `/System`, `/bin`, `/sbin`, `/usr`, `/private`, `/Library`, and `/Applications`.
- Decided: Skip symlinks by default.
- Decided: Persist exact local paths only in local user state.
- Decided: Do not include delete behavior in v1.
- Decided: Use a sparse, btop-style clickable UI with a reduced palette.
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
