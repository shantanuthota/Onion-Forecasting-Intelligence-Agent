"""API contract tests using a fake predictor; no model weights/network needed."""
from __future__ import annotations
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
        self.calls.append({"columns": list(df.columns), "future_columns": list(future_df.columns), "target": target})
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
    assert body["model_history_rows"] == 1760
    assert body["requested_end_date"] == "2026-11-02"
    assert body["visible_end_date"] == "2026-11-02"
    assert body["actual_end_date"] == "2026-10-29"
    assert body["forecast_end_date"] == "2026-11-02"
    # Retail target context contains only date/id, AP retail, AP wholesale and AP mandi modal.
    retail_calls = [c for c in FakeChronosPipeline.calls if c["target"] == "AP_retail_rs_per_metric_quintal"]
    assert len(retail_calls) == 1
    assert set(retail_calls[0]["columns"]) == {"item_id", "timestamp", "AP_retail_rs_per_metric_quintal", "AP_wholesale_rs_per_metric_quintal", "AP_mandi_modal_rs_per_metric_quintal"}
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


def test_graph1_two_month_forecast_is_within_limit_with_current_history(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    response = client.get(
        "/api/graphs/ap-retail?price_types=wholesale&start_date=2026-10-29&end_date=2026-12-29",
        headers={"Authorization": "Bearer test-api-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["last_observation_date"] == "2026-10-26"
    assert body["horizon_days"] == 64
    assert len(body["forecast"]) == 62
    assert body["forecast"][0]["date"] == "2026-10-29"
    assert body["forecast"][-1]["date"] == "2026-12-29"
    assert not any("180-day limit" in warning for warning in body["warnings"])


def test_graph1_over_limit_warning_explains_stale_history(monkeypatch, tmp_path):
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
    assert body["horizon_days"] == 214
    assert body["forecast"] == []
    range_warnings = [
        warning for warning in body["warnings"]
        if warning.startswith("Forecast requires")
    ]
    assert range_warnings == [
        "Forecast requires 214 days from the latest observation (2026-05-29) "
        "through requested end_date 2026-12-29, exceeding the 180-day limit. "
        "no graph data was returned and no forecast was saved."
    ]


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
    assert len(body["quantity_forecast"]) == 14
    assert {r["metric"] for r in body["quantity_forecast"]} == {
        "supply", "consumption", "arrival", "production"
    }
    assert len(body["balance_forecast"]) == 7
    assert body["balance_formula"] == "supply - consumption"
    assert body["saved_to_sqlite"]["saved_values"] == 21
    supply_calls = [c for c in FakeChronosPipeline.calls if c["target"] == "AP_supply_metric_quintal_per_day"]
    assert len(supply_calls) == 1
    assert set(supply_calls[0]["columns"]) == {"item_id", "timestamp", "AP_supply_metric_quintal_per_day"}


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
        (
            "/api/graphs/supply-balance?states=AP&start_date=2026-10-01&end_date=2027-05-01",
            ("historical", "quantity_forecast", "balance_forecast"),
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

    path, empty_fields = requests[2]
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert all(body[field] == [] for field in empty_fields)
    assert body["saved_to_sqlite"] is None
    assert any("no graph data was returned" in warning for warning in body["warnings"])


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
