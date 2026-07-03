const REGIONS = ["All", "Catch", "Providence"];

// GitHub Pages build: no backend, data comes from pre-rendered JSON published
// on a schedule. The flag is injected into index.html by scripts/publish_static.py.
const STATIC_MODE = Boolean(window.EVE_STATIC);

const state = {
  systems: [],
  region: "All",
  query: "",
  iceOnly: false,
  selectedSystemId: null,
  sortKey: "thirty_day_overall_risk_score",
  sortDirection: "desc",
  loadingScores: false,
  refreshingSystem: false,
};

const tableColumns = [
  { key: "_rank", label: "#", type: "rank", tooltip: "Rank under the current sort" },
  { key: "name", label: "System", type: "system", tooltip: "Solar system / region" },
  {
    key: "thirty_day_overall_risk_score", label: "30d Risk", type: "risk",
    tooltip: "Composite risk percentile vs all tracked systems, last 30 days. Falls back to cached-history score when no 30d data.",
  },
  {
    key: "thirty_day_hunter_score", label: "Hunters", type: "hunt",
    tooltip: "Solo / 2-3 pilot gang kills as a share of the last 30 days, discounted when only a few kills are on record — the profile that catches miners and ratters",
  },
  {
    key: "all_time_overall_risk_score", label: "History", type: "score",
    tooltip: "Composite risk percentile over all cached killmails",
  },
  { key: "gate_count", label: "Gates", type: "number", tooltip: "Stargate connections — more gates, more through-traffic" },
  { key: "kill_count_24h", label: "24h", type: "number", tooltip: "Kills in the last 24 hours" },
  { key: "kill_count_7d", label: "7d", type: "number", tooltip: "Kills in the last 7 days" },
  { key: "kill_count_30d", label: "30d", type: "number", tooltip: "Kills in the last 30 days" },
  { key: "last_killmail_time", label: "Last Kill", type: "time", tooltip: "Most recent cached killmail" },
  { key: "last_fetched_at", label: "Updated", type: "time", tooltip: "When this system was last refreshed from zKillboard" },
  { key: "data_confidence", label: "Data", type: "confidence", tooltip: "Sample-size confidence: 20+ kills high, 5+ medium" },
];

const metricRows = [
  ["overall_risk_score", "Overall", "Weighted percentile composite of the metrics below"],
  ["activity_score", "Activity", "Kill rate (pods excluded), log-scaled so 10 kills/day = 100"],
  ["hunter_score", "Hunters", "Share of kills by solo / 2-3 pilot gangs, discounted for small samples"],
  ["prey_score", "Prey", "Share of victims that were mining or hauling ships, discounted for small samples"],
  ["camping_score", "Camping", "Attacker-corp concentration (small samples discounted) — high means resident campers"],
  ["gang_composition_score", "Gangs", "Share of kills by fleets of 10+ pilots, discounted for small samples"],
  ["blop_susceptibility_score", "Drops", "Share of kills involving capital or black-ops attackers, discounted for small samples"],
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

function formatRelativeTime(value) {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "never";
  const minutes = Math.floor((Date.now() - date.getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
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

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`Request failed: ${url}`);
  return response.json();
}

function getSortValue(system, column) {
  if (column.type === "risk") {
    return system.thirty_day_overall_risk_score ?? system.all_time_overall_risk_score;
  }
  if (column.type === "hunt") {
    return system.thirty_day_hunter_score ?? system.all_time_hunter_score;
  }
  return system[column.key];
}

function compareRows(a, b, column) {
  const aValue = getSortValue(a, column);
  const bValue = getSortValue(b, column);
  const direction = state.sortDirection === "asc" ? 1 : -1;

  if (aValue === null || aValue === undefined) return 1;
  if (bValue === null || bValue === undefined) return -1;

  if (["number", "score", "risk", "hunt"].includes(column.type)) {
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
  button.textContent = state.loadingScores ? "Loading..." : "Refresh All";
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
    if (column.tooltip) th.dataset.tip = column.tooltip;
    if (column.type === "rank") {
      th.className = "no-sort";
      th.textContent = column.label;
      headerRow.appendChild(th);
      continue;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = column.label;
    button.addEventListener("click", () => updateSort(column));
    if (state.sortKey === column.key) {
      const marker = document.createElement("span");
      marker.className = "sort-marker";
      marker.textContent = state.sortDirection === "asc" ? " ↓" : " ↑";
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
    rows.forEach((system, index) => {
      tbody.appendChild(renderSystemRow(system, index + 1));
    });
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

function renderSystemRow(system, rank) {
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
    td.appendChild(renderCell(system, column, rank));
    tr.appendChild(td);
  }
  return tr;
}

function renderCell(system, column, rank) {
  if (column.type === "rank") return renderRankCell(rank);
  if (column.type === "system") return renderSystemCell(system);
  if (column.type === "risk") return renderRiskCell(system);
  if (column.type === "hunt") return renderHuntCell(system);
  if (column.type === "score") return textNode(formatScore(system[column.key]), "score-text");
  if (column.type === "number") return textNode(formatNumber(system[column.key]), "numeric");
  if (column.type === "time") return textNode(formatTime(system[column.key]), "muted");
  if (column.type === "confidence") {
    const value = system[column.key] || "unknown";
    const node = textNode(value, `data-label data-${value}`);
    node.dataset.tip = confidenceTips[value] || "";
    return node;
  }
  return textNode(system[column.key] ?? "-", "");
}

function renderRankCell(rank) {
  const span = document.createElement("span");
  span.className = rank <= 3 ? "rank-cell top" : "rank-cell";
  span.textContent = String(rank).padStart(2, "0");
  return span;
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

  const nameLine = document.createElement("div");
  nameLine.className = "name-line";

  const name = document.createElement("strong");
  name.textContent = system.name;
  nameLine.appendChild(name);

  if (system.has_ice_belt) {
    const glyph = document.createElement("span");
    glyph.className = "ice-glyph";
    glyph.dataset.tip = "Ice anomaly system";
    // Crystalline glyph
    glyph.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.2"><path d="M6 1v10M2 3l8 6M2 9l8-6"/></svg>';
    nameLine.appendChild(glyph);
  }
  wrapper.appendChild(nameLine);

  const meta = document.createElement("span");
  meta.className = "meta";
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

function renderHuntCell(system) {
  const score = system.thirty_day_hunter_score ?? system.all_time_hunter_score;
  const span = document.createElement("span");
  span.className = `hunt-value risk-${riskBand(score)}`;
  span.textContent = formatScore(score);
  return span;
}

async function loadSystems() {
  const table = byId("systems-table");
  table.innerHTML = '<tbody><tr><td class="empty-row">Loading systems</td></tr></tbody>';

  try {
    if (STATIC_MODE) {
      let systems = await fetchJson("data/systems.json");
      if (state.region !== "All") systems = systems.filter((s) => s.region === state.region);
      if (state.iceOnly) systems = systems.filter((s) => s.has_ice_belt);
      state.systems = systems;
    } else {
      const params = new URLSearchParams();
      if (state.region !== "All") params.set("region", state.region);
      if (state.iceOnly) params.set("ice_only", "true");
      const qs = params.toString() ? `?${params}` : "";
      state.systems = await fetchJson(`/api/systems${qs}`);
    }
    renderTable();
    updateIceToggleAvailability();
  } catch (err) {
    table.innerHTML = '<tbody><tr><td class="empty-row">Could not load systems</td></tr></tbody>';
  }
}

function updateIceToggleAvailability() {
  // Be honest when no ice data has been curated yet: a filter that silently
  // returns zero rows looks like a bug.
  const input = byId("ice-only");
  const label = input.closest(".ice-toggle");
  const anyIce = state.iceOnly || state.systems.some((s) => s.has_ice_belt);
  input.disabled = !anyIce;
  label.classList.toggle("disabled", !anyIce);
  label.dataset.tip = anyIce
    ? "Show only systems with ice anomalies"
    : "No ice-anomaly data curated yet — needs in-game survey data";
}

function setProgress(completed, total, failed) {
  const wrap = byId("refresh-progress");
  const fill = wrap.querySelector(".fill");
  if (total <= 0) {
    wrap.classList.remove("active");
    fill.style.width = "0%";
    return;
  }
  wrap.classList.add("active");
  const pct = Math.min(100, (completed / total) * 100);
  fill.style.width = `${pct}%`;
  if (failed > 0) {
    wrap.classList.add("has-failures");
    const failPct = (failed / total) * 100;
    wrap.style.setProperty("--fail-pct", `${failPct}%`);
  } else {
    wrap.classList.remove("has-failures");
  }
}

function hideProgress() {
  byId("refresh-progress").classList.remove("active", "has-failures");
}

async function loadScoresForCurrentView() {
  if (state.loadingScores) return;

  state.loadingScores = true;
  renderRefreshState("Starting...");
  setProgress(0, 1, 0);

  const params = new URLSearchParams();
  if (state.region !== "All") params.set("region", state.region);
  const qs = params.toString() ? `?${params}` : "";

  let total = 0;
  let completed = 0;
  let failed = 0;
  let lastFailureReason = "";

  try {
    const response = await fetch(`/api/refresh-all${qs}`, { method: "POST" });
    if (!response.ok || !response.body) throw new Error("Refresh failed");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "start") {
          total = event.total;
          renderRefreshState(total === 0 ? "Up to date" : `0 / ${total}`);
          setProgress(0, Math.max(total, 1), 0);
        } else if (event.type === "progress") {
          completed = event.completed;
          if (!event.ok) {
            failed += 1;
            if (event.error) lastFailureReason = event.error;
          }
          setProgress(completed, total, failed);
          const tail = failed > 0 ? `  -  ${failed} failed` : "";
          renderRefreshState(`${completed} / ${total}${tail}`);
        } else if (event.type === "complete") {
          await loadSystems();
          const parts = [];
          if (event.succeeded) parts.push(`${event.succeeded} updated`);
          if (event.failed) parts.push(`${event.failed} failed`);
          if (event.skipped) parts.push(`${event.skipped} fresh`);
          const headline = parts.join(" / ") || "Up to date";
          renderRefreshState(failed > 0 && lastFailureReason
            ? `${headline}  -  ${lastFailureReason}`
            : headline);
        }
      }
    }
  } catch (err) {
    renderRefreshState("Refresh failed");
  } finally {
    state.loadingScores = false;
    // Re-render so the button re-enables, keeping whatever status message
    // the complete/error path just wrote.
    renderRefreshState(byId("refresh-status").textContent);
    setTimeout(hideProgress, 1500);
  }
}

function renderDetailEmpty() {
  byId("system-detail").innerHTML = `
    <div class="detail-empty">
      <h2>No system selected</h2>
      <p>Select a system to inspect cached scores, activity patterns, and killmails.</p>
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
    if (STATIC_MODE) {
      const bundle = await fetchJson(`data/systems/${systemId}.json`);
      if (state.selectedSystemId !== systemId) return;
      renderSystemDetail(bundle, bundle.killmails, bundle.activity,
        bundle.top_attackers, bundle.top_attackers_window);
      return;
    }

    const [detail, killmails, activity, topAttackers30d] = await Promise.all([
      fetchJson(`/api/systems/${systemId}`),
      fetchJson(`/api/systems/${systemId}/killmails?limit=50`),
      fetchJson(`/api/systems/${systemId}/activity`),
      fetchJson(`/api/systems/${systemId}/top-attackers?window=30_day`),
    ]);

    // Quiet system in the last month? Fall back to the full cached history so
    // the section still names the local residents.
    let topAttackers = topAttackers30d;
    let topAttackersWindow = "last 30 days";
    if (topAttackers.length === 0) {
      topAttackers = await fetchJson(`/api/systems/${systemId}/top-attackers?window=all_time`);
      topAttackersWindow = "cached history";
    }

    if (state.selectedSystemId !== systemId) return; // user clicked elsewhere meanwhile
    renderSystemDetail(detail, killmails, activity, topAttackers, topAttackersWindow);
  } catch (err) {
    byId("system-detail").innerHTML = `
      <div class="detail-empty">
        <h2>System unavailable</h2>
        <p>Cached data could not be loaded.</p>
      </div>
    `;
  }
}

async function refreshSelectedSystem() {
  const systemId = state.selectedSystemId;
  if (!systemId || state.refreshingSystem) return;
  state.refreshingSystem = true;

  const button = byId("detail-refresh");
  if (button) {
    button.disabled = true;
    button.textContent = "Updating...";
  }

  try {
    await fetch(`/api/systems/${systemId}/refresh?force=true`, { method: "POST" });
  } catch (err) {
    // fall through -- reload whatever we have
  } finally {
    state.refreshingSystem = false;
  }
  await Promise.all([loadSystems(), selectSystem(systemId)]);
}

function renderSystemDetail(data, killmails, activity, topAttackers, topAttackersWindow) {
  const system = data.system;
  const thirtyDay = data.scores["30_day"];
  const allTime = data.scores.all_time;
  const riskScore = thirtyDay?.overall_risk_score ?? allTime?.overall_risk_score;
  const band = riskBand(riskScore);
  const summaryRow = state.systems.find((s) => s.system_id === system.system_id);

  const metaBits = [`${system.gate_count ?? 0} gates`];
  if (system.has_ice_belt) metaBits.push("ice anomaly");
  if (summaryRow) metaBits.push(`${formatNumber(summaryRow.kill_count_all_time)} kills cached`);

  byId("system-detail").innerHTML = `
    <div class="detail-header">
      <div>
        <span class="region-chip">${escapeHtml(system.region)}</span>
        <h2>${escapeHtml(system.name)}</h2>
        <p class="detail-meta">${escapeHtml(metaBits.join(" · "))}</p>
      </div>
      <div class="detail-risk risk-${band}">
        <strong>${formatScore(riskScore)}</strong>
        <span>${riskLabel(riskScore)}</span>
      </div>
    </div>
    <div class="detail-actions">
      ${STATIC_MODE ? "" : `<button id="detail-refresh" class="refresh-button small" type="button"
        data-tip="Pull the latest killmails for this system from zKillboard and rescore">Update Now</button>`}
      <span class="detail-updated" data-tip="When this system's killmails were last pulled from zKillboard">
        updated ${escapeHtml(formatRelativeTime(system.last_fetched_at))}</span>
    </div>
    <section class="detail-section">
      <div class="section-title">
        <h3>Activity</h3>
        <span>last 30 days</span>
      </div>
      ${renderSparkline(activity.daily)}
      <div class="section-title heat-title">
        <h3>Danger Hours</h3>
        <span>EVE time (UTC)</span>
      </div>
      ${renderHourHeatmap(activity.hourly)}
    </section>
    <section class="detail-section">
      <div class="section-title">
        <h3>Risk Breakdown</h3>
        <span>30d vs history</span>
      </div>
      ${renderMetricsTable(thirtyDay, allTime)}
    </section>
    <section class="detail-section">
      <div class="section-title">
        <h3>Top Hunters</h3>
        <span>${escapeHtml(topAttackersWindow)}</span>
      </div>
      ${renderTopAttackers(topAttackers)}
    </section>
    <section class="detail-section">
      <div class="section-title">
        <h3>Recent Killmails</h3>
        <span>${killmails.length} cached</span>
      </div>
      ${renderKillmailTable(killmails)}
    </section>
  `;

  if (!STATIC_MODE) {
    byId("detail-refresh").addEventListener("click", refreshSelectedSystem);
  }
}

function renderSparkline(daily) {
  if (!daily || !daily.length) {
    return '<p class="empty-note">No activity data.</p>';
  }
  const max = Math.max(...daily.map((d) => d.kills), 1);
  const total = daily.reduce((sum, d) => sum + d.kills, 0);
  const barWidth = 100 / daily.length;

  const bars = daily.map((d, i) => {
    const height = d.kills === 0 ? 1.5 : Math.max((d.kills / max) * 100, 6);
    const x = i * barWidth;
    const cls = d.kills === 0 ? "spark-bar zero" : "spark-bar";
    const tip = `${d.date} — ${d.kills} ship kill${d.kills === 1 ? "" : "s"}`;
    return `<rect class="${cls}" x="${(x + barWidth * 0.15).toFixed(2)}" y="${(100 - height).toFixed(2)}"
      width="${(barWidth * 0.7).toFixed(2)}" height="${height.toFixed(2)}" data-tip="${escapeHtml(tip)}"></rect>`;
  }).join("");

  return `
    <div class="sparkline-wrap">
      <svg class="sparkline" viewBox="0 0 100 100" preserveAspectRatio="none" role="img"
        aria-label="Kills per day, last 30 days">${bars}</svg>
      <div class="sparkline-legend">
        <span>30d ago</span>
        <span>${total} ship kill${total === 1 ? "" : "s"} · peak ${max}/day</span>
        <span>today</span>
      </div>
    </div>
  `;
}

function renderHourHeatmap(hourly) {
  if (!hourly || hourly.length !== 24) {
    return '<p class="empty-note">No hourly data.</p>';
  }
  const max = Math.max(...hourly, 1);
  const total = hourly.reduce((a, b) => a + b, 0);
  if (total === 0) {
    return '<p class="empty-note">No kills cached yet — no hourly pattern to show.</p>';
  }
  const nowHour = new Date().getUTCHours();

  const cells = hourly.map((count, hour) => {
    const intensity = count / max;
    const nowClass = hour === nowHour ? " now" : "";
    const label = `${String(hour).padStart(2, "0")}:00 EVE — ${count} kill${count === 1 ? "" : "s"}`
      + (hour === nowHour ? " (current EVE hour)" : "");
    return `<div class="heat-cell${nowClass}" style="--heat: ${intensity.toFixed(3)}" data-tip="${escapeHtml(label)}">
      ${hour % 6 === 0 ? `<span class="heat-tick">${String(hour).padStart(2, "0")}</span>` : ""}
    </div>`;
  }).join("");

  return `
    <div class="heatmap" role="img" aria-label="Kills by hour of day, EVE time">${cells}</div>
    <p class="heat-note">Brighter cells = more kills at that hour. Outlined cell is the current EVE hour.</p>
  `;
}

function renderMetricsTable(thirtyDay, allTime) {
  const rows = metricRows.map(([key, label, tooltip]) => {
    const recent = thirtyDay?.[key];
    const historic = allTime?.[key];
    return `
      <tr>
        <th data-tip="${escapeHtml(tooltip)}">${label}</th>
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

function renderTopAttackers(topAttackers) {
  if (!topAttackers || !topAttackers.length) {
    return '<p class="empty-note">No player attackers on record.</p>';
  }
  const max = Math.max(...topAttackers.map((c) => c.kill_count), 1);
  const rows = topAttackers.map((corp) => {
    const name = corp.name || `Corp #${corp.corporation_id}`;
    const width = Math.max((corp.kill_count / max) * 100, 4);
    const zkbUrl = `https://zkillboard.com/corporation/${corp.corporation_id}/`;
    return `
      <li class="attacker-row">
        <a class="attacker-name" href="${escapeHtml(zkbUrl)}" target="_blank" rel="noreferrer"
          data-tip="Open this corporation on zKillboard">${escapeHtml(name)}</a>
        <span class="attacker-last">${formatTime(corp.last_seen)}</span>
        <span class="attacker-kills">${formatNumber(corp.kill_count)}</span>
        <span class="attacker-bar"><span style="width: ${width.toFixed(1)}%"></span></span>
      </li>
    `;
  }).join("");
  return `<ul class="attacker-list">${rows}</ul>`;
}

const confidenceTips = {
  high: "High confidence — 20+ kills sampled for this system",
  medium: "Medium confidence — 5-19 kills sampled; scores are indicative",
  low: "Low confidence — fewer than 5 kills sampled; treat scores as provisional",
  unknown: "Never fetched — use Refresh All or open the system and press Update Now",
};

const victimClassChips = {
  prey: { label: "IND", title: "Industrial / mining victim — direct evidence krabs get caught here" },
  pod: { label: "POD", title: "Capsule kill" },
};

function renderKillmailTable(killmails) {
  if (!killmails.length) {
    return '<p class="empty-note">No killmails stored for this system.</p>';
  }

  const rows = killmails.map((killmail) => {
    const shipLabel = killmail.victim_ship_name
      ? escapeHtml(killmail.victim_ship_name)
      : (killmail.victim_ship_type_id != null ? `#${killmail.victim_ship_type_id}` : "-");
    const chip = victimClassChips[killmail.victim_class];
    const chipHtml = chip
      ? `<span class="class-chip chip-${killmail.victim_class}" data-tip="${escapeHtml(chip.title)}">${chip.label}</span>`
      : "";
    const players = killmail.player_attacker_count ?? killmail.attacker_count;
    const isHunterKill = players >= 1 && players <= 3;
    const gangClass = isHunterKill ? "gang-size hunter" : "gang-size";
    const gangTip = isHunterKill
      ? `${players} player pilot${players === 1 ? "" : "s"} — solo/small-gang hunter kill`
      : `${players} player pilots on the killmail`;
    const rowClass = killmail.victim_class === "pod" ? ' class="pod-row"' : "";
    return `
    <tr${rowClass}>
      <td>${formatTime(killmail.killmail_time)}</td>
      <td class="ship-cell">${shipLabel}${chipHtml}</td>
      <td><span class="${gangClass}" data-tip="${escapeHtml(gangTip)}">${formatNumber(players)}</span></td>
      <td>${killmail.has_capital_attacker ? "Yes" : "No"}</td>
      <td><a href="${escapeHtml(killmail.zkillboard_url)}" target="_blank" rel="noreferrer">Open</a></td>
    </tr>
  `;
  }).join("");

  return `
    <div class="killmail-wrap">
      <table class="killmail-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Victim Ship</th>
            <th data-tip="Player attackers (NPC rats excluded); red = solo/small-gang hunter kill">Pilots</th>
            <th data-tip="Capital or black-ops ship on the attacker list">Drop</th>
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
  if (STATIC_MODE) {
    // No backend to refresh against: hide the button and show data freshness
    // from the publish manifest instead.
    byId("refresh-scores").hidden = true;
    byId("refresh-progress").hidden = true;
    const status = byId("refresh-status");
    const showFreshness = async () => {
      try {
        const manifest = await fetchJson(`data/manifest.json?t=${Date.now()}`);
        status.textContent = `auto-updated ${formatRelativeTime(manifest.generated_at)}`;
        status.dataset.tip = "This site refreshes its killmail data automatically on a schedule";
      } catch (err) {
        status.textContent = "";
      }
    };
    showFreshness();
    setInterval(showFreshness, 5 * 60 * 1000);
    return;
  }
  byId("refresh-scores").addEventListener("click", loadScoresForCurrentView);
}

function initIceToggle() {
  byId("ice-only").addEventListener("change", (event) => {
    state.iceOnly = event.target.checked;
    state.selectedSystemId = null;
    renderDetailEmpty();
    loadSystems();
  });
}

// Single floating tooltip driven by [data-tip] attributes. Native title=""
// tooltips are slow to appear and invisible until hovered for ~1s; this one is
// instant, styled to match the UI, and follows keyboard focus too.
function initTooltips() {
  const tip = document.createElement("div");
  tip.className = "app-tooltip";
  tip.setAttribute("role", "tooltip");
  document.body.appendChild(tip);
  let anchor = null;

  function show(el) {
    const text = el.dataset ? el.dataset.tip : null;
    if (!text) return;
    anchor = el;
    tip.textContent = text;
    tip.classList.add("visible");
    const rect = el.getBoundingClientRect();
    const tipRect = tip.getBoundingClientRect();
    let top = rect.top - tipRect.height - 8;
    if (top < 6) top = rect.bottom + 8;
    let left = rect.left + rect.width / 2 - tipRect.width / 2;
    left = Math.max(6, Math.min(left, window.innerWidth - tipRect.width - 6));
    tip.style.top = `${top}px`;
    tip.style.left = `${left}px`;
  }

  function hide() {
    anchor = null;
    tip.classList.remove("visible");
  }

  document.addEventListener("mouseover", (event) => {
    const el = event.target.closest ? event.target.closest("[data-tip]") : null;
    if (el) {
      if (el !== anchor) show(el);
    } else if (anchor) {
      hide();
    }
  });
  document.addEventListener("focusin", (event) => {
    const el = event.target.closest ? event.target.closest("[data-tip]") : null;
    if (el) show(el);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("scroll", hide, true);
}

function initMethodology() {
  const button = byId("methodology-toggle");
  const popover = byId("methodology");
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    popover.classList.toggle("open");
  });
  document.addEventListener("click", (event) => {
    if (popover.classList.contains("open") && !popover.contains(event.target)) {
      popover.classList.remove("open");
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") popover.classList.remove("open");
  });
}

renderRegions();
renderDetailEmpty();
initSearch();
initRefresh();
initIceToggle();
initMethodology();
initTooltips();
renderRefreshState();
loadSystems();
