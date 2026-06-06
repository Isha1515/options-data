"""
Daily Options Data Collector
==============================
Run this script once per day AFTER market close (4pm ET or later).
It pulls the full option chain for each symbol, calculates Greeks
using Black-Scholes, estimates IV Rank from rolling history, and
saves everything to CSV in the format expected by options_backtest.py.

Usage:
    python collect_options_data.py                  # collect all symbols
    python collect_options_data.py --symbols AAPL MSFT   # specific symbols
    python collect_options_data.py --data-dir ./mydata   # custom folder

Schedule (Windows Task Scheduler / Mac/Linux cron):
    # Run Mon-Fri at 4:30pm ET
    # Linux/Mac crontab:  30 16 * * 1-5 /usr/bin/python3 /path/to/collect_options_data.py
    # Windows: use Task Scheduler to run daily at 16:30

Requirements:
    pip install yfinance pandas numpy scipy
"""

import os
import time
import argparse
import logging
from datetime import datetime, date, timedelta
from math import log, sqrt, exp
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm
from scipy.optimize import brentq

# ─── Configuration ───────────────────────────────────────────────────────────

SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META",
    "GOOGL", "TSLA", "JPM", "AVGO", "LLY",
    "UNH", "XOM", "V", "MA", "HD",
    "PG", "JNJ", "ORCL", "MRK", "ABBV"
]

DATA_DIR        = "./data"
RISK_FREE_RATE  = 0.05      # Approximate risk-free rate (update periodically)
DTE_MIN         = 7         # Skip options expiring sooner than this
DTE_MAX         = 90        # Skip options expiring later than this
IV_RANK_WINDOW  = 252       # Trading days for IV rank calculation (~1 year)
SLEEP_BETWEEN   = 2         # Seconds between symbol requests (be polite to Yahoo)

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("collector.log", mode='a')
    ]
)
log = logging.getLogger(__name__)

# ─── Black-Scholes Greeks ─────────────────────────────────────────────────────

def bs_d1_d2(S, K, T, r, sigma):
    """Compute d1 and d2 for Black-Scholes."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None, None
    d1 = (log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    return d1, d2


def bs_price(S, K, T, r, sigma, option_type='call'):
    d1, d2 = bs_d1_d2(S, K, T, r, sigma)
    if d1 is None:
        return np.nan
    if option_type == 'call':
        return S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2)
    else:
        return K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def implied_volatility(market_price, S, K, T, r, option_type='call'):
    """Compute IV via Brent's method. Returns NaN if it can't converge."""
    if T <= 0 or market_price <= 0:
        return np.nan
    intrinsic = max(0, S - K) if option_type == 'call' else max(0, K - S)
    if market_price < intrinsic:
        return np.nan
    try:
        iv = brentq(
            lambda sigma: bs_price(S, K, T, r, sigma, option_type) - market_price,
            1e-6, 20.0, xtol=1e-6, maxiter=200
        )
        return iv * 100  # return as percentage
    except (ValueError, RuntimeError):
        return np.nan


def calculate_greeks(S, K, T, r, sigma_pct, option_type='call'):
    """
    Calculate delta, gamma, theta, vega given sigma as a percentage (e.g. 35.0).
    Returns dict with all Greeks.
    """
    sigma = sigma_pct / 100.0
    d1, d2 = bs_d1_d2(S, K, T, r, sigma)
    if d1 is None:
        return dict(delta=np.nan, gamma=np.nan, theta=np.nan, vega=np.nan)

    nd1  = norm.cdf(d1)
    nd1_ = norm.pdf(d1)   # standard normal PDF at d1

    if option_type == 'call':
        delta = nd1
        theta = (-(S * nd1_ * sigma) / (2 * sqrt(T))
                 - r * K * exp(-r * T) * norm.cdf(d2)) / 365
    else:
        delta = nd1 - 1
        theta = (-(S * nd1_ * sigma) / (2 * sqrt(T))
                 + r * K * exp(-r * T) * norm.cdf(-d2)) / 365

    gamma = nd1_ / (S * sigma * sqrt(T))
    vega  = S * nd1_ * sqrt(T) / 100  # per 1% move in IV

    return dict(delta=round(delta, 6), gamma=round(gamma, 6),
                theta=round(theta, 6), vega=round(vega, 6))


# ─── IV Rank Calculation ──────────────────────────────────────────────────────

def load_iv_history(symbol: str, data_dir: str) -> pd.Series:
    """
    Load daily average IV history from previously saved CSVs.
    Returns a Series indexed by date.
    """
    pattern = os.path.join(data_dir, symbol, "*.csv")
    import glob
    files = glob.glob(pattern)
    if not files:
        return pd.Series(dtype=float)

    iv_records = []
    for f in files:
        try:
            df = pd.read_csv(f, usecols=['quote_date', 'implied_volatility'])
            df['quote_date'] = pd.to_datetime(df['quote_date'])
            daily_iv = df.groupby('quote_date')['implied_volatility'].mean()
            iv_records.append(daily_iv)
        except Exception:
            continue

    if not iv_records:
        return pd.Series(dtype=float)

    combined = pd.concat(iv_records).sort_index()
    combined = combined[~combined.index.duplicated(keep='last')]
    return combined


def calculate_iv_rank(current_iv: float, iv_history: pd.Series) -> float:
    """
    IV Rank = (current_IV - 52wk_low) / (52wk_high - 52wk_low) * 100
    Returns NaN if insufficient history.
    """
    recent = iv_history.tail(IV_RANK_WINDOW)
    if len(recent) < 20:  # need at least 20 days of history
        return np.nan
    iv_low  = recent.min()
    iv_high = recent.max()
    if iv_high == iv_low:
        return 50.0
    return round((current_iv - iv_low) / (iv_high - iv_low) * 100, 2)


# ─── Data Fetching ────────────────────────────────────────────────────────────

def fetch_risk_free_rate() -> float:
    """Try to get current 3-month T-bill rate from yfinance. Falls back to default."""
    try:
        tbill = yf.Ticker("^IRX")
        hist = tbill.history(period="5d")
        if not hist.empty:
            rate = hist['Close'].iloc[-1] / 100
            log.info(f"Risk-free rate fetched: {rate:.4f} ({rate*100:.2f}%)")
            return rate
    except Exception:
        pass
    log.info(f"Using default risk-free rate: {RISK_FREE_RATE:.4f}")
    return RISK_FREE_RATE


def fetch_option_chain(symbol: str, today: date, risk_free: float) -> Optional[pd.DataFrame]:
    """
    Fetch full option chain for a symbol, compute Greeks and IV.
    Returns a cleaned DataFrame ready for saving.
    """
    try:
        tk = yf.Ticker(symbol)

        # Get current stock price
        info = tk.fast_info
        spot = getattr(info, 'last_price', None) or getattr(info, 'regularMarketPrice', None)
        if not spot:
            hist = tk.history(period="2d")
            if hist.empty:
                log.warning(f"  {symbol}: Could not get spot price, skipping")
                return None
            spot = hist['Close'].iloc[-1]

        # Get all expirations
        expirations = tk.options
        if not expirations:
            log.warning(f"  {symbol}: No expirations available")
            return None

        rows = []
        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, '%Y-%m-%d').date()
            dte = (exp_date - today).days

            if dte < DTE_MIN or dte > DTE_MAX:
                continue

            T = dte / 365.0  # time to expiration in years

            try:
                chain = tk.option_chain(exp_str)
            except Exception as e:
                log.debug(f"  {symbol} {exp_str}: chain fetch failed — {e}")
                continue

            for opt_type, df in [('call', chain.calls), ('put', chain.puts)]:
                if df.empty:
                    continue

                for _, row in df.iterrows():
                    strike = row.get('strike', np.nan)
                    bid    = row.get('bid', np.nan)
                    ask    = row.get('ask', np.nan)
                    volume = row.get('volume', 0)
                    oi     = row.get('openInterest', 0)

                    # Skip illiquid / bad data
                    if pd.isna(bid) or pd.isna(ask):
                        continue
                    if bid < 0 or ask <= 0:
                        continue
                    if ask < bid:
                        continue

                    mid = (bid + ask) / 2

                    # yfinance returns IV as decimal (e.g. 0.35 = 35%)
                    raw_iv = row.get('impliedVolatility', np.nan)
                    if pd.isna(raw_iv) or raw_iv <= 0:
                        # Calculate IV ourselves from mid price
                        iv_pct = implied_volatility(mid, spot, strike, T, risk_free, opt_type)
                    else:
                        iv_pct = raw_iv * 100  # convert to percent

                    if pd.isna(iv_pct) or iv_pct <= 0 or iv_pct > 500:
                        continue

                    greeks = calculate_greeks(spot, strike, T, risk_free, iv_pct, opt_type)

                    rows.append({
                        'symbol':             symbol,
                        'quote_date':         today.strftime('%Y-%m-%d'),
                        'expiration':         exp_str,
                        'strike':             round(strike, 2),
                        'option_type':        opt_type[0].upper(),  # C or P
                        'bid':                round(bid, 4),
                        'ask':                round(ask, 4),
                        'mid':                round(mid, 4),
                        'implied_volatility': round(iv_pct, 4),
                        'delta':              greeks['delta'],
                        'gamma':              greeks['gamma'],
                        'theta':              greeks['theta'],
                        'vega':              greeks['vega'],
                        'dte':                dte,
                        'underlying_price':   round(spot, 4),
                        'volume':             int(volume) if not pd.isna(volume) else 0,
                        'open_interest':      int(oi) if not pd.isna(oi) else 0,
                    })

        if not rows:
            log.warning(f"  {symbol}: No valid option rows in DTE range {DTE_MIN}-{DTE_MAX}")
            return None

        df_out = pd.DataFrame(rows)
        log.info(f"  {symbol}: {len(df_out):>5,} rows  |  "
                 f"{df_out['expiration'].nunique()} expirations  |  "
                 f"spot=${spot:.2f}")
        return df_out

    except Exception as e:
        log.error(f"  {symbol}: Unexpected error — {e}")
        return None


def add_iv_rank(df: pd.DataFrame, symbol: str, data_dir: str) -> pd.DataFrame:
    """Add iv_rank column using historical IV data from saved CSVs."""
    iv_history = load_iv_history(symbol, data_dir)
    current_iv  = df['implied_volatility'].mean()

    iv_rank = calculate_iv_rank(current_iv, iv_history)

    if pd.isna(iv_rank):
        days_needed = IV_RANK_WINDOW - len(iv_history)
        log.info(f"  {symbol}: IV Rank = N/A (need ~{max(0,days_needed)} more trading days of history)")
        iv_rank = np.nan

    else:
        log.info(f"  {symbol}: IV Rank = {iv_rank:.1f}  |  Current IV = {current_iv:.1f}%")

    df['iv_rank'] = round(iv_rank, 2) if not pd.isna(iv_rank) else np.nan
    return df


# ─── Saving ──────────────────────────────────────────────────────────────────

def save_data(df: pd.DataFrame, symbol: str, today: date, data_dir: str):
    """Save today's data to data_dir/SYMBOL/YYYY-MM-DD.csv"""
    sym_dir = os.path.join(data_dir, symbol)
    os.makedirs(sym_dir, exist_ok=True)

    filepath = os.path.join(sym_dir, f"{today.strftime('%Y-%m-%d')}.csv")
    df.to_csv(filepath, index=False)
    log.info(f"  {symbol}: Saved → {filepath}")


# ─── Summary ─────────────────────────────────────────────────────────────────

def print_summary(results: dict, today: date):
    success = [s for s, ok in results.items() if ok]
    failed  = [s for s, ok in results.items() if not ok]

    print("\n" + "="*55)
    print(f"  COLLECTION SUMMARY  —  {today}")
    print("="*55)
    print(f"  ✓ Success : {len(success):>3}  —  {', '.join(success)}")
    if failed:
        print(f"  ✗ Failed  : {len(failed):>3}  —  {', '.join(failed)}")
    print("="*55)
    print(f"\n  Data saved in: {os.path.abspath(DATA_DIR)}/SYMBOL/YYYY-MM-DD.csv")
    print("  Run options_backtest.py once you have enough data collected.\n")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Daily Options Data Collector')
    parser.add_argument('--symbols',  nargs='+', default=None,
                        help='Symbols to collect (default: all 20 liquid S&P500 names)')
    parser.add_argument('--data-dir', type=str, default=DATA_DIR,
                        help=f'Output directory (default: {DATA_DIR})')
    parser.add_argument('--date',     type=str, default=None,
                        help='Override date YYYY-MM-DD (default: today)')
    parser.add_argument('--rate',     type=float, default=None,
                        help='Risk-free rate override e.g. 0.05 for 5%%')
    args = parser.parse_args()

    symbols  = args.symbols or SYMBOLS
    data_dir = args.data_dir
    today    = date.fromisoformat(args.date) if args.date else date.today()

    os.makedirs(data_dir, exist_ok=True)

    print("\n" + "="*55)
    print("  OPTIONS DATA COLLECTOR")
    print(f"  Date    : {today}")
    print(f"  Symbols : {len(symbols)}")
    print(f"  Save to : {os.path.abspath(data_dir)}")
    print("="*55 + "\n")

    # Risk-free rate
    risk_free = args.rate if args.rate else fetch_risk_free_rate()

    results = {}

    for i, symbol in enumerate(symbols, 1):
        print(f"[{i:>2}/{len(symbols)}] {symbol}")
        df = fetch_option_chain(symbol, today, risk_free)

        if df is None:
            results[symbol] = False
            time.sleep(SLEEP_BETWEEN)
            continue

        df = add_iv_rank(df, symbol, data_dir)
        save_data(df, symbol, today, data_dir)
        results[symbol] = True

        if i < len(symbols):
            time.sleep(SLEEP_BETWEEN)

    print_summary(results, today)


if __name__ == '__main__':
    main()
