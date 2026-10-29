"""HTTP client for the UPAg Dash MIP `mip-by-crop-tab` dashboard endpoint.

The endpoint is a public Plotly Dash callback (POST `_dash-update-component`); no
credentials are involved. The header set below must match the browser capture that
was verified against live responses (2026-10-28), because the server treats the
_callback_ as part of the request contract. Series metric IDs are pinned at capture
time (`ONION_METRIC_IDS`) so the requested series cannot silently change.

Verified request/output contract (see `tests/fixtures/mip_response_<STATE>.json`):
- Retail Price (DOCA)          -> Prices / Retail Prices,    Rs/Qtl, no conversion
- Wholesale Price (DOCA)       -> Prices / Wholesale Prices, Rs/Qtl, no conversion
- Mandi Modal Price (AgMarknet)-> Prices / Mandi Prices,     Rs/Qtl, no conversion
- Mandi Arrival Quantity       -> Arrivals / Mandi Arrivals, Tonnes, x10 -> metric quintals
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from copy import deepcopy
from typing import Any

import requests

logger = logging.getLogger("onion-dash-client")

DASH_URL = "https://dash.upag.gov.in/_dash-update-component"

# Referer/UA/X-Requested-With are part of the verified request contract; do not change them.
PAGE_REFERER = "https://dash.upag.gov.in/dash-reports/mip/mip-by-crop-tab?rtab=Analytics&rtype=dashboards"
REQUEST_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": PAGE_REFERER,
    "X-Requested-With": "XMLHttpRequest",
}

# state_code -> (display state name, UPAg StateCode)
STATE_CONFIG: dict[str, tuple[str, int]] = {
    "AP": ("Andhra Pradesh", 28),
    "MH": ("Maharashtra", 27),
    "KA": ("Karnataka", 29),
    "MP": ("Madhya Pradesh", 23),
}

# Pinned filter values from the verified capture; do not "improve" them.
ONION_COMMODITY_ID = 9
ONION_CROP_ID = 22
# Metric IDs: 10 retail price, 13 mandi modal price, 9 mandi arrival quantity, 8 wholesale price.
ONION_METRIC_IDS = [10, 13, 9, 8]

DEFAULT_TIMEOUT = 60
# Delay (seconds) after each failed attempt, before retries 1..3 (4 attempts total).
RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0, 45.0)


def _base_filters() -> dict[str, Any]:
    """Filter blocks exactly as captured from the dashboard.

    The "frequncy" misspelling is part of the server contract; do not correct it.
    MONTH/YEAR fields are placeholders filled by build_payload().
    """
    return {
        "prices": {
            "frequncy": "Daily",
            "from": "",
            "to": "",
            "monthfrom": 1,
            "yearfrom": 2022,
            "monthto": 10,
            "yearto": 2026,
            "filter": ["By Metric"],
            "commodity_cropfilter": [ONION_CROP_ID],
            "metric_category_cropfilter": 3,
            "metric_cropfilter": 13,
            "commodity": ONION_COMMODITY_ID,
            "metric_category": [3, 2],
            "metric": ONION_METRIC_IDS,
        },
        "correlation": {
            "from": "",
            "to": "",
            "location": 999999,
            "filter": "By Metric",
            "commodity": 1,
            "metric": [2, 13],
            "commodity_cropfilter": [1, 22],
            "metric_cropfilter": 2,
        },
        "trade": {
            "export_import": "Export",
            "insights_export_import": "Export",
            "price_quantity": "Quantity",
            "monthly_yearly": "Monthly",
            "datefrom_month": "",
            "datefrom_year": "",
            "dateto_month": "",
            "dateto_year": "",
            "filter": "By Country",
            "top": 0,
            "country_multi": [9999],
            "country_single": 9999,
            "cropcategory": "Foodgrains",
            "commodity_tradecomp_multi": ["Rice", "Wheat"],
            "commodity_multi": ["Rice", "Wheat"],
            "commodity_single": "Wheat",
            "uom": "Thousand",
            "tradecomparision_filter": "quantity",
            "tradecomparision_uom": "Thousand",
            "tradecomparision_year": "",
            "tradecomparision_from_month": 1,
            "tradecomparision_to_month": 12,
        },
    }


def _base_payload() -> dict[str, Any]:
    return {
        "output": (
            "..mip-bycrop-line-graph.figure...mip-bycrop-store.data"
            "...mip-bycrop-grid-title.children...mip-source.children.."
        ),
        "outputs": [
            {"id": "mip-bycrop-line-graph", "property": "figure"},
            {"id": "mip-bycrop-store", "property": "data"},
            {"id": "mip-bycrop-grid-title", "property": "children"},
            {"id": "mip-source", "property": "children"},
        ],
        "inputs": [
            {"id": "mip-header-tabs", "property": "value", "value": "mip-by-crop-tab"},
            {"id": "mip-bycrop-right-tab", "property": "value", "value": "data"},
            {"id": "mip-bycrop-location-filter-aggrid", "property": "selectedRows", "value": []},
            {"id": "mip-filters-store", "property": "data", "value": _base_filters()},
        ],
        "changedPropIds": ["mip-filters-store.data"],
        "state": [
            {
                "id": "url",
                "property": "search",
                "value": "?t=&active=mip-by-crop-tab&%20stateID=0&rtab=Analytics&rtype=dashboards",
            }
        ],
    }


def new_session() -> requests.Session:
    """Session pre-loaded with the exact captured headers."""
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    return session


def _input_by_id(payload: dict[str, Any], input_id: str) -> dict[str, Any]:
    for item in payload["inputs"]:
        if item.get("id") == input_id:
            return item
    raise KeyError(f"Payload template is missing input id {input_id!r}.")


def build_payload(state_code: str, from_iso: str, to_iso: str) -> dict[str, Any]:
    """One state + date window = one verified request body.

    MONTH/YEAR filter fields are always derived from the ISO dates; captured
    browser payloads contained internally inconsistent month fields (e.g.
    from=2022-10-28 with monthfrom=11), so they are never copied.
    """
    if state_code not in STATE_CONFIG:
        raise ValueError(f"Unknown state code {state_code!r}; allowed: {sorted(STATE_CONFIG)}")
    try:
        from_date = dt.date.fromisoformat(from_iso)
        to_date = dt.date.fromisoformat(to_iso)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Dates must be ISO YYYY-MM-DD strings: {exc}") from exc
    if from_date > to_date:
        raise ValueError(f"from date {from_iso} is after to date {to_iso}.")

    state_name, state_id = STATE_CONFIG[state_code]
    payload = deepcopy(_base_payload())
    _input_by_id(payload, "mip-bycrop-location-filter-aggrid")["value"] = [
        {"State": state_name, "StateCode": state_id}
    ]
    prices = _input_by_id(payload, "mip-filters-store")["value"]["prices"]
    prices["from"] = from_date.isoformat()
    prices["to"] = to_date.isoformat()
    prices["monthfrom"] = from_date.month
    prices["yearfrom"] = from_date.year
    prices["monthto"] = to_date.month
    prices["yearto"] = to_date.year
    return payload


def _store_rows(body: Any) -> list[dict[str, Any]] | None:
    if not isinstance(body, dict):
        return None
    response = body.get("response")
    if not isinstance(response, dict):
        return None
    store = response.get("mip-bycrop-store")
    if not isinstance(store, dict):
        return None
    data = store.get("data")
    # An empty list is valid (the source simply has no rows for the window).
    if not isinstance(data, list):
        return None
    return [row for row in data if isinstance(row, dict)]


def fetch_state(
    state_code: str,
    from_iso: str,
    to_iso: str,
    session: requests.Session | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retry_delays: tuple[float, ...] = RETRY_DELAYS,
) -> list[dict[str, Any]]:
    """POST the verified payload for one state and return mip-bycrop-store.data rows.

    Retries connection errors, non-200 responses, non-JSON bodies and responses
    without the store block, then raises a descriptive RuntimeError.
    """
    payload = build_payload(state_code, from_iso, to_iso)
    own_session = session is None
    requests_session = session if session is not None else new_session()
    failures: list[str] = []
    try:
        attempts = len(retry_delays) + 1
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                delay = retry_delays[min(attempt - 2, len(retry_delays) - 1)]
                logger.warning("UPAg Dash retry %d/%d for %s in %.0fs", attempt - 1, len(retry_delays), state_code, delay)
                time.sleep(delay)
            try:
                response = requests_session.post(DASH_URL, json=payload, timeout=timeout)
            except requests.RequestException as exc:
                failures.append(f"attempt {attempt}: network error: {exc}")
                continue
            if response.status_code != 200:
                failures.append(f"attempt {attempt}: HTTP {response.status_code}: {response.text[:200]}")
                continue
            try:
                body = response.json()
            except ValueError as exc:
                failures.append(f"attempt {attempt}: response is not JSON: {exc}")
                continue
            rows = _store_rows(body)
            if rows is None:
                failures.append("attempt %s: response has no mip-bycrop-store.data list" % attempt)
                continue
            return rows
        raise RuntimeError(
            f"UPAg Dash fetch failed for {state_code} ({from_iso}..{to_iso}) after "
            f"{attempts} attempt(s). Causes: {'; '.join(failures)}"
        )
    finally:
        if own_session:
            requests_session.close()
