"""Market data fetching from FMP, Finnhub, Polygon, FRED, and yfinance (fallback).

Provides both live and historical (backtest) data access with caching.
yfinance is used as a free fallback when FMP returns 403/401.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from src.config import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# yfinance fallback helpers (free, no API key needed)
# ---------------------------------------------------------------------------


def _yf_get_historical(ticker: str, from_date: str, to_date: str) -> pd.DataFrame:
    """Fetch historical OHLCV via yfinance."""
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        df = tk.history(start=from_date, end=to_date, auto_adjust=True)
        if df.empty:
            return pd.DataFrame()
        df = df.reset_index()
        df.columns = [c.lower() for c in df.columns]
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        return df[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    except Exception as e:
        logger.warning("yfinance historical failed for %s: %s", ticker, e)
        return pd.DataFrame()


def _yf_get_quotes(tickers: list[str]) -> dict[str, dict]:
    """Fetch current quotes via yfinance."""
    try:
        import yfinance as yf
        result = {}
        data = yf.download(tickers, period="2d", group_by="ticker", progress=False, threads=True)
        if data.empty:
            return {}
        for ticker in tickers:
            try:
                if len(tickers) == 1:
                    ticker_data = data
                else:
                    ticker_data = data[ticker]
                if ticker_data.empty:
                    continue
                last = ticker_data.iloc[-1]
                result[ticker] = {
                    "symbol": ticker,
                    "price": float(last["Close"]),
                    "volume": float(last["Volume"]) if "Volume" in last else 0,
                }
            except (KeyError, IndexError):
                continue
        return result
    except Exception as e:
        logger.warning("yfinance quotes failed: %s", e)
        return {}

# ---------------------------------------------------------------------------
# Provider clients
# ---------------------------------------------------------------------------


class MarketDataProvider:
    """Unified market data interface across multiple providers."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.cache_dir = self.settings.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._http = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self._http.aclose()

    # ── FMP (Financial Modeling Prep) ────────────────────────────────────

    async def _fmp(self, endpoint: str, params: dict | None = None) -> Any:
        """Call FMP v3 API."""
        params = params or {}
        params["apikey"] = self.settings.fmp_api_key
        url = f"https://financialmodelingprep.com/api/v3/{endpoint}"
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _fmp_stable(self, endpoint: str, params: dict | None = None) -> Any:
        """Call FMP stable API (works on free tier)."""
        params = params or {}
        params["apikey"] = self.settings.fmp_api_key
        url = f"https://financialmodelingprep.com/stable/{endpoint}"
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_quote(self, ticker: str) -> dict:
        """Current quote for a single ticker."""
        try:
            data = await self._fmp_stable("quote", {"symbol": ticker})
            return data[0] if data else {}
        except (httpx.HTTPStatusError, Exception):
            logger.debug("FMP quote failed for %s, falling back to yfinance", ticker)
            quotes = _yf_get_quotes([ticker])
            return quotes.get(ticker, {})

    async def get_quotes(self, tickers: list[str]) -> dict[str, dict]:
        """Current quotes for multiple tickers."""
        # FMP stable API: one call per ticker, batch them
        try:
            result = {}
            for ticker in tickers:
                data = await self._fmp_stable("quote", {"symbol": ticker})
                if data:
                    item = data[0] if isinstance(data, list) else data
                    result[item.get("symbol", ticker)] = item
            if result:
                return result
        except (httpx.HTTPStatusError, Exception):
            pass
        logger.info("FMP quotes failed, falling back to yfinance for %d tickers", len(tickers))
        return _yf_get_quotes(tickers)

    async def get_historical_price(
        self, ticker: str, from_date: str, to_date: str
    ) -> pd.DataFrame:
        """Historical daily OHLCV data. Checks local EOD store first, then APIs."""
        # Try local store first (fast, no API call)
        from src.agents.eod_store import get_historical_from_store, store_has_data
        if store_has_data(ticker):
            df = get_historical_from_store(ticker, from_date, to_date)
            if not df.empty:
                return df

        # Fall back to APIs
        try:
            data = await self._fmp_stable(
                "historical-price-eod/full",
                {"symbol": ticker, "from": from_date, "to": to_date},
            )
            if not data:
                raise ValueError("No FMP data")
            df = pd.DataFrame(data)
            df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date").reset_index(drop=True)
        except (httpx.HTTPStatusError, ValueError, Exception):
            logger.debug("FMP historical failed for %s, falling back to yfinance", ticker)
            return _yf_get_historical(ticker, from_date, to_date)

    async def get_income_statement(self, ticker: str, period: str = "annual") -> list[dict]:
        data = await self._fmp(f"income-statement/{ticker}", {"period": period, "limit": 4})
        return data

    async def get_key_metrics(self, ticker: str) -> list[dict]:
        data = await self._fmp(f"key-metrics/{ticker}", {"limit": 4})
        return data

    # ── Finnhub ──────────────────────────────────────────────────────────

    async def _finnhub(self, endpoint: str, params: dict | None = None) -> Any:
        params = params or {}
        params["token"] = self.settings.finnhub_api_key
        url = f"https://finnhub.io/api/v1/{endpoint}"
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_market_news(self, category: str = "general", min_id: int = 0) -> list[dict]:
        if not self.settings.finnhub_api_key:
            return []
        try:
            return await self._finnhub("news", {"category": category, "minId": min_id})
        except httpx.HTTPStatusError:
            logger.debug("Finnhub news failed, returning empty")
            return []

    async def get_company_news(self, ticker: str, from_date: str, to_date: str) -> list[dict]:
        return await self._finnhub(
            "company-news", {"symbol": ticker, "from": from_date, "to": to_date}
        )

    async def get_sentiment(self, ticker: str) -> dict:
        return await self._finnhub("news-sentiment", {"symbol": ticker})

    # ── Polygon ──────────────────────────────────────────────────────────

    async def _polygon(self, endpoint: str, params: dict | None = None) -> Any:
        params = params or {}
        params["apiKey"] = self.settings.polygon_api_key
        url = f"https://api.polygon.io/{endpoint}"
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_aggregate_bars(
        self, ticker: str, from_date: str, to_date: str, timespan: str = "day"
    ) -> pd.DataFrame:
        data = await self._polygon(
            f"v2/aggs/ticker/{ticker}/range/1/{timespan}/{from_date}/{to_date}",
            {"adjusted": "true", "sort": "asc", "limit": 5000},
        )
        if "results" not in data:
            return pd.DataFrame()
        df = pd.DataFrame(data["results"])
        df["date"] = pd.to_datetime(df["t"], unit="ms")
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        return df[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

    # ── FRED ─────────────────────────────────────────────────────────────

    async def _fred(self, series_id: str, limit: int = 60) -> pd.DataFrame:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": self.settings.fred_api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        }
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        df = pd.DataFrame(data["observations"])
        df["date"] = pd.to_datetime(df["date"])
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        return df[["date", "value"]].dropna().sort_values("date").reset_index(drop=True)

    # ── FRED convenience methods ────────────────────────────────────────

    async def get_fed_funds_rate(self) -> pd.DataFrame:
        return await self._fred("DFF")

    async def get_treasury_yield(self, maturity: str = "10y") -> pd.DataFrame:
        series_map = {"2y": "DGS2", "5y": "DGS5", "10y": "DGS10", "30y": "DGS30"}
        return await self._fred(series_map.get(maturity, "DGS10"))

    async def get_yield_curve_spread(self) -> pd.DataFrame:
        return await self._fred("T10Y2Y")

    async def get_breakeven_inflation(self) -> pd.DataFrame:
        return await self._fred("T10YIE")

    async def get_vix(self) -> pd.DataFrame:
        return await self._fred("VIXCLS")

    async def get_dxy(self) -> pd.DataFrame:
        return await self._fred("DTWEXBGS")

    # ── yfinance market data (commodities, indices, ETFs) ────────────────

    async def _yf_latest_price(self, ticker: str) -> float | None:
        """Get latest price for a ticker via yfinance."""
        try:
            df = _yf_get_historical(ticker, (date.today() - timedelta(days=7)).isoformat(), date.today().isoformat())
            if df.empty:
                return None
            return float(df.iloc[-1]["close"])
        except Exception:
            return None

    async def _yf_returns(self, ticker: str, days: int = 20) -> dict[str, float | None]:
        """Get price and returns for a ticker."""
        try:
            start = (date.today() - timedelta(days=days + 10)).isoformat()
            df = _yf_get_historical(ticker, start, date.today().isoformat())
            if df.empty or len(df) < 2:
                return {"price": None, "return_1d": None, "return_5d": None, "return_20d": None}
            returns = df["close"].pct_change().dropna()
            return {
                "price": float(df.iloc[-1]["close"]),
                "return_1d": float(returns.iloc[-1]) if len(returns) >= 1 else None,
                "return_5d": float(returns.tail(5).sum()) if len(returns) >= 5 else None,
                "return_20d": float(returns.tail(20).sum()) if len(returns) >= 20 else None,
            }
        except Exception:
            return {"price": None, "return_1d": None, "return_5d": None, "return_20d": None}

    # ── Composite data bundles ───────────────────────────────────────────

    # All FRED series we want for the macro layer
    FRED_SERIES = {
        # Rates & yields
        "fed_funds_rate": "DFF",
        "treasury_3m": "DGS3MO",
        "treasury_2y": "DGS2",
        "treasury_5y": "DGS5",
        "treasury_10y": "DGS10",
        "treasury_30y": "DGS30",
        "yield_curve_10y2y": "T10Y2Y",
        "yield_curve_10y3m": "T10Y3M",
        "real_rate_5y": "DFII5",
        "real_rate_10y": "DFII10",
        # Inflation
        "breakeven_inflation_5y": "T5YIE",
        "breakeven_inflation_10y": "T10YIE",
        # Credit & risk
        "ice_bofa_hy_spread": "BAMLH0A0HYM2",      # High yield OAS
        "ice_bofa_ig_spread": "BAMLC0A0CM",          # Investment grade OAS
        "ted_spread": "TEDRATE",                     # TED spread (interbank risk)
        # Volatility
        "vix": "VIXCLS",
        # Dollar
        "dxy": "DTWEXBGS",
        # Labor market
        "initial_claims": "ICSA",                    # Weekly initial jobless claims
        "continued_claims": "CCSA",                  # Continued claims
        "unemployment_rate": "UNRATE",               # Monthly unemployment
        # Consumer
        "umich_consumer_sentiment": "UMCSENT",       # U of Michigan sentiment
        "retail_sales_yoy": "RSAFS",                 # Retail sales
        # Manufacturing & activity
        "ism_pmi": "MANEMP",                         # ISM Manufacturing employment (proxy)
        "industrial_production": "INDPRO",           # Industrial production index
        "capacity_utilization": "TCU",               # Capacity utilization
        # Housing
        "housing_starts": "HOUST",                   # Housing starts
        "mortgage_rate_30y": "MORTGAGE30US",         # 30-year mortgage rate
        # Money supply
        "m2_money_supply": "M2SL",                   # M2
        # Leading indicators
        "lei": "USSLIND",                            # Leading economic index
    }

    # Market tickers to fetch via yfinance for macro context
    MARKET_TICKERS = {
        # Major indices
        "sp500": "SPY",
        "nasdaq": "QQQ",
        "russell_2000": "IWM",
        "dow": "DIA",
        # International
        "msci_em": "EEM",
        "msci_eafe": "EFA",
        "china_large_cap": "FXI",
        "japan": "EWJ",
        # Sector ETFs
        "etf_tech": "XLK",
        "etf_financials": "XLF",
        "etf_energy": "XLE",
        "etf_healthcare": "XLV",
        "etf_industrials": "XLI",
        "etf_consumer_disc": "XLY",
        "etf_consumer_staples": "XLP",
        "etf_utilities": "XLU",
        "etf_real_estate": "XLRE",
        "etf_materials": "XLB",
        "etf_semis": "SMH",
        "etf_biotech": "XBI",
        # Commodities
        "oil_wti": "CL=F",
        "gold": "GC=F",
        "silver": "SI=F",
        "copper": "HG=F",
        "natural_gas": "NG=F",
        "corn": "ZC=F",
        "soybeans": "ZS=F",
        # Bonds
        "tlt_20y_bonds": "TLT",
        "hyg_high_yield": "HYG",
        "lqd_ig_bonds": "LQD",
        "tip_inflation_protected": "TIP",
        # Currencies
        "usd_jpy": "JPY=X",
        "eur_usd": "EURUSD=X",
        "gbp_usd": "GBPUSD=X",
        "usd_cny": "CNY=X",
        # Volatility
        "vix_futures": "^VIX",
        "move_bond_vol": "^MOVE",
    }

    async def get_macro_data(self, as_of: date | None = None) -> dict[str, Any]:
        """Fetch comprehensive macro data for Layer 1 agents.

        Pulls from FRED (rates, credit, labor, inflation, sentiment)
        and yfinance (indices, sectors, commodities, currencies, bonds).
        """
        import asyncio

        # ── FRED data (all series in parallel) ──
        fred_tasks = {
            name: self._fred(series_id)
            for name, series_id in self.FRED_SERIES.items()
        }
        fred_results = await asyncio.gather(*fred_tasks.values(), return_exceptions=True)
        fred_names = list(fred_tasks.keys())

        def _latest(df_or_err) -> float | None:
            if isinstance(df_or_err, Exception):
                return None
            if hasattr(df_or_err, 'empty') and df_or_err.empty:
                return None
            return float(df_or_err.iloc[-1]["value"])

        def _series(df_or_err, n: int = 5) -> list[float]:
            """Return last N values for trend analysis."""
            if isinstance(df_or_err, Exception):
                return []
            if hasattr(df_or_err, 'empty') and df_or_err.empty:
                return []
            return [float(v) for v in df_or_err.tail(n)["value"].tolist()]

        fred_data = {}
        for i, name in enumerate(fred_names):
            fred_data[name] = _latest(fred_results[i])
            fred_data[f"{name}_trend"] = _series(fred_results[i], 5)

        # ── Market data via yfinance (indices, commodities, etc.) ──
        market_tasks = {
            name: self._yf_returns(ticker)
            for name, ticker in self.MARKET_TICKERS.items()
        }
        market_results = await asyncio.gather(*market_tasks.values(), return_exceptions=True)
        market_names = list(market_tasks.keys())

        market_data = {}
        for i, name in enumerate(market_names):
            r = market_results[i]
            if isinstance(r, Exception):
                market_data[name] = {"price": None, "return_1d": None, "return_5d": None, "return_20d": None}
            else:
                market_data[name] = r

        # ── News ──
        news = await self.get_market_news()

        # ── Derived signals ──
        derived = {}

        # Yield curve inversion flag
        t10y = fred_data.get("treasury_10y")
        t2y = fred_data.get("treasury_2y")
        t3m = fred_data.get("treasury_3m")
        if t10y and t2y:
            derived["curve_10y2y_inverted"] = t10y < t2y
        if t10y and t3m:
            derived["curve_10y3m_inverted"] = t10y < t3m

        # Real rate (10y nominal - 10y breakeven)
        be10 = fred_data.get("breakeven_inflation_10y")
        if t10y and be10:
            derived["real_rate_10y_approx"] = round(t10y - be10, 3)

        # Credit stress signal
        hy = fred_data.get("ice_bofa_hy_spread")
        if hy is not None:
            derived["credit_stress"] = "HIGH" if hy > 5.0 else "ELEVATED" if hy > 4.0 else "NORMAL"

        # Risk appetite composite
        vix = fred_data.get("vix")
        if vix is not None:
            if vix > 30:
                derived["vix_regime"] = "FEAR"
            elif vix > 20:
                derived["vix_regime"] = "ELEVATED"
            elif vix > 15:
                derived["vix_regime"] = "NORMAL"
            else:
                derived["vix_regime"] = "COMPLACENT"

        # Sector rotation signal (cyclicals vs defensives)
        cyc = market_data.get("etf_consumer_disc", {}).get("return_20d")
        def_ = market_data.get("etf_utilities", {}).get("return_20d")
        if cyc is not None and def_ is not None:
            derived["cyclical_vs_defensive_20d"] = round(cyc - def_, 4)
            derived["rotation_signal"] = "RISK_ON" if cyc > def_ else "RISK_OFF"

        # Dollar strength interpretation
        dxy = fred_data.get("dxy")
        if dxy is not None:
            derived["dollar_regime"] = "VERY_STRONG" if dxy > 115 else "STRONG" if dxy > 105 else "NEUTRAL" if dxy > 95 else "WEAK"

        # Gold/copper ratio (recession indicator)
        gold_p = market_data.get("gold", {}).get("price")
        copper_p = market_data.get("copper", {}).get("price")
        if gold_p and copper_p and copper_p > 0:
            derived["gold_copper_ratio"] = round(gold_p / copper_p, 2)

        return {
            "as_of": str(as_of or date.today()),
            "fred": fred_data,
            "markets": market_data,
            "derived": derived,
            "news": news,
        }

    async def get_sector_data(
        self,
        tickers: list[str],
        lookback_days: int = 60,
        etf_ticker: str | None = None,
    ) -> dict[str, Any]:
        """Fetch price data and fundamentals for sector tickers.

        If etf_ticker is provided, includes ETF-level metrics under the
        "_etf" key so sector agents can see broad sector momentum.
        """
        import asyncio

        to_date = date.today().isoformat()
        from_date = (date.today() - timedelta(days=lookback_days + 10)).isoformat()

        # Include ETF in the fetch if provided
        all_tickers = list(tickers) + ([etf_ticker] if etf_ticker else [])

        # Fetch prices in parallel
        price_tasks = [self.get_historical_price(t, from_date, to_date) for t in all_tickers]
        prices = await asyncio.gather(*price_tasks, return_exceptions=True)

        sector_data: dict[str, Any] = {}
        for ticker, price_data in zip(all_tickers, prices):
            if isinstance(price_data, Exception) or price_data.empty:
                continue
            last = price_data.iloc[-1]
            returns = price_data["close"].pct_change().dropna()
            metrics = {
                "price": float(last["close"]),
                "volume": float(last["volume"]),
                "return_1d": float(returns.iloc[-1]) if len(returns) > 0 else 0,
                "return_5d": float(returns.tail(5).sum()) if len(returns) >= 5 else 0,
                "return_20d": float(returns.tail(20).sum()) if len(returns) >= 20 else 0,
                "volatility_20d": float(returns.tail(20).std() * np.sqrt(252)) if len(returns) >= 20 else 0,
                "rsi_14": _compute_rsi(price_data["close"], 14),
                "sma_50_vs_price": _sma_ratio(price_data["close"], 50),
            }

            if ticker == etf_ticker:
                # Add extra ETF-specific fields
                metrics["return_60d"] = float(returns.tail(60).sum()) if len(returns) >= 60 else 0
                metrics["sma_20_vs_price"] = _sma_ratio(price_data["close"], 20)
                sector_data["_etf"] = {"ticker": etf_ticker, **metrics}
            else:
                sector_data[ticker] = metrics

        return sector_data

    # ── Caching helpers ──────────────────────────────────────────────────

    def _cache_path(self, key: str, as_of: date) -> Path:
        return self.cache_dir / f"{key}_{as_of.isoformat()}.json"

    async def get_macro_data_cached(self, as_of: date) -> dict[str, Any]:
        """Fetch macro data with daily cache."""
        cache = self._cache_path("macro", as_of)
        if cache.exists():
            return json.loads(cache.read_text())
        data = await self.get_macro_data(as_of)
        cache.write_text(json.dumps(data, default=str))
        return data


# ---------------------------------------------------------------------------
# Technical indicator helpers
# ---------------------------------------------------------------------------


def _compute_rsi(prices: pd.Series, period: int = 14) -> float:
    """Compute RSI for a price series."""
    delta = prices.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty and not np.isnan(rsi.iloc[-1]) else 50.0


def _sma_ratio(prices: pd.Series, period: int) -> float:
    """Return (current_price / SMA) - 1 as a percentage."""
    if len(prices) < period:
        return 0.0
    sma = prices.rolling(window=period).mean().iloc[-1]
    if sma == 0 or np.isnan(sma):
        return 0.0
    return float((prices.iloc[-1] / sma) - 1)


# ---------------------------------------------------------------------------
# Backtest data loader (reads from cached historical files)
# ---------------------------------------------------------------------------


class BacktestDataLoader:
    """Loads pre-cached historical data for backtesting without live API calls."""

    def __init__(self, backtest_dir: Path):
        self.backtest_dir = backtest_dir
        self.backtest_dir.mkdir(parents=True, exist_ok=True)

    def load_macro_snapshot(self, as_of: date) -> dict[str, Any]:
        """Load macro data snapshot for a specific date."""
        path = self.backtest_dir / "macro" / f"{as_of.isoformat()}.json"
        if path.exists():
            return json.loads(path.read_text())
        return {}

    def load_prices(self, ticker: str, as_of: date, lookback: int = 60) -> pd.DataFrame:
        """Load historical prices up to as_of date."""
        path = self.backtest_dir / "prices" / f"{ticker}.parquet"
        if not path.exists():
            path_csv = path.with_suffix(".csv")
            if path_csv.exists():
                df = pd.read_csv(path_csv, parse_dates=["date"])
            else:
                return pd.DataFrame()
        else:
            df = pd.read_parquet(path)

        df["date"] = pd.to_datetime(df["date"])
        cutoff = pd.Timestamp(as_of)
        start = cutoff - pd.Timedelta(days=lookback + 10)
        return df[(df["date"] >= start) & (df["date"] <= cutoff)].reset_index(drop=True)

    def save_macro_snapshot(self, as_of: date, data: dict) -> None:
        """Cache a macro snapshot for future backtest use."""
        path = self.backtest_dir / "macro" / f"{as_of.isoformat()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, default=str))

    def save_prices(self, ticker: str, df: pd.DataFrame) -> None:
        """Cache price data for future backtest use."""
        path = self.backtest_dir / "prices" / f"{ticker}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            df.to_parquet(path, index=False)
        except ImportError:
            df.to_csv(path.with_suffix(".csv"), index=False)
