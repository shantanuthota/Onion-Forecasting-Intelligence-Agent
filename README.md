# Onion Forecasting MVP — Chronos-2 + FastAPI + Streamlit

This project is a local, runnable MVP using separate daily price and AP monthly quantity datasets. Graph-specific observations are synchronized into SQLite before forecasting.

## What is included

- **Graph 1 API/UI:** Forecasts AP retail, wholesale, and mandi modal prices using the specified AP price and arrival history.
- **Graph 2 API/UI:** Forecasts retail, wholesale, and mandi modal prices independently for AP, MH, KA, and MP using only each selected state's matching price and arrival history.
- **Graph 3 API/UI:** Forecasts AP monthly production, consumption, and arrivals in tonnes/month, then derives shortfall as `consumption - (arrival + production)`.
- **SQLite history store:** Automatically synchronizes the configured daily price CSV and monthly AP CSV into separate graph-specific SQLite tables. Source changes are reloaded on the next graph request. The existing full-schema ingestion store remains available for its existing ingestion workflows.
- **UPAg discovery utility:** Uses credentials from local `.env`, logs into the official UPAg Open API, lists the source names permitted for that account, and can save raw JSON responses when a source-specific request payload is provided.
- **Mocked API tests:** Check request/response structure without downloading Chronos-2 model weights.

## Important honesty/limitations

1. The actual Chronos-2 model has to be installed and its weights downloaded on your machine. The mocked tests do **not** claim real inference was executed here.
2. The configured daily price CSV ends on **2026-10-25**; the monthly AP CSV ends in **October 2026**. Forecast results use the source's daily or monthly frequency.
3. The supplied CSV does not contain weather. This MVP does not yet claim to forecast under El Niño/cyclone scenarios.
4. Graph 3 quantities are monthly totals supplied by the BA team. Validate the definitions of production, consumption, and arrivals before interpreting the resulting shortfall.
5. Shortfall quantile bounds are conservative combinations of individual component quantiles, not a calibrated joint interval.
6. UPAg credentials alone do not prove that a source provides the fields required by the model. First list the sources authorized to the account and inspect the exact response schema. The included UPAg utility fetches **raw data only**; it does not silently map unknown source columns into the model-ready dataset.

## Recommended environment

Use **64-bit Python 3.11** for the MVP. The first real forecast requires internet access to install dependencies and download the public Chronos-2 model weights. CPU inference works but can be slower than a supported CUDA GPU.

## Windows / Antigravity IDE setup

1. Extract the ZIP into a separate working folder, for example `C:\work\onion_forecasting_mvp`, and open that folder in Antigravity IDE.
2. In Antigravity's terminal, run these PowerShell commands from the project root:

```powershell
Copy-Item .env.example .env
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation, use Command Prompt and run `.venv\Scripts\activate.bat`, or execute `.venv\Scripts\python.exe` directly.

3. Start the API in terminal 1:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

4. Open `http://127.0.0.1:8000/health` to verify the API and data store. Then open `http://127.0.0.1:8000/docs`.
5. Start the dashboard in terminal 2:

```powershell
.\.venv\Scripts\python.exe -m streamlit run dashboard.py
```

6. Open the local Streamlit address printed by the terminal, usually `http://localhost:8501`.

**The first Graph 1/2/3 request loads Chronos-2 weights and may take noticeably longer.** Do not repeatedly click Generate while the first model load is running.

## Frontend integration

See [`FRONTEND_INTEGRATION.md`](FRONTEND_INTEGRATION.md) for the JSON contract, supported parameters and examples for the production frontend.

## Direct API checks

```text
GET http://127.0.0.1:8000/api/graphs/ap-retail?horizon_days=7
GET http://127.0.0.1:8000/api/graphs/ap-retail?horizon_days=14
GET http://127.0.0.1:8000/api/graphs/ap-retail?horizon_days=30
GET http://127.0.0.1:8000/api/graphs/ap-retail?horizon_days=60
GET http://127.0.0.1:8000/api/graphs/ap-retail?horizon_days=90&price_types=retail,mandi_modal

GET http://127.0.0.1:8000/api/graphs/state-comparison?states=AP,MH&price_types=retail,wholesale&horizon_days=14
GET http://127.0.0.1:8000/api/graphs/state-comparison?states=KA,MP&price_types=retail,wholesale&horizon_days=30
GET http://127.0.0.1:8000/api/graphs/state-comparison?states=KA,MP&price_types=retail,wholesale&horizon_days=90

GET http://127.0.0.1:8000/api/graphs/supply-balance?states=AP&horizon_days=7
GET http://127.0.0.1:8000/api/ingestion/status
```

## History store and CSV updates

The existing full-schema ingestion store is initialized from its configured legacy CSV when required. Graph requests separately synchronize the new daily and monthly datasets into graph-specific tables in the configured SQLite database (default: `data/onion_forecasting_v4.sqlite3`). The SQLite file is ignored by Git and is created locally.

Graphs 1 and 2 instead read the daily dataset configured by `ONION_DAILY_PRICE_DATA_PATH`; Graph 3 reads the monthly AP dataset configured by `ONION_AP_MONTHLY_DATA_PATH`. The supplied defaults point to the two CSV paths provided for this change. Set these variables in `.env` if the files are stored elsewhere. The API validates each dataset and synchronizes it into `daily_price_observations` or `ap_monthly_observations` in the same SQLite database. These graph endpoints do not use the legacy combined model-ready CSV as their observation source.

If you receive a newer **full combined model-ready** CSV, put it inside `data/` and import it:

```powershell
.\.venv\Scripts\python.exe sync_model_ready_csv.py data\Onion_Model_Ready_Dataset_Chronos2_updated.csv
```

It is an upsert keyed by date; it will not duplicate dates. Supply the full aligned history, not a file containing only the latest row. A row must contain the expected AP/KA/MH/MP fields so the combined date can be validated. After the import, the API reads the new database history and forecast cache keys change automatically.

There is also a secured local admin endpoint for a CSV already placed inside `data/`:

1. Change `INGESTION_API_KEY` in `.env` to a long, unique secret.
2. Place the normalized combined CSV under `data/`.
3. Call `POST /api/ingestion/sync-csv?filename=your_file.csv` with request header `X-Ingestion-Key: <your-secret>`.

Do not expose the local ingestion endpoint to the public internet.

## UPAg Open API discovery

Official docs: https://data.upag.gov.in/docs and https://data.upag.gov.in/redoc

The documented API uses a login at `/v1/upag/api-data-share/login`, then lists account-authorized data sources at `/v1/upag/api-data-share/sources/user-allowed-sources`, and fetches a source with `POST /v1/upag/api-data-share/sources/{source_name}`. Source request objects differ by source.

1. Put the account's authorized credentials in `.env` (`UPAG_USERNAME`, `UPAG_PASSWORD`, plus client ID/secret only if your account documentation requires them). Do not share passwords, tokens, cookies, or `.env`.
2. Run:

```powershell
.\.venv\Scripts\python.exe upag_client.py --list-sources
```

3. Confirm whether `agmarknet` and/or `doca` appear in `allowedSources`.
4. To retrieve one source, create a JSON payload using the exact source-specific request schema from the official docs and run:

```powershell
.\.venv\Scripts\python.exe upag_client.py --source agmarknet --payload-file config\agmarknet_request.json
```

The raw response will be saved under `data/upag_raw_agmarknet.json`. The payload cannot be guessed: confirm field names/filters from Swagger and examine the response keys.

Before turning polling on, the next implementation step is a source adapter that maps the real UPAg response into the canonical daily dataset (date × state, retail price, mandi price, arrivals, and validated quantity series). Retail and mandi/arrival data may come from different sources and different geographic granularities; do not merge them by date alone until the state, unit, crop and market dimensions are verified. Share only the **source names, redacted request payload, and a few redacted response rows** so the adapter can be tailored. Never share account credentials.

## Automated 6-hourly UPAg Dash ingestion

`pull_upag_dash.py` keeps the SQLite history current without any manual CSV work. Every run:

1. Requests **yesterday's date** (Asia/Kolkata) from the public UPAg Dash MIP by-crop endpoint for all four states (AP, MH, KA, MP).
2. Extracts the four sourced series per state — **retail, wholesale and mandi modal prices (Rs/Qtl, unconverted)** and **mandi arrivals (Tonnes ×10 → metric quintals)**.
3. Appends dates that are missing, corrects single values that the source revised (logged in `data/corrections_log.csv`), and skips identical values — so the four runs per day are idempotent. Values are written to the legacy ingestion history and mirrored into `daily_price_observations` for the graph API.
4. Forward-fills fields not supplied by this API on new daily rows, matching how the "Interpolated" dataset was built. AP daily arrivals are also rolled up from quintals/day to tonnes/month in the matching `ap_monthly_observations` row; monthly production and consumption remain sourced from the configured monthly dataset. If the DB lags behind yesterday, the window auto-extends back to `DB_last + 1` and unfilled dates become `upag_dash_6h_gapfill` rows so the daily index never breaks.

Mapping provenance: every series was cross-checked against the ingested history on 2026-10-28 with anchor dates for all four states (e.g. AP 2022-01-01 retail 4000.0, MP 2023-06-18 wholesale 1205.45, MP arrivals 1219.35 T → 12193.5 qtl). A mismatch raises loudly and blocks ingestion (`dash_extract.ANCHORS`).

Run it manually:

```powershell
.\.venv\Scripts\python.exe pull_upag_dash.py --dry-run      # fetch + compare, zero DB writes
.\.venv\Scripts\python.exe pull_upag_dash.py --once        # append/correct now
.\.venv\Scripts\python.exe pull_upag_dash.py --days 3      # catch-up back 3 days (late mandi rows)
.\.venv\Scripts\python.exe pull_upag_dash.py --full-history # 2022-01-01..yesterday reconciliation
```

Schedule it (runs at 00:00 / 06:00 / 12:00 / 18:00 local time, survives API/dashboard restarts):

```powershell
.\setup_scheduler_6h.bat     # creates task "OnionUPAgDash6h"
.\remove_scheduler_6h.bat    # removes it again
```

Backend-coupled alternative: `.\backend.bat` starts the FastAPI server **and** enables
the task only while the backend runs (disables it again on Ctrl+C). The standalone
install above remains the default and is independent of the backend.

Every run is auditable in `ingestion_log`, and raw responses are snapshotted under `data/snapshots/` (newest 30 kept). Environment switches live in `.env` (`UPAG_DASH_ENABLED`, `UPAG_DASH_DAYS`, `UPAG_DASH_SNAPSHOTS`, `UPAG_DASH_UPDATE_EXISTING`, …). Lock file `data/pull_upag_dash.lock` prevents overlapping runs. Offline tests: `pytest tests\test_dash_ingestion.py`.

The scheduled job fetches UPAg Dash data and writes it directly to SQLite; it does not call the local FastAPI HTTP API. The API reads the updated database when its data endpoints are requested. If an AP monthly row is not yet present, the job reports that its arrival total could not be updated; it does not invent monthly production or consumption values.

## Chronos-2 output interpretation

- P50 is the median forecast.
- P10–P90 is a nominal 80% prediction interval.
- `interval_width_pct_of_median` is a forecast spread indicator, **not a calibrated confidence score**.
- A reliability score requires rolling-origin backtests with actual held-out values for each horizon/state/price type.

## Run tests without model downloads

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

These tests monkeypatch Chronos-2 to verify the three API response shapes. To confirm real forecasting, run the service and request one graph endpoint with the actual model installed.


## v4 graph-specific model logic and persisted forecasts

The daily graph dataset contains 1,759 rows through 2026-10-25; its prices are converted from INR/kg to INR per metric quintal for model context, while forecast outputs are converted back to INR/kg. Daily arrivals are converted from tonnes/day to metric quintals/day. The monthly AP dataset contains 58 months through October 2026 and retains its tonnes/month units.

Each price forecast includes its own historical target context. Retail inputs are same-state wholesale, mandi modal, and arrivals; wholesale inputs are same-state mandi modal and arrivals; mandi modal inputs are same-state arrivals. Unknown future covariates are independently forecast first. Graph 3 forecasts monthly production, consumption, and arrivals as separate univariate targets and derives `Shortfall = Consumption - (Arrival + Production)`. The model's 180-day maximum is preserved; longer requested ranges return forecasts up to the maximum supported date and flag the truncated range in the response. Forecasts are persisted to `forecast_runs` and `forecast_values`. See `IMPLEMENTATION_NOTES.md` and `FRONTEND_INTEGRATION.md`.
