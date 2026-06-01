"""FastAPI app: serves the patient UI and the SSE endpoints."""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

from app import board, sessions, llm, prompts  # noqa: E402

log = logging.getLogger("uvicorn.error")

STATIC_DIR = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(sessions.cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()


app = FastAPI(title="AI for Cancer Patients", lifespan=lifespan)

_origins = os.getenv("CANCERPATIENT_ALLOWED_ORIGINS", "").strip()
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _origins.split(",") if o.strip()],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class PatientRequest(BaseModel):
    case: str = Field(..., min_length=20, max_length=10000)
    location: str = Field(default="", max_length=200)
    preferences: str = Field(default="", max_length=500)
    target_language: str = Field(default="English", max_length=60)


class BoardResponse(BaseModel):
    session_id: str


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/about")
async def about() -> FileResponse:
    return FileResponse(STATIC_DIR / "about.html")


@app.get("/privacy")
async def privacy() -> FileResponse:
    return FileResponse(STATIC_DIR / "privacy.html")


_MAX_ACTIVE_SESSIONS = int(os.getenv("CANCERPATIENT_MAX_ACTIVE_SESSIONS", "20"))


@app.post("/api/board", response_model=BoardResponse)
async def start_board(req: PatientRequest) -> BoardResponse:
    active = sum(1 for s in sessions.SESSIONS.values() if s.finished_at is None)
    if active >= _MAX_ACTIVE_SESSIONS:
        raise HTTPException(
            status_code=503,
            detail=f"Server busy ({active} active sessions). Try again in a few minutes.",
        )
    session = sessions.new_session()
    emit = sessions.emit_factory(session)

    async def _runner() -> None:
        try:
            result = await board.run_board(
                req.case,
                req.location,
                req.target_language,
                emit,
                preferences=req.preferences,
            )
            session.final_result = result
        except asyncio.CancelledError:
            emit("error", {"message": "Session cancelled."})
            raise
        except Exception as e:
            log.exception("Board run failed")
            msg = f"{type(e).__name__}: {e}"
            if len(msg) > 240:
                msg = msg[:237] + "..."
            emit("error", {"message": msg})
            session.error = str(e)
        finally:
            session.finished_at = time.time()
            try:
                session.queue.put_nowait({"type": "__end__", "payload": {}})
            except asyncio.QueueFull:
                pass

    session.task = asyncio.create_task(_runner())
    return BoardResponse(session_id=session.sid)


@app.get("/api/board/{sid}/stream")
async def stream_board(sid: str) -> StreamingResponse:
    session = sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    if session.is_streaming:
        raise HTTPException(
            status_code=409,
            detail="This session is already being streamed by another client.",
        )

    async def event_generator():
        session.is_streaming = True
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(session.queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield f"event: ping\ndata: {{\"ts\": {time.time()}}}\n\n"
                    continue

                if ev.get("type") == "__end__":
                    break

                payload = json.dumps(ev)
                yield f"data: {payload}\n\n"

                if ev.get("type") in ("final", "error"):
                    break
        finally:
            session.is_streaming = False

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete("/api/board/{sid}")
async def cancel_board(sid: str) -> dict:
    session = sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    if session.task and not session.task.done():
        session.task.cancel()
    return {"cancelled": True, "session_id": sid}


@app.get("/api/board/{sid}")
async def board_state(sid: str) -> dict:
    session = sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    return {
        "session_id": sid,
        "finished": session.finished_at is not None,
        "final_result": session.final_result,
        "error": session.error,
    }


@app.get("/api/board/{sid}/lay_summary/{label}")
async def lay_summary(sid: str, label: str) -> dict:
    """On-demand plain-English summary for a single citation. Called by the
    frontend when the patient hovers a `[N]` citation in the rendered summary.
    Cached per-session so a re-hover doesn't pay the LLM cost twice."""
    session = sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    if not session.final_result:
        raise HTTPException(status_code=409, detail="Session not finished yet")

    # Cache hit
    cached = session.lay_summaries.get(label)
    if cached:
        return {"label": label, "lay_summary": cached, "cached": True}

    # Find the referenced entry in the final_result references list
    refs = session.final_result.get("references") or []
    entry = next((r for r in refs if str(r.get("label")) == str(label)), None)
    if not entry:
        raise HTTPException(status_code=404, detail=f"No reference with label [{label}]")

    user_content = (
        f"TITLE: {entry.get('title') or '(no title)'}\n"
        f"DOMAIN: {entry.get('journal') or '(unknown)'}\n"
        f"YEAR: {entry.get('year') or '(unknown)'}\n"
        f"SNIPPET: {(entry.get('summary') or '(no snippet)')[:1500]}"
    )
    messages = [
        {"role": "system", "content": prompts.LAY_SUMMARY},
        {"role": "user", "content": user_content},
    ]

    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(llm.chat, messages, tools=None),
            timeout=20.0,
        )
        text = (resp.choices[0].message.content or "").strip()
    except llm.QuotaExceeded:
        text = ""
    except (asyncio.TimeoutError, Exception):
        log.exception("lay_summary failed for sid=%s label=%s", sid, label)
        text = ""

    if not text:
        # Fall back to the snippet so the popup always shows something useful.
        snippet = (entry.get("summary") or "").strip()
        if snippet:
            text = snippet[:240].rsplit(" ", 1)[0] + ("…" if len(snippet) > 240 else "")

    session.lay_summaries[label] = text
    return {"label": label, "lay_summary": text, "cached": False}
