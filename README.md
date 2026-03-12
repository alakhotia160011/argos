# ARGOS - Self-Improving AI Trading Agents

[Karpathy's autoresearch](https://github.com/karpathy/autoresearch) applied to financial markets. The agent prompts are the weights. Sharpe ratio is the loss function. No GPU needed.

---

## What Is This?

ARGOS is a framework for autonomous AI trading agents that improve their own prompts through market feedback.

25 agents debate markets daily across 4 layers. Every recommendation is scored against real outcomes. The worst-performing agent gets its prompt rewritten. If performance improves, the git commit survives. If not, git revert.

---

## Architecture

### Layer 1 - Macro (10 agents)
Central bank, geopolitical, China, dollar, yield curve, commodities, volatility, emerging markets, news sentiment, institutional flow.

These agents set the regime. Risk on or risk off? What's the macro backdrop?

### Layer 2 - Sector Desks (7 agents)
Semiconductor, energy, biotech, consumer, industrials, financials, plus a relationship mapper that tracks supply chains, ownership, analyst coverage, and competitive dynamics.

They take the macro regime from Layer 1 and identify the best names within each sector.

### Layer 3 - Superinvestors (4 agents)
- **Druckenmiller** - macro/momentum: what's the big asymmetric trade?
- **Aschenbrenner** - AI/compute: who benefits from the capex cycle?
- **Baker** - deep tech/biotech: who has real IP moats?
- **Ackman** - quality compounder: pricing power + FCF + catalyst?

They filter sector picks through different investment philosophies.

### Layer 4 - Decision (4 agents)
- **CRO** - adversarial risk officer: attacks every idea, finds correlated risks
- **Alpha Discovery** - finds names nobody else mentioned
- **Autonomous Execution** - converts signals to sized trades
- **CIO** - synthesises all prior layers, weighted by Darwinian agent scores, makes the final call

Each layer feeds into the next. The CIO only sees ideas that survived three rounds of analysis.

---

## The Autoresearch Loop

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch). Same pattern, different domain.

**Karpathy's version:**
- Agent modifies training code (train.py)
- 5-minute GPU training run
- Check validation loss
- Keep or revert

**Ours:**
- System identifies worst agent by rolling Sharpe
- Generates one targeted prompt modification
- Runs for 5 trading days
- Checks if agent's Sharpe improved
- Keep (git commit) or revert (git reset)

The agent prompts are the weights being optimised. Each trading day is one training iteration.

**Darwinian Weights:**
Each agent has a weight between 0.3 (minimum, near-silenced) and 2.5 (maximum, highly trusted). After each day, top quartile agents get weight x 1.05. Bottom quartile get weight x 0.95. The CIO proportionally weights agent input by these scores.

Over time, good agents get louder. Bad agents get quieter. The system learns who to trust.

---

## Data Pipeline

### Market Data
- **FRED** - 30 macro series (rates, credit, labor, inflation, sentiment)
- **yfinance** - indices, sector ETFs, commodities, bonds, currencies, volatility
- **FMP** - fundamentals, financials (with stable API fallback)

### EOD Data Store
Pre-cached local data store for all S&P 500 + macro tickers:
- **Daily OHLCV** - full history per ticker
- **1-Hour Intraday** - granular price action
- **Fundamentals** - financials, analyst ratings, institutional holdings, options data

Incremental updates — only fetches new days since last stored date.

### Stock Universe
- Full **S&P 500** constituents (fetched from Wikipedia, mapped via GICS sectors)
- **Sector ETFs** (SMH, XLE, XBI, XLY, XLI, XLF) with per-desk metrics
- **37 macro market tickers** (indices, commodities, bonds, currencies)

---

## Project Structure

```
argos/
├── src/
│   ├── config.py                  # Settings, agent registry, constants
│   ├── agents/
│   │   ├── eod_cycle.py           # 4-layer daily pipeline orchestration
│   │   ├── backtest_loop.py       # Portfolio simulation + training loop
│   │   ├── market_data.py         # Multi-source data provider (FMP, FRED, yfinance)
│   │   ├── eod_store.py           # Incremental EOD data store
│   │   ├── scorecard.py           # Agent Sharpe ratios + Darwinian weights
│   │   ├── autoresearch.py        # Self-improving prompt evolution
│   │   └── universe.py            # S&P 500 universe + GICS sector mapping
│   └── utils/
│       ├── llm.py                 # Anthropic API wrapper with retries
│       ├── git_ops.py             # Git branching for autoresearch
│       └── logging.py             # Structured logging (JSONL)
├── prompts/trained/               # 25 agent prompts (evolutionary optimised)
├── data/
│   ├── eod/                       # Cached daily + hourly price data
│   ├── fundamentals/              # Cached company fundamentals
│   ├── state/                     # Live pipeline state (weights, portfolio, regime)
│   ├── logs/                      # Structured logs (trades, performance, agent calls)
│   └── cache/                     # S&P 500 constituent cache
├── results/                       # Backtest outputs (summary, charts, trajectory)
├── pyproject.toml
└── .env                           # API keys (not committed)
```

---

## Quick Start

### 1. Install

```bash
pip install -e ".[dev,data]"
```

### 2. Set up API keys

```bash
cp .env.example .env
# Add your keys: ANTHROPIC_API_KEY, FRED_API_KEY, FMP_API_KEY (optional)
```

### 3. Download market data

```bash
# Initial download (daily + hourly + fundamentals for ~544 tickers)
python -m src.agents.eod_store

# Incremental update (only fetches new days)
python -m src.agents.eod_store

# Check coverage
python -m src.agents.eod_store --coverage
```

### 4. Run a backtest

```bash
# Full backtest (uses Anthropic API — each day calls 25 agents)
argos-backtest --start 2026-01-01 --end 2026-03-12

# Or run directly
python -m src.agents.backtest_loop --start 2026-01-01 --end 2026-03-12
```

### 5. Run live EOD cycle

```bash
argos-eod
```

---

## Outputs

### Results (`results/`)
- `summary.json` - return, Sharpe, max drawdown, agent weights
- `equity_curve.png` - NAV, daily returns, drawdown chart
- `agent_weights.png` - Darwinian weights + Sharpe ratios
- `exposure.png` - gross/net exposure over time
- `portfolio_trajectory.csv` - daily NAV series

### Logs (`data/logs/`)
- `argos_YYYY-MM-DD.log` - human-readable daily log
- `trades.jsonl` - every trade execution
- `performance.jsonl` - daily NAV/exposure snapshots
- `agent_calls.jsonl` - all agent inputs/outputs
- `weights.jsonl` - Darwinian weight changes
- `autoresearch.jsonl` - prompt modification events
- `errors.jsonl` - failures with context

---

## Tech Stack

- **Agents:** Claude Sonnet (Anthropic API)
- **Data:** FRED, yfinance, FMP (optional)
- **Version Control:** Git feature branches for autoresearch tracking
- **Language:** Python 3.11+

---

## License

MIT License - see [LICENSE](LICENSE) for details.
