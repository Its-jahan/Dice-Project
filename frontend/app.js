/* DICE frontend.
 *
 * The Dune API key is saved on the server (single-user deployment): pressing
 * "Save key" POSTs it to /api/key, where it is validated against Dune and
 * stored. Every later request — including scheduled watchlist monitoring —
 * uses that stored key. Typing a key without saving still works for one-off
 * requests: it rides along as the X-Dune-Api-Key header and overrides the
 * stored key for that request only.
 */

const $ = (id) => document.getElementById(id);

const PREVIEW_INITIAL = 10;
const PREVIEW_STEP = 50;

const state = {
  config: null,       // /api/config payload
  jobId: null,
  preview: [],
  summary: [],
  tab: "snapshots",
  shown: { snapshots: PREVIEW_INITIAL, summary: PREVIEW_INITIAL },
  lastRun: null,      // last successful holders response (for save-as-watchlist)
  watchlists: [],
  signals: [],
  editingWatchlist: null,   // watchlist object open in the edit modal
  expandedSignals: new Set(),
  realtime: null,     // /api/settings/realtime payload
};

/* -------------------------------------------------------------------- utils */

function setStatus(id, message, kind) {
  const el = $(id);
  el.textContent = message;
  el.classList.remove("text-success", "text-danger", "text-body-secondary");
  el.classList.add(
    kind === "ok" ? "text-success" : kind === "error" ? "text-danger" : "text-body-secondary",
  );
}

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

function currentKey() {
  // A typed key overrides the server-stored one for this request only.
  return $("apiKey").value.trim();
}

function serverKeyConfigured() {
  return !!(state.config && state.config.server_key_configured);
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
    return detail
      .map((d) => `${(d.loc || []).slice(1).join(".") || "input"}: ${d.msg}`)
      .join("; ");
  }
  return `Request failed (HTTP ${status}).`;
}

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

/* ---------------------------------------------------------------- key panel */

function paintKeyPill() {
  const pill = $("keyPill");
  pill.className = "badge rounded-pill";
  if (serverKeyConfigured()) {
    pill.classList.add("text-bg-success");
    pill.textContent = `Key saved ${state.config.server_key_hint || ""}`.trim();
  } else {
    pill.classList.add("text-bg-secondary");
    pill.textContent = "No API key saved";
  }
}

async function refreshConfig() {
  try {
    state.config = await api("/api/config");
  } catch {
    return;
  }
  paintKeyPill();
  const monitor = state.config.monitor || {};
  let hint;
  if (monitor.auto_possible) {
    hint = "Scheduled runs are active — the saved key powers them.";
  } else {
    hint = "Save your Dune key above to activate scheduled runs.";
  }
  if (monitor.telegram_configured) hint += " Telegram alerts are on.";
  $("autoMonitorHint").textContent = hint;
}

async function saveKey() {
  const key = currentKey();
  if (!key) {
    setStatus("keyStatus", "Paste a key first.", "error");
    return;
  }
  await withBusy($("saveKey"), async () => {
    setStatus("keyStatus", "Checking the key with Dune…", "");
    try {
      const saved = await api("/api/key", { method: "POST", body: { key } });
      $("apiKey").value = "";
      await refreshConfig();
      setStatus(
        "keyStatus",
        `Key ${saved.hint || ""} verified and saved on the server. ` +
          "Scheduled monitoring will use it.",
        "ok",
      );
    } catch (error) {
      setStatus("keyStatus", error.message, "error");
    }
  });
}

async function testKey() {
  if (!currentKey() && !serverKeyConfigured()) {
    setStatus("keyStatus", "Paste a key first (or save one).", "error");
    return;
  }
  await withBusy($("testKey"), async () => {
    setStatus("keyStatus", "Checking with Dune…", "");
    try {
      const { valid } = await api("/api/key/validate", { method: "POST" });
      setStatus(
        "keyStatus",
        valid ? "Key works." : "Dune rejected this key.",
        valid ? "ok" : "error",
      );
    } catch (error) {
      setStatus("keyStatus", error.message, "error");
    }
  });
}

async function clearKey() {
  await withBusy($("clearKey"), async () => {
    try {
      await api("/api/key", { method: "DELETE" });
      $("apiKey").value = "";
      await refreshConfig();
      setStatus("keyStatus", "Key removed from the server.", "");
    } catch (error) {
      setStatus("keyStatus", error.message, "error");
    }
  });
}

async function archiveQueries() {
  const sure = window.confirm(
    "Archive every query named “DICE …” in the Dune account?\n" +
    "This frees private-query slots; your other queries are untouched.",
  );
  if (!sure) return;
  await withBusy($("archiveQueries"), async () => {
    setStatus("keyStatus", "Listing and archiving DICE queries on Dune…", "");
    try {
      const result = await api("/api/dune/archive-queries", { method: "POST" });
      let message = `Archived ${result.archived} of ${result.found} DICE queries.`;
      if (result.failed) {
        message += ` ${result.failed} failed — ${(result.errors || []).join("; ")}`;
      }
      setStatus("keyStatus", message, result.failed ? "error" : "ok");
    } catch (error) {
      setStatus("keyStatus", error.message, "error");
    }
  });
}

/* -------------------------------------------------------------- query form */

const EVM_ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const SOLANA_MINT = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

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

function requireKey(statusId) {
  if (!currentKey() && !serverKeyConfigured()) {
    setStatus(statusId, "Save your Dune API key above first.", "error");
    $("apiKey").focus();
    return false;
  }
  return true;
}

async function showSql() {
  if (!requireKey("runStatus")) return;
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
      $("sqlCard").classList.remove("d-none");
      stopElapsed();
      setStatus("runStatus", "", "");
    } catch (error) {
      stopElapsed();
      setStatus("runStatus", error.message, "error");
    }
  });
}

async function diagnose() {
  if (!requireKey("runStatus")) return;
  await withBusy($("diagnoseBtn"), async () => {
    startElapsed("runStatus", "Asking Dune which balance table to read…");
    try {
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
      $("sqlCard").classList.remove("d-none");
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
  if (!requireKey("runStatus")) return;
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
      state.shown = { snapshots: PREVIEW_INITIAL, summary: PREVIEW_INITIAL };
      state.lastRun = { request, wallet_count: data.wallet_count, row_count: data.row_count };
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

/* ----------------------------------------------------------------- results */

function renderResults(data) {
  $("resultsCard").classList.remove("d-none");
  bootstrap.Collapse.getOrCreateInstance($("resultsBody"), { toggle: false }).show();
  $("resultsMeta").textContent =
    `${data.wallet_count.toLocaleString()} wallets · ` +
    `${data.row_count.toLocaleString()} snapshot rows · ` +
    `execution ${data.execution_id}` +
    (data.truncated ? " · row cap reached, export is truncated" : "") +
    (data.end_date_clamped
      ? ` · end date clamped to ${data.effective_end_date} — no data exists after today`
      : "");
  renderTable();
}

function activeRows() {
  return state.tab === "summary" ? state.summary : state.preview;
}

function renderTable() {
  const rows = activeRows();
  const visible = rows.slice(0, state.shown[state.tab]);
  const table = $("resultsTable");
  const head = table.tHead;
  const body = table.tBodies[0];
  head.innerHTML = "";
  body.innerHTML = "";

  if (visible.length) {
    const columns = Object.keys(visible[0]);
    const headRow = head.insertRow();
    for (const column of columns) {
      headRow.appendChild(el("th", null, column));
    }
    for (const row of visible) {
      const tr = body.insertRow();
      for (const column of columns) {
        tr.insertCell().textContent = formatCell(row[column]);
      }
    }
  }

  const remaining = rows.length - visible.length;
  $("showMoreBtn").classList.toggle("d-none", remaining <= 0);
  if (remaining > 0) {
    $("showMoreBtn").textContent = `Show ${Math.min(PREVIEW_STEP, remaining)} more`;
  }

  const total = state.lastRun ? state.lastRun.row_count : rows.length;
  $("previewNote").textContent = rows.length
    ? `Showing ${visible.length.toLocaleString()} of ${rows.length.toLocaleString()} ` +
      `loaded rows (${total.toLocaleString()} total — download for everything).`
    : "";
}

function showMore() {
  state.shown[state.tab] += PREVIEW_STEP;
  renderTable();
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
  // leads the file. No key in the URL.
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

/* ------------------------------------------------------- save as watchlist */

/** Enable the Live switch only when Alchemy is set up and covers the chain. */
function updateRealtimeAvailability() {
  const chain = $("chain").value;
  const supported = realtimeSupports(chain);
  const ready = realtimeReady();
  const toggle = $("watchlistRealtime");
  toggle.disabled = !(ready && supported);
  if (toggle.disabled) toggle.checked = false;
  $("watchlistRealtimeLabel").textContent = !supported
    ? "n/a here"
    : ready
      ? "Alchemy"
      : "set up below";
  $("watchlistRealtimeLabel").title = !supported
    ? `Alchemy webhooks are not wired up for ${chain}.`
    : ready
      ? "Signals fire seconds after a buy instead of at the next scheduled check."
      : "Add your Alchemy auth token and public URL in the Live monitoring card.";
}

function prepareWatchlistSave() {
  if (!state.lastRun) return;
  const { request, wallet_count } = state.lastRun;
  const token = request.token_address;
  $("watchlistName").value =
    `${request.chain} ${token.slice(0, 10)}… buyers ` +
    `${request.start_date}→${request.end_date}`;
  const over = wallet_count > monitorCap();
  $("topNField").classList.toggle("d-none", !over);
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
    const body = {
      name: $("watchlistName").value.trim() || null,
      monitor_interval_hours: Number($("watchlistInterval").value) || 24,
      buy_detection: $("watchlistDetection").value,
      realtime: $("watchlistRealtime").checked,
    };
    const topN = Number($("watchlistTopN").value);
    if (!$("topNField").classList.contains("d-none") && topN > 0) {
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
          `${created.wallet_count.toLocaleString()} wallets — checking every ` +
          `${created.monitor_interval_hours}h.`,
        "ok",
      );
      await loadWatchlists();
      $("watchlistsCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      setStatus("watchlistSaveStatus", error.message, "error");
      if (/cap/i.test(error.message)) {
        $("topNField").classList.remove("d-none");
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
  table.classList.toggle("d-none", !state.watchlists.length);
  empty.classList.toggle("d-none", !!state.watchlists.length);
  table.tHead.innerHTML = "";
  table.tBodies[0].innerHTML = "";
  if (!state.watchlists.length) return;

  const headRow = table.tHead.insertRow();
  for (const title of [
    "Watchlist", "Wallets", "Threshold", "Window", "Every", "Counts", "Live",
    "Auto", "Last run", "Signals", "",
  ]) {
    headRow.appendChild(el("th", "small text-body-secondary", title));
  }

  for (const watchlist of state.watchlists) {
    renderWatchlistRow(table.tBodies[0], watchlist);
  }
}

function renderWatchlistRow(tbody, wl) {
  const tr = tbody.insertRow();

  const nameCell = tr.insertCell();
  nameCell.appendChild(el("span", "fw-medium", wl.name));
  nameCell.appendChild(el("span", "badge text-bg-light border ms-2", wl.chain));
  if (wl.notes) nameCell.title = wl.notes;

  tr.insertCell().textContent = wl.wallet_count.toLocaleString();

  const pctNote = wl.min_wallets_pct > 0 ? ` (≥${wl.min_wallets_pct}%)` : "";
  tr.insertCell().textContent =
    `${wl.effective_min_wallets} of ${wl.wallet_count}${pctNote}`;

  tr.insertCell().textContent = `${wl.buy_window_hours}h`;
  tr.insertCell().textContent = `${wl.monitor_interval_hours}h`;

  const detectionCell = tr.insertCell();
  const detection = wl.buy_detection || "both";
  const detectionLabel =
    detection === "dex" ? "DEX only" : detection === "balance" ? "positions" : "both";
  const detectionBadge = el("span", "badge text-bg-light border text-body-secondary", detectionLabel);
  detectionBadge.title =
    detection === "dex"
      ? "Only confirmed DEX swaps count as a buy"
      : detection === "balance"
        ? "Any token whose balance went 0 → positive counts"
        : "DEX swaps and new positions, each labelled (two Dune queries per run)";
  detectionCell.appendChild(detectionBadge);

  const liveCell = tr.insertCell();
  if (wl.realtime) {
    const liveBadge = el("span", "badge text-bg-danger", "● live");
    liveBadge.title =
      "Alchemy pushes token arrivals for these wallets; signals fire in seconds.";
    liveCell.appendChild(liveBadge);
  } else {
    liveCell.appendChild(el("span", "small text-body-secondary", "—"));
  }

  const autoCell = tr.insertCell();
  const autoPossible =
    state.config && state.config.monitor && state.config.monitor.auto_possible;
  let autoBadge;
  if (!wl.auto_monitor) {
    autoBadge = el("span", "badge text-bg-secondary", "off");
  } else if (autoPossible) {
    autoBadge = el("span", "badge text-bg-success", "on");
    if (wl.next_run_at) autoBadge.title = `next run ${fmtTime(wl.next_run_at)}`;
  } else {
    autoBadge = el("span", "badge text-bg-warning", "needs key");
    autoBadge.title = "Save your Dune API key to activate scheduled runs.";
  }
  autoCell.appendChild(autoBadge);

  const lastRunCell = tr.insertCell();
  if (wl.last_run_status) {
    const ok = wl.last_run_status === "ok";
    const badge = el(
      "span",
      "badge " + (ok ? "text-bg-success" : "text-bg-danger"),
      wl.last_run_status,
    );
    badge.title = (ok ? "" : (wl.last_run_error || "") + " — ") + fmtTime(wl.last_run_at);
    lastRunCell.appendChild(badge);
    lastRunCell.appendChild(
      el("span", "small text-body-secondary ms-1", fmtTime(wl.last_run_at)),
    );
  } else {
    lastRunCell.appendChild(el("span", "small text-body-secondary", "never"));
  }

  const signalsCell = tr.insertCell();
  signalsCell.appendChild(
    el(
      "span",
      "badge " + (wl.active_signals ? "text-bg-primary" : "text-bg-light border text-body-secondary"),
      String(wl.active_signals),
    ),
  );

  const actions = tr.insertCell();
  actions.className = "text-nowrap text-end";
  const runBtn = el("button", "btn btn-primary btn-sm", "Run now");
  runBtn.type = "button";
  runBtn.addEventListener("click", () => runMonitorNow(wl, runBtn));
  const editBtn = el("button", "btn btn-outline-secondary btn-sm ms-1", "Edit");
  editBtn.type = "button";
  editBtn.addEventListener("click", () => openWatchlistModal(wl));
  const deleteBtn = el("button", "btn btn-outline-danger btn-sm ms-1", "Delete");
  deleteBtn.type = "button";
  deleteBtn.addEventListener("click", () => deleteWatchlist(wl));
  actions.append(runBtn, editBtn, deleteBtn);
}

/* ------------------------------------------------------ watchlist modal */

function ensureIntervalOption(select, value) {
  const text = String(value);
  if (![...select.options].some((option) => option.value === text)) {
    const option = document.createElement("option");
    option.value = text;
    option.textContent = text;
    select.appendChild(option);
  }
  select.value = text;
}

function openWatchlistModal(wl) {
  state.editingWatchlist = wl;
  $("wlModalTitle").textContent = `Edit “${wl.name}”`;
  $("wlMinWallets").value = wl.min_wallets;
  $("wlPct").value = wl.min_wallets_pct;
  $("wlMinUsd").value = wl.min_buy_usd;
  $("wlWindow").value = wl.buy_window_hours;
  ensureIntervalOption($("wlInterval"), wl.monitor_interval_hours);
  $("wlDetection").value = wl.buy_detection || "both";
  $("wlAuto").checked = wl.auto_monitor;
  $("wlRealtime").checked = wl.realtime;
  $("wlRealtime").disabled = !(realtimeReady() && realtimeSupports(wl.chain));
  $("wlIgnore").value = (wl.ignore_tokens || []).join("\n");
  $("wlAddWallets").value = "";
  setStatus("wlModalStatus", "", "");
  bootstrap.Modal.getOrCreateInstance($("watchlistModal")).show();
}

async function saveWatchlistSettings() {
  const wl = state.editingWatchlist;
  if (!wl) return;
  await withBusy($("wlSaveBtn"), async () => {
    const body = {
      min_wallets: Number($("wlMinWallets").value),
      min_wallets_pct: Number($("wlPct").value),
      buy_window_hours: Number($("wlWindow").value),
      monitor_interval_hours: Number($("wlInterval").value),
      min_buy_usd: Number($("wlMinUsd").value),
      auto_monitor: $("wlAuto").checked,
      realtime: $("wlRealtime").checked,
      buy_detection: $("wlDetection").value,
      ignore_tokens: $("wlIgnore").value.split("\n").map((s) => s.trim()).filter(Boolean),
    };
    const added = $("wlAddWallets").value.split("\n").map((s) => s.trim()).filter(Boolean);
    if (added.length) body.add_wallets = added;
    try {
      await api(`/api/watchlists/${wl.id}`, { method: "PATCH", body });
      bootstrap.Modal.getOrCreateInstance($("watchlistModal")).hide();
      state.editingWatchlist = null;
      await loadWatchlists();
    } catch (error) {
      setStatus("wlModalStatus", error.message, "error");
    }
  });
}

async function runMonitorNow(wl, button) {
  if (!requireKey("watchlistsStatus")) return;
  await withBusy(button, async () => {
    setStatus(
      "watchlistsStatus",
      `Checking what “${wl.name}” bought in the last ${wl.buy_window_hours}h…`,
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
  table.classList.toggle("d-none", !state.signals.length);
  empty.classList.toggle("d-none", !!state.signals.length);
  table.tHead.innerHTML = "";
  table.tBodies[0].innerHTML = "";
  if (!state.signals.length) return;

  const headRow = table.tHead.insertRow();
  for (const title of [
    "Updated", "Watchlist", "Token", "Buyers", "Volume", "Status", "",
  ]) {
    headRow.appendChild(el("th", "small text-body-secondary", title));
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
  const link = el(
    "a",
    "link-primary fw-medium text-decoration-none",
    signal.token_symbol || shortAddress(signal.token_address),
  );
  link.href = dexscreenerUrl(signal.chain, signal.token_address);
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.title = "Open on DexScreener";
  link.addEventListener("click", (event) => event.stopPropagation());
  tokenCell.appendChild(link);
  tokenCell.appendChild(el("span", "mono ms-2", shortAddress(signal.token_address)));
  const copyBtn = el("button", "btn btn-link btn-sm p-0 ms-2 small", "copy");
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
  const buyersCell = tr.insertCell();
  buyersCell.appendChild(
    el("span", null, `${signal.wallet_count}/${signal.watchlist_size} (${share}%)`),
  );
  // How the buyers were seen: confirmed DEX swaps vs. positions that simply
  // appeared. A signal made only of the latter deserves a closer look.
  const counts = { dex: 0, balance: 0, live: 0 };
  for (const buyer of signal.buyers) counts[buyer.via || "dex"] += 1;
  const badges = [
    ["dex", "text-bg-success", "DEX", "Confirmed DEX swaps"],
    ["live", "text-bg-danger", "live",
      "Pushed by Alchemy within seconds of the block"],
    ["balance", "text-bg-secondary", "pos",
      "New position with no matching DEX trade (OTC, CEX, transfer…)"],
  ];
  for (const [key, cls, label, title] of badges) {
    if (!counts[key]) continue;
    buyersCell.appendChild(
      Object.assign(el("span", `badge ${cls} ms-1`, `${counts[key]} ${label}`), {
        title,
      }),
    );
  }

  tr.insertCell().textContent = fmtUsd(signal.total_usd);

  const statusCell = tr.insertCell();
  const isNew =
    signal.status === "active" && signal.first_seen_at === signal.last_updated_at;
  statusCell.appendChild(
    el(
      "span",
      "badge " +
        (signal.status === "dismissed"
          ? "text-bg-secondary"
          : isNew
            ? "text-bg-primary"
            : "text-bg-success"),
      signal.status === "dismissed" ? "dismissed" : isNew ? "new" : "active",
    ),
  );

  const actions = tr.insertCell();
  actions.className = "text-nowrap text-end";
  const toggleBtn = el(
    "button",
    "btn btn-sm " +
      (signal.status === "dismissed" ? "btn-outline-secondary" : "btn-outline-danger"),
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
  tr.className = "table-active";
  const cell = tr.insertCell();
  cell.colSpan = 7;

  cell.appendChild(
    el(
      "div",
      "small text-body-secondary mb-2",
      `Full address: ${signal.token_address} — wallets that bought inside the window:`,
    ),
  );

  const table = document.createElement("table");
  table.className = "table table-sm mb-0";
  const head = table.createTHead().insertRow();
  for (const title of ["Wallet", "Seen as", "Buys", "USD", "First buy", "Last buy"]) {
    head.appendChild(el("th", "small text-body-secondary", title));
  }
  const body = table.createTBody();
  for (const buyer of signal.buyers) {
    const row = body.insertRow();
    row.insertCell().appendChild(el("span", "mono", buyer.wallet_address));
    const viaCell = row.insertCell();
    const via = buyer.via || "dex";
    const style = {
      dex: ["text-bg-success", "DEX buy",
        "Confirmed swap in Dune's DEX trade tables"],
      live: ["text-bg-danger", "live",
        "Token arrival pushed by Alchemy seconds after the block"],
      balance: ["text-bg-secondary", "new position",
        "Balance went 0 → positive with no matching DEX trade"],
    }[via] || ["text-bg-secondary", via, ""];
    const badge = el("span", "badge " + style[0], style[1]);
    badge.title = style[2];
    viaCell.appendChild(badge);
    row.insertCell().textContent = String(buyer.buy_count);
    row.insertCell().textContent = fmtUsd(buyer.amount_usd);
    row.insertCell().textContent = fmtTime(buyer.first_buy_at);
    row.insertCell().textContent = fmtTime(buyer.last_buy_at);
  }
  const wrap = el("div", "table-responsive");
  wrap.appendChild(table);
  cell.appendChild(wrap);
}

/* -------------------------------------------------------------- realtime */

function realtimeReady() {
  const rt = (state.config && state.config.realtime) || {};
  return !!(rt.configured && rt.public_url_set);
}

function realtimeSupports(chain) {
  const rt = (state.config && state.config.realtime) || {};
  return (rt.supported_chains || []).includes(chain);
}

async function loadRealtimeSettings() {
  try {
    const data = await api("/api/settings/realtime");
    state.realtime = data;
    $("rtUrl").value = data.public_base_url || "";
    $("rtToken").placeholder = data.token_hint
      ? `Auth token saved (${data.token_hint}) — paste to replace`
      : "Alchemy auth token";
    const badge = $("rtBadge");
    const live = data.configured && data.public_base_url;
    badge.className = "badge rounded-pill " +
      (live ? "text-bg-success" : "text-bg-secondary");
    badge.textContent = live ? "On" : "Off";
    $("rtChains").textContent =
      "Chains available for live monitoring: " +
      (data.supported_chains || []).join(", ") + ".";
    renderWebhookTable(data.webhooks || []);
  } catch {
    /* non-fatal */
  }
}

function renderWebhookTable(webhooks) {
  const table = $("rtTable");
  table.classList.toggle("d-none", !webhooks.length);
  table.tHead.innerHTML = "";
  table.tBodies[0].innerHTML = "";
  if (!webhooks.length) return;

  const head = table.tHead.insertRow();
  for (const title of ["Chain", "Network", "Wallets watched", "Last synced"]) {
    head.appendChild(el("th", "small text-body-secondary", title));
  }
  for (const hook of webhooks) {
    const row = table.tBodies[0].insertRow();
    row.insertCell().textContent = hook.chain;
    row.insertCell().appendChild(el("span", "mono", hook.network));
    row.insertCell().textContent = hook.address_count.toLocaleString();
    row.insertCell().textContent = fmtTime(hook.synced_at);
  }
}

async function saveRealtimeSettings() {
  await withBusy($("rtSave"), async () => {
    const body = { public_base_url: $("rtUrl").value.trim() };
    const token = $("rtToken").value.trim();
    if (token) body.auth_token = token;
    try {
      await api("/api/settings/realtime", { method: "PUT", body });
      $("rtToken").value = "";
      await Promise.all([loadRealtimeSettings(), refreshConfig()]);
      setStatus("rtStatus", "Saved. Now switch on Live for a watchlist.", "ok");
    } catch (error) {
      setStatus("rtStatus", error.message, "error");
    }
  });
}

async function syncRealtime() {
  await withBusy($("rtSync"), async () => {
    setStatus("rtStatus", "Reconciling watched wallets with Alchemy…", "");
    try {
      const result = await api("/api/settings/realtime/sync", { method: "POST" });
      const failures = result.synced.filter((entry) => entry.error);
      const total = result.synced
        .filter((entry) => !entry.error)
        .reduce((sum, entry) => sum + (entry.addresses || 0), 0);
      setStatus(
        "rtStatus",
        failures.length
          ? `Failed: ${failures.map((f) => `${f.chain} — ${f.error}`).join("; ")}`
          : `${total.toLocaleString()} wallets registered with Alchemy.`,
        failures.length ? "error" : "ok",
      );
      await Promise.all([loadRealtimeSettings(), loadWatchlists()]);
    } catch (error) {
      setStatus("rtStatus", error.message, "error");
    }
  });
}

/* ----------------------------------------------------------- notifications */

async function loadNotificationSettings() {
  try {
    const data = await api("/api/settings/notifications");
    $("tgChat").value = data.chat_id || "";
    $("tgToken").placeholder = data.bot_token_hint
      ? `Bot token saved (${data.bot_token_hint}) — paste to replace`
      : "Bot token (123456:ABC-…)";
    const badge = $("tgBadge");
    badge.className = "badge rounded-pill " +
      (data.telegram_configured ? "text-bg-success" : "text-bg-secondary");
    badge.textContent = data.telegram_configured ? "On" : "Off";
  } catch {
    /* non-fatal */
  }
}

async function saveNotificationSettings() {
  await withBusy($("tgSave"), async () => {
    const body = {};
    const token = $("tgToken").value.trim();
    const chat = $("tgChat").value.trim();
    if (token) body.bot_token = token;
    body.chat_id = chat;   // empty clears the chat id deliberately
    try {
      await api("/api/settings/notifications", { method: "PUT", body });
      $("tgToken").value = "";
      await Promise.all([loadNotificationSettings(), refreshConfig()]);
      setStatus("tgStatus", "Telegram settings saved.", "ok");
    } catch (error) {
      setStatus("tgStatus", error.message, "error");
    }
  });
}

async function testNotification() {
  await withBusy($("tgTest"), async () => {
    setStatus("tgStatus", "Sending a test message…", "");
    try {
      const result = await api("/api/settings/notifications/test", { method: "POST" });
      // The chat id may have been auto-corrected to the -100… channel form.
      $("tgChat").value = result.chat_id || $("tgChat").value;
      setStatus(
        "tgStatus",
        `Test message sent to ${result.chat_id}` +
          (result.bot_username ? ` via @${result.bot_username}` : "") + ".",
        "ok",
      );
      await loadNotificationSettings();
    } catch (error) {
      setStatus("tgStatus", error.message, "error");
    }
  });
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
}

function init() {
  initDates();

  $("saveKey").addEventListener("click", saveKey);
  $("clearKey").addEventListener("click", clearKey);
  $("testKey").addEventListener("click", testKey);
  $("archiveQueries").addEventListener("click", archiveQueries);
  $("toggleKey").addEventListener("click", () => {
    const input = $("apiKey");
    const hidden = input.type === "password";
    input.type = hidden ? "text" : "password";
    $("toggleKey").textContent = hidden ? "Hide" : "Show";
  });

  $("queryForm").addEventListener("submit", runQuery);
  $("showSql").addEventListener("click", showSql);
  $("diagnoseBtn").addEventListener("click", diagnose);
  $("downloadBtn").addEventListener("click", download);
  $("showMoreBtn").addEventListener("click", showMore);

  $("saveWatchlistBtn").addEventListener("click", saveWatchlist);
  $("refreshWatchlists").addEventListener("click", loadWatchlists);
  $("refreshSignals").addEventListener("click", loadSignals);
  $("showDismissed").addEventListener("change", loadSignals);
  $("wlSaveBtn").addEventListener("click", saveWatchlistSettings);

  $("tgSave").addEventListener("click", saveNotificationSettings);
  $("tgTest").addEventListener("click", testNotification);
  $("rtSave").addEventListener("click", saveRealtimeSettings);
  $("rtSync").addEventListener("click", syncRealtime);
  $("chain").addEventListener("change", updateRealtimeAvailability);

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".tab")) {
        other.classList.toggle("active", other === tab);
      }
      state.tab = tab.dataset.tab;
      renderTable();
    });
  }

  refreshConfig().then(() => {
    loadWatchlists();
    loadSignals();
    updateRealtimeAvailability();
  });
  loadNotificationSettings();
  loadRealtimeSettings();
  startPolling();
}

document.addEventListener("DOMContentLoaded", init);
