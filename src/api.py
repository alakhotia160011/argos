"""FastAPI backend for ARGOS dashboard.

Endpoints:
  POST /api/backtest       — Start a backtest (returns run_id)
  GET  /api/backtest/{id}  — SSE stream of progress events
  GET  /api/results        — Latest results from disk
  GET  /api/trades         — Trade log from disk
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from src.config import DEFAULT_INITIAL_CASH, Settings

logger = logging.getLogger(__name__)

app = FastAPI(title="ARGOS API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store for active backtest runs
_runs: dict[str, dict[str, Any]] = {}

RESULTS_DIR = Path("results")
LOGS_DIR = Path("data/logs")


# ─── Models ───────────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    initial_cash: float = DEFAULT_INITIAL_CASH


class BacktestResponse(BaseModel):
    run_id: str
    status: str


# ─── Backtest runner with per-agent progress ─────────────────────────────────

async def _run_backtest_with_progress(run_id: str, start: date, end: date, cash: float):
    """Run backtest using the real backtest loop, with agent-level SSE progress."""
    queue: asyncio.Queue = _runs[run_id]["queue"]

    # Wire up per-agent progress callback
    from src.agents.eod_cycle import set_progress_callback

    async def on_agent_progress(event: str, data: dict):
        await queue.put({"event": event, "data": data})

    set_progress_callback(on_agent_progress)

    try:
        await queue.put({"event": "started", "data": {"start": str(start), "end": str(end), "cash": cash}})

        # Clear previous state
        state_dir = Path("data/state")
        if state_dir.exists():
            for f in state_dir.glob("*.json"):
                f.unlink()

        from src.agents.backtest_loop import run_backtest
        from src.utils.logging import setup_logging
        setup_logging()

        # Monkey-patch logger.info to also emit day-level progress
        _orig_info = logging.getLogger("__main__").info
        day_count = {"current": 0, "total": 0}

        orig_logger = logging.getLogger("src.agents.backtest_loop")
        orig_eod_logger = logging.getLogger("src.agents.eod_cycle")

        class ProgressHandler(logging.Handler):
            def emit(self, record):
                msg = record.getMessage()
                # Detect day boundaries
                if "=== Day" in msg and ":" in msg:
                    # e.g. "=== Day 1: 2026-03-03 ==="
                    parts = msg.split("Day ")[1].split(":")
                    day_num = int(parts[0].strip())
                    day_date = parts[1].strip().rstrip(" =").strip()
                    day_count["current"] = day_num
                    asyncio.get_event_loop().call_soon_threadsafe(
                        lambda: queue.put_nowait({"event": "day_start", "data": {
                            "day": day_num, "total": day_count["total"], "date": day_date,
                        }})
                    )
                elif "Backtest:" in msg and "trading days" in msg:
                    parts = msg.split()
                    for i, p in enumerate(parts):
                        if p.isdigit():
                            day_count["total"] = int(p)
                            break
                    asyncio.get_event_loop().call_soon_threadsafe(
                        lambda: queue.put_nowait({"event": "info", "data": {
                            "total_days": day_count["total"], "message": msg,
                        }})
                    )
                elif "Executed:" in msg:
                    asyncio.get_event_loop().call_soon_threadsafe(
                        lambda m=msg: queue.put_nowait({"event": "trade", "data": {"message": m}})
                    )
                elif "=== Layer" in msg:
                    asyncio.get_event_loop().call_soon_threadsafe(
                        lambda m=msg: queue.put_nowait({"event": "layer_log", "data": {"message": m}})
                    )

        handler = ProgressHandler()
        handler.setLevel(logging.INFO)
        orig_logger.addHandler(handler)
        orig_eod_logger.addHandler(handler)

        # Run the actual backtest
        result = await run_backtest(start, end, initial_cash=cash)

        # Clean up
        orig_logger.removeHandler(handler)
        orig_eod_logger.removeHandler(handler)
        set_progress_callback(None)

        _runs[run_id]["status"] = "completed"
        _runs[run_id]["result"] = result
        await queue.put({"event": "completed", "data": result})

    except Exception as e:
        logger.exception("Backtest failed")
        set_progress_callback(None)
        _runs[run_id]["status"] = "failed"
        await queue.put({"event": "error", "data": {"message": str(e)}})


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/api/backtest", response_model=BacktestResponse)
async def start_backtest(req: BacktestRequest):
    """Start a new backtest run."""
    run_id = str(uuid.uuid4())[:8]

    try:
        start = date.fromisoformat(req.start_date)
        end = date.fromisoformat(req.end_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

    if start >= end:
        raise HTTPException(status_code=400, detail="start_date must be before end_date")

    _runs[run_id] = {
        "status": "running",
        "queue": asyncio.Queue(),
        "result": None,
    }

    asyncio.create_task(_run_backtest_with_progress(run_id, start, end, req.initial_cash))

    return BacktestResponse(run_id=run_id, status="running")


@app.get("/api/backtest/{run_id}/stream")
async def stream_backtest(run_id: str):
    """SSE stream of backtest progress."""
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run not found")

    queue: asyncio.Queue = _runs[run_id]["queue"]

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=300)
                yield {"event": event["event"], "data": json.dumps(event["data"])}
                if event["event"] in ("completed", "error"):
                    break
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}

    return EventSourceResponse(event_generator())


@app.get("/api/results")
async def get_results():
    """Return the latest results/summary.json."""
    summary_path = RESULTS_DIR / "summary.json"
    if not summary_path.exists():
        raise HTTPException(status_code=404, detail="No results yet")
    return json.loads(summary_path.read_text())


@app.get("/api/trades")
async def get_trades():
    """Return the trade log."""
    trades_path = LOGS_DIR / "trades.jsonl"
    if not trades_path.exists():
        raise HTTPException(status_code=404, detail="No trades yet")

    trades = []
    for line in trades_path.read_text().strip().split("\n"):
        if line.strip():
            trades.append(json.loads(line))
    return trades


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api:app", host="0.0.0.0", port=8000, reload=True)
