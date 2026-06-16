const REGIONS = ["All", "Catch", "Providence"];

const state = {
  systems: [],
  region: "All",
  query: "",
  selectedSystemId: null,
  sortKey: "thirty_day_overall_risk_score",
  sortDirection: "desc",
  loadingScores: false,
};

const tableColumns = [
  { key: "name", label: "System", type: "system" },
  { key: "thirty_day_overall_risk_score", label: "30d Risk", type: "risk" },
  { key: "all_time_overall_risk_score", label: "All-Time", type: "score" },
  { key: "kill_count_24h", label: "24h", type: "number" },
  { key: "kill_count_7d", label: "7d", type: "number" },
  { key: "kill_count_30d", label: "30d", type: "number" },
  { key: "last_killmail_time", label: "Last Kill", type: "time" },
  { key: "last_fetched_at", label: "Updated", type: "time" },
  { key: "data_confidence", label: "Data", type: "confidence" },
];

const metricRows = [
  ["overall_risk_score", "Overall"],
  ["activity_score", "Activity"],
  ["camping_score", "Camping"],
  ["gang_composition_score", "Gang"],
  ["blop_susceptibility_score", "Drop"],
];

function byId(id) {
  return document.getElementById(id);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function scoreNumber(value) {
  if (value === null || value === undefined) return null;
  const number = Number(value);
  return Number.isNaN(number) ? null : number;
}

function formatScore(value) {
  const number = scoreNumber(value);
  return number === null ? "-" : number.toFixed(1);
}

function formatNumber(value) {
  if (value === null || value === undefined) return "0";
  return Number(value).toLocaleString();
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function riskBand(value) {
  const number = scoreNumber(value);
  if (number === null) return "unknown";
  if (number >= 70) return "high";
  if (number >= 40) return "elevated";
  return "low";
}

function riskLabel(value) {
  const band = riskBand(value);
  if (band === "high") return "High";
  if (band === "elevated") return "Elevated";
  if (band === "low") return "Low";
  return "Unknown";
}

function getSortValue(system, column) {
  if (column.type === "risk") {
    return system.thirty_day_overall_risk_score ?? system.all_time_overall_risk_score;
  }
  return system[column.key];
}

function compareRows(a, b, column) {
  const aValue = getSortValue(a, column);
  const bValue = getSortValue(b, column);
  const direction = state.sortDirection === "asc" ? 1 : -1;

  if (aValue === null || aValue === undefined) return 1;
  if (bValue === null || bValue === undefined) return -1;

  if (column.type === "number" || column.type === "score" || column.type === "risk") {
    return (Number(aValue) - Number(bValue)) * direction;
  }
  if (column.type === "time") {
    return (new Date(aValue).getTime() - new Date(bValue).getTime()) * direction;
  }
  return String(aValue).localeCompare(String(bValue)) * direction;
}

function filteredSystems() {
  const query = state.query.trim().toLowerCase();
  const rows = query
    ? state.systems.filter((system) => system.name.toLowerCase().includes(query))
    : state.systems.slice();
  const column = tableColumns.find((candidate) => candidate.key === state.sortKey);
  if (!column) return rows;
  return rows.sort((a, b) => compareRows(a, b, column));
}

function renderRegions() {
  const tabs = byId("region-tabs");
  tabs.innerHTML = "";

  for (const region of REGIONS) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = region;
    button.className = region === state.region ? "active" : "";
    button.addEventListener("click", () => {
      state.region = region;
      state.selectedSystemId = null;
      renderRegions();
      renderDetailEmpty();
      loadSystems();
    });
    tabs.appendChild(button);
  }
}

function renderRefreshState(text = "") {
  const button = byId("refresh-scores");
  const status = byId("refresh-status");
  button.disabled = state.loadingScores;
  button.textContent = state.loadingScores ? "Loading..." : "Load Scores";
  status.textContent = text;
}

function renderSummary(rows) {
  const scored = rows.filter((system) => system.all_time_overall_risk_score !== null).length;
  const activeRegion = state.region === "All" ? "All regions" : state.region;
  byId("table-summary").textContent = `${activeRegion} / ${rows.length} systems / ${scored} scored`;
}

function renderTable() {
  const table = byId("systems-table");
  const rows = filteredSystems();
  renderSummary(rows);
  table.innerHTML = "";

  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  for (const column of tableColumns) {
    const th = document.createElement("th");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = column.label;
    button.addEventListener("click", () => updateSort(column));
    if (state.sortKey === column.key) {
      const marker = document.createElement("span");
      marker.className = "sort-marker";
      marker.textContent = state.sortDirection === "asc" ? " asc" : " desc";
      button.appendChild(marker);
    }
    th.appendChild(button);
    headerRow.appendChild(th);
  }
  thead.appendChild(headerRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = tableColumns.length;
    td.className = "empty-row";
    td.textContent = "No matching systems";
    tr.appendChild(td);
    tbody.appendChild(tr);
  } else {
    for (const system of rows) {
      tbody.appendChild(renderSystemRow(system));
    }
  }
  table.appendChild(tbody);
}

function updateSort(column) {
  if (state.sortKey === column.key) {
    state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
  } else {
    state.sortKey = column.key;
    state.sortDirection = column.type === "system" ? "asc" : "desc";
  }
  renderTable();
}

function renderSystemRow(system) {
  const tr = document.createElement("tr");
  tr.tabIndex = 0;
  tr.className = system.system_id === state.selectedSystemId ? "selected" : "";
  tr.addEventListener("click", () => selectSystem(system.system_id));
  tr.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectSystem(system.system_id);
    }
  });

  for (const column of tableColumns) {
    const td = document.createElement("td");
    td.appendChild(renderCell(system, column));
    tr.appendChild(td);
  }
  return tr;
}

function renderCell(system, column) {
  if (column.type === "system") return renderSystemCell(system);
  if (column.type === "risk") return renderRiskCell(system);
  if (column.type === "score") return textNode(formatScore(system[column.key]), "score-text");
  if (column.type === "number") return textNode(formatNumber(system[column.key]), "numeric");
  if (column.type === "time") return textNode(formatTime(system[column.key]), "muted");
  if (column.type === "confidence") {
    return textNode(system[column.key] || "unknown", `data-label data-${system[column.key] || "unknown"}`);
  }
  return textNode(system[column.key] ?? "-", "");
}

function textNode(text, className) {
  const span = document.createElement("span");
  span.textContent = text;
  if (className) span.className = className;
  return span;
}

function renderSystemCell(system) {
  const wrapper = document.createElement("div");
  wrapper.className = "system-cell";

  const name = document.createElement("strong");
  name.textContent = system.name;
  wrapper.appendChild(name);

  const meta = document.createElement("span");
  meta.textContent = system.region;
  wrapper.appendChild(meta);
  return wrapper;
}

function renderRiskCell(system) {
  const score = system.thirty_day_overall_risk_score ?? system.all_time_overall_risk_score;
  const band = riskBand(score);
  const wrapper = document.createElement("div");
  wrapper.className = `risk-cell risk-${band}`;

  const topLine = document.createElement("div");
  topLine.className = "risk-line";

  const number = document.createElement("strong");
  number.textContent = formatScore(score);
  topLine.appendChild(number);

  const label = document.createElement("span");
  label.textContent = riskLabel(score);
  topLine.appendChild(label);
  wrapper.appendChild(topLine);

  const bar = document.createElement("div");
  bar.className = "risk-bar";
  const fill = document.createElement("span");
  fill.style.width = `${Math.max(0, Math.min(scoreNumber(score) ?? 0, 100))}%`;
  bar.appendChild(fill);
  wrapper.appendChild(bar);
  return wrapper;
}

async function loadSystems() {
  const table = byId("systems-table");
  const regionParam = state.region === "All" ? "" : `?region=${encodeURIComponent(state.region)}`;
  table.innerHTML = '<tbody><tr><td class="empty-row">Loading systems</td></tr></tbody>';

  try {
    const response = await fetch(`/api/systems${regionParam}`);
    if (!response.ok) throw new Error("System request failed");
    state.systems = await response.json();
    renderTable();
  } catch (err) {
    table.innerHTML = '<tbody><tr><td class="empty-row">Could not load systems</td></tr></tbody>';
  }
}

async function loadScoresForCurrentView() {
  if (state.loadingScores) return;

  state.loadingScores = true;
  renderRefreshState("Refreshing...");

  const regionParam = state.region === "All" ? "" : `?region=${encodeURIComponent(state.region)}`;
  try {
    const response = await fetch(`/api/refresh-all${regionParam}`, { method: "POST" });
    if (!response.ok) throw new Error("Refresh failed");
    const result = await response.json();
    await loadSystems();
    const parts = [];
    if (result.succeeded) parts.push(`${result.succeeded} updated`);
    if (result.failed) parts.push(`${result.failed} failed`);
    if (result.skipped) parts.push(`${result.skipped} fresh`);
    renderRefreshState(parts.join(" / ") || "Up to date");
  } catch (err) {
    renderRefreshState("Refresh failed");
  } finally {
    state.loadingScores = false;
  }
}

function renderDetailEmpty() {
  byId("system-detail").innerHTML = `
    <div class="detail-empty">
      <h2>No system selected</h2>
      <p>Select a system to inspect cached scores and killmails.</p>
    </div>
  `;
}

async function selectSystem(systemId) {
  state.selectedSystemId = systemId;
  renderTable();
  byId("system-detail").innerHTML = `
    <div class="detail-empty">
      <h2>Loading system</h2>
    </div>
  `;

  try {
    const detailResponse = await fetch(`/api/systems/${systemId}`);
    if (!detailResponse.ok) throw new Error("Detail request failed");
    const detailData = await detailResponse.json();

    const killmailResponse = await fetch(`/api/systems/${systemId}/killmails?limit=50`);
    if (!killmailResponse.ok) throw new Error("Killmail request failed");
    const killmails = await killmailResponse.json();

    renderSystemDetail(detailData, killmails);
    await loadSystems();
  } catch (err) {
    byId("system-detail").innerHTML = `
      <div class="detail-empty">
        <h2>System unavailable</h2>
        <p>Cached data could not be loaded.</p>
      </div>
    `;
  }
}

function renderSystemDetail(data, killmails) {
  const system = data.system;
  const thirtyDay = data.scores["30_day"];
  const allTime = data.scores.all_time;
  const riskScore = thirtyDay?.overall_risk_score ?? allTime?.overall_risk_score;
  const band = riskBand(riskScore);

  byId("system-detail").innerHTML = `
    <div class="detail-header">
      <div>
        <span class="region-chip">${escapeHtml(system.region)}</span>
        <h2>${escapeHtml(system.name)}</h2>
      </div>
      <div class="detail-risk risk-${band}">
        <strong>${formatScore(riskScore)}</strong>
        <span>${riskLabel(riskScore)}</span>
      </div>
    </div>
    <section class="detail-section">
      <div class="section-title">
        <h3>Risk Breakdown</h3>
        <span>30d vs all-time</span>
      </div>
      ${renderMetricsTable(thirtyDay, allTime)}
    </section>
    <section class="detail-section">
      <div class="section-title">
        <h3>Recent Killmails</h3>
        <span>${killmails.length} cached</span>
      </div>
      ${renderKillmailTable(killmails)}
    </section>
  `;
}

function renderMetricsTable(thirtyDay, allTime) {
  const rows = metricRows.map(([key, label]) => {
    const recent = thirtyDay?.[key];
    const historic = allTime?.[key];
    return `
      <tr>
        <th>${label}</th>
        <td>${renderMetricValue(recent)}</td>
        <td>${renderMetricValue(historic)}</td>
      </tr>
    `;
  }).join("");

  return `
    <table class="metrics-table">
      <thead>
        <tr>
          <th>Metric</th>
          <th>30d</th>
          <th>All</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderMetricValue(value) {
  const band = riskBand(value);
  const width = Math.max(0, Math.min(scoreNumber(value) ?? 0, 100));
  return `
    <div class="metric-value risk-${band}">
      <span>${formatScore(value)}</span>
      <div class="risk-bar"><span style="width: ${width}%"></span></div>
    </div>
  `;
}

function renderKillmailTable(killmails) {
  if (!killmails.length) {
    return '<p class="empty-note">No killmails stored for this system.</p>';
  }

  const rows = killmails.map((killmail) => `
    <tr>
      <td>${formatTime(killmail.killmail_time)}</td>
      <td>${killmail.victim_ship_type_id ?? "-"}</td>
      <td>${formatNumber(killmail.attacker_count)}</td>
      <td>${killmail.has_capital_attacker ? "Yes" : "No"}</td>
      <td><a href="${escapeHtml(killmail.zkillboard_url)}" target="_blank" rel="noreferrer">Open</a></td>
    </tr>
  `).join("");

  return `
    <div class="killmail-wrap">
      <table class="killmail-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Victim Ship</th>
            <th>Attackers</th>
            <th>Drop</th>
            <th>Link</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function initSearch() {
  byId("system-search").addEventListener("input", (event) => {
    state.query = event.target.value;
    renderTable();
  });
}

function initRefresh() {
  byId("refresh-scores").addEventListener("click", loadScoresForCurrentView);
}

renderRegions();
renderDetailEmpty();
initSearch();
initRefresh();
renderRefreshState();
loadSystems();
