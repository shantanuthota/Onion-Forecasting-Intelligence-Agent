"""Offline chatbot tests (Architecture A): scripted fake Groq brain, no network.

Reuses `client_with_fake` from test_api_mocked.py so the chatbot runs against the
same tiny CSV/SQLite fixtures and the FakeChronosPipeline — Graph 1/2/3 handlers
therefore return real payloads while the LLM is fully stubbed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from fastapi.testclient import TestClient

import chatbot.agent as agent
import chatbot.sessions as sessions
import chatbot.tools as tools
import history_store
from test_api_mocked import client_with_fake

AUTH = {"Authorization": "Bearer test-api-token"}

READ_ONLY_TOOL_NAMES = [
    "forecast_ap_prices",
    "forecast_state_comparison",
    "forecast_supply_balance",
    "factors",
    "get_price_history",
    "data_freshness",
    "get_saved_forecast",
]


# ---------------------------------------------------------------------------
# Fake Groq brain (records every turn; scripted steps)
# ---------------------------------------------------------------------------

_CALL_SEQ = {"n": 0}


class _FakeFunction:
    def __init__(self, name: str, arguments: dict):
        self.name = name
        self.arguments = json.dumps(arguments)


class _FakeCall:
    def __init__(self, name: str, arguments: dict):
        _CALL_SEQ["n"] += 1
        self.id = f"call_{_CALL_SEQ['n']}"
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


def scripted_brain(monkeypatch, steps: list, final_text: str = "Understood, all tool data checked."):
    """Patch agent._call_model_with_fallback.

    steps entries:
      - dict with 'tool_calls': [(name, args), ...]
      - str: a scripted final-text response
    Whenever the agent asks for a turn WITHOUT tools (round-cap nudge), the fake
    returns `final_text`. Returns a record dict for assertions."""
    record = {"turns": [], "use_tools": []}
    cursor = {"index": 0}

    def fake_call(client, messages, use_tools):
        record["use_tools"].append(use_tools)
        record["turns"].append(len(messages))
        if not use_tools:
            return _FakeMessage(content=final_text), "fake-primary"
        step = steps[min(cursor["index"], len(steps) - 1)]
        cursor["index"] += 1
        if isinstance(step, dict) and "tool_calls" in step:
            calls = [_FakeCall(name, args) for name, args in step["tool_calls"]]
            return _FakeMessage(content=step.get("content"), tool_calls=calls), "fake-primary"
        text = step if isinstance(step, str) else (step or {}).get("content", "")
        return _FakeMessage(content=text), "fake-primary"

    monkeypatch.setattr(agent, "_call_model_with_fallback", fake_call)
    return record


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("GROQ_ENABLED", "1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_placeholder")
    monkeypatch.setenv("CHAT_RETRY_DELAY_SEC", "0")
    return client_with_fake(monkeypatch, tmp_path)


def post_chat(client: TestClient, message: str, session_id: str | None = None):
    return client.post("/api/chat", json={"message": message, "session_id": session_id},
                       headers=AUTH)


def _observation_row_count() -> int:
    with history_store.connect() as conn:
        history_store._create_schema(conn)
        return int(conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"])


# ---------------------------------------------------------------------------
# Tool catalog + executor (pure, no LLM)
# ---------------------------------------------------------------------------

def test_tool_catalog_is_read_only():
    names = [schema["function"]["name"] for schema in tools.TOOL_SCHEMAS]
    assert names == READ_ONLY_TOOL_NAMES
    forbidden = ("ingest", "run_ingestion", "refresh", "write", "delete")
    for name in names:
        assert not any(flag in name for flag in forbidden), f"write-like tool leaked: {name}"


def test_forecast_ap_prices_tool_executes_and_compacts(client):
    outcome = tools.execute_tool("forecast_ap_prices", {
        "price_types": ["retail"], "start_date": "2026-10-26", "end_date": "2026-10-30",
    })
    assert outcome.error is None
    assert outcome.payload["graph"] == "ap_price_forecast"
    assert outcome.payload["unit"] in {"₹/kg", "INR/kg"}  # handler may use ASCII unit
    assert outcome.is_graph
    compact_text = tools.tool_message_content(outcome)
    assert len(compact_text) < 7000, "compact must stay small; full payload goes to chart"
    summary = outcome.compact["forecast_summary"]
    assert any(entry["state"] == "AP" and entry["series"] == "retail" for entry in summary)
    assert "p50_rs_kg" not in compact_text.lower()
    assert "p10_rs_kg" not in compact_text.lower()
    assert "p90_rs_kg" not in compact_text.lower()
    forecast = next(entry for entry in summary if entry["state"] == "AP" and entry["series"] == "retail")
    assert forecast["unit_value_key"] == "predicted_price_rs_kg"
    assert {"lower_bound_rs_kg", "predicted_price_rs_kg", "upper_bound_rs_kg"} <= set(
        forecast["sample_rows"][0])
    assert forecast["sample_rows"][0]["confidence_score"] >= 0


def test_forecast_system_prompt_uses_plain_language():
    prompt = agent.build_system_prompt().lower()
    assert "predicted price" in prompt
    assert "lower bound" in prompt
    assert "upper bound" in prompt
    assert "confidence score" in prompt
    assert "uncalibrated" not in prompt
    assert "discuss calibration" in prompt
    assert "concise bullet points" in prompt
    assert "2–4 bullets" in prompt
    assert "do not mention internal implementation details" in prompt
    assert "graph numbers or names" in prompt
    assert "api calls" in prompt
    assert "forecast_ap_prices" not in prompt
    assert "do not suggest refreshing data" in prompt
    assert "pull_upag_dash.py" not in prompt


def test_chat_hides_score_methodology_warnings():
    warnings = agent._chat_chart_warnings({
        "warnings": [
            "Derived confidence score is an uncalibrated placeholder.",
            "Forecast range was shortened to the available horizon.",
        ],
    })
    assert warnings == ["Forecast range was shortened to the available horizon."]


def test_forecast_state_comparison_tool_uppercases_states(client):
    outcome = tools.execute_tool("forecast_state_comparison", {
        "states": ["ap", "ka"], "price_types": ["retail"],
        "start_date": "2026-10-26", "end_date": "2026-10-30",
    })
    assert outcome.error is None, outcome.error
    assert outcome.payload["graph"] == "state_price_comparison"
    assert outcome.arguments["states"] == "AP,KA"


def test_supply_balance_tool_returns_ap_mass_balance(client):
    outcome = tools.execute_tool("forecast_supply_balance", {
        "start_date": "2026-10-25", "end_date": "2027-01-24",
    })
    assert outcome.error is None
    payload = outcome.payload
    assert payload["graph"] == "supply_demand_shortfall"
    assert payload.get("balance_formula")
    monthly = [row["date"][:7] for row in payload.get("balance_forecast", [])]
    assert monthly, "balance_forecast rows expected for AP"
    compact = tools.tool_message_content(outcome).lower()
    assert "p10" not in compact and "p50" not in compact and "p90" not in compact
    assert outcome.compact["quantity_forecast_summary"]
    assert "lower_bound_shortfall_tonnes_month" in compact
    assert "upper_bound_shortfall_tonnes_month" in compact


def test_factors_tool_reports_actual_retail_context_and_limits(client):
    outcome = tools.execute_tool("factors", {"target": "retail", "state": "AP"})

    assert outcome.error is None
    result = outcome.compact
    assert result["forecasts"][0]["forecast"] == "AP retail prices"
    factors = result["forecasts"][0]["model_context"]
    names = [factor["factor"] for factor in factors]
    assert names == [
        "Past AP retail prices",
        "Past AP wholesale prices",
        "Past AP mandi modal prices",
        "Past AP mandi arrivals",
    ]
    assert "separate forecasts" in result["forecasts"][0]["future_context"]
    assert "Weather and storms" in result["not_included_as_separate_inputs"]
    assert "do not prove what caused" in result["limits"]
    assert tools.execute_tool("factors", {"target": "supply_balance", "state": "KA"}).error

    supply = tools.execute_tool("factors", {"target": "supply_balance"})
    assert supply.error is None
    assert [entry["forecast"] for entry in supply.compact["forecasts"]] == [
        "AP monthly supply balance",
    ]


def test_factors_prompt_requires_simple_non_causal_explanations():
    prompt = agent.build_system_prompt().lower()
    assert "call the factors tool" in prompt
    assert "explain simply and briefly" in prompt
    assert "do not claim a factor caused a specific change" in prompt


def test_invalid_tool_arguments_return_chat_safe_errors():
    outcome = tools.execute_tool("forecast_ap_prices", {"start_date": "not-a-date",
                                                        "end_date": "2026-10-30"})
    assert outcome.error and "start_date" in outcome.error
    assert outcome.payload is None
    assert tools.execute_tool("not_a_tool", {}).error


# ---------------------------------------------------------------------------
# POST /api/chat end-to-end with scripted brain
# ---------------------------------------------------------------------------

def test_chat_response_contract_and_chart_attachment(client, monkeypatch):
    scripted_brain(monkeypatch, steps=[
        {"tool_calls": [("forecast_ap_prices",
                         {"price_types": ["retail"], "start_date": "2026-10-26",
                          "end_date": "2026-10-30"})]},
        "AP retail forecast: the predicted price is within the lower and upper bounds. "
        "Confidence score is included where available.",
    ])
    response = post_chat(client, "What will AP retail price do this week?")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "session_id", "answer", "answer_format", "chart", "chart_display",
        "tools_used", "session_title", "warnings",
    }
    assert body["answer_format"] == "markdown"
    assert body["chart_display"] == "collapsed"
    assert body["tools_used"] == ["forecast_ap_prices"]
    assert body["chart"] is not None
    assert body["chart"]["graph"] == "ap_price_forecast"
    assert body["session_title"].startswith("What will AP retail")
    assert body["session_id"]


def test_chat_multi_state_routes_to_graph2_chart(client, monkeypatch):
    scripted_brain(monkeypatch, steps=[
        {"tool_calls": [("forecast_state_comparison",
                         {"states": ["AP", "KA"], "price_types": ["retail"],
                          "start_date": "2026-10-26", "end_date": "2026-10-30"})]},
        "AP stays near the tool numbers; KA differs slightly.",
    ])
    body = post_chat(client, "Compare AP vs KA retail onions.").json()
    assert body["tools_used"] == ["forecast_state_comparison"]
    assert body["chart"]["graph"] == "state_price_comparison"


def test_tool_error_is_surfaced_to_llm_not_crash(client, monkeypatch):
    scripted_brain(monkeypatch, steps=[
        {"tool_calls": [("forecast_supply_balance",
                         {"start_date": "2026-13-40", "end_date": "2027-01-24"})]},
        "I could not fetch the supply balance; the given dates were invalid. "
        "The balance dataset covers Andhra Pradesh (AP) only.",
    ])
    body = post_chat(client, "Show me MP shortage for next quarter.").json()
    assert body["answer"].startswith("I could not")
    assert body["tools_used"] == ["forecast_supply_balance"]
    assert body["chart"] is None
    assert any("could not provide data" in warning for warning in body["warnings"])
    assert "AP" in body["answer"]


def test_data_freshness_flow_is_read_only(client, monkeypatch):
    """History tables untouched + ingestion writers unreachable through the chat flow."""

    def _no_writes(*_args, **_kwargs):
        raise AssertionError("chatbot must never trigger ingestion writes")

    for writer in ("upsert_dash_observations", "upsert_dash_graph_observations",
                   "upsert_frame", "sync_daily_price_csv_from_graph"):
        monkeypatch.setattr(history_store, writer, _no_writes)

    rows_before = _observation_row_count()
    freshness = tools.execute_tool("data_freshness", {})
    freshness_message = tools.tool_message_content(freshness)
    assert freshness.error is None
    assert freshness.compact
    assert "ingestion" not in freshness_message
    assert "pull_upag_dash" not in freshness_message

    scripted_brain(monkeypatch, steps=[
        {"tool_calls": [("data_freshness", {})]},
        "Data is current — the last observation is 2026-10-25 (staleness noted).",
    ])
    body = post_chat(client, "Is the data fresh right now?").json()
    assert body["tools_used"] == ["data_freshness"]
    assert body["chart"] is None
    assert "2026-10-25" in body["answer"]  # verified date survives redaction
    assert "pull_upag_dash" not in body["answer"]
    assert "scheduled ingestion" not in body["answer"].lower()
    assert _observation_row_count() == rows_before, "freshness must not write anything"


def test_unknown_numbers_are_redacted_from_answer(client, monkeypatch):
    scripted_brain(monkeypatch, steps=[
        {"tool_calls": [("forecast_ap_prices",
                         {"price_types": ["retail"], "start_date": "2026-10-26",
                          "end_date": "2026-10-30"})]},
        "Retail price will be ₹99.99/kg, rising 12.5% next week.",
    ])
    body = post_chat(client, "AP retail price next week?").json()
    assert "99.99" not in body["answer"]
    assert "12.5" not in body["answer"]
    assert "…" in body["answer"]
    assert any("could not be verified" in warning for warning in body["warnings"])


def test_verified_tool_numbers_survive(client, monkeypatch):
    # end_date must extend past "today" (2026-10-29 in the fixture) so the
    # handler produces forecast rows starting at max(today, last_obs + 1).
    outcome = tools.execute_tool("forecast_ap_prices",
                                 {"start_date": "2026-10-29", "end_date": "2026-10-31"})
    p50_quote = outcome.compact["forecast_summary"][0]["last"]
    scripted_brain(monkeypatch, steps=[
        {"tool_calls": [("forecast_ap_prices",
                         {"start_date": "2026-10-29", "end_date": "2026-10-31"})]},
        f"The tool quotes ₹{p50_quote}/kg as the latest median forecast.",
    ])
    body = post_chat(client, "Exact last forecast number?").json()
    assert f"₹{p50_quote}/kg" in body["answer"], body["answer"]


def test_sessions_history_endpoints_and_auto_title(client, monkeypatch):
    scripted_brain(monkeypatch, steps=[
        "Checked via tools; AP retail data is stored through the tool range.",
        "Wholesale is also stored; both series verified.",
    ])
    first = post_chat(client, "What is AP retail price today?").json()
    second = post_chat(client, "And AP wholesale?", first["session_id"]).json()
    assert second["session_id"] == first["session_id"]

    history = client.get("/api/chat/history",
                         params={"session_id": first["session_id"]},
                         headers=AUTH).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant", "user", "assistant"]

    listing = client.get("/api/chat/sessions", headers=AUTH).json()
    assert listing["sessions"] and listing["sessions"][0]["session_id"] == first["session_id"]
    assert listing["sessions"][0]["title"] == "What is AP retail price today?"[:48]

    assert client.get("/api/chat/history",
                      params={"session_id": "does-not-exist"},
                      headers=AUTH).status_code == 404


def test_chat_requires_bearer_token(client):
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert client.get("/api/chat/sessions").status_code == 401
    assert client.get("/api/chat/history", params={"session_id": "x"}).status_code == 401


# ---------------------------------------------------------------------------
# Agent configuration & guards
# ---------------------------------------------------------------------------

def test_chat_disabled_returns_503(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GROQ_ENABLED", "0")
    response = post_chat(client, "hello")
    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]["message"].lower()


def test_chat_without_api_key_returns_503(monkeypatch, tmp_path):
    client = client_with_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GROQ_ENABLED", "1")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    response = post_chat(client, "hello")
    assert response.status_code == 503
    detail = response.json()["detail"]["message"]
    assert "GROQ_API_KEY" in detail and "console.groq.com" in detail


def test_chat_input_too_long_is_422(client, monkeypatch):
    monkeypatch.setenv("CHAT_MAX_INPUT_CHARS", "50")
    response = post_chat(client, "x" * 60)
    assert response.status_code == 422
    assert "too long" in response.json()["detail"]


def test_fallback_model_chain(monkeypatch):
    """Primary fails → retry → fallback answers. Verifies the chain order."""
    monkeypatch.setenv("CHAT_RETRY_DELAY_SEC", "0")
    monkeypatch.setattr(agent, "_get_client", lambda: object())
    attempts: list[str] = []

    def flaky_complete(client, model, messages, use_tools):
        attempts.append(model)
        if model == agent._cfg()["model"]:
            raise RuntimeError("synthetic primary outage")
        return _FakeMessage(content="fallback answer")

    monkeypatch.setattr(agent, "_complete_turn", flaky_complete)
    message, model = agent._call_model_with_fallback(
        object(), [{"role": "user", "content": "hi"}], use_tools=False)
    assert model == "llama-3.1-8b-instant"
    assert message.content == "fallback answer"
    # primary once → fallback once (the chain in agent.py)
    assert attempts == ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]


def test_max_tool_rounds_nudges_final_answer(client, monkeypatch):
    monkeypatch.setenv("CHAT_MAX_TOOL_ROUNDS", "2")
    record = scripted_brain(monkeypatch, steps=[
        {"tool_calls": [("data_freshness", {})]},
    ])
    body = post_chat(client, "summarize data status").json()
    assert body["answer"] == "Understood, all tool data checked."
    assert body["tools_used"] == ["data_freshness", "data_freshness"]
    # two tool rounds + forced final call without tools
    assert record["use_tools"] == [True, True, False]
