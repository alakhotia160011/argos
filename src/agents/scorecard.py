"""Agent scorecard: tracks recommendations, calculates Sharpe ratios, manages Darwinian weights."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from src.config import (
    AGENT_REGISTRY,
    WEIGHT_CEILING,
    WEIGHT_FLOOR,
    WEIGHT_START,
    TOP_QUARTILE_MULTIPLIER,
    BOTTOM_QUARTILE_MULTIPLIER,
    SHARPE_LOOKBACK_DAYS,
    Settings,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Recommendation:
    """A single agent recommendation."""

    agent: str
    date: str
    ticker: str
    direction: str  # LONG or SHORT
    conviction: int  # 1-100
    entry_price: float
    forward_return_1d: float | None = None
    forward_return_5d: float | None = None
    forward_return_20d: float | None = None

    @property
    def weighted_return(self) -> float | None:
        """Conviction-weighted return using 5d forward return."""
        if self.forward_return_5d is None:
            return None
        ret = self.forward_return_5d * (self.conviction / 100)
        if self.direction == "SHORT":
            ret *= -1
        return ret


@dataclass
class AgentScore:
    """Rolling performance score for an agent."""

    name: str
    sharpe: float = 0.0
    total_recs: int = 0
    win_rate: float = 0.0
    darwinian_weight: float = WEIGHT_START
    last_modified_day: int = -999  # day number of last autoresearch modification


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------


class Scorecard:
    """Tracks all agent recommendations and computes performance metrics."""

    def __init__(self, state_dir: Path | None = None):
        settings = Settings()
        self.state_dir = state_dir or settings.state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.recommendations: list[Recommendation] = []
        self.scores: dict[str, AgentScore] = {}
        self._init_scores()
        self._load()

    def _init_scores(self) -> None:
        """Initialise scores for all registered agents."""
        for name in AGENT_REGISTRY:
            if name not in self.scores:
                self.scores[name] = AgentScore(name=name)

    # ── Persistence ──────────────────────────────────────────────────────

    def _recs_path(self) -> Path:
        return self.state_dir / "recommendations.json"

    def _scores_path(self) -> Path:
        return self.state_dir / "agent_scores.json"

    def _weights_path(self) -> Path:
        return self.state_dir / "darwinian_weights.json"

    def _load(self) -> None:
        if self._recs_path().exists():
            data = json.loads(self._recs_path().read_text())
            self.recommendations = [Recommendation(**r) for r in data]

        if self._scores_path().exists():
            data = json.loads(self._scores_path().read_text())
            for name, score_data in data.items():
                self.scores[name] = AgentScore(**score_data)

    def save(self) -> None:
        self._recs_path().write_text(
            json.dumps([asdict(r) for r in self.recommendations], indent=2)
        )
        self._scores_path().write_text(
            json.dumps({k: asdict(v) for k, v in self.scores.items()}, indent=2)
        )
        self._weights_path().write_text(
            json.dumps({k: v.darwinian_weight for k, v in self.scores.items()}, indent=2)
        )

    # ── Recording ────────────────────────────────────────────────────────

    def record_recommendation(self, rec: Recommendation) -> None:
        """Record a new recommendation."""
        self.recommendations.append(rec)
        logger.debug("Recorded rec: %s %s %s conv=%d", rec.agent, rec.direction, rec.ticker, rec.conviction)

    def update_forward_returns(self, ticker: str, rec_date: str, returns: dict[str, float]) -> None:
        """Fill in forward returns for a past recommendation."""
        for rec in self.recommendations:
            if rec.ticker == ticker and rec.date == rec_date:
                if "1d" in returns:
                    rec.forward_return_1d = returns["1d"]
                if "5d" in returns:
                    rec.forward_return_5d = returns["5d"]
                if "20d" in returns:
                    rec.forward_return_20d = returns["20d"]

    # ── Sharpe calculation ───────────────────────────────────────────────

    def agent_sharpe(self, agent_name: str, lookback: int = SHARPE_LOOKBACK_DAYS) -> float:
        """Calculate rolling Sharpe ratio for an agent."""
        recs = [
            r for r in self.recommendations
            if r.agent == agent_name and r.weighted_return is not None
        ]
        recs = recs[-lookback:]

        if len(recs) < 3:
            return 0.0

        returns = [r.weighted_return for r in recs]
        std = np.std(returns)
        if std == 0:
            return 0.0
        return float(np.mean(returns) / std)

    def recalculate_all_sharpes(self) -> None:
        """Recalculate Sharpe ratios for all agents."""
        for name in self.scores:
            self.scores[name].sharpe = self.agent_sharpe(name)
            agent_recs = [r for r in self.recommendations if r.agent == name]
            self.scores[name].total_recs = len(agent_recs)
            if agent_recs:
                wins = sum(
                    1 for r in agent_recs
                    if r.weighted_return is not None and r.weighted_return > 0
                )
                self.scores[name].win_rate = wins / len(agent_recs)

    # ── Darwinian weights ────────────────────────────────────────────────

    def update_darwinian_weights(self) -> None:
        """Update Darwinian weights based on daily performance quartiles."""
        self.recalculate_all_sharpes()

        sharpes = [(name, score.sharpe) for name, score in self.scores.items()]
        sharpes.sort(key=lambda x: x[1])

        n = len(sharpes)
        q1_cutoff = n // 4
        q3_cutoff = 3 * n // 4

        bottom_quartile = {s[0] for s in sharpes[:q1_cutoff]}
        top_quartile = {s[0] for s in sharpes[q3_cutoff:]}

        for name, score in self.scores.items():
            if name in top_quartile:
                score.darwinian_weight = min(
                    WEIGHT_CEILING, score.darwinian_weight * TOP_QUARTILE_MULTIPLIER
                )
            elif name in bottom_quartile:
                score.darwinian_weight = max(
                    WEIGHT_FLOOR, score.darwinian_weight * BOTTOM_QUARTILE_MULTIPLIER
                )

        logger.info(
            "Darwinian weights updated. Top: %s, Bottom: %s",
            sorted(top_quartile),
            sorted(bottom_quartile),
        )

    # ── Autoresearch helpers ─────────────────────────────────────────────

    def worst_agent(self, cooldown_day: int, cooldown_period: int = 5) -> str | None:
        """Return the agent with the lowest Sharpe that isn't in cooldown."""
        candidates = [
            (name, score)
            for name, score in self.scores.items()
            if score.total_recs >= 3
            and (cooldown_day - score.last_modified_day) >= cooldown_period
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda x: x[1].sharpe)[0]

    def get_recent_recs(self, agent_name: str, n: int = 20) -> list[Recommendation]:
        """Get the most recent N recommendations for an agent."""
        recs = [r for r in self.recommendations if r.agent == agent_name]
        return recs[-n:]

    def get_weight(self, agent_name: str) -> float:
        """Get current Darwinian weight for an agent."""
        return self.scores.get(agent_name, AgentScore(name=agent_name)).darwinian_weight

    def get_all_weights(self) -> dict[str, float]:
        """Get all current Darwinian weights."""
        return {name: score.darwinian_weight for name, score in self.scores.items()}

    def mark_modified(self, agent_name: str, day: int) -> None:
        """Record that an agent was modified by autoresearch."""
        if agent_name in self.scores:
            self.scores[agent_name].last_modified_day = day
