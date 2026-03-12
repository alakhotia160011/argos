"""End-of-day cycle: orchestrates all 25 agents through the 4-layer pipeline.

This is the core daily execution — run Layer 1 (macro) in parallel, aggregate
a regime signal, pass it to Layer 2 (sector desks), then Layer 3
(superinvestors), and finally Layer 4 (decision).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from src.config import (
    AGENT_REGISTRY,
    Layer,
    Settings,
    agents_for_layer,
    REGIME_RISK_ON_THRESHOLD,
    REGIME_RISK_OFF_THRESHOLD,
    MAX_POSITION_PCT,
    MAX_GROSS_EXPOSURE,
    MAX_NET_EXPOSURE,
    MAX_POSITIONS,
    MIN_CASH_RESERVE_PCT,
)
from src.agents.scorecard import Scorecard, Recommendation
from src.utils.llm import call_agent_json
from src.utils.logging import log_agent_call

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


def load_prompt(agent_name: str, prompt_dir: Path) -> str:
    """Load an agent's prompt file."""
    cfg = AGENT_REGISTRY[agent_name]
    path = prompt_dir / cfg.prompt_file
    if not path.exists():
        raise FileNotFoundError(f"Prompt file missing: {path}")
    return path.read_text()


# ---------------------------------------------------------------------------
# Layer 1: Macro
# ---------------------------------------------------------------------------


async def run_macro_agent(
    agent_name: str,
    prompt_dir: Path,
    macro_data: dict[str, Any],
) -> dict[str, Any]:
    """Run a single macro agent and return its signal."""
    system_prompt = load_prompt(agent_name, prompt_dir)
    user_msg = (
        f"Today's date: {macro_data.get('as_of', 'unknown')}\n\n"
        f"## Market Data\n```json\n{json.dumps(macro_data, indent=2, default=str)}\n```\n\n"
        "Analyse the data above and provide your signal."
    )
    result = await call_agent_json(system_prompt, user_msg)
    result["_agent"] = agent_name
    log_agent_call(agent_name, "macro", {"as_of": macro_data.get("as_of")}, result)
    return result


async def run_macro_layer(
    prompt_dir: Path,
    macro_data: dict[str, Any],
    weights: dict[str, float],
) -> dict[str, Any]:
    """Run all macro agents in parallel and aggregate into a regime signal."""
    agents = agents_for_layer(Layer.MACRO)
    tasks = [run_macro_agent(a.name, prompt_dir, macro_data) for a in agents]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    signals = []
    agent_outputs = {}
    for result in results:
        if isinstance(result, Exception):
            logger.warning("Macro agent failed: %s", result)
            continue
        name = result.get("_agent", "unknown")
        agent_outputs[name] = result

        # Convert signal to numeric
        signal_str = result.get("signal", result.get("regime", "NEUTRAL")).upper()
        conviction = result.get("conviction", 50) / 100
        weight = weights.get(name, 1.0)

        if "BULL" in signal_str or "RISK_ON" in signal_str:
            numeric = conviction * weight
        elif "BEAR" in signal_str or "RISK_OFF" in signal_str:
            numeric = -conviction * weight
        else:
            numeric = 0.0

        signals.append(numeric)

    if not signals:
        regime = "NEUTRAL"
        regime_score = 0.0
    else:
        regime_score = sum(signals) / sum(weights.get(a.name, 1.0) for a in agents)
        if regime_score > REGIME_RISK_ON_THRESHOLD:
            regime = "RISK_ON"
        elif regime_score < REGIME_RISK_OFF_THRESHOLD:
            regime = "RISK_OFF"
        else:
            regime = "NEUTRAL"

    return {
        "regime": regime,
        "regime_score": round(regime_score, 4),
        "agent_outputs": agent_outputs,
    }


# ---------------------------------------------------------------------------
# Layer 2: Sector Desks
# ---------------------------------------------------------------------------

# Core focus tickers per sector desk (high-priority names always included)
SECTOR_TICKERS = {
    "semiconductor": ["NVDA", "AMD", "AVGO", "TSM", "ASML", "INTC", "QCOM", "MU"],
    "energy": ["XOM", "CVX", "SLB", "OXY", "COP", "EOG", "DVN", "HAL"],
    "biotech": ["AMGN", "GILD", "REGN", "VRTX", "MRNA", "BIIB", "ILMN", "BMRN"],
    "consumer": ["AMZN", "TSLA", "NKE", "SBUX", "TGT", "COST", "PG", "KO"],
    "industrials": ["LMT", "RTX", "BA", "CAT", "DE", "GE", "HON", "UNP"],
    "financials": ["JPM", "BAC", "GS", "MS", "BLK", "SCHW", "AXP", "V"],
}

SECTOR_ETFS = {
    "semiconductor": "SMH",
    "energy": "XLE",
    "biotech": "XBI",
    "consumer": "XLY",
    "industrials": "XLI",
    "financials": "XLF",
}

# S&P 500 universe cache (loaded once per session)
_sp500_universe: dict[str, list[str]] | None = None


def get_sector_tickers(sector: str, max_per_sector: int = 25) -> list[str]:
    """Get tickers for a sector: focus names + S&P 500 constituents.

    Returns focus tickers first (always included), then fills
    with broader S&P 500 names up to max_per_sector.
    """
    global _sp500_universe
    focus = SECTOR_TICKERS.get(sector, [])

    try:
        if _sp500_universe is None:
            from src.agents.universe import get_sp500_by_sector
            _sp500_universe = get_sp500_by_sector()

        broader = _sp500_universe.get(sector, [])
        result = list(focus)
        for ticker in broader:
            if ticker not in result and len(result) < max_per_sector:
                result.append(ticker)
        return result
    except Exception as e:
        logger.warning("Failed to load S&P 500 universe: %s. Using focus tickers only.", e)
        return focus


async def run_sector_agent(
    agent_name: str,
    prompt_dir: Path,
    macro_regime: dict[str, Any],
    sector_data: dict[str, Any],
) -> dict[str, Any]:
    """Run a single sector desk agent."""
    if agent_name == "relationship_mapper":
        return await run_relationship_mapper(prompt_dir, macro_regime, sector_data)

    system_prompt = load_prompt(agent_name, prompt_dir)
    user_msg = (
        f"## Macro Regime\n{json.dumps(macro_regime, indent=2)}\n\n"
        f"## Sector Data\n```json\n{json.dumps(sector_data, indent=2, default=str)}\n```\n\n"
        "Provide your sector view with top long and short picks."
    )
    result = await call_agent_json(system_prompt, user_msg)
    result["_agent"] = agent_name
    log_agent_call(agent_name, "sector", {"regime": macro_regime.get("regime")}, result)
    return result


async def run_relationship_mapper(
    prompt_dir: Path,
    macro_regime: dict[str, Any],
    all_sector_data: dict[str, Any],
) -> dict[str, Any]:
    """Run the relationship mapper across all sectors."""
    system_prompt = load_prompt("relationship_mapper", prompt_dir)
    user_msg = (
        f"## Macro Regime\n{json.dumps(macro_regime, indent=2)}\n\n"
        f"## All Sector Data\n```json\n{json.dumps(all_sector_data, indent=2, default=str)}\n```\n\n"
        "Identify cross-sector relationships, supply chain links, and ownership connections."
    )
    result = await call_agent_json(system_prompt, user_msg)
    result["_agent"] = "relationship_mapper"
    return result


async def run_sector_layer(
    prompt_dir: Path,
    macro_regime: dict[str, Any],
    sector_data: dict[str, dict],
) -> dict[str, Any]:
    """Run all sector desk agents in parallel."""
    agents = agents_for_layer(Layer.SECTOR)
    tasks = []
    for a in agents:
        data = sector_data.get(a.name, sector_data)
        tasks.append(run_sector_agent(a.name, prompt_dir, macro_regime, data))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    sector_picks = {}
    for result in results:
        if isinstance(result, Exception):
            logger.warning("Sector agent failed: %s", result)
            continue
        name = result.get("_agent", "unknown")
        sector_picks[name] = result

    return sector_picks


# ---------------------------------------------------------------------------
# Layer 3: Superinvestors
# ---------------------------------------------------------------------------


async def run_superinvestor_agent(
    agent_name: str,
    prompt_dir: Path,
    sector_picks: dict[str, Any],
    portfolio: dict[str, Any],
    macro_regime: dict[str, Any],
) -> dict[str, Any]:
    """Run a single superinvestor agent."""
    system_prompt = load_prompt(agent_name, prompt_dir)
    user_msg = (
        f"## Macro Regime\n{json.dumps(macro_regime, indent=2)}\n\n"
        f"## Sector Picks\n```json\n{json.dumps(sector_picks, indent=2, default=str)}\n```\n\n"
        f"## Current Portfolio\n```json\n{json.dumps(portfolio, indent=2, default=str)}\n```\n\n"
        "Review the sector picks through your investment philosophy. "
        "Provide verdicts on current positions and identify any missing names."
    )
    result = await call_agent_json(system_prompt, user_msg)
    result["_agent"] = agent_name
    log_agent_call(agent_name, "superinvestor", {"regime": macro_regime.get("regime")}, result)
    return result


async def run_superinvestor_layer(
    prompt_dir: Path,
    sector_picks: dict[str, Any],
    portfolio: dict[str, Any],
    macro_regime: dict[str, Any],
) -> dict[str, Any]:
    """Run all superinvestor agents in parallel."""
    agents = agents_for_layer(Layer.SUPERINVESTOR)
    tasks = [
        run_superinvestor_agent(a.name, prompt_dir, sector_picks, portfolio, macro_regime)
        for a in agents
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    views = {}
    for result in results:
        if isinstance(result, Exception):
            logger.warning("Superinvestor agent failed: %s", result)
            continue
        name = result.get("_agent", "unknown")
        views[name] = result

    return views


# ---------------------------------------------------------------------------
# Layer 4: Decision
# ---------------------------------------------------------------------------


async def run_cro(
    prompt_dir: Path,
    all_recommendations: dict[str, Any],
    portfolio: dict[str, Any],
    macro_regime: dict[str, Any],
) -> dict[str, Any]:
    """Run the CRO (adversarial risk review)."""
    system_prompt = load_prompt("cro", prompt_dir)
    user_msg = (
        f"## Macro Regime\n{json.dumps(macro_regime, indent=2)}\n\n"
        f"## All Recommendations\n```json\n{json.dumps(all_recommendations, indent=2, default=str)}\n```\n\n"
        f"## Current Portfolio\n```json\n{json.dumps(portfolio, indent=2, default=str)}\n```\n\n"
        "Review all recommendations. Attack each idea. Identify risks. "
        "Flag any recommendations that should be blocked."
    )
    result = await call_agent_json(system_prompt, user_msg)
    result["_agent"] = "cro"
    log_agent_call("cro", "decision", {"regime": macro_regime.get("regime")}, result)
    return result


async def run_alpha_discovery(
    prompt_dir: Path,
    all_recommendations: dict[str, Any],
    portfolio: dict[str, Any],
    macro_regime: dict[str, Any],
) -> dict[str, Any]:
    """Run the Alpha Discovery agent."""
    system_prompt = load_prompt("alpha_discovery", prompt_dir)
    user_msg = (
        f"## Macro Regime\n{json.dumps(macro_regime, indent=2)}\n\n"
        f"## Current Recommendations\n```json\n{json.dumps(all_recommendations, indent=2, default=str)}\n```\n\n"
        f"## Current Portfolio\n```json\n{json.dumps(portfolio, indent=2, default=str)}\n```\n\n"
        "Find names NOT mentioned by other agents that deserve attention. "
        "Focus on overlooked opportunities or risks."
    )
    result = await call_agent_json(system_prompt, user_msg)
    result["_agent"] = "alpha_discovery"
    log_agent_call("alpha_discovery", "decision", {"regime": macro_regime.get("regime")}, result)
    return result


async def run_cio(
    prompt_dir: Path,
    macro_regime: dict[str, Any],
    sector_picks: dict[str, Any],
    superinvestor_views: dict[str, Any],
    cro_review: dict[str, Any],
    alpha_ideas: dict[str, Any],
    portfolio: dict[str, Any],
    weights: dict[str, float],
) -> dict[str, Any]:
    """Run the CIO for final portfolio decisions."""
    system_prompt = load_prompt("cio", prompt_dir)
    user_msg = (
        f"## Macro Regime\n{json.dumps(macro_regime, indent=2)}\n\n"
        f"## Sector Picks (Darwinian-weighted)\n```json\n{json.dumps(sector_picks, indent=2, default=str)}\n```\n\n"
        f"## Superinvestor Views\n```json\n{json.dumps(superinvestor_views, indent=2, default=str)}\n```\n\n"
        f"## CRO Risk Review\n```json\n{json.dumps(cro_review, indent=2, default=str)}\n```\n\n"
        f"## Alpha Discovery\n```json\n{json.dumps(alpha_ideas, indent=2, default=str)}\n```\n\n"
        f"## Current Portfolio\n```json\n{json.dumps(portfolio, indent=2, default=str)}\n```\n\n"
        f"## Agent Darwinian Weights\n```json\n{json.dumps(weights, indent=2)}\n```\n\n"
        f"## Risk Limits\n"
        f"- Max position: {MAX_POSITION_PCT*100}% of portfolio\n"
        f"- Max gross exposure: {MAX_GROSS_EXPOSURE}\n"
        f"- Max net exposure: {MAX_NET_EXPOSURE}\n"
        f"- Max positions: {MAX_POSITIONS}\n"
        f"- Min cash reserve: {MIN_CASH_RESERVE_PCT*100}%\n\n"
        "Synthesise all inputs. Make final BUY/SELL/HOLD decisions with position sizes."
    )
    result = await call_agent_json(system_prompt, user_msg, max_tokens=8192)
    result["_agent"] = "cio"
    log_agent_call("cio", "decision", {"regime": macro_regime.get("regime")}, result)
    return result


async def run_decision_layer(
    prompt_dir: Path,
    macro_regime: dict[str, Any],
    sector_picks: dict[str, Any],
    superinvestor_views: dict[str, Any],
    portfolio: dict[str, Any],
    weights: dict[str, float],
) -> dict[str, Any]:
    """Run the full decision layer: CRO + Alpha Discovery in parallel, then CIO."""
    # Combine all recommendations for CRO and Alpha Discovery
    all_recs = {**sector_picks, **superinvestor_views}

    # CRO and Alpha Discovery run in parallel
    cro_result, alpha_result = await asyncio.gather(
        run_cro(prompt_dir, all_recs, portfolio, macro_regime),
        run_alpha_discovery(prompt_dir, all_recs, portfolio, macro_regime),
    )

    # CIO runs last with all inputs
    cio_result = await run_cio(
        prompt_dir, macro_regime, sector_picks, superinvestor_views,
        cro_result, alpha_result, portfolio, weights,
    )

    return {
        "cro": cro_result,
        "alpha_discovery": alpha_result,
        "cio": cio_result,
    }


# ---------------------------------------------------------------------------
# Full EOD cycle
# ---------------------------------------------------------------------------


async def run_eod_cycle(
    prompt_dir: Path,
    macro_data: dict[str, Any],
    sector_data: dict[str, dict],
    portfolio: dict[str, Any],
    weights: dict[str, float],
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the complete end-of-day cycle across all 4 layers.

    Returns the full pipeline output including regime, sector picks,
    superinvestor views, and CIO decisions.
    """
    settings = Settings()
    state = state_dir or settings.state_dir
    state.mkdir(parents=True, exist_ok=True)

    # Layer 1: Macro
    logger.info("=== Layer 1: Macro ===")
    macro_result = await run_macro_layer(prompt_dir, macro_data, weights)
    (state / "macro_regime.json").write_text(json.dumps(macro_result, indent=2, default=str))

    # Layer 2: Sector Desks
    logger.info("=== Layer 2: Sector Desks === (regime=%s)", macro_result["regime"])
    sector_picks = await run_sector_layer(prompt_dir, macro_result, sector_data)
    (state / "sector_picks.json").write_text(json.dumps(sector_picks, indent=2, default=str))

    # Layer 3: Superinvestors
    logger.info("=== Layer 3: Superinvestors ===")
    superinvestor_views = await run_superinvestor_layer(
        prompt_dir, sector_picks, portfolio, macro_result,
    )
    (state / "superinvestor_views.json").write_text(
        json.dumps(superinvestor_views, indent=2, default=str)
    )

    # Layer 4: Decision
    logger.info("=== Layer 4: Decision ===")
    decision = await run_decision_layer(
        prompt_dir, macro_result, sector_picks, superinvestor_views, portfolio, weights,
    )
    (state / "cro_review.json").write_text(json.dumps(decision["cro"], indent=2, default=str))
    (state / "portfolio_actions.json").write_text(
        json.dumps(decision["cio"], indent=2, default=str)
    )

    return {
        "macro": macro_result,
        "sector_picks": sector_picks,
        "superinvestor_views": superinvestor_views,
        "decision": decision,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for argos-eod."""
    import argparse

    parser = argparse.ArgumentParser(description="Run ARGOS end-of-day cycle")
    parser.parse_args()

    from src.utils.logging import setup_logging
    setup_logging()

    settings = Settings()
    logger.info("Starting EOD cycle")

    # This would be called with real data in production
    asyncio.run(_run_live(settings))


async def _run_live(settings: Settings) -> None:
    """Run a live EOD cycle with real market data."""
    from src.agents.market_data import MarketDataProvider

    provider = MarketDataProvider(settings)
    try:
        macro_data = await provider.get_macro_data()

        # Fetch sector data (S&P 500 universe + sector ETFs)
        all_sector_data = {}
        for sector in SECTOR_TICKERS:
            tickers = get_sector_tickers(sector)
            etf = SECTOR_ETFS.get(sector)
            logger.info("Sector %s: %d tickers, ETF=%s", sector, len(tickers), etf)
            all_sector_data[sector] = await provider.get_sector_data(tickers, etf_ticker=etf)

        # Load current portfolio
        portfolio_path = settings.state_dir / "portfolio.json"
        if portfolio_path.exists():
            portfolio = json.loads(portfolio_path.read_text())
        else:
            portfolio = {"cash": 1_000_000, "positions": {}, "total_value": 1_000_000}

        # Load weights
        scorecard = Scorecard(settings.state_dir)
        weights = scorecard.get_all_weights()

        result = await run_eod_cycle(
            settings.prompt_dir, macro_data, all_sector_data, portfolio, weights, settings.state_dir,
        )
        logger.info("EOD cycle complete. Regime: %s", result["macro"]["regime"])
    finally:
        await provider.close()


if __name__ == "__main__":
    main()
