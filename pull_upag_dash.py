"""Scheduled ingestion job: pull yesterday's UPAg Dash data for 4 states into SQLite.

Run modes:
  python pull_upag_dash.py --once --json      default action (intended for Task Scheduler)
  python pull_upag_dash.py --dry-run          fetch + compare, zero DB writes
  python pull_upag_dash.py --days 3           catch-up back 3 days from yesterday
  python pull_upag_dash.py --full-history     2022-01-01..yesterday reconciliation
  python pull_upag_dash.py --states AP,MP     limit states

Each run requests the previous calendar day (Asia/Kolkata). Values already in the
dataset are compared; missing dates are appended (unsourced columns carry the
previous row forward), differing values are corrected (single column, audited),
identical values are skipped, so the 4 daily runs are idempotent.

Exit codes: 0 success / skipped-with-lock-busy; 1 one or more states failed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

import dash_client
import dash_extract
import history_store

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("onion-dash-ingestion")

# India's business timezone; a fixed offset avoids needing an IANA tz database on Windows.
IST_TZ = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")
DATASET_FIRST_DATE = dt.date(2022, 1, 1)
DEFAULT_LOCK_MAX_AGE_SEC = 7200
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
SNAPSHOT_PREFIX = "upag_dash_"
LOCK_PATH = ROOT / "data" / "pull_upag_dash.lock"
CORRECTIONS_CSV = ROOT / "data" / "corrections_log.csv"
CORRECTIONS_COLUMNS = ["ingested_utc", "state", "date", "column", "old_value", "new_value", "applied"]
SOURCE_DEFAULT = "upag_dash_6h"
SOURCE_CORRECTION = "upag_dash_6h_correction"
SOURCE_GAPFILL = "upag_dash_6h_gapfill"


def today_ist() -> dt.date:
    return dt.datetime.now(IST_TZ).date()


def daterange(from_date: dt.date, to_date: dt.date):
    day = from_date
    while day <= to_date:
        yield day
        day += dt.timedelta(days=1)


def plan_window(today: dt.date, days_back: int, explicit_from: str | None, explicit_to: str | None,
                full_history: bool) -> tuple[str, str]:
    """One scheduled run's requested window as (from_iso, to_iso). Pure for tests."""
    if explicit_from or explicit_to:
        if not (explicit_from and explicit_to):
            raise ValueError("--from and --to must be provided together.")
        try:
            from_date = dt.date.fromisoformat(explicit_from)
            to_date = dt.date.fromisoformat(explicit_to)
        except ValueError as exc:
            raise ValueError(f"Dates must be ISO YYYY-MM-DD strings: {exc}") from exc
        if from_date > to_date:
            raise ValueError("--from must not be after --to.")
        return from_date.isoformat(), to_date.isoformat()
    yesterday = today - dt.timedelta(days=1)
    if full_history:
        return DATASET_FIRST_DATE.isoformat(), yesterday.isoformat()
    days_back = max(1, int(days_back))
    return (today - dt.timedelta(days=days_back)).isoformat(), yesterday.isoformat()


def extend_window_for_gap(from_iso: str, db_last_iso: str | None) -> tuple[str, list[str]]:
    """Never create a date gap: when the DB ends before the requested window,
    move the request start back to DB_last + 1.

    Returns (from_iso, bridged_window_dates) where bridged_window_dates lists the
    extra calendar dates added in front of the original window (they usually end
    up forward-filled gap rows when the source has no data for them).
    """
    if not db_last_iso:
        return from_iso, []
    db_last = dt.date.fromisoformat(db_last_iso)
    from_date = dt.date.fromisoformat(from_iso)
    first_needed = db_last + dt.timedelta(days=1)
    if from_date <= first_needed:
        return from_iso, []
    bridged = [d.isoformat() for d in daterange(first_needed, from_date - dt.timedelta(days=1))]
    logger.warning("DB history ends %s; window extended back to %s to bridge %d date(s).",
                   db_last.isoformat(), first_needed.isoformat(), len(bridged))
    return first_needed.isoformat(), bridged


def detect_gap_dates(from_iso: str, to_iso: str, provided_dates: set[str],
                     existing_dates: set[str]) -> list[str]:
    """Window dates missing from the DB that no successful state provided."""
    return [d.isoformat() for d in daterange(dt.date.fromisoformat(from_iso), dt.date.fromisoformat(to_iso))
            if d.isoformat() not in provided_dates and d.isoformat() not in existing_dates]


def frame_to_updates(frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    """DataFrame (ISO date index, canonical columns) -> date -> {column: value}."""
    updates: dict[str, dict[str, float]] = {}
    for date_iso, row in frame.iterrows():
        values = {column: float(value) for column, value in row.items() if not pd.isna(value)}
        if values:
            updates[str(date_iso)] = values
    return updates


@dataclass
class StateOutcome:
    state: str
    status: str = "ok"
    rows_seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    dates_with_data: list[str] = field(default_factory=list)
    latest_source_date: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _env_flag(name: str, default: str = "1") -> bool:
    return str(os.getenv(name, default)).strip() == "1"


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except ValueError:
        logger.warning("%s is not an integer; using default %d", name, default)
        return default


def read_lock() -> dict[str, Any] | None:
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def acquire_lock(max_age_seconds: int = DEFAULT_LOCK_MAX_AGE_SEC) -> bool:
    """PID + timestamp lock; a stale lock older than max_age is taken over."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    now_utc = dt.datetime.now(dt.timezone.utc)
    held = read_lock()
    if held and held.get("created_utc"):
        try:
            created = dt.datetime.fromisoformat(str(held["created_utc"]))
            age = (now_utc - created).total_seconds()
        except ValueError:
            age = max_age_seconds + 1
        if age <= max_age_seconds:
            logger.info("Previous ingestion run (PID %s) still active; skipping.", held.get("pid"))
            return False
        logger.warning("Stale ingestion lock (%d s old) - taking over.", int(age))
    LOCK_PATH.write_text(json.dumps({"pid": os.getpid(), "created_utc": now_utc.isoformat(timespec="seconds")}),
                         encoding="utf-8")
    return True


def release_lock() -> None:
    held = read_lock()
    if held and held.get("pid") == os.getpid():
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass


def _snapshot_path(state: str) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return SNAPSHOT_DIR / f"{SNAPSHOT_PREFIX}{state}_{stamp}.json"


def write_snapshot(state: str, from_iso: str, to_iso: str, rows: list[dict[str, Any]]) -> Path | None:
    if not _env_flag("UPAG_DASH_SNAPSHOTS", "1") or not rows:
        return None
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = _snapshot_path(state)
        path.write_text(json.dumps({"state": state, "state_name": dash_client.STATE_CONFIG[state][0],
                                    "window": {"from": from_iso, "to": to_iso}, "fetched_at_utc":
                                    dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                    "rows": rows}, ensure_ascii=False), encoding="utf-8")
        prune_snapshots(_env_int("UPAG_DASH_SNAPSHOT_KEEP", 30))
        return path
    except OSError as exc:
        logger.warning("Snapshot write failed (%s); continuing without it.", exc)
        return None


def prune_snapshots(keep: int) -> None:
    try:
        files = sorted(SNAPSHOT_DIR.glob(f"{SNAPSHOT_PREFIX}*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[max(keep, 0):]:
            path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Snapshot pruning failed: %s", exc)


def append_corrections_csv(state: str, corrections: list[dict[str, Any]]) -> int:
    if not corrections:
        return 0
    written = 0
    header_needed = not CORRECTIONS_CSV.exists()
    try:
        with CORRECTIONS_CSV.open("a", newline="", encoding="utf-8") as handle:
            if header_needed:
                handle.write(",".join(CORRECTIONS_COLUMNS) + "\n")
            for entry in corrections:
                handle.write(",".join(str(entry[key]) for key in
                                      ("ingested_utc", "state", "date", "column", "old_value", "new_value", "applied"))
                             + "\n")
                written += 1
    except OSError as exc:
        logger.warning("Corrections log write failed: %s", exc)
    return written


def pull_state(state: str, from_iso: str, to_iso: str, session, dry_run: bool,
               update_existing: bool) -> tuple[StateOutcome, dict[str, dict[str, float]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch, parse, anchor-check and classify one state. Never writes to the DB."""
    outcome = StateOutcome(state=state)
    rows = dash_client.fetch_state(state, from_iso, to_iso, session=session)
    frame, meta = dash_extract.extract_series(rows, state)
    dash_extract.assert_anchors(frame, state)
    updates = frame_to_updates(frame)
    outcome.rows_seen = int(meta["rows_seen"])
    outcome.dates_with_data = list(meta["dates_with_data"])
    outcome.latest_source_date = meta["dates_with_data"][-1] if meta["dates_with_data"] else None
    classification = history_store.classify_dash_updates(updates)
    outcome.unchanged = int(classification["unchanged"])
    missing = set(classification["missing_dates"])
    outcome.inserted = sum(len(v) for d, v in updates.items() if d in missing)
    corrections = list(classification["corrections"])
    applied = not dry_run and update_existing
    outcome.updated = len(corrections) if applied else 0
    if corrections and not applied:
        # Corrections discovered but intentionally not applied (UPAG_DASH_UPDATE_EXISTING=0).
        outcome.unchanged += len(corrections)
    return outcome, updates, corrections, rows


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    started_at = time.monotonic()
    days_back = args.days if args.days is not None else _env_int("UPAG_DASH_DAYS", 1)
    update_existing = _env_flag("UPAG_DASH_UPDATE_EXISTING", "1")
    from_iso, to_iso = plan_window(today_ist(), days_back, args.from_date, args.to_date, args.full_history)
    if not args.dry_run and not _env_flag("UPAG_DASH_ENABLED", "1"):
        return {"status": "skipped", "reason": "UPAG_DASH_ENABLED is not 1; set the flag in .env to enable runs."}
    if not acquire_lock(_env_int("UPAG_DASH_LOCK_MAX_AGE_SEC", DEFAULT_LOCK_MAX_AGE_SEC)):
        return {"status": "skipped", "reason": "previous run still active (lock held)."}

    db_last_before = history_store.latest_observation_date()
    from_iso, bridged = extend_window_for_gap(from_iso, db_last_before)
    outcomes: list[StateOutcome] = []
    state_updates: dict[str, dict[str, dict[str, float]]] = {}
    all_corrections: list[tuple[str, dict[str, Any]]] = []
    snapshots: list[str] = []
    session = dash_client.new_session()
    try:
        history_store.ensure_store_ready()
        for state in args.states:
            try:
                outcome, updates, corrections, raw_rows = pull_state(state, from_iso, to_iso, session,
                                                                    args.dry_run, update_existing)
                outcomes.append(outcome)
                state_updates[state] = updates
                for entry in corrections:
                    entry["ingested_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                    all_corrections.append((state, entry))
                if not args.dry_run:
                    path = write_snapshot(state, from_iso, to_iso, raw_rows)
                    if path is not None:
                        snapshots.append(str(path))
            except Exception as exc:
                outcome = StateOutcome(state=state, status="error", error=f"{type(exc).__name__}: {exc}")
                outcomes.append(outcome)
                logger.exception("State %s pull failed", state)
                if not args.dry_run:
                    history_store.record_sync(f"{SOURCE_DEFAULT}:{state}", "error", message=str(exc))

        ingestion = {"inserted": 0, "updated": 0, "unchanged": 0, "gap_filled": 0}
        gap_dates: list[str] = []
        upsert_result = None
        gapfill_result = None
        if not args.dry_run:
            combined: dict[str, dict[str, float]] = {}
            for updates in state_updates.values():
                for date_iso, columns in updates.items():
                    combined.setdefault(date_iso, {}).update(columns)
            if combined:
                upsert_result = history_store.upsert_dash_observations(
                    combined, source=SOURCE_DEFAULT, update_existing=update_existing)
                ingestion["inserted"] += upsert_result.inserted
                ingestion["updated"] += upsert_result.updated
                ingestion["unchanged"] += upsert_result.unchanged
            existing = history_store.existing_dates_between(from_iso, to_iso)
            provided = {d for updates in state_updates.values() for d in updates}
            gap_dates = detect_gap_dates(from_iso, to_iso, provided, existing)
            if gap_dates:
                gapfill_result = history_store.upsert_dash_observations({}, gap_dates=gap_dates,
                                                                        source=SOURCE_GAPFILL)
                ingestion["gap_filled"] += gapfill_result.gap_filled
                ingestion["inserted"] += gapfill_result.gap_filled
            corrections_written = 0
            for state, entry in all_corrections:
                line = {"ingested_utc": entry["ingested_utc"], "state": state, "date": entry["date"],
                        "column": entry["column"], "old_value": entry["old_value"],
                        "new_value": entry["new_value"], "applied": entry.get("applied", False)}
                corrections_written += append_corrections_csv(state, [line])
            ingestion["corrections_logged"] = corrections_written
            successful = [o for o in outcomes if o.status == "ok"]
            totals_rows_seen = sum(o.rows_seen for o in successful)
            latest = upsert_result.latest_observation_date if upsert_result else (
                gapfill_result.latest_observation_date if gapfill_result else db_last_before)
            history_store.record_sync(
                SOURCE_DEFAULT, "success", rows_seen=totals_rows_seen,
                inserted=ingestion["inserted"], updated=ingestion["updated"],
                unchanged=ingestion["unchanged"], latest_observation=latest,
                message=f"{len(successful)}/{len(args.states)} states ok; "
                        f"corrections_logged={corrections_written}; gap_filled={ingestion['gap_filled']}; "
                        f"window={from_iso}..{to_iso}",
            )
        else:
            for _state, entry in all_corrections:
                entry["applied"] = False
            corrections_written = 0

        ok_states = [o for o in outcomes if o.status == "ok"]
        error_states = [o for o in outcomes if o.status == "error"]
        summary: dict[str, Any] = {
            "status": "ok" if not error_states else ("error" if not ok_states else "partial"),
            "dry_run": bool(args.dry_run),
            "started_utc": started,
            "window": {"from": from_iso, "to": to_iso,
                       "auto_extended_front": bool(bridged), "bridged_dates": bridged},
            "db_last_observation_before_run": db_last_before,
            "latest_observation_date": (
                history_store.latest_observation_date() if not args.dry_run else db_last_before),
            "states": [o.as_dict() for o in outcomes],
            "gap_filled_dates": gap_dates,
            "ingestion": ingestion,
            "corrections_discovered": len(all_corrections),
            "snapshots": snapshots,
            "elapsed_seconds": round(time.monotonic() - started_at, 1),
        }
        return summary
    finally:
        release_lock()
        session.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="Perform one run and exit (default behaviour).")
    parser.add_argument("--dry-run", action="store_true", help="Fetch + compare only; no DB writes or audit rows.")
    parser.add_argument("--states", default="AP,MH,KA,MP", help="Comma-separated state codes (default: all four).")
    parser.add_argument("--days", type=int, default=None, help="Catch-up: window = today-N .. yesterday (N>=1).")
    parser.add_argument("--from", dest="from_date", default=None, help="Explicit window start (ISO, with --to).")
    parser.add_argument("--to", dest="to_date", default=None, help="Explicit window end (ISO, with --from).")
    parser.add_argument("--full-history", action="store_true", dest="full_history",
                        help="Reconcile 2022-01-01..yesterday against the DB (expected mostly unchanged).")
    parser.add_argument("--json", action="store_true", help="Print the run summary as JSON (default behaviour).")
    args = parser.parse_args(argv)
    requested = [s.strip().upper() for s in args.states.split(",") if s.strip()]
    unknown = sorted(set(requested) - set(dash_client.STATE_CONFIG))
    if unknown:
        parser.error(f"Unknown state codes: {unknown}. Allowed: {sorted(dash_client.STATE_CONFIG)}")
    args.states = list(dict.fromkeys(requested))
    if args.days is not None and args.days < 1:
        parser.error("--days must be >= 1.")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = run_once(args)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    has_error = any(state.get("status") == "error" for state in summary.get("states", []))
    if summary.get("status") == "skipped":
        return 0
    return 1 if has_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
