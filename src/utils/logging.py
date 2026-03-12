"""Structured logging for ARGOS trading system.

Writes daily log files to data/logs/ capturing:
  - Agent inputs/outputs (what each agent saw and recommended)
  - Trade executions (buys, sells, sizes, prices)
  - Performance snapshots (NAV, P&L, exposure)
  - Errors and API retries

Log files:
  data/logs/argos_YYYY-MM-DD.log   — human-readable daily log
  data/logs/trades.jsonl            — append-only trade ledger
  data/logs/performance.jsonl       — daily NAV/exposure snapshots
  data/logs/agent_calls.jsonl       — every agent input/output
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

LOGS_DIR = Path("data/logs")


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging for ARGOS: console + daily file.

    Call once at startup (in main() or backtest entry).
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    log_file = LOGS_DIR / f"argos_{today}.log"

    fmt = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
    datefmt = "%H:%M:%S"

    # Root logger
    root = logging.getLogger()
    root.setLevel(level)

    # Clear existing handlers to avoid duplicates on re-init
    root.handlers.clear()

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(console)

    # File handler (daily)
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # capture everything to file
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s")
    )
    root.addHandler(file_handler)

    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Structured JSONL loggers
# ---------------------------------------------------------------------------


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append a JSON record to a JSONL file."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def log_agent_call(
    agent_name: str,
    layer: str,
    input_summary: dict[str, Any],
    output: dict[str, Any],
    as_of: date | str | None = None,
) -> None:
    """Log an agent's input/output to agent_calls.jsonl."""
    _append_jsonl(LOGS_DIR / "agent_calls.jsonl", {
        "timestamp": datetime.now().isoformat(),
        "as_of": str(as_of) if as_of else date.today().isoformat(),
        "agent": agent_name,
        "layer": layer,
        "input_keys": list(input_summary.keys()) if isinstance(input_summary, dict) else str(input_summary),
        "input_size_bytes": len(json.dumps(input_summary, default=str)),
        "output": _truncate_output(output),
        "parse_error": output.get("_parse_error", False),
    })


def log_trade(
    as_of: date | str,
    ticker: str,
    action: str,
    direction: str,
    shares: int | float,
    price: float,
    notional: float,
    reason: str = "",
    agent_source: str = "",
    conviction: float | None = None,
) -> None:
    """Log a trade execution to trades.jsonl."""
    _append_jsonl(LOGS_DIR / "trades.jsonl", {
        "timestamp": datetime.now().isoformat(),
        "as_of": str(as_of),
        "ticker": ticker,
        "action": action,
        "direction": direction,
        "shares": shares,
        "price": round(price, 4),
        "notional": round(notional, 2),
        "reason": reason,
        "agent_source": agent_source,
        "conviction": conviction,
    })


def log_performance(
    as_of: date | str,
    nav: float,
    cash: float,
    num_positions: int,
    gross_exposure: float,
    net_exposure: float,
    daily_pnl: float = 0.0,
    daily_return_pct: float = 0.0,
    positions: dict[str, Any] | None = None,
    regime: str = "",
) -> None:
    """Log a daily performance snapshot to performance.jsonl."""
    _append_jsonl(LOGS_DIR / "performance.jsonl", {
        "timestamp": datetime.now().isoformat(),
        "as_of": str(as_of),
        "nav": round(nav, 2),
        "cash": round(cash, 2),
        "num_positions": num_positions,
        "gross_exposure": round(gross_exposure, 4),
        "net_exposure": round(net_exposure, 4),
        "daily_pnl": round(daily_pnl, 2),
        "daily_return_pct": round(daily_return_pct, 4),
        "positions": {
            t: {"shares": p.get("shares"), "price": round(p.get("current_price", 0), 2), "pnl": round(p.get("pnl", 0), 2)}
            for t, p in (positions or {}).items()
        },
        "regime": regime,
    })


def log_error(
    context: str,
    error: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Log an error with context to errors.jsonl."""
    _append_jsonl(LOGS_DIR / "errors.jsonl", {
        "timestamp": datetime.now().isoformat(),
        "context": context,
        "error": error,
        "details": details or {},
    })


def log_weight_update(
    as_of: date | str,
    agent_name: str,
    old_weight: float,
    new_weight: float,
    sharpe: float | None = None,
    quartile: str = "",
) -> None:
    """Log a Darwinian weight change to weights.jsonl."""
    _append_jsonl(LOGS_DIR / "weights.jsonl", {
        "timestamp": datetime.now().isoformat(),
        "as_of": str(as_of),
        "agent": agent_name,
        "old_weight": round(old_weight, 4),
        "new_weight": round(new_weight, 4),
        "change_pct": round((new_weight / max(old_weight, 0.01) - 1) * 100, 2),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "quartile": quartile,
    })


def log_autoresearch(
    as_of: date | str,
    agent_name: str,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Log autoresearch events to autoresearch.jsonl."""
    _append_jsonl(LOGS_DIR / "autoresearch.jsonl", {
        "timestamp": datetime.now().isoformat(),
        "as_of": str(as_of),
        "agent": agent_name,
        "action": action,  # "propose", "observe", "keep", "revert"
        "details": details or {},
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate_output(output: dict[str, Any], max_len: int = 2000) -> dict[str, Any]:
    """Truncate large output values for logging."""
    result = {}
    for k, v in output.items():
        if k.startswith("_"):
            continue
        s = json.dumps(v, default=str)
        if len(s) > max_len:
            result[k] = f"<truncated, {len(s)} chars>"
        else:
            result[k] = v
    return result


def read_trades(as_of: date | str | None = None) -> list[dict]:
    """Read trade log, optionally filtered to a date."""
    path = LOGS_DIR / "trades.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if as_of:
        records = [r for r in records if r.get("as_of") == str(as_of)]
    return records


def read_performance(last_n: int | None = None) -> list[dict]:
    """Read performance snapshots."""
    path = LOGS_DIR / "performance.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if last_n:
        records = records[-last_n:]
    return records
