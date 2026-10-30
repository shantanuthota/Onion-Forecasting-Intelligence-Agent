"""API contract tests using a fake predictor; no model weights/network needed."""
from __future__ import annotations
import os
import sys
from pathlib import Path
from datetime import date
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from fastapi.testclient import TestClient
import history_store
import main


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 29)


class FakeChronosPipeline:
    calls = []
    def predict_df(self, df, future_df, id_column, timestamp_column, target,
                   prediction_length, quantile_levels, freq):
        self.calls.append({"columns": list(df.columns), "future_columns": list(future_df.columns),
                           "target": target, "freq": freq})
        recent = float(pd.to_numeric(df[target], errors="coerce").dropna().iloc[-1])
        rows = []
        for i, ts in enumerate(future_df[timestamp_column]):
            median = max(0.0, recent * (1 + (i + 1) * 0.0005))
            rows.append({id_column: future_df[id_column].iloc[i], timestamp_column: ts,
                         "target_name": target, "predictions": median,
                         "0.1": median * .95, "0.5": median, "0.9": median * 1.05})
        return pd.DataFrame(rows)


def client_with_fake(monkeypatch, tmp_path):
    monkeypatch.setattr(history_store, "DB_PATH", tmp_path / "test.sqlite3")
    daily_path = tmp_path / "daily_prices.csv"
    daily_rows = []
    for day_index, stamp in enumerate(("2026-10-23", "2026-10-24", "2026-10-25")):
        row = {"date": stamp}
        for state_index, state in enumerate(history_store.STATE_CODES):
            base = 2000 + state_index * 100 + day_index
            row.update({
                f"{state}_retail_rs_kg": base / 100,
                f"{state}_wholesale_rs_kg": (base - 500) / 100,
                f"{state}_mandi_modal_rs_kg": (base - 800) / 100,
                f"{state}_arrival_t_day": 10 + state_index + day_index,
            })
        daily_rows.append(row)
    pd.DataFrame(daily_rows).to_csv(daily_path, index=False)
    monthly_path = tmp_path / "monthly_ap.csv"
    pd.DataFrame([
        {"month_start": "Aug-26", "production_t_month": 52000,
         "consumption_t_month": 69000, "arrival_t_month": 4500},
        {"month_start": "Sep-26", "production_t_month": 53000,
         "consumption_t_month": 70000, "arrival_t_month": 4700},
        {"month_start": "Oct-26", "production_t_month": 54000,
         "consumption_t_month": 71000, "arrival_t_month": 4900},
    ]).to_csv(monthly_path, index=False)
    monkeypatch.setattr(history_store, "DAILY_PRICE_CSV_PATH", daily_path)
    monkeypatch.setattr(history_store, "AP_MONTHLY_CSV_PATH", monthly_path)
    monkeypatch.setattr(main, "get_pipeline", lambda: FakeChronosPipeline())
    monkeypatch.setattr(main, "date", FixedDate)
    monkeypatch.setenv("API_BEARER_TOKEN", "test-api-token")
    main._PREDICTION_CACHE.clear()
    FakeChronosPipeline.calls = []
    return TestClient(main.app)


def test_api_requires_bearer_token(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    assert client.get("/api/ingestion/status").status_code == 401
    assert client.get("/api/ingestion/status", headers={"Authorization": "Bearer wrong"}).status_code == 401

    response = client.get(
        "/api/ingestion/status",
        headers={"Authorization": "Bearer test-api-token"},
    )
    assert response.status_code == 200


def test_api_rejects_malformed_authorization_without_server_error(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)

    response = client.get(
        "/api/ingestion/status",
        headers={"Authorization": "BearerTokenWithoutSeparator"},
    )

    assert response.status_code == 401


def test_api_fails_closed_without_configured_bearer_token(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    monkeypatch.delenv("API_BEARER_TOKEN")

    response = client.get(
        "/api/ingestion/status",
        headers={"Authorization": "Bearer test-api-token"},
    )
    assert response.status_code == 503


def test_api_cors_preflight_does_not_require_bearer_token(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.options(
        "/api/ingestion/status",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_graph1_retail_uses_only_requested_context_and_converts_forecast(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/ap-retail?price_types=retail&start_date=2026-10-27&end_date=2026-11-02",
        headers={"Authorization": "Bearer test-api-token"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["forecast"]) == 5
    assert body["forecast"][0]["date"] == "2026-10-29"
    assert body["forecast"][-1]["date"] == "2026-11-02"
    assert body["unit"] == "INR/kg"
    assert body["threshold_rs_kg"] == 44.0
    assert body["price_types"] == ["retail"]
    assert body["model_history_rows"] == 3
    assert body["requested_end_date"] == "2026-11-02"
    assert body["visible_end_date"] == "2026-11-02"
    assert body["actual_end_date"] == "2026-10-29"
    assert body["forecast_end_date"] == "2026-11-02"
    # Retail target context uses only AP retail, wholesale, mandi modal and arrivals.
    retail_calls = [c for c in FakeChronosPipeline.calls if c["target"] == "AP_retail_rs_per_metric_quintal"]
    assert len(retail_calls) == 1
    assert set(retail_calls[0]["columns"]) == {
        "item_id", "timestamp", "AP_retail_rs_per_metric_quintal",
        "AP_wholesale_rs_per_metric_quintal", "AP_mandi_modal_rs_per_metric_quintal",
        "AP_arrival_metric_quintal",
    }
    # Forecast median is converted from ₹/metric quintal to ₹/kg.
    assert body["forecast"][0]["predicted_price_rs_kg"] < 100
    assert body["saved_to_sqlite"]["saved_values"] == 5
    override = client.get(
        "/api/graphs/ap-retail?price_types=retail&start_date=2026-10-27&end_date=2026-11-02&threshold_rs_kg=50",
        headers={"Authorization": "Bearer test-api-token"},
    )
    assert override.status_code == 200, override.text
    assert override.json()["threshold_rs_kg"] == 50.0
    latest = client.get(
        "/api/forecasts/latest?graph_code=graph1_ap_prices",
        headers={"Authorization": "Bearer test-api-token"},
    ).json()["forecast"]
    assert latest is not None and latest["forecast_values"][0]["unit"] == "INR/kg"


def test_new_graph_datasets_are_loaded_into_sqlite(monkeypatch, tmp_path):
    client_with_fake(monkeypatch, tmp_path)

    daily = history_store.load_daily_price_history()
    monthly = history_store.load_ap_monthly_history()

    assert len(daily) == 3
    assert daily["timestamp"].max().date().isoformat() == "2026-10-25"
    assert daily.iloc[0]["AP_retail_rs_per_metric_quintal"] == 2000
    assert daily.iloc[0]["AP_arrival_metric_quintal"] == 100
    assert len(monthly) == 3
    assert monthly.iloc[-1]["month_start"].date().isoformat() == "2026-10-01"
    assert monthly.iloc[-1]["production_t_month"] == 54000

    with history_store.connect() as conn:
        daily_count = conn.execute("SELECT COUNT(*) FROM daily_price_observations").fetchone()[0]
        monthly_count = conn.execute("SELECT COUNT(*) FROM ap_monthly_observations").fetchone()[0]
    assert daily_count == 3
    assert monthly_count == 3


def test_dash_ingestion_updates_survive_graph_dataset_reload(monkeypatch, tmp_path):
    client_with_fake(monkeypatch, tmp_path)
    history_store.load_daily_price_history()
    history_store.load_ap_monthly_history()

    result = history_store.upsert_dash_graph_observations({
        "2026-10-26": {
            history_store.PRICE_COLUMNS["AP"]["retail"]: 4100.0,
            history_store.QUANTITY_COLUMNS["AP"]["arrival"]: 130.0,
        },
    })

    daily = history_store.load_daily_price_history()
    monthly = history_store.load_ap_monthly_history()
    latest_daily = daily.loc[daily["timestamp"] == pd.Timestamp("2026-10-26")].iloc[0]
    october = monthly.loc[monthly["month_start"] == pd.Timestamp("2026-10-01")].iloc[0]
    assert result["rows_inserted"] == 1
    assert latest_daily["AP_retail_rs_per_metric_quintal"] == 4100.0
    assert latest_daily["AP_arrival_metric_quintal"] == 130.0
    assert october["production_t_month"] == 54000
    assert october["arrival_t_month"] == 46.0


def test_graph1_two_month_forecast_is_within_limit_with_current_history(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/ap-retail?price_types=wholesale&start_date=2026-10-29&end_date=2026-12-29",
        headers={"Authorization": "Bearer test-api-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["last_observation_date"] == "2026-10-25"
    assert body["horizon_days"] == 65
    assert len(body["forecast"]) == 62
    assert body["forecast"][0]["date"] == "2026-10-29"
    assert body["forecast"][-1]["date"] == "2026-12-29"
    assert not any("180-day limit" in warning for warning in body["warnings"])


def test_graph1_reported_url_dates_use_current_daily_dataset(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/ap-retail?start_date=2026-09-29&end_date=2026-11-29&price_types=retail,wholesale,mandi_modal",
        headers={"Authorization": f"Bearer {os.getenv('API_BEARER_TOKEN')}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["last_observation_date"] == "2026-10-25"
    assert body["horizon_days"] == 35
    assert body["forecast_start_date"] == "2026-10-29"
    assert body["forecast_end_date"] == "2026-11-29"
    assert body["historical"]
    assert len(body["forecast"]) == 32 * 3
    assert body["saved_to_sqlite"]["saved_values"] == 32 * 3


def test_graph1_over_limit_request_returns_maximum_forecast(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    history = main.get_data().copy()
    history["timestamp"] = pd.to_datetime(history["timestamp"]) - pd.Timedelta(days=150)
    monkeypatch.setattr(main, "get_data", lambda: history)

    response = client.get(
        "/api/graphs/ap-retail?price_types=wholesale&start_date=2026-10-29&end_date=2026-12-29",
        headers={"Authorization": "Bearer test-api-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["horizon_days"] == 180
    assert body["forecast_truncated"] is True
    assert body["forecast_limit_date"] == "2026-11-24"
    assert body["forecast_end_date"] == "2026-11-24"
    assert body["forecast"][-1]["date"] == "2026-11-24"
    assert body["requested_end_date"] == "2026-12-29"
    assert any("maximum supported date" in warning for warning in body["warnings"])


def test_graph1_all_three_price_series(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/ap-retail?price_types=retail,wholesale,mandi_modal&start_date=2026-10-27&end_date=2027-01-24",
        headers={"Authorization": "Bearer test-api-token"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["forecast"]) == 264
    assert set(body["price_types"]) == {"retail", "wholesale", "mandi_modal"}
    assert body["saved_to_sqlite"]["saved_values"] == 264
    expected_contexts = {
        "AP_wholesale_rs_per_metric_quintal": {
            "item_id", "timestamp", "AP_wholesale_rs_per_metric_quintal",
            "AP_mandi_modal_rs_per_metric_quintal", "AP_arrival_metric_quintal",
        },
        "AP_mandi_modal_rs_per_metric_quintal": {
            "item_id", "timestamp", "AP_mandi_modal_rs_per_metric_quintal",
            "AP_arrival_metric_quintal",
        },
    }
    for target, expected in expected_contexts.items():
        target_call = next(
            call for call in FakeChronosPipeline.calls
            if call["target"] == target and "AP_arrival_metric_quintal" in call["columns"]
        )
        assert set(target_call["columns"]) == expected


def test_graph2_only_selected_states_and_series(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/state-comparison?states=KA,MP&price_types=retail,wholesale,mandi_modal&start_date=2026-10-27&end_date=2026-11-09",
        headers={"Authorization": "Bearer test-api-token"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selected_states"] == ["KA", "MP"]
    assert body["threshold_rs_kg"] == 44.0
    assert {r["state"] for r in body["forecast"]} == {"KA", "MP"}
    assert {r["price_type"] for r in body["forecast"]} == {"retail", "wholesale", "mandi_modal"}
    assert len(body["forecast"]) == 12 * 2 * 3
    assert body["saved_to_sqlite"]["saved_values"] == 72
    assert body["requested_end_date"] == "2026-11-09"
    assert body["visible_end_date"] == "2026-11-09"
    assert body["actual_end_date"] == "2026-10-29"
    assert body["forecast_end_date"] == "2026-11-09"
    ka_retail = next(
        call for call in FakeChronosPipeline.calls
        if call["target"] == "KA_retail_rs_per_metric_quintal"
    )
    assert set(ka_retail["columns"]) == {
        "item_id", "timestamp", "KA_retail_rs_per_metric_quintal",
        "KA_wholesale_rs_per_metric_quintal", "KA_mandi_modal_rs_per_metric_quintal",
        "KA_arrival_metric_quintal",
    }
    assert not any(column.startswith(("AP_", "MP_", "MH_")) for column in ka_retail["columns"])
    override = client.get(
        "/api/graphs/state-comparison?states=KA&price_types=retail&start_date=2026-10-27&end_date=2026-11-09&threshold_rs_kg=50",
        headers={"Authorization": "Bearer test-api-token"},
    )
    assert override.status_code == 200, override.text
    assert override.json()["threshold_rs_kg"] == 50.0


def test_graph2_filters_forecast_to_requested_start_date(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/state-comparison?states=KA&price_types=wholesale&start_date=2026-10-29&end_date=2026-11-09",
        headers={"Authorization": "Bearer test-api-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["forecast"][0]["date"] == "2026-10-29"
    assert body["forecast"][-1]["date"] == "2026-11-09"
    assert len(body["forecast"]) == 12
    assert body["visible_start_date"] == "2026-10-29"
    assert body["visible_end_date"] == "2026-11-09"
    assert body["actual_end_date"] == "2026-10-29"
    assert body["saved_to_sqlite"]["saved_values"] == 12


def test_graph3_supply_consumption_and_shortfall(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/supply-balance?states=AP&start_date=2026-10-27&end_date=2026-11-02",
        headers={"Authorization": "Bearer test-api-token"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["quantity_forecast"]) == 3
    assert {r["metric"] for r in body["quantity_forecast"]} == {
        "consumption", "arrival", "production"
    }
    assert len(body["balance_forecast"]) == 1
    assert body["balance_formula"] == "consumption - (arrival + production)"
    assert body["quantity_unit"] == "tonnes/month"
    assert body["saved_to_sqlite"]["saved_values"] == 4
    assert {call["target"] for call in FakeChronosPipeline.calls} == {
        "production_t_month", "consumption_t_month", "arrival_t_month"
    }
    assert all(call["freq"] == "MS" for call in FakeChronosPipeline.calls)
    assert all(
        set(call["columns"]) == {"item_id", "timestamp", call["target"]}
        for call in FakeChronosPipeline.calls
    )
    assert body["balance_forecast"][0]["shortfall_p50_tonnes_month"] == round(
        next(row["predicted_quantity_tonnes_month"] for row in body["quantity_forecast"] if row["metric"] == "consumption")
        - next(row["predicted_quantity_tonnes_month"] for row in body["quantity_forecast"] if row["metric"] == "arrival")
        - next(row["predicted_quantity_tonnes_month"] for row in body["quantity_forecast"] if row["metric"] == "production"),
        4,
    )


def test_graph3_is_ap_only_and_returns_maximum_monthly_forecast(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    headers = {"Authorization": "Bearer test-api-token"}
    unsupported = client.get(
        "/api/graphs/supply-balance?states=KA&start_date=2026-10-01&end_date=2026-11-02",
        headers=headers,
    )
    assert unsupported.status_code == 422

    too_far = client.get(
        "/api/graphs/supply-balance?states=AP&start_date=2026-10-01&end_date=2027-04-15",
        headers=headers,
    )
    assert too_far.status_code == 200, too_far.text
    body = too_far.json()
    assert body["horizon_days"] == 180
    assert body["forecast_truncated"] is True
    assert body["requested_end_date"] == "2027-04-15"
    assert body["forecast_limit_date"] == "2027-03-30"
    assert body["forecast_end_date"] == "2027-03-01"
    assert len(body["balance_forecast"]) == 5
    assert any("maximum supported date" in warning for warning in body["warnings"])


def test_graph2_year_request_returns_maximum_forecast_for_each_state(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/state-comparison?states=AP,KA,MH,MP&price_types=retail&start_date=2026-04-30&end_date=2027-04-30",
        headers={"Authorization": f"Bearer {os.getenv('API_BEARER_TOKEN')}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["last_observation_date"] == "2026-10-25"
    assert body["horizon_days"] == 180
    assert body["forecast_truncated"] is True
    assert body["forecast_limit_date"] == "2027-04-23"
    assert body["forecast_end_date"] == "2027-04-23"
    assert len(body["forecast"]) == 177 * 4
    assert {row["state"] for row in body["forecast"]} == {"AP", "KA", "MH", "MP"}
    assert body["requested_end_date"] == "2027-04-30"
    assert any("maximum supported date" in warning for warning in body["warnings"])


def test_graph1_and_graph2_split_actuals_and_forecasts_at_today(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    headers = {"Authorization": "Bearer test-api-token"}
    requests = [
        (
            "/api/graphs/ap-retail?price_types=retail&start_date=2026-10-01&end_date=2026-11-02",
            "forecast",
        ),
        (
            "/api/graphs/state-comparison?states=AP&price_types=retail&start_date=2026-10-01&end_date=2026-11-09",
            "forecast",
        ),
    ]

    for path, forecast_field in requests[:2]:
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        expected_end_date = "2026-11-02" if "ap-retail" in path else "2026-11-09"
        assert body["requested_end_date"] == expected_end_date
        assert body["visible_end_date"] == expected_end_date
        assert body["actual_end_date"] == "2026-10-29"
        assert min(row["date"] for row in body[forecast_field]) == "2026-10-29"
        assert max(row["date"] for row in body[forecast_field]) == expected_end_date

def test_graph_returns_historical_only_when_no_future_days_remain(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/ap-retail?price_types=wholesale&start_date=2026-10-01&end_date=2026-10-25",
        headers={"Authorization": "Bearer test-api-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["horizon_days"] == 0
    assert body["forecast"] == []
    assert body["historical"]
    assert body["saved_to_sqlite"] is None
    assert FakeChronosPipeline.calls == []


def test_forecast_saved_runs_endpoint(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/ap-retail?price_types=wholesale&start_date=2026-10-27&end_date=2026-11-02",
        headers={"Authorization": "Bearer test-api-token"},
    )
    assert response.status_code == 200
    runs = client.get(
        "/api/forecasts/runs",
        headers={"Authorization": "Bearer test-api-token"},
    ).json()["runs"]
    assert len(runs) >= 1
    assert runs[0]["graph_code"] == "graph1_ap_prices"


def test_invalid_horizon_rejected(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/ap-retail?horizon_days=10",
        headers={"Authorization": "Bearer test-api-token"},
    )
    assert response.status_code == 422
