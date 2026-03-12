"""ARGOS configuration: settings, constants, and agent registry."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Environment-driven settings loaded from .env file."""

    model_config = {"env_prefix": "ARGOS_", "env_file": ".env", "extra": "ignore"}

    # API keys (no prefix — loaded directly from env)
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    fmp_api_key: str = Field(default="", alias="FMP_API_KEY")
    finnhub_api_key: str = Field(default="", alias="FINNHUB_API_KEY")
    polygon_api_key: str = Field(default="", alias="POLYGON_API_KEY")
    fred_api_key: str = Field(default="", alias="FRED_API_KEY")

    # Paths
    data_dir: Path = Path("./data")
    prompt_dir: Path = Path("./prompts/trained")

    # Logging
    log_level: str = "INFO"

    # LLM
    llm_model: str = "claude-sonnet-4-20250514"
    llm_max_retries: int = 3
    llm_concurrency: int = 5

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def backtest_dir(self) -> Path:
        return self.data_dir / "backtest"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def track_record_dir(self) -> Path:
        return self.data_dir / "track_record"


# ---------------------------------------------------------------------------
# Darwinian weight constants
# ---------------------------------------------------------------------------
WEIGHT_CEILING = 2.5
WEIGHT_FLOOR = 0.3
WEIGHT_START = 1.0
TOP_QUARTILE_MULTIPLIER = 1.05
BOTTOM_QUARTILE_MULTIPLIER = 0.95

# ---------------------------------------------------------------------------
# Autoresearch constants
# ---------------------------------------------------------------------------
AUTORESEARCH_OBSERVATION_DAYS = 5
AUTORESEARCH_COOLDOWN_DAYS = 5
SHARPE_LOOKBACK_DAYS = 60

# ---------------------------------------------------------------------------
# Portfolio constants
# ---------------------------------------------------------------------------
DEFAULT_INITIAL_CASH = 1_000_000
MAX_POSITION_PCT = 0.10  # 10% max single position
MAX_GROSS_EXPOSURE = 1.5
MAX_NET_EXPOSURE = 0.8
MIN_CASH_RESERVE_PCT = 0.05  # keep 5% cash minimum
MAX_POSITIONS = 30
DRAWDOWN_THRESHOLD_PCT = -0.10  # trigger risk reduction at -10%
DRAWDOWN_LOOKBACK_DAYS = 20
DRAWDOWN_EXPOSURE_CUT = 0.5  # halve gross exposure on drawdown trigger

# ---------------------------------------------------------------------------
# Regime thresholds
# ---------------------------------------------------------------------------
REGIME_RISK_ON_THRESHOLD = 0.2
REGIME_RISK_OFF_THRESHOLD = -0.2


# ---------------------------------------------------------------------------
# Layer enum
# ---------------------------------------------------------------------------
class Layer(str, Enum):
    MACRO = "macro"
    SECTOR = "sector"
    SUPERINVESTOR = "superinvestor"
    DECISION = "decision"


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------
class AgentConfig:
    """Metadata for a single agent."""

    __slots__ = ("name", "layer", "prompt_file", "default_weight")

    def __init__(self, name: str, layer: Layer, prompt_file: str, default_weight: float = 1.0):
        self.name = name
        self.layer = layer
        self.prompt_file = prompt_file
        self.default_weight = default_weight

    def __repr__(self) -> str:
        return f"AgentConfig({self.name!r}, {self.layer.value})"


# fmt: off
AGENT_REGISTRY: dict[str, AgentConfig] = {
    # Layer 1 — Macro (10)
    "central_bank":        AgentConfig("central_bank",        Layer.MACRO,         "central_bank.md"),
    "geopolitical":        AgentConfig("geopolitical",        Layer.MACRO,         "geopolitical.md"),
    "china":               AgentConfig("china",               Layer.MACRO,         "china.md"),
    "dollar":              AgentConfig("dollar",              Layer.MACRO,         "dollar.md"),
    "yield_curve":         AgentConfig("yield_curve",         Layer.MACRO,         "yield_curve.md"),
    "commodities":         AgentConfig("commodities",         Layer.MACRO,         "commodities.md"),
    "volatility":          AgentConfig("volatility",          Layer.MACRO,         "volatility.md"),
    "emerging_markets":    AgentConfig("emerging_markets",    Layer.MACRO,         "emerging_markets.md"),
    "news_sentiment":      AgentConfig("news_sentiment",      Layer.MACRO,         "news_sentiment.md"),
    "institutional_flow":  AgentConfig("institutional_flow",  Layer.MACRO,         "institutional_flow.md"),
    # Layer 2 — Sector Desks (7)
    "semiconductor":       AgentConfig("semiconductor",       Layer.SECTOR,        "semiconductor.md"),
    "energy":              AgentConfig("energy",              Layer.SECTOR,        "energy.md"),
    "biotech":             AgentConfig("biotech",             Layer.SECTOR,        "biotech.md"),
    "consumer":            AgentConfig("consumer",            Layer.SECTOR,        "consumer.md"),
    "industrials":         AgentConfig("industrials",         Layer.SECTOR,        "industrials.md"),
    "financials":          AgentConfig("financials",          Layer.SECTOR,        "financials.md"),
    "relationship_mapper": AgentConfig("relationship_mapper", Layer.SECTOR,        "relationship_mapper.md"),
    # Layer 3 — Superinvestors (4)
    "druckenmiller":       AgentConfig("druckenmiller",       Layer.SUPERINVESTOR, "druckenmiller.md"),
    "aschenbrenner":       AgentConfig("aschenbrenner",       Layer.SUPERINVESTOR, "aschenbrenner.md"),
    "baker":               AgentConfig("baker",               Layer.SUPERINVESTOR, "baker.md"),
    "ackman":              AgentConfig("ackman",              Layer.SUPERINVESTOR, "ackman.md"),
    # Layer 4 — Decision (4)
    "cro":                 AgentConfig("cro",                 Layer.DECISION,      "cro.md"),
    "alpha_discovery":     AgentConfig("alpha_discovery",     Layer.DECISION,      "alpha_discovery.md"),
    "autonomous_execution":AgentConfig("autonomous_execution",Layer.DECISION,      "autonomous_execution.md"),
    "cio":                 AgentConfig("cio",                 Layer.DECISION,      "cio.md"),
}
# fmt: on


def agents_for_layer(layer: Layer) -> list[AgentConfig]:
    """Return all agents belonging to a given layer."""
    return [a for a in AGENT_REGISTRY.values() if a.layer == layer]
