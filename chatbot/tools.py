"""Read-only tool layer for the chatbot (Architecture A).

Each tool wraps EXISTING project code — the Graph 1/2/3 handlers in main.py
(plain functions), history loaders and status helpers. No new numerical logic:
the chatbot calls the same functions the dashboard and production frontend use.

Executed in-process (no HTTP loopback) so `_PREDICTION_CACHE`, dataset mtime
sync short-circuits and validation all apply unchanged.

`execute_tool` returns a ToolOutcome with TWO views of the SAME result:
- payload   : the full API-contract object (attached to the chat response so
              the UI can draw the real chart behind a toggle)
- compact   : a token-guarded summary (grouped stats + sample rows + warnings)
              that is shown to the Groq model so it narrates without 25KB dumps.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import history_store
from history_store import STATE_CODES, STATE_NAMES

GRAPH_CODES = {
    "ap_price_forecast",
    "state_price_comparison",
    "supply_demand_shortfall",
}
_SAVED_GRAPH_CODES = tuple(sorted(GRAPH_CODES))

_SAMPLE_HEAD = 5
_SAMPLE_TAIL = 3
_MAX_SAMPLE_ROWS = 8


# ---------------------------------------------------------------------------
# OpenAI/Groq tool schemas (the LLM's "phone list")
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "forecast_ap_prices",
            "description": (
                "Graph 1: forecast Andhra Pradesh (AP) daily onion retail, wholesale and/or "
                "mandi modal prices from today through end_date. Returns the predicted price, lower bound, "
                "upper bound and confidence score per date in ₹/kg, plus change vs latest actual. "
                "Use it for single-state AP price questions."),
            "parameters": {
                "type": "object",
                "properties": {
                    "price_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["retail", "wholesale", "mandi_modal"]},
                        "description": "Which AP price series to forecast. Default: retail.",
                        "minItems": 1,
                    },
                    "start_date": {"type": "string", "description": "Display range start, YYYY-MM-DD."},
                    "end_date": {"type": "string", "description": "Forecast through date (max 180 days past last observation), YYYY-MM-DD."},
                    "threshold_rs_kg": {"type": "number", "description": "Optional price threshold line in ₹/kg (default 44)."},
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forecast_state_comparison",
            "description": (
                "Graph 2: forecast onion retail/wholesale/mandi modal prices for any subset of the four "
                "states AP, KA, MH, MP — each state independently. Same predicted price, lower bound, "
                "upper bound and confidence score per-date output shape in ₹/kg as Graph 1. "
                "Use for multi-state comparisons."),
            "parameters": {
                "type": "object",
                "properties": {
                    "states": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(STATE_CODES)},
                        "description": "States to include.",
                        "minItems": 1,
                    },
                    "price_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["retail", "wholesale", "mandi_modal"]},
                        "description": "Price series to include. Default: retail, wholesale, mandi_modal.",
                        "minItems": 1,
                    },
                    "start_date": {"type": "string", "description": "Display range start, YYYY-MM-DD."},
                    "end_date": {"type": "string", "description": "Forecast through date, YYYY-MM-DD."},
                    "threshold_rs_kg": {"type": "number", "description": "Optional threshold line in ₹/kg (default 44)."},
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forecast_supply_balance",
            "description": (
                "Graph 3: AP-ONLY monthly production, consumption and arrivals forecast in tonnes/month plus the "
                "derived shortfall = consumption - (arrival + production). For non-AP states this dataset does not "
                "exist (the handler rejects them). Use for supply/demand shortfall questions."),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Display range start, YYYY-MM-DD."},
                    "end_date": {"type": "string", "description": "Forecast through date, YYYY-MM-DD."},
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "factors",
            "description": (
                "Explain in simple, concise, non-technical language which historical data the forecast "
                "uses as context and what those signals can suggest. Use for questions about what influences "
                "prices, why a forecast moves, or which factors are included. This tool does not identify "
                "the cause of a particular price change. Clearly separate model inputs from external factors "
                "that are not included as separate inputs."),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": ["all", "retail", "wholesale", "mandi_modal", "supply_balance"],
                        "description": "Which forecast to explain. Default: all.",
                    },
                    "state": {
                        "type": "string",
                        "enum": list(STATE_CODES),
                        "description": "State for price forecasts. Default: AP. Supply balance is AP only.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_price_history",
            "description": (
                "Actual (non-forecast) stored history. Daily: per-state retail/wholesale/mandi prices in ₹/kg. "
                "Monthly: AP production, consumption and arrivals in tonnes/month. Use for 'what was price X on date Y' questions."),
            "parameters": {
                "type": "object",
                "properties": {
                    "frequency": {"type": "string", "enum": ["daily", "monthly"], "description": "daily (default) or monthly (AP quantities)."},
                    "state": {"type": "string", "enum": list(STATE_CODES), "description": "Daily frequency only. Default AP."},
                    "price_type": {"type": "string", "enum": ["retail", "wholesale", "mandi_modal"], "description": "Daily frequency only. Default retail."},
                    "start_date": {"type": "string", "description": "History window start, YYYY-MM-DD."},
                    "end_date": {"type": "string", "description": "History window end, YYYY-MM-DD."},
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "data_freshness",
            "description": (
                "Check the latest observation date, data age, forecast start date, and warnings. "
                "Use whenever 'is the data up to date?' matters."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_saved_forecast",
            "description": (
                "Fetch the latest forecast run persisted in SQLite (by Graph 1/2/3 calls) with its saved "
                "predicted values, lower bounds and upper bounds. "
                "Use for 'repeat the last saved forecast' style questions."),
            "parameters": {
                "type": "object",
                "properties": {
                    "graph_code": {
                        "type": "string",
                        "enum": _SAVED_GRAPH_CODES,
                        "description": "Omit for the latest run of any graph.",
                    },
                    "limit": {"type": "integer", "description": "How many saved values to list (default 20, max 40)."},
                },
            },
        },
    },
]

_TOOL_NAMES = {schema["function"]["name"] for schema in TOOL_SCHEMAS}


# ---------------------------------------------------------------------------
# Outcome container
# ---------------------------------------------------------------------------

@dataclass
class ToolOutcome:
    name: str
    arguments: dict[str, Any]
    payload: dict[str, Any] | None = None
    compact: dict[str, Any] | None = None
    error: str | None = None
    tools_exceptions: list[str] = field(default_factory=list)

    @property
    def is_graph(self) -> bool:
        return bool(self.payload and self.payload.get("graph") in GRAPH_CODES)

    @property
    def chart_kind(self) -> str | None:
        return self.payload.get("graph") if self.is_graph else None


def _bad_args(name: str, message: str) -> ToolOutcome:
    return ToolOutcome(name=name, arguments={}, error=message)


def _parse_date(raw: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD: {raw!r}") from exc


def _join(values: Any, allowed: set[str] | tuple[str, ...], label: str, default: str,
          transform=None) -> str:
    if values is None:
        return default
    if transform is None:
        transform = lambda s: s.strip().lower()  # noqa: E731  (price types are lowercase)
    if isinstance(values, str):
        values = [v for v in values.split(",") if v.strip()]
    cleaned = []
    for value in values:
        check = transform(str(value))
        if check not in allowed:
            raise ValueError(f"Unsupported {label}: {value!r}. Allowed: {sorted(allowed)}")
        cleaned.append(check)
    if not cleaned:
        raise ValueError(f"Select at least one {label}.")
    return ",".join(dict.fromkeys(cleaned))


def _optional_number(args: dict[str, Any], key: str) -> float | None:
    raw = args.get(key)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number: {raw!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{key} must be a non-negative number: {raw!r}")
    return value


# ---------------------------------------------------------------------------
# Compaction helpers (token guard): summarize rows per series/group
# ---------------------------------------------------------------------------

def _rows_in_range(rows: list[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
    selected = [
        row for row in rows
        if start.isoformat() <= str(row.get("date", ""))[:10] <= end.isoformat()
    ]
    return selected or []


def _group_key(row: dict[str, Any]) -> tuple[str, ...]:
    parts = (row.get("state"), row.get("price_type") or row.get("metric"))
    return tuple(str(part) for part in parts if part is not None)


def _value_key(rows: list[dict[str, Any]], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if rows and candidate in rows[0]:
            return candidate
    return None


def _sample_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First N + last M rows (sorted by date), deduplicated."""
    ordered = sorted(rows, key=lambda r: str(r.get("date", "")))
    head = ordered[:_SAMPLE_HEAD]
    tail = ordered[-_SAMPLE_TAIL:] if len(ordered) > _SAMPLE_HEAD else []
    seen, sample = set(), []
    for row in [*head, *tail]:
        key = row.get("date")
        if key not in seen:
            seen.add(key)
            sample.append(row)
    return sample


_CHAT_FORECAST_FIELD_NAMES = {
    "p10": "lower_bound",
    "p50": "predicted_value",
    "p90": "upper_bound",
    "p10_rs_kg": "lower_bound_rs_kg",
    "p50_rs_kg": "predicted_price_rs_kg",
    "p90_rs_kg": "upper_bound_rs_kg",
    "p10_tonnes_month": "lower_bound_tonnes_month",
    "p50_tonnes_month": "predicted_quantity_tonnes_month",
    "p90_tonnes_month": "upper_bound_tonnes_month",
    "p10_mq_day": "lower_bound_mq_day",
    "p50_mq_day": "predicted_quantity_mq_day",
    "p90_mq_day": "upper_bound_mq_day",
    "shortfall_p10_tonnes_month": "lower_bound_shortfall_tonnes_month",
    "shortfall_p50_tonnes_month": "predicted_shortfall_tonnes_month",
    "shortfall_p90_tonnes_month": "upper_bound_shortfall_tonnes_month",
    "shortfall_p10_metric_quintal_day": "lower_bound_shortfall_metric_quintal_day",
    "shortfall_p50_metric_quintal_day": "predicted_shortfall_metric_quintal_day",
    "shortfall_p90_metric_quintal_day": "upper_bound_shortfall_metric_quintal_day",
}


def _chat_forecast_row(row: dict[str, Any]) -> dict[str, Any]:
    """Present internal forecast fields to the assistant in plain language."""
    return {_CHAT_FORECAST_FIELD_NAMES.get(key, key): value for key, value in row.items()}


def _summarize_series(rows: list[dict[str, Any]], value_candidates: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_group_key(row), []).append(row)

    summaries = []
    for key, group_rows in sorted(groups.items()):
        ordered = sorted(group_rows, key=lambda r: str(r.get("date", "")))
        value_key = _value_key(ordered, value_candidates)
        values = [float(row[value_key]) for row in ordered if value_key and row.get(value_key) is not None]
        state, series = (key + ("<none>",))[:2]
        summary: dict[str, Any] = {
            "state": state,
            "series": series,
            "rows": len(ordered),
            "first_date": ordered[0].get("date") if ordered else None,
            "last_date": ordered[-1].get("date") if ordered else None,
        }
        if value_key and values:
            summary["unit_value_key"] = _CHAT_FORECAST_FIELD_NAMES.get(value_key, value_key)
            summary["first"] = round(values[0], 4)
            summary["last"] = round(values[-1], 4)
            summary["min"] = round(min(values), 4)
            summary["max"] = round(max(values), 4)
            summary["avg"] = round(sum(values) / len(values), 4)
            summary["trend"] = round(values[-1] - values[0], 4)
        for extra in ("confidence_score", "predicted_price_rs_kg", "p50_rs_kg",
                      "p10_rs_kg", "p90_rs_kg"):
            if extra in (ordered[0] if ordered else {}):
                display_key = _CHAT_FORECAST_FIELD_NAMES.get(extra, extra)
                summary.setdefault("first_row", {})[display_key] = ordered[0][extra]
        sample = _sample_rows(ordered)
        if sample:
            summary["sample_rows"] = [_chat_forecast_row(row) for row in sample]
        summaries.append(summary)
    return summaries


_HISTORY_VALUE_KEYS = ("actual_price_rs_kg", "actual_quantity_tonnes_month")
_FORECAST_VALUE_KEYS = ("p50_rs_kg", "predicted_quantity_tonnes_month")
_GRAPH_META_KEYS = (
    "graph", "title", "unit", "threshold_rs_kg", "horizon_days",
    "last_observation_date", "data_staleness_days", "actual_end_date",
    "requested_end_date", "forecast_limit_date", "forecast_truncated",
    "forecast_start_date", "forecast_end_date", "balance_formula",
    "quantity_unit", "selected_states", "selected_state_names",
    "selected_price_types", "price_types",
)


def compact_graph_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compact = {key: payload.get(key) for key in _GRAPH_META_KEYS if payload.get(key) is not None}
    compact["history_summary"] = _summarize_series(payload.get("historical") or [], _HISTORY_VALUE_KEYS)
    compact["forecast_summary"] = _summarize_series(payload.get("forecast") or [], _FORECAST_VALUE_KEYS)
    quantity_forecast = payload.get("quantity_forecast") or []
    if quantity_forecast:
        compact["quantity_forecast_summary"] = _summarize_series(
            quantity_forecast, _FORECAST_VALUE_KEYS)
    balance = payload.get("balance_forecast") or []
    if balance:
        compact["balance_forecast"] = [_chat_forecast_row(row) for row in balance]
    compact["data_verify_note"] = (
        "Quote only numbers present above. Forecast rows use predicted values, lower bounds, "
        "upper bounds and confidence scores when available."
    )
    return compact


def compact_payload(payload: dict[str, Any], graph_like: bool) -> dict[str, Any]:
    if graph_like:
        return compact_graph_payload(payload)
    return payload


# ---------------------------------------------------------------------------
# Tool implementations (thin wrappers over EXISTING project code)
# ---------------------------------------------------------------------------

def _callable_kwargs(FN, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Resolve FastAPI Query-defaults to their plain values.

    Graph handlers are plain functions but their defaults are `fastapi.params.Query`
    instances (for the HTTP layer). Called in-process, an unpassed optional argument
    would otherwise leak the Query OBJECT into the payload and crash handlers that
    do `.split()` on it. This mirrors what FastAPI does at request time.
    """
    import inspect

    try:
        from fastapi import params as fastapi_params
        query_types = (fastapi_params.Query,)
    except Exception:  # pragma: no cover - FastAPI always present here
        query_types = ()

    resolved: dict[str, Any] = {}
    for name, param in inspect.signature(FN).parameters.items():
        if name in kwargs:
            resolved[name] = kwargs[name]
            continue
        default = param.default
        if query_types and isinstance(default, query_types):
            resolved[name] = getattr(default, "default", None)
    return resolved


def _run(FN, name: str, /, **kwargs) -> ToolOutcome:
    """Call an existing graph handler, converting FastAPI errors to chat-safe errors."""
    try:
        payload = FN(**_callable_kwargs(FN, kwargs))
    except Exception as exc:  # includes fastapi.HTTPException(422/500)
        detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
        return ToolOutcome(name=name, arguments=kwargs, error=str(detail))
    return ToolOutcome(
        name=name,
        arguments=kwargs,
        payload=payload,
        compact=compact_graph_payload(payload),
    )


def tool_forecast_ap_prices(args: dict[str, Any]) -> ToolOutcome:
    from main import ap_retail_graph

    try:
        kwargs = dict(
            price_types=_join(args.get("price_types"), {"retail", "wholesale", "mandi_modal"},
                              "price types", "retail"),
            start_date=_parse_date(args.get("start_date"), "start_date"),
            end_date=_parse_date(args.get("end_date"), "end_date"),
        )
        threshold = _optional_number(args, "threshold_rs_kg")
        if threshold is not None:
            kwargs["threshold_rs_kg"] = threshold
    except ValueError as exc:
        return _bad_args("forecast_ap_prices", str(exc))
    return _run(ap_retail_graph, "forecast_ap_prices", **kwargs)


def tool_forecast_state_comparison(args: dict[str, Any]) -> ToolOutcome:
    from main import state_comparison_graph

    try:
        kwargs = dict(
            states=_join(args.get("states"), set(STATE_CODES), "states", "AP,KA,MH,MP",
                         transform=lambda s: s.strip().upper()),
            price_types=_join(args.get("price_types"), {"retail", "wholesale", "mandi_modal"},
                              "price types", "retail,wholesale,mandi_modal"),
            start_date=_parse_date(args.get("start_date"), "start_date"),
            end_date=_parse_date(args.get("end_date"), "end_date"),
        )
        threshold = _optional_number(args, "threshold_rs_kg")
        if threshold is not None:
            kwargs["threshold_rs_kg"] = threshold
    except ValueError as exc:
        return _bad_args("forecast_state_comparison", str(exc))
    return _run(state_comparison_graph, "forecast_state_comparison", **kwargs)


def tool_forecast_supply_balance(args: dict[str, Any]) -> ToolOutcome:
    from main import supply_balance_graph

    try:
        kwargs = dict(
            start_date=_parse_date(args.get("start_date"), "start_date"),
            end_date=_parse_date(args.get("end_date"), "end_date"),
        )
    except ValueError as exc:
        return _bad_args("forecast_supply_balance", str(exc))
    return _run(supply_balance_graph, "forecast_supply_balance", **kwargs)


def tool_get_price_history(args: dict[str, Any]) -> ToolOutcome:
    import main as api
    from main import _history_price_records

    try:
        start = _parse_date(args.get("start_date"), "start_date")
        end = _parse_date(args.get("end_date"), "end_date")
        if start > end:
            raise ValueError("start_date must be on or before end_date.")
        frequency = str(args.get("frequency") or "daily").lower()
        if frequency not in {"daily", "monthly"}:
            raise ValueError(f"frequency must be daily or monthly: {frequency!r}")
    except ValueError as exc:
        return _bad_args("get_price_history", str(exc))

    if frequency == "daily":
        try:
            state = _join(args.get("state"), set(STATE_CODES), "state", "AP",
                          transform=lambda s: s.strip().upper())
            price_type = _join(args.get("price_type"), {"retail", "wholesale", "mandi_modal"},
                               "price type", "retail")
        except ValueError as exc:
            return _bad_args("get_price_history", str(exc))
        try:
            data = api.get_data()
            rows = _history_price_records(data, state.strip().upper(), price_type, start, end)
        except Exception as exc:
            return ToolOutcome(name="get_price_history", arguments=args,
                               error=f"{type(exc).__name__}: {exc}")
        payload = {"frequency": "daily", "state": state.strip().upper(), "price_type": price_type,
                   "start_date": start.isoformat(), "end_date": end.isoformat(), "rows": rows}
        compact = _compact_history_rows(rows, start, end,
                                        ("state", "price_type"), value_keys=_HISTORY_VALUE_KEYS)
    else:
        try:
            monthly = history_store.load_ap_monthly_history()
        except Exception as exc:
            return ToolOutcome(name="get_price_history", arguments=args,
                               error=f"{type(exc).__name__}: {exc}")
        rows = []
        mask = (monthly["month_start"].dt.date >= start) & (monthly["month_start"].dt.date <= end)
        for _, month_row in monthly.loc[mask].sort_values("month_start").iterrows():
            stamp = pd_month_iso(month_row["month_start"])
            for metric, column in (("production", "production_t_month"),
                                   ("consumption", "consumption_t_month"),
                                   ("arrival", "arrival_t_month")):
                rows.append({"date": stamp, "state": "AP", "metric": metric,
                             "actual_quantity_tonnes_month": round(float(month_row[column]), 4),
                             "unit": "tonnes/month"})
        payload = {"frequency": "monthly", "state": "AP",
                   "start_date": start.isoformat(), "end_date": end.isoformat(), "rows": rows}
        compact = _compact_history_rows(rows, start, end,
                                        ("state", "metric"), value_keys=_HISTORY_VALUE_KEYS)
    return ToolOutcome(name="get_price_history", arguments=args, payload=payload, compact=compact)


def tool_factors(args: dict[str, Any]) -> ToolOutcome:
    """Describe the real forecast inputs without claiming causal attribution."""
    target = str(args.get("target") or "all").strip().lower()
    targets = {"retail", "wholesale", "mandi_modal", "supply_balance"}
    if target != "all" and target not in targets:
        return _bad_args("factors", f"Unsupported target: {target!r}. Allowed: {sorted(targets | {'all'})}")

    state = str(args.get("state") or "AP").strip().upper()
    if state not in STATE_CODES:
        return _bad_args("factors", f"Unsupported state: {state!r}. Allowed: {list(STATE_CODES)}")
    if target == "supply_balance" and state != "AP":
        return _bad_args("factors", "The monthly supply-balance forecast is available for AP only.")

    from main import _price_context_columns

    price_names = {
        "retail": "retail prices",
        "wholesale": "wholesale prices",
        "mandi_modal": "mandi modal prices",
    }
    relationship_notes = {
        "wholesale": (
            "Wholesale prices can signal broader market movement that may also reach retail; "
            "the model does not measure a guaranteed pass-through."
        ),
        "mandi_modal": (
            "Mandi modal prices show market trading levels and can provide context for retail movement; "
            "the model does not measure a guaranteed effect."
        ),
        "arrival": (
            "Lower arrivals can coincide with upward price pressure, while higher arrivals can ease it; "
            "this is context, not proof of cause."
        ),
    }
    selected_targets = (
        list(price_names) if target == "all"
        else [] if target == "supply_balance"
        else [target]
    )
    factors_by_target: list[dict[str, Any]] = []

    for price_type in selected_targets:
        price_column = history_store.PRICE_COLUMNS[state][price_type]
        arrival_column = history_store.QUANTITY_COLUMNS[state]["arrival"]
        factors = []
        for column in _price_context_columns(state, price_type):
            if column == price_column:
                factors.append({
                    "factor": f"Past {state} {price_names[price_type]}",
                    "role": "The price being forecast",
                    "what_it_can_suggest": "Recent values help the model follow the pattern in this same price series.",
                })
                continue
            if column == arrival_column:
                factor_key = "arrival"
                factor_name = f"Past {state} mandi arrivals"
            else:
                factor_key = next(
                    measure for measure in price_names
                    if history_store.PRICE_COLUMNS[state][measure] == column
                )
                factor_name = f"Past {state} {price_names[factor_key]}"
            factors.append({
                "factor": factor_name,
                "role": "Market context",
                "what_it_can_suggest": relationship_notes[factor_key],
            })
        factors_by_target.append({
            "forecast": f"{state} {price_names[price_type]}",
            "model_context": factors,
            "future_context": (
                "For future dates, the model also uses separate forecasts of the listed market-context "
                "series; these are estimates, not known future observations."
            ),
        })

    if target in {"all", "supply_balance"}:
        factors_by_target.append({
            "forecast": "AP monthly supply balance",
            "model_context": [
                {
                    "factor": f"Past AP monthly {metric}",
                    "role": f"Used to forecast {metric} on its own",
                    "what_it_can_suggest": description,
                }
                for metric, description in (
                    ("production", "Earlier production values help the model continue that series' pattern."),
                    ("consumption", "Earlier consumption values help the model continue that series' pattern."),
                    ("arrivals", "Earlier arrival values help the model continue that series' pattern."),
                )
            ],
            "future_context": (
                "Production, consumption, and arrivals are forecast separately; the shortfall or surplus "
                "is then calculated from those three forecasts."
            ),
        })

    payload = {
        "target": target,
        "state": state,
        "forecasts": factors_by_target,
        "not_included_as_separate_inputs": [
            "Weather and storms",
            "Transport or fuel costs",
            "Policy changes or sudden disruptions",
        ],
        "limits": (
            "These inputs provide historical context; they do not prove what caused a particular price "
            "change or measure each factor's effect. External factors are not added as separate model inputs, "
            "though past effects may already appear in the recorded prices or arrivals."
        ),
    }
    return ToolOutcome(name="factors", arguments=args, payload=payload, compact=payload)


def pd_month_iso(timestamp) -> str:
    return f"{timestamp.year:04d}-{timestamp.month:02d}-01"


def _compact_history_rows(rows: list[dict[str, Any]], start: date, end: date,
                          group_keys: tuple[str, ...], value_keys: tuple[str, ...]) -> dict[str, Any]:
    summaries = _summarize_series(rows, value_keys)
    for summary in summaries:
        state_code = summary.get("state")
        if state_code in STATE_NAMES:
            summary["state_name"] = STATE_NAMES[state_code]
    in_range = [r for r in rows if start.isoformat() <= str(r.get("date", ""))[:10] <= end.isoformat()]
    return {"frequency_window": [start.isoformat(), end.isoformat()],
            "rows_total": len(rows), "rows_in_window": len(in_range),
            "history_summary": summaries,
            "data_verify_note": ("Quote only numbers present above; sample_rows show the raw rows for exact values."),
            }


def tool_data_freshness(args: dict[str, Any]) -> ToolOutcome:
    import main as api

    try:
        data = api.get_data()
        status = api._status(data)
    except Exception as exc:
        return ToolOutcome(name="data_freshness", arguments=args, error=f"{type(exc).__name__}: {exc}")

    return ToolOutcome(
        name="data_freshness",
        arguments=args,
        payload=status,
        compact=status,
    )


def tool_get_saved_forecast(args: dict[str, Any]) -> ToolOutcome:
    raw_code = args.get("graph_code")
    graph_code = str(raw_code) if raw_code else None
    if graph_code and graph_code not in GRAPH_CODES:
        return _bad_args("get_saved_forecast",
                         f"Unknown graph_code {graph_code!r}. Allowed: {sorted(GRAPH_CODES)}")
    try:
        limit_raw = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit_raw = 20
    limit = max(1, min(limit_raw, 40))
    try:
        run = history_store.get_latest_forecast(graph_code)
    except Exception as exc:
        return ToolOutcome(name="get_saved_forecast", arguments=args,
                           error=f"{type(exc).__name__}: {exc}")
    if run is None:
        payload = {"saved": False, "message": "No forecasts have been saved yet."}
        return ToolOutcome(name="get_saved_forecast", arguments=args, payload=payload, compact=dict(payload))
    values = run.get("forecast_values") or []
    run_view = {k: run.get(k) for k in ("run_id", "graph_code", "generated_at", "model_id",
                                        "horizon_days", "last_observation_date")}
    compact = {**run_view, "saved_values_count": len(values),
               "values_sample": [_chat_forecast_row(row) for row in values[:limit]]}
    return ToolOutcome(name="get_saved_forecast", arguments=args,
                       payload={**run_view, "forecast_values": values}, compact=compact)


# NOTE (intent clarity): the arguments-dict key order matches the schema so the
# compact JSON shown to the LLM mirrors the tool descriptions.

EXECUTORS = {
    "forecast_ap_prices": tool_forecast_ap_prices,
    "forecast_state_comparison": tool_forecast_state_comparison,
    "forecast_supply_balance": tool_forecast_supply_balance,
    "factors": tool_factors,
    "get_price_history": tool_get_price_history,
    "data_freshness": tool_data_freshness,
    "get_saved_forecast": tool_get_saved_forecast,
}


def execute_tool(name: str, arguments: dict[str, Any]) -> ToolOutcome:
    """Dispatch a (validated) tool call. Unknown tools become errors narrated by the LLM."""
    if name not in _TOOL_NAMES:
        return ToolOutcome(name=name, arguments={},
                           error=f"Unknown tool {name!r}. Allowed: {sorted(_TOOL_NAMES)}")
    call_args = arguments if isinstance(arguments, dict) else {}
    return EXECUTORS[name](call_args)


def tool_message_content(outcome: ToolOutcome) -> str:
    if outcome.error:
        return json.dumps({"error": outcome.error}, ensure_ascii=False)
    return json.dumps(outcome.compact or {}, ensure_ascii=False, default=str)
