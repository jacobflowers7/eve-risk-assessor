async function loadSystemList() {
  const response = await fetch("/api/systems");
  const systems = await response.json();
  const list = document.getElementById("system-list");
  list.innerHTML = "";
  for (const system of systems) {
    const item = document.createElement("li");
    const score = system.overall_risk_score ?? "—";
    item.textContent = `${system.name} (${system.region}) — Risk: ${score}`;
    item.addEventListener("click", () => loadSystemDetail(system.system_id));
    list.appendChild(item);
  }
}

async function loadSystemDetail(systemId) {
  const detail = document.getElementById("system-detail");
  detail.innerHTML = "<p>Loading…</p>";

  const response = await fetch(`/api/systems/${systemId}`);
  if (!response.ok) {
    detail.innerHTML = "<p>Could not load system data.</p>";
    return;
  }
  const data = await response.json();
  const allTime = data.scores.all_time;
  const thirtyDay = data.scores["30_day"];

  detail.innerHTML = `
    <h2>${data.system.name} (${data.system.region})</h2>
    <p>Last fetched: ${data.system.last_fetched_at ?? "never"}</p>
    <h3>All-time</h3>
    ${renderScoreTable(allTime)}
    <h3>Last 30 days</h3>
    ${renderScoreTable(thirtyDay)}
  `;
}

function renderScoreTable(scores) {
  if (!scores) return "<p>No data yet.</p>";
  return `
    <table>
      <tr><td>Overall Risk</td><td>${scores.overall_risk_score}</td></tr>
      <tr><td>Activity</td><td>${scores.activity_score}</td></tr>
      <tr><td>Camping</td><td>${scores.camping_score}</td></tr>
      <tr><td>Gang Composition</td><td>${scores.gang_composition_score}</td></tr>
      <tr><td>Blop/Drop Susceptibility</td><td>${scores.blop_susceptibility_score}</td></tr>
    </table>
  `;
}

loadSystemList();
