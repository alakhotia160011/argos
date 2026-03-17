# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ARGOS is a self-improving AI trading agent framework that applies Karpathy's autoresearch methodology to financial markets. 25 AI agents (powered by Claude) are organized into a 4-layer pipeline. Prompts are treated as weights and Sharpe ratio is the loss function — the system evolves its own prompts through Darwinian selection and targeted mutation.

## Commands

```bash
# Install
pip install -e ".[dev,data]"

# Download/update market data
argos-data                          # Full update (daily + 1h + fundamentals + macro)
argos-data --start 2025-01-01       # Custom start date
argos-data --only prices            # Only price data
argos-data --coverage               # Check data coverage

# Run backtest
argos-backtest                      # Default date range
argos-backtest --start 2026-01-01 --end 2026-03-12 --cash 500000

# Run live EOD cycle
argos-eod --dry-run                 # Log trades without executing
argos-eod --execute                 # Execute via Alpaca (paper by default)

# Lint
ruff check src/
ruff format src/
```

## Architecture: 4-Layer Agent Pipeline

Each trading day, the pipeline runs sequentially through 4 layers:

**Layer 1 — MACRO (10 agents, parallel):** Central Bank, Geopolitical, China, Dollar, Yield Curve, Commodities, Volatility, Emerging Markets, News Sentiment, Institutional Flow. Outputs an aggregated regime signal: `RISK_ON` / `RISK_OFF` / `NEUTRAL`.

**Layer 2 — SECTOR DESKS (7 agents, parallel):** Semiconductor, Energy, Biotech, Consumer, Industrials, Financials, Relationship Mapper. Each desk covers 8-20 tickers and generates LONG/SHORT picks informed by the regime signal.

**Layer 3 — SUPERINVESTORS (4 agents, parallel):** Druckenmiller, Aschenbrenner, Baker, Ackman. Filter sector picks through distinct investment philosophies.

**Layer 4 — DECISION (4 agents, sequential):** CRO (adversarial risk), Alpha Discovery (missed names), Autonomous Execution (trade sizing), CIO (final synthesis).

## Key Modules

- **`src/agents/backtest_loop.py`** — Main training loop. Iterates trading days, runs EOD cycle, executes paper trades, scores recommendations against forward returns, updates Darwinian weights, triggers autoresearch.
- **`src/agents/eod_cycle.py`** — Orchestrates the 4-layer pipeline for a single day. Runs agents via async, aggregates signals between layers.
- **`src/agents/market_data.py`** — Unified data provider (FMP, FRED, yfinance, Finnhub, Polygon). `get_quotes_as_of()` enforces look-ahead prevention for backtesting.
- **`src/agents/eod_store.py`** — Downloads and incrementally updates EOD prices, fundamentals, and 29 FRED macro series. Entry point for `argos-data`.
- **`src/agents/scorecard.py`** — Tracks per-agent Sharpe ratios (60-day rolling) and manages Darwinian weights (daily quartile-based adjustment, floor 0.3, ceiling 2.5).
- **`src/agents/autoresearch.py`** — Identifies worst agent, generates prompt mutation via Claude, creates git branch, observes 5 days, keeps if Sharpe improves, reverts if not.
- **`src/agents/universe.py`** — S&P 500 constituents + GICS sector mapping to agent desks.
- **`src/config.py`** — Settings (pydantic, loads `.env`), agent registry, all constants (position limits, exposure caps, weight bounds, autoresearch params).
- **`src/utils/llm.py`** — `call_agent()` / `call_agent_json()` wrappers around Anthropic API with retry and concurrency control.
- **`src/broker/alpaca.py`** — Live order execution with pre-trade risk checks.

## Autoresearch Loop

The core innovation: automatic prompt evolution measured by Sharpe ratio.
1. Identify worst-performing agent (lowest rolling Sharpe, not in cooldown)
2. Generate a targeted prompt modification via Claude (surgical edit, not full rewrite)
3. Create a git feature branch, commit the modified prompt
4. Observe for 5 trading days
5. If Sharpe improves → merge; otherwise → revert branch

Agent prompts live in `prompts/trained/` — these files are actively modified by the autoresearch engine.

## Bias Controls

All backtesting enforces `as_of=trading_date` to prevent look-ahead:
- Prices: local EOD store only via `get_quotes_as_of()`
- Macro (FRED): filtered to `<= trading_date`
- Known residual biases: survivorship (S&P 500 uses today's list), fundamentals look-ahead (yfinance returns latest), news not date-filtered

## Risk Parameters (from config.py)

- Max single position: 10% of portfolio
- Max gross exposure: 1.5x, max net exposure: 0.8x
- Min cash reserve: 5%, max positions: 30
- Drawdown threshold: -10% triggers 50% gross exposure cut

## Environment Variables

Required: `ANTHROPIC_API_KEY`. Optional but recommended: `FMP_API_KEY`, `FRED_API_KEY`. Optional: `FINNHUB_API_KEY`, `POLYGON_API_KEY`, `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`. See `.env.example` for the full list.

## Output Files

- `results/summary.json` — Performance metrics (return, Sharpe, drawdown, agent weights)
- `results/equity_curve.png`, `results/agent_weights.png` — Visualizations
- `results/portfolio_trajectory.csv` — Daily NAV series
- `data/logs/` — Structured JSONL logs (agent calls, trades, performance, weights)
