"""Offline tests for the UPAg Dash ingestion pipeline.

Covers dash_client payload building, dash_extract parsing/mapping/units/anchors,
history_store.upsert_dash_observations append/correct/gapfill semantics, the
pull_upag_dash window planning + lock, and full --dry-run / --once runs with a
stubbed network layer. No requests leave this machine.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dash_client  # noqa: E402
import dash_extract  # noqa: E402
import history_store  # noqa: E402
import pull_upag_dash  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

AP_RETAIL = history_store.PRICE_COLUMNS["AP"]["retail"]
AP_WHOLESALE = history_store.PRICE_COLUMNS["AP"]["wholesale"]
MP_RETAIL = history_store.PRICE_COLUMNS["MP"]["retail"]


def load_fixture(state: str) -> list[dict]:
    body = json.loads((FIXTURES / f"mip_response_{state}.json").read_text(encoding="utf-8-sig"))
    return body["response"]["mip-bycrop-store"]["data"]


def make_row(state_code: str, day: str, sub_category: str, value: float,
             main_category: str = "Prices", uom: str = "Rs/Qtl") -> dict:
    state_name, state_id = dash_client.STATE_CONFIG[state_code]
    return {
        "stateid": str(state_id), "State": state_name, "CalendarDay": day, "Crop": "Onion",
        "sourcename": "DOCA", "MetricMainCategoryName": main_category,
        "MetricSubCategoryName": sub_category, "Metric": f"{sub_category} (stub)", "Value": value,
        "UOMName": uom, "subsidary_crop": None,
    }


def seed_history(tmp_path: Path, monkeypatch, days: int = 3, end: str = "2026-10-26") -> None:
    """Small complete observation history inside a temp SQLite DB."""
    monkeypatch.setattr(history_store, "DB_PATH", tmp_path / "test.sqlite3")
    # The tests build their own small history; never auto-import the project CSV here.
    monkeypatch.setattr(history_store, "ensure_store_ready", lambda: None)
    timestamps = pd.date_range(end=pd.Timestamp(end), periods=days, freq="D")
    frame = pd.DataFrame({"item_id": "AP_onion", "timestamp": timestamps})
    for state in history_store.STATE_CODES:
        for measure in ("retail", "wholesale", "mandi_modal"):
            base = {"retail": 4000.0, "wholesale": 3100.0, "mandi_modal": 2500.0}[measure]
            frame[history_store.PRICE_COLUMNS[state][measure]] = base
        for measure, base in (("arrival", 10000.0), ("production", 20000.0), ("consumption", 22000.0)):
            frame[history_store.QUANTITY_COLUMNS[state][measure]] = base
    history_store.upsert_frame(frame)


def observation_row(date_iso: str) -> dict:
    with history_store.connect() as conn:
        row = conn.execute('SELECT * FROM "observations" WHERE "timestamp"=?', (date_iso,)).fetchone()
    return dict(row) if row is not None else {}


# ---------------------------------------------------------------------------
# dash_client
# ---------------------------------------------------------------------------

def test_parse_calendar_day_cases():
    assert dash_extract.parse_calendar_day("18 Jun 2023") == dt.date(2023, 6, 18)
    assert dash_extract.parse_calendar_day("27 Oct 2026") == dt.date(2026, 10, 27)
    assert dash_extract.parse_calendar_day("01 Jan 2022") == dt.date(2022, 1, 1)
    with pytest.raises(ValueError):
        dash_extract.parse_calendar_day("32 Jun 2023")
    with pytest.raises(ValueError):
        dash_extract.parse_calendar_day("bad data")


def test_build_payload_derives_state_and_dates():
    payload = dash_client.build_payload("AP", "2026-10-27", "2026-10-27")
    selected = [i for i in payload["inputs"] if i["id"] == "mip-bycrop-location-filter-aggrid"][0]["value"]
    assert selected == [{"State": "Andhra Pradesh", "StateCode": 28}]
    prices = [i for i in payload["inputs"] if i["id"] == "mip-filters-store"][0]["value"]["prices"]
    assert prices["from"] == "2026-10-27" and prices["to"] == "2026-10-27"
    assert (prices["monthfrom"], prices["yearfrom"], prices["monthto"], prices["yearto"]) == (10, 2026, 10, 2026)
    assert prices["metric"] == [10, 13, 9, 8]
    assert payload["changedPropIds"] == ["mip-filters-store.data"]
    with pytest.raises(ValueError):
        dash_client.build_payload("AP", "2026-10-28", "2026-10-27")
    with pytest.raises(ValueError):
        dash_client.build_payload("XX", "2026-10-27", "2026-10-27")


# ---------------------------------------------------------------------------
# dash_extract
# ---------------------------------------------------------------------------

def test_extract_series_maps_all_series_and_units():
    frame, meta = dash_extract.extract_series(load_fixture("MP"), "MP")
    assert set(frame.columns) == {
        "MP_retail_rs_per_metric_quintal", "MP_wholesale_rs_per_metric_quintal",
        "MP_mandi_modal_rs_per_metric_quintal", "MP_arrival_metric_quintal",
    }
    row = frame.loc["2023-06-18"]
    assert row["MP_wholesale_rs_per_metric_quintal"] == pytest.approx(1205.45)
    assert row["MP_retail_rs_per_metric_quintal"] == pytest.approx(1611.76)
    assert row["MP_mandi_modal_rs_per_metric_quintal"] == pytest.approx(561.4)
    assert row["MP_arrival_metric_quintal"] == pytest.approx(1219.35 * 10)
    assert meta["series"]["arrival"]["rows"] == 1752
    assert meta["series"]["retail"]["rows"] == 1761
    assert list(frame.index) == sorted(frame.index)


def test_extract_series_raises_on_unknown_selector():
    rows = load_fixture("MP")[:5]
    rows.append(make_row("MP", "27 Oct 2026", "Mystery Prices", 1.0, main_category="Foo"))
    with pytest.raises(RuntimeError) as excinfo:
        dash_extract.extract_series(rows, "MP")
    assert "Foo" in str(excinfo.value)


def test_extract_series_raises_on_uom_mismatch():
    rows = load_fixture("MP")[:5]
    rows.append(make_row("MP", "27 Oct 2026", "Retail Prices", 100.0, uom="USD/kg"))
    with pytest.raises(RuntimeError) as excinfo:
        dash_extract.extract_series(rows, "MP")
    assert "USD/kg" in str(excinfo.value)


def test_assert_anchors_pass_and_fail():
    frame, _ = dash_extract.extract_series(load_fixture("AP"), "AP")
    dash_extract.assert_anchors(frame, "AP")  # must not raise
    assert_2022 = frame.loc["2022-01-01"]
    assert assert_2022["AP_retail_rs_per_metric_quintal"] == pytest.approx(4000.0)
    assert assert_2022["AP_arrival_metric_quintal"] == pytest.approx(10.0)
    corrupted = frame.copy()
    corrupted.loc["2022-01-01", "AP_retail_rs_per_metric_quintal"] = 9999.0
    with pytest.raises(RuntimeError) as excinfo:
        dash_extract.assert_anchors(corrupted, "AP")
    assert "anchor mismatch" in str(excinfo.value)
    # Window without anchor dates -> silently skipped.
    dash_extract.assert_anchors(frame[frame.index == "2026-10-27"], "AP")


# ---------------------------------------------------------------------------
# history_store.upsert_dash_observations
# ---------------------------------------------------------------------------

def test_upsert_appends_missing_date_with_forward_fill(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    updates = {"2026-10-27": {AP_RETAIL: 4100.0, MP_RETAIL: 2600.0}}
    result = history_store.upsert_dash_observations(updates)
    assert (result.inserted, result.updated, result.gap_filled) == (2, 0, 0)
    row = observation_row("2026-10-27")
    assert row[AP_RETAIL] == pytest.approx(4100.0)
    assert row[MP_RETAIL] == pytest.approx(2600.0)
    assert row[AP_WHOLESALE] == pytest.approx(3100.0)  # carried forward
    assert row["AP_production_metric_quintal_per_day"] == pytest.approx(20000.0)
    assert row["AP_consumption_metric_quintal_per_day"] == pytest.approx(22000.0)
    assert row["source"] == "upag_dash_6h"
    assert row["item_id"] == "AP_onion"
    assert result.latest_observation_date == "2026-10-27"


def test_upsert_is_idempotent(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    updates = {"2026-10-27": {AP_RETAIL: 4100.0}}
    first = history_store.upsert_dash_observations(updates)
    second = history_store.upsert_dash_observations(updates)
    assert first.inserted == 1
    assert second.inserted == 0
    assert second.unchanged == 1
    assert observation_row("2026-10-27")["source"] == "upag_dash_6h"


def test_upsert_correction_applies_single_column(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    history_store.upsert_dash_observations({"2026-10-27": {AP_RETAIL: 4100.0}})
    result = history_store.upsert_dash_observations({"2026-10-27": {AP_RETAIL: 4123.45}})
    assert result.updated == 1
    assert result.correction_count == 1
    row = observation_row("2026-10-27")
    assert row[AP_RETAIL] == pytest.approx(4123.45)
    assert row[AP_WHOLESALE] == pytest.approx(3100.0)  # other columns untouched
    assert row["source"] == "upag_dash_6h_correction"
    correction = result.corrections[0]
    assert correction["old_value"] == pytest.approx(4100.0)
    assert correction["new_value"] == pytest.approx(4123.45)
    assert correction["applied"] is True


def test_upsert_correction_disabled_logs_only(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    history_store.upsert_dash_observations({"2026-10-27": {AP_RETAIL: 4100.0}})
    result = history_store.upsert_dash_observations({"2026-10-27": {AP_RETAIL: 4123.45}},
                                                    update_existing=False)
    assert result.updated == 0
    assert result.correction_count == 1
    assert result.corrections[0]["applied"] is False
    row = observation_row("2026-10-27")
    assert row[AP_RETAIL] == pytest.approx(4100.0)
    assert row["source"] == "upag_dash_6h"


def test_gapfill_inserts_full_forward_filled_rows(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    result = history_store.upsert_dash_observations({}, gap_dates=["2026-10-27", "2026-10-28"])
    assert result.gap_filled == 2
    for stamp in ("2026-10-27", "2026-10-28"):
        row = observation_row(stamp)
        assert row[AP_RETAIL] == pytest.approx(4000.0)
        assert row["AP_production_metric_quintal_per_day"] == pytest.approx(20000.0)
        assert row["source"] == "upag_dash_6h_gapfill"
    frame = history_store.load_history()
    assert not frame.isna().any().any()
    assert len(frame) == 5


def test_gapfill_skips_existing_dates(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    result = history_store.upsert_dash_observations({}, gap_dates=["2026-10-25", "2026-10-30"])
    assert result.gap_filled == 1
    assert observation_row("2026-10-25")["source"] != "upag_dash_6h_gapfill"


def test_classify_dash_updates_classification(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    updates = {
        "2026-10-25": {AP_RETAIL: 4000.0},              # existing, identical
        "2026-10-26": {AP_RETAIL: 4321.0},              # existing, differs
        "2026-10-27": {AP_RETAIL: 4100.0, MP_RETAIL: 2600.0},  # missing date
    }
    classification = history_store.classify_dash_updates(updates)
    assert classification["missing_dates"] == ["2026-10-27"]
    assert classification["unchanged"] == 1
    assert len(classification["corrections"]) == 1
    assert classification["corrections"][0]["date"] == "2026-10-26"


def test_latest_observation_date_and_existing_dates_between(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    assert history_store.latest_observation_date() == "2026-10-26"
    assert history_store.existing_dates_between("2026-10-24", "2026-12-31") == {
        "2026-10-24", "2026-10-25", "2026-10-26"}


def test_upsert_rejects_bad_input(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        history_store.upsert_dash_observations({"2026/10/27": {AP_RETAIL: 1.0}})
    with pytest.raises(ValueError):
        history_store.upsert_dash_observations({"2026-10-27": {"not_a_column": 1.0}})
    with pytest.raises(ValueError):
        history_store.upsert_dash_observations({"2026-10-27": {AP_RETAIL: float("nan")}})


# ---------------------------------------------------------------------------
# pull_upag_dash planning + lock
# ---------------------------------------------------------------------------

def test_plan_window_rules():
    today = dt.date(2026, 10, 28)
    assert pull_upag_dash.plan_window(today, 1, None, None, False) == ("2026-10-27", "2026-10-27")
    assert pull_upag_dash.plan_window(today, 3, None, None, False) == ("2026-10-25", "2026-10-27")
    assert pull_upag_dash.plan_window(today, 1, None, None, True) == ("2022-01-01", "2026-10-27")
    assert pull_upag_dash.plan_window(today, 1, "2026-10-20", "2026-10-22", False) == ("2026-10-20", "2026-10-22")
    with pytest.raises(ValueError):
        pull_upag_dash.plan_window(today, 1, "2026-10-20", None, False)
    with pytest.raises(ValueError):
        pull_upag_dash.plan_window(today, 1, "2026-10-22", "2026-10-20", False)


def test_extend_window_for_gap():
    from_iso, bridged = pull_upag_dash.extend_window_for_gap("2026-10-27", "2026-10-20")
    assert from_iso == "2026-10-21"
    assert bridged == ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26"]
    from_iso, bridged = pull_upag_dash.extend_window_for_gap("2026-10-21", "2026-10-20")
    assert (from_iso, bridged) == ("2026-10-21", [])
    from_iso, bridged = pull_upag_dash.extend_window_for_gap("2026-10-20", None)
    assert (from_iso, bridged) == ("2026-10-20", [])


def test_detect_gap_dates():
    gaps = pull_upag_dash.detect_gap_dates(
        "2026-10-21", "2026-10-27",
        provided_dates={"2026-10-27"},
        existing_dates={"2026-10-20"},
    )
    assert gaps == ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26"]


def test_lock_acquire_release_and_stale_takeover(tmp_path, monkeypatch):
    monkeypatch.setattr(pull_upag_dash, "LOCK_PATH", tmp_path / "pull.lock")
    assert pull_upag_dash.acquire_lock() is True
    assert pull_upag_dash.acquire_lock() is False
    pull_upag_dash.release_lock()
    assert not (tmp_path / "pull.lock").exists()
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=90000)
    (tmp_path / "pull.lock").write_text(json.dumps({"pid": 1, "created_utc": stale.isoformat()}), encoding="utf-8")
    assert pull_upag_dash.acquire_lock() is True
    pull_upag_dash.release_lock()


# ---------------------------------------------------------------------------
# Full runs with a stubbed network layer
# ---------------------------------------------------------------------------

def _stub_fetch(monkeypatch, row_overrides: dict[str, list[dict]] | None = None,
                fail_states: set[str] | None = None) -> None:
    fail_states = fail_states or set()
    row_overrides = row_overrides or {}

    def fake_fetch(state, from_iso, to_iso, session=None, timeout=60, retry_delays=(0.0, 0.0, 0.0)):
        if state in fail_states:
            raise RuntimeError(f"stub failure for {state}")
        return row_overrides.get(state, [])

    monkeypatch.setattr(dash_client, "fetch_state", fake_fetch)


def _isolate_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pull_upag_dash, "LOCK_PATH", tmp_path / "pull.lock")
    monkeypatch.setattr(pull_upag_dash, "CORRECTIONS_CSV", tmp_path / "corrections_log.csv")
    monkeypatch.setattr(pull_upag_dash, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(history_store, "ensure_store_ready", lambda: None)
    monkeypatch.setenv("UPAG_DASH_SNAPSHOTS", "0")
    monkeypatch.setenv("UPAG_DASH_ENABLED", "1")
    with history_store.connect() as conn:
        history_store._create_schema(conn)
        daily_columns = history_store.DAILY_GRAPH_COLUMNS
        quoted_columns = ",".join(f'"{column}"' for column in daily_columns)
        source_rows = conn.execute(
            f'SELECT {quoted_columns} FROM observations ORDER BY timestamp').fetchall()
        placeholders = ",".join("?" for _ in daily_columns)
        conn.executemany(
            f'INSERT OR REPLACE INTO daily_price_observations ({quoted_columns}) '
            f'VALUES ({placeholders})',
            [tuple(row) for row in source_rows],
        )
        conn.execute(
            """INSERT OR REPLACE INTO ap_monthly_observations
            (month_start, production_t_month, consumption_t_month, arrival_t_month)
            VALUES('2026-10-01', 52000, 69000, 3000)"""
        )
    daily_source = pd.DataFrame([tuple(row) for row in source_rows], columns=daily_columns)
    daily_csv = pd.DataFrame({
        "date": [
            f"{stamp.month}/{stamp.day}/{stamp.year}"
            for stamp in pd.to_datetime(daily_source["timestamp"])
        ],
    })
    for state in history_store.STATE_CODES:
        for measure in ("retail", "wholesale", "mandi_modal"):
            canonical = history_store.PRICE_COLUMNS[state][measure]
            raw_column = (f"{state}_{measure}_rs_kg" if measure != "mandi_modal"
                          else f"{state}_mandi_modal_rs_kg")
            daily_csv[raw_column] = daily_source[canonical] / 100.0
        daily_csv[f"{state}_arrival_t_day"] = (
            daily_source[history_store.QUANTITY_COLUMNS[state]["arrival"]] / 10.0
        )
    daily_path = tmp_path / "daily_prices.csv"
    daily_csv.to_csv(daily_path, index=False)
    monkeypatch.setattr(history_store, "DAILY_PRICE_CSV_PATH", daily_path)


def test_run_once_dry_run_writes_nothing(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(pull_upag_dash, "today_ist", lambda: dt.date(2026, 10, 28))
    mp_rows = [make_row("MP", "27 Oct 2026", "Retail Prices", 2605.0)]
    _stub_fetch(monkeypatch, {"MP": mp_rows})
    exit_code = pull_upag_dash.main(["--dry-run", "--states", "MP"])
    assert exit_code == 0
    with history_store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]
        logs = conn.execute("SELECT COUNT(*) AS n FROM ingestion_log").fetchone()["n"]
    assert count == 3  # dry-run must not add rows
    assert logs == 1   # only the seed import's own audit row; the run adds none
    assert not (tmp_path / "corrections_log.csv").exists()
    assert not (tmp_path / "pull.lock").exists()


def test_run_once_appends_yesterday(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(pull_upag_dash, "today_ist", lambda: dt.date(2026, 10, 28))
    mp_rows = [make_row("MP", "27 Oct 2026", "Retail Prices", 2605.0)]
    _stub_fetch(monkeypatch, {"MP": mp_rows})
    exit_code = pull_upag_dash.main(["--once", "--states", "MP"])
    assert exit_code == 0
    frame = history_store.load_history()
    assert len(frame) == 4
    row = observation_row("2026-10-27")
    assert row[MP_RETAIL] == pytest.approx(2605.0)
    assert row["MP_production_metric_quintal_per_day"] == pytest.approx(20000.0)  # forward-filled
    assert row["source"] == "upag_dash_6h"
    with history_store.connect() as conn:
        latest = conn.execute("SELECT source, status, inserted FROM ingestion_log "
                              "ORDER BY id DESC LIMIT 1").fetchone()
    assert latest["source"] == "upag_dash_6h" and latest["status"] == "success"
    assert latest["inserted"] == 1
    assert not (tmp_path / "pull.lock").exists()


def test_run_once_writes_graph_tables_in_their_dataset_units(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(pull_upag_dash, "today_ist", lambda: dt.date(2026, 10, 28))
    ap_rows = [
        make_row("AP", "27 Oct 2026", "Retail Prices", 4100.0),
        make_row("AP", "27 Oct 2026", "Mandi Arrivals", 2.0,
                 main_category="Arrivals", uom="Tonnes"),
    ]
    _stub_fetch(monkeypatch, {"AP": ap_rows})

    exit_code = pull_upag_dash.main(["--once", "--states", "AP"])
    assert exit_code == 0

    with history_store.connect() as conn:
        daily = conn.execute(
            'SELECT * FROM daily_price_observations WHERE timestamp=?', ("2026-10-27",)
        ).fetchone()
        monthly = conn.execute(
            'SELECT * FROM ap_monthly_observations WHERE month_start=?', ("2026-10-01",)
        ).fetchone()
    assert daily["AP_retail_rs_per_metric_quintal"] == pytest.approx(4100.0)
    assert daily["AP_arrival_metric_quintal"] == pytest.approx(20.0)
    assert daily["MP_retail_rs_per_metric_quintal"] == pytest.approx(4000.0)
    daily_csv = pd.read_csv(history_store.DAILY_PRICE_CSV_PATH)
    latest_csv = daily_csv.iloc[-1]
    assert pd.to_datetime(latest_csv["date"]).date().isoformat() == "2026-10-27"
    assert latest_csv["AP_retail_rs_kg"] == pytest.approx(41.0)
    assert latest_csv["AP_arrival_t_day"] == pytest.approx(2.0)
    assert latest_csv["MP_retail_rs_kg"] == pytest.approx(40.0)
    assert monthly["production_t_month"] == pytest.approx(52000.0)
    assert monthly["consumption_t_month"] == pytest.approx(69000.0)
    assert monthly["arrival_t_month"] == pytest.approx(3002.0)


def test_run_once_state_failure_isolation(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(pull_upag_dash, "today_ist", lambda: dt.date(2026, 10, 28))
    mp_rows = [make_row("MP", "27 Oct 2026", "Retail Prices", 2605.0)]
    _stub_fetch(monkeypatch, {"MP": mp_rows}, fail_states={"AP"})
    exit_code = pull_upag_dash.main(["--once", "--states", "AP,MP"])
    assert exit_code == 1
    assert observation_row("2026-10-27")[MP_RETAIL] == pytest.approx(2605.0)  # MP still ingested
    with history_store.connect() as conn:
        errors = conn.execute("SELECT source FROM ingestion_log WHERE status='error'").fetchall()
    assert any("AP" in e["source"] for e in errors)


def test_run_once_gapfill_when_db_behind(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch, days=3, end="2026-10-24")
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(pull_upag_dash, "today_ist", lambda: dt.date(2026, 10, 28))
    mp_rows = [make_row("MP", "27 Oct 2026", "Retail Prices", 2605.0)]
    _stub_fetch(monkeypatch, {"MP": mp_rows})
    exit_code = pull_upag_dash.main(["--once", "--states", "MP"])
    assert exit_code == 0
    assert len(history_store.load_history()) == 6  # 22,23,24 + gapfill 25,26 + sourced 27
    assert observation_row("2026-10-25")["source"] == "upag_dash_6h_gapfill"
    assert observation_row("2026-10-26")["source"] == "upag_dash_6h_gapfill"
    assert observation_row("2026-10-27")["source"] == "upag_dash_6h"


def test_run_once_second_run_reports_no_inserts(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(pull_upag_dash, "today_ist", lambda: dt.date(2026, 10, 28))
    mp_rows = [make_row("MP", "27 Oct 2026", "Retail Prices", 2605.0)]
    _stub_fetch(monkeypatch, {"MP": mp_rows})
    pull_upag_dash.main(["--once", "--states", "MP"])
    # Second scheduled run of the same day must be a no-op.
    exit_code = pull_upag_dash.main(["--once", "--states", "MP"])
    assert exit_code == 0
    assert len(history_store.load_history()) == 4
    assert observation_row("2026-10-27")[MP_RETAIL] == pytest.approx(2605.0)
    daily_csv = pd.read_csv(history_store.DAILY_PRICE_CSV_PATH)
    assert len(daily_csv) == 4
    assert pd.to_datetime(daily_csv["date"]).max().date().isoformat() == "2026-10-27"


def test_run_once_applies_correction_from_source(tmp_path, monkeypatch):
    seed_history(tmp_path, monkeypatch)
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(pull_upag_dash, "today_ist", lambda: dt.date(2026, 10, 28))
    mp_rows = [make_row("MP", "27 Oct 2026", "Retail Prices", 2605.0)]
    _stub_fetch(monkeypatch, {"MP": mp_rows})
    pull_upag_dash.main(["--once", "--states", "MP"])
    # The source later publishes a corrected value for the same date.
    _stub_fetch(monkeypatch, {"MP": [make_row("MP", "27 Oct 2026", "Retail Prices", 2630.0)]})
    exit_code = pull_upag_dash.main(["--once", "--states", "MP"])
    assert exit_code == 0
    row = observation_row("2026-10-27")
    assert row[MP_RETAIL] == pytest.approx(2630.0)
    assert row["source"] == "upag_dash_6h_correction"
    assert (tmp_path / "corrections_log.csv").exists()
