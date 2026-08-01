/* DICE frontend.
 *
 * The Dune API key lives only in the browser. Depending on the "Remember"
 * choice it goes to localStorage, sessionStorage, or nowhere at all (kept in a
 * variable for the lifetime of the page). It is attached to API calls as the
 * X-Dune-Api-Key header and is never put in a URL, so it cannot leak through
 * server access logs or the Referer header.
 */

const STORAGE_KEY = "dice.dune_api_key";
const STORAGE_PREF = "dice.dune_api_key_storage";

const $ = (id) => document.getElementById(id);

const state = {
  key: "",          // in-memory copy, always authoritative
  jobId: null,
  preview: [],
  summary: [],
  tab: "snapshots",
  config: null,       // /api/config payload (monitor caps, auto possibility)
  lastRun: null,      // last successful holders response (for save-as-watchlist)
  watchlists: [],
  signals: [],
  editingWatchlist: null,   // id with the settings editor open
  expandedSignals: new Set(),
};

/* ------------------------------------------------------------------ key I/O */

function storageFor(pref) {
  if (pref === "local") return window.localStorage;
  if (pref === "session") return window.sessionStorage;
  return null;
}

function loadKey() {
  const pref = window.localStorage.getItem(STORAGE_PREF) || "local";
  $("keyStorage").value = pref;
  const store = storageFor(pref);
  const saved = store ? store.getItem(STORAGE_KEY) : null;
  if (saved) {
    state.key = saved;
    $("apiKey").value = saved;
  }
  paintKeyPill(saved ? "set" : "none");
}

function saveKey() {
  const value = $("apiKey").value.trim();
  const pref = $("keyStorage").value;
  window.localStorage.setItem(STORAGE_PREF, pref);

  // Clear both stores first so switching preference never leaves a stale copy.
  window.localStorage.removeItem(STORAGE_KEY);
  window.sessionStorage.removeItem(STORAGE_KEY);

  state.key = value;
  if (!value) {
    paintKeyPill("none");
    setStatus("keyStatus", "Key cleared.", "");
    return;
  }
  const store = storageFor(pref);
  if (store) store.setItem(STORAGE_KEY, value);
  paintKeyPill("set");
  setStatus(
    "keyStatus",
    pref === "none" ? "Key kept for this page only." : "Key saved in your browser.",
    "ok",
  );
}

function clearKey() {
  window.localStorage.removeItem(STORAGE_KEY);
  window.sessionStorage.removeItem(STORAGE_KEY);
  state.key = "";
  $("apiKey").value = "";
  paintKeyPill("none");
  setStatus("keyStatus", "Key removed from this browser.", "");
}

function paintKeyPill(kind) {
  const pill = $("keyPill");
  pill.className = "pill";
  if (kind === "valid") {
    pill.classList.add("pill--ok");
    pill.textContent = "API key verified";
  } else if (kind === "invalid") {
    pill.classList.add("pill--bad");
    pill.textContent = "API key rejected";
  } else if (kind === "set") {
    pill.textContent = "API key set";
  } else {
    pill.classList.add("pill--muted");
    pill.textContent = "No API key";
  }
}

/* -------------------------------------------------------------------- utils */

/* A Dune execution routinely runs for minutes. Without a moving number the
 * page looks hung, and people reload — which abandons a query they have
 * already paid for. */
let elapsedTimer = null;

function startElapsed(id, message) {
  stopElapsed();
  const startedAt = Date.now();
  const tick = () => {
    const seconds = Math.round((Date.now() - startedAt) / 1000);
    const shown =
      seconds < 60
        ? `${seconds}s`
        : `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
    setStatus(id, `${message} (${shown})`, "");
  };
  tick();
  elapsedTimer = setInterval(tick, 1000);
}

function stopElapsed() {
  if (elapsedTimer !== null) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

function setStatus(id, message, kind) {
  const el = $(id);
  el.textContent = message;
  el.className = "status" + (kind ? " " + kind : "");
}

function currentKey() {
  // A typed-but-unsaved key should still work.
  return $("apiKey").value.trim() || state.key;
}

async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  const key = currentKey();
  if (key) headers["X-Dune-Api-Key"] = key;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(describeError(payload, response.status));
  return payload;
}

function describeError(payload, status) {
  const detail = payload && payload.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    // FastAPI validation errors.
    return detail
      .map((d) => `${(d.loc || []).slice(1).join(".") || "input"}: ${d.msg}`)
      .join("; ");
  }
  return `Request failed (HTTP ${status}).`;
}

const EVM_ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const SOLANA_MINT = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

/** Return a human message if the form cannot possibly be valid, else null. */
function validateForm() {
  const chain = $("chain").value;
  const token = $("tokenAddress").value.trim();
  if (!token) return "Enter a token contract or mint address.";
  if (chain === "solana") {
    if (!SOLANA_MINT.test(token)) {
      return "That is not a Solana mint address (base58, 32–44 characters).";
    }
  } else if (!EVM_ADDRESS.test(token)) {
    return "That is not an EVM contract address (0x followed by 40 hex characters).";
  }
  const start = $("startDate").value;
  const end = $("endDate").value;
  if (!start || !end) return "Pick both a start and an end date.";
  if (start > end) return "Start date must be on or before the end date.";
  return null;
}

function readRequest() {
  return {
    chain: $("chain").value,
    token_address: $("tokenAddress").value.trim(),
    start_date: $("startDate").value,
    end_date: $("endDate").value,
    min_balance: Number($("minBalance").value || 0),
    holder_mode: $("holderMode").value,
    include_contracts: $("includeContracts").checked,
    exclude_burn_addresses: $("excludeBurn").checked,
  };
}

/* ------------------------------------------------------------------ actions */

async function testKey() {
  if (!currentKey()) {
    setStatus("keyStatus", "Enter a key first.", "error");
    return;
  }
  await withBusy($("testKey"), async () => {
    setStatus("keyStatus", "Checking with Dune…", "");
    try {
      const { valid } = await api("/api/key/validate", { method: "POST" });
      paintKeyPill(valid ? "valid" : "invalid");
      setStatus(
        "keyStatus",
        valid ? "Key works." : "Dune rejected this key.",
        valid ? "ok" : "error",
      );
    } catch (error) {
      paintKeyPill("invalid");
      setStatus("keyStatus", error.message, "error");
    }
  });
}

async function showSql() {
  if (!currentKey()) {
    setStatus("runStatus", "Enter your Dune API key above first.", "error");
    $("apiKey").focus();
    return;
  }
  const problem = validateForm();
  if (problem) {
    setStatus("runStatus", problem, "error");
    return;
  }
  await withBusy($("showSql"), async () => {
    startElapsed("runStatus", "Resolving the source table…");
    try {
      const data = await api("/api/sql", { method: "POST", body: readRequest() });
      $("sqlOut").textContent = data.sql;
      $("sqlCard").classList.remove("hidden");
      stopElapsed();
      setStatus("runStatus", "", "");
    } catch (error) {
      stopElapsed();
      setStatus("runStatus", error.message, "error");
    }
  });
}

async function diagnose() {
  if (!currentKey()) {
    setStatus("runStatus", "Enter your Dune API key above first.", "error");
    return;
  }
  await withBusy($("diagnoseBtn"), async () => {
    startElapsed("runStatus", "Asking Dune which balance table to read…");
    try {
      // Ask what DICE actually resolved for this chain, rather than listing
      // the whole catalogue — the resolver already made the decision.
      const chain = $("chain").value;
      const data = await api(
        `/api/source?chain=${encodeURIComponent(chain)}&refresh=true`,
      );
      const cols = data.columns;
      const lines = [
        `Chain:  ${data.chain}`,
        `Table:  ${data.table}`,
        `Shape:  ${data.shape}` +
          (data.shape === "interval"
            ? "  (sparse [valid_from, valid_to) — expanded per day)"
            : "  (already one row per day)"),
        "",
        "Column mapping:",
        ...Object.entries(cols)
          .filter(([, value]) => value)
          .map(([role, value]) => {
            const type = (data.column_types || {})[value];
            return `  ${role.padEnd(11)}${value}${type ? `  (${type})` : ""}`;
          }),
      ];
      $("sqlOut").textContent = lines.join("\n");
      $("sqlCard").classList.remove("hidden");
      stopElapsed();
      setStatus("runStatus", "", "");
    } catch (error) {
      stopElapsed();
      setStatus("runStatus", error.message, "error");
    }
  });
}

async function runQuery(event) {
  event.preventDefault();
  if (!currentKey()) {
    setStatus("runStatus", "Enter your Dune API key above first.", "error");
    $("apiKey").focus();
    return;
  }
  const problem = validateForm();
  if (problem) {
    setStatus("runStatus", problem, "error");
    return;
  }

  await withBusy($("runBtn"), async () => {
    startElapsed("runStatus", "Running on Dune — this can take a few minutes…");
    try {
      const request = readRequest();
      const data = await api("/api/holders", { method: "POST", body: request });
      state.jobId = data.job_id;
      state.preview = data.preview;
      state.summary = data.summary_preview;
      state.lastRun = { request, wallet_count: data.wallet_count };
      stopElapsed();
      renderResults(data);
      prepareWatchlistSave();
      setStatus(
        "runStatus",
        data.row_count ? "Done." : "No holders matched — try a wider range or a lower minimum.",
        data.row_count ? "ok" : "",
      );
    } catch (error) {
      stopElapsed();
      setStatus("runStatus", error.message, "error");
    }
  });
}

function renderResults(data) {
  $("resultsCard").classList.remove("hidden");
  $("resultsMeta").textContent =
    `${data.wallet_count.toLocaleString()} wallets · ` +
    `${data.row_count.toLocaleString()} snapshot rows · ` +
    `execution ${data.execution_id}` +
    (data.truncated ? " · row cap reached, export is truncated" : "") +
    (data.end_date_clamped
      ? ` · end date clamped to ${data.effective_end_date} — no data exists after today`
      : "");
  $("previewNote").textContent =
    data.row_count > state.preview.length
      ? `Showing the first ${state.preview.length.toLocaleString()} rows — ` +
        "download for the full result."
      : "";
  updateDownloadLabel();
  renderTable();
}

function updateDownloadLabel() {
  $("downloadBtn").textContent =
    state.tab === "summary" ? "Download summary" : "Download snapshots";
}

function renderTable() {
  const rows = state.tab === "summary" ? state.summary : state.preview;
  const table = $("resultsTable");
  const head = table.tHead;
  const body = table.tBodies[0];
  head.innerHTML = "";
  body.innerHTML = "";
  if (!rows.length) return;

  const columns = Object.keys(rows[0]);
  const headRow = head.insertRow();
  for (const column of columns) {
    const th = document.createElement("th");
    th.textContent = column;
    headRow.appendChild(th);
  }
  for (const row of rows) {
    const tr = body.insertRow();
    for (const column of columns) {
      tr.insertCell().textContent = formatCell(row[column]);
    }
  }
}

function formatCell(value) {
  if (typeof value === "number" && !Number.isInteger(value)) {
    return value.toLocaleString(undefined, { maximumFractionDigits: 8 });
  }
  return String(value);
}

function download() {
  if (!state.jobId) return;
  const format = $("format").value;
  // Download what the user is looking at: the active tab decides which table
  // leads the file, not just the holder mode.
  // Plain navigation: no key in the URL, the file streams straight from /api/export.
  window.location.href =
    `/api/export/${state.jobId}?format=${format}&dataset=${state.tab}`;
}

async function withBusy(button, work) {
  button.disabled = true;
  try {
    await work();
  } finally {
    button.disabled = false;
  }
}

/* --------------------------------------------------- watchlists & signals */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function shortAddress(address) {
  return address.length > 14
    ? `${address.slice(0, 6)}…${address.slice(-4)}`
    : address;
}

function fmtUsd(value) {
  if (value === null || value === undefined) return "—";
  return "$" + Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function fmtTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function dexscreenerUrl(chain, token) {
  return `https://dexscreener.com/${encodeURIComponent(chain)}/${encodeURIComponent(token)}`;
}

function monitorCap() {
  return (state.config && state.config.monitor && state.config.monitor.max_wallets) || 2000;
}

/* ------------------------------------------------------- save as watchlist */

function prepareWatchlistSave() {
  if (!state.lastRun) return;
  const { request, wallet_count } = state.lastRun;
  const token = request.token_address;
  $("watchlistName").value =
    `${request.chain} ${token.slice(0, 10)}… buyers ` +
    `${request.start_date}→${request.end_date}`;
  const over = wallet_count > monitorCap();
  $("topNField").classList.toggle("hidden", !over);
  if (over) $("watchlistTopN").value = monitorCap();
  setStatus(
    "watchlistSaveStatus",
    over
      ? `${wallet_count.toLocaleString()} wallets — above the monitoring cap ` +
        `of ${monitorCap().toLocaleString()}, so only the top holders will be kept.`
      : "",
    "",
  );
}

async function saveWatchlist() {
  if (!state.jobId) return;
  await withBusy($("saveWatchlistBtn"), async () => {
    setStatus("watchlistSaveStatus", "Creating watchlist…", "");
    const body = { name: $("watchlistName").value.trim() || null };
    const topN = Number($("watchlistTopN").value);
    if (!$("topNField").classList.contains("hidden") && topN > 0) {
      body.top_n = Math.floor(topN);
    }
    try {
      const created = await api(`/api/watchlists/from-job/${state.jobId}`, {
        method: "POST",
        body,
      });
      setStatus(
        "watchlistSaveStatus",
        `Watchlist "${created.name}" created with ` +
          `${created.wallet_count.toLocaleString()} wallets.`,
        "ok",
      );
      await loadWatchlists();
      $("watchlistsCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      setStatus("watchlistSaveStatus", error.message, "error");
      if (/cap/i.test(error.message)) {
        $("topNField").classList.remove("hidden");
        if (!$("watchlistTopN").value) $("watchlistTopN").value = monitorCap();
      }
    }
  });
}

/* ------------------------------------------------------------- watchlists */

async function loadWatchlists() {
  try {
    state.watchlists = await api("/api/watchlists");
    renderWatchlists();
    setStatus("watchlistsStatus", "", "");
  } catch (error) {
    setStatus("watchlistsStatus", error.message, "error");
  }
}

function renderWatchlists() {
  const table = $("watchlistsTable");
  const empty = $("watchlistsEmpty");
  table.classList.toggle("hidden", !state.watchlists.length);
  empty.classList.toggle("hidden", !!state.watchlists.length);
  table.tHead.innerHTML = "";
  table.tBodies[0].innerHTML = "";
  if (!state.watchlists.length) return;

  const headRow = table.tHead.insertRow();
  for (const title of [
    "Watchlist", "Wallets", "Signal threshold", "Buy window",
    "Every", "Auto", "Last run", "Signals", "",
  ]) {
    headRow.appendChild(el("th", null, title));
  }

  for (const watchlist of state.watchlists) {
    renderWatchlistRow(table.tBodies[0], watchlist);
  }
}

function renderWatchlistRow(tbody, wl) {
  const tr = tbody.insertRow();

  const nameCell = tr.insertCell();
  nameCell.appendChild(el("span", null, wl.name));
  nameCell.appendChild(el("span", "chain-pill", wl.chain));
  if (wl.notes) nameCell.title = wl.notes;

  tr.insertCell().textContent = wl.wallet_count.toLocaleString();

  const pctNote = wl.min_wallets_pct > 0 ? ` (≥${wl.min_wallets_pct}%)` : "";
  tr.insertCell().textContent =
    `${wl.effective_min_wallets} of ${wl.wallet_count}${pctNote}`;

  tr.insertCell().textContent = `${wl.buy_window_hours}h`;
  tr.insertCell().textContent = `${wl.monitor_interval_hours}h`;

  const autoCell = tr.insertCell();
  const autoPossible =
    state.config && state.config.monitor && state.config.monitor.auto_possible;
  const autoBadge = el(
    "span",
    "badge" + (wl.auto_monitor ? " badge--ok" : ""),
    wl.auto_monitor ? "on" : "off",
  );
  if (wl.auto_monitor && wl.next_run_at) {
    autoBadge.title = `next run ${fmtTime(wl.next_run_at)}`;
  }
  if (wl.auto_monitor && !autoPossible) {
    autoBadge.className = "badge";
    autoBadge.textContent = "on*";
    autoBadge.title =
      "Scheduled runs need DUNE_API_KEY configured on the server.";
  }
  autoCell.appendChild(autoBadge);

  const lastRunCell = tr.insertCell();
  if (wl.last_run_status) {
    const ok = wl.last_run_status === "ok";
    const badge = el(
      "span",
      "badge " + (ok ? "badge--ok" : "badge--bad"),
      `${wl.last_run_status} · ${fmtTime(wl.last_run_at)}`,
    );
    if (!ok && wl.last_run_error) badge.title = wl.last_run_error;
    lastRunCell.appendChild(badge);
  } else {
    lastRunCell.textContent = "never";
  }

  const signalsCell = tr.insertCell();
  signalsCell.appendChild(
    el(
      "span",
      "badge" + (wl.active_signals ? " badge--hot" : ""),
      String(wl.active_signals),
    ),
  );

  const actions = tr.insertCell();
  const runBtn = el("button", "small primary", "Run now");
  runBtn.type = "button";
  runBtn.addEventListener("click", () => runMonitorNow(wl, runBtn));
  const editBtn = el("button", "small", state.editingWatchlist === wl.id ? "Close" : "Edit");
  editBtn.type = "button";
  editBtn.addEventListener("click", () => {
    state.editingWatchlist = state.editingWatchlist === wl.id ? null : wl.id;
    renderWatchlists();
  });
  const deleteBtn = el("button", "small danger-ghost", "Delete");
  deleteBtn.type = "button";
  deleteBtn.addEventListener("click", () => deleteWatchlist(wl));
  actions.append(runBtn, " ", editBtn, " ", deleteBtn);

  if (state.editingWatchlist === wl.id) {
    renderWatchlistEditor(tbody, wl);
  }
}

function renderWatchlistEditor(tbody, wl) {
  const tr = tbody.insertRow();
  tr.className = "editor-row";
  const cell = tr.insertCell();
  cell.colSpan = 9;

  const grid = el("div", "editor-grid");
  const fields = [
    ["min_wallets", "Min wallets", wl.min_wallets, { min: 2, step: 1 }],
    ["min_wallets_pct", "Min % of list", wl.min_wallets_pct, { min: 0, max: 100, step: 0.5 }],
    ["buy_window_hours", "Buy window (h)", wl.buy_window_hours, { min: 1, step: 1 }],
    ["monitor_interval_hours", "Run every (h)", wl.monitor_interval_hours, { min: 1, step: 0.5 }],
    ["min_buy_usd", "Min buy (USD)", wl.min_buy_usd, { min: 0, step: 1 }],
  ];
  const inputs = {};
  for (const [key, label, value, attrs] of fields) {
    const field = el("div", "field");
    field.appendChild(el("label", null, label));
    const input = document.createElement("input");
    input.type = "number";
    Object.assign(input, attrs);
    input.value = value;
    inputs[key] = input;
    field.appendChild(input);
    grid.appendChild(field);
  }

  const autoField = el("div", "field");
  autoField.appendChild(el("label", null, "Auto monitor"));
  const autoLabel = el("label", "check-inline");
  const autoInput = document.createElement("input");
  autoInput.type = "checkbox";
  autoInput.checked = wl.auto_monitor;
  autoLabel.append(autoInput, " scheduled runs");
  autoField.appendChild(autoLabel);
  grid.appendChild(autoField);

  const ignoreField = el("div", "field span-all");
  ignoreField.appendChild(
    el("label", null, "Ignored tokens (one address per line; stables/wrapped native are always ignored)"),
  );
  const ignoreArea = document.createElement("textarea");
  ignoreArea.value = wl.ignore_tokens.join("\n");
  ignoreField.appendChild(ignoreArea);
  grid.appendChild(ignoreField);

  const addField = el("div", "field span-all");
  addField.appendChild(el("label", null, "Add wallets (one address per line)"));
  const addArea = document.createElement("textarea");
  addArea.placeholder = "0x…";
  addField.appendChild(addArea);
  grid.appendChild(addField);

  const actionsRow = el("div", "actions");
  const saveBtn = el("button", "small primary", "Save settings");
  saveBtn.type = "button";
  const status = el("span", "status");
  saveBtn.addEventListener("click", async () => {
    await withBusy(saveBtn, async () => {
      const body = {
        min_wallets: Number(inputs.min_wallets.value),
        min_wallets_pct: Number(inputs.min_wallets_pct.value),
        buy_window_hours: Number(inputs.buy_window_hours.value),
        monitor_interval_hours: Number(inputs.monitor_interval_hours.value),
        min_buy_usd: Number(inputs.min_buy_usd.value),
        auto_monitor: autoInput.checked,
        ignore_tokens: ignoreArea.value.split("\n").map((s) => s.trim()).filter(Boolean),
      };
      const added = addArea.value.split("\n").map((s) => s.trim()).filter(Boolean);
      if (added.length) body.add_wallets = added;
      try {
        await api(`/api/watchlists/${wl.id}`, { method: "PATCH", body });
        state.editingWatchlist = null;
        await loadWatchlists();
      } catch (error) {
        status.textContent = error.message;
        status.className = "status error";
      }
    });
  });
  actionsRow.append(saveBtn, status);

  cell.append(grid, actionsRow);
}

async function runMonitorNow(wl, button) {
  if (!currentKey()) {
    setStatus("watchlistsStatus", "Enter your Dune API key above first.", "error");
    return;
  }
  await withBusy(button, async () => {
    setStatus(
      "watchlistsStatus",
      `Checking what ${wl.name} bought in the last ${wl.buy_window_hours}h…`,
      "",
    );
    try {
      const result = await api(`/api/watchlists/${wl.id}/monitor`, { method: "POST" });
      const fresh = result.new_signals.length;
      const updated = result.updated_signals.length;
      setStatus(
        "watchlistsStatus",
        `${wl.name}: ${result.run.buy_rows} buy rows, ` +
          `${fresh} new signal${fresh === 1 ? "" : "s"}, ${updated} updated.`,
        fresh ? "ok" : "",
      );
      await Promise.all([loadWatchlists(), loadSignals()]);
    } catch (error) {
      setStatus("watchlistsStatus", error.message, "error");
      await loadWatchlists();
    }
  });
}

async function deleteWatchlist(wl) {
  const sure = window.confirm(
    `Delete watchlist "${wl.name}"? Its signals and run history go with it.`,
  );
  if (!sure) return;
  try {
    await api(`/api/watchlists/${wl.id}`, { method: "DELETE" });
    await Promise.all([loadWatchlists(), loadSignals()]);
  } catch (error) {
    setStatus("watchlistsStatus", error.message, "error");
  }
}

/* ---------------------------------------------------------------- signals */

async function loadSignals() {
  try {
    const include = $("showDismissed").checked ? "true" : "false";
    state.signals = await api(`/api/signals?include_dismissed=${include}`);
    renderSignals();
    setStatus("signalsStatus", "", "");
  } catch (error) {
    setStatus("signalsStatus", error.message, "error");
  }
}

function renderSignals() {
  const table = $("signalsTable");
  const empty = $("signalsEmpty");
  table.classList.toggle("hidden", !state.signals.length);
  empty.classList.toggle("hidden", !!state.signals.length);
  table.tHead.innerHTML = "";
  table.tBodies[0].innerHTML = "";
  if (!state.signals.length) return;

  const headRow = table.tHead.insertRow();
  for (const title of [
    "Updated", "Watchlist", "Token", "Buyers", "Volume", "Status", "",
  ]) {
    headRow.appendChild(el("th", null, title));
  }

  for (const signal of state.signals) {
    renderSignalRow(table.tBodies[0], signal);
  }
}

function renderSignalRow(tbody, signal) {
  const tr = tbody.insertRow();
  tr.className = "signal-row";

  const updated = tr.insertCell();
  updated.textContent = fmtTime(signal.last_updated_at);
  updated.title = `first seen ${fmtTime(signal.first_seen_at)}`;

  tr.insertCell().textContent = signal.watchlist_name || `#${signal.watchlist_id}`;

  const tokenCell = tr.insertCell();
  const link = el("a", "token-link", signal.token_symbol || shortAddress(signal.token_address));
  link.href = dexscreenerUrl(signal.chain, signal.token_address);
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.title = "Open on DexScreener";
  tokenCell.appendChild(link);
  tokenCell.appendChild(el("span", "mono", " " + shortAddress(signal.token_address)));
  const copyBtn = el("button", "small ghost", "copy");
  copyBtn.type = "button";
  copyBtn.title = "Copy token address";
  copyBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    navigator.clipboard.writeText(signal.token_address).then(() => {
      copyBtn.textContent = "copied";
      setTimeout(() => (copyBtn.textContent = "copy"), 1200);
    });
  });
  tokenCell.appendChild(copyBtn);

  const share = Math.round((signal.wallet_count / signal.watchlist_size) * 100);
  tr.insertCell().textContent =
    `${signal.wallet_count}/${signal.watchlist_size} (${share}%)`;

  tr.insertCell().textContent = fmtUsd(signal.total_usd);

  const statusCell = tr.insertCell();
  const isNew =
    signal.status === "active" && signal.first_seen_at === signal.last_updated_at;
  statusCell.appendChild(
    el(
      "span",
      "badge " + (signal.status === "dismissed" ? "" : isNew ? "badge--hot" : "badge--ok"),
      signal.status === "dismissed" ? "dismissed" : isNew ? "new" : "active",
    ),
  );

  const actions = tr.insertCell();
  const toggleBtn = el(
    "button",
    "small" + (signal.status === "dismissed" ? "" : " danger-ghost"),
    signal.status === "dismissed" ? "Restore" : "Dismiss",
  );
  toggleBtn.type = "button";
  toggleBtn.addEventListener("click", async (event) => {
    event.stopPropagation();
    const action = signal.status === "dismissed" ? "restore" : "dismiss";
    try {
      await api(`/api/signals/${signal.id}/${action}`, { method: "POST" });
      await Promise.all([loadSignals(), loadWatchlists()]);
    } catch (error) {
      setStatus("signalsStatus", error.message, "error");
    }
  });
  actions.appendChild(toggleBtn);

  tr.addEventListener("click", () => {
    if (state.expandedSignals.has(signal.id)) {
      state.expandedSignals.delete(signal.id);
    } else {
      state.expandedSignals.add(signal.id);
    }
    renderSignals();
  });

  if (state.expandedSignals.has(signal.id)) {
    renderSignalDetails(tbody, signal);
  }
}

function renderSignalDetails(tbody, signal) {
  const tr = tbody.insertRow();
  tr.className = "signal-details";
  const cell = tr.insertCell();
  cell.colSpan = 7;

  cell.appendChild(
    el(
      "div",
      "row-note",
      `Full address: ${signal.token_address} — wallets that bought inside the window:`,
    ),
  );

  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  for (const title of ["Wallet", "Buys", "USD", "First buy", "Last buy"]) {
    head.appendChild(el("th", null, title));
  }
  const body = table.createTBody();
  for (const buyer of signal.buyers) {
    const row = body.insertRow();
    const walletCell = row.insertCell();
    walletCell.appendChild(el("span", "mono", buyer.wallet_address));
    row.insertCell().textContent = String(buyer.buy_count);
    row.insertCell().textContent = fmtUsd(buyer.amount_usd);
    row.insertCell().textContent = fmtTime(buyer.first_buy_at);
    row.insertCell().textContent = fmtTime(buyer.last_buy_at);
  }
  const wrap = el("div", "scroll");
  wrap.appendChild(table);
  cell.appendChild(wrap);
}

/* ---------------------------------------------------------------- polling */

function startPolling() {
  window.setInterval(() => {
    if (document.visibilityState === "visible") {
      loadWatchlists();
      loadSignals();
    }
  }, 60_000);
}

/* --------------------------------------------------------------------- init */

function initDates() {
  const today = new Date();
  const end = today.toISOString().slice(0, 10);
  const start = new Date(today.getTime() - 6 * 86400000).toISOString().slice(0, 10);
  $("startDate").value = start;
  $("endDate").value = end;
  // Nothing exists after today; a future end date would otherwise project
  // current balances forward into days that have not happened.
  $("startDate").max = end;
  $("endDate").max = end;
}

function init() {
  loadKey();
  initDates();

  $("saveKey").addEventListener("click", saveKey);
  $("clearKey").addEventListener("click", clearKey);
  $("testKey").addEventListener("click", testKey);
  $("toggleKey").addEventListener("click", () => {
    const input = $("apiKey");
    const hidden = input.type === "password";
    input.type = hidden ? "text" : "password";
    $("toggleKey").textContent = hidden ? "Hide" : "Show";
  });
  $("apiKey").addEventListener("input", () => {
    state.key = $("apiKey").value.trim();
    paintKeyPill(state.key ? "set" : "none");
  });

  $("queryForm").addEventListener("submit", runQuery);
  // Deliberately no dynamic min on the end date: a native constraint blocks
  // the submit event silently, so our own validation never runs and whatever
  // error was on screen stays there, describing the wrong problem. Let the
  // form submit and report the range mistake in the same place as every
  // other message.
  //
  // Clear a stale error as soon as anything is edited, so a message never
  // outlives the input it was about.
  for (const field of $("queryForm").querySelectorAll("input, select")) {
    field.addEventListener("input", () => {
      if ($("runStatus").classList.contains("error")) setStatus("runStatus", "", "");
    });
  }
  $("chain").addEventListener("change", () => {
    const solana = $("chain").value === "solana";
    $("tokenAddress").placeholder = solana ? "Base58 mint address…" : "0x…";
  });
  $("showSql").addEventListener("click", showSql);
  $("diagnoseBtn").addEventListener("click", diagnose);
  $("downloadBtn").addEventListener("click", download);

  $("saveWatchlistBtn").addEventListener("click", saveWatchlist);
  $("refreshWatchlists").addEventListener("click", loadWatchlists);
  $("refreshSignals").addEventListener("click", loadSignals);
  $("showDismissed").addEventListener("change", loadSignals);

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".tab")) {
        other.classList.toggle("is-active", other === tab);
      }
      state.tab = tab.dataset.tab;
      updateDownloadLabel();
      renderTable();
    });
  }

  api("/api/config")
    .then((config) => {
      state.config = config;
      if (config.server_key_configured && !currentKey()) {
        setStatus(
          "keyStatus",
          "A server-side key is configured; you can run queries without entering one.",
          "",
        );
      }
      const monitor = config.monitor || {};
      let hint;
      if (monitor.auto_possible) {
        hint = "Scheduled runs are on (server-side key configured).";
      } else {
        hint =
          "Scheduled auto-runs need DUNE_API_KEY on the server; " +
          "the Run now button always works with your browser key.";
      }
      if (monitor.telegram_configured) hint += " Telegram alerts are on.";
      $("autoMonitorHint").textContent = hint;
    })
    .catch(() => {});

  loadWatchlists();
  loadSignals();
  startPolling();
}

document.addEventListener("DOMContentLoaded", init);
