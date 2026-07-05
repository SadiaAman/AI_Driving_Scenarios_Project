// Variant B (unstructured) — natural-language scenario input.
// Sends free text to /generate-from-text; the backend parses it into the same
// ScenarioRequest the structured form produces and reuses the shared generator.
const API_BASE = "http://127.0.0.1:8000";
let latestFilename = null;

// ---- Pipeline status indicator (honest: pending -> done / failed) ----
const PIPELINE_STEPS = ["pl-parse", "pl-generate", "pl-validate", "pl-refine", "pl-export"];

function setPipelineStep(id, state) {
  // state: "pending" | "running" | "done" | "failed"
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("pending", "running", "done", "failed");
  el.classList.add(state);
  el.querySelector("b").textContent =
    state === "done" ? "✓" : state === "failed" ? "✗" : state === "running" ? "◉" : "○";
}
function resetPipeline() {
  PIPELINE_STEPS.forEach(id => setPipelineStep(id, "pending"));
  document.getElementById("parsedPanel").classList.add("hidden");
  const llmNote = document.querySelector("#pl-refine .pl-note");
  if (llmNote) llmNote.textContent = "not used";
}

function setLlmParseStep(parserUsed, success) {
  const llmNote = document.querySelector("#pl-refine .pl-note");
  if (parserUsed === "gemini") {
    setPipelineStep("pl-refine", success ? "done" : "failed");
    if (llmNote) llmNote.textContent = "via Gemini";
  } else {
    // Rule-based: the LLM parsing step was not exercised — leave pending.
    if (llmNote) llmNote.textContent = "rule-based";
  }
}

// ---- Parsed-parameter preview ----
const PARSED_FIELDS = [
  ["egoSpeed", "Ego speed (km/h)"],
  ["trafficVehicles", "Traffic vehicles"],
  ["timeOfDay", "Time of day"],
  ["weather", "Weather"],
  ["npcType", "NPC / actor type"],
  ["npcSpeed", "NPC speed (km/h)"],
  ["npcBehavior", "NPC behaviour"],
  ["egoResponse", "Ego response"]
];

function renderParsed(parsed) {
  const rows = PARSED_FIELDS.map(([key, label]) => {
    const v = parsed && parsed[key] != null ? parsed[key] : "—";
    return `<tr><td>${label}</td><td>${v}</td></tr>`;
  }).join("");
  document.getElementById("parsedTable").innerHTML =
    `<tr><th>Parameter</th><th>Understood as</th></tr>${rows}`;
}

function promptFromParsed(p) {
  return `The ego vehicle is travelling at ${p.egoSpeed} km/h `
    + `in ${p.weather} conditions during ${String(p.timeOfDay).toLowerCase()}. `
    + `A ${p.npcType} travelling at ${p.npcSpeed} km/h ${p.npcBehavior}. `
    + `The ego vehicle ${p.egoResponse}.`;
}

function showClarifications(messages) {
  const box = document.getElementById("clarifyBox");
  box.innerHTML = "<strong>Please refine your description:</strong><ul>"
    + messages.map(m => `<li>${m}</li>`).join("") + "</ul>";
  box.classList.remove("hidden");
}

async function generateFromText() {
  const text = document.getElementById("sceneText").value.trim();
  document.getElementById("clarifyBox").classList.add("hidden");
  if (!text) {
    document.getElementById("logBox").textContent = "✗ Please describe the scenario before generating.";
    return;
  }

  resetPipeline();
  setPipelineStep("pl-parse", "running");
  document.getElementById("resultCard").classList.add("hidden");
  document.getElementById("logBox").textContent = "› Parsing your description...";

  try {
    const res = await fetch(`${API_BASE}/generate-from-text`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, autoValidate: document.getElementById("autoValidate").checked })
    });
    const result = await res.json();

    document.getElementById("logBox").textContent = (result.pipeline_log || []).join("\n");

    // Always show parsed params after any attempt (success or failure).
    renderParsed(result.parsed);
    document.getElementById("parsedPanel").classList.remove("hidden");

    if (!result.ok) {
      // Parsing failed — show clarifications, leave result card hidden.
      setPipelineStep("pl-parse", "failed");
      setLlmParseStep(result.parserUsed || "rule-based", false);
      showClarifications(result.messages || ["Could not understand the description."]);
      return;
    }

    // Success: parsed + generated.
    setPipelineStep("pl-parse", "done");
    setPipelineStep("pl-generate", "done");
    setPipelineStep("pl-export", "done");
    setLlmParseStep(result.parserUsed || "rule-based", true);
    // pl-validate stays pending until Run in esmini.

    document.getElementById("promptPreview").textContent = promptFromParsed(result.parsed);
    document.getElementById("xmlPreview").textContent = result.xosc_preview;

    latestFilename = result.filename;
    const fileLink = document.getElementById("fileLink");
    fileLink.textContent = latestFilename;
    fileLink.href = `${API_BASE}/download-xosc?variant=unstructured`;
    document.getElementById("fileMeta").textContent =
      `Generated just now · ${result.actors} actors · ${result.npcSpeed} km/h NPC · fabriksgatan road`;

    const card = document.getElementById("resultCard");
    card.classList.remove("hidden");
    card.scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    setPipelineStep("pl-parse", "failed");
    document.getElementById("logBox").textContent +=
      `\n✗ Error: ${err.message}\nMake sure the backend is running: uvicorn main:app --reload`;
  }
}

document.getElementById("generateBtn").addEventListener("click", generateFromText);

document.getElementById("exampleBtn").addEventListener("click", () => {
  document.getElementById("sceneText").value =
    "The ego vehicle is travelling at 50 km/h during day. A pedestrian crosses the road, and the ego vehicle brakes immediately.";
  document.getElementById("sceneText").focus();
});

document.getElementById("runBtn").addEventListener("click", async () => {
  document.getElementById("logBox").textContent += `\n› Starting esmini with ${latestFilename || "the latest scenario"}...`;
  try {
    const res = await fetch(`${API_BASE}/run-esmini?variant=unstructured`, { method: "POST" });
    const result = await res.json();
    document.getElementById("logBox").textContent += `\n${result.message}`;
    if (res.ok) setPipelineStep("pl-validate", "done");
  } catch (err) {
    document.getElementById("logBox").textContent += `\n✗ Could not run esmini: ${err.message}`;
  }
});

// Run in esmini AND capture its window to an MP4 (unstructured folder).
// Keep the esmini window visible on-screen during recording.
document.getElementById("recordBtn").addEventListener("click", async () => {
  if (!latestFilename) {
    alert("Generate a scenario first, then Run and record video.");
    return;
  }
  const recordBtn = document.getElementById("recordBtn");
  const original = recordBtn.textContent;
  recordBtn.disabled = true;
  recordBtn.textContent = "Recording scenario…";
  document.getElementById("downloadVideoBtn").classList.add("hidden");
  document.getElementById("logBox").textContent +=
    `\n› Recording ${latestFilename} in esmini… keep the esmini window visible and on top.`;
  try {
    const res = await fetch(`${API_BASE}/record-esmini`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variant: "unstructured", filename: latestFilename })
    });
    const result = await res.json();
    if (!res.ok) throw new Error(result.detail || "Recording failed");
    document.getElementById("logBox").textContent += `\n✓ Video saved: ${result.videoFilename}`;
    const dl = document.getElementById("downloadVideoBtn");
    dl.classList.remove("hidden");
    dl.onclick = () => window.open(
      `${API_BASE}/download-video?variant=unstructured&filename=${encodeURIComponent(result.videoFilename)}`,
      "_blank"
    );
  } catch (err) {
    document.getElementById("logBox").textContent += `\n✗ Video recording failed: ${err.message}`;
  } finally {
    recordBtn.disabled = false;
    recordBtn.textContent = original;
  }
});

document.getElementById("exportBtn").addEventListener("click", () => {
  window.open(`${API_BASE}/download-xosc?variant=unstructured`, "_blank");
});
document.getElementById("exportXodrBtn").addEventListener("click", () => {
  window.open(`${API_BASE}/download-xodr`, "_blank");
});

document.getElementById("newScenarioBtn").addEventListener("click", () => {
  document.getElementById("sceneText").value = "";
  document.getElementById("clarifyBox").classList.add("hidden");
  document.getElementById("resultCard").classList.add("hidden");
  document.getElementById("logBox").textContent = "Waiting for scenario description...";
  latestFilename = null;
  resetPipeline();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

// Start honest: nothing done yet.
resetPipeline();
