async function loadSystemList() {
  const list = document.getElementById("system-list");
  let response;
  try {
    response = await fetch("/api/systems");
  } catch (err) {
    list.textContent = "Could not load system list.";
    return;
  }
  if (!response.ok) {
    list.textContent = "Could not load system list.";
    return;
  }
  const systems = await response.json();
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

  detail.innerHTML = "";

  const heading = document.createElement("h2");
  heading.textContent = `${data.system.name} (${data.system.region})`;
  detail.appendChild(heading);

  const lastFetched = document.createElement("p");
  lastFetched.textContent = `Last fetched: ${data.system.last_fetched_at ?? "never"}`;
  detail.appendChild(lastFetched);

  const allTimeHeading = document.createElement("h3");
  allTimeHeading.textContent = "All-time";
  detail.appendChild(allTimeHeading);
  detail.appendChild(renderScoreTable(allTime));

  const thirtyDayHeading = document.createElement("h3");
  thirtyDayHeading.textContent = "Last 30 days";
  detail.appendChild(thirtyDayHeading);
  detail.appendChild(renderScoreTable(thirtyDay));
}

function renderScoreTable(scores) {
  if (!scores) {
    const p = document.createElement("p");
    p.textContent = "No data yet.";
    return p;
  }
  const table = document.createElement("table");
  const rows = [
    ["Overall Risk", scores.overall_risk_score],
    ["Activity", scores.activity_score],
    ["Camping", scores.camping_score],
    ["Gang Composition", scores.gang_composition_score],
    ["Blop/Drop Susceptibility", scores.blop_susceptibility_score],
  ];
  for (const [label, value] of rows) {
    const tr = document.createElement("tr");
    const tdLabel = document.createElement("td");
    tdLabel.textContent = label;
    const tdValue = document.createElement("td");
    tdValue.textContent = value;
    tr.appendChild(tdLabel);
    tr.appendChild(tdValue);
    table.appendChild(tr);
  }
  return table;
}

loadSystemList();
