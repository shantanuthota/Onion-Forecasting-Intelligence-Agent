"""Chat orchestration for the Onion Forecasting chatbot (Architecture A).

Loop per user message:
1. build messages: system prompt + last N messages + new user text
2. call Groq (OpenAI-compatible) with the 6 read-only tool schemas
3. while the model requests tool calls (≤ CHAT_MAX_TOOL_ROUNDS): execute each
   via chatbot.tools.execute_tool, feed COMPACT results back, keep the FULL
   graph payload(s) for the chart attachment
4. compose the final answer, verify quoted ₹/% numbers against tool data
   (redaction), persist both messages, return the chat contract response.

The LLM is a router/narrator only — every number it may quote must exist in the
tool results; anything else is redacted.

Chatting brain selection: GROQ_ENABLED + GROQ_API_KEY (+ GROQ_BASE_URL) in .env;
model + fallback configured via CHAT_LLM_MODEL / CHAT_LLM_FALLBACK_MODEL.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import date, timedelta
from typing import Any

from . import sessions, tools
from .prompts import FINAL_NUDGE, SYSTEM_PROMPT_TEMPLATE
from .tools import TOOL_SCHEMAS, execute_tool, tool_message_content

logger = logging.getLogger("onion-chat-agent")

GRAPH_TOOL_NAMES = {"forecast_ap_prices", "forecast_state_comparison", "forecast_supply_balance"}


class ChatUnavailable(RuntimeError):
    """Raised when no usable LLM brain is configured/reachable (maps to HTTP 503)."""


class ChatInputError(ValueError):
    """Raised for invalid chat input (maps to HTTP 422)."""


# ---------------------------------------------------------------------------
# Live-reading config (tests can monkeypatch env between calls)
# ---------------------------------------------------------------------------

def _cfg() -> dict[str, Any]:
    return {
        "enabled": os.getenv("GROQ_ENABLED", "1").strip() == "1",
        "api_key": os.getenv("GROQ_API_KEY", "").strip(),
        "base_url": os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
        "model": os.getenv("CHAT_LLM_MODEL", "llama-3.3-70b-versatile"),
        "fallback_model": os.getenv("CHAT_LLM_FALLBACK_MODEL", "llama-3.1-8b-instant"),
        "temperature": float(os.getenv("CHAT_TEMPERATURE", "0.2")),
        "history_limit": int(os.getenv("CHAT_HISTORY_LIMIT", "10")),
        "max_tool_rounds": int(os.getenv("CHAT_MAX_TOOL_ROUNDS", "4")),
        "max_input_chars": int(os.getenv("CHAT_MAX_INPUT_CHARS", "2000")),
        "timeout_sec": float(os.getenv("CHAT_LLM_TIMEOUT_SEC", "60")),
        "retry_delay_sec": float(os.getenv("CHAT_RETRY_DELAY_SEC", "2")),
        "redaction": os.getenv("CHAT_REDACTION", "1").strip() == "1",
    }


# ---------------------------------------------------------------------------
# Groq client seam (single pluggable point; tests replace _complete_turn)
# ---------------------------------------------------------------------------

def _get_client():
    cfg = _cfg()
    if not cfg["enabled"]:
        raise ChatUnavailable("Chatbot is disabled (GROQ_ENABLED=0).")
    if not cfg["api_key"]:
        raise ChatUnavailable(
            "GROQ_API_KEY is not configured in .env. Create a free key at console.groq.com "
            "and set GROQ_API_KEY plus GROQ_BASE_URL=https://api.groq.com/openai/v1.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ChatUnavailable(
            "The 'openai' package is missing. Activate the project virtual environment and run: "
            "pip install -r requirements.txt") from exc
    return OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])


def _complete_turn(client: Any, model: str, messages: list[dict[str, Any]], use_tools: bool):
    """One chat.completions call. Returns an OpenAI message-like object."""
    cfg = _cfg()
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": cfg["temperature"],
        "max_tokens": 1400,
        "timeout": cfg["timeout_sec"],
    }
    if use_tools:
        kwargs["tools"] = TOOL_SCHEMAS
        kwargs["tool_choice"] = "auto"
    return client.chat.completions.create(**kwargs).choices[0].message


_AI_TRANSIENT = ()


def _call_model_with_fallback(client: Any, messages: list[dict[str, Any]], use_tools: bool):
    """Primary model → one retry → fallback model, all failures raise ChatUnavailable."""
    cfg = _cfg()
    errors: list[str] = []
    for attempt, model in enumerate([cfg["model"], cfg["fallback_model"]]):
        try:
            return _complete_turn(client, model, messages, use_tools), model
        except Exception as exc:  # openai SDK raises plain Exceptions; all treated as retryable here
            errors.append(f"{model}: {type(exc).__name__}: {exc}")
            logger.warning("Groq call failed (%s)", errors[-1])
            if attempt == 0:
                time.sleep(max(0.0, cfg["retry_delay_sec"]))
    raise ChatUnavailable("LLM is temporarily unavailable. Tried: " + " | ".join(errors))


# ---------------------------------------------------------------------------
# Redaction verifier — quoted ₹/% numbers must exist in the tool data
# ---------------------------------------------------------------------------

_ISO_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_QUOTED = re.compile(r"₹\s*-?\d[\d,]*(?:\.\d+)?|-?\d[\d,]*(?:\.\d+)?\s*%")


def _collect_allowed_numbers(*payloads: Any) -> set[float]:
    """Numbers the LLM is ALLOWED to quote: everything present in tool compacts."""
    allowed: set[float] = set()
    for payload in payloads:
        text = json.dumps(payload, default=str, ensure_ascii=False)
        text = _ISO_DATE.sub(" ", text)
        for match in _NUMBER.findall(text):
            try:
                allowed.add(float(match.replace(",", "")))
            except ValueError:
                continue
    return allowed


def _number_verified(token: str, allowed: set[float]) -> bool:
    try:
        value = float(token.replace(",", ""))
    except ValueError:
        return True  # non-numeric → nothing to verify
    for candidate in allowed:
        if math.isclose(value, candidate, rel_tol=0.01, abs_tol=0.7):
            return True
    return False


def verify_answer(answer: str, allowed: set[float]) -> tuple[str, int]:
    """Redact quoted ₹/% numbers absent from tool data. Returns (answer, redacted_count)."""
    if not answer:
        return answer, 0
    redacted_count = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal redacted_count
        text = match.group(0)
        token = _NUMBER.search(text)
        if token is None or _number_verified(token.group(0), allowed):
            return text
        redacted_count += 1
        return "…"

    redacted_answer = _QUOTED.sub(_replace, answer)
    return redacted_answer, redacted_count


def _chat_chart_warnings(chart_payload: dict[str, Any]) -> list[str]:
    """Keep backend-only score-methodology notes out of user-facing chat."""
    return [
        str(warning) for warning in chart_payload.get("warnings") or []
        if "calibrat" not in str(warning).casefold()
    ]


# ---------------------------------------------------------------------------
# Message assembly + main entry point
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    today = date.today()
    return SYSTEM_PROMPT_TEMPLATE.format(
        today_iso=today.isoformat(),
        today_plus_7_iso=(today + timedelta(days=7)).isoformat(),
    )


def _history_messages(session_id: str, limit: int) -> list[dict[str, Any]]:
    return [
        {"role": m["role"], "content": m["content"]}
        for m in sessions.get_history(session_id, limit=limit)
    ]


def run_chat(message: str, session_id: str | None = None,
             wants_chart: bool | None = None) -> dict[str, Any]:
    """Run one chat turn end-to-end and return the chat contract response."""
    if not isinstance(message, str):
        raise ChatInputError("Message must be text.")
    message = message.strip()
    cfg = _cfg()
    if not message:
        raise ChatInputError("Message must not be empty.")
    if len(message) > cfg["max_input_chars"]:
        raise ChatInputError(
            f"Message is too long ({len(message)} chars; limit {cfg['max_input_chars']}).")

    # 1) session + history ----------------------------------------------------
    session = None
    if session_id:
        session = sessions.get_session(session_id)
    if session is None:
        session = sessions.create_session()
        session_id = session["session_id"]
    else:
        session_id = session["session_id"]
    history = _history_messages(session_id, cfg["history_limit"])
    sessions.append_message(session_id, "user", message)
    sessions.auto_title_if_default(session_id, message)

    # 2) brain ---------------------------------------------------------------
    client = _get_client()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt()},
        *history,
        {"role": "user", "content": message},
    ]

    chart_payload: dict[str, Any] | None = None
    chart_kinds: list[str] = []
    tools_used: list[str] = []
    warnings: list[str] = []
    responses_text: list[str] = []
    answer = ""
    model_used = None

    try:
        for round_index in range(cfg["max_tool_rounds"]):
            use_tools = True
            reply, model_used = _call_model_with_fallback(client, messages, use_tools)
            tool_calls = list(getattr(reply, "tool_calls", None) or [])
            if not tool_calls:
                responses_text.append(reply.content or "")
                answer = reply.content or ""
                break

            assistant_record = {
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name,
                                     "arguments": call.function.arguments or "{}"},
                    }
                    for call in tool_calls
                ],
            }
            messages.append(assistant_record)
            for call in tool_calls:
                name = call.function.name
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be a JSON object")
                except (ValueError, TypeError) as exc:
                    outcome = tools.ToolOutcome(name=name, arguments={},
                                                error=f"Invalid tool arguments: {exc}")
                else:
                    outcome = execute_tool(name, arguments)
                tools_used.append(name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": name,
                    "content": tool_message_content(outcome),
                })
                if outcome.error:
                    warnings.append(f"Tool {name} could not provide data: {outcome.error}")
                    continue
                if outcome.is_graph:
                    chart_payload = outcome.payload
                    chart_kinds.append(outcome.chart_kind)
                for warning in (outcome.compact or {}).get("warnings") or []:
                    warnings.append(str(warning))
            if model_used == cfg["fallback_model"]:
                # Be gentle when already on the fallback slice: keep default behavior.
                pass
        else:
            messages.append({"role": "system", "content": FINAL_NUDGE})
            reply, model_used = _call_model_with_fallback(client, messages, use_tools=False)
            responses_text.append(reply.content or "")
            answer = reply.content or ""
    except ChatUnavailable:
        raise
    except Exception as exc:
        logger.exception("Chat pipeline failure")
        sessions.append_message(session_id, "assistant",
                                "The assistant hit an internal error. Please try again.",
                                tools_used=tools_used, chart_kind=chart_kinds[-1] if chart_kinds else None)
        raise ChatUnavailable(f"Chat pipeline failure: {type(exc).__name__}: {exc}") from exc

    answer = answer.strip()
    if not answer:
        logger.error("LLM returned an empty answer after tool rounds")
        raise ChatUnavailable("The assistant returned an empty answer. Please try again.")

    # 3) verification --------------------------------------------------------
    redacted_count = 0
    if cfg["redaction"]:
        # Verify against everything the model actually saw: all tool messages content.
        seen_payloads = [m["content"] for m in messages if m["role"] == "tool"]
        allowed = _collect_allowed_numbers(*seen_payloads)
        answer, redacted_count = verify_answer(answer, allowed)
        if redacted_count:
            answer += "\n\n⚠️ Some quoted values could not be verified against tool data and were replaced with “…”."
            warnings.append(
                "Some quoted values could not be verified against tool data and were replaced with “…”.")

    # 4) persist + respond ---------------------------------------------------
    sessions.append_message(session_id, "assistant", answer,
                            tools_used=tools_used,
                            chart_kind=chart_kinds[-1] if chart_kinds else None)
    refreshed = sessions.get_session(session_id) or session
    response = {
        "session_id": session_id,
        "answer": answer,
        "answer_format": "markdown",
        "chart": chart_payload,
        "chart_display": "collapsed",
        "tools_used": tools_used,
        "session_title": refreshed.get("title"),
        "warnings": warnings,
    }
    if chart_payload is not None:
        response["warnings"] = list(warnings) + _chat_chart_warnings(chart_payload)
    return response
