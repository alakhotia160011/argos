"""Main backtest training loop.

Simulates N trading days:
1. Load historical market data for the date
2. Run the 25-agent EOD cycle
3. Execute portfolio actions (paper trading)
4. Score recommendations against actual future returns
5. Update Darwinian weights
6. Trigger autoresearch if conditions met
7. Log everything

Bias controls:
  - All data functions receive `as_of=trading_date` to prevent look-ahead.
  - Prices come from the local EOD store via `get_quotes_as_of()`.
  - FRED macro data is filtered to `<= trading_date`.
  - Sector data uses `trading_date` as the end of the lookback window.

Known residual biases (not fixable without point-in-time datasets):
  - Survivorship: S&P 500 universe is today's membership list, not point-in-time.
  - Fundamentals look-ahead: yfinance returns the latest financials, not
    the version available on `trading_date`.
  These biases are minor for short backtests (< 1 year) but significant for
  multi-year tests. Use results directionally, not as precise P&L forecasts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import (
    DEFAULT_INITIAL_CASH,
    MAX_POSITION_PCT,
    MAX_GROSS_EXPOSURE,
    MAX_NET_EXPOSURE,
    MAX_POSITIONS,
    MIN_CASH_RESERVE_PCT,
    DRAWDOWN_THRESHOLD_PCT,
    DRAWDOWN_LOOKBACK_DAYS,
    DRAWDOWN_EXPOSURE_CUT,
    Settings,
)
from src.agents.eod_cycle import run_eod_cycle, SECTOR_TICKERS, SECTOR_ETFS, get_sector_tickers
from src.agents.scorecard import Scorecard, Recommendation
from src.agents.autoresearch import AutoresearchEngine
from src.agents.market_data import MarketDataProvider, BacktestDataLoader
from src.utils.logging import (
    setup_logging, log_trade, log_performance, log_error,
    log_weight_update, log_autoresearch,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Portfolio simulator
# ---------------------------------------------------------------------------


@dataclass
class Position:
    ticker: str
    shares: int
    entry_price: float
    entry_date: str
    direction: str = "LONG"  # LONG or SHORT

    @property
    def is_short(self) -> bool:
        return self.direction == "SHORT"


@dataclass
class Portfolio:
    cash: float = DEFAULT_INITIAL_CASH
    positions: dict[str, Position] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    trade_count: int = 0

    def mark_to_market(self, prices: dict[str, float]) -> float:
        """Calculate portfolio value using current prices.

        Cash already includes short sale proceeds, so short positions
        contribute only their unrealised P&L (entry - current) * shares.
        Long positions contribute current_price * shares.
        """
        value = self.cash
        for ticker, pos in self.positions.items():
            price = prices.get(ticker, pos.entry_price)
            if pos.is_short:
                # Cash already has the proceeds; only add unrealised P&L
                value += (pos.entry_price - price) * pos.shares
            else:
                value += price * pos.shares
        return value

    def gross_exposure(self, prices: dict[str, float]) -> float:
        """Total absolute exposure as fraction of portfolio value."""
        total = self.mark_to_market(prices)
        if total <= 0:
            return 0.0
        return sum(
            abs(prices.get(t, p.entry_price) * p.shares) for t, p in self.positions.items()
        ) / total

    def net_exposure(self, prices: dict[str, float]) -> float:
        """Net long/short exposure as fraction of portfolio value."""
        total = self.mark_to_market(prices)
        if total <= 0:
            return 0.0
        net = sum(
            prices.get(t, p.entry_price) * p.shares * (-1 if p.is_short else 1)
            for t, p in self.positions.items()
        )
        return net / total

    def execute_action(
        self, ticker: str, action: str, shares: int, price: float, date_str: str,
        current_prices: dict[str, float] | None = None,
    ) -> bool:
        """Execute a portfolio action. Returns True if successful.

        current_prices: used to compute mark-to-market for budget checks.
        Falls back to cash-based check if not provided.
        """
        action = action.upper()
        # Budget is based on portfolio value, not raw cash (raw cash can be
        # inflated by short sale proceeds).
        if current_prices:
            budget = self.mark_to_market(current_prices) * (1 - MIN_CASH_RESERVE_PCT)
        else:
            budget = self.cash * (1 - MIN_CASH_RESERVE_PCT)

        if action == "BUY":
            cost = price * shares
            available = min(budget, self.cash)  # can't spend more cash than we have
            if cost > available:
                max_shares = int(available / price)
                if max_shares <= 0:
                    logger.warning("Insufficient cash for %s BUY %d @ %.2f", ticker, shares, price)
                    return False
                shares = max_shares

            if ticker in self.positions:
                # Add to existing position
                existing = self.positions[ticker]
                total_shares = existing.shares + shares
                avg_price = (existing.entry_price * existing.shares + price * shares) / total_shares
                existing.shares = total_shares
                existing.entry_price = avg_price
            else:
                if len(self.positions) >= MAX_POSITIONS:
                    logger.warning("Max positions reached, cannot add %s", ticker)
                    return False
                self.positions[ticker] = Position(ticker, shares, price, date_str, "LONG")

            self.cash -= price * shares
            self.trade_count += 1
            return True

        elif action == "SELL":
            if ticker not in self.positions:
                logger.warning("Cannot sell %s: not in portfolio", ticker)
                return False
            pos = self.positions[ticker]
            sell_shares = min(shares, pos.shares)
            self.cash += price * sell_shares
            pos.shares -= sell_shares
            if pos.shares <= 0:
                del self.positions[ticker]
            self.trade_count += 1
            return True

        elif action == "SHORT":
            if ticker in self.positions and not self.positions[ticker].is_short:
                logger.warning("Cannot short %s: already long", ticker)
                return False
            if ticker in self.positions and self.positions[ticker].is_short:
                # Average into existing short position
                existing = self.positions[ticker]
                total_shares = existing.shares + shares
                avg_price = (existing.entry_price * existing.shares + price * shares) / total_shares
                existing.shares = total_shares
                existing.entry_price = avg_price
            else:
                if len(self.positions) >= MAX_POSITIONS:
                    logger.warning("Max positions reached, cannot short %s", ticker)
                    return False
                self.positions[ticker] = Position(ticker, shares, price, date_str, "SHORT")
            self.cash += price * shares  # Receive cash from short sale
            self.trade_count += 1
            return True

        elif action == "COVER":
            if ticker not in self.positions or not self.positions[ticker].is_short:
                logger.warning("Cannot cover %s: not short", ticker)
                return False
            pos = self.positions[ticker]
            cover_shares = min(shares, pos.shares)
            self.cash -= price * cover_shares  # Pay to cover
            pos.shares -= cover_shares
            if pos.shares <= 0:
                del self.positions[ticker]
            self.trade_count += 1
            return True

        return False

    def to_dict(self, prices: dict[str, float] | None = None) -> dict:
        prices = prices or {}
        return {
            "cash": round(self.cash, 2),
            "positions": {
                t: {
                    "shares": p.shares,
                    "entry_price": p.entry_price,
                    "current_price": prices.get(t, p.entry_price),
                    "direction": p.direction,
                    "pnl": round(
                        (prices.get(t, p.entry_price) - p.entry_price) * p.shares
                        * (-1 if p.is_short else 1), 2
                    ),
                }
                for t, p in self.positions.items()
            },
            "total_value": round(self.mark_to_market(prices), 2),
            "gross_exposure": round(self.gross_exposure(prices), 4),
            "net_exposure": round(self.net_exposure(prices), 4),
            "num_positions": len(self.positions),
        }

    def snapshot(self, day: int, date_str: str, prices: dict[str, float]) -> dict:
        """Record a daily snapshot."""
        snap = {
            "day": day,
            "date": date_str,
            **self.to_dict(prices),
        }
        self.history.append(snap)
        return snap


# ---------------------------------------------------------------------------
# Backtest loop
# ---------------------------------------------------------------------------


async def run_backtest(
    start_date: date,
    end_date: date,
    settings: Settings | None = None,
    initial_cash: float = DEFAULT_INITIAL_CASH,
) -> dict[str, Any]:
    """Run the full backtest from start_date to end_date.

    Each trading day:
    1. Fetch/load market data
    2. Run 25-agent EOD cycle
    3. Execute CIO portfolio actions
    4. Score past recommendations (fill forward returns)
    5. Update Darwinian weights
    6. Trigger autoresearch
    """
    settings = settings or Settings()
    prompt_dir = settings.prompt_dir
    state_dir = settings.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    # Initialize components
    portfolio = Portfolio(cash=initial_cash)
    scorecard = Scorecard(state_dir)
    autoresearch = AutoresearchEngine(settings)
    data_provider = MarketDataProvider(settings)
    backtest_loader = BacktestDataLoader(settings.backtest_dir)

    # Generate trading days (skip weekends)
    trading_days = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:  # Monday=0, Friday=4
            trading_days.append(current)
        current += timedelta(days=1)

    logger.info("Backtest: %d trading days from %s to %s", len(trading_days), start_date, end_date)

    # Track equity curve
    equity_curve = []

    try:
        for day_num, trading_date in enumerate(trading_days, 1):
            logger.info("=== Day %d: %s ===", day_num, trading_date)

            # 1. Fetch market data (try cache first, then live)
            # Pass trading_date as as_of to prevent look-ahead bias
            macro_data = backtest_loader.load_macro_snapshot(trading_date)
            if not macro_data:
                try:
                    macro_data = await data_provider.get_macro_data(as_of=trading_date)
                    backtest_loader.save_macro_snapshot(trading_date, macro_data)
                except Exception as e:
                    logger.warning("Failed to fetch macro data for %s: %s", trading_date, e)
                    macro_data = {"as_of": trading_date.isoformat()}

            # Get sector data (S&P 500 universe) — pass trading_date to prevent look-ahead
            all_sector_data = {}
            all_tickers = set()
            for sector in SECTOR_TICKERS:
                tickers = get_sector_tickers(sector)
                all_tickers.update(tickers)
                etf = SECTOR_ETFS.get(sector)
                try:
                    all_sector_data[sector] = await data_provider.get_sector_data(
                        tickers, etf_ticker=etf, as_of=trading_date,
                    )
                except Exception:
                    all_sector_data[sector] = {}

            # Add portfolio positions to price fetch
            for t in portfolio.positions:
                all_tickers.add(t)

            # Use store-based quotes with date cutoff to avoid look-ahead bias
            prices = {}
            try:
                quotes = await data_provider.get_quotes_as_of(list(all_tickers), trading_date)
                prices = {t: q.get("price", 0) for t, q in quotes.items() if q.get("price")}
            except Exception:
                pass

            # 2. Run EOD cycle
            weights = scorecard.get_all_weights()
            portfolio_dict = portfolio.to_dict(prices)

            try:
                result = await run_eod_cycle(
                    prompt_dir, macro_data, all_sector_data, portfolio_dict, weights, state_dir,
                )
            except Exception as e:
                logger.error("EOD cycle failed on day %d: %s", day_num, e)
                log_error("eod_cycle", str(e), {"day": day_num, "date": trading_date.isoformat()})
                equity_curve.append({
                    "day": day_num,
                    "date": trading_date.isoformat(),
                    "value": portfolio.mark_to_market(prices),
                })
                continue

            # 3. Execute CIO actions
            cio_output = result.get("decision", {}).get("cio", {})
            actions = cio_output.get("portfolio_actions", cio_output.get("actions", []))

            for action_item in actions:
                if not isinstance(action_item, dict):
                    continue
                ticker = action_item.get("ticker", "")
                action = action_item.get("action", "HOLD")
                shares = action_item.get("shares", 0)
                if not ticker or action == "HOLD" or shares <= 0:
                    continue

                price = prices.get(ticker, 0)
                if price <= 0:
                    continue

                # Check position size limit (applies to BUY and SHORT)
                position_value = price * shares
                total_value = portfolio.mark_to_market(prices)
                if total_value > 0 and position_value / total_value > MAX_POSITION_PCT:
                    shares = int(total_value * MAX_POSITION_PCT / price)
                    if shares <= 0:
                        continue

                success = portfolio.execute_action(
                    ticker, action, shares, price, trading_date.isoformat(),
                    current_prices=prices,
                )
                if success:
                    logger.info("Executed: %s %s %d @ %.2f", action, ticker, shares, price)
                    log_trade(
                        as_of=trading_date,
                        ticker=ticker,
                        action=action,
                        direction=action_item.get("direction", action),
                        shares=shares,
                        price=price,
                        notional=price * shares,
                        reason=action_item.get("rationale", ""),
                        agent_source="cio",
                        conviction=action_item.get("conviction"),
                    )

            # 3b. Post-trade exposure enforcement
            _enforce_exposure_limits(portfolio, prices, trading_date.isoformat())

            # 4. Record recommendations from all agents for scoring
            _record_agent_recommendations(result, scorecard, trading_date, prices)

            # 5. Fill forward returns for past recommendations
            await _fill_forward_returns(scorecard, trading_date, prices, data_provider)

            # 6. Update Darwinian weights
            scorecard.update_darwinian_weights()

            # 7. Autoresearch
            evaluated = autoresearch.maybe_evaluate(scorecard, day_num)
            if evaluated:
                logger.info(
                    "Autoresearch evaluation: %s %s (Sharpe %.3f → %.3f)",
                    "KEPT" if evaluated.kept else "REVERTED",
                    evaluated.agent,
                    evaluated.pre_sharpe or 0,
                    evaluated.post_sharpe or 0,
                )
                log_autoresearch(
                    as_of=trading_date,
                    agent_name=evaluated.agent,
                    action="keep" if evaluated.kept else "revert",
                    details={
                        "pre_sharpe": evaluated.pre_sharpe,
                        "post_sharpe": evaluated.post_sharpe,
                    },
                )

            await autoresearch.maybe_propose(scorecard, day_num, trading_date)

            # 8. Drawdown protection
            _check_drawdown(portfolio, equity_curve, prices, day_num)

            # 9. Record daily snapshot
            snap = portfolio.snapshot(day_num, trading_date.isoformat(), prices)
            equity_curve.append({
                "day": day_num,
                "date": trading_date.isoformat(),
                "value": snap["total_value"],
            })

            # Log performance
            prev_value = equity_curve[-2]["value"] if len(equity_curve) >= 2 else initial_cash
            daily_pnl = snap["total_value"] - prev_value
            daily_ret = daily_pnl / prev_value if prev_value > 0 else 0
            log_performance(
                as_of=trading_date,
                nav=snap["total_value"],
                cash=snap["cash"],
                num_positions=snap["num_positions"],
                gross_exposure=snap["gross_exposure"],
                net_exposure=snap["net_exposure"],
                daily_pnl=daily_pnl,
                daily_return_pct=daily_ret * 100,
                positions=snap.get("positions", {}),
                regime=result.get("macro", {}).get("regime", ""),
            )

            # Save state
            scorecard.save()
            autoresearch.save()
            (state_dir / "portfolio.json").write_text(
                json.dumps(portfolio.to_dict(prices), indent=2)
            )

            if day_num % 20 == 0:
                logger.info(
                    "Progress: day %d/%d, value=%.2f, positions=%d",
                    day_num, len(trading_days), snap["total_value"], len(portfolio.positions),
                )

    finally:
        await data_provider.close()

    # Final results
    final_value = equity_curve[-1]["value"] if equity_curve else initial_cash
    total_return = (final_value - initial_cash) / initial_cash * 100

    # Compute additional stats
    values = [e["value"] for e in equity_curve]
    daily_returns = [
        (values[i] - values[i - 1]) / values[i - 1]
        for i in range(1, len(values)) if values[i - 1] > 0
    ]
    running_peak = initial_cash
    drawdowns = []
    for v in values:
        running_peak = max(running_peak, v)
        drawdowns.append((v - running_peak) / running_peak if running_peak > 0 else 0)
    max_drawdown = min(drawdowns) * 100 if drawdowns else 0

    ann_return = ((final_value / initial_cash) ** (252 / max(len(trading_days), 1)) - 1) * 100
    ann_vol = float(np.std(daily_returns) * np.sqrt(252) * 100) if daily_returns else 0
    sharpe = ann_return / ann_vol if ann_vol > 0 else 0

    summary = {
        "period": f"{start_date} to {end_date}",
        "trading_days": len(trading_days),
        "starting_value": initial_cash,
        "ending_value": round(final_value, 2),
        "total_return_pct": round(total_return, 2),
        "annualized_return_pct": round(ann_return, 2),
        "annualized_volatility_pct": round(ann_vol, 2),
        "sharpe_ratio": round(sharpe, 3),
        "max_drawdown_pct": round(max_drawdown, 2),
        "total_trades": portfolio.trade_count,
        "final_positions": len(portfolio.positions),
        "autoresearch": autoresearch.stats(),
        "final_agent_weights": scorecard.get_all_weights(),
    }

    # Save results
    results_dir = Path("results")
    results_dir.mkdir(exist_ok=True)
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (results_dir / "autoresearch_log.json").write_text(
        json.dumps([asdict(m) for m in autoresearch.modifications], indent=2)
    )

    # Equity curve CSV
    df = pd.DataFrame(equity_curve)
    df.to_csv(results_dir / "portfolio_trajectory.csv", index=False)

    # Generate charts
    _generate_charts(equity_curve, scorecard, portfolio, results_dir, initial_cash)

    logger.info("Backtest complete: %.2f%% return over %d days", total_return, len(trading_days))
    return summary


def _generate_charts(
    equity_curve: list[dict],
    scorecard: Scorecard,
    portfolio: Portfolio,
    results_dir: Path,
    initial_cash: float,
) -> None:
    """Generate performance charts and save to results/."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        logger.warning("matplotlib not installed — skipping charts. Install with: pip install matplotlib")
        return

    dates = [pd.Timestamp(e["date"]) for e in equity_curve]
    values = [e["value"] for e in equity_curve]

    if len(dates) < 2:
        logger.info("Not enough data points for charts")
        return

    # ── 1. Equity Curve ──────────────────────────────────────────────────
    fig, axes = plt.subplots(3, 1, figsize=(14, 12), gridspec_kw={"height_ratios": [3, 1, 1]})
    fig.suptitle("ARGOS Portfolio Performance", fontsize=16, fontweight="bold")

    # NAV
    ax1 = axes[0]
    ax1.plot(dates, values, color="#2962FF", linewidth=1.5, label="Portfolio NAV")
    ax1.axhline(y=initial_cash, color="gray", linestyle="--", linewidth=0.8, alpha=0.6, label="Starting Capital")
    ax1.fill_between(
        dates, initial_cash, values,
        where=[v >= initial_cash for v in values],
        alpha=0.15, color="green",
    )
    ax1.fill_between(
        dates, initial_cash, values,
        where=[v < initial_cash for v in values],
        alpha=0.15, color="red",
    )
    ax1.set_ylabel("Portfolio Value ($)")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))

    # Daily returns
    ax2 = axes[1]
    daily_rets = [0] + [
        (values[i] - values[i - 1]) / values[i - 1] * 100
        for i in range(1, len(values))
    ]
    colors = ["green" if r >= 0 else "red" for r in daily_rets]
    ax2.bar(dates, daily_rets, color=colors, alpha=0.7, width=0.8)
    ax2.set_ylabel("Daily Return (%)")
    ax2.axhline(y=0, color="black", linewidth=0.5)
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))

    # Drawdown
    ax3 = axes[2]
    peak = initial_cash
    drawdowns = []
    for v in values:
        peak = max(peak, v)
        drawdowns.append((v - peak) / peak * 100)
    ax3.fill_between(dates, 0, drawdowns, color="red", alpha=0.3)
    ax3.plot(dates, drawdowns, color="red", linewidth=0.8)
    ax3.set_ylabel("Drawdown (%)")
    ax3.set_xlabel("Date")
    ax3.grid(True, alpha=0.3)
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))

    plt.tight_layout()
    fig.savefig(results_dir / "equity_curve.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info("Saved equity_curve.png")

    # ── 2. Agent Weights Evolution ───────────────────────────────────────
    weights = scorecard.get_all_weights()
    agents = sorted(weights.keys(), key=lambda a: weights[a], reverse=True)

    fig, axes = plt.subplots(2, 1, figsize=(14, 10))
    fig.suptitle("Agent Darwinian Weights", fontsize=16, fontweight="bold")

    # Bar chart of current weights
    ax1 = axes[0]
    w_values = [weights[a] for a in agents]
    colors = []
    for w in w_values:
        if w > 1.0:
            colors.append("#2E7D32")  # green — outperforming
        elif w < 1.0:
            colors.append("#C62828")  # red — underperforming
        else:
            colors.append("#1565C0")  # blue — neutral
    bars = ax1.barh(agents, w_values, color=colors, alpha=0.8)
    ax1.axvline(x=1.0, color="black", linestyle="--", linewidth=0.8, label="Starting weight")
    ax1.set_xlabel("Darwinian Weight")
    ax1.set_title("Current Agent Weights (green = outperforming, red = underperforming)")
    ax1.grid(True, alpha=0.3, axis="x")
    ax1.legend()

    # Sharpe ratios
    ax2 = axes[1]
    sharpes = [scorecard.scores[a].sharpe for a in agents]
    win_rates = [scorecard.scores[a].win_rate * 100 for a in agents]
    s_colors = ["#2E7D32" if s > 0 else "#C62828" for s in sharpes]
    ax2.barh(agents, sharpes, color=s_colors, alpha=0.8)
    ax2.axvline(x=0, color="black", linewidth=0.8)
    ax2.set_xlabel("Sharpe Ratio")
    ax2.set_title("Agent Sharpe Ratios")
    ax2.grid(True, alpha=0.3, axis="x")

    plt.tight_layout()
    fig.savefig(results_dir / "agent_weights.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info("Saved agent_weights.png")

    # ── 3. Portfolio Composition Over Time ────────────────────────────────
    if portfolio.history:
        fig, ax = plt.subplots(figsize=(14, 5))
        hist_dates = [pd.Timestamp(h["date"]) for h in portfolio.history]
        num_pos = [h["num_positions"] for h in portfolio.history]
        gross_exp = [h.get("gross_exposure", 0) * 100 for h in portfolio.history]
        net_exp = [h.get("net_exposure", 0) * 100 for h in portfolio.history]

        ax.plot(hist_dates, gross_exp, label="Gross Exposure %", color="#FF6F00", linewidth=1.2)
        ax.plot(hist_dates, net_exp, label="Net Exposure %", color="#2962FF", linewidth=1.2)
        ax.fill_between(hist_dates, 0, gross_exp, alpha=0.1, color="#FF6F00")
        ax.set_ylabel("Exposure (%)")
        ax.set_xlabel("Date")
        ax.set_title("Portfolio Exposure Over Time")
        ax.legend(loc="upper left")
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))

        # Secondary axis for position count
        ax2 = ax.twinx()
        ax2.bar(hist_dates, num_pos, alpha=0.2, color="gray", width=0.8, label="# Positions")
        ax2.set_ylabel("# Positions")
        ax2.legend(loc="upper right")

        plt.tight_layout()
        fig.savefig(results_dir / "exposure.png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        logger.info("Saved exposure.png")


def _enforce_exposure_limits(
    portfolio: Portfolio, prices: dict[str, float], date_str: str,
) -> None:
    """Trim positions proportionally if gross or net exposure exceeds limits."""
    gross = portfolio.gross_exposure(prices)
    if gross <= MAX_GROSS_EXPOSURE:
        return

    # Scale factor to bring gross exposure within limit
    scale = MAX_GROSS_EXPOSURE / gross if gross > 0 else 1.0
    logger.warning(
        "Gross exposure %.1f%% exceeds limit %.1f%%. Trimming positions.",
        gross * 100, MAX_GROSS_EXPOSURE * 100,
    )
    for ticker, pos in list(portfolio.positions.items()):
        trim_shares = pos.shares - int(pos.shares * scale)
        if trim_shares > 0:
            price = prices.get(ticker, pos.entry_price)
            action = "COVER" if pos.is_short else "SELL"
            portfolio.execute_action(ticker, action, trim_shares, price, date_str)


def _record_agent_recommendations(
    result: dict, scorecard: Scorecard, trading_date: date, prices: dict[str, float]
) -> None:
    """Extract and record recommendations from all agents."""
    date_str = trading_date.isoformat()

    # Sector picks
    for agent_name, output in result.get("sector_picks", {}).items():
        for pick_key in ("top_long", "top_short"):
            pick = output.get(pick_key, {})
            if isinstance(pick, dict) and pick.get("ticker"):
                ticker = pick["ticker"]
                direction = "LONG" if "long" in pick_key else "SHORT"
                conviction = pick.get("conviction", 50)
                price = prices.get(ticker, 0)
                if price > 0:
                    scorecard.record_recommendation(Recommendation(
                        agent=agent_name,
                        date=date_str,
                        ticker=ticker,
                        direction=direction,
                        conviction=conviction,
                        entry_price=price,
                    ))

    # Superinvestor verdicts
    for agent_name, output in result.get("superinvestor_views", {}).items():
        verdicts = output.get("portfolio_verdicts", output.get("portfolio_review", []))
        for v in verdicts:
            if not isinstance(v, dict) or not v.get("ticker"):
                continue
            action = v.get("action", v.get("verdict", "HOLD")).upper()
            if action in ("ADD", "BUY"):
                direction = "LONG"
            elif action in ("EXIT", "TRIM"):
                direction = "SHORT"
            else:
                continue
            ticker = v["ticker"]
            price = prices.get(ticker, 0)
            if price > 0:
                scorecard.record_recommendation(Recommendation(
                    agent=agent_name,
                    date=date_str,
                    ticker=ticker,
                    direction=direction,
                    conviction=v.get("conviction", 50),
                    entry_price=price,
                ))

        # Missing name
        missing = output.get("missing_name", {})
        if isinstance(missing, dict) and missing.get("ticker"):
            ticker = missing["ticker"]
            price = prices.get(ticker, 0)
            if price > 0:
                scorecard.record_recommendation(Recommendation(
                    agent=agent_name,
                    date=date_str,
                    ticker=ticker,
                    direction="LONG",
                    conviction=missing.get("conviction", 50),
                    entry_price=price,
                ))


def _trading_days_between(start: date, end: date) -> int:
    """Count weekdays (Mon-Fri) between two dates, exclusive of start."""
    if end <= start:
        return 0
    count = 0
    d = start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


async def _fill_forward_returns(
    scorecard: Scorecard,
    current_date: date,
    current_prices: dict[str, float],
    provider: MarketDataProvider,
) -> None:
    """Fill in forward returns for past recommendations that now have enough data."""
    for rec in scorecard.recommendations:
        if rec.forward_return_5d is not None:
            continue  # Already filled

        rec_date = date.fromisoformat(rec.date)
        trading_days = _trading_days_between(rec_date, current_date)

        if trading_days >= 1 and rec.forward_return_1d is None:
            current = current_prices.get(rec.ticker)
            if current and rec.entry_price > 0:
                rec.forward_return_1d = (current / rec.entry_price) - 1

        if trading_days >= 5:
            current = current_prices.get(rec.ticker)
            if current and rec.entry_price > 0:
                rec.forward_return_5d = (current / rec.entry_price) - 1

        if trading_days >= 20 and rec.forward_return_20d is None:
            current = current_prices.get(rec.ticker)
            if current and rec.entry_price > 0:
                rec.forward_return_20d = (current / rec.entry_price) - 1


def _check_drawdown(
    portfolio: Portfolio,
    equity_curve: list[dict],
    prices: dict[str, float],
    day_num: int,
) -> None:
    """Check for drawdown and reduce exposure if triggered."""
    if len(equity_curve) < DRAWDOWN_LOOKBACK_DAYS:
        return

    recent = equity_curve[-DRAWDOWN_LOOKBACK_DAYS:]
    peak = max(r["value"] for r in recent)
    current = portfolio.mark_to_market(prices)

    if peak > 0:
        drawdown = (current - peak) / peak
        if drawdown < DRAWDOWN_THRESHOLD_PCT:
            logger.warning(
                "DRAWDOWN TRIGGER: %.1f%% drawdown (threshold: %.1f%%). Reducing exposure.",
                drawdown * 100,
                DRAWDOWN_THRESHOLD_PCT * 100,
            )
            # Trim all positions by DRAWDOWN_EXPOSURE_CUT
            for ticker, pos in list(portfolio.positions.items()):
                trim_shares = int(pos.shares * DRAWDOWN_EXPOSURE_CUT)
                if trim_shares > 0:
                    price = prices.get(ticker, pos.entry_price)
                    action = "COVER" if pos.is_short else "SELL"
                    portfolio.execute_action(
                        ticker, action, trim_shares, price,
                        equity_curve[-1].get("date", ""),
                    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for argos-backtest."""
    import argparse

    parser = argparse.ArgumentParser(description="Run ARGOS backtest")
    parser.add_argument("--start", type=str, default="2024-09-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-03-07", help="End date (YYYY-MM-DD)")
    parser.add_argument("--cash", type=float, default=DEFAULT_INITIAL_CASH, help="Starting cash")
    args = parser.parse_args()

    setup_logging()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    result = asyncio.run(run_backtest(start, end, initial_cash=args.cash))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
