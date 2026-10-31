# Onion Forecasting Intelligence — API Documentation

This document covers all API endpoints, parameters, request headers, service logic, and response schemas implemented across the codebase (`main.py`, `history_store.py`, and `upag_client.py`).

---

## Quick Reference & Base URL

- **Base URL**: `http://127.0.0.1:8000`
- **Interactive Swagger Docs**: `http://127.0.0.1:8000/docs`
- **ReDoc**: `http://127.0.0.1:8000/redoc`
- **Authentication**: All `/api/*` routes except CSV import require `Authorization: Bearer <API_BEARER_TOKEN>`.
- **Model Engine**: Amazon Chronos-2 (`amazon/chronos-2`) probabilistic zero-shot time-series model
- **Database**: SQLite (`data/onion_forecasting_v4.sqlite3`)
- **Key Units**:
  - Observation prices in SQLite: ₹ / metric quintal
  - Forecast prices in API & persistence: ₹ / kg (converted automatically: divide by 100)
  - Historical prices returned in graphs: ₹ / kg (converted for direct comparison with forecasts)
  - Graph 1/2 daily arrivals: Metric quintals / day in model context
  - Graph 3 AP production, consumption, arrivals, and shortfall: Tonnes / month

Protected data and forecast routes require `Authorization: Bearer <API_BEARER_TOKEN>`.
Configure `API_BEARER_TOKEN` in the API server's `.env`; keep the value in a trusted
server-side secret store rather than frontend browser code. The CSV import route uses
its separate `X-Ingestion-Key`, while `/`, `/health`, and CORS preflight `OPTIONS`
requests do not require the bearer token.

---

## 1. Core Forecasting Graph APIs

These endpoints execute Chronos-2 predictions on historical market data and automatically persist forecast runs and quantile values to SQLite.

All three graph endpoints require `start_date` and `end_date` in `YYYY-MM-DD` format.
Actual observations and forecast records are returned only inside this inclusive date
range. Graphs 1 and 2 use daily data and forecast from the day after the latest
observation. Graph 3 uses monthly AP data and forecasts month-start periods through the
month containing `end_date`. The existing 180-day request limit is retained for all
graphs. `horizon_days` remains the calendar-day distance from the latest source date to
the requested end date, capped at 180. If `end_date` exceeds the supported horizon, the
API forecasts through the latest supported date and returns a successful, partial-range
response rather than rejecting the request. Graph 3 forecasts month-start periods through
the last monthly observation not beyond that date. Successful forecasts are saved to
SQLite and identical predictions may be reused from the API's in-memory cache.
`start_date` must be on or before `end_date`.

The 180-day cap is measured from the latest observation in the API server's database,
not from `start_date` or from the length of the selected display range. If a short
selection exceeds the cap, check `last_observation_date` and `horizon_days` in the
response, then compare the remote server's `GET /api/ingestion/status` with the local
server. Sync or redeploy current history to the remote API before retrying.

For Graph 1 and Graph 2, actual observations are returned from `start_date` through
`actual_end_date` (the earlier of `end_date` and the server's current date). Forecast
rows are returned from today through the effective forecast limit date, but never overlap an actual observation:
if today's observation is already present, forecasts start the next day. Their responses
include `requested_end_date`, `forecast_limit_date`, `forecast_truncated`,
`actual_end_date`, and `forecast_start_date`. `forecast_end_date` is the last point
actually returned. The `visible_end_date` remains the requested `end_date`. If `end_date`
is before today, these endpoints return historical observations only and do not run or
save a forecast. Graph 3 returns month-start points up to the same 180-day limit.

### 1.1 Graph 1: Andhra Pradesh (AP) Price Forecast
*Endpoint for predicting retail, wholesale, and mandi modal onion prices for Andhra Pradesh.*

- **Method**: `GET`
- **Path**: `/api/graphs/ap-retail`
- **Description**: Returns historical observations and future forecasts for Andhra Pradesh using the configured daily price dataset. The three price targets use only their specified AP price and arrival history.

#### Query Parameters:
| Parameter | Type | Required | Default | Allowed Values / Constraints | Description |
|---|---|---|---|---|---|
| `price_types` | string | No | `retail` | Comma-separated: `retail`, `wholesale`, `mandi_modal` | Price series to include. Case-insensitive. |
| `start_date` | date | Yes | — | `YYYY-MM-DD` | First date to include in returned actual and forecast records. |
| `end_date` | date | Yes | — | `YYYY-MM-DD`; requests beyond 180 days return the maximum supported forecast range | Requested last date. If beyond the limit, response includes forecasts only through `forecast_limit_date` and sets `forecast_truncated` to `true`. |
| `threshold_rs_kg` | number | No | `44.0` | Greater than or equal to `0` | Chart reference threshold in INR/kg; echoed in the response. |

#### Target & Covariate Logic:
- **`retail`**: Context uses Date + AP Retail + AP Wholesale + AP Mandi Modal + AP Arrivals. Future covariates are independently forecast first.
- **`wholesale`**: Context uses Date + AP Wholesale + AP Mandi Modal + AP Arrivals. Future covariates are independently forecast first.
- **`mandi_modal`**: Context uses Date + AP Mandi Modal + AP Arrivals. Future arrivals are forecast first.

#### Example Request:
```http
GET /api/graphs/ap-retail?price_types=retail,wholesale,mandi_modal&start_date=2026-10-01&end_date=2026-11-30&threshold_rs_kg=44
```

#### Sample Output:
```json
{
  "graph": "ap_price_forecast",
  "title": "Andhra Pradesh Onion Price Forecast",
  "state": "AP",
  "state_name": "Andhra Pradesh",
  "price_types": ["retail", "wholesale", "mandi_modal"],
  "unit": "INR/kg",
  "threshold_rs_kg": 44.0,
  "horizon_days": 46,
  "model_history_rows": 1760,
  "visible_start_date": "2026-10-01",
  "visible_end_date": "2026-11-30",
  "requested_end_date": "2026-11-30",
  "actual_end_date": "2026-10-29",
  "context_logic": {
    "retail": ["timestamp", "AP_retail_rs_per_metric_quintal", "AP_wholesale_rs_per_metric_quintal", "AP_mandi_modal_rs_per_metric_quintal", "AP_arrival_metric_quintal"],
    "wholesale": ["timestamp", "AP_wholesale_rs_per_metric_quintal", "AP_mandi_modal_rs_per_metric_quintal", "AP_arrival_metric_quintal"],
    "mandi_modal": ["timestamp", "AP_mandi_modal_rs_per_metric_quintal", "AP_arrival_metric_quintal"]
  },
  "historical": [
    {
      "date": "2026-10-01",
      "state": "AP",
      "price_type": "retail",
      "price_type_label": "Retail price",
      "actual_price_rs_kg": 32.50
    }
  ],
  "forecast": [
    {
      "date": "2026-10-29",
      "state": "AP",
      "state_name": "Andhra Pradesh",
      "price_type": "retail",
      "price_type_label": "Retail price",
      "latest_actual_date": "2026-10-15",
      "latest_actual_price_rs_kg": 32.50,
      "predicted_price_rs_kg": 34.20,
      "change_rs_kg": 1.70,
      "change_percent": 5.23,
      "change_direction": "increase",
      "p10_rs_kg": 30.10,
      "p50_rs_kg": 34.20,
      "p90_rs_kg": 38.80,
      "prediction_interval_width_rs_kg": 8.70,
      "interval_width_pct_of_median": 25.44,
      "confidence_score": 32.6
    }
  ],
  "metadata": {
    "model": "amazon/chronos-2",
    "frequency": "daily",
    "observation_price_unit": "INR/metric quintal",
    "forecast_price_unit": "INR/kg",
    "quantity_unit": "metric quintals/day",
    "quantiles": {"lower": 0.1, "median": 0.5, "upper": 0.9},
    "prediction_interval": {
      "nominal_coverage": 0.8,
      "description": "P10–P90 is a nominal central 80% prediction interval, not a guarantee."
    },
    "confidence_score": {
      "available": true,
      "method": "tolerance_band_probability",
      "applies_to": "Graph 1/2 daily price forecast rows",
      "description": "Derived per-day probability (%) that the actual price stays within ±5% of the forecast median, computed from the Chronos-2 P10/P90 spread stretched by the calibration factor k. Monotone (non-increasing) across forecast dates per series.",
      "spread_multiplier_k": 1.2,
      "tolerance_band_pct": 5.0,
      "calibrated": false,
      "note": "k=1.2 is an uncalibrated placeholder; when backtests supply calibrated values, set ONION_CONFIDENCE_SPREAD_K=<k> and ONION_CONFIDENCE_K_CALIBRATED=1."
    }
  },
  "saved_to_sqlite": {
    "run_id": 1,
    "generated_at": "2026-10-27T18:00:00Z",
    "saved_values": 138,
    "graph_code": "graph1_ap_prices"
  },
  "last_observation_date": "2026-10-15",
  "data_staleness_days": 12,
  "forecast_start_date": "2026-10-29",
  "forecast_end_date": "2026-11-30",
  "warnings": [
    "Latest observation is 2026-10-15 (14 day(s) behind the server date)."
  ]
}
```

---

### 1.2 Graph 2: Multi-State Price Comparison
*Endpoint for comparing onion prices across key producing and consuming states.*

- **Method**: `GET`
- **Path**: `/api/graphs/state-comparison`
- **Description**: Compares onion prices across any combination of supported states (`AP`, `KA`, `MH`, `MP`) and price types.
- Retail targets use their own historical price plus same-state wholesale, mandi modal, and arrival inputs. Wholesale targets use same-state mandi modal and arrivals; mandi modal targets use same-state arrivals. Unknown future covariates are independently forecast first.

#### Query Parameters:
| Parameter | Type | Required | Default | Allowed Values / Constraints | Description |
|---|---|---|---|---|---|
| `states` | string | No | `AP,KA,MH,MP` | Comma-separated: `AP`, `KA`, `MH`, `MP` | States to forecast and compare. |
| `price_types` | string | No | `retail,wholesale,mandi_modal` | Comma-separated: `retail`, `wholesale`, `mandi_modal` | Price types to include. |
| `start_date` | date | Yes | — | `YYYY-MM-DD` | First date to include in returned actual and forecast records. |
| `end_date` | date | Yes | — | `YYYY-MM-DD`; requests beyond 180 days return the maximum supported forecast range | Requested last date. If beyond the limit, response includes forecasts only through `forecast_limit_date` and sets `forecast_truncated` to `true`. |
| `threshold_rs_kg` | number | No | `44.0` | Greater than or equal to `0` | Chart reference threshold in INR/kg; echoed in the response. |

#### Example Request:
```http
GET /api/graphs/state-comparison?states=AP,MH&price_types=retail,wholesale&start_date=2026-10-01&end_date=2026-11-30&threshold_rs_kg=44
```

#### Sample Output:
```json
{
  "graph": "state_price_comparison",
  "title": "Selected States — Onion Price Forecasts",
  "selected_states": ["AP", "MH"],
  "selected_state_names": ["Andhra Pradesh", "Maharashtra"],
  "selected_price_types": ["retail", "wholesale"],
  "horizon_days": 46,
  "unit": "INR/kg",
  "threshold_rs_kg": 44.0,
  "visible_start_date": "2026-10-01",
  "visible_end_date": "2026-11-30",
  "requested_end_date": "2026-11-30",
  "actual_end_date": "2026-10-29",
  "historical": [
    {
      "date": "2026-10-01",
      "state": "MH",
      "price_type": "wholesale",
      "price_type_label": "Wholesale price",
      "actual_price_rs_kg": 24.10
    }
  ],
  "forecast": [
    {
      "date": "2026-10-29",
      "state": "MH",
      "state_name": "Maharashtra",
      "price_type": "wholesale",
      "price_type_label": "Wholesale price",
      "latest_actual_date": "2026-10-15",
      "latest_actual_price_rs_kg": 24.10,
      "predicted_price_rs_kg": 26.50,
      "change_rs_kg": 2.40,
      "change_percent": 9.96,
      "change_direction": "increase",
      "p10_rs_kg": 23.20,
      "p50_rs_kg": 26.50,
      "p90_rs_kg": 30.10,
      "prediction_interval_width_rs_kg": 6.90,
      "interval_width_pct_of_median": 26.04,
      "confidence_score": 31.9
    }
  ],
  "metadata": {
    "model": "amazon/chronos-2",
    "frequency": "daily",
    "observation_price_unit": "INR/metric quintal",
    "forecast_price_unit": "INR/kg",
    "quantity_unit": "metric quintals/day",
    "quantiles": {"lower": 0.1, "median": 0.5, "upper": 0.9},
    "prediction_interval": {
      "nominal_coverage": 0.8,
      "description": "P10–P90 is a nominal central 80% prediction interval, not a guarantee."
    },
    "confidence_score": {
      "available": true,
      "method": "tolerance_band_probability",
      "applies_to": "Graph 1/2 daily price forecast rows",
      "description": "Derived per-day probability (%) that the actual price stays within ±5% of the forecast median, computed from the Chronos-2 P10/P90 spread stretched by the calibration factor k. Monotone (non-increasing) across forecast dates per series.",
      "spread_multiplier_k": 1.2,
      "tolerance_band_pct": 5.0,
      "calibrated": false,
      "note": "k=1.2 is an uncalibrated placeholder; when backtests supply calibrated values, set ONION_CONFIDENCE_SPREAD_K=<k> and ONION_CONFIDENCE_K_CALIBRATED=1."
    }
  },
  "saved_to_sqlite": {
    "run_id": 2,
    "generated_at": "2026-10-27T18:05:00Z",
    "saved_values": 184,
    "graph_code": "graph2_state_comparison"
  },
  "last_observation_date": "2026-10-15",
  "data_staleness_days": 12,
  "forecast_start_date": "2026-10-29",
  "forecast_end_date": "2026-11-30",
  "warnings": [
    "Latest observation is 2026-10-15 (14 day(s) behind the server date)."
  ]
}
```

---

### 1.3 Graph 3: AP Monthly Supply-Demand Balance Forecast
*Endpoint for forecasting monthly AP production, consumption, and arrivals and deriving the supply-demand shortfall.*

- **Method**: `GET`
- **Path**: `/api/graphs/supply-balance`
- **Description**: Reads AP monthly quantities from the configured monthly dataset, forecasts production, consumption, and arrivals independently in tonnes/month, then derives shortfall with conservative component bounds. No separate supply or shortfall target is forecast.

#### Query Parameters:
| Parameter | Type | Required | Default | Allowed Values / Constraints | Description |
|---|---|---|---|---|---|
| `states` | string | No | `AP` | `AP` only | AP is the only state in the monthly dataset. Other state codes receive HTTP 422. |
| `start_date` | date | Yes | — | `YYYY-MM-DD` | First date to include in returned actual and forecast records. |
| `end_date` | date | Yes | — | `YYYY-MM-DD`; requests beyond 180 days return the maximum supported forecast range | Requested last date; monthly forecast points are returned through the latest month start within `forecast_limit_date`. |

#### Formula & Logic:
- **Source data**: AP production, consumption, and monthly arrivals from the configured monthly CSV; all values are in tonnes/month.
- **Forecast response**: `quantity_forecast` contains one row per month and metric (`production`, `consumption`, or `arrival`), with P10/P50/P90 quantity fields. The descriptive `*_tonnes_month` fields are provided alongside the legacy `*_mq_day` compatibility fields.
- **Historical response**: `historical` contains the same three metrics and actual values in `actual_quantity_tonnes_month`, alongside the legacy `actual_quantity_mq_day` field.
- **Shortfall Formula**: `Shortfall = Consumption - (Arrival + Production)`
  - $P_{50} = \text{Consumption } P_{50} - \text{Arrival } P_{50} - \text{Production } P_{50}$
  - $P_{10} = \text{Consumption } P_{10} - \text{Arrival } P_{90} - \text{Production } P_{90}$ (conservative lower bound)
  - $P_{90} = \text{Consumption } P_{90} - \text{Arrival } P_{10} - \text{Production } P_{10}$ (conservative upper bound)
  - Positive values and `potential_shortfall` mean demand exceeds estimated supply; negative values and `potential_surplus` mean estimated supply exceeds demand.

#### Example Request:
```http
GET /api/graphs/supply-balance?states=AP&start_date=2026-10-01&end_date=2026-11-02
```

#### Sample Output:
```json
{
  "graph": "supply_demand_shortfall",
  "title": "Andhra Pradesh Monthly Supply-Demand Balance Forecast",
  "selected_states": ["AP"],
  "selected_state_names": ["Andhra Pradesh"],
  "horizon_days": 32,
  "quantity_unit": "tonnes/month",
  "historical": [
    {
      "date": "2026-10-01",
      "state": "AP",
      "state_name": "Andhra Pradesh",
      "metric": "production",
      "actual_quantity_tonnes_month": 53990.0,
      "unit": "tonnes/month"
    }
  ],
  "quantity_forecast": [
    {
      "date": "2026-11-01",
      "state": "AP",
      "state_name": "Andhra Pradesh",
      "metric": "production",
      "predicted_quantity_tonnes_month": 54017.0,
      "p10_tonnes_month": 51316.15,
      "p50_tonnes_month": 54017.0,
      "p90_tonnes_month": 56717.85,
      "unit": "tonnes/month"
    },
    {
      "date": "2026-11-01",
      "state": "AP",
      "state_name": "Andhra Pradesh",
      "metric": "consumption",
      "predicted_quantity_tonnes_month": 71695.83,
      "p10_tonnes_month": 68111.0385,
      "p50_tonnes_month": 71695.83,
      "p90_tonnes_month": 75280.6215,
      "unit": "tonnes/month"
    },
    {
      "date": "2026-11-01",
      "state": "AP",
      "state_name": "Andhra Pradesh",
      "metric": "arrival",
      "predicted_quantity_tonnes_month": 4856.3269,
      "p10_tonnes_month": 4613.5106,
      "p50_tonnes_month": 4856.3269,
      "p90_tonnes_month": 5099.1433,
      "unit": "tonnes/month"
    }
  ],
  "balance_forecast": [
    {
      "date": "2026-11-01",
      "state": "AP",
      "state_name": "Andhra Pradesh",
      "shortfall_p10_tonnes_month": 6294.0504,
      "shortfall_p50_tonnes_month": 12822.5031,
      "shortfall_p90_tonnes_month": 19350.9609,
      "classification": "potential_shortfall"
    }
  ],
  "balance_formula": "consumption - (arrival + production)",
  "metadata": {
    "model": "amazon/chronos-2",
    "frequency": "monthly",
    "quantity_unit": "tonnes/month",
    "quantiles": {"lower": 0.1, "median": 0.5, "upper": 0.9},
    "prediction_interval": {
      "nominal_coverage": 0.8,
      "description": "P10–P90 is a nominal central 80% prediction interval, not a guarantee."
    },
    "confidence_score": {
      "available": false,
      "reason": "The tolerance-band confidence is implemented for Graph 1/2 daily price rows; Graph 3 combines component forecasts, so a per-day score is not applied yet."
    }
  },
  "warnings": [
    "Shortfall = Consumption - (Arrival + Production). Positive values indicate potential shortfall; negative values indicate potential surplus.",
    "Shortfall P10/P90 combine component quantiles conservatively and are not a calibrated joint prediction interval."
  ],
  "saved_to_sqlite": {
    "run_id": 3,
    "generated_at": "2026-10-27T18:10:00Z",
    "saved_values": 4,
    "graph_code": "graph3_supply_balance"
  },
  "last_observation_date": "2026-10-01",
  "forecast_start_date": "2026-11-01",
  "forecast_end_date": "2026-11-01"
}
```

---

## 2. Forecast Persistence & Run History APIs

Every forecast generated by the graph endpoints is automatically stored in SQLite. These endpoints allow querying stored runs.

### 2.1 Get Latest Saved Forecast
- **Method**: `GET`
- **Path**: `/api/forecasts/latest`
- **Description**: Returns the latest forecast run and its detailed predicted quantile values.

#### Query Parameters:
| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `graph_code` | string | No | `null` | Optional filter: `graph1_ap_prices`, `graph2_state_comparison`, or `graph3_supply_balance`. If omitted, returns the latest run across all graphs. |

#### Sample Output:
```json
{
  "forecast": {
    "run_id": 12,
    "graph_code": "graph1_ap_prices",
    "generated_at": "2026-10-27T17:45:10Z",
    "model_id": "amazon/chronos-2",
    "horizon_days": 14,
    "last_observation_date": "2026-10-15",
    "context_hash": "a1b2c3d4e5f6789012345678",
    "metadata": {
      "stored_forecast_price_unit": "INR/kg",
      "observation_price_unit": "INR/metric quintal",
      "quantiles": [0.1, 0.5, 0.9]
    },
    "forecast_values": [
      {
        "state_code": "AP",
        "target_key": "retail",
        "forecast_date": "2026-10-16",
        "p10": 30.12,
        "p50": 34.50,
        "p90": 39.20,
        "unit": "INR/kg"
      }
    ]
  }
}
```

---

### 2.2 List Recent Forecast Runs
- **Method**: `GET`
- **Path**: `/api/forecasts/runs`
- **Description**: Returns a summary list of past forecast runs (without expanding individual value rows).

#### Query Parameters:
| Parameter | Type | Required | Default | Allowed Values | Description |
|---|---|---|---|---|---|
| `limit` | integer | No | `20` | `1` to `100` | Number of recent runs to return. |

#### Sample Output:
```json
{
  "runs": [
    {
      "run_id": 12,
      "graph_code": "graph1_ap_prices",
      "generated_at": "2026-10-27T17:45:10Z",
      "model_id": "amazon/chronos-2",
      "horizon_days": 14,
      "last_observation_date": "2026-10-15",
      "context_hash": "a1b2c3d4e5f6789012345678"
    },
    {
      "run_id": 11,
      "graph_code": "graph3_supply_balance",
      "generated_at": "2026-10-27T17:30:22Z",
      "model_id": "amazon/chronos-2",
      "horizon_days": 30,
      "last_observation_date": "2026-10-15",
      "context_hash": "b2c3d4e5f6a1789012345678"
    }
  ]
}
```

---

## 3. Data Health, Staleness & Ingestion APIs

These endpoints provide observability into the underlying SQLite database, observation freshness, and dataset synchronization.

### 3.1 Service Root
- **Method**: `GET`
- **Path**: `/`
- **Description**: Entrypoint providing API service information, links to Swagger UI docs, and sample graph endpoints.

#### Sample Output:
The graph values below are bare paths; add the required `start_date` and `end_date`
parameters (and any optional filters) when calling a graph endpoint.

```json
{
  "service": "Onion Forecasting MVP API v3",
  "docs": "/docs",
  "graphs": {
    "graph_1_ap_prices": "/api/graphs/ap-retail",
    "graph_2_state_comparison": "/api/graphs/state-comparison",
    "graph_3_supply_balance": "/api/graphs/supply-balance"
  },
  "ops": {
    "health": "/health",
    "ingestion_status": "/api/ingestion/status",
    "saved_forecasts": "/api/forecasts/latest"
  }
}
```

---

### 3.2 Health Check
- **Method**: `GET`
- **Path**: `/health`
- **Description**: Verifies API operational status, SQLite connection, whether Chronos-2 pipeline is loaded in memory, and dataset staleness relative to system clock.

#### Sample Output:
```json
{
  "status": "ok",
  "model_loaded": true,
  "model_id": "amazon/chronos-2",
  "history_db": "c:\\Users\\user\\Desktop\\Onion-Forecasting-Intelligence\\data\\onion_forecasting_v4.sqlite3",
  "ingestion": {
    "database_path": "c:\\Users\\user\\Desktop\\Onion-Forecasting-Intelligence\\data\\onion_forecasting_v4.sqlite3",
    "observation_rows": 1760,
    "first_observation_date": "2022-01-01",
    "last_observation_date": "2026-10-15",
    "forecast_run_count": 12,
    "last_ingestion": {
      "id": 1,
      "checked_at": "2026-10-27T10:00:00Z",
      "source": "initial_csv_seed",
      "status": "success",
      "rows_seen": 1760,
      "inserted": 1760,
      "updated": 0,
      "unchanged": 0,
      "latest_observation_date": "2026-10-15",
      "message": null
    }
  },
  "last_observation_date": "2026-10-15",
  "data_staleness_days": 12,
  "forecast_start_date": "2026-10-16",
  "warnings": [
    "Latest observation is 2026-10-15 (12 day(s) behind the server date)."
  ]
}
```

---

### 3.3 Data Status
- **Method**: `GET`
- **Path**: `/api/data/status`
- **Description**: Lightweight overview of observation freshness, staleness warning, and last sync summary.

#### Response Shape:
Returns `ingestion` (the same object as `/api/ingestion/status`) plus
`last_observation_date`, `data_staleness_days`, `forecast_start_date`, and `warnings`.

---

### 3.4 Ingestion Status
- **Method**: `GET`
- **Path**: `/api/ingestion/status`
- **Description**: Returns database statistics including total observation rows, date span, total forecast runs, and the latest ingestion audit log.

#### Response Shape:
`database_path`, `observation_rows`, `first_observation_date`,
`last_observation_date`, `forecast_run_count`, and `last_ingestion` (an ingestion-log
object with `id`, `checked_at`, `source`, `status`, `rows_seen`, `inserted`,
`updated`, `unchanged`, `latest_observation_date`, and `message`, or `null`).

---

### 3.5 Sync / Import Model-Ready CSV
- **Method**: `POST`
- **Path**: `/api/ingestion/sync-csv`
- **Description**: Performs a timestamp-based upsert of a combined model-ready CSV located inside the `data/` directory into the SQLite database. Invalidates the prediction cache. Protected by an API key header.

#### Headers:
| Header | Type | Required | Description |
|---|---|---|---|
| `X-Ingestion-Key` | string | **Yes** | Secret token matching `INGESTION_API_KEY` defined in `.env`. |

#### Query Parameters:
| Parameter | Type | Required | Description |
|---|---|---|---|
| `filename` | string | **Yes** | Name of the CSV file inside `data/` (e.g. `Onion_Model_Ready_Dataset_Chronos2.csv`). Cannot contain directory traversal paths. |

#### Sample Request:
```http
POST /api/ingestion/sync-csv?filename=Onion_Model_Ready_Dataset_Chronos2_2026-10-26_Interpolated_Metric_Quintal.csv
X-Ingestion-Key: your-secret-key-here
```

#### Sample Output:
```json
{
  "status": "success",
  "source": "csv_import:Onion_Model_Ready_Dataset_Chronos2_2026-10-26_Interpolated_Metric_Quintal.csv",
  "rows_seen": 1760,
  "inserted": 0,
  "updated": 15,
  "unchanged": 1745,
  "latest_observation_date": "2026-10-15",
  "history": {
    "database_path": "c:\\...\\data\\onion_forecasting_v4.sqlite3",
    "observation_rows": 1760,
    "first_observation_date": "2022-01-01",
    "last_observation_date": "2026-10-15",
    "forecast_run_count": 12
  }
}
```

---

## 4. External Data Integration Service: UPAg Client (`upag_client.py`)

This service integrates with the government **UPAg (Unified Portal for Agricultural Statistics)** Open API at `https://data.upag.gov.in`. It securely manages authentication and raw source extraction without altering schema.

### Endpoints Consumed:
| Method | UPAg Path | Service Function | Description |
|---|---|---|---|
| `POST` | `/v1/upag/api-data-share/login` | `UPAgClient.login()` | Authenticates using credentials in `.env` (`UPAG_USERNAME`, `UPAG_PASSWORD`, `UPAG_CLIENT_ID`, `UPAG_CLIENT_SECRET`) via OAuth2 password grant to acquire a Bearer token. |
| `GET` | `/v1/upag/api-data-share/sources/user-allowed-sources` | `UPAgClient.allowed_sources()` | Returns the list of datasets/sources authorized for the account. |
| `POST` | `/v1/upag/api-data-share/sources/{source_name}` | `UPAgClient.fetch_source(source_name, payload)` | Queries a specific allowed source with a user-supplied JSON filter object. |

### Allowed Sources Supported:
`agmarknet`, `dafw_state`, `dafw_district`, `doca`, `enam`, `horticulture`, `fci_procurement`, `fci_stock`, `mncfc`, `ieg`, `dcs`, `dgcis`, `farmers_survey`, `nsso`, `cwwg`, `pmfby_ay`, `trs`, `state_reported`, `sopa`, `wpi`, `ncdex`, `pink_sheet`.

### CLI Service Usage:
```powershell
# 1. List sources allowed for your account:
python upag_client.py --list-sources

# 2. Fetch raw source data using a JSON payload file:
python upag_client.py --source agmarknet --payload-file payload.json --output data/raw_agmarknet.json
```

---

## 5. Summary of Error Codes & HTTP Statuses

### Bearer authentication for remote frontends

Every `/api/*` route except `POST /api/ingestion/sync-csv` requires a bearer token. Set
`API_BEARER_TOKEN` in the API server's `.env` to a unique secret. Generate one locally:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Keep the token in the frontend **server's** secret store and send it as:

```http
Authorization: Bearer <API_BEARER_TOKEN>
```

Do not put the shared token in browser JavaScript or a public frontend build: users can
extract it. Browser clients should call their own backend, which adds the bearer header
when it calls this API. For a cross-origin browser request, set `CORS_ORIGINS` to the
exact frontend origin; CORS is not authentication. To allow remote connections, bind
the API to an appropriate network interface (for example, in PowerShell set
`$env:API_HOST = "0.0.0.0"` before running `start_api.bat`). Expose it only over HTTPS
through a trusted reverse proxy/firewall; bearer tokens sent over plain HTTP can be
intercepted. Leave the CSV import endpoint on its separate `X-Ingestion-Key`.

| Status Code | Reason | Cause |
|---|---|---|
| `200 OK` | Success | Request succeeded and forecast/data returned. |
| `400 Bad Request` | Invalid filename | `sync-csv` filename contains path separators or does not end with `.csv`. |
| `401 Unauthorized` | Invalid credentials | Bearer token is missing/invalid, or the `X-Ingestion-Key` header is missing/invalid for CSV import. |
| `404 Not Found` | File not found | CSV file specified in `sync-csv` was not found in `data/`. |
| `422 Unprocessable Entity` | Parameter validation error | Missing/malformed dates, `start_date` after `end_date`, an end date that produces an invalid forecast horizon, negative threshold, unsupported state/price type, or invalid CSV data. |
| `500 Internal Server Error` | Execution error | Missing required column in SQLite, or Chronos-2 pipeline failure. |
| `503 Service Unavailable` | Service or Model not ready | Model dependencies missing, `API_BEARER_TOKEN` is not configured for protected API routes, or a unique `INGESTION_API_KEY` has not been configured for CSV import. |

---

## 6. Chatbot API (Postman and Frontend)

The chatbot is a read-only natural-language interface over the existing onion
forecast/history tools. It does not expose a separate login or user identity API.
All four routes below require the same `API_BEARER_TOKEN` used by the other `/api/*`
routes.

### Postman setup

1. Start the API server (for example, `start_api.bat`) and set `API_BEARER_TOKEN` in
   the server's `.env`. Set `GROQ_API_KEY` as well, and ensure `GROQ_ENABLED=1`.
2. In Postman, create environment variables:

   | Variable | Example |
   |---|---|
   | `api_base_url` | `http://127.0.0.1:8000` |
   | `api_token` | The exact value configured as `API_BEARER_TOKEN` |

3. For each request, set **Authorization → Bearer Token** to `{{api_token}}`.
   For JSON requests, set `Content-Type: application/json`.
4. Send the first chat turn, copy its returned `session_id` into
   `chat_session_id`, then use that value in follow-up turns and history requests.

The bearer token is a server secret. Do not embed it in browser JavaScript or a
public frontend build. A browser frontend should call its own backend/proxy, which
adds `Authorization: Bearer <API_BEARER_TOKEN>` when calling this API.

### Endpoint quick reference

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat` | Send a message and receive a complete answer (recommended for Postman). |
| `POST` | `/api/chat/stream` | Receive the answer as Server-Sent Events (SSE). |
| `GET` | `/api/chat/sessions?limit=20` | List recent sessions, newest activity first. |
| `GET` | `/api/chat/history?session_id={{chat_session_id}}&limit=100` | Read messages in one session, oldest first within the selected slice. |

### 1. Send a message — `POST /api/chat`

Postman URL: `{{api_base_url}}/api/chat`

```json
{
  "message": "What is the AP retail onion price forecast for next week?",
  "session_id": null,
  "wants_chart": null
}
```

`message` is required, must contain non-whitespace text, and is limited to 2,000
characters by default (`CHAT_MAX_INPUT_CHARS` can change this). Omit `session_id` or
send `null` to start a conversation. For subsequent messages, pass the `session_id`
returned by the previous response. An unknown session ID starts a new session.
`wants_chart` is optional and currently does not force or suppress a chart; a chart is
attached when the assistant uses a graph/forecast tool.

Example response:

```json
{
  "session_id": "d56f9cde8e98496397a0aa54c32dd180",
  "answer": "The AP retail forecast is ...",
  "answer_format": "markdown",
  "chart": null,
  "chart_display": "collapsed",
  "tools_used": ["forecast_ap_prices"],
  "session_title": "What is the AP retail onion price forecast for next week?",
  "warnings": []
}
```

`chart` is either `null` or a graph payload using the existing Graph 1/2/3 response
contract. `tools_used` contains tool names selected for the turn; it can be empty for
a conversational answer. `warnings` may report unavailable tool data or values
redacted because they could not be verified. Session titles are derived from the first
message and limited to 48 characters.

Postman **Tests** script for saving the returned session automatically:

```javascript
pm.test("Chat turn succeeds", function () {
  pm.response.to.have.status(200);
  const body = pm.response.json();
  pm.expect(body.session_id).to.be.a("string");
  pm.environment.set("chat_session_id", body.session_id);
});
```

Follow-up request body:

```json
{
  "message": "And what about wholesale?",
  "session_id": "{{chat_session_id}}"
}
```

### 2. Stream a response — `POST /api/chat/stream`

Postman URL: `{{api_base_url}}/api/chat/stream`. Use the same headers and request
body as `/api/chat`; add `Accept: text/event-stream`.

The response uses `Content-Type: text/event-stream`. Each event is a `data:` line
containing one JSON object, followed by a blank line. Possible events:

```text
data: {"stage":"thinking"}

data: {"chunk":"The AP retail forecast is ..."}

data: {"chart":{"graph":"ap_price_forecast","...":"graph response fields"}}

data: {"done":{"session_id":"d56f9cde8e98496397a0aa54c32dd180","tools_used":["forecast_ap_prices"],"session_title":"AP retail forecast","warnings":[]}}

```

The `chunk` event is optional/repeated. A final successful event contains `done`.
Failures produced after the stream starts use
`data: {"stage":"error","detail":"..."}`. Authentication failures (and missing API
token configuration) are ordinary HTTP `401`/`503` responses before the stream.

**Streaming note:** the current implementation completes the assistant/tool turn
before emitting answer chunks. The chunks divide the completed answer into pieces;
they are not live tokens emitted while the model is generating.

### 3. List sessions — `GET /api/chat/sessions`

Postman URL: `{{api_base_url}}/api/chat/sessions?limit=20`

`limit` is optional (default `20`, range `1`–`100`).

```json
{
  "sessions": [
    {
      "session_id": "d56f9cde8e98496397a0aa54c32dd180",
      "title": "AP retail forecast",
      "created_at": "2026-10-31T12:00:00+00:00",
      "updated_at": "2026-10-31T12:01:00+00:00",
      "user_label": null,
      "messages_count": 2
    }
  ]
}
```

### 4. Read session history — `GET /api/chat/history`

Postman URL:
`{{api_base_url}}/api/chat/history?session_id={{chat_session_id}}&limit=100`

`session_id` is required. `limit` is optional (default `100`, range `1`–`200`).
History is returned oldest-first for the selected newest-message slice. `tools_used`
is stored/returned as a JSON-encoded string; `chart_kind` may be `null`. Full chart
payloads are not persisted.

```json
{
  "session_id": "d56f9cde8e98496397a0aa54c32dd180",
  "messages": [
    {
      "id": 1,
      "role": "user",
      "content": "What is the AP retail onion price forecast for next week?",
      "tools_used": "[]",
      "chart_kind": null,
      "created_at": "2026-10-31T12:00:00+00:00"
    },
    {
      "id": 2,
      "role": "assistant",
      "content": "The AP retail forecast is ...",
      "tools_used": "[\"forecast_ap_prices\"]",
      "chart_kind": "ap_price_forecast",
      "created_at": "2026-10-31T12:01:00+00:00"
    }
  ]
}
```

### Chatbot errors

| HTTP status | Example / meaning |
|---|---|
| `401` | Missing/invalid bearer token: `{"detail":"Invalid or missing bearer token."}` |
| `404` | History session not found: `{"detail":"Chat session not found."}` |
| `422` | Empty/overlong message or invalid query parameters. |
| `503` | Chat disabled, Groq key missing/unavailable, or API bearer token not configured. Chat-specific failures include a `detail.message` and setup hint. |
| `500` | Unexpected synchronous chat failure; check API server logs. |

The chatbot calls Groq and therefore requires a valid `GROQ_API_KEY`; its primary and
fallback models and other limits are configurable using the `CHAT_*` settings in
`.env` (see `.env.example`). No chatbot tool triggers CSV ingestion or modifies
forecast data.
