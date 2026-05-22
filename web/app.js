const API = {
  disk: "/api/disk",
  scan: "/api/scan",
  status: "/api/scan/status",
  results: "/api/scan/results",
  suggestions: "/api/suggestions",
  export: "/api/export",
};

const state = {
  activeTab: "overview",
  disk: null,
  status: { status: "idle" },
  snapshot: null,
  entries: [],
  logs: [],
  suggestions: [],
  selectedPath: "",
  history: loadHistory(),
  scanFrame: 0,
};

let scanStatusTimer = null;
let scanStatusRequestRunning = false;

const screens = Object.fromEntries(
  Array.from(document.querySelectorAll(".screen")).map((screen) => [screen.id, screen])
);
const message = document.querySelector("#message");
const scanButton = document.querySelector("#scan-button");
const exportButton = document.querySelector("#export-button");

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    setActiveTab(tab.dataset.tab);
  });
});

scanButton.addEventListener("click", runScan);
exportButton.addEventListener("click", exportReport);

init();

async function init() {
  render();
  await refreshAll();
}

async function refreshAll() {
  const [disk, status, results, suggestions] = await Promise.allSettled([
    getJson(API.disk),
    getJson(API.status),
    getJson(API.results),
    getJson(API.suggestions),
  ]);

  if (disk.status === "fulfilled") {
    state.disk = disk.value;
  }
  if (status.status === "fulfilled") {
    state.status = { ...scanProgressDefaults(), ...status.value };
  }
  if (results.status === "fulfilled") {
    applyResults(results.value);
  }
  if (suggestions.status === "fulfilled") {
    state.suggestions = Array.isArray(suggestions.value.suggestions) ? suggestions.value.suggestions : [];
    if (!state.selectedPath && state.suggestions[0]) {
      state.selectedPath = state.suggestions[0].path || "";
    }
  }

  if (results.status === "rejected") {
    state.snapshot = null;
    state.entries = [];
    state.logs = [];
  }
  const failed = [
    disk.status === "rejected" ? "disk" : "",
    status.status === "rejected" ? "status" : "",
    results.status === "rejected" ? "results" : "",
    suggestions.status === "rejected" ? "suggestions" : "",
  ].filter(Boolean);
  if (failed.length) {
    setNotice(`Partial load failed: ${failed.join(", ")}.`);
  }
  render();
}

async function runScan() {
  setNotice("Scan running. Read-only metadata only.");
  scanButton.disabled = true;
  state.status = {
    ...scanProgressDefaults(),
    ...state.status,
    status: "running",
    phase: "starting",
    started_at: new Date().toISOString(),
    finished_at: null,
  };
  startScanStatusPolling();
  render();

  try {
    const payload = await getJson(API.scan, { method: "POST" });
    if (payload.snapshot) {
      applyResults({ snapshot: payload.snapshot, entries: payload.snapshot.entries, logs: payload.snapshot.logs });
    }
    await refreshAll();
    setNotice("Scan complete.");
  } catch (error) {
    state.status = { ...scanProgressDefaults(), ...state.status, status: "failed", phase: "failed", error: "scan_failed" };
    setNotice(`Scan failed: ${error.message}`);
    render();
  } finally {
    stopScanStatusPolling();
    syncScanControls();
  }
}

function startScanStatusPolling() {
  stopScanStatusPolling();
  scanStatusTimer = window.setInterval(refreshScanStatus, 350);
}

function stopScanStatusPolling() {
  if (scanStatusTimer !== null) {
    window.clearInterval(scanStatusTimer);
    scanStatusTimer = null;
  }
}

async function refreshScanStatus() {
  if (scanStatusRequestRunning) {
    return;
  }
  scanStatusRequestRunning = true;
  try {
    const status = await getJson(API.status);
    state.status = status;
    state.scanFrame += 1;
    render();
  } catch (error) {
    setNotice(`Scan status unavailable: ${error.message}`);
  } finally {
    scanStatusRequestRunning = false;
  }
}

async function exportReport() {
  try {
    const payload = await getJson(API.export);
    const blob = new Blob([JSON.stringify(payload.report || payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `lighthouse-export-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setNotice("Export downloaded locally.");
  } catch (error) {
    setNotice(`Export unavailable: ${error.message}`);
  }
}

async function copyPath(path) {
  if (!path) {
    return;
  }
  if (!navigator.clipboard) {
    setNotice("Clipboard unavailable in this browser.");
    return;
  }
  try {
    await navigator.clipboard.writeText(path);
    setNotice("Path copied.");
  } catch (error) {
    setNotice(`Copy failed: ${error.message}`);
  }
}

async function getJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(String(payload.error || response.statusText || "request_failed"));
  }
  return payload;
}

function applyResults(payload) {
  const snapshot = payload.snapshot || null;
  state.snapshot = snapshot;
  state.entries = Array.isArray(payload.entries) ? payload.entries : entriesFromSnapshot(snapshot);
  state.logs = Array.isArray(payload.logs) ? payload.logs : logsFromSnapshot(snapshot);
  if (!state.selectedPath && state.suggestions[0]) {
    state.selectedPath = state.suggestions[0].path || "";
  }
  rememberSnapshot(snapshot);
}

function render() {
  Object.entries(screens).forEach(([name, screen]) => {
    screen.classList.toggle("active", name === state.activeTab);
  });
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === state.activeTab);
  });

  screens.overview.innerHTML = renderOverview();
  screens.review.innerHTML = renderReview();
  screens.files.innerHTML = renderFiles();
  screens.folders.innerHTML = renderFolders();
  screens.growth.innerHTML = renderGrowth();
  screens.log.innerHTML = renderLog();
  bindRenderedEvents();
  syncScanControls();
}

function renderOverview() {
  const diskPercent = number(state.disk && state.disk.percent);
  const roots = rootSummaries().slice(0, 3);
  const status = state.status.status || "idle";
  return `
    <article class="panel">
      <div class="panel-head"><h2>Disk</h2><span>${formatBytes(state.disk && state.disk.free)} free</span></div>
      <div class="panel-body stack">
        ${meter("Local disk", diskPercent, `${diskPercent.toFixed(1)}% used`, "cyan")}
        <div class="meta">Used ${formatBytes(state.disk && state.disk.used)} / ${formatBytes(state.disk && state.disk.total)}</div>
      </div>
    </article>
    <article class="panel">
      <div class="panel-head"><h2>Scan Status</h2><span class="${statusClass(status)}">${escapeHtml(status.toUpperCase())}</span></div>
      <div class="panel-body stack">
        <div class="row">
          <div><div class="path">Latest snapshot</div><div class="reason">${escapeHtml(snapshotTime())}</div></div>
          <div class="value">${state.entries.length} rows</div>
        </div>
        <div class="row">
          <div><div class="path">Safe review candidates</div><div class="reason">Safe + caution only</div></div>
          <div class="value">${state.suggestions.length}</div>
        </div>
        <div class="row">
          <div><div class="path">Log events</div><div class="reason">Denied, symlink, permission skips</div></div>
          <div class="value">${state.logs.length}</div>
        </div>
      </div>
    </article>
    <article class="panel wide">
      <div class="panel-head"><h2>Roots</h2><span>top 3 by scanned bytes</span></div>
      <div class="panel-body stack">
        ${roots.length ? roots.map((root) => meter(root.path, root.percent, formatEntrySize(root), "green")).join("") : empty("No scan yet. Run Scan to populate root meters.")}
      </div>
    </article>
    ${renderScanDebug(roots)}
  `;
}

function renderReview() {
  const items = state.suggestions.slice(0, 12);
  const selected = selectedSuggestion(items);
  return `
    <article class="panel">
      <div class="panel-head"><h2>Review Queue</h2><span>safe/caution</span></div>
      <div class="panel-body">
        ${items.length ? `<div class="row-list">${items.map(renderSuggestionRow).join("")}</div>` : empty("No safe or caution suggestions yet.")}
      </div>
    </article>
    <article class="panel">
      <div class="panel-head"><h2>Selected Item</h2><span>${escapeHtml(selected ? selected.classification : "none")}</span></div>
      <div class="panel-body">
        ${selected ? renderDetail(selected) : empty("Select a review row to inspect path, size, risk, and reason.")}
      </div>
    </article>
  `;
}

function renderFiles() {
  const allFiles = state.entries.filter((entry) => entry.kind === "file").sort(bySizeDesc);
  const files = allFiles.slice(0, 24);
  const largest = files[0] || null;
  const shownTotal = files.reduce((sum, entry) => sum + sizeOf(entry), 0);
  return `
    <article class="panel">
      <div class="panel-head"><h2>Largest Files</h2><span>${files.length} shown</span></div>
      <div class="panel-body">
        ${files.length ? `<div class="row-list">${files.map(renderEntryRow).join("")}</div>` : empty("No file rows available. Run Scan first.")}
      </div>
    </article>
    <article class="panel">
      <div class="panel-head"><h2>File Summary</h2><span>top ${files.length}: ${formatBytes(shownTotal)}</span></div>
      <div class="panel-body stack">
        ${largest ? renderDetail({
          path: largest.path,
          kind: largest.kind,
          size_bytes: largest.size_bytes,
          classification: largest.classification,
          risk: largest.risk,
          reason: largest.reason,
        }) : empty("No file summary yet.")}
      </div>
    </article>
  `;
}

function renderFolders() {
  const folders = state.entries.filter((entry) => entry.kind === "folder").sort(bySizeDesc).slice(0, 24);
  const max = Math.max(1, ...folders.map((entry) => sizeOf(entry)));
  const largest = folders[0] || null;
  return `
    <article class="panel">
      <div class="panel-head"><h2>Largest Folders</h2><span>${folders.length} shown</span></div>
      <div class="panel-body">
        ${
          folders.length
            ? `<div class="row-list">${folders.map((entry) => renderFolderRow(entry, max)).join("")}</div>`
            : empty("No folder rows available. Run Scan first.")
        }
      </div>
    </article>
    <article class="panel">
      <div class="panel-head"><h2>Folder Focus</h2><span>${largest ? formatBytes(largest.size_bytes) : "empty"}</span></div>
      <div class="panel-body stack">
        ${
          largest
            ? `${meter(largest.path || "largest folder", 100, formatBytes(largest.size_bytes), "cyan")}${renderDetail({
                path: largest.path,
                kind: largest.kind,
                size_bytes: largest.size_bytes,
                classification: largest.classification,
                risk: largest.risk,
                reason: largest.reason,
              })}`
            : empty("No folder focus yet.")
        }
      </div>
    </article>
  `;
}

function renderGrowth() {
  const history = state.history.slice(-10);
  if (history.length < 2) {
    return `
      <article class="panel">
        <div class="panel-head"><h2>Growth</h2><span>needs 2 snapshots</span></div>
        <div class="panel-body">${empty("Not enough local snapshot history yet. Run another scan later to compare growth.")}</div>
      </article>
      <article class="panel">
        <div class="panel-head"><h2>Snapshot</h2><span>${history.length} local sample${history.length === 1 ? "" : "s"}</span></div>
        <div class="panel-body stack">
          ${meter("Latest scanned total", history[0] ? 100 : 0, history[0] ? formatBytes(history[0].totalBytes) : "-", "cyan")}
          <div class="row"><div><div class="path">Latest</div><div class="reason">${history[0] ? escapeHtml(history[0].timestamp) : "No local history"}</div></div><div class="value">${history[0] ? formatBytes(history[0].totalBytes) : "-"}</div></div>
        </div>
      </article>
    `;
  }

  const max = Math.max(...history.map((item) => item.totalBytes), 1);
  const latest = history[history.length - 1];
  const previous = history[history.length - 2];
  const delta = latest.totalBytes - previous.totalBytes;
  return `
    <article class="panel">
      <div class="panel-head"><h2>Trend</h2><span>${history.length} local samples</span></div>
      <div class="panel-body">
        ${tuiTrend(history, max)}
      </div>
    </article>
    <article class="panel">
      <div class="panel-head"><h2>Delta</h2><span class="${delta >= 0 ? "warn" : "ok"}">${delta >= 0 ? "+" : ""}${formatBytes(delta)}</span></div>
      <div class="panel-body stack">
        ${meter("Latest scanned total", (latest.totalBytes / max) * 100, formatBytes(latest.totalBytes), "cyan")}
        <div class="row"><div><div class="path">Previous</div><div class="reason">${escapeHtml(previous.timestamp)}</div></div><div class="value">${formatBytes(previous.totalBytes)}</div></div>
      </div>
    </article>
  `;
}

function renderLog() {
  const events = state.logs.filter((log) => /denied|symlink|permission|error/i.test(`${log.event || ""} ${log.message || ""}`));
  const visible = (events.length ? events : state.logs).slice(0, 24);
  const counts = logCounts(state.logs);
  return `
    <article class="panel">
      <div class="panel-head"><h2>Log</h2><span>denied / symlink / permission</span></div>
      <div class="panel-body">
        ${visible.length ? `<div class="row-list">${visible.map(renderLogRow).join("")}</div>` : empty("No denied paths, symlink skips, or permission errors recorded.")}
      </div>
    </article>
    <article class="panel">
      <div class="panel-head"><h2>Log Mix</h2><span>${state.logs.length} events</span></div>
      <div class="panel-body stack">
        ${meter("Denied", counts.denied, `${counts.deniedCount} events`, "red")}
        ${meter("Symlink", counts.symlink, `${counts.symlinkCount} events`, "amber")}
        ${meter("Permission", counts.permission, `${counts.permissionCount} events`, "amber")}
      </div>
    </article>
  `;
}

function renderSuggestionRow(item) {
  const path = item.path || "";
  const selected = path === state.selectedPath ? " selected" : "";
  return `
    <button class="row${selected}" type="button" data-select-path="${escapeAttr(path)}">
      <div>
        <div class="path">${escapeHtml(path || "unknown path")}</div>
        <div class="reason ${riskClass(item.classification)}">${escapeHtml(item.reason || "No reason supplied")}</div>
      </div>
      <div class="value">${formatEntrySize(item)}</div>
    </button>
  `;
}

function renderEntryRow(entry) {
  return `
    <div class="row">
      <div>
        <div class="path">${escapeHtml(entry.path || "unknown path")}</div>
        <div class="reason ${riskClass(entry.classification)}">${escapeHtml(entry.classification || "unknown")} | ${escapeHtml(entry.reason || "No reason supplied")}</div>
      </div>
      <div class="value">${formatEntrySize(entry)}</div>
    </div>
  `;
}

function renderFolderRow(entry, max) {
  const percent = (sizeOf(entry) / max) * 100;
  const fileCount = Number.isFinite(Number(entry.file_count)) ? `${Number(entry.file_count)} files` : "files unknown";
  return `
    <div class="row">
      <div class="stack">
        <div>
          <div class="path">${escapeHtml(entry.path || "unknown path")}</div>
          <div class="reason">${escapeHtml(fileCount)} | ${escapeHtml(entry.reason || "No reason supplied")}</div>
        </div>
        ${meter("", percent, "", "cyan")}
      </div>
      <div class="value">${formatEntrySize(entry)}</div>
    </div>
  `;
}

function renderLogRow(log) {
  return `
    <div class="row">
      <div>
        <div class="path">${escapeHtml(log.path || "unknown path")}</div>
        <div class="reason">${escapeHtml(log.message || "No message")}</div>
      </div>
      <div class="value ${/denied|error|permission/i.test(log.event || "") ? "bad" : "warn"}">${escapeHtml(log.event || "event")}</div>
    </div>
  `;
}

function renderDetail(item) {
  return `
    <div class="detail">
      <div class="detail-path">${escapeHtml(item.path || "unknown path")}</div>
      <div class="detail-grid">
        <div class="cell-muted">Size</div><div>${formatEntrySize(item)}</div>
        <div class="cell-muted">Kind</div><div>${escapeHtml(item.kind || "unknown")}</div>
        <div class="cell-muted">Class</div><div class="${riskClass(item.classification)}">${escapeHtml(item.classification || "unknown")}</div>
        <div class="cell-muted">Risk</div><div class="${riskClass(item.risk)}">${escapeHtml(item.risk || "unknown")}</div>
        <div class="cell-muted">Reason</div><div>${escapeHtml(item.reason || "No reason supplied")}</div>
      </div>
      <div class="detail-actions">
        <button class="button secondary" type="button" data-copy-path="${escapeAttr(item.path || "")}">Copy Path</button>
        <button class="button" type="button" disabled title="No approved read-only reveal endpoint exists">Reveal in Finder unavailable</button>
      </div>
    </div>
  `;
}

function renderScanDebug(roots) {
  const status = { ...scanProgressDefaults(), ...(state.status || {}) };
  const scanStatus = String(status.status || "idle");
  const activePath = status.active_path || status.active_root || "waiting for scan";
  const phase = status.phase || scanStatus;
  const rootRows = scanDebugRoots(status, roots);
  const recentLogs = state.logs.slice(-4).reverse();
  return `
    <article class="panel wide scan-debug">
      <div class="panel-head">
        <h2>Scan Debug</h2>
        <span class="${statusClass(scanStatus)}">${escapeHtml(scanStatus.toUpperCase())}</span>
      </div>
      <div class="panel-body stack">
        <div class="debug-toolbar">
          <button class="button primary" type="button" data-run-scan ${scanStatus === "running" ? "disabled" : ""}>Scan</button>
          <span>${escapeHtml(String(phase).toUpperCase())}</span>
        </div>
        <div class="debug-line">
          <span class="debug-label">active</span>
          <span class="scan-marker ${scanStatus === "running" ? "running" : ""}">${escapeHtml(scanMarker(scanStatus))}</span>
          <span class="debug-path">${escapeHtml(activePath)}</span>
        </div>
        <div class="debug-grid">
          <div><span class="debug-label">entries</span><strong>${number(status.entries_seen)}</strong></div>
          <div><span class="debug-label">events</span><strong>${number(status.logs_seen)}</strong></div>
          <div><span class="debug-label">started</span><strong>${escapeHtml(shortTime(status.started_at))}</strong></div>
          <div><span class="debug-label">finished</span><strong>${escapeHtml(shortTime(status.finished_at))}</strong></div>
        </div>
        <div class="debug-roots">
          ${rootRows.length ? rootRows.map(renderScanRootRow).join("") : empty("No roots queued yet. Press Scan to start a debug trace.")}
        </div>
        ${
          recentLogs.length
            ? `<div class="debug-events">${recentLogs.map((log) => `<div><span>${escapeHtml(log.event || "event")}</span><span>${escapeHtml(log.path || "")}</span></div>`).join("")}</div>`
            : ""
        }
      </div>
    </article>
  `;
}

function renderScanRootRow(row) {
  return `
    <div class="debug-root ${escapeAttr(row.state)}">
      <span>${escapeHtml(row.marker)}</span>
      <span>${escapeHtml(row.path)}</span>
      <span>${escapeHtml(row.state)}</span>
    </div>
  `;
}

function meter(label, percent, value, color) {
  const safePercent = Math.max(0, Math.min(100, Number.isFinite(percent) ? percent : 0));
  const active = Math.round((safePercent / 100) * 20);
  const bar = `${"█".repeat(active)}${"·".repeat(20 - active)}`;
  return `
    <div class="meter">
      ${label || value ? `<div class="meter-top"><span>${escapeHtml(label)}</span><span>${escapeHtml(value)}</span></div>` : ""}
      <div class="tui-meter ${escapeAttr(color)}" aria-label="${escapeAttr(label)} meter">
        <span>[</span><span>${bar}</span><span>]</span>
      </div>
    </div>
  `;
}

function tuiTrend(history, max) {
  return `
    <div class="tui-bars">
      ${history
        .map((item) => {
          const active = Math.max(1, Math.round((item.totalBytes / max) * 8));
          const cells = Array.from({ length: 8 }, (_, index) => {
            const on = index >= 8 - active;
            return `<span class="${on ? "on" : ""}">${on ? "█" : "·"}</span>`;
          }).join("");
          return `<div class="tui-vbar" title="${escapeAttr(item.timestamp)}">${cells}</div>`;
        })
        .join("")}
    </div>
  `;
}

function empty(text) {
  return `<div class="empty">${escapeHtml(text)}</div>`;
}

function bindRenderedEvents() {
  document.querySelectorAll("[data-run-scan]").forEach((button) => {
    button.addEventListener("click", runScan);
  });
  document.querySelectorAll("[data-select-path]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedPath = button.dataset.selectPath || "";
      render();
    });
  });
  document.querySelectorAll("[data-copy-path]").forEach((button) => {
    button.addEventListener("click", () => copyPath(button.dataset.copyPath || ""));
  });
}

function syncScanControls() {
  const running = state.status && state.status.status === "running";
  scanButton.disabled = running;
  document.querySelectorAll("[data-run-scan]").forEach((button) => {
    button.disabled = running;
  });
}

function setActiveTab(name) {
  state.activeTab = name;
  render();
}

function setNotice(text) {
  message.textContent = text;
}

function selectedSuggestion(items) {
  return items.find((item) => item.path === state.selectedPath) || items[0] || null;
}

function rootSummaries() {
  const roots = Array.isArray(state.snapshot && state.snapshot.roots) ? state.snapshot.roots : [];
  const totals = roots.map((root) => {
    const rootEntry = state.entries.find((entry) => entry.path === root && entry.kind === "folder");
    const size = rootEntry
      ? sizeOf(rootEntry)
      : state.entries
          .filter((entry) => entry.root === root && entry.kind === "file")
          .reduce((sum, entry) => sum + sizeOf(entry), 0);
    return { path: root, size, size_bytes: size, size_status: rootEntry ? rootEntry.size_status : "partial" };
  });
  const max = Math.max(1, ...totals.map((root) => root.size));
  return totals
    .map((root) => ({ ...root, percent: (root.size / max) * 100 }))
    .sort((a, b) => b.size - a.size);
}

function entriesFromSnapshot(snapshot) {
  return snapshot && Array.isArray(snapshot.entries) ? snapshot.entries : [];
}

function logsFromSnapshot(snapshot) {
  return snapshot && Array.isArray(snapshot.logs) ? snapshot.logs : [];
}

function logCounts(logs) {
  const total = Math.max(1, logs.length);
  const deniedCount = logs.filter((log) => /denied/i.test(log.event || "")).length;
  const symlinkCount = logs.filter((log) => /symlink/i.test(log.event || "")).length;
  const permissionCount = logs.filter((log) => /permission/i.test(log.event || "")).length;
  return {
    denied: (deniedCount / total) * 100,
    deniedCount,
    symlink: (symlinkCount / total) * 100,
    symlinkCount,
    permission: (permissionCount / total) * 100,
    permissionCount,
  };
}

function scanDebugRoots(status, roots) {
  const completed = new Set(arrayOfStrings(status.completed_roots));
  const pending = arrayOfStrings(status.pending_roots);
  const activeRoot = String(status.active_root || "");
  const ordered = [];
  [...completed, activeRoot, ...pending, ...roots.map((root) => root.path)].forEach((path) => {
    if (path && !ordered.includes(path)) {
      ordered.push(path);
    }
  });
  return ordered.map((path) => {
    if (path === activeRoot && status.status === "running") {
      return { path, state: "scanning", marker: scanMarker("running") };
    }
    if (completed.has(path)) {
      return { path, state: "done", marker: "█████" };
    }
    return { path, state: "queued", marker: "-----" };
  });
}

function scanMarker(status) {
  if (status !== "running") {
    return "-----";
  }
  return ["-----", "\\\\\\\\\\", "|||||", "/////"][state.scanFrame % 4];
}

function scanProgressDefaults() {
  return {
    phase: "idle",
    active_root: null,
    active_path: null,
    completed_roots: [],
    pending_roots: [],
    entries_seen: 0,
    logs_seen: 0,
    started_at: null,
    finished_at: null,
  };
}

function arrayOfStrings(value) {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function shortTime(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function rememberSnapshot(snapshot) {
  if (!snapshot || !snapshot.id) {
    return;
  }
  const totalBytes = rootTotalBytes(entriesFromSnapshot(snapshot), snapshot);
  const item = {
    id: snapshot.id,
    timestamp: snapshot.timestamp || snapshot.id,
    totalBytes,
  };
  state.history = [...state.history.filter((entry) => entry.id !== item.id), item].slice(-10);
  localStorage.setItem("lighthouse-snapshot-history", JSON.stringify(state.history));
}

function rootTotalBytes(entries, snapshot) {
  const roots = Array.isArray(snapshot && snapshot.roots) ? snapshot.roots : [];
  const rootEntries = entries.filter((entry) => roots.includes(entry.path) && entry.kind === "folder");
  if (rootEntries.length) {
    return rootEntries.reduce((sum, entry) => sum + sizeOf(entry), 0);
  }
  return entries.filter((entry) => entry.kind === "file").reduce((sum, entry) => sum + sizeOf(entry), 0);
}

function loadHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem("lighthouse-snapshot-history") || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function snapshotTime() {
  const snapshot = state.snapshot || {};
  const latest = (state.status.latest_snapshot && state.status.latest_snapshot.timestamp) || snapshot.timestamp;
  return latest || "No scan yet";
}

function statusClass(status) {
  if (status === "completed" || status === "idle") {
    return "ok";
  }
  if (status === "failed") {
    return "bad";
  }
  return "warn";
}

function riskClass(value) {
  const token = String(value || "")
    .toLowerCase()
    .replaceAll(" ", "_")
    .replace(/[^a-z0-9_-]/g, "");
  return `risk-${token}`;
}

function bySizeDesc(a, b) {
  return sizeOf(b) - sizeOf(a);
}

function sizeOf(entry) {
  return Number(entry && entry.size_bytes) || 0;
}

function formatEntrySize(entry) {
  const status = entry && typeof entry.size_status === "string" ? entry.size_status : "exact";
  const suffix = status === "partial" || status === "cached" ? ` ${status}` : "";
  return `${formatBytes(entry && (entry.size_bytes ?? entry.size))}${suffix}`;
}

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes)) {
    return "-";
  }
  const sign = bytes < 0 ? "-" : "";
  let size = Math.abs(bytes);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const precision = unit === 0 || size >= 10 ? 0 : 1;
  return `${sign}${size.toFixed(precision)} ${units[unit]}`;
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}
