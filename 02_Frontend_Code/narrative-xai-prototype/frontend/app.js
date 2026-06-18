const API_BASE = "http://127.0.0.1:8000";
const fields = ["egoSpeed", "trafficVehicles", "timeOfDay", "laneCount", "weather", "npcType", "npcBehavior", "egoResponse", "npcSpeed"];
let latestFilename = null;

// Resolve the NPC speed (km/h) from the dropdown, or the custom input when
// "Custom speed…" is selected. Returns "" if missing/invalid so the form stays
// incomplete until a positive, reasonable value (1–200 km/h) is given.
function resolveNpcSpeed() {
  const sel = document.getElementById("npcSpeed").value;
  if (sel !== "custom") return sel;
  const v = parseFloat(document.getElementById("npcSpeedCustom").value);
  return (Number.isFinite(v) && v >= 1 && v <= 200) ? String(v) : "";
}

// Which behaviours make sense for each actor type. Pedestrians and cyclists
// cross the road; cars and trucks drive ahead and can brake or change lane.
// This keeps the generated prompt text and the actual simulation in sync.
const NPC_BEHAVIOURS = {
  pedestrian: ["crosses the road"],
  cyclist: ["crosses the road"],
  car: ["brakes suddenly", "changes lane suddenly", "cuts in front of ego"],
  truck: ["brakes suddenly", "changes lane suddenly", "cuts in front of ego"]
};

// Rebuild the NPC behaviour dropdown to match the selected actor type.
function updateBehaviourOptions() {
  const npcType = document.getElementById("npcType").value;
  const select = document.getElementById("npcBehavior");
  const previous = select.value;
  const allowed = NPC_BEHAVIOURS[npcType] || [];

  select.innerHTML = '<option value="" selected disabled>Select NPC behaviour</option>';
  for (const behaviour of allowed) {
    const option = document.createElement("option");
    option.value = behaviour;
    option.textContent = behaviour;
    select.appendChild(option);
  }

  // Keep the previous choice if it is still valid for the new actor type.
  if (allowed.includes(previous)) {
    select.value = previous;
  }

  updatePreview();
}

function getData() {
  const d = Object.fromEntries(fields.map(id => [id, document.getElementById(id).value]));
  d.npcSpeed = resolveNpcSpeed();   // override with the resolved/validated value
  return d;
}

function isFormComplete(d = getData()) {
  return fields.every(id => d[id]);
}

function makePrompt(d = getData()) {
  if (!isFormComplete(d)) {
    return "Please select all scenario parameters to generate a prompt.";
  }

  return `The ego vehicle is travelling at ${d.egoSpeed} km/h on a ${d.laneCount}-lane road in ${d.weather} conditions during ${d.timeOfDay.toLowerCase()}. A ${d.npcType} travelling at ${d.npcSpeed} km/h ${d.npcBehavior}. The ego vehicle ${d.egoResponse}.`;
}

function updatePreview() {
  const d = getData();
  const completedCount = fields.filter(id => d[id]).length;
  const percentage = Math.round((completedCount / fields.length) * 100);
  const complete = isFormComplete(d);

  document.getElementById("promptPreview").textContent = makePrompt(d);

  const checklistItems = [
    { label: "Ego vehicle speed selected", done: !!d.egoSpeed },
    { label: "Number of traffic vehicles selected", done: !!d.trafficVehicles },
    { label: "Time of day chosen", done: !!d.timeOfDay },
    { label: "Number of lanes selected", done: !!d.laneCount },
    { label: "Weather conditions set", done: !!d.weather },
    { label: "NPC actor specified", done: !!d.npcType },
    { label: "NPC behaviour described", done: !!d.npcBehavior },
    { label: "Ego response defined", done: !!d.egoResponse },
    { label: "NPC speed set", done: !!d.npcSpeed }
  ];

  document.getElementById("checklist").innerHTML = checklistItems
    .map(item => `<li class="${item.done ? "done" : "pending"}">${item.done ? "✓" : "○"} ${item.label}</li>`)
    .join("");

  document.getElementById("progressBar").style.width = `${percentage}%`;
  document.getElementById("progressText").textContent = `${percentage}%`;

  const progressMessage = document.getElementById("progressMessage");
  if (progressMessage) {
    progressMessage.textContent = complete ? "Ready to generate!" : "Please complete all required fields.";
  }

  const generateBtn = document.getElementById("generateBtn");
  generateBtn.disabled = !complete;
  generateBtn.textContent = complete ? "Generate scenario" : "Complete all fields first";
}

fields.forEach(id => document.getElementById(id).addEventListener("change", updatePreview));
// When the actor type changes, rebuild the behaviour list to match it.
document.getElementById("npcType").addEventListener("change", updateBehaviourOptions);

// Show the custom NPC-speed input only when "Custom speed…" is selected.
function updateNpcSpeedUi() {
  const isCustom = document.getElementById("npcSpeed").value === "custom";
  document.getElementById("npcSpeedCustomRow").classList.toggle("hidden", !isCustom);
}
document.getElementById("npcSpeed").addEventListener("change", () => { updateNpcSpeedUi(); updatePreview(); });
document.getElementById("npcSpeedCustom").addEventListener("input", updatePreview);
updateNpcSpeedUi();

updateBehaviourOptions();
updatePreview();

// Render a generate/refine result into the result card.
// --- Pipeline status indicator -------------------------------------------
// Each step starts "pending" (grey ○) and turns "done" (green ✓) only when it
// has actually happened, so the strip reflects real progress.
const PIPELINE_STEPS = ["pl-parse", "pl-generate", "pl-validate", "pl-refine", "pl-export"];

function setPipelineStep(id, done) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle("done", done);
  el.classList.toggle("pending", !done);
  el.querySelector("b").textContent = done ? "✓" : "○";
}

function resetPipeline() {
  PIPELINE_STEPS.forEach(id => setPipelineStep(id, false));
}

function renderScenarioResult(result) {
  document.getElementById("logBox").textContent = result.pipeline_log.join("\n");
  document.getElementById("resultCard").classList.remove("hidden");
  const fileLink = document.getElementById("fileLink");
  latestFilename = result.filename;
  fileLink.textContent = latestFilename;
  fileLink.href = `${API_BASE}/download-xosc`;
  document.getElementById("fileMeta").textContent = `Generated just now · ${result.actors} actors · ${result.duration}s duration${result.improved ? " · ★ improved behavior" : ""}`;
  document.getElementById("xmlPreview").textContent = result.xosc_preview;
  // A generate/refine just succeeded: prompt parsed, XOSC built, file exportable.
  setPipelineStep("pl-parse", true);
  setPipelineStep("pl-generate", true);
  setPipelineStep("pl-export", true);
  document.getElementById("resultCard").scrollIntoView({ behavior: "smooth" });
}

// Set the form dropdowns to match a resolved parameter set (used after refine).
function syncFormToParams(params) {
  ["egoSpeed", "trafficVehicles", "timeOfDay", "weather", "npcType", "egoResponse"].forEach(k => {
    if (params[k] != null) document.getElementById(k).value = String(params[k]);
  });
  updateBehaviourOptions(); // rebuild behaviour list for the (possibly new) npcType
  if (params.npcBehavior != null) document.getElementById("npcBehavior").value = params.npcBehavior;
  updatePreview();
}

async function generateScenario(improve = false) {
  const data = getData();

  if (!isFormComplete(data)) {
    document.getElementById("logBox").textContent = "Please complete all required fields before generating a scenario.";
    return;
  }

  const prompt = makePrompt(data);

  document.getElementById("logBox").textContent = improve
    ? `› Improving actor behavior for current scenario...`
    : `› Parsing prompt: "${prompt}"
› Sending structured prompt to backend...`;

  try {
    const res = await fetch(`${API_BASE}/generate-scenario`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...data, prompt, improve })
    });

    const result = await res.json();

    if (!res.ok) {
      throw new Error(result.detail || "Backend error");
    }

    renderScenarioResult(result);

  } catch (err) {
    document.getElementById("logBox").textContent += `\n✗ Error: ${err.message}\nMake sure backend is running: uvicorn main:app --reload`;
  }
}

document.getElementById("generateBtn").addEventListener("click", () => generateScenario(false));

document.getElementById("runBtn").addEventListener("click", async () => {
  const filenameMessage = latestFilename ? `latest generated file ${latestFilename}` : "the latest generated file";
  document.getElementById("logBox").textContent += `\n› Starting esmini with ${filenameMessage}...`;

  try {
    const res = await fetch(`${API_BASE}/run-esmini`, { method: "POST" });
    const result = await res.json();
    document.getElementById("logBox").textContent += `\n${result.message}`;
    if (res.ok) setPipelineStep("pl-validate", true);  // validated by running in esmini
  } catch (err) {
    document.getElementById("logBox").textContent += `\n✗ Could not run esmini: ${err.message}`;
  }
});

document.getElementById("exportBtn").addEventListener("click", () => {
  window.open(`${API_BASE}/download-xosc`, "_blank");
});

document.getElementById("exportXodrBtn").addEventListener("click", () => {
  window.open(`${API_BASE}/download-xodr`, "_blank");
});

document.getElementById("refineBtn").addEventListener("click", async () => {
  const text = document.getElementById("refineText").value.trim();
  if (!text) {
    return alert("Write a small refinement instruction first.");
  }
  if (!isFormComplete()) {
    return alert("Generate a scenario first, then refine it.");
  }

  const data = getData();
  const prompt = makePrompt(data);
  document.getElementById("logBox").textContent = `› Applying refinement: "${text}"...`;

  try {
    const res = await fetch(`${API_BASE}/refine-scenario`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...data, prompt, refineText: text })
    });
    const result = await res.json();
    if (!res.ok) {
      throw new Error(result.detail || "Backend error");
    }

    syncFormToParams(result.params);   // reflect the new parameters in the form
    renderScenarioResult(result);      // show the new file + change log
    setPipelineStep("pl-refine", true);  // an LLM/keyword refinement was applied
    document.getElementById("refineText").value = "";
  } catch (err) {
    document.getElementById("logBox").textContent += `\n✗ Refine failed: ${err.message}\nMake sure backend is running: uvicorn main:app --reload`;
  }
});

document.getElementById("improveBtn").addEventListener("click", () => {
  if (!isFormComplete()) {
    alert("Generate a scenario first, then Improve actor behavior.");
    return;
  }
  generateScenario(true);
});

// ---- Compare versions ----
let compareScenarios = [];
const COMPARE_FIELDS = [
  ["egoSpeed", "Ego speed (km/h)"],
  ["trafficVehicles", "Traffic vehicles"],
  ["timeOfDay", "Time of day"],
  ["weather", "Weather"],
  ["npcType", "NPC / actor type"],
  ["npcBehavior", "NPC behaviour"],
  ["egoResponse", "Ego response"],
  ["improved", "Improved behavior"]
];

function fillCompareSelect(select, selectedIndex) {
  select.innerHTML = compareScenarios
    .map((s, i) => `<option value="${i}">${s.filename}</option>`)
    .join("");
  select.selectedIndex = selectedIndex;
}

function renderCompare() {
  const a = compareScenarios[document.getElementById("compareA").value];
  const b = compareScenarios[document.getElementById("compareB").value];
  if (!a || !b) return;

  const rows = COMPARE_FIELDS.map(([key, label]) => {
    const va = a[key] == null ? "—" : a[key];
    const vb = b[key] == null ? "—" : b[key];
    const diff = String(va) !== String(vb) ? "diff" : "";
    return `<tr class="${diff}"><td>${label}</td><td>${va}</td><td>${vb}</td></tr>`;
  }).join("");

  document.getElementById("compareTable").innerHTML =
    `<tr><th>Parameter</th><th>Version A</th><th>Version B</th></tr>${rows}`;
}

document.getElementById("compareBtn").addEventListener("click", async () => {
  try {
    const res = await fetch(`${API_BASE}/list-scenarios`);
    const data = await res.json();
    compareScenarios = data.scenarios || [];

    if (compareScenarios.length < 2) {
      alert("Generate at least two scenarios before comparing.");
      return;
    }

    fillCompareSelect(document.getElementById("compareA"), 0);
    fillCompareSelect(document.getElementById("compareB"), 1);
    const panel = document.getElementById("comparePanel");
    panel.classList.remove("hidden");
    renderCompare();
    panel.scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    alert("Could not load scenarios to compare: " + err.message);
  }
});

document.getElementById("compareA").addEventListener("change", renderCompare);
document.getElementById("compareB").addEventListener("change", renderCompare);

function startNewScenario() {
  // Reset every dropdown back to its placeholder.
  fields.forEach(id => { document.getElementById(id).value = ""; });
  // Reset the custom NPC-speed input and hide its row.
  document.getElementById("npcSpeedCustom").value = "";
  updateNpcSpeedUi();
  // Rebuild the behaviour list (clears it back to "select actor type first")
  // and refresh the prompt preview, checklist and progress bar.
  updateBehaviourOptions();

  // Clear the result card, compare panel and the log/refinement inputs.
  document.getElementById("resultCard").classList.add("hidden");
  document.getElementById("comparePanel").classList.add("hidden");
  document.getElementById("logBox").textContent = "Waiting for scenario generation...";
  document.getElementById("refineText").value = "";
  latestFilename = null;
  resetPipeline();   // back to all-pending

  window.scrollTo({ top: 0, behavior: "smooth" });
}

document.getElementById("newScenarioBtn").addEventListener("click", startNewScenario);

// Start with the pipeline in its honest "nothing done yet" state.
resetPipeline();