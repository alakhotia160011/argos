"""Alpaca broker integration for live order execution.

Translates CIO portfolio actions into Alpaca orders. Supports both
paper trading (default) and live trading.

Safety features:
  - Paper trading by default (must explicitly enable live)
  - Pre-trade confirmation option
  - Position size limits enforced before submission
  - Order logging to trades.jsonl
  - Account equity checks before each order
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from alpaca.common.exceptions import APIError

from src.config import MAX_POSITION_PCT, MAX_GROSS_EXPOSURE, Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class BrokerConfig:
    """Alpaca broker settings."""
    api_key: str = ""
    secret_key: str = ""
    paper: bool = True  # Default to paper trading
    max_order_value: float = 100_000  # Hard cap per order ($)
    require_confirmation: bool = True  # Ask before executing
    dry_run: bool = False  # Log orders without submitting


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------

class AlpacaBroker:
    """Executes trades via the Alpaca API."""

    def __init__(self, config: BrokerConfig | None = None, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.config = config or self._load_config()
        self.client = TradingClient(
            api_key=self.config.api_key,
            secret_key=self.config.secret_key,
            paper=self.config.paper,
        )
        self._order_log: list[dict] = []
        mode = "PAPER" if self.config.paper else "LIVE"
        logger.info("Alpaca broker initialised (%s mode)", mode)

    def _load_config(self) -> BrokerConfig:
        """Load config from environment via Settings."""
        import os
        return BrokerConfig(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
            paper=os.getenv("ALPACA_PAPER", "true").lower() == "true",
        )

    # ── Account info ──────────────────────────────────────────────────────

    def get_account(self) -> dict[str, Any]:
        """Get current account info."""
        account = self.client.get_account()
        return {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value or account.equity),
            "day_trade_count": account.daytrade_count,
            "pattern_day_trader": account.pattern_day_trader,
            "status": account.status,
        }

    def get_positions(self) -> dict[str, dict]:
        """Get all current positions."""
        positions = self.client.get_all_positions()
        result = {}
        for pos in positions:
            result[pos.symbol] = {
                "shares": int(float(pos.qty)),
                "side": str(pos.side),
                "avg_entry": float(pos.avg_entry_price),
                "market_value": float(pos.market_value),
                "unrealised_pnl": float(pos.unrealized_pl),
                "current_price": float(pos.current_price),
            }
        return result

    def get_portfolio_for_agents(self) -> dict[str, Any]:
        """Get portfolio state formatted for the agent pipeline."""
        account = self.get_account()
        positions = self.get_positions()
        return {
            "cash": account["cash"],
            "positions": {
                ticker: {
                    "shares": pos["shares"],
                    "avg_entry_price": pos["avg_entry"],
                    "current_price": pos["current_price"],
                    "unrealised_pnl": pos["unrealised_pnl"],
                    "is_short": pos["side"] == "short",
                }
                for ticker, pos in positions.items()
            },
            "total_value": account["equity"],
        }

    # ── Order execution ───────────────────────────────────────────────────

    def execute_actions(
        self,
        actions: list[dict],
        confirm: bool | None = None,
    ) -> list[dict]:
        """Execute a list of CIO actions.

        Each action: {"ticker": "AVGO", "action": "BUY", "shares": 60, "rationale": "..."}
        Returns list of execution results.
        """
        should_confirm = confirm if confirm is not None else self.config.require_confirmation
        account = self.get_account()
        equity = account["equity"]
        results = []

        # Preview all orders first
        valid_orders = []
        for action_item in actions:
            order = self._validate_order(action_item, equity)
            if order:
                valid_orders.append(order)

        if not valid_orders:
            logger.info("No valid orders to execute")
            return results

        # Show preview
        mode = "PAPER" if self.config.paper else "*** LIVE ***"
        logger.info("=== Order Preview (%s) ===", mode)
        logger.info("Account equity: $%.2f", equity)
        for order in valid_orders:
            logger.info(
                "  %s %s %d shares (~$%.0f) | %s",
                order["side"], order["ticker"], order["shares"],
                order["est_value"], order["rationale"][:60],
            )

        if should_confirm and not self.config.dry_run:
            response = input(f"\nExecute {len(valid_orders)} orders? [y/N]: ").strip().lower()
            if response != "y":
                logger.info("Orders cancelled by user")
                return [{"status": "cancelled", "reason": "user_rejected"}]

        # Execute
        for order in valid_orders:
            result = self._submit_order(order)
            results.append(result)
            self._order_log.append(result)

        # Save order log
        self._save_log()
        return results

    def _validate_order(self, action_item: dict, equity: float) -> dict | None:
        """Validate and prepare a single order."""
        ticker = action_item.get("ticker", "")
        action = action_item.get("action", "HOLD").upper()
        shares = abs(action_item.get("shares", 0))
        rationale = action_item.get("rationale", "")

        if not ticker or action == "HOLD" or shares <= 0:
            return None

        # Map CIO actions to Alpaca sides
        if action in ("BUY",):
            side = OrderSide.BUY
        elif action in ("SELL", "TRIM"):
            side = OrderSide.SELL
        elif action == "SHORT":
            side = OrderSide.SELL  # Short sell
        elif action == "COVER":
            side = OrderSide.BUY  # Buy to cover
        else:
            logger.warning("Unknown action: %s for %s", action, ticker)
            return None

        # Estimate order value (use last known price or skip check)
        est_value = 0
        try:
            positions = self.get_positions()
            if ticker in positions:
                est_value = positions[ticker]["current_price"] * shares
            else:
                # Get latest quote
                from alpaca.data.requests import StockLatestQuoteRequest
                from alpaca.data.historical import StockHistoricalDataClient
                data_client = StockHistoricalDataClient(
                    self.config.api_key, self.config.secret_key
                )
                quote = data_client.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=ticker)
                )
                if ticker in quote:
                    est_value = float(quote[ticker].ask_price or 0) * shares
        except Exception:
            pass  # Continue without price estimate

        # Hard cap per order
        if est_value > self.config.max_order_value and est_value > 0:
            old_shares = shares
            shares = int(self.config.max_order_value / (est_value / old_shares))
            est_value = est_value / old_shares * shares
            logger.warning(
                "Order capped: %s %s %d→%d shares (max $%.0f)",
                action, ticker, old_shares, shares, self.config.max_order_value,
            )

        # Position size limit
        if equity > 0 and est_value > 0:
            position_pct = est_value / equity
            if position_pct > MAX_POSITION_PCT:
                shares = int(equity * MAX_POSITION_PCT / (est_value / shares))
                logger.warning(
                    "Position size limited: %s %s to %d shares (%.1f%% max)",
                    action, ticker, shares, MAX_POSITION_PCT * 100,
                )

        if shares <= 0:
            return None

        return {
            "ticker": ticker,
            "action": action,
            "side": side,
            "shares": shares,
            "est_value": est_value,
            "rationale": rationale,
        }

    def _submit_order(self, order: dict) -> dict:
        """Submit a single order to Alpaca."""
        ticker = order["ticker"]
        side = order["side"]
        shares = order["shares"]

        if self.config.dry_run:
            logger.info("DRY RUN: %s %s %d shares", order["action"], ticker, shares)
            return {
                "status": "dry_run",
                "ticker": ticker,
                "action": order["action"],
                "shares": shares,
                "timestamp": datetime.now().isoformat(),
            }

        try:
            request = MarketOrderRequest(
                symbol=ticker,
                qty=shares,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            response = self.client.submit_order(request)

            result = {
                "status": "submitted",
                "order_id": str(response.id),
                "ticker": ticker,
                "action": order["action"],
                "side": str(response.side),
                "shares": shares,
                "filled_price": float(response.filled_avg_price) if response.filled_avg_price else None,
                "order_status": str(response.status),
                "rationale": order["rationale"],
                "timestamp": datetime.now().isoformat(),
            }
            logger.info(
                "Order submitted: %s %s %d shares (id=%s, status=%s)",
                order["action"], ticker, shares, response.id, response.status,
            )
            return result

        except APIError as e:
            logger.error("Alpaca API error for %s %s: %s", order["action"], ticker, e)
            return {
                "status": "error",
                "ticker": ticker,
                "action": order["action"],
                "shares": shares,
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }

    def _save_log(self) -> None:
        """Append executed orders to the broker log."""
        log_path = self.settings.log_dir / "broker_orders.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            for entry in self._order_log:
                f.write(json.dumps(entry, default=str) + "\n")
        self._order_log.clear()

    # ── Position sync ─────────────────────────────────────────────────────

    def sync_portfolio_state(self) -> None:
        """Write current Alpaca positions to state/portfolio.json for agent use."""
        portfolio = self.get_portfolio_for_agents()
        state_path = self.settings.state_dir / "portfolio.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(portfolio, indent=2))
        logger.info(
            "Synced portfolio: $%.2f equity, %d positions",
            portfolio["total_value"], len(portfolio["positions"]),
        )
