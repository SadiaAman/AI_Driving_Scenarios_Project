# Project Context — AI Driving Scenarios (Narrative XAI Prototype)

_Last updated: 2026-06-19_

## What this project is

A prototype that generates **OpenSCENARIO 1.2 `.xosc`** driving scenarios and runs
them in **esmini**. It is built for a **user study** comparing two input variants:

- **Variant A — Structured** (`frontend/index.html` + `app.js`): dropdown form
  (ego speed, traffic vehicles, time of day, weather, NPC type, NPC behaviour,
  ego response, NPC speed). Builds a generated-prompt preview and a readiness
  checklist.
- **Variant B — Unstructured** (`frontend/variant-b.html` + `app_b.js`): free-text
  natural-language description. The backend **rule-based parser** (regex + keyword
  maps, NOT Gemini) extracts the same parameters and feeds the **same shared
  generator**.

Both variants share **one XOSC generator** (`build_xosc_preview`) and **one esmini
runner** in the FastAPI backend.

## Key paths

- Backend: `02_Frontend_Code/narrative-xai-prototype/backend/main.py`
- Builder helpers: `.../backend/scenario_builder.py` (scenariogeneration-based)
- Frontend: `.../frontend/{index.html, app.js, variant-b.html, app_b.js, style.css}`
- Generated output: `.../backend/generated/`
- Fixed road: `fabriksgatan.xodr` (logic) + `fabriksgatan.osgb` (scene graph),
  bundled under `esmini-demo/resources/{xodr,models}`
- XOSC archive: `05_XOSC_Files/`

## Architecture / stack

- FastAPI backend; Pydantic `ScenarioRequest` (extra fields ignored, so an old
  frontend sending `laneCount` won't break anything).
- `scenariogeneration` library used for Environment, Entities, and Ego-Init blocks
  (migrated slice-by-slice); the rest of the XOSC is still assembled as XML strings.
- Rule-based NL parser for Variant B (`parse_scenario_text`) → `/generate-from-text`.
- Endpoints: `/generate`, `/generate-from-text`, `/run-esmini`, `/download-xosc`,
  `/download-xodr`, refine/improve paths.
- Pipeline status indicator (pending → done/failed): parse, generate, validate,
  refine, export.

## Major change just completed: "Number of lanes" feature REMOVED

Every scenario now uses **one fixed road: `fabriksgatan.xodr`**. Users no longer
choose lane count.

- `main.py`: removed `laneCount` from `ScenarioRequest`; replaced dynamic road
  selection with `road_files_for()` that always returns fabriksgatan
  (`.xodr` + `.osgb`); **deleted** the dynamic N-lane generator
  `build_generated_road_xosc()` and the dispatcher in `build_xosc_preview()`;
  removed the `LaneCount` `<ParameterDeclaration>`, the `laneCount` result field,
  and lane-count pipeline-log lines; removed lane/road-type extraction from
  `parse_scenario_text`.
- `scenario_builder.py`: deleted `build_lane_road_xodr()` and its now-unused
  `xodr`/`Path` imports.
- Frontend: removed the "Number of lanes" dropdown (`index.html`), lanes
  field/checklist/prompt (`app.js`), and lanes parsed-row/prompt/result-meta
  (`app_b.js`). Added note: "All scenarios use the fixed fabriksgatan road."
- Cleanup: deleted orphaned cached `generated/roads/road_*lane.xodr` files.

### Validated
- Variant A + B both generate XOSC with `<LogicFile>` = `fabriksgatan.xodr`,
  no `LaneCount` param.
- esmini loads the generated file headless with **rc 0, no errors**.
- Export XODR serves `fabriksgatan.xodr`.
- Refine + clarification/error paths still work.
- JS syntax OK; zero stray lane-count references.

## Constraints (still in effect)

- Repo must be **PRIVATE** and only on the user's own account (SadiaAman remote
  is the only authorized remote).
- `backend/.env` holds a **real Gemini API key**, is git-ignored, must NEVER be
  committed. `backend/.env.example` (placeholder) is the only committed env file.
- Variant B parser must stay **rule-based** for the first implementation.

## Git status

Lane-removal edits are staged as working-tree changes, **not yet committed**.
Pre-existing modified files: `02_Frontend_Code/.../generated/scenario_v1.xosc`,
`05_XOSC_Files/scenario_v1.xosc`.

## Next implementation steps

1. **Commit the lane-count removal** (awaiting user approval). Suggested message:
   "Remove lane-count feature; always use fixed fabriksgatan road."
2. **End-to-end manual test in the running app** (restart `uvicorn main:app
   --reload`, hard-refresh both pages): confirm no lanes field, both variants
   generate on fabriksgatan, Run in esmini shows the map, Export XODR downloads
   fabriksgatan.xodr.
3. **Continue scenariogeneration migration** (optional): more XOSC blocks
   (storyboard/triggers) from manual XML strings to the library, validating each
   slice against esmini.
4. **Variant B parser hardening**: broaden keyword/regex coverage and
   clarification messages as study test inputs reveal gaps.
5. **User-study prep**: finalize both variant pages, instructions, and logging.
