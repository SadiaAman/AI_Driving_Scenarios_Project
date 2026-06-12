const API_BASE = "http://127.0.0.1:8000";
const fields = ["egoSpeed", "trafficVehicles", "weather", "npcType", "npcBehavior", "egoResponse"];

function getData() {
  return Object.fromEntries(fields.map(id => [id, document.getElementById(id).value]));
}

function isFormComplete(d = getData()) {
  return fields.every(id => d[id]);
}

function makePrompt(d = getData()) {
  if (!isFormComplete(d)) {
    return "Please select all scenario parameters to generate a prompt.";
  }

  return `The ego vehicle travelling at ${d.egoSpeed} km/h in ${d.weather} conditions. A ${d.npcType} ${d.npcBehavior}. The ego vehicle ${d.egoResponse}.`;
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
    { label: "Weather conditions set", done: !!d.weather },
    { label: "NPC actor specified", done: !!d.npcType },
    { label: "NPC behaviour described", done: !!d.npcBehavior },
    { label: "Ego response defined", done: !!d.egoResponse }
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
updatePreview();

async function generateScenario() {
  const data = getData();

  if (!isFormComplete(data)) {
    document.getElementById("logBox").textContent = "Please complete all required fields before generating a scenario.";
    return;
  }

  const prompt = makePrompt(data);

  document.getElementById("logBox").textContent = `› Parsing prompt: "${prompt}"
› Sending structured prompt to backend...`;

  try {
    const res = await fetch(`${API_BASE}/generate-scenario`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...data, prompt })
    });

    const result = await res.json();

    if (!res.ok) {
      throw new Error(result.detail || "Backend error");
    }

    document.getElementById("logBox").textContent = result.pipeline_log.join("\n");
    document.getElementById("resultCard").classList.remove("hidden");
    document.getElementById("fileName").textContent = result.filename;
    document.getElementById("fileMeta").textContent = `Generated just now · ${result.actors} actors · ${result.duration}s duration`;
    document.getElementById("xmlPreview").textContent = result.xosc_preview;
    document.getElementById("resultCard").scrollIntoView({ behavior: "smooth" });

  } catch (err) {
    document.getElementById("logBox").textContent += `\n✗ Error: ${err.message}\nMake sure backend is running: uvicorn main:app --reload`;
  }
}

document.getElementById("generateBtn").addEventListener("click", generateScenario);

document.getElementById("runBtn").addEventListener("click", async () => {
  document.getElementById("logBox").textContent += "\n› Starting esmini...";

  try {
    const res = await fetch(`${API_BASE}/run-esmini`, { method: "POST" });
    const result = await res.json();
    document.getElementById("logBox").textContent += `\n${result.message}`;
  } catch (err) {
    document.getElementById("logBox").textContent += `\n✗ Could not run esmini: ${err.message}`;
  }
});

document.getElementById("exportBtn").addEventListener("click", () => {
  window.open(`${API_BASE}/download-xosc`, "_blank");
});

document.getElementById("refineBtn").addEventListener("click", () => {
  const text = document.getElementById("refineText").value.trim();

  if (!text) {
    return alert("Write a small refinement instruction first.");
  }

  document.getElementById("logBox").textContent += `\n› Refinement note saved: ${text}\n› In the next version, this can be sent to Claude/OpenAI for LLM refinement.`;
});

document.getElementById("improveBtn").addEventListener("click", () => {
  alert("Prototype idea: this button can ask the LLM to make actor behavior more realistic.");
});

document.getElementById("compareBtn").addEventListener("click", () => {
  alert("Prototype idea: save scenario_v1, scenario_v2 and compare changed parameters.");
});