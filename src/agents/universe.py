"""S&P 500 stock universe with GICS sector mapping.

Fetches the current S&P 500 constituent list from Wikipedia and maps
GICS sectors to our internal sector desk structure. Falls back to a
cached static list if the fetch fails.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GICS → ARGOS sector mapping
# ---------------------------------------------------------------------------

# Map GICS sectors to our 6 sector desks + "other" for coverage gaps
GICS_TO_ARGOS = {
    "Information Technology": "semiconductor",  # tech desk covers semis + software
    "Energy": "energy",
    "Health Care": "biotech",                   # biotech desk covers all healthcare
    "Consumer Discretionary": "consumer",
    "Consumer Staples": "consumer",
    "Industrials": "industrials",
    "Financials": "financials",
    "Communication Services": "consumer",       # META, GOOG, NFLX etc → consumer
    "Materials": "industrials",                  # chemicals, metals → industrials
    "Real Estate": "financials",                 # REITs → financials desk
    "Utilities": "industrials",                  # utilities → industrials desk
}


def fetch_sp500_constituents() -> list[dict[str, str]]:
    """Fetch current S&P 500 constituents from Wikipedia.

    Returns list of dicts with keys: symbol, name, gics_sector, gics_sub_industry.
    """
    try:
        import httpx
        import pandas as pd
        import io

        resp = httpx.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0 ARGOS/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        df = tables[0]

        constituents = []
        for _, row in df.iterrows():
            symbol = str(row["Symbol"]).replace(".", "-")  # BRK.B → BRK-B for yfinance
            constituents.append({
                "symbol": symbol,
                "name": str(row["Security"]),
                "gics_sector": str(row["GICS Sector"]),
                "gics_sub_industry": str(row.get("GICS Sub-Industry", "")),
            })

        logger.info("Fetched %d S&P 500 constituents", len(constituents))
        return constituents

    except Exception as e:
        logger.warning("Failed to fetch S&P 500 list: %s. Using cache.", e)
        return _load_cached()


def _cache_path() -> Path:
    return Path("data/cache/sp500_constituents.json")


def _load_cached() -> list[dict[str, str]]:
    """Load cached constituent list."""
    path = _cache_path()
    if path.exists():
        return json.loads(path.read_text())
    return []


def save_cache(constituents: list[dict[str, str]]) -> None:
    """Cache the constituent list for offline use."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(constituents, indent=2))


def get_sp500_by_sector() -> dict[str, list[str]]:
    """Get S&P 500 tickers organized by ARGOS sector desk.

    Returns dict mapping sector desk name → list of tickers.
    """
    constituents = fetch_sp500_constituents()
    if constituents:
        save_cache(constituents)

    sectors: dict[str, list[str]] = {
        "semiconductor": [],
        "energy": [],
        "biotech": [],
        "consumer": [],
        "industrials": [],
        "financials": [],
    }

    for c in constituents:
        argos_sector = GICS_TO_ARGOS.get(c["gics_sector"])
        if argos_sector and argos_sector in sectors:
            sectors[argos_sector].append(c["symbol"])

    # Log coverage
    total = sum(len(v) for v in sectors.values())
    logger.info(
        "S&P 500 mapped: %d stocks across %d desks: %s",
        total,
        len(sectors),
        {k: len(v) for k, v in sectors.items()},
    )
    return sectors


def get_sector_with_focus(sector: str, max_tickers: int = 20) -> list[str]:
    """Get tickers for a sector, prioritizing our focus names.

    Returns the focus tickers first, then fills up to max_tickers
    from the broader S&P 500 universe.
    """
    from src.agents.eod_cycle import SECTOR_TICKERS

    focus = SECTOR_TICKERS.get(sector, [])
    all_sp500 = get_sp500_by_sector()
    broader = all_sp500.get(sector, [])

    # Focus names first, then additional S&P 500 names
    result = list(focus)
    for ticker in broader:
        if ticker not in result and len(result) < max_tickers:
            result.append(ticker)

    return result


def get_full_universe() -> dict[str, list[str]]:
    """Get the full S&P 500 universe organized by sector.

    Unlike get_sp500_by_sector(), this preserves all tickers (no limit).
    """
    return get_sp500_by_sector()


def get_all_tickers() -> list[str]:
    """Get flat list of all S&P 500 tickers."""
    sectors = get_sp500_by_sector()
    return [t for tickers in sectors.values() for t in tickers]
