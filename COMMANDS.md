# Command cheat-sheet — automated 6-hourly UPAg Dash ingestion

All commands assume you are in the project folder:

```powershell
cd "C:\Users\syamm\OneDrive\Desktop\Onion Forecasting Intelligence\Onion-Forecasting-Intelligence"
```

## 1. Daily-use commands (the job: `pull_upag_dash.py`)

| Purpose | Command |
|---|---|
| Look without touching anything (zero DB writes) | `.venv\Scripts\python.exe pull_upag_dash.py --dry-run --json` |
| Run the ingestion now (yesterday's data) | `.venv\Scripts\python.exe pull_upag_dash.py --once --json` |
| Re-check last N days (source published late) | `.venv\Scripts\python.exe pull_upag_dash.py --once --days 3 --json` |
| Explicit date window | `.venv\Scripts\python.exe pull_upag_dash.py --once --from 2026-10-21 --to 2026-10-27 --json` |
| Only some states | `.venv\Scripts\python.exe pull_upag_dash.py --once --states MP --json` |
| Full-history reconciliation (2022 → yesterday) | `.venv\Scripts\python.exe pull_upag_dash.py --once --full-history --json` |

Window rule: every run requests **yesterday = today (Asia/Kolkata) − 1**; the 4 runs per day
(00:00 / 06:00 / 12:00 / 18:00) re-check the same date — first inserts, rest skip or correct.

## 2. Scheduled task (`OnionUPAgDash6h`)

| Purpose | Command |
|---|---|
| Install the 6-hourly automation | `.\setup_scheduler_6h.bat` |
| Remove it | `.\remove_scheduler_6h.bat` |
| Check status + next run time | `schtasks /query /tn "OnionUPAgDash6h" /fo LIST` |
| Trigger a run right now | `schtasks /run /tn "OnionUPAgDash6h"` |
| Pause / resume without uninstalling | `schtasks /change /tn "OnionUPAgDash6h" /disable` / `/enable` |

## 3. Verification

```powershell
# Latest 3 dataset rows + their source label
.venv\Scripts\python.exe -c "import sqlite3; conn=sqlite3.connect('data/onion_forecasting_v4.sqlite3'); conn.row_factory=sqlite3.Row; [print(dict(r)) for r in conn.execute('SELECT timestamp, AP_retail_rs_per_metric_quintal, source FROM observations ORDER BY timestamp DESC LIMIT 3')]"

# Last 3 audit entries (one row per run)
.venv\Scripts\python.exe -c "import sqlite3; conn=sqlite3.connect('data/onion_forecasting_v4.sqlite3'); conn.row_factory=sqlite3.Row; [print(dict(r)) for r in conn.execute('SELECT checked_at, source, status, inserted, unchanged FROM ingestion_log ORDER BY id DESC LIMIT 3')]"

# Full offline test suite (31 tests)
.venv\Scripts\python.exe -m pytest -q
```

Other audit locations: `data\corrections_log.csv` (value corrections), `data\snapshots\`
(raw responses, newest 30 kept), `data\pull_upag_dash.lock` (overlap guard — safe to
delete if a run crashed and left a stale lock).

## 4. From-scratch setup (new PC, or .venv deleted)

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install --no-cache-dir fastapi httpx pandas numpy requests python-dotenv pytest
Copy-Item .env.example .env
.venv\Scripts\python.exe -m pytest -q                        # 31 should pass
.venv\Scripts\python.exe pull_upag_dash.py --dry-run --json  # sanity check
.\setup_scheduler_6h.bat                                     # reinstall automation
```

Note: this venv is for ingestion + tests only. Running the forecasting API (`main.py`)
additionally needs `torch` + `chronos` (`pip install torch chronos`).

## 5. Troubleshooting quick map

| Symptom | Check |
|---|---|
| Task exists but runs fail | `.venv` present? `.env` present with `UPAG_DASH_ENABLED=1`? |
| `inserted=0` but data is missing | Raise catch-up depth: `--days 3` (mandi rows publish late) |
| One state failed, others ok | Per-state isolation is by design — see the per-state `error` in `--json` output and `ingestion_log` rows with `status='error'` |
| Source changed column names/UOMs | Pipeline fails loudly (never guesses); inspect the newest snapshot in `data\snapshots\` |
| Value was wrong, now source fixed it | Automatic: next run applies it as a correction + logs to `data\corrections_log.csv` |
