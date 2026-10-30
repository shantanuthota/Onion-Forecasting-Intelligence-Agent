"""SQLite dataset and forecast persistence for the onion Chronos-2 MVP.

The legacy daily observation store uses:
- prices: INR per metric quintal (₹/metric quintal)
- arrivals: metric quintals per observation/day
- production and consumption: metric quintals/day

Graph-specific tables store daily price/arrival observations in model units and AP
monthly production, consumption, and arrivals in tonnes/month. Forecast prices are
persisted separately in INR/kg.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DEFAULT_CSV = ROOT / "data" / "Onion_Model_Ready_Dataset_Chronos2_2026-10-26_Interpolated_Metric_Quintal.csv"
DEFAULT_DAILY_PRICE_CSV = Path(
    r"C:\Users\shant\Downloads\Onion_Daily_Price_Forecasting_Dataset_2022-01-01_to_2026-10-25.csv"
)
DEFAULT_AP_MONTHLY_CSV = Path(
    r"C:\Users\shant\Downloads\Onion_AP_Monthly_Dataset_2022-01_to_2026-10.csv"
)
STATE_CODES = ("AP", "KA", "MH", "MP")
STATE_NAMES = {"AP": "Andhra Pradesh", "KA": "Karnataka", "MH": "Maharashtra", "MP": "Madhya Pradesh"}

PRICE_COLUMNS = {
    state: {
        "retail": f"{state}_retail_rs_per_metric_quintal",
        "wholesale": f"{state}_wholesale_rs_per_metric_quintal",
        "mandi_modal": f"{state}_mandi_modal_rs_per_metric_quintal",
    } for state in STATE_CODES
}
QUANTITY_COLUMNS = {
    state: {
        "arrival": f"{state}_arrival_metric_quintal",
        "production": f"{state}_production_metric_quintal_per_day",
        "consumption": f"{state}_consumption_metric_quintal_per_day",
    } for state in STATE_CODES
}
SUPPLY_COLUMNS = {state: f"{state}_supply_metric_quintal_per_day" for state in STATE_CODES}
EXPECTED_COLUMNS = ["item_id", "timestamp"]
for _state in STATE_CODES:
    EXPECTED_COLUMNS.extend(PRICE_COLUMNS[_state][m] for m in ("retail", "wholesale", "mandi_modal"))
    EXPECTED_COLUMNS.extend(QUANTITY_COLUMNS[_state][m] for m in ("arrival", "production", "consumption"))
NUMERIC_COLUMNS = [c for c in EXPECTED_COLUMNS if c not in ("item_id", "timestamp")]


def _resolve_path(env_name: str, fallback: Path) -> Path:
    raw = os.getenv(env_name)
    if not raw:
        return fallback
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()

CSV_PATH = _resolve_path("ONION_DATA_PATH", DEFAULT_CSV)
DAILY_PRICE_CSV_PATH = _resolve_path("ONION_DAILY_PRICE_DATA_PATH", DEFAULT_DAILY_PRICE_CSV)
AP_MONTHLY_CSV_PATH = _resolve_path("ONION_AP_MONTHLY_DATA_PATH", DEFAULT_AP_MONTHLY_CSV)
DB_PATH = _resolve_path("ONION_DB_PATH", ROOT / "data" / "onion_forecasting_v4.sqlite3")

DAILY_GRAPH_COLUMNS = [
    "item_id", "timestamp",
    *(PRICE_COLUMNS[state][measure] for state in STATE_CODES for measure in ("retail", "wholesale", "mandi_modal")),
    *(QUANTITY_COLUMNS[state]["arrival"] for state in STATE_CODES),
]
MONTHLY_GRAPH_COLUMNS = [
    "month_start", "production_t_month", "consumption_t_month", "arrival_t_month",
]


@dataclass
class SyncResult:
    source: str
    rows_seen: int
    inserted: int
    updated: int
    unchanged: int
    latest_observation_date: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "rows_seen": self.rows_seen, "inserted": self.inserted,
                "updated": self.updated, "unchanged": self.unchanged,
                "latest_observation_date": self.latest_observation_date}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='observations'").fetchone()
    if existing:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("observations")').fetchall()}
        if not set(EXPECTED_COLUMNS).issubset(cols):
            legacy_name = "observations_legacy_previous"
            legacy_exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (legacy_name,)).fetchone()
            if not legacy_exists:
                conn.execute(f'ALTER TABLE observations RENAME TO "{legacy_name}"')
            else:
                conn.execute("DROP TABLE observations")
    cols_sql = ['"timestamp" TEXT PRIMARY KEY', '"item_id" TEXT NOT NULL',
                *[f'"{c}" REAL NOT NULL' for c in NUMERIC_COLUMNS],
                '"source" TEXT NOT NULL', '"ingested_at" TEXT NOT NULL']
    conn.execute(f"CREATE TABLE IF NOT EXISTS observations ({', '.join(cols_sql)})")
    conn.execute("""CREATE TABLE IF NOT EXISTS ingestion_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TEXT NOT NULL, source TEXT NOT NULL,
        status TEXT NOT NULL, rows_seen INTEGER NOT NULL DEFAULT 0, inserted INTEGER NOT NULL DEFAULT 0,
        updated INTEGER NOT NULL DEFAULT 0, unchanged INTEGER NOT NULL DEFAULT 0,
        latest_observation_date TEXT, message TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS source_sync_state (
        source_path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, synced_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS forecast_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT, graph_code TEXT NOT NULL, generated_at TEXT NOT NULL,
        model_id TEXT NOT NULL, horizon_days INTEGER NOT NULL, last_observation_date TEXT NOT NULL,
        context_hash TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_forecast_runs_graph_latest ON forecast_runs(graph_code, generated_at DESC)")
    conn.execute("""CREATE TABLE IF NOT EXISTS forecast_values (
        run_id INTEGER NOT NULL, state_code TEXT NOT NULL, target_key TEXT NOT NULL,
        forecast_date TEXT NOT NULL, p10 REAL NOT NULL, p50 REAL NOT NULL, p90 REAL NOT NULL,
        unit TEXT NOT NULL, PRIMARY KEY (run_id,state_code,target_key,forecast_date),
        FOREIGN KEY (run_id) REFERENCES forecast_runs(run_id) ON DELETE CASCADE)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_forecast_values_latest ON forecast_values(state_code,target_key,forecast_date)")
    daily_value_columns = ", ".join(
        f'"{column}" REAL NOT NULL' for column in DAILY_GRAPH_COLUMNS[2:])
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS daily_price_observations ('
        f'"timestamp" TEXT PRIMARY KEY, "item_id" TEXT NOT NULL, {daily_value_columns})'
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS ap_monthly_observations (
        month_start TEXT PRIMARY KEY, production_t_month REAL NOT NULL,
        consumption_t_month REAL NOT NULL, arrival_t_month REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS graph_dataset_sync_state (
        dataset_key TEXT PRIMARY KEY, source_path TEXT NOT NULL, source_mtime_ns INTEGER NOT NULL,
        source_size INTEGER NOT NULL, synced_at TEXT NOT NULL)""")


def _canonicalize_source_columns(frame: pd.DataFrame) -> pd.DataFrame:
    src = frame.copy()
    src.columns = [str(c).replace("\ufeff", "").strip() for c in src.columns]
    rename = {}
    for state in STATE_CODES:
        # Price column alternatives, including the older MVP's INR/kg columns.
        for measure in ("retail", "wholesale", "mandi_modal"):
            target = PRICE_COLUMNS[state][measure]
            alternatives = [
                target,
                f"{state}_{measure}_rs_per_metric_quintal",
                f"{state}_{measure}_rs_kg",
                f"{state}_{measure}_rs_kg" if measure != "mandi_modal" else f"{state}_mandi_modal_rs_kg",
            ]
            for candidate in alternatives:
                if candidate in src.columns:
                    rename[candidate] = target
                    break
        # Quantity alternatives.
        quantity_alts = {
            "arrival": [QUANTITY_COLUMNS[state]["arrival"], f"{state}_arrival_mq", f"{state}_arrival_t",
                        f"{state}_arrival_t_day"],
            "production": [QUANTITY_COLUMNS[state]["production"], f"{state}_production_mq_day", f"{state}_production_t_day"],
            "consumption": [QUANTITY_COLUMNS[state]["consumption"], f"{state}_consumption_mq_day", f"{state}_consumption_t_day"],
        }
        for measure, candidates in quantity_alts.items():
            for candidate in candidates:
                if candidate in src.columns:
                    rename[candidate] = QUANTITY_COLUMNS[state][measure]
                    break
    src = src.rename(columns=rename)
    return src


def _validate_graph_frame(out: pd.DataFrame, date_column: str, frequency: str) -> pd.DataFrame:
    if out.empty:
        raise ValueError("Dataset must contain at least one observation.")
    if out[date_column].isna().any():
        raise ValueError(f"Dataset contains invalid or blank {date_column} values.")
    if out[date_column].duplicated().any():
        raise ValueError(f"Dataset contains duplicate {date_column} values.")
    for column in out.columns:
        if column in (date_column, "item_id"):
            continue
        out[column] = pd.to_numeric(out[column], errors="coerce")
    numeric = [column for column in out.columns if column not in (date_column, "item_id")]
    bad = [column for column in numeric if out[column].isna().any()
           or not np.isfinite(out[column].to_numpy(dtype=float)).all()]
    if bad:
        raise ValueError("Dataset has missing/non-numeric/non-finite values in: " + ", ".join(bad))
    out = out.sort_values(date_column).reset_index(drop=True)
    if frequency == "D":
        expected = pd.date_range(out[date_column].min(), out[date_column].max(), freq="D")
        observed = pd.DatetimeIndex(out[date_column])
    else:
        expected = pd.date_range(out[date_column].min(), out[date_column].max(), freq="MS")
        observed = pd.DatetimeIndex(out[date_column])
    if not observed.equals(expected):
        raise ValueError(f"Dataset must have a continuous {frequency} date index.")
    return out


def normalize_daily_price_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize the new daily market dataset to graph 1/2 model columns."""
    original = frame.copy()
    original.columns = [str(c).replace("\ufeff", "").strip() for c in original.columns]
    if "timestamp" not in original.columns and "date" in original.columns:
        original = original.rename(columns={"date": "timestamp"})
    src = _canonicalize_source_columns(original)
    if "timestamp" not in src.columns:
        raise ValueError("Daily price dataset must include a 'date' or 'timestamp' column.")

    out = pd.DataFrame()
    out["timestamp"] = pd.to_datetime(src["timestamp"], errors="coerce").dt.normalize()
    out["item_id"] = src["item_id"].astype(str) if "item_id" in src.columns else "AP_onion"
    for state in STATE_CODES:
        for measure in ("retail", "wholesale", "mandi_modal"):
            canonical = PRICE_COLUMNS[state][measure]
            if canonical not in src.columns:
                raise ValueError(f"Missing daily price column for {state} {measure}.")
            values = pd.to_numeric(src[canonical], errors="coerce")
            old_name = f"{state}_{measure}_rs_kg"
            if measure == "mandi_modal":
                old_name = f"{state}_mandi_modal_rs_kg"
            if old_name in original.columns and canonical not in original.columns:
                values = values * 100.0
            out[canonical] = values

        arrival = QUANTITY_COLUMNS[state]["arrival"]
        if arrival not in src.columns:
            raise ValueError(f"Missing daily arrival column for {state}.")
        values = pd.to_numeric(src[arrival], errors="coerce")
        if f"{state}_arrival_t_day" in original.columns and arrival not in original.columns:
            values = values * 10.0
        out[arrival] = values

    if out["item_id"].nunique() != 1:
        raise ValueError("Daily graph dataset must contain one item_id and one aligned row per date.")
    out["item_id"] = out["item_id"].astype(str)
    return _validate_graph_frame(out[DAILY_GRAPH_COLUMNS], "timestamp", "D")


def normalize_ap_monthly_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize monthly AP quantities, preserving their source tonnes/month units."""
    src = frame.copy()
    src.columns = [str(c).replace("\ufeff", "").strip() for c in src.columns]
    if "month_start" not in src.columns:
        raise ValueError("Monthly AP dataset must include a 'month_start' column.")
    required = MONTHLY_GRAPH_COLUMNS[1:]
    missing = [column for column in required if column not in src.columns]
    if missing:
        raise ValueError("Monthly AP dataset is missing columns: " + ", ".join(missing))
    parsed = pd.to_datetime(src["month_start"], format="%b-%y", errors="coerce")
    invalid = parsed.isna()
    if invalid.any():
        parsed.loc[invalid] = pd.to_datetime(src.loc[invalid, "month_start"], errors="coerce")

    out = pd.DataFrame({
        "month_start": parsed.dt.to_period("M").dt.to_timestamp(),
        **{column: src[column] for column in required},
    })
    return _validate_graph_frame(out[MONTHLY_GRAPH_COLUMNS], "month_start", "MS")


def _sync_graph_dataset(dataset_key: str, path: Path, table: str, frame: pd.DataFrame,
                        date_column: str) -> None:
    stat = path.stat()
    resolved = str(path.resolve())
    with connect() as conn:
        _create_schema(conn)
        state = conn.execute(
            "SELECT source_path,source_mtime_ns,source_size FROM graph_dataset_sync_state WHERE dataset_key=?",
            (dataset_key,),
        ).fetchone()
        if (state is not None and state["source_path"] == resolved
                and int(state["source_mtime_ns"]) == int(stat.st_mtime_ns)
                and int(state["source_size"]) == int(stat.st_size)):
            return

        columns = list(frame.columns)
        quoted_columns = ",".join(f'"{column}"' for column in columns)
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(
            f'"{column}"=excluded."{column}"'
            for column in columns if column != date_column
        )
        records = []
        for row in frame.itertuples(index=False, name=None):
            values = list(row)
            date_index = columns.index(date_column)
            values[date_index] = pd.Timestamp(values[date_index]).strftime("%Y-%m-%d")
            records.append(tuple(values))
        conn.executemany(
            f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders}) '
            f'ON CONFLICT("{date_column}") DO UPDATE SET {updates}',
            records,
        )
        conn.execute("""INSERT INTO graph_dataset_sync_state
            (dataset_key,source_path,source_mtime_ns,source_size,synced_at) VALUES(?,?,?,?,?)
            ON CONFLICT(dataset_key) DO UPDATE SET source_path=excluded.source_path,
            source_mtime_ns=excluded.source_mtime_ns,source_size=excluded.source_size,
            synced_at=excluded.synced_at""",
            (dataset_key, resolved, int(stat.st_mtime_ns), int(stat.st_size),
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def load_daily_price_history() -> pd.DataFrame:
    if not DAILY_PRICE_CSV_PATH.exists():
        raise FileNotFoundError(f"Daily price dataset not found: {DAILY_PRICE_CSV_PATH}")
    frame = normalize_daily_price_frame(
        pd.read_csv(DAILY_PRICE_CSV_PATH, encoding="utf-8-sig"))
    _sync_graph_dataset("daily_prices", DAILY_PRICE_CSV_PATH,
                        "daily_price_observations", frame, "timestamp")
    with connect() as conn:
        quoted_columns = ",".join(f'"{column}"' for column in DAILY_GRAPH_COLUMNS)
        stored = pd.read_sql_query(
            f'SELECT {quoted_columns} FROM daily_price_observations ORDER BY timestamp',
            conn,
        )
    stored["timestamp"] = pd.to_datetime(stored["timestamp"])
    return stored


def load_ap_monthly_history() -> pd.DataFrame:
    if not AP_MONTHLY_CSV_PATH.exists():
        raise FileNotFoundError(f"Monthly AP dataset not found: {AP_MONTHLY_CSV_PATH}")
    frame = normalize_ap_monthly_frame(
        pd.read_csv(AP_MONTHLY_CSV_PATH, encoding="utf-8-sig"))
    _sync_graph_dataset("ap_monthly", AP_MONTHLY_CSV_PATH,
                        "ap_monthly_observations", frame, "month_start")
    with connect() as conn:
        quoted_columns = ",".join(f'"{column}"' for column in MONTHLY_GRAPH_COLUMNS)
        stored = pd.read_sql_query(
            f'SELECT {quoted_columns} FROM ap_monthly_observations ORDER BY month_start',
            conn,
        )
    stored["month_start"] = pd.to_datetime(stored["month_start"])
    return stored


def ensure_graph_datasets_ready() -> None:
    """Import the configured graph datasets when their SQLite tables are empty."""
    with connect() as conn:
        _create_schema(conn)
        daily_count = int(conn.execute(
            "SELECT COUNT(*) AS n FROM daily_price_observations").fetchone()["n"])
        monthly_count = int(conn.execute(
            "SELECT COUNT(*) AS n FROM ap_monthly_observations").fetchone()["n"])
    if daily_count == 0:
        load_daily_price_history()
    if monthly_count == 0:
        load_ap_monthly_history()


def normalize_source_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize schema to DB's canonical columns and units (prices ₹/metric quintal).

    The cleaned standardized file already uses ₹/metric quintal and metric quintals.
    Legacy *_rs_kg prices are multiplied by 100; legacy tonne quantities are multiplied
    by 10. Unit conversions are only done when the input column name indicates the old unit.
    """
    original = frame.copy()
    original.columns = [str(c).replace("\ufeff", "").strip() for c in original.columns]
    src = _canonicalize_source_columns(original)
    if "timestamp" not in src.columns:
        raise ValueError("Dataset must include a 'timestamp' date column.")
    out = pd.DataFrame()
    out["timestamp"] = pd.to_datetime(src["timestamp"], errors="coerce")
    if out["timestamp"].isna().any():
        raise ValueError("Input contains invalid or blank timestamps.")
    out["timestamp"] = out["timestamp"].dt.normalize()
    out["item_id"] = src["item_id"].astype(str) if "item_id" in src.columns else "AP_onion"

    for state in STATE_CODES:
        for measure in ("retail", "wholesale", "mandi_modal"):
            canonical = PRICE_COLUMNS[state][measure]
            if canonical not in src.columns:
                raise ValueError(f"Missing price column for {state} {measure}: expected {canonical}.")
            vals = pd.to_numeric(src[canonical], errors="coerce")
            # If source was the legacy model-ready file, its old name means ₹/kg.
            old_name = f"{state}_{measure}_rs_kg"
            if measure == "mandi_modal":
                old_name = f"{state}_mandi_modal_rs_kg"
            if old_name in original.columns and canonical not in original.columns:
                vals = vals * 100.0
            out[canonical] = vals
        for measure in ("arrival", "production", "consumption"):
            canonical = QUANTITY_COLUMNS[state][measure]
            if canonical not in src.columns:
                raise ValueError(f"Missing quantity column for {state} {measure}: expected {canonical}.")
            vals = pd.to_numeric(src[canonical], errors="coerce")
            legacy = f"{state}_{measure}_t" if measure == "arrival" else f"{state}_{measure}_t_day"
            if legacy in original.columns and canonical not in original.columns:
                vals = vals * 10.0
            out[canonical] = vals

    out = out[EXPECTED_COLUMNS]
    if out["timestamp"].duplicated().any():
        dates = out.loc[out["timestamp"].duplicated(keep=False), "timestamp"].dt.strftime("%Y-%m-%d").head(4).tolist()
        raise ValueError(f"Input contains duplicate dates, e.g. {dates}.")
    for col in NUMERIC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    bad = [c for c in NUMERIC_COLUMNS if out[c].isna().any() or not np.isfinite(out[c].to_numpy(dtype=float)).all()]
    if bad:
        raise ValueError("Input has missing/non-numeric/non-finite values in: " + ", ".join(bad))
    if out["item_id"].nunique() != 1:
        raise ValueError("Combined dataset must contain one item_id and one aligned row per date.")
    out["item_id"] = out["item_id"].astype(str)
    out = out.sort_values("timestamp").reset_index(drop=True)
    expected = pd.date_range(out["timestamp"].min(), out["timestamp"].max(), freq="D")
    if not pd.DatetimeIndex(out["timestamp"]).equals(expected):
        raise ValueError("Dataset must have a continuous daily date index.")
    return out


def _log_sync(conn: sqlite3.Connection, result: SyncResult, status: str = "success", message: str | None = None) -> None:
    checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("""INSERT INTO ingestion_log
        (checked_at,source,status,rows_seen,inserted,updated,unchanged,latest_observation_date,message)
        VALUES (?,?,?,?,?,?,?,?,?)""", (checked, result.source, status, result.rows_seen, result.inserted,
        result.updated, result.unchanged, result.latest_observation_date, message))


def upsert_frame(frame: pd.DataFrame, source: str = "model_ready_csv") -> SyncResult:
    out = normalize_source_frame(frame)
    inserted = updated = unchanged = 0
    ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    insert_columns = ["timestamp", "item_id", *NUMERIC_COLUMNS, "source", "ingested_at"]
    placeholders = ",".join("?" for _ in insert_columns)
    update_cols = ["item_id", *NUMERIC_COLUMNS, "source", "ingested_at"]
    update_sql = ",".join(f'"{c}"=excluded."{c}"' for c in update_cols)
    quoted_insert = ",".join(f'"{c}"' for c in insert_columns)
    compare_cols = ["item_id", *NUMERIC_COLUMNS]
    quoted_compare = ",".join(f'"{c}"' for c in compare_cols)
    sql = f'INSERT INTO observations ({quoted_insert}) VALUES ({placeholders}) ON CONFLICT(timestamp) DO UPDATE SET {update_sql}'
    with connect() as conn:
        _create_schema(conn)
        try:
            for _, row in out.iterrows():
                stamp = row["timestamp"].strftime("%Y-%m-%d")
                values = (stamp, str(row["item_id"]), *[float(row[c]) for c in NUMERIC_COLUMNS], source, ingested_at)
                existing = conn.execute(f'SELECT {quoted_compare} FROM observations WHERE timestamp=?', (stamp,)).fetchone()
                if existing is None:
                    inserted += 1
                    conn.execute(sql, values)
                else:
                    new_cmp = values[1:2 + len(NUMERIC_COLUMNS)]
                    old_cmp = tuple(existing[c] for c in compare_cols)
                    if new_cmp[0] == old_cmp[0] and all(abs(float(a)-float(b)) < 1e-9 for a,b in zip(new_cmp[1:], old_cmp[1:])):
                        unchanged += 1
                    else:
                        updated += 1
                        conn.execute(sql, values)
            latest = out["timestamp"].max().strftime("%Y-%m-%d") if not out.empty else None
            result = SyncResult(source, len(out), inserted, updated, unchanged, latest)
            _log_sync(conn, result)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            failed = SyncResult(source, len(out), inserted, updated, unchanged,
                                out["timestamp"].max().strftime("%Y-%m-%d") if not out.empty else None)
            _log_sync(conn, failed, status="error", message=str(exc))
            conn.commit()
            raise
    return result


TOLERANCE = 1e-6


@dataclass
class DashUpsertResult:
    source: str
    rows_seen: int
    inserted: int
    updated: int
    unchanged: int
    gap_filled: int = 0
    correction_count: int = 0
    latest_observation_date: str | None = None
    corrections: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "rows_seen": self.rows_seen, "inserted": self.inserted,
                "updated": self.updated, "unchanged": self.unchanged, "gap_filled": self.gap_filled,
                "corrections": self.correction_count,
                "latest_observation_date": self.latest_observation_date}


def latest_observation_date() -> str | None:
    with connect() as conn:
        _create_schema(conn)
        row = conn.execute("SELECT MAX(timestamp) AS latest, COUNT(*) AS n FROM observations").fetchone()
    if row is None or int(row["n"]) == 0:
        return None
    return str(row["latest"])


def existing_dates_between(from_iso: str, to_iso: str) -> set[str]:
    with connect() as conn:
        _create_schema(conn)
        rows = conn.execute("SELECT timestamp FROM observations WHERE timestamp >= ? AND timestamp <= ?",
                            (from_iso, to_iso)).fetchall()
    return {str(r["timestamp"]) for r in rows}


def _validate_dash_updates(series_updates: dict[str, dict[str, float]]) -> None:
    if not isinstance(series_updates, dict):
        raise ValueError("series_updates must be a dict of date -> {column: value}.")
    for stamp, columns in series_updates.items():
        try:
            datetime.strptime(str(stamp), "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"Invalid observation date {stamp!r}; expected YYYY-MM-DD: {exc}") from exc
        unknown = [c for c in columns if c not in NUMERIC_COLUMNS]
        if unknown:
            raise ValueError(f"Unknown observation columns for {stamp}: {unknown}.")
        for column, value in columns.items():
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError(f"Non-finite value for {column} on {stamp}: {value!r}.")


def upsert_dash_graph_observations(
        series_updates: dict[str, dict[str, float]],
        gap_dates: Iterable[str] | None = None,
        update_existing: bool = True) -> dict[str, Any]:
    """Mirror fetched Dash values into graph tables using their canonical units.

    Daily graph rows are completed by carrying forward the preceding row. AP
    monthly arrival totals are refreshed from daily quintals converted to tonnes;
    production and consumption remain owned by the monthly source dataset.
    """
    _validate_dash_updates(series_updates)
    daily_columns = set(DAILY_GRAPH_COLUMNS) - {"timestamp", "item_id"}
    for stamp, columns in series_updates.items():
        unknown = sorted(set(columns) - daily_columns)
        if unknown:
            raise ValueError(f"Columns are not part of daily_price_observations on {stamp}: {unknown}.")

    requested_dates = sorted(set(series_updates) | set(gap_dates or []))
    result: dict[str, Any] = {
        "rows_inserted": 0,
        "values_updated": 0,
        "values_unchanged": 0,
        "gap_rows_filled": 0,
        "monthly_arrivals_updated": 0,
        "monthly_arrival_months_missing": [],
        "latest_observation_date": None,
    }
    if not requested_dates:
        return result

    monthly_arrival_column = QUANTITY_COLUMNS["AP"]["arrival"]
    affected_months: set[str] = set()
    insert_columns = ["timestamp", "item_id", *DAILY_GRAPH_COLUMNS[2:]]
    insert_sql = (
        f'INSERT INTO "daily_price_observations" '
        f'({", ".join(chr(34) + column + chr(34) for column in insert_columns)}) '
        f'VALUES ({", ".join("?" for _ in insert_columns)})'
    )

    with connect() as conn:
        _create_schema(conn)
        try:
            for stamp in requested_dates:
                sourced = series_updates.get(stamp, {})
                if monthly_arrival_column in sourced:
                    month = datetime.strptime(stamp, "%Y-%m-%d").replace(day=1)
                    affected_months.add(month.strftime("%Y-%m-%d"))

                existing = conn.execute(
                    'SELECT * FROM "daily_price_observations" WHERE "timestamp"=?',
                    (stamp,),
                ).fetchone()
                if existing is not None:
                    changes: dict[str, float] = {}
                    for column, raw_value in sourced.items():
                        value = float(raw_value)
                        old_value = float(existing[column])
                        if abs(value - old_value) <= TOLERANCE:
                            result["values_unchanged"] += 1
                        elif update_existing:
                            changes[column] = value
                            result["values_updated"] += 1
                    if changes:
                        set_sql = ", ".join(f'"{column}"=?' for column in changes)
                        conn.execute(
                            f'UPDATE "daily_price_observations" SET {set_sql} WHERE "timestamp"=?',
                            [*changes.values(), stamp],
                        )
                    continue

                previous = conn.execute(
                    'SELECT * FROM "daily_price_observations" WHERE "timestamp"<? '
                    'ORDER BY "timestamp" DESC LIMIT 1',
                    (stamp,),
                ).fetchone()
                if previous is None:
                    raise ValueError(
                        "daily_price_observations has no earlier row to carry forward; "
                        "load the configured daily dataset before ingestion."
                    )

                previous_date = datetime.strptime(str(previous["timestamp"]), "%Y-%m-%d").date()
                target_date = datetime.strptime(stamp, "%Y-%m-%d").date()
                next_date = previous_date + timedelta(days=1)
                while next_date <= target_date:
                    day = next_date.strftime("%Y-%m-%d")
                    day_existing = conn.execute(
                        'SELECT * FROM "daily_price_observations" WHERE "timestamp"=?',
                        (day,),
                    ).fetchone()
                    if day_existing is not None:
                        previous = day_existing
                    else:
                        values = {column: previous[column] for column in insert_columns
                                  if column not in ("timestamp", "item_id")}
                        day_values = series_updates.get(day, {})
                        values.update({column: float(value) for column, value in day_values.items()})
                        conn.execute(insert_sql, [
                            day,
                            str(previous["item_id"]),
                            *[values[column] for column in insert_columns[2:]],
                        ])
                        result["rows_inserted"] += 1
                        if day_values:
                            result["values_updated"] += len(day_values)
                        else:
                            result["gap_rows_filled"] += 1
                        previous = conn.execute(
                            'SELECT * FROM "daily_price_observations" WHERE "timestamp"=?',
                            (day,),
                        ).fetchone()
                    next_date += timedelta(days=1)

            for month_start in sorted(affected_months):
                month_date = datetime.strptime(month_start, "%Y-%m-%d")
                next_month = (month_date.replace(day=28) + timedelta(days=4)).replace(day=1)
                monthly_row = conn.execute(
                    'SELECT arrival_t_month FROM "ap_monthly_observations" WHERE month_start=?',
                    (month_start,),
                ).fetchone()
                if monthly_row is None:
                    result["monthly_arrival_months_missing"].append(month_start)
                    continue
                arrival_quintals = conn.execute(
                    'SELECT COALESCE(SUM("AP_arrival_metric_quintal"), 0) AS total '
                    'FROM "daily_price_observations" WHERE "timestamp">=? AND "timestamp"<?',
                    (month_start, next_month.strftime("%Y-%m-%d")),
                ).fetchone()["total"]
                arrival_tonnes = float(arrival_quintals) / 10.0
                if abs(arrival_tonnes - float(monthly_row["arrival_t_month"])) > TOLERANCE:
                    conn.execute(
                        'UPDATE "ap_monthly_observations" SET arrival_t_month=? WHERE month_start=?',
                        (arrival_tonnes, month_start),
                    )
                    result["monthly_arrivals_updated"] += 1

            latest = conn.execute(
                'SELECT MAX("timestamp") AS latest FROM "daily_price_observations"').fetchone()["latest"]
            result["latest_observation_date"] = str(latest) if latest else None
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return result


def classify_dash_updates(series_updates: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Read-only classification of (date, column) values against the current DB.

    Returns missing_dates (rows that would be inserted), corrections (existing
    values that differ beyond TOLERANCE; never applied here) and unchanged count.
    Used by pull_upag_dash.py for --dry-run reporting and pre-write attribution.
    """
    _validate_dash_updates(series_updates)
    corrections: list[dict[str, Any]] = []
    unchanged = 0
    missing_dates: list[str] = []
    with connect() as conn:
        _create_schema(conn)
        for stamp in sorted(series_updates):
            row = conn.execute('SELECT * FROM "observations" WHERE "timestamp"=?', (stamp,)).fetchone()
            if row is None:
                missing_dates.append(stamp)
                continue
            for column in sorted(series_updates[stamp]):
                new_value = float(series_updates[stamp][column])
                old_value = float(row[column])
                if abs(new_value - old_value) <= TOLERANCE:
                    unchanged += 1
                else:
                    corrections.append({"date": stamp, "column": column,
                                        "old_value": old_value, "new_value": new_value, "applied": False})
    return {"missing_dates": missing_dates, "corrections": corrections, "unchanged": unchanged}


def upsert_dash_observations(series_updates: dict[str, dict[str, float]],
                             gap_dates: Iterable[str] | None = None,
                             source: str = "upag_dash_6h",
                             source_correction: str = "upag_dash_6h_correction",
                             source_gapfill: str = "upag_dash_6h_gapfill",
                             update_existing: bool = True) -> DashUpsertResult:
    """Partial-aware append/correct upsert for scheduled Dash ingestion.

    - For each date in series_updates with sourced values (ascending order):
      * date absent -> INSERT a complete row: sourced values + every not-sourced
        column forward-filled from the previous row (mirrors the "Interpolated"
        dataset convention). All 24 columns are REAL NOT NULL.
      * date present -> compare per column: <= TOLERANCE -> unchanged; differs ->
        correction: update ONLY that column (source/source_correction) and record
        before/after; skipped when update_existing is False (logged, not applied).
    - For each date in gap_dates: if still absent, INSERT a full forward-filled
      row (source/source_gapfill) so the daily index never gets a gap.

    The caller is responsible for writing the ingestion_log audit row
    (see record_sync); this function only performs observation-table writes.
    """
    _validate_dash_updates(series_updates)
    rows_seen = sum(len(columns) for columns in series_updates.values())
    corrections: list[dict[str, Any]] = []
    inserted = updated = unchanged = gap_filled = 0
    ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    insert_columns = ["timestamp", "item_id", *NUMERIC_COLUMNS, "source", "ingested_at"]
    insert_sql = (
        f'INSERT INTO "observations" ({", ".join(chr(34) + c + chr(34) for c in insert_columns)}) '
        f'VALUES ({", ".join("?" for _ in insert_columns)})'
    )

    with connect() as conn:
        _create_schema(conn)
        try:
            current = conn.execute('SELECT * FROM "observations" ORDER BY "timestamp" DESC LIMIT 1').fetchone()
            if current is None:
                raise ValueError(
                    "SQLite history is empty; import the model-ready CSV first with sync_model_ready_csv.py."
                )
            last_stamp = str(current["timestamp"])
            last_item_id = str(current["item_id"])
            last_values = {c: float(current[c]) for c in NUMERIC_COLUMNS}

            def refresh_last() -> None:
                nonlocal last_stamp, last_values, last_item_id
                row = conn.execute('SELECT * FROM "observations" ORDER BY "timestamp" DESC LIMIT 1').fetchone()
                if row is not None:
                    last_stamp = str(row["timestamp"])
                    last_values = {c: float(row[c]) for c in NUMERIC_COLUMNS}
                    last_item_id = str(row["item_id"])

            for stamp in sorted(set(series_updates) | set(gap_dates or [])):
                sourced = series_updates.get(stamp)
                existing = conn.execute('SELECT * FROM "observations" WHERE "timestamp"=?', (stamp,)).fetchone()
                if existing is None:
                    row_values = {c: last_values[c] for c in NUMERIC_COLUMNS}
                    if sourced:
                        row_values.update({c: float(v) for c, v in sourced.items()})
                        inserted += len(sourced)
                        label = source
                    else:
                        gap_filled += 1
                        label = source_gapfill
                    conn.execute(insert_sql, [stamp, last_item_id, *[row_values[c] for c in NUMERIC_COLUMNS],
                                              label, ingested_at])
                    if stamp > last_stamp:
                        refresh_last()
                elif sourced:
                    changes: dict[str, float] = {}
                    edits = sorted(sourced)
                    for column in edits:
                        new_value = float(sourced[column])
                        old_value = float(existing[column])
                        if abs(new_value - old_value) <= TOLERANCE:
                            unchanged += 1
                        elif update_existing:
                            changes[column] = new_value
                            updated += 1
                            corrections.append({"date": stamp, "column": column, "old_value": old_value,
                                                "new_value": new_value, "applied": True})
                        else:
                            corrections.append({"date": stamp, "column": column, "old_value": old_value,
                                                "new_value": new_value, "applied": False})
                            unchanged += 1
                    if changes:
                        set_sql = ", ".join(f'"{c}"=?' for c in changes)
                        conn.execute(
                            f'UPDATE "observations" SET {set_sql}, "source"=?, "ingested_at"=? WHERE "timestamp"=?',
                            [*changes.values(), source_correction, ingested_at, stamp],
                        )
                        if stamp >= last_stamp:
                            refresh_last()
            latest = conn.execute('SELECT MAX("timestamp") AS m FROM "observations"').fetchone()["m"]
            conn.commit()
        except Exception as exc:
            conn.rollback()
            failed = SyncResult(source, rows_seen, inserted, updated, unchanged, None)
            _log_sync(conn, failed, status="error", message=str(exc))
            conn.commit()
            raise
    return DashUpsertResult(source=source, rows_seen=rows_seen, inserted=inserted, updated=updated,
                            unchanged=unchanged, gap_filled=gap_filled, correction_count=len(corrections),
                            latest_observation_date=str(latest) if latest else None,
                            corrections=corrections)


def record_sync(source: str, status: str, rows_seen: int = 0, inserted: int = 0, updated: int = 0,
                unchanged: int = 0, latest_observation: str | None = None, message: str | None = None) -> None:
    """Public audit-row helper for callers outside this module."""
    with connect() as conn:
        _create_schema(conn)
        _log_sync(conn, SyncResult(source, rows_seen, inserted, updated, unchanged, latest_observation),
                  status=status, message=message)
        conn.commit()


def _mark_source_synced(path: Path) -> None:
    stat = path.stat()
    with connect() as conn:
        _create_schema(conn)
        conn.execute("""INSERT INTO source_sync_state(source_path,mtime_ns,synced_at) VALUES(?,?,?)
            ON CONFLICT(source_path) DO UPDATE SET mtime_ns=excluded.mtime_ns,synced_at=excluded.synced_at""",
            (str(path.resolve()), int(stat.st_mtime_ns), datetime.now(timezone.utc).isoformat(timespec="seconds")))


def sync_model_ready_csv(path: str | Path, source: str = "model_ready_csv") -> SyncResult:
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")
    frame = pd.read_csv(path, encoding="utf-8-sig")
    result = upsert_frame(frame, source=source)
    if path.resolve() == CSV_PATH.resolve():
        _mark_source_synced(path)
    return result


def ensure_store_ready() -> None:
    with connect() as conn:
        _create_schema(conn)
        count = int(conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"])
        state = conn.execute("SELECT mtime_ns FROM source_sync_state WHERE source_path=?", (str(CSV_PATH.resolve()),)).fetchone()
    if not CSV_PATH.exists():
        if count == 0:
            raise FileNotFoundError(f"No history rows and seed CSV missing at {CSV_PATH}.")
        return
    csv_mtime = CSV_PATH.stat().st_mtime_ns
    if count == 0 or state is None or int(state["mtime_ns"]) != int(csv_mtime):
        result = sync_model_ready_csv(CSV_PATH, source="initial_csv_seed" if count == 0 else "configured_csv_refresh")
        print(f"Synchronized configured dataset into SQLite: {result.as_dict()}")


def load_history() -> pd.DataFrame:
    ensure_store_ready()
    quoted = ",".join(f'"{c}"' for c in EXPECTED_COLUMNS)
    with connect() as conn:
        frame = pd.read_sql_query(f"SELECT {quoted} FROM observations ORDER BY timestamp", conn)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    for state in STATE_CODES:
        frame[SUPPLY_COLUMNS[state]] = frame[QUANTITY_COLUMNS[state]["arrival"]] + frame[QUANTITY_COLUMNS[state]["production"]]
    return frame


def save_forecast_run(graph_code: str, horizon_days: int, last_observation_date: str, context_hash: str,
                      forecast_values: Iterable[dict[str, Any]], model_id: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = list(forecast_values)
    with connect() as conn:
        _create_schema(conn)
        cur = conn.execute("""INSERT INTO forecast_runs(graph_code,generated_at,model_id,horizon_days,last_observation_date,context_hash,metadata_json)
            VALUES(?,?,?,?,?,?,?)""", (graph_code, generated_at, model_id, int(horizon_days), last_observation_date,
            context_hash, json.dumps(metadata or {}, ensure_ascii=False, default=str)))
        run_id = int(cur.lastrowid)
        conn.executemany("""INSERT INTO forecast_values(run_id,state_code,target_key,forecast_date,p10,p50,p90,unit)
            VALUES(?,?,?,?,?,?,?,?)""", [(run_id, str(v["state_code"]), str(v["target_key"]), str(v["forecast_date"]),
            float(v["p10"]), float(v["p50"]), float(v["p90"]), str(v["unit"])) for v in values])
        conn.commit()
    return {"run_id": run_id, "generated_at": generated_at, "saved_values": len(values), "graph_code": graph_code}


def get_latest_forecast(graph_code: str | None = None) -> dict[str, Any] | None:
    ensure_store_ready()
    with connect() as conn:
        if graph_code:
            row = conn.execute("SELECT * FROM forecast_runs WHERE graph_code=? ORDER BY generated_at DESC,run_id DESC LIMIT 1", (graph_code,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM forecast_runs ORDER BY generated_at DESC,run_id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        values = conn.execute("SELECT state_code,target_key,forecast_date,p10,p50,p90,unit FROM forecast_values WHERE run_id=? ORDER BY state_code,target_key,forecast_date", (row["run_id"],)).fetchall()
    result = dict(row)
    result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
    result["forecast_values"] = [dict(v) for v in values]
    return result


def list_forecast_runs(limit: int = 20) -> list[dict[str, Any]]:
    ensure_store_ready()
    with connect() as conn:
        rows = conn.execute("SELECT run_id,graph_code,generated_at,model_id,horizon_days,last_observation_date,context_hash FROM forecast_runs ORDER BY generated_at DESC,run_id DESC LIMIT ?", (max(1,min(int(limit),100)),)).fetchall()
    return [dict(r) for r in rows]


def ingestion_status() -> dict[str, Any]:
    ensure_store_ready()
    with connect() as conn:
        latest_log = conn.execute("SELECT * FROM ingestion_log ORDER BY id DESC LIMIT 1").fetchone()
        count = conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]
        dates = conn.execute("SELECT MIN(timestamp) AS first_date,MAX(timestamp) AS last_date FROM observations").fetchone()
        runs = conn.execute("SELECT COUNT(*) AS n FROM forecast_runs").fetchone()["n"]
    return {"database_path": str(DB_PATH), "observation_rows": int(count),
            "first_observation_date": dates["first_date"], "last_observation_date": dates["last_date"],
            "forecast_run_count": int(runs), "last_ingestion": dict(latest_log) if latest_log else None}
