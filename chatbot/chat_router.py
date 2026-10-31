"""FastAPI router for the Onion Forecasting chatbot (Architecture A).

Endpoints (all inherit the app's /api/* Bearer-token middleware):
- POST /api/chat            → one chat turn (blocking; primary path for Streamlit)
- POST /api/chat/stream     → SSE events (thinking/chunk/chart/done) for React later
- GET  /api/chat/sessions   → recent conversations for the panel's history list
- GET  /api/chat/history    → full message list of one session

The router only delegates to chatbot.agent / chatbot.sessions — the forecasting
core, ingestion and auth middleware remain untouched.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import agent, sessions

logger = logging.getLogger("onion-chat-api")

router = APIRouter(prefix="/api/chat", tags=["chatbot"])


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    wants_chart: bool | None = None


@router.post("")
def chat_endpoint(request: ChatRequest) -> dict[str, Any]:
    try:
        return agent.run_chat(request.message, request.session_id, request.wants_chart)
    except agent.ChatInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except agent.ChatUnavailable as exc:
        logger.warning("Chat unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "message": str(exc),
                "hint": "Set GROQ_API_KEY in .env (free at console.groq.com), or check Groq status.",
            },
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:  # keep raw500 out of the chat surface
        logger.exception("Unexpected chat failure")
        raise HTTPException(status_code=500, detail="Unexpected chat failure. Check server logs.") from exc


@router.get("/sessions")
def chat_sessions(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    return {"sessions": sessions.list_sessions(limit)}


@router.get("/history")
def chat_history(session_id: str = Query(...),
                 limit: int = Query(100, ge=1, le=200)) -> dict[str, Any]:
    if sessions.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Chat session not found.")
    return {"session_id": session_id, "messages": sessions.get_history(session_id, limit)}


def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/stream")
def chat_stream_endpoint(request: ChatRequest) -> StreamingResponse:
    """SSE variant emitting status → answer chunks → chart → done."""

    def events():
        try:
            yield _sse({"stage": "thinking"})
            response = agent.run_chat(request.message, request.session_id, request.wants_chart)
            answer = response.get("answer") or ""
            for index in range(0, len(answer), 180):
                yield _sse({"chunk": answer[index:index + 180]})
            if response.get("chart"):
                yield _sse({"chart": response["chart"]})
            yield _sse({"done": {k: response.get(k) for k in
                                 ("session_id", "tools_used", "session_title", "warnings")}})
        except agent.ChatUnavailable as exc:
            yield _sse({"stage": "error", "detail": str(exc)})
        except Exception as exc:
            logger.exception("Chat stream failure")
            yield _sse({"stage": "error", "detail": "Unexpected chat failure."})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
