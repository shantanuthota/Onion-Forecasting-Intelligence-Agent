# Graph-specific forecasting and SQLite persistence

## Graph observation datasets

Graphs 1 and 2 use the configured daily price CSV (1,759 rows, 2022-01-01 through 2026-10-25). Its prices are normalized to INR per metric quintal and arrivals to metric quintals/day before being synchronized into `daily_price_observations`. Graph 3 uses the configured AP monthly CSV (58 rows, January 2022 through October 2026), synchronized into `ap_monthly_observations` in tonnes/month. Both sources refresh their SQLite table when the configured CSV changes.

Forecast price values are persisted in INR/kg. Historical prices are converted in the response layer so chart lines remain comparable with forecasts.

## Explicit target/context rules

### Graph 1 — Andhra Pradesh

- AP retail target: `AP_retail_rs_per_metric_quintal`; context is timestamp + AP retail + AP wholesale + AP mandi modal + AP arrivals. Unknown future covariates are independently forecast first.
- AP wholesale target: timestamp + AP wholesale + AP mandi modal + AP arrivals. Unknown future covariates are independently forecast first.
- AP mandi modal target: timestamp + AP mandi modal + AP arrivals. Unknown future arrivals are independently forecast first.
- The UI/API can request any subset using `price_types=retail,wholesale,mandi_modal`.

### Graph 2 — selected states

The same input hierarchy is applied independently for each selected state: retail uses same-state wholesale, mandi modal, and arrivals; wholesale uses same-state mandi modal and arrivals; mandi modal uses same-state arrivals. Each target also uses its own historical target series. Only selected states/price types are returned and persisted for that graph run.

### Graph 3 — AP monthly production / consumption / arrivals / shortfall

- Production, consumption, and arrivals are loaded from the AP monthly CSV in tonnes/month.
- Each component is forecast as a separate monthly target, with only its own historical series and month timestamps as context.
- `shortfall = consumption - (arrival + production)`; positive values mean potential shortfall, negative values mean potential surplus.
- Shortfall is derived from the three component forecasts and stored alongside them. It is not a separate Chronos target.

## Forecast persistence

Every successful graph endpoint call creates a row in `forecast_runs` and saves its forecast quantiles in `forecast_values`. Each saved value has graph run, state, target, forecast date, P10/P50/P90 and unit. Price forecast values are stored in INR/kg; Graph 3 quantities and shortfall are stored in tonnes/month. Inspect them with:

- `GET /api/forecasts/latest?graph_code=graph1_ap_prices`
- `GET /api/forecasts/runs`

## Important modelling caveats

- The Chronos-2 inference path is implemented using the selected context columns. Real Chronos-2 inference still needs to be run in the user's local environment with the model dependencies and weights installed.
- Price models need future covariate values; this implementation forecasts the same-state inputs first instead of leaking unknown actual future values.
- Graph 3 uses monthly source data and the requested shortfall formula. Validate the definitions of the monthly measures before operational use.
- Requests beyond 180 days return the maximum supported forecast range, with `forecast_truncated`, `forecast_limit_date`, and a warning describing the cap.
- P10/P50/P90 are quantiles, not a scalar confidence score.

## Automated UPAg Dash ingestion (scheduled)

`pull_upag_dash.py` (Task Scheduler task `OnionUPAgDash6h`, every 6 hours) pulls the previous day (Asia/Kolkata) for AP/MH/KA/MP from the public UPAg Dash MIP by-crop callback and merges into `observations` via `upsert_dash_observations()`:

- Sourced series: retail, wholesale, mandi modal prices (Rs/Qtl, stored as-is) and mandi arrivals (Tonnes ×10 → metric quintals). Series are selected by `(MetricMainCategoryName, MetricSubCategoryName)` + asserted UOM; the free-text `Metric` label is never used for mapping.
- Window: `from = to = yesterday`. If the DB ends before the window, the request start auto-extends back to `DB_last + 1`; dates the source does not provide become full forward-filled rows (`source='upag_dash_6h_gapfill'`), keeping the daily index continuous.
- Presence semantics per `(date, column)`: absent → insert; equal within 1e-6 → unchanged; differing → correction of that single column (`source='upag_dash_6h_correction'`) plus a row in `data/corrections_log.csv`. `UPAG_DASH_UPDATE_EXISTING=0` logs corrections without applying them.
- Only **production and consumption are not sourced** from this endpoint; on new dates they carry forward from the previous row (same convention as the supplied "Interpolated" dataset). Wholesale, retail, mandi modal and arrivals are all sourced values.
- Mandi modal/arrivals report non-daily and their coverage varies by state (e.g. AP mandi rows ≈1,270 vs MP ≈1,752 over the full range); retail and wholesale (DOCA) are daily. Absent series on a date are expected, never an error.
- Verification anchors (`dash_extract.ANCHORS`, all matched on 2026-10-28): AP/KA/MH/MP 2022-01-01 and MP 2023-06-18 — four series each, arrivals post-×10. Drift raises and blocks that state's ingestion.
- Audit: one `ingestion_log` row per run (success with counts, or per-state error rows); optional raw snapshots in `data/snapshots/` (newest 30 kept); `data/pull_upag_dash.lock` (PID + timestamp, stale after `UPAG_DASH_LOCK_MAX_AGE_SEC`) prevents overlapping runs.
- Tests: `tests/test_dash_ingestion.py` (25 offline tests with recorded state fixtures + stubbed network).
