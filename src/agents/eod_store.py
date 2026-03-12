"""EOD data store — downloads and incrementally updates price + fundamentals.

Stores data per ticker in data/eod/:
  - {ticker}.csv         → daily OHLCV
  - {ticker}_1h.csv      → 1-hour intraday OHLCV
  - data/fundamentals/   → financials, analyst ratings, holdings (JSON)

On first run, backfills from a start date (default 2026-01-01).
On subsequent runs, only fetches missing data since the last stored date.

Usage:
    python -m src.agents.eod_store                     # full update (daily + 1h + fundamentals)
    python -m src.agents.eod_store --start 2025-01-01  # custom start date
    python -m src.agents.eod_store --only prices       # only price data
    python -m src.agents.eod_store --only fundamentals  # only fundamentals
    python -m src.agents.eod_store --coverage          # print store report
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

EOD_DIR = Path("data/eod")
FUNDAMENTALS_DIR = Path("data/fundamentals")
MACRO_DIR = Path("data/macro")

# Tickers that are indices/futures/currencies — no fundamentals available
NON_EQUITY_PREFIXES = ("^", "=")
NON_EQUITY_SUFFIXES = ("=F", "=X")


def _is_equity(ticker: str) -> bool:
    """Check if a ticker is an equity (not a future/index/currency)."""
    if ticker.startswith(NON_EQUITY_PREFIXES):
        return False
    if any(ticker.endswith(s) for s in NON_EQUITY_SUFFIXES):
        return False
    return True


# ---------------------------------------------------------------------------
# Ticker universe — everything ARGOS needs
# ---------------------------------------------------------------------------


def get_all_tickers() -> list[str]:
    """Return the full list of tickers to store EOD data for."""
    from src.agents.eod_cycle import SECTOR_TICKERS, SECTOR_ETFS

    tickers: set[str] = set()

    # Sector focus tickers
    for sector_tickers in SECTOR_TICKERS.values():
        tickers.update(sector_tickers)

    # Sector ETFs
    tickers.update(SECTOR_ETFS.values())

    # S&P 500 universe
    try:
        from src.agents.universe import get_sp500_by_sector
        sp500 = get_sp500_by_sector()
        for sector_tickers in sp500.values():
            tickers.update(sector_tickers)
    except Exception as e:
        logger.warning("Could not load S&P 500 universe: %s", e)

    # Macro market tickers (indices, commodities, bonds, currencies, vol)
    MACRO_TICKERS = [
        "SPY", "QQQ", "IWM", "DIA",           # indices
        "EEM", "EFA", "FXI", "EWJ",           # international
        "XLK", "XLF", "XLE", "XLV", "XLI",    # sector ETFs
        "XLY", "XLP", "XLU", "XLRE", "XLB",
        "SMH", "XBI",
        "CL=F", "GC=F", "SI=F", "HG=F",       # commodities
        "NG=F", "ZC=F", "ZS=F",
        "TLT", "HYG", "LQD", "TIP",           # bonds
        "JPY=X", "EURUSD=X", "GBPUSD=X", "CNY=X",  # currencies
        "^VIX", "^MOVE",                       # volatility
    ]
    tickers.update(MACRO_TICKERS)

    return sorted(tickers)


# ---------------------------------------------------------------------------
# File path helpers
# ---------------------------------------------------------------------------


def _sanitize(ticker: str) -> str:
    """Sanitize ticker for filesystem."""
    return ticker.replace("=", "_").replace("^", "_").replace("/", "_")


def _ticker_path(ticker: str, interval: str = "1d") -> Path:
    """Path to a ticker's CSV file."""
    safe = _sanitize(ticker)
    if interval == "1d":
        return EOD_DIR / f"{safe}.csv"
    return EOD_DIR / f"{safe}_{interval}.csv"


def _fundamentals_path(ticker: str) -> Path:
    """Path to a ticker's fundamentals JSON."""
    return FUNDAMENTALS_DIR / f"{_sanitize(ticker)}.json"


def _macro_path(series_id: str) -> Path:
    """Path to a FRED series CSV."""
    return MACRO_DIR / f"{series_id}.csv"


# ---------------------------------------------------------------------------
# FRED macro series
# ---------------------------------------------------------------------------

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
    "ice_bofa_hy_spread": "BAMLH0A0HYM2",
    "ice_bofa_ig_spread": "BAMLC0A0CM",
    "ted_spread": "TEDRATE",
    # Volatility
    "vix": "VIXCLS",
    # Dollar
    "dxy": "DTWEXBGS",
    # Labor market
    "initial_claims": "ICSA",
    "continued_claims": "CCSA",
    "unemployment_rate": "UNRATE",
    # Consumer
    "umich_consumer_sentiment": "UMCSENT",
    "retail_sales_yoy": "RSAFS",
    # Manufacturing & activity
    "ism_pmi": "MANEMP",
    "industrial_production": "INDPRO",
    "capacity_utilization": "TCU",
    # Housing
    "housing_starts": "HOUST",
    "mortgage_rate_30y": "MORTGAGE30US",
    # Money supply
    "m2_money_supply": "M2SL",
    # Leading indicators
    "lei": "USSLIND",
}


def _fetch_fred_series(
    series_id: str, api_key: str, start_date: str, end_date: str,
) -> pd.DataFrame:
    """Fetch a FRED series via the API."""
    import httpx

    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start_date,
        "observation_end": end_date,
        "sort_order": "asc",
    }
    resp = httpx.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    df = pd.DataFrame(data["observations"])
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df[["date", "value"]].dropna().sort_values("date").reset_index(drop=True)


def load_macro_series(series_id: str) -> pd.DataFrame:
    """Load stored FRED series. Returns empty DataFrame if none."""
    path = _macro_path(series_id)
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, parse_dates=["date"])
    return df.sort_values("date").reset_index(drop=True)


def save_macro_series(series_id: str, df: pd.DataFrame) -> None:
    """Save a FRED series to CSV."""
    if df.empty:
        return
    MACRO_DIR.mkdir(parents=True, exist_ok=True)
    df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    df.to_csv(_macro_path(series_id), index=False)


def update_macro_series(series_id: str, api_key: str, start_date: date) -> int:
    """Update a single FRED series incrementally. Returns new rows added."""
    existing = load_macro_series(series_id)

    if not existing.empty:
        last = existing["date"].max().date()
        fetch_start = last + timedelta(days=1)
    else:
        fetch_start = start_date

    today = date.today()
    if fetch_start > today:
        return 0

    new_data = _fetch_fred_series(
        series_id, api_key, fetch_start.isoformat(), today.isoformat(),
    )
    if new_data.empty:
        return 0

    if not existing.empty:
        combined = pd.concat([existing, new_data], ignore_index=True)
        combined = combined.drop_duplicates(subset=["date"], keep="last")
        combined = combined.sort_values("date").reset_index(drop=True)
    else:
        combined = new_data

    save_macro_series(series_id, combined)
    return len(new_data)


def update_all_macro(start_date: date = date(2026, 1, 1)) -> dict[str, int]:
    """Update all FRED macro series."""
    from src.config import Settings
    settings = Settings()
    api_key = settings.fred_api_key

    if not api_key:
        logger.warning("No FRED_API_KEY set — skipping macro data download")
        return {}

    logger.info("── Updating %d FRED macro series ──", len(FRED_SERIES))
    results: dict[str, int] = {}
    failed: list[str] = []

    for i, (name, series_id) in enumerate(FRED_SERIES.items()):
        try:
            n = update_macro_series(series_id, api_key, start_date)
            results[name] = n
            if n > 0:
                logger.info("[%d/%d] %s (%s): +%d rows", i + 1, len(FRED_SERIES), name, series_id, n)
        except Exception as e:
            logger.warning("[%d/%d] %s (%s): FAILED - %s", i + 1, len(FRED_SERIES), name, series_id, e)
            failed.append(name)

        # FRED rate limit: 120 requests/minute
        if (i + 1) % 20 == 0:
            time.sleep(1)

    total = sum(results.values())
    logger.info("FRED update: %d new rows across %d series, %d failed", total, len(results), len(failed))
    return results


def get_macro_from_store(series_id: str, from_date: str, to_date: str) -> pd.DataFrame:
    """Read stored FRED data for a series within a date range."""
    df = load_macro_series(series_id)
    if df.empty:
        return df
    mask = (df["date"] >= pd.Timestamp(from_date)) & (df["date"] <= pd.Timestamp(to_date))
    return df.loc[mask].reset_index(drop=True)


def macro_store_has_data(series_id: str) -> bool:
    """Check if we have stored data for a FRED series."""
    return _macro_path(series_id).exists()


# ---------------------------------------------------------------------------
# Price data operations (daily + intraday)
# ---------------------------------------------------------------------------


def load_ticker(ticker: str, interval: str = "1d") -> pd.DataFrame:
    """Load stored price data for a ticker. Returns empty DataFrame if none."""
    path = _ticker_path(ticker, interval)
    if not path.exists():
        return pd.DataFrame()
    dt_col = "datetime" if interval != "1d" else "date"
    df = pd.read_csv(path, parse_dates=[dt_col])
    return df.sort_values(dt_col).reset_index(drop=True)


def save_ticker(ticker: str, df: pd.DataFrame, interval: str = "1d") -> None:
    """Save price data for a ticker (overwrites)."""
    if df.empty:
        return
    EOD_DIR.mkdir(parents=True, exist_ok=True)
    dt_col = "datetime" if interval != "1d" else "date"
    df = df.sort_values(dt_col).drop_duplicates(subset=[dt_col], keep="last")
    df.to_csv(_ticker_path(ticker, interval), index=False)


def last_date(ticker: str, interval: str = "1d") -> date | None:
    """Get the last stored date for a ticker, or None."""
    df = load_ticker(ticker, interval)
    if df.empty:
        return None
    dt_col = "datetime" if interval != "1d" else "date"
    return df[dt_col].max().date()


def _fetch_yfinance(
    ticker: str, start: str, end: str, interval: str = "1d"
) -> pd.DataFrame:
    """Fetch OHLCV from yfinance at the given interval."""
    import yfinance as yf

    tk = yf.Ticker(ticker)
    df = tk.history(start=start, end=end, interval=interval, auto_adjust=True)
    if df.empty:
        return pd.DataFrame()

    df = df.reset_index()
    df.columns = [c.lower() for c in df.columns]

    # Normalize the datetime column
    if interval == "1d":
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        dt_col = "date"
    else:
        # Intraday — column is usually "datetime" or "date"
        for col in ["datetime", "date"]:
            if col in df.columns:
                df["datetime"] = pd.to_datetime(df[col]).dt.tz_localize(None)
                break
        dt_col = "datetime"

    cols = [dt_col, "open", "high", "low", "close", "volume"]
    available = [c for c in cols if c in df.columns]
    return df[available].reset_index(drop=True)


def update_ticker_prices(ticker: str, start_date: date, interval: str = "1d") -> int:
    """Update a single ticker's price data incrementally.

    Returns the number of new rows added.
    """
    existing = load_ticker(ticker, interval)
    last = last_date(ticker, interval)

    if last is not None:
        fetch_start = last + timedelta(days=1)
    else:
        fetch_start = start_date

    today = date.today()
    if fetch_start > today:
        return 0

    # yfinance 1h data: max 730 days back
    if interval == "1h":
        earliest = today - timedelta(days=729)
        if fetch_start < earliest:
            fetch_start = earliest

    new_data = _fetch_yfinance(
        ticker, fetch_start.isoformat(), (today + timedelta(days=1)).isoformat(),
        interval=interval,
    )
    if new_data.empty:
        return 0

    dt_col = "datetime" if interval != "1d" else "date"

    if not existing.empty:
        combined = pd.concat([existing, new_data], ignore_index=True)
        combined = combined.drop_duplicates(subset=[dt_col], keep="last")
        combined = combined.sort_values(dt_col).reset_index(drop=True)
    else:
        combined = new_data

    save_ticker(ticker, combined, interval)
    return len(new_data)


# ---------------------------------------------------------------------------
# Fundamentals data
# ---------------------------------------------------------------------------


def _fetch_fundamentals(ticker: str) -> dict[str, Any]:
    """Fetch all available fundamental data from yfinance."""
    import yfinance as yf

    tk = yf.Ticker(ticker)
    data: dict[str, Any] = {"ticker": ticker, "fetched_at": date.today().isoformat()}

    # Company info
    try:
        info = tk.info
        data["info"] = {
            "name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "market_cap": info.get("marketCap"),
            "enterprise_value": info.get("enterpriseValue"),
            "trailing_pe": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "peg_ratio": info.get("pegRatio"),
            "price_to_book": info.get("priceToBook"),
            "price_to_sales": info.get("priceToSalesTrailing12Months"),
            "ev_to_ebitda": info.get("enterpriseToEbitda"),
            "ev_to_revenue": info.get("enterpriseToRevenue"),
            "profit_margin": info.get("profitMargins"),
            "operating_margin": info.get("operatingMargins"),
            "return_on_equity": info.get("returnOnEquity"),
            "return_on_assets": info.get("returnOnAssets"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "dividend_yield": info.get("dividendYield"),
            "payout_ratio": info.get("payoutRatio"),
            "beta": info.get("beta"),
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "50d_avg": info.get("fiftyDayAverage"),
            "200d_avg": info.get("twoHundredDayAverage"),
            "avg_volume": info.get("averageVolume"),
            "shares_outstanding": info.get("sharesOutstanding"),
            "float_shares": info.get("floatShares"),
            "short_ratio": info.get("shortRatio"),
            "short_pct_of_float": info.get("shortPercentOfFloat"),
            "held_pct_insiders": info.get("heldPercentInsiders"),
            "held_pct_institutions": info.get("heldPercentInstitutions"),
            "current_price": info.get("currentPrice"),
            "target_high_price": info.get("targetHighPrice"),
            "target_low_price": info.get("targetLowPrice"),
            "target_mean_price": info.get("targetMeanPrice"),
            "target_median_price": info.get("targetMedianPrice"),
            "recommendation_key": info.get("recommendationKey"),
            "number_of_analyst_opinions": info.get("numberOfAnalystOpinions"),
        }
    except Exception as e:
        logger.debug("Info failed for %s: %s", ticker, e)

    # Income statement (quarterly)
    try:
        inc = tk.quarterly_income_stmt
        if inc is not None and not inc.empty:
            data["income_statement"] = {
                col.isoformat() if hasattr(col, "isoformat") else str(col): {
                    str(idx): _safe_val(inc.at[idx, col])
                    for idx in inc.index
                }
                for col in inc.columns[:4]  # last 4 quarters
            }
    except Exception as e:
        logger.debug("Income stmt failed for %s: %s", ticker, e)

    # Balance sheet (quarterly)
    try:
        bs = tk.quarterly_balance_sheet
        if bs is not None and not bs.empty:
            data["balance_sheet"] = {
                col.isoformat() if hasattr(col, "isoformat") else str(col): {
                    str(idx): _safe_val(bs.at[idx, col])
                    for idx in bs.index
                }
                for col in bs.columns[:4]
            }
    except Exception as e:
        logger.debug("Balance sheet failed for %s: %s", ticker, e)

    # Cash flow (quarterly)
    try:
        cf = tk.quarterly_cashflow
        if cf is not None and not cf.empty:
            data["cash_flow"] = {
                col.isoformat() if hasattr(col, "isoformat") else str(col): {
                    str(idx): _safe_val(cf.at[idx, col])
                    for idx in cf.index
                }
                for col in cf.columns[:4]
            }
    except Exception as e:
        logger.debug("Cash flow failed for %s: %s", ticker, e)

    # Analyst recommendations
    try:
        recs = tk.recommendations
        if recs is not None and not recs.empty:
            recent = recs.tail(10).reset_index()
            recent.columns = [str(c) for c in recent.columns]
            data["analyst_recommendations"] = recent.to_dict(orient="records")
    except Exception as e:
        logger.debug("Recommendations failed for %s: %s", ticker, e)

    # Institutional holders
    try:
        inst = tk.institutional_holders
        if inst is not None and not inst.empty:
            data["institutional_holders"] = inst.head(15).to_dict(orient="records")
    except Exception as e:
        logger.debug("Inst holders failed for %s: %s", ticker, e)

    # Mutual fund holders
    try:
        mf = tk.mutualfund_holders
        if mf is not None and not mf.empty:
            data["mutualfund_holders"] = mf.head(10).to_dict(orient="records")
    except Exception as e:
        logger.debug("MF holders failed for %s: %s", ticker, e)

    # Earnings dates
    try:
        ed = tk.earnings_dates
        if ed is not None and not ed.empty:
            recent_ed = ed.head(4).reset_index()
            recent_ed.columns = [str(c) for c in recent_ed.columns]
            data["earnings_dates"] = recent_ed.to_dict(orient="records")
    except Exception as e:
        logger.debug("Earnings dates failed for %s: %s", ticker, e)

    # Dividends
    try:
        divs = tk.dividends
        if divs is not None and len(divs) > 0:
            recent_divs = divs.tail(8).reset_index()
            recent_divs.columns = [str(c) for c in recent_divs.columns]
            data["dividends"] = recent_divs.to_dict(orient="records")
    except Exception as e:
        logger.debug("Dividends failed for %s: %s", ticker, e)

    # Options — aggregate open interest and implied vol
    try:
        if tk.options:
            next_exp = tk.options[0]
            chain = tk.option_chain(next_exp)
            calls = chain.calls
            puts = chain.puts
            data["options_summary"] = {
                "next_expiry": next_exp,
                "total_call_oi": int(calls["openInterest"].sum()) if "openInterest" in calls.columns else 0,
                "total_put_oi": int(puts["openInterest"].sum()) if "openInterest" in puts.columns else 0,
                "put_call_oi_ratio": round(
                    float(puts["openInterest"].sum() / max(calls["openInterest"].sum(), 1)), 3
                ) if "openInterest" in calls.columns else None,
                "avg_call_iv": round(float(calls["impliedVolatility"].mean()), 4) if "impliedVolatility" in calls.columns else None,
                "avg_put_iv": round(float(puts["impliedVolatility"].mean()), 4) if "impliedVolatility" in puts.columns else None,
                "num_expirations": len(tk.options),
            }
    except Exception as e:
        logger.debug("Options failed for %s: %s", ticker, e)

    return data


def _safe_val(v: Any) -> Any:
    """Convert numpy/pandas types to JSON-serializable Python types."""
    import numpy as np
    if isinstance(v, (float, int)):
        if np.isnan(v) or np.isinf(v):
            return None
        return v
    if hasattr(v, "item"):
        val = v.item()
        if isinstance(val, float) and (pd.isna(val) or pd.isinf(val)):
            return None
        return val
    return v


def update_fundamentals(ticker: str) -> bool:
    """Fetch and store fundamentals for a ticker. Returns True if successful."""
    if not _is_equity(ticker):
        return False

    # Only refresh if data is stale (> 1 day old)
    path = _fundamentals_path(ticker)
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            fetched = existing.get("fetched_at")
            if fetched and date.fromisoformat(fetched) >= date.today():
                return True  # Already fresh
        except Exception:
            pass

    try:
        data = _fetch_fundamentals(ticker)
        FUNDAMENTALS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))
        return True
    except Exception as e:
        logger.warning("Fundamentals failed for %s: %s", ticker, e)
        return False


# ---------------------------------------------------------------------------
# Bulk update operations
# ---------------------------------------------------------------------------


def update_all_prices(
    start_date: date = date(2026, 1, 1),
    intervals: list[str] | None = None,
    batch_size: int = 20,
) -> dict[str, dict[str, int]]:
    """Update price data for all tickers at all intervals."""
    if intervals is None:
        intervals = ["1d", "1h"]

    tickers = get_all_tickers()
    results: dict[str, dict[str, int]] = {}
    failed: list[str] = []

    for interval in intervals:
        logger.info("── Updating %s data for %d tickers ──", interval, len(tickers))
        interval_results: dict[str, int] = {}

        for i, ticker in enumerate(tickers):
            try:
                n = update_ticker_prices(ticker, start_date, interval)
                interval_results[ticker] = n
                if n > 0:
                    logger.info("[%d/%d] %s (%s): +%d rows", i + 1, len(tickers), ticker, interval, n)
            except Exception as e:
                logger.warning("[%d/%d] %s (%s): FAILED - %s", i + 1, len(tickers), ticker, interval, e)
                failed.append(f"{ticker}({interval})")

            if (i + 1) % batch_size == 0:
                time.sleep(1)

        total = sum(interval_results.values())
        logger.info("%s update: %d new rows, %d failed", interval, total, len(failed))
        results[interval] = interval_results

    return results


def update_all_fundamentals(batch_size: int = 10) -> dict[str, bool]:
    """Update fundamentals for all equity tickers."""
    tickers = [t for t in get_all_tickers() if _is_equity(t)]
    logger.info("── Updating fundamentals for %d equity tickers ──", len(tickers))

    results: dict[str, bool] = {}
    succeeded = 0
    failed = 0

    for i, ticker in enumerate(tickers):
        ok = update_fundamentals(ticker)
        results[ticker] = ok
        if ok:
            succeeded += 1
            if (i + 1) % 50 == 0:
                logger.info("[%d/%d] fundamentals: %d ok, %d failed", i + 1, len(tickers), succeeded, failed)
        else:
            failed += 1

        # Rate limit: yfinance info calls are heavier
        if (i + 1) % batch_size == 0:
            time.sleep(2)

    logger.info("Fundamentals update: %d succeeded, %d failed", succeeded, failed)
    return results


def update_all(start_date: date = date(2026, 1, 1), batch_size: int = 20) -> None:
    """Full update: daily prices + 1h prices + fundamentals + macro."""
    update_all_prices(start_date, intervals=["1d", "1h"], batch_size=batch_size)
    update_all_fundamentals(batch_size=10)
    update_all_macro(start_date)


# ---------------------------------------------------------------------------
# Read helpers for market_data.py integration
# ---------------------------------------------------------------------------


def get_historical_from_store(
    ticker: str, from_date: str, to_date: str, interval: str = "1d",
) -> pd.DataFrame:
    """Read stored price data for a ticker within a date range."""
    df = load_ticker(ticker, interval)
    if df.empty:
        return df

    dt_col = "datetime" if interval != "1d" else "date"
    mask = (df[dt_col] >= pd.Timestamp(from_date)) & (df[dt_col] <= pd.Timestamp(to_date))
    return df.loc[mask].reset_index(drop=True)


def get_fundamentals_from_store(ticker: str) -> dict[str, Any] | None:
    """Read stored fundamentals for a ticker."""
    path = _fundamentals_path(ticker)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def store_has_data(ticker: str, interval: str = "1d") -> bool:
    """Check if we have any stored data for a ticker."""
    return _ticker_path(ticker, interval).exists()


def store_coverage() -> dict[str, Any]:
    """Report on the store's coverage."""
    tickers = get_all_tickers()

    daily_stored = 0
    hourly_stored = 0
    fundamentals_stored = 0
    daily_rows = 0
    hourly_rows = 0
    min_date = None
    max_date = None

    for t in tickers:
        # Daily
        df = load_ticker(t, "1d")
        if not df.empty:
            daily_stored += 1
            daily_rows += len(df)
            t_min = df["date"].min().date()
            t_max = df["date"].max().date()
            if min_date is None or t_min < min_date:
                min_date = t_min
            if max_date is None or t_max > max_date:
                max_date = t_max

        # Hourly
        df_h = load_ticker(t, "1h")
        if not df_h.empty:
            hourly_stored += 1
            hourly_rows += len(df_h)

        # Fundamentals
        if _fundamentals_path(t).exists():
            fundamentals_stored += 1

    equity_count = sum(1 for t in tickers if _is_equity(t))

    # Macro (FRED)
    macro_stored = 0
    macro_rows = 0
    for series_id in FRED_SERIES.values():
        df_m = load_macro_series(series_id)
        if not df_m.empty:
            macro_stored += 1
            macro_rows += len(df_m)

    return {
        "total_tickers": len(tickers),
        "daily": f"{daily_stored} tickers, {daily_rows:,} rows",
        "hourly": f"{hourly_stored} tickers, {hourly_rows:,} rows",
        "fundamentals": f"{fundamentals_stored}/{equity_count} equities",
        "macro": f"{macro_stored}/{len(FRED_SERIES)} FRED series, {macro_rows:,} rows",
        "date_range": f"{min_date} to {max_date}" if min_date else "no data",
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="ARGOS EOD data store")
    parser.add_argument(
        "--start", default="2026-01-01",
        help="Start date for initial backfill (default: 2026-01-01)",
    )
    parser.add_argument(
        "--only", choices=["prices", "fundamentals", "daily", "hourly", "macro"],
        help="Only update a specific data type",
    )
    parser.add_argument(
        "--coverage", action="store_true",
        help="Print store coverage report and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.coverage:
        report = store_coverage()
        for k, v in report.items():
            print(f"  {k}: {v}")
        return

    start = date.fromisoformat(args.start)

    if args.only == "prices":
        update_all_prices(start, intervals=["1d", "1h"])
    elif args.only == "daily":
        update_all_prices(start, intervals=["1d"])
    elif args.only == "hourly":
        update_all_prices(start, intervals=["1h"])
    elif args.only == "fundamentals":
        update_all_fundamentals()
    elif args.only == "macro":
        update_all_macro(start)
    else:
        update_all(start)


if __name__ == "__main__":
    main()
