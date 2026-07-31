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
  await withBusy($("showSql"), async () => {
    try {
      const { sql } = await api("/api/sql", { method: "POST", body: readRequest() });
      $("sqlOut").textContent = sql;
      $("sqlCard").classList.remove("hidden");
      setStatus("runStatus", "", "");
    } catch (error) {
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
    setStatus("runStatus", "Asking Dune which balance tables your key can see…", "");
    try {
      // No pattern: the backend searches Dune's curated balance schemas.
      // A free-text search drowns in per-contract decoded tables.
      const data = await api("/api/discover");
      const reachable = new Set(data.tables);
      const lines = [
        `Curated balance tables your key can reach: ${data.count}` +
          (data.truncated ? " (list truncated)" : ""),
        "",
        "What DICE expects:",
        ...data.expected_by_dice.map(
          (t) => `  ${reachable.has(t) ? "OK     " : "MISSING"}  ${t}`,
        ),
        "",
        "All matching tables:",
        ...(data.tables.length ? data.tables.map((t) => `  ${t}`) : ["  (none)"]),
      ];
      $("sqlOut").textContent = lines.join("\n");
      $("sqlCard").classList.remove("hidden");
      setStatus("runStatus", "", "");
    } catch (error) {
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

  await withBusy($("runBtn"), async () => {
    setStatus("runStatus", "Running on Dune — this can take a few minutes…", "");
    try {
      const data = await api("/api/holders", { method: "POST", body: readRequest() });
      state.jobId = data.job_id;
      state.preview = data.preview;
      state.summary = data.summary_preview;
      renderResults(data);
      setStatus("runStatus", "Done.", "ok");
    } catch (error) {
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
    (data.truncated ? " · row cap reached, export is truncated" : "");
  $("previewNote").textContent =
    data.row_count > state.preview.length
      ? `Showing the first ${state.preview.length.toLocaleString()} rows — ` +
        "download for the full result."
      : "";
  renderTable();
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
  // Plain navigation: no key in the URL, the file streams straight from /api/export.
  window.location.href = `/api/export/${state.jobId}?format=${format}`;
}

async function withBusy(button, work) {
  button.disabled = true;
  try {
    await work();
  } finally {
    button.disabled = false;
  }
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
  $("showSql").addEventListener("click", showSql);
  $("diagnoseBtn").addEventListener("click", diagnose);
  $("downloadBtn").addEventListener("click", download);

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".tab")) {
        other.classList.toggle("is-active", other === tab);
      }
      state.tab = tab.dataset.tab;
      renderTable();
    });
  }

  api("/api/config")
    .then((config) => {
      if (config.server_key_configured && !currentKey()) {
        setStatus(
          "keyStatus",
          "A server-side key is configured; you can run queries without entering one.",
          "",
        );
      }
    })
    .catch(() => {});
}

document.addEventListener("DOMContentLoaded", init);
