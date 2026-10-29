# Frontend integration contract — v4

Base URL locally: `http://127.0.0.1:8000`

All `/api/*` routes except CSV import require `Authorization: Bearer <API_BEARER_TOKEN>`.
Keep the shared token in a trusted frontend backend/server secret store; do not embed it
in browser code. For browser-originated API calls, have the frontend backend proxy the
request with the bearer header. Configure `CORS_ORIGINS` with the exact frontend origin
only when the browser must call this API directly; CORS does not protect the token.
For remote access, use HTTPS and a firewall/reverse proxy. See [API.md](API.md) for
token generation and server setup.

All graph endpoints require inclusive `start_date` and `end_date` values in `YYYY-MM-DD` format. The model forecasts from the day after the latest stored observation through `end_date`; `end_date` must be no more than 180 days after the latest observation. The response includes calculated `horizon_days`; it is not a request parameter. For Graph 1 and Graph 2, actual rows are returned through `actual_end_date` (the earlier of `end_date` and today), and forecast rows are returned from today through `end_date`. If today's actual observation exists, forecasts start the next day to avoid overlapping actual and predicted values. If `end_date` is before today, these graphs return actual rows only. Graph 3 retains the full requested-range behavior. Forecast price outputs are in INR/kg. The SQLite observations table retains historical prices in INR/metric quintal; actual-history values in the graph API are converted at response time to INR/kg so chart lines are comparable. Forecast price quantiles are converted to INR/kg and saved to SQLite.

The 180-day limit is based on the remote API database's `last_observation_date`, not
the selected display-range length. If the warning says the forecast horizon exceeds
the cap for a short selection, check `GET /api/ingestion/status` on the remote API;
its history may be stale compared with the local API.

## Graph 1 — AP prices

`GET /api/graphs/ap-retail?start_date=2026-10-01&end_date=2026-11-30&price_types=retail,wholesale,mandi_modal&threshold_rs_kg=44`

- `price_types`: comma-separated `retail`, `wholesale`, `mandi_modal`.
- `threshold_rs_kg`: optional horizontal chart threshold in INR/kg; defaults to `44`.
- Response field `threshold_rs_kg`: the effective threshold value used for the chart.
- `historical`: `date`, `state`, `price_type`, `actual_price_rs_kg`.
- `forecast`: `date`, `state`, `price_type`, `latest_actual_date`, `latest_actual_price_rs_kg`, `predicted_price_rs_kg`, `change_rs_kg`, `change_percent`, `change_direction`, `p10_rs_kg`, `p50_rs_kg`, `p90_rs_kg`, `prediction_interval_width_rs_kg`, `interval_width_pct_of_median`.
- Top-level date/status fields include `visible_start_date`, `visible_end_date`, `actual_end_date`, `requested_end_date`, `last_observation_date`, `data_staleness_days`, `forecast_start_date`, `forecast_end_date`, and `warnings`.
- `saved_to_sqlite`: run ID and number of saved quantile rows.

Target logic:
- retail uses past AP retail, wholesale and mandi modal; future wholesale and mandi modal values are first forecast as covariates.
- wholesale uses only its own past series.
- mandi modal uses only its own past series.

## Graph 2 — state comparison

`GET /api/graphs/state-comparison?states=AP,MH&price_types=retail,wholesale,mandi_modal&start_date=2026-10-01&end_date=2026-11-30&threshold_rs_kg=44`

- `states`: any comma-separated subset of `AP`, `KA`, `MH`, `MP`.
- `price_types`: any subset of `retail`, `wholesale`, `mandi_modal`.
- `threshold_rs_kg`: optional horizontal chart threshold in INR/kg; defaults to `44` and is returned in the response.
- `visible_start_date`, `visible_end_date`, `actual_end_date`, and `requested_end_date` describe the selected date range and actual-data cutoff.
- Actual rows stop at today; forecast rows begin today or the next day if today's actual exists, and continue through `end_date`.
- Price `forecast` rows include the latest observed price/date, median prediction, absolute/percentage change and direction, P10/P50/P90, and prediction-interval width fields (same shape as Graph 1).
- Only selected states and series are returned and saved.
- Retail uses each state's own retail/wholesale/mandi history; wholesale and mandi modal each use only their own history.

## Graph 3 — supply / consumption / shortfall

`GET /api/graphs/supply-balance?states=AP&start_date=2026-10-01&end_date=2026-11-30`

- `quantity_forecast`: `metric` is `supply` or `consumption`, with P10/P50/P90 in `metric_quintal/day`.
- `balance_forecast`: `shortfall_p50_metric_quintal_day` and bounds; formula is `supply - consumption`. Negative = potential shortfall; positive = potential surplus.
- Supply is created from historical arrival + production and forecast as a univariate target. Consumption is forecast as a separate univariate target. Shortfall is calculated, not independently forecast.
- Top-level response includes `historical`, `balance_formula`, `warnings`, `metadata`, `saved_to_sqlite`, date/staleness fields, and calculated `horizon_days`.

## Forecast persistence endpoints

- `GET /api/forecasts/latest` — latest saved forecast run.
- `GET /api/forecasts/latest?graph_code=graph1_ap_prices` — latest Graph 1 run.
- `GET /api/forecasts/runs?limit=20` — recent saved runs.

## Data ingestion

- `GET /api/ingestion/status` — SQLite history and last ingestion status.
- `POST /api/ingestion/sync-csv?filename=Onion_Model_Ready_Dataset_Chronos2_2026-10-26_Interpolated_Metric_Quintal.csv` with `X-Ingestion-Key` header — imports a combined CSV located inside `data/`. Set `INGESTION_API_KEY` in `.env` first.

The CSV endpoint requires a configured unique `INGESTION_API_KEY`; it does not use the
API bearer token. It only accepts a `.csv` filename located directly in the project's
`data/` directory. Successful imports return the upsert counts and refreshed history
status.
