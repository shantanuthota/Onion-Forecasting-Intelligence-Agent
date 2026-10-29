STEP-BY-STEP: Start the server + frontend

Prerequisite: terminal in the project folder:

    cd "C:\Users\syamm\OneDrive\Desktop\Onion Forecasting Intelligence\Onion-Forecasting-Intelligence"

---
STEP 1: Start the backend (this also enables the 6-hourly task)
---

    python backend.py

What happens:
- FastAPI server starts on http://127.0.0.1:8000
- Ingestion task "OnionUPAgDash6h" is enabled (runs 00:00 / 06:00 / 12:00 / 18:00)
- If .env has UPAG_DASH_ENABLED=0, task stays untouched; server still starts

Keep this terminal open. Wait for line: "Starting API on http://127.0.0.1:8000"

---
STEP 2: Verify the backend is alive
---
In a NEW terminal (same folder):

    python -c "import httpx; r=httpx.get('http://127.0.0.1:8000/health', timeout=5); print('Status:', r.status_code); print(r.json()['status'])"

Expected: Status: 200, status: ok
Also check the ingestion task is enabled:

    schtasks /query /tn "OnionUPAgDash6h" /fo LIST | Select-String Status,Next Run

---
STEP 3: Start the frontend (Streamlit dashboard)
---
In another NEW terminal (same folder):

    .\start_dashboard.bat

Or directly:

    .venv\Scripts\python.exe -m streamlit run dashboard.py

The browser opens automatically. The dashboard reads live data from the backend (port 8000) and displays the 3 forecast graphs.

---
STEP 4: Stop cleanly (when done)
---
1. Frontend: press Ctrl+C in its terminal (or close the browser tab); it does NOT affect the backend.
2. Backend: press Ctrl+C in the backend terminal. This stops the server AND disables the ingestion task automatically.

If the backend crashes unexpectedly (hard kill), the ingestion task may stay enabled. To fix: run `python backend.py --smoke-test` (tests the cycle) or manually disable with `schtasks /change /tn "OnionUPAgDash6h" /disable`.

---
QUICK ALTERNATIVE (no frontend, just the API)
---
If you only want the data / ingestion and not the visual dashboard:

    python backend.py              # that's it — the graphs are served at /api/graphs/*

The ingestion runs in the background regardless of whether you open any graph.
