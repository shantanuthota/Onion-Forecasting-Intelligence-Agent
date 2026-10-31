"""Graph-specific Chronos-2 forecasting API for the onion forecasting MVP.

Daily observation prices are stored in SQLite as INR per metric quintal. Chronos-2
sees those source units; only predicted price quantiles are converted to INR/kg before
being returned and persisted in forecast_values. Graph 3 monthly observations and
forecasts remain in tonnes per month.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import logging
import math
import os
import time
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from history_store import (
    CSV_PATH, DB_PATH,
    PRICE_COLUMNS, QUANTITY_COLUMNS,
    STATE_CODES, STATE_NAMES, get_latest_forecast, ingestion_status, list_forecast_runs,
    load_ap_monthly_history, load_daily_price_history, save_forecast_run,
    sync_model_ready_csv,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("onion-forecast-api")
MODEL_ID = os.getenv("CHRONOS_MODEL_ID", "amazon/chronos-2")
MAX_FORECAST_HORIZON_DAYS = 180
QUANTILES = [0.1, 0.5, 0.9]
# Derived per-day forecast confidence (tolerance-band probability). The spread
# multiplier k scales the Chronos-2 P10/P90 spread; replace the placeholder 1.2
# with the backtest-calibrated value and set ONION_CONFIDENCE_K_CALIBRATED=1
# once rolling-origin backtests have run.
CONFIDENCE_SPREAD_K = float(os.getenv("ONION_CONFIDENCE_SPREAD_K", "1.2"))
CONFIDENCE_TOLERANCE_PCT = float(os.getenv("ONION_CONFIDENCE_TOLERANCE_PCT", "0.05"))
CONFIDENCE_K_CALIBRATED = os.getenv("ONION_CONFIDENCE_K_CALIBRATED", "").strip() == "1"
Z90 = 1.2815515655446004  # standard normal z-score of the 0.9 quantile
ALLOWED_HORIZONS = (7, 14, 30, 60, 90, 180)
PRICE_TYPES = {"RETAIL": "retail",
               "WHOLESALE": "wholesale", "MANDI_MODAL": "mandi_modal"}
PRICE_LABELS = {"retail": "Retail price",
                "wholesale": "Wholesale price", "mandi_modal": "Mandi modal price"}

app = FastAPI(
    title="Onion Forecasting MVP API",
    version="3.0.0",
    description=(
        "Graph-specific Chronos-2 forecasts. Historical price observations are stored in ₹/metric quintal; "
        "forecasted prices are converted to ₹/kg and persisted to SQLite."
    ),
)

# Chatbot (Architecture A): read-only Groq tool-calling assistant over /api/chat.
from chatbot.chat_router import router as chat_router  # noqa: E402

# Register CORS AFTER the authentication middleware.
allowed_origins = [
    x.strip()
    for x in os.getenv("CORS_ORIGINS", "*,http://192.168.1.245:8080").split(",")
    if x.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(chat_router)


# Startup event: pre-warm the Chronos-2 pipeline so the first user request does not
# trigger a cold-start download/init (latency fix #1). We also log the warm-up time.
@app.add_event_handler("startup")
async def _prewarm_model_on_startup():
    start = time.time()
    logger.info("Pre-warming Chronos-2 pipeline at server startup...")
    try:
        # Force pipeline initialization without a full prediction (just load weights).
        pipeline = get_pipeline()
        logger.info(
            "Chronos-2 pipeline pre-warmed in %.1fs on %s (model=%s)",
            time.time() - start,
            pipeline.device if hasattr(pipeline, "device") else "unknown",
            MODEL_ID,
        )
    except Exception:
        logger.exception("Failed to pre-warm Chronos-2 pipeline; forecasts will be slower until first successful call.")


@app.middleware("http")
async def authenticate_api_requests(request: Request, call_next):
    if (
        request.method != "OPTIONS"
        and request.url.path.startswith("/api/")
        and request.url.path != "/api/ingestion/sync-csv"
    ):
        configured_token = os.getenv("API_BEARER_TOKEN", "").strip()

        if not configured_token:
            return JSONResponse(
                status_code=503,
                content={"detail": "API_BEARER_TOKEN is not configured."},
            )

        authorization = request.headers.get("authorization", "")
        scheme, separator, supplied_token = authorization.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not separator
            or not supplied_token.strip()
            or not hmac.compare_digest(
                supplied_token.strip(), configured_token
            )
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing bearer token."},
                headers={"WWW-Authenticate": "Bearer"},
            )

    return await call_next(request)


def get_data() -> pd.DataFrame:
    try:
        return load_daily_price_history()
    except Exception as exc:
        logger.exception("Could not load daily price history")
        raise HTTPException(
            status_code=500, detail=f"Could not load daily price history: {exc}") from exc


def get_monthly_data() -> pd.DataFrame:
    try:
        return load_ap_monthly_history()
    except Exception as exc:
        logger.exception("Could not load monthly AP quantity history")
        raise HTTPException(
            status_code=500, detail=f"Could not load monthly AP quantity history: {exc}") from exc


@lru_cache(maxsize=1)
def get_pipeline():
    try:
        import torch
        from chronos import Chronos2Pipeline
    except ImportError as exc:
        raise RuntimeError(
            "Chronos-2 dependencies are missing. Activate the project's virtual environment and run: "
            "pip install -r requirements.txt"
        ) from exc
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info(
        "Loading %s on %s; first load may download model weights", MODEL_ID, device)
    return Chronos2Pipeline.from_pretrained(MODEL_ID, device_map=device)


def _validate_horizon(horizon_days: int) -> int:
    if not 1 <= horizon_days <= MAX_FORECAST_HORIZON_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"Forecast horizon must be between 1 and {MAX_FORECAST_HORIZON_DAYS} days.")
    return horizon_days


def _date_range_horizon(data: pd.DataFrame, start_date: date, end_date: date) -> int:
    if start_date > end_date:
        raise HTTPException(
            status_code=422, detail="start_date must be on or before end_date.")

    last_observation = pd.Timestamp(data["timestamp"].max()).date()
    requested_horizon = max(0, (end_date - last_observation).days)
    return min(requested_horizon, MAX_FORECAST_HORIZON_DAYS)


def _forecast_limit_end_date(data: pd.DataFrame, requested_end_date: date) -> date:
    last_observation = pd.Timestamp(data["timestamp"].max()).date()
    latest_supported_date = last_observation + pd.Timedelta(days=MAX_FORECAST_HORIZON_DAYS)
    return min(requested_end_date, latest_supported_date)


def _forecast_limit_warning(requested_end_date: date, limit_end_date: date) -> str | None:
    if requested_end_date <= limit_end_date:
        return None
    return (
        f"Requested end_date {requested_end_date.isoformat()} is beyond the "
        f"{MAX_FORECAST_HORIZON_DAYS}-day forecast limit. Forecasts are returned "
        f"through {limit_end_date.isoformat()} (the maximum supported date)."
    )


def _actual_end_date(requested_end_date: date) -> date:
    return min(requested_end_date, date.today())


def _run_async(blocking_func, *args, **kwargs):
    """Offload blocking inference to the default ThreadPoolExecutor (latency fix #4)."""
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return loop.run_in_executor(pool, blocking_func, *args, **kwargs)


def _forecast_display_start_date(
    data: pd.DataFrame, start_date: date, end_date: date
) -> date | None:
    last_observation = pd.Timestamp(data["timestamp"].max()).date()
    forecast_start = max(start_date, date.today(), last_observation + pd.Timedelta(days=1))
    return forecast_start if forecast_start <= end_date else None


def _parse_list(raw: str, allowed: set[str], label: str) -> list[str]:
    values = [part.strip().upper() for part in raw.split(",") if part.strip()]
    if not values:
        raise HTTPException(
            status_code=422, detail=f"Select at least one {label}.")
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"Unsupported {label}: {unknown}. Allowed values: {sorted(allowed)}")
    return list(dict.fromkeys(values))


def _future_dates(data: pd.DataFrame, horizon: int, freq: str = "D") -> pd.DataFrame:
    last_date = pd.Timestamp(data["timestamp"].max()).normalize()
    next_date = last_date + (pd.offsets.MonthBegin(1) if freq == "MS" else pd.Timedelta(days=1))
    dates = pd.date_range(next_date, periods=horizon, freq=freq)
    return pd.DataFrame({"item_id": [str(data["item_id"].iloc[0])] * horizon, "timestamp": dates})


def _frame_hash(frame: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(frame, index=True).values.tobytes()).hexdigest()[:24]


# Prediction cache with both size limit and time-based TTL (prevents stale results
# from persisting indefinitely and keeps memory bounded under concurrent load).
_PREDICTION_CACHE: dict[str, tuple[pd.DataFrame, float]] = {}
_PREDICTION_CACHE_TTL_SEC = 3600  # 1 hour TTL; refresh after ingestion changes
_PREDICTION_CACHE_MAX_SIZE = 256


def _run_target(data: pd.DataFrame, target: str, horizon: int,
                context_covariates: list[str] | None = None,
                future_covariates: pd.DataFrame | None = None,
                freq: str = "D") -> pd.DataFrame:
    """Run exactly one target with only its specified historical context columns.

    Chronos-2's known future covariates are supplied via future_covariates. Unknown
    future price and arrival covariates are forecast independently before use.
    """
    _validate_horizon(horizon)
    context_covariates = list(context_covariates or [])
    if target not in data.columns:
        raise HTTPException(
            status_code=500, detail=f"Missing target column {target} in SQLite history.")
    for col in context_covariates:
        if col not in data.columns:
            raise HTTPException(
                status_code=500, detail=f"Missing context column {col} in SQLite history.")
    selected_cols = list(dict.fromkeys(
        ["item_id", "timestamp", *context_covariates, target]))
    context = data[selected_cols].copy()
    context["timestamp"] = pd.to_datetime(context["timestamp"])
    future = _future_dates(data, horizon, freq=freq)
    if future_covariates is not None and context_covariates:
        future_covariates = future_covariates.copy()
        future_covariates["timestamp"] = pd.to_datetime(
            future_covariates["timestamp"])
        future = future.merge(future_covariates[[
                              "timestamp", *context_covariates]], on="timestamp", how="left", validate="one_to_one")
        if future[context_covariates].isna().any().any():
            raise HTTPException(
                status_code=500, detail=f"Future covariate forecast missing values for target {target}.")
    key = "|".join([_frame_hash(context), _frame_hash(
        future), target, str(horizon), freq, MODEL_ID])
    # Check cache with TTL: stale entries beyond TTL are ignored (not deleted here
    # to avoid mutation during concurrent reads; eviction is lazy on write).
    if key in _PREDICTION_CACHE:
        cached_df, cached_at = _PREDICTION_CACHE[key]
        if (time.time() - cached_at) < _PREDICTION_CACHE_TTL_SEC:
            return cached_df.copy(deep=True)
    try:
        pipeline = get_pipeline()
        result = pipeline.predict_df(
            df=context,
            future_df=future,
            id_column="item_id",
            timestamp_column="timestamp",
            target=target,
            prediction_length=horizon,
            quantile_levels=QUANTILES,
            freq=freq,
        )
    except RuntimeError as exc:
        logger.exception("Chronos-2 unavailable")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Chronos-2 forecast failed for target %s", target)
        raise HTTPException(
            status_code=500, detail=f"Chronos-2 forecast failed for {target}: {type(exc).__name__}: {exc}") from exc
    required = {"timestamp", "target_name", "0.1", "0.5", "0.9"}
    missing = required - set(result.columns)
    if missing:
        raise HTTPException(
            status_code=500, detail=f"Chronos-2 output missing columns: {sorted(missing)}")
    result["timestamp"] = pd.to_datetime(result["timestamp"])
    # Eviction: if over max size, drop oldest (by insertion time). With concurrent
    # access we rely on Python's dict ordering being insertion-ordered (CPython 3.7+).
    while len(_PREDICTION_CACHE) >= _PREDICTION_CACHE_MAX_SIZE:
        oldest_key = next(iter(_PREDICTION_CACHE))
        _PREDICTION_CACHE.pop(oldest_key)
    _PREDICTION_CACHE[key] = (result.copy(deep=True), time.time())
    return result


def _univariate_future(data: pd.DataFrame, col: str, horizon: int,
                       freq: str = "D") -> pd.DataFrame:
    result = _run_target(data, col, horizon, freq=freq)
    result = result.sort_values("timestamp")
    return pd.DataFrame({
        "timestamp": result["timestamp"].values,
        col: result["0.5"].astype(float).values,
    })


def _price_target_forecast(data: pd.DataFrame, state: str, price_type: str, horizon: int) -> pd.DataFrame:
    target = PRICE_COLUMNS[state][price_type]
    covariates = _price_context_columns(state, price_type)[1:]
    future_covariates = None
    if covariates:
        forecasts = [_univariate_future(data, col, horizon) for col in covariates]
        future_covariates = forecasts[0]
        for values in forecasts[1:]:
            future_covariates = future_covariates.merge(
                values, on="timestamp", validate="one_to_one")
    return _run_target(data, target, horizon, context_covariates=covariates, future_covariates=future_covariates)


def _price_context_columns(state: str, price_type: str) -> list[str]:
    inputs = {
        "retail": ("wholesale", "mandi_modal", "arrival"),
        "wholesale": ("mandi_modal", "arrival"),
        "mandi_modal": ("arrival",),
    }
    target = PRICE_COLUMNS[state][price_type]
    covariates = [
        PRICE_COLUMNS[state][measure] if measure != "arrival"
        else QUANTITY_COLUMNS[state]["arrival"]
        for measure in inputs[price_type]
    ]
    return [target, *covariates]


def _quantile_values(row: pd.Series) -> tuple[float, float, float]:
    p10, p50, p90 = (max(0.0, float(row[k])) for k in ("0.1", "0.5", "0.9"))
    return min(p10, p50), p50, max(p50, p90)


def _normal_cdf(value: float) -> float:
    """Standard normal CDF Phi(x). Identical to scipy.special.ndtr without SciPy."""
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _confidence_score(q10: float, median: float, q90: float) -> float:
    """Derived per-day confidence: probability (0-100, one decimal) that the actual
    value stays within +/-CONFIDENCE_TOLERANCE_PCT of the forecast median.

    1) The Chronos-2 spread around the median is stretched by the backtest
       calibration multiplier CONFIDENCE_SPREAD_K.
    2) Each side's calibrated distance (median->P10, median->P90) is treated as a
       one-sided 0.90-quantile distance and converted to a sigma via Z90.
    3) The probability mass inside the [median*(1-tol), median*(1+tol)] band of
       that asymmetric normal is the per-day confidence.
    """
    calibrated_q10 = median + CONFIDENCE_SPREAD_K * (q10 - median)
    calibrated_q90 = median + CONFIDENCE_SPREAD_K * (q90 - median)
    calibrated_q10 = min(calibrated_q10, median)
    calibrated_q90 = max(calibrated_q90, median)
    s_lo = max((median - calibrated_q10) / Z90, 1e-9)
    s_hi = max((calibrated_q90 - median) / Z90, 1e-9)
    band_half = CONFIDENCE_TOLERANCE_PCT * abs(median)
    p_up = max(0.0, min(0.5, _normal_cdf(band_half / s_hi) - 0.5))
    p_down = max(0.0, min(0.5, 0.5 - _normal_cdf(-band_half / s_lo)))
    score = (p_up + p_down) * 100.0
    return round(max(0.0, min(100.0, score)), 1)


def _price_forecast_records(data: pd.DataFrame, state: str, price_type: str, result: pd.DataFrame) -> list[dict]:
    col = PRICE_COLUMNS[state][price_type]
    latest_row = data.loc[pd.to_datetime(data["timestamp"]).idxmax()]
    latest_actual_price_rs_kg = float(latest_row[col]) / 100.0
    latest_actual_date = pd.Timestamp(latest_row["timestamp"]).date().isoformat()

    records: list[dict] = []
    running_min_confidence = float("inf")
    for _, row in result.sort_values("timestamp").iterrows():
        q10, q50, q90 = _quantile_values(row)
        # Source and model units are INR/metric quintal. Convert ONLY forecast outputs to INR/kg.
        p10, p50, p90 = q10 / 100.0, q50 / 100.0, q90 / 100.0
        width = p90 - p10
        change_rs_kg = p50 - latest_actual_price_rs_kg
        # Per-day confidence from the calibrated spread. The score is a cumulative
        # minimum across forecast dates (it may only stay level or decrease with
        # horizon), matching the pred["confidence_score"].cummin() post-process.
        running_min_confidence = min(running_min_confidence, _confidence_score(q10, q50, q90))

        records.append({
            "date": pd.Timestamp(row["timestamp"]).date().isoformat(),
            "state": state,
            "state_name": STATE_NAMES[state],
            "price_type": price_type,
            "price_type_label": PRICE_LABELS[price_type],
            "latest_actual_date": latest_actual_date,
            "latest_actual_price_rs_kg": round(latest_actual_price_rs_kg, 4),
            "predicted_price_rs_kg": round(p50, 4),
            "change_rs_kg": round(change_rs_kg, 4),
            "change_percent": (
                round(change_rs_kg / latest_actual_price_rs_kg * 100, 2)
                if latest_actual_price_rs_kg != 0 else None
            ),
            "change_direction": (
                "increase" if change_rs_kg > 0 else
                "decrease" if change_rs_kg < 0 else
                "no_change"
            ),
            "p10_rs_kg": round(p10, 4),
            "p50_rs_kg": round(p50, 4),
            "p90_rs_kg": round(p90, 4),
            "prediction_interval_width_rs_kg": round(width, 4),
            "interval_width_pct_of_median": round(width / max(abs(p50), 1e-9) * 100, 2),
            "confidence_score": round(running_min_confidence, 1),
        })
    return records


def _overall_confidence_scores(forecast: list[dict]) -> dict[str, float | None]:
    """Aggregate per-day confidence scores from a forecast array."""
    scores = [float(r["confidence_score"]) for r in forecast if r.get("confidence_score") is not None]
    if not scores:
        return {"overall_confidence_score": None, "minimum_confidence_score": None,
                "final_confidence_score": None, "forecast_points_for_overall": 0}
    return {
        "overall_confidence_score": round(sum(scores) / len(scores), 1),
        "minimum_confidence_score": round(min(scores), 1),
        "final_confidence_score": round(scores[-1], 1),
        "forecast_points_for_overall": len(scores),
    }


def _history_price_records(
    data: pd.DataFrame, state: str, price_type: str, start_date: date, end_date: date
) -> list[dict]:
    col = PRICE_COLUMNS[state][price_type]
    timestamps = pd.to_datetime(data["timestamp"]).dt.date
    in_range = (timestamps >= start_date) & (timestamps <= end_date)
    selected = data.loc[in_range, ["timestamp", col]]

    return [
        {
            "date": row.timestamp.date().isoformat(),
            "state": state,
            "price_type": price_type,
            "price_type_label": PRICE_LABELS[price_type],
            "actual_price_rs_kg": round(float(getattr(row, col)) / 100.0, 4),
        }
        for row in selected.itertuples(index=False)
    ]


def _filter_date_records(records: list[dict], start_date: date, end_date: date) -> list[dict]:
    return [
        record for record in records
        if start_date <= date.fromisoformat(record["date"]) <= end_date
    ]


def _context_signature(data: pd.DataFrame, cols: list[str]) -> str:
    return _frame_hash(data[["timestamp", *cols]].copy())


def _persist_prices(graph_code: str, data: pd.DataFrame, horizon: int, records: list[dict], context_cols: list[str]) -> dict:
    db_records = [{"state_code": r["state"], "target_key": r["price_type"], "forecast_date": r["date"],
                   "p10": r["p10_rs_kg"], "p50": r["p50_rs_kg"], "p90": r["p90_rs_kg"], "unit": "INR/kg"}
                  for r in records]
    return save_forecast_run(graph_code, horizon, pd.Timestamp(data["timestamp"].max()).date().isoformat(),
                             _context_signature(
                                 data, context_cols), db_records, MODEL_ID,
                             {"stored_forecast_price_unit": "INR/kg", "observation_price_unit": "INR/metric quintal",
                              "context_columns": context_cols, "quantiles": QUANTILES})


def _status(data: pd.DataFrame, frequency: str = "daily") -> dict:
    last_date = pd.Timestamp(data["timestamp"].max()).date()
    stale = (date.today() - last_date).days
    warnings = []
    stale_limit = 45 if frequency == "monthly" else 1
    if stale > stale_limit:
        warnings.append(
            f"Latest observation is {last_date.isoformat()} ({stale} day(s) behind the server date).")
    if stale < 0:
        warnings.append(
            "Latest observation date is in the future relative to the server date; verify timestamps.")
    forecast_start = (
        pd.Timestamp(last_date).to_period("M").to_timestamp() + pd.offsets.MonthBegin(1)
        if frequency == "monthly" else last_date + pd.Timedelta(days=1)
    )
    return {"last_observation_date": last_date.isoformat(), "data_staleness_days": stale,
            "forecast_start_date": forecast_start.isoformat(), "warnings": warnings}


def _metadata() -> dict:
    return {"model": MODEL_ID, "frequency": "daily", "observation_price_unit": "INR/metric quintal",
            "forecast_price_unit": "INR/kg", "quantity_unit": "metric quintals/day",
            "quantiles": {"lower": 0.1, "median": 0.5, "upper": 0.9},
            "prediction_interval": {"nominal_coverage": 0.8, "description": "P10–P90 is a nominal central 80% prediction interval, not a guarantee."},
            "confidence_score": {"available": True,
                                 "method": "tolerance_band_probability",
                                 "applies_to": "Graph 1/2 daily price forecast rows",
                                 "description": ("Derived per-day probability (%) that the actual price stays within "
                                                 f"±{CONFIDENCE_TOLERANCE_PCT * 100:.0f}% of the forecast median, assuming an "
                                                 "asymmetric normal built from the Chronos-2 P10/P90 spread scaled by "
                                                 f"the spread multiplier k={CONFIDENCE_SPREAD_K}. Monotone (non-increasing) "
                                                 "across forecast dates per series."),
                                 "spread_multiplier_k": CONFIDENCE_SPREAD_K,
                                 "tolerance_band_pct": CONFIDENCE_TOLERANCE_PCT * 100,
                                 "calibrated": CONFIDENCE_K_CALIBRATED,
                                 "note": ("k is an uncalibrated placeholder; when the backtest supplies calibrated "
                                          "values, set ONION_CONFIDENCE_SPREAD_K=<k> and ONION_CONFIDENCE_K_CALIBRATED=1.")}}


@app.get("/")
def root() -> dict:
    return {"service": "Onion Forecasting MVP API v3", "docs": "/docs",
            "graphs": {"graph_1_ap_prices": "/api/graphs/ap-retail",
                       "graph_2_state_comparison": "/api/graphs/state-comparison",
                       "graph_3_supply_balance": "/api/graphs/supply-balance"},
            "ops": {"health": "/health", "ingestion_status": "/api/ingestion/status", "saved_forecasts": "/api/forecasts/latest"}}


@app.get("/health")
def health() -> dict:
    data = get_data()
    return {"status": "ok", "model_loaded": get_pipeline.cache_info().currsize > 0, "model_id": MODEL_ID,
            "history_db": str(DB_PATH), "ingestion": ingestion_status(), **_status(data)}


@app.get("/api/data/status")
def data_status() -> dict:
    data = get_data()
    return {"ingestion": ingestion_status(), **_status(data)}


@app.get("/api/ingestion/status")
def ingestion_status_endpoint() -> dict:
    return ingestion_status()


@app.post("/api/ingestion/sync-csv")
def sync_csv_endpoint(filename: str = Query(..., description="CSV filename already in project data/ folder"),
                      x_ingestion_key: str | None = Header(default=None)) -> dict:
    configured_key = os.getenv("INGESTION_API_KEY", "").strip()
    if not configured_key or configured_key == "change-this-local-secret":
        raise HTTPException(
            status_code=503, detail="Set a unique INGESTION_API_KEY in .env before enabling ingestion.")
    if not x_ingestion_key or x_ingestion_key != configured_key:
        raise HTTPException(
            status_code=401, detail="Invalid or missing X-Ingestion-Key header.")
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith(".csv"):
        raise HTTPException(
            status_code=400, detail="filename must be a CSV filename without directory paths.")
    candidate = (ROOT / "data" / safe_name).resolve()
    if candidate.parent != (ROOT / "data").resolve() or not candidate.exists():
        raise HTTPException(
            status_code=404, detail="CSV file not found inside project data/ folder.")
    try:
        result = sync_model_ready_csv(
            candidate, source=f"csv_import:{safe_name}")
    except Exception as exc:
        logger.exception("CSV history sync failed")
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _PREDICTION_CACHE.clear()
    return {"status": "success", **result.as_dict(), "history": ingestion_status()}


@app.get("/api/graphs/ap-retail")
async def ap_retail_graph(
        price_types: str = Query(
            "retail", description="Comma-separated retail, wholesale, mandi_modal"),
        start_date: date = Query(...,
                                 description="First date to display, YYYY-MM-DD"),
        end_date: date = Query(
            ..., description="Last date to display and forecast through, YYYY-MM-DD; actuals display through today, and the forecast displays from today (or the next day if today's actual exists) through end_date"),
        threshold_rs_kg: float = Query(
            44.0,
            ge=0,
            description="Horizontal price threshold in INR/kg",
        ),) -> dict:
    data = get_data()
    requested = _parse_list(price_types, set(PRICE_TYPES), "price types")
    selected = [PRICE_TYPES[x] for x in requested]
    actual_end_date = _actual_end_date(end_date)
    forecast_limit_end = _forecast_limit_end_date(data, end_date)
    forecast_display_start = _forecast_display_start_date(data, start_date, forecast_limit_end)
    horizon_days = _date_range_horizon(data, start_date, end_date)
    forecast: list[dict] = []
    context_cols: list[str] = []
    # Latency fix #3: parallel inference for multi-series using ThreadPoolExecutor.
    # Each _price_target_forecast releases the GIL during torch inference, so
    # concurrent threads run on the GPU in parallel.
    def _forecast_for_type(price_type: str) -> tuple[list[str], list[dict]]:
        cols = _price_context_columns("AP", price_type)
        if horizon_days and forecast_display_start:
            result = _price_target_forecast(data, "AP", price_type, horizon_days)
            records = _filter_date_records(
                _price_forecast_records(data, "AP", price_type, result),
                forecast_display_start,
                forecast_limit_end,
            )
        else:
            records = []
        return cols, records

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(selected) or 1)) as executor:
        futures = [executor.submit(_forecast_for_type, pt) for pt in selected]
        for future in concurrent.futures.as_completed(futures):
            cols, records = future.result()
            context_cols.extend(cols)
            forecast.extend(records)
    historical = []
    for price_type in selected:
        historical.extend(_history_price_records(
            data, "AP", price_type, start_date, actual_end_date))
    saved = (
        _persist_prices("graph1_ap_prices", data, horizon_days, forecast, list(
            dict.fromkeys(context_cols)))
        if horizon_days and forecast_display_start
        else None
    )
    status = _status(data)
    forecast_warning = _forecast_limit_warning(end_date, forecast_limit_end)
    if forecast_warning:
        status["warnings"].append(forecast_warning)
    return {"graph": "ap_price_forecast", "title": "Andhra Pradesh Onion Price Forecast", "state": "AP",
            "state_name": STATE_NAMES["AP"], "price_types": selected, "unit": "INR/kg",
            "threshold_rs_kg": threshold_rs_kg, "horizon_days": horizon_days,
            "model_history_rows": int(len(data)), "visible_start_date": start_date.isoformat(),
            "visible_end_date": end_date.isoformat(),
            "requested_end_date": end_date.isoformat(),
            "forecast_limit_date": forecast_limit_end.isoformat(),
            "forecast_truncated": forecast_limit_end < end_date,
            "actual_end_date": actual_end_date.isoformat(),
            "context_logic": {
                price_type: ["timestamp", *_price_context_columns("AP", price_type)]
                for price_type in ("retail", "wholesale", "mandi_modal")
            },
            "historical": historical, "forecast": forecast, "metadata": _metadata(), "saved_to_sqlite": saved,
            **status,
            "forecast_start_date": forecast_display_start.isoformat() if forecast_display_start else None,
            "forecast_end_date": max((r["date"] for r in forecast), default=None)}


@app.get("/api/graphs/state-comparison")
async def state_comparison_graph(
    states: str = Query("AP,KA,MH,MP"),
    price_types: str = Query("retail,wholesale,mandi_modal"),
    start_date: date = Query(...,
                             description="First date to display, YYYY-MM-DD"),
    end_date: date = Query(...,
                           description="Last date to display and forecast through, YYYY-MM-DD; actuals display through today, and the forecast displays from today (or the next day if today's actual exists) through end_date"),
    threshold_rs_kg: float = Query(
        44.0,
        ge=0,
        description="Horizontal price threshold in INR/kg",
    ),
) -> dict:
    data = get_data()
    selected_states = _parse_list(states, set(STATE_CODES), "states")
    requested = _parse_list(price_types, set(PRICE_TYPES), "price types")
    selected_types = [PRICE_TYPES[x] for x in requested]
    actual_end_date = _actual_end_date(end_date)
    forecast_limit_end = _forecast_limit_end_date(data, end_date)
    forecast_display_start = _forecast_display_start_date(data, start_date, forecast_limit_end)
    horizon_days = _date_range_horizon(data, start_date, end_date)

    forecast: list[dict] = []
    historical: list[dict] = []
    context_cols: list[str] = []
    # Latency fix #3 (continued): parallel multi-state/multi-price inference.
    # Collect all (state, price_type, context_cols, records) in parallel, then build lists.
    def _forecast_pair(args):
        state, price_type = args
        cols = _price_context_columns(state, price_type)
        records = []
        hist = []
        if horizon_days and forecast_display_start:
            result = _price_target_forecast(data, state, price_type, horizon_days)
            records = _filter_date_records(
                _price_forecast_records(data, state, price_type, result),
                forecast_display_start, forecast_limit_end)
        hist.extend(_history_price_records(data, state, price_type, start_date, actual_end_date))
        return cols, records, hist

    pairs = [(s, p) for s in selected_states for p in selected_types]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(pairs) or 1)) as executor:
        results = list(executor.map(_forecast_pair, pairs))
    for cols, records, hist in results:
        context_cols.extend(cols)
        forecast.extend(records)
        historical.extend(hist)

    saved = (
        _persist_prices(
            "graph2_state_comparison",
            data,
            horizon_days,
            forecast,
            list(dict.fromkeys(context_cols)),
        )
        if horizon_days and forecast_display_start
        else None
    )
    status = _status(data)
    forecast_warning = _forecast_limit_warning(end_date, forecast_limit_end)
    if forecast_warning:
        status["warnings"].append(forecast_warning)
    return {
        "graph": "state_price_comparison",
        "title": "Selected States — Onion Price Forecasts",
        "selected_states": selected_states,
        "selected_state_names": [STATE_NAMES[x] for x in selected_states],
        "selected_price_types": selected_types,
        "horizon_days": horizon_days,
        "unit": "INR/kg",
        "threshold_rs_kg": threshold_rs_kg,
        "visible_start_date": start_date.isoformat(),
        "visible_end_date": end_date.isoformat(),
        "requested_end_date": end_date.isoformat(),
        "forecast_limit_date": forecast_limit_end.isoformat(),
        "forecast_truncated": forecast_limit_end < end_date,
        "actual_end_date": actual_end_date.isoformat(),
        "historical": historical,
        "forecast": forecast,
        "metadata": _metadata(),
        "saved_to_sqlite": saved,
        **status,
        "forecast_start_date": forecast_display_start.isoformat() if forecast_display_start else None,
        "forecast_end_date": max((r["date"] for r in forecast), default=None),
    }


@app.get("/api/graphs/supply-balance")
def supply_balance_graph(
    states: str = Query("AP"),
    start_date: date = Query(...,
                             description="First date to display, YYYY-MM-DD"),
    end_date: date = Query(...,
                           description="Last date to display and forecast through, YYYY-MM-DD; requests beyond 180 days return the maximum supported forecast"),
) -> dict:
    """Graph 3: forecast AP monthly quantities and derive shortfall from their forecasts."""
    data = get_monthly_data().rename(columns={"month_start": "timestamp"})
    data["item_id"] = "AP_onion"
    selected_states = _parse_list(states, set(STATE_CODES), "states")
    if selected_states != ["AP"]:
        raise HTTPException(
            status_code=422,
            detail="The monthly supply-demand dataset currently contains Andhra Pradesh (AP) only.",
        )
    forecast_limit_end = _forecast_limit_end_date(data, end_date)
    horizon_days = _date_range_horizon(data, start_date, end_date)
    latest_month = pd.Timestamp(data["timestamp"].max()).to_period("M")
    limit_month = pd.Period(forecast_limit_end, freq="M")
    horizon_months = max(0, limit_month.ordinal - latest_month.ordinal)
    forecast_rows: list[dict] = []
    historical: list[dict] = []
    indexed: dict[str, dict[str, dict]] = {}
    db_values: list[dict] = []
    timestamps = pd.to_datetime(data["timestamp"]).dt.date
    history_mask = (timestamps >= start_date) & (timestamps <= end_date)
    target_columns = {
        "production": "production_t_month",
        "consumption": "consumption_t_month",
        "arrival": "arrival_t_month",
    }
    if horizon_months and start_date <= forecast_limit_end:
        for metric, column in target_columns.items():
            result = _run_target(data, column, horizon_months, freq="MS")
            for _, row in result.sort_values("timestamp").iterrows():
                month = pd.Timestamp(row["timestamp"]).date().isoformat()
                if not start_date.isoformat() <= month <= forecast_limit_end.isoformat():
                    continue
                q10, q50, q90 = _quantile_values(row)
                item = {
                    "date": month,
                    "state": "AP",
                    "state_name": STATE_NAMES["AP"],
                    "metric": metric,
                    "predicted_quantity_tonnes_month": round(q50, 4),
                    "p10_tonnes_month": round(q10, 4),
                    "p50_tonnes_month": round(q50, 4),
                    "p90_tonnes_month": round(q90, 4),
                    "predicted_quantity_mq_day": round(q50, 4),
                    "p10_mq_day": round(q10, 4),
                    "p50_mq_day": round(q50, 4),
                    "p90_mq_day": round(q90, 4),
                    "unit": "tonnes/month",
                }
                forecast_rows.append(item)
                indexed.setdefault(month, {})[metric] = item

    for metric, column in target_columns.items():
        for row in data.loc[history_mask, ["timestamp", column]].itertuples(index=False):
            quantity = round(float(getattr(row, column)), 4)
            historical.append({
                "date": row.timestamp.date().isoformat(),
                "state": "AP",
                "state_name": STATE_NAMES["AP"],
                "metric": metric,
                "actual_quantity_tonnes_month": quantity,
                "actual_quantity_mq_day": quantity,
                "unit": "tonnes/month",
            })

    balance = []
    for month, vals in sorted(indexed.items()):
        if not {"production", "consumption", "arrival"}.issubset(vals):
            continue
        production, consumption, arrival = (
            vals["production"], vals["consumption"], vals["arrival"])
        p10 = (consumption["p10_tonnes_month"] - arrival["p90_tonnes_month"]
               - production["p90_tonnes_month"])
        p50 = (consumption["predicted_quantity_tonnes_month"]
               - arrival["predicted_quantity_tonnes_month"]
               - production["predicted_quantity_tonnes_month"])
        p90 = (consumption["p90_tonnes_month"] - arrival["p10_tonnes_month"]
               - production["p10_tonnes_month"])
        balance.append({
            "date": month,
            "state": "AP",
            "state_name": STATE_NAMES["AP"],
            "shortfall_p10_tonnes_month": round(p10, 4),
            "shortfall_p50_tonnes_month": round(p50, 4),
            "shortfall_p90_tonnes_month": round(p90, 4),
            "shortfall_p10_metric_quintal_day": round(p10, 4),
            "shortfall_p50_metric_quintal_day": round(p50, 4),
            "shortfall_p90_metric_quintal_day": round(p90, 4),
            "classification": "potential_shortfall" if p50 > 0 else (
                "potential_surplus" if p50 < 0 else "near_balance"),
        })

    for row in forecast_rows:
        db_values.append({
            "state_code": row["state"], "target_key": row["metric"],
            "forecast_date": row["date"], "p10": row["p10_tonnes_month"],
            "p50": row["predicted_quantity_tonnes_month"],
            "p90": row["p90_tonnes_month"], "unit": "tonnes/month",
        })
    for row in balance:
        db_values.append({
            "state_code": row["state"], "target_key": "shortfall",
            "forecast_date": row["date"], "p10": row["shortfall_p10_tonnes_month"],
            "p50": row["shortfall_p50_tonnes_month"],
            "p90": row["shortfall_p90_tonnes_month"], "unit": "tonnes/month",
        })
    context_cols = list(target_columns.values())
    saved = (
        save_forecast_run(
            "graph3_supply_balance", horizon_days,
            pd.Timestamp(data["timestamp"].max()).date().isoformat(),
            _context_signature(data, context_cols), db_values, MODEL_ID,
            {
                "context_logic": {
                    metric: ["month_start", column]
                    for metric, column in target_columns.items()
                },
                "balance_formula": "consumption - (arrival + production)",
                "unit": "tonnes/month",
                "forecast_frequency": "monthly",
            },
        )
        if balance else None
    )
    warnings = [
        "Shortfall = Consumption - (Arrival + Production). Positive values indicate potential shortfall; negative values indicate potential surplus.",
        "Shortfall P10/P90 combine component quantiles conservatively and are not a calibrated joint prediction interval.",
    ]
    forecast_warning = _forecast_limit_warning(end_date, forecast_limit_end)
    if forecast_warning:
        warnings.append(forecast_warning)
    status = _status(data, frequency="monthly")
    first_forecast_date = min((row["date"] for row in forecast_rows), default=None)
    status["forecast_start_date"] = first_forecast_date
    status["warnings"] = warnings + status["warnings"]
    metadata = {
        **_metadata(),
        "frequency": "monthly",
        "quantity_unit": "tonnes/month",
        "confidence_score": {
            "available": False,
            "reason": ("The tolerance-band confidence is implemented for Graph 1/2 daily price rows; "
                       "Graph 3 combines component forecasts, so a per-day score is not applied yet."),
        },
    }
    return {"graph": "supply_demand_shortfall", "title": "Andhra Pradesh Monthly Supply-Demand Balance Forecast",
            "selected_states": selected_states, "selected_state_names": [STATE_NAMES[x] for x in selected_states],
            "horizon_days": horizon_days, "quantity_unit": "tonnes/month", "historical": historical,
            "quantity_forecast": forecast_rows, "balance_forecast": balance,
            "balance_formula": "consumption - (arrival + production)", "warnings": warnings, "metadata": metadata,
            "requested_end_date": end_date.isoformat(),
            "forecast_limit_date": forecast_limit_end.isoformat(),
            "forecast_truncated": forecast_limit_end < end_date,
            "saved_to_sqlite": saved, **status,
            "forecast_end_date": max((r["date"] for r in forecast_rows), default=None)}


@app.get("/api/forecasts/latest")
def latest_saved_forecast(graph_code: str | None = None) -> dict:
    result = get_latest_forecast(graph_code)
    if result is None:
        return {"forecast": None, "message": "No forecasts have been saved yet. Run a graph forecast first."}
    return {"forecast": result}


@app.get("/api/forecasts/runs")
def saved_forecast_runs(limit: int = Query(20, ge=1, le=100)) -> dict:
    return {"runs": list_forecast_runs(limit)}
