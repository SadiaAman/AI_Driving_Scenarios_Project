# Narrative XAI Driving Scenario Prototype

This prototype has:
- Frontend UI based on the Figma screenshot
- FastAPI backend
- XOSC-like scenario file generation
- Export button
- Placeholder endpoint for running esmini

## 1. Run backend

```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Backend runs at: http://127.0.0.1:8000

## 2. Run frontend

Open `frontend/index.html` directly in Chrome/Edge.

## 3. Connect esmini later

Download esmini release/binaries. Then set environment variable:

Windows PowerShell:
```powershell
$env:ESMINI_EXE="C:\path\to\esmini.exe"
uvicorn main:app --reload
```

Then click `Run in esmini` in the UI.

## 4. Next improvement

Replace `build_xosc_preview()` in `backend/main.py` with real OpenSCENARIO generation using the `scenariogeneration` Python package.
