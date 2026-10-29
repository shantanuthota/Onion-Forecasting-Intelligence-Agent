"""Graph-specific Chronos-2 forecasting API for the onion forecasting MVP.

Observation prices are stored in SQLite as INR per metric quintal. Chronos-2 sees
those source units; only predicted price quantiles are converted to INR/kg before
being returned and persisted in forecast_values. Quantity observations and forecasts
are in metric quintals per day (subject to source-period validation).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import date
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from history_store import (
    CSV_PATH, DB_PATH, PRICE_COLUMNS, QUANTITY_COLUMNS, SUPPLY_COLUMNS,
    STATE_CODES, STATE_NAMES, get_latest_forecast, ingestion_status, list_forecast_runs,
    load_history, save_forecast_run, sync_model_ready_csv,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("onion-forecast-api")
MODEL_ID = os.getenv("CHRONOS_MODEL_ID", "amazon/chronos-2")
MAX_FORECAST_HORIZON_DAYS = 180
QUANTILES = [0.1, 0.5, 0.9]
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


app = FastAPI(
    title="Onion Forecasting MVP API",
    version="3.0.0",
    description=(
        "Graph-specific Chronos-2 forecasts. Historical price observations are stored in ₹/metric quintal; "
        "forecasted prices are converted to ₹/kg and persisted to SQLite."
    ),
)

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
        return load_history()
    except Exception as exc:
        logger.exception("Could not load market history")
        raise HTTPException(
            status_code=500, detail=f"Could not load market history: {exc}") from exc


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
    horizon_days = max(0, (end_date - last_observation).days)
    if horizon_days > MAX_FORECAST_HORIZON_DAYS:
        raise HTTPException(
            status_code=422,
            detail="forecasting cannot exceed 180 days",
        )
    return horizon_days


def _actual_end_date(requested_end_date: date) -> date:
    return min(requested_end_date, date.today())


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


def _future_dates(data: pd.DataFrame, horizon: int) -> pd.DataFrame:
    last_date = pd.Timestamp(data["timestamp"].max()).normalize()
    dates = pd.date_range(last_date + pd.Timedelta(days=1),
                          periods=horizon, freq="D")
    return pd.DataFrame({"item_id": [str(data["item_id"].iloc[0])] * horizon, "timestamp": dates})


def _frame_hash(frame: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(frame, index=True).values.tobytes()).hexdigest()[:24]


_PREDICTION_CACHE: dict[str, pd.DataFrame] = {}
_CACHE_LIMIT = 64


def _run_target(data: pd.DataFrame, target: str, horizon: int,
                context_covariates: list[str] | None = None,
                future_covariates: pd.DataFrame | None = None) -> pd.DataFrame:
    """Run exactly one target with only its specified historical context columns.

    Chronos-2's known future covariates are supplied via future_covariates. For retail
    targets, wholesale and mandi modal future values are first forecast independently,
    then passed as future covariates; no future actual values are leaked.
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
    future = _future_dates(data, horizon)
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
        future), target, str(horizon), MODEL_ID])
    if key in _PREDICTION_CACHE:
        return _PREDICTION_CACHE[key].copy(deep=True)
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
            freq="D",
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
    if len(_PREDICTION_CACHE) >= _CACHE_LIMIT:
        _PREDICTION_CACHE.pop(next(iter(_PREDICTION_CACHE)))
    _PREDICTION_CACHE[key] = result.copy(deep=True)
    return result


def _univariate_future(data: pd.DataFrame, col: str, horizon: int) -> pd.DataFrame:
    result = _run_target(data, col, horizon)
    result = result.sort_values("timestamp")
    return pd.DataFrame({
        "timestamp": result["timestamp"].values,
        col: result["0.5"].astype(float).values,
    })


def _price_target_forecast(data: pd.DataFrame, state: str, price_type: str, horizon: int) -> pd.DataFrame:
    target = PRICE_COLUMNS[state][price_type]
    covariates: list[str] = []
    future_covariates = None
    if price_type == "retail":
        # The retail target uses its own history plus same-state wholesale and mandi-modal history.
        wholesale_col = PRICE_COLUMNS[state]["wholesale"]
        mandi_col = PRICE_COLUMNS[state]["mandi_modal"]
        covariates = [wholesale_col, mandi_col]
        # Their future values are unknown: forecast each as a univariate series first.
        wholesale_future = _univariate_future(data, wholesale_col, horizon)
        mandi_future = _univariate_future(data, mandi_col, horizon)
        future_covariates = wholesale_future.merge(
            mandi_future, on="timestamp", validate="one_to_one")
    # For wholesale and mandi modal targets, context is only Date + that target.
    return _run_target(data, target, horizon, context_covariates=covariates, future_covariates=future_covariates)


def _quantile_values(row: pd.Series) -> tuple[float, float, float]:
    p10, p50, p90 = (max(0.0, float(row[k])) for k in ("0.1", "0.5", "0.9"))
    return min(p10, p50), p50, max(p50, p90)


def _price_forecast_records(data: pd.DataFrame, state: str, price_type: str, result: pd.DataFrame) -> list[dict]:
    col = PRICE_COLUMNS[state][price_type]
    latest_row = data.loc[pd.to_datetime(data["timestamp"]).idxmax()]
    latest_actual_price_rs_kg = float(latest_row[col]) / 100.0
    latest_actual_date = pd.Timestamp(latest_row["timestamp"]).date().isoformat()

    records = []
    for _, row in result.sort_values("timestamp").iterrows():
        q10, q50, q90 = _quantile_values(row)
        # Source and model units are INR/metric quintal. Convert ONLY forecast outputs to INR/kg.
        p10, p50, p90 = q10 / 100.0, q50 / 100.0, q90 / 100.0
        width = p90 - p10
        change_rs_kg = p50 - latest_actual_price_rs_kg

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
        })
    return records


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


def _status(data: pd.DataFrame) -> dict:
    last_date = pd.Timestamp(data["timestamp"].max()).date()
    stale = (date.today() - last_date).days
    warnings = []
    if stale > 1:
        warnings.append(
            f"Latest observation is {last_date.isoformat()} ({stale} day(s) behind the server date).")
    if stale < 0:
        warnings.append(
            "Latest observation date is in the future relative to the server date; verify timestamps.")
    return {"last_observation_date": last_date.isoformat(), "data_staleness_days": stale,
            "forecast_start_date": (last_date + pd.Timedelta(days=1)).isoformat(), "warnings": warnings}


def _metadata() -> dict:
    return {"model": MODEL_ID, "frequency": "daily", "observation_price_unit": "INR/metric quintal",
            "forecast_price_unit": "INR/kg", "quantity_unit": "metric quintals/day (source period needs validation)",
            "quantiles": {"lower": 0.1, "median": 0.5, "upper": 0.9},
            "prediction_interval": {"nominal_coverage": 0.8, "description": "P10–P90 is a nominal central 80% prediction interval, not a guarantee."},
            "confidence_score": {"available": False, "reason": "Chronos-2 returns forecast quantiles, not a native calibrated scalar confidence score."}}


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
def ap_retail_graph(
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
    forecast_display_start = _forecast_display_start_date(data, start_date, end_date)
    horizon_days = _date_range_horizon(data, start_date, end_date)
    forecast: list[dict] = []
    context_cols: list[str] = []
    for price_type in selected:
        target = PRICE_COLUMNS["AP"][price_type]
        if price_type == "retail":
            context_cols.extend(
                [target, PRICE_COLUMNS["AP"]["wholesale"], PRICE_COLUMNS["AP"]["mandi_modal"]])
        else:
            context_cols.append(target)
        if horizon_days and forecast_display_start:
            result = _price_target_forecast(data, "AP", price_type, horizon_days)
            forecast.extend(_filter_date_records(
                _price_forecast_records(data, "AP", price_type, result),
                forecast_display_start,
                end_date,
            ))
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
    return {"graph": "ap_price_forecast", "title": "Andhra Pradesh Onion Price Forecast", "state": "AP",
            "state_name": STATE_NAMES["AP"], "price_types": selected, "unit": "INR/kg",
            "threshold_rs_kg": threshold_rs_kg, "horizon_days": horizon_days,
            "model_history_rows": int(len(data)), "visible_start_date": start_date.isoformat(),
            "visible_end_date": end_date.isoformat(),
            "requested_end_date": end_date.isoformat(),
            "actual_end_date": actual_end_date.isoformat(),
            "context_logic": {"retail": ["timestamp", PRICE_COLUMNS["AP"]["retail"], PRICE_COLUMNS["AP"]["wholesale"], PRICE_COLUMNS["AP"]["mandi_modal"]],
                              "wholesale": ["timestamp", PRICE_COLUMNS["AP"]["wholesale"]],
                              "mandi_modal": ["timestamp", PRICE_COLUMNS["AP"]["mandi_modal"]]},
            "historical": historical, "forecast": forecast, "metadata": _metadata(), "saved_to_sqlite": saved,
            **status,
            "forecast_start_date": forecast_display_start.isoformat() if forecast_display_start else None,
            "forecast_end_date": max((r["date"] for r in forecast), default=None)}


@app.get("/api/graphs/state-comparison")
def state_comparison_graph(
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
    forecast_display_start = _forecast_display_start_date(data, start_date, end_date)
    horizon_days = _date_range_horizon(data, start_date, end_date)

    forecast: list[dict] = []
    historical: list[dict] = []
    context_cols: list[str] = []
    for state in selected_states:
        for price_type in selected_types:
            target = PRICE_COLUMNS[state][price_type]
            if price_type == "retail":
                context_cols.extend([
                    target,
                    PRICE_COLUMNS[state]["wholesale"],
                    PRICE_COLUMNS[state]["mandi_modal"],
                ])
            else:
                context_cols.append(target)

            if horizon_days and forecast_display_start:
                result = _price_target_forecast(
                    data, state, price_type, horizon_days)
                forecast.extend(_filter_date_records(
                    _price_forecast_records(data, state, price_type, result),
                    forecast_display_start,
                    end_date,
                ))
            historical.extend(
                _history_price_records(
                    data, state, price_type, start_date, actual_end_date)
            )

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
                           description="Last date to display and forecast through, YYYY-MM-DD; more than 180 days after the latest observation returns an empty graph"),
) -> dict:
    """Graph 3: forecast supply=arrival+production and consumption as independent univariate targets."""
    data = get_data()
    selected_states = _parse_list(states, set(STATE_CODES), "states")
    horizon_days = _date_range_horizon(data, start_date, end_date)
    forecast_rows: list[dict] = []
    historical: list[dict] = []
    indexed: dict[tuple[str, str], dict[str, dict]] = {}
    db_values: list[dict] = []
    timestamps = pd.to_datetime(data["timestamp"]).dt.date
    history_mask = (timestamps >= start_date) & (timestamps <= end_date)
    for state in selected_states:
        supply_col = SUPPLY_COLUMNS[state]
        consumption_col = QUANTITY_COLUMNS[state]["consumption"]
        arrival_col = QUANTITY_COLUMNS[state]["arrival"]
        production_col = QUANTITY_COLUMNS[state]["production"]
        # Explicit requested logic: supply = arrival + production; target uses only Date + Supply.
        supply_result = _run_target(data, supply_col, horizon_days)
        consumption_result = _run_target(data, consumption_col, horizon_days)
        arrival_result = _run_target(data, arrival_col, horizon_days)
        production_result = _run_target(data, production_col, horizon_days)
        for metric, result in (("supply", supply_result),
    ("consumption", consumption_result),
    ("arrival", arrival_result),
    ("production", production_result)):
            for _, row in result.sort_values("timestamp").iterrows():
                q10, q50, q90 = _quantile_values(row)
                day = pd.Timestamp(row["timestamp"]).date().isoformat()
                if not start_date.isoformat() <= day <= end_date.isoformat():
                    continue
                item = {"date": day, "state": state, "state_name": STATE_NAMES[state], "metric": metric,
                        "predicted_quantity_mq_day": round(q50, 4), "p10_mq_day": round(q10, 4),
                        "p50_mq_day": round(q50, 4), "p90_mq_day": round(q90, 4),
                        "unit": "metric_quintal/day"}
                forecast_rows.append(item)
                indexed.setdefault((state, day), {})[metric] = item
        # Historical data returns supply and consumption only as target-series context; arrival/production
        # components remain in SQLite and are combined into supply on read.
        for metric, col in (("supply", supply_col),
    ("consumption", consumption_col),
    ("arrival", arrival_col),
    ("production", production_col),):
            for row in data.loc[history_mask, ["timestamp", col]].itertuples(index=False):
                historical.append({"date": row.timestamp.date().isoformat(), "state": state, "state_name": STATE_NAMES[state],
                                   "metric": metric, "actual_quantity_mq_day": round(float(getattr(row, col)), 4),
                                   "unit": "metric_quintal/day"})
    balance = []
    for (state, day), vals in sorted(indexed.items()):
        if "supply" not in vals or "consumption" not in vals:
            continue
        supply, cons = vals["supply"], vals["consumption"]
        # Conservative component bounds; these are not a calibrated joint interval.
        p10 = supply["p10_mq_day"] - cons["p90_mq_day"]
        p50 = supply["predicted_quantity_mq_day"] - \
            cons["predicted_quantity_mq_day"]
        p90 = supply["p90_mq_day"] - cons["p10_mq_day"]
        balance.append({"date": day, "state": state, "state_name": STATE_NAMES[state],
                        "shortfall_p10_metric_quintal_day": round(p10, 4),
                        "shortfall_p50_metric_quintal_day": round(p50, 4),
                        "shortfall_p90_metric_quintal_day": round(p90, 4),
                        "classification": "potential_surplus" if p50 > 0 else ("potential_shortfall" if p50 < 0 else "near_balance")})
    for row in forecast_rows:
        db_values.append({"state_code": row["state"], "target_key": row["metric"], "forecast_date": row["date"],
                          "p10": row["p10_mq_day"], "p50": row["predicted_quantity_mq_day"],
                          "p90": row["p90_mq_day"], "unit": "metric_quintal/day"})
    for row in balance:
        db_values.append({"state_code": row["state"], "target_key": "shortfall", "forecast_date": row["date"],
                          "p10": row["shortfall_p10_metric_quintal_day"], "p50": row["shortfall_p50_metric_quintal_day"],
                          "p90": row["shortfall_p90_metric_quintal_day"], "unit": "metric_quintal/day"})
    context_cols = []
    for state in selected_states:
        context_cols.extend(
            [SUPPLY_COLUMNS[state],
    QUANTITY_COLUMNS[state]["consumption"],
    QUANTITY_COLUMNS[state]["arrival"],
    QUANTITY_COLUMNS[state]["production"],])
    saved = save_forecast_run("graph3_supply_balance", horizon_days,
                              pd.Timestamp(
                                  data["timestamp"].max()).date().isoformat(),
                              _context_signature(
                                  data, context_cols), db_values, MODEL_ID,
                              {"context_logic": {s: {"supply": ["timestamp", SUPPLY_COLUMNS[s]],
                                                     "consumption": ["timestamp", QUANTITY_COLUMNS[s]["consumption"]], "arrival": ["timestamp", QUANTITY_COLUMNS[s]["arrival"]],
"production": ["timestamp", QUANTITY_COLUMNS[s]["production"]],}
                                                 for s in selected_states},
                               "balance_formula": "supply - consumption", "unit": "metric_quintal/day"})
    warnings = [
        "Supply is calculated as arrivals + production as requested; confirm that arrivals and production are compatible flows and do not double-count the same supply.",
        "The source production/consumption series repeat across many daily rows; verify their real reporting frequency before interpreting daily forecasts.",
        "Shortfall is supply minus consumption: negative values indicate demand exceeds supply; positive values indicate surplus.",
        "Shortfall P10/P90 combine component quantiles conservatively and are not a calibrated joint prediction interval.",
    ]
    return {"graph": "supply_demand_shortfall", "title": "Onion Supply, Consumption and Shortfall Forecast",
            "selected_states": selected_states, "selected_state_names": [STATE_NAMES[x] for x in selected_states],
            "horizon_days": horizon_days, "quantity_unit": "metric_quintal/day", "historical": historical,
            "quantity_forecast": forecast_rows, "balance_forecast": balance,
            "balance_formula": "supply - consumption", "warnings": warnings, "metadata": _metadata(),
            "saved_to_sqlite": saved, **_status(data),
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
