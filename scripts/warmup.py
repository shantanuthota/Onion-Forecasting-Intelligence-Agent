#!/usr/bin/env python3
"""Post-deploy warm-up script: hits all common forecast endpoints to populate
prediction cache and confirm the pipeline is healthy (latency prevention).
Run after every server restart or deployment: python scripts/warmup.py
"""
import requests
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = __import__("os").getenv("API_BEARER_TOKEN", "").strip()
BASE = __import__("os").getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

ENDPOINTS = [
    ("/api/graphs/ap-retail", {"price_types": "retail", "start_date": "2026-10-01", "end_date": "2026-11-15"}),
    ("/api/graphs/state-comparison", {"states": "AP", "price_types": "retail", "start_date": "2026-10-01", "end_date": "2026-11-15"}),
    ("/api/graphs/supply-balance", {"states": "AP", "start_date": "2026-10-01", "end_date": "2026-11-15"}),
]

if not TOKEN:
    print("ERROR: API_BEARER_TOKEN not set in .env. Set it first.")
    sys.exit(1)

headers = {"Authorization": f"Bearer {TOKEN}"}
for path, params in ENDPOINTS:
    url = f"{BASE}{path}"
    t0 = time.time()
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=300)
        resp.raise_for_status()
        body = resp.json()
        forecast_len = len(body.get("forecast", []))
        print(f"OK  {path}  |  forecast_rows={forecast_len}  |  time={time.time()-t0:.1f}s")
    except Exception as exc:
        print(f"FAIL {path}  |  {exc}  |  time={time.time()-t0:.1f}s")

print("\nWarm-up complete. Prediction cache should now contain common patterns.")
