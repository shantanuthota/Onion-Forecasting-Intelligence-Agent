"""Parse UPAg Dash MIP store rows into canonical per-date onion columns.

Series are selected strictly by (MetricMainCategoryName, MetricSubCategoryName),
and the verified UOM is asserted for each. Arrivals are converted from Tonnes to
metric quintals exactly once, here, through TONNES_TO_QUINTALS. The full `Metric`
label is never used for mapping because its "(DOCA) (DOCA)" / "(AgMarknet)"
suffixes have varied between captures.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

import pandas as pd

from dash_client import STATE_CONFIG
from history_store import PRICE_COLUMNS, QUANTITY_COLUMNS

logger = logging.getLogger("onion-dash-extract")

# CalendarDay examples: "01 Jan 2022", "27 Oct 2026". Explicit month map so the
# parser never depends on the machine's locale strftime settings.
MONTH_NUMBERS: dict[str, int] = {
    name: number for number, name in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
    )
}

# 1 tonne = 10 metric quintals (1 metric quintal = 100 kg).
TONNES_TO_QUINTALS = 10.0

# (MetricMainCategoryName, MetricSubCategoryName) -> (series key, expected UOMName, multiplier)
SERIES_SELECTORS: dict[tuple[str, str], tuple[str, str, float]] = {
    ("Arrivals", "Mandi Arrivals"): ("arrival", "Tonnes", TONNES_TO_QUINTALS),
    ("Prices", "Mandi Prices"): ("mandi_modal", "Rs/Qtl", 1.0),
    ("Prices", "Retail Prices"): ("retail", "Rs/Qtl", 1.0),
    ("Prices", "Wholesale Prices"): ("wholesale", "Rs/Qtl", 1.0),
}

# Values verified against the ingested SQLite/CSV history on 2026-10-28 (post-conversion).
# A mismatch raises loudly: it means the source remapped metric IDs or changed units.
ANCHORS: dict[tuple[str, str], dict[str, float]] = {
    ("AP", "2022-01-01"): {
        "arrival": 10.0, "mandi_modal": 2500.0, "retail": 4000.0, "wholesale": 3075.0,
    },
    ("KA", "2022-01-01"): {
        "arrival": 30470.0, "mandi_modal": 2472.34, "retail": 3858.33, "wholesale": 3283.33,
    },
    ("MH", "2022-01-01"): {
        "arrival": 232750.0, "mandi_modal": 1987.64, "retail": 3020.0, "wholesale": 2287.5,
    },
    ("MP", "2022-01-01"): {
        "arrival": 64795.3, "mandi_modal": 1120.21, "retail": 2600.0, "wholesale": 2027.73,
    },
    ("MP", "2023-06-18"): {
        "arrival": 12193.5, "mandi_modal": 561.4, "retail": 1611.76, "wholesale": 1205.45,
    },
}


def parse_calendar_day(raw: Any) -> dt.date:
    """Parse "01 Jan 2022" -> date(2022, 1, 1) using the explicit month map."""
    if raw is None:
        raise ValueError("Empty CalendarDay.")
    parts = str(raw).strip().split()
    if len(parts) != 3:
        raise ValueError(f"CalendarDay {str(raw)!r} is not 'DD Mon YYYY'.")
    day_text, month_text, year_text = parts
    month = MONTH_NUMBERS.get(month_text[:3].capitalize())
    if month is None:
        raise ValueError(f"Unrecognized month name in CalendarDay {str(raw)!r}.")
    try:
        return dt.date(int(year_text), month, int(day_text))
    except ValueError as exc:
        raise ValueError(f"Invalid CalendarDay {str(raw)!r}: {exc}") from exc


def _series_column(state_code: str, series: str) -> str:
    if series == "arrival":
        return QUANTITY_COLUMNS[state_code]["arrival"]
    return PRICE_COLUMNS[state_code][series]


def extract_series(rows: list[dict[str, Any]], state_code: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Filter + map + convert rows for one state; returns (frame, meta).

    The frame is indexed by ISO date strings and holds one column per series the
    source reported (canonical names from history_store). Missing series on a
    date is normal: mandi/arrival reporting skips days (state-dependent), so
    absence is never an error.
    """
    if state_code not in STATE_CONFIG:
        raise ValueError(f"Unknown state code {state_code!r}; allowed: {sorted(STATE_CONFIG)}")
    state_name, source_state_id = STATE_CONFIG[state_code]
    state_id = str(source_state_id)

    unknown_selectors: set[tuple[str, str, str]] = set()
    uom_mismatches: list[tuple[str, str, str, str]] = []
    skipped_other_state = 0
    skipped_other_crop = 0
    skipped_malformed = 0
    duplicate_series_rows = 0
    collected: dict[str, dict[str, float]] = {}
    series_stats: dict[str, dict[str, Any]] = {}

    for row in rows:
        stateid = str(row.get("stateid", "") or "")
        if stateid != state_id:
            skipped_other_state += 1
            continue
        if str(row.get("Crop") or "") != "Onion":
            skipped_other_crop += 1
            continue
        main = row.get("MetricMainCategoryName")
        sub = row.get("MetricSubCategoryName")
        uom = str(row.get("UOMName") or "")
        selector = SERIES_SELECTORS.get((str(main or ""), str(sub or "")))
        if selector is None:
            unknown_selectors.add((str(main or ""), str(sub or ""), uom))
            continue
        series, expected_uom, multiplier = selector
        if uom != expected_uom:
            uom_mismatches.append((str(main), str(sub), uom, expected_uom))
            continue
        raw_day = row.get("CalendarDay")
        raw_value = row.get("Value")
        if raw_day is None or raw_value is None:
            skipped_malformed += 1
            continue
        try:
            day = parse_calendar_day(raw_day)
            numeric = float(raw_value)
        except (TypeError, ValueError):
            skipped_malformed += 1
            continue
        if not math.isfinite(numeric) or not math.isfinite(numeric * multiplier):
            skipped_malformed += 1
            continue
        if (str(main), str(sub), uom) not in unknown_selectors and series not in series_stats:
            series_stats[series] = {"rows": 0, "first": day.isoformat(), "last": day.isoformat()}
        converted = numeric * multiplier
        iso_day = day.isoformat()
        slot = collected.setdefault(iso_day, {})
        if series in slot:
            duplicate_series_rows += 1  # keep the last occurrence
        slot[series] = converted
        stats = series_stats[series]
        stats["rows"] += 1
        if day.isoformat() < stats["first"]:
            stats["first"] = day.isoformat()
        if day.isoformat() > stats["last"]:
            stats["last"] = day.isoformat()

    if unknown_selectors or uom_mismatches:
        details = "; ".join(f"Main={m!r} Sub={s!r} UOM={u!r}" for m, s, u in sorted(unknown_selectors))
        detail_uom = "; ".join(f"Main={m!r} Sub={s!r} UOM got {got!r} expected {exp!r}"
                               for m, s, got, exp in uom_mismatches)
        raise RuntimeError(
            f"UPAg Dash schema drift for {state_code}. Unknown selectors: {details or 'none'}. "
            f"UOM mismatches: {detail_uom or 'none'} (expected prices Rs/Qtl, arrivals Tonnes)."
        )
    if skipped_malformed:
        logger.warning("State %s: skipped %d malformed row(s).", state_code, skipped_malformed)

    dates = sorted(collected)
    records = [
        {"date": d, **{_series_column(state_code, series): value for series, value in collected[d].items()}}
        for d in dates
    ]
    frame = pd.DataFrame(records).set_index("date") if dates else pd.DataFrame(index=pd.Index([], name="date"))
    meta: dict[str, Any] = {
        "state": state_code,
        "state_name": state_name,
        "rows_seen": len(rows),
        "rows_used": sum(stats["rows"] for stats in series_stats.values()),
        "dates_with_data": dates,
        "series": series_stats,
        "skipped_other_state": skipped_other_state,
        "skipped_other_crop": skipped_other_crop,
        "skipped_malformed": skipped_malformed,
        "duplicate_series_rows": duplicate_series_rows,
    }
    return frame, meta


def assert_anchors(frame: pd.DataFrame, state_code: str, tolerance: float = 1e-6) -> None:
    """Fail loudly if an anchor date present in the window drifts from the verified value.

    Anchor dates outside the window are skipped silently; a series missing on an
    anchor date is also acceptable (sparse mandi reporting).
    """
    if frame.empty:
        return
    for (anchor_state, anchor_date), series_values in sorted(ANCHORS.items()):
        if anchor_state != state_code or anchor_date not in set(frame.index.astype(str)):
            continue
        for series, expected in sorted(series_values.items()):
            column = _series_column(state_code, series)
            if column not in frame.columns:
                continue
            got = frame.loc[anchor_date, column]
            got_f = float(got) if not pd.isna(got) else None
            if got_f is None:
                continue
            if abs(got_f - expected) > tolerance:
                raise RuntimeError(
                    f"UPAg anchor mismatch for {state_code} {anchor_date} series {series!r} "
                    f"(column {column}): got {got_f!r}, expected {expected!r}. The source likely "
                    f"remapped metric IDs or changed units; ingestion must not continue."
                )
