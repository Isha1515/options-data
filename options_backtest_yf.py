"""
Options Strategy Backtester  (yfinance data edition)
======================================================
Reads data collected by collect_options_data.py and backtests
two options-selling strategies.

Strategy 1 — Iron Condors
    Sell OTM put spread + OTM call spread on the same expiration.
    Entry filter : IV 30-80%, IV Rank 15-20, delta 0.15-0.25, DTE 30-45.
    Exit         : 50% of max profit.

Strategy 2 — Directional Credit Spreads
    20 SMA > 50 SMA  →  sell put credit spread (bullish)
    50 SMA > 20 SMA  →  sell call credit spread (bearish)
    Same IV / delta / DTE filters as Strategy 1.
    Exit : 50% of max profit.

Account rules
    Starting capital : $5,000
    Max risk/trade   : 3% of current capital
    Max deployed     : 55% of capital at any time

Usage
    python options_backtest_yf.py --strategy 1
    python options_backtest_yf.py --strategy 2
    python options_backtest_yf.py --strategy 1 --symbols AAPL MSFT
    python options_backtest_yf.py --strategy 1 --data-dir ./mydata
"""

import os, glob, argparse, warnings
import pandas as pd
import numpy as np
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple

warnings.filterwarnings("ignore")

# ─── Strategy / Account Parameters ──────────────────────────────────────────

STARTING_CAPITAL = 5_000.0
MAX_RISK_PCT     = 0.03       # 3 % max loss per trade
MAX_DEPLOY_PCT   = 0.55       # 55 % capital deployed cap
TAKE_PROFIT_PCT  = 0.50       # exit at 50 % of max profit
IV_MIN, IV_MAX   = 30, 80
IVR_MIN, IVR_MAX = 15, 20
DELTA_MIN, DELTA_MAX = 0.15, 0.25
DTE_MIN, DTE_MAX = 30, 45
SMA_FAST, SMA_SLOW = 20, 50

# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class Leg:
    strike:      float
    option_type: str        # 'C' or 'P'
    expiration:  datetime
    entry_mid:   float      # credit = negative, debit = positive
    delta:       float


@dataclass
class Trade:
    symbol:       str
    strategy:     str
    entry_date:   datetime
    expiration:   datetime
    legs:         List[Leg]
    max_profit:   float     # dollars (net credit × 100 × contracts)
    max_loss:     float     # dollars (positive)
    target_pnl:   float     # 50 % of max_profit
    contracts:    int = 1
    status:       str = "open"   # open | win | loss | expired
    exit_date:    Optional[datetime] = None
    pnl:          float = 0.0
    at_risk:      float = 0.0    # max_loss × contracts


# ─── Data Loader ─────────────────────────────────────────────────────────────

class Loader:
    """
    Reads data/SYMBOL/YYYY-MM-DD.csv files written by collect_options_data.py.
    Returns a dict  {symbol: DataFrame}  with normalised columns.
    """

    def __init__(self, data_dir: str, symbols: Optional[List[str]] = None):
        self.data_dir = data_dir
        self.filter_symbols = set(symbols) if symbols else None

    def load(self) -> Dict[str, pd.DataFrame]:
        data: Dict[str, pd.DataFrame] = {}

        # Support both  data/SYMBOL/*.csv  and  data/*.csv  layouts
        sym_dirs = [
            d for d in glob.glob(os.path.join(self.data_dir, "*"))
            if os.path.isdir(d)
        ]

        if sym_dirs:
            # Per-symbol sub-directories  (collect_options_data.py layout)
            sources = {}
            for d in sym_dirs:
                sym = os.path.basename(d)
                if self.filter_symbols and sym not in self.filter_symbols:
                    continue
                files = sorted(glob.glob(os.path.join(d, "*.csv")))
                if files:
                    sources[sym] = files
        else:
            # Flat directory — group by 'symbol' column after loading
            files = sorted(glob.glob(os.path.join(self.data_dir, "*.csv")))
            sources = {"__flat__": files}

        if not sources:
            print(f"\n⚠️  No CSV files found in '{self.data_dir}'")
            print("   Run collect_options_data.py first to build your dataset.\n")
            return {}

        for sym, files in sources.items():
            frames = []
            for f in files:
                try:
                    frames.append(pd.read_csv(f, low_memory=False))
                except Exception as e:
                    print(f"  ✗ {os.path.basename(f)}: {e}")

            if not frames:
                continue

            df = pd.concat(frames, ignore_index=True)
            df = self._normalise(df)

            if sym == "__flat__":
                # Split by symbol column
                for s, g in df.groupby("symbol"):
                    if self.filter_symbols and s not in self.filter_symbols:
                        continue
                    data[s] = g.sort_values("quote_date").reset_index(drop=True)
                    print(f"  ✓ {s}: {len(data[s]):,} rows")
            else:
                data[sym] = df.sort_values("quote_date").reset_index(drop=True)
                print(f"  ✓ {sym}: {len(df):,} rows  ({df['quote_date'].dt.date.min()} → {df['quote_date'].dt.date.max()})")

        return data

    @staticmethod
    def _normalise(df: pd.DataFrame) -> pd.DataFrame:
        # Date columns
        for col in ("quote_date", "expiration"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")

        # Numeric columns
        for col in ("strike", "bid", "ask", "mid", "implied_volatility",
                    "delta", "dte", "iv_rank", "underlying_price"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Derived columns
        if "mid" not in df.columns:
            df["mid"] = (df["bid"] + df["ask"]) / 2

        if "abs_delta" not in df.columns:
            df["abs_delta"] = df["delta"].abs()

        # option_type: normalise to 'C' / 'P'
        if "option_type" in df.columns:
            df["option_type"] = (
                df["option_type"].astype(str).str.strip().str.upper().str[0]
            )

        # DTE from dates if missing
        if "dte" not in df.columns and "expiration" in df.columns and "quote_date" in df.columns:
            df["dte"] = (df["expiration"] - df["quote_date"]).dt.days

        return df


# ─── Helpers: sizing & capital tracking ──────────────────────────────────────

def contracts_for(max_loss_per_contract: float, capital: float) -> int:
    if max_loss_per_contract <= 0:
        return 0
    max_dollars = capital * MAX_RISK_PCT
    return max(1, int(max_dollars / (max_loss_per_contract * 100)))


def deployed(open_trades: List[Trade]) -> float:
    return sum(t.at_risk for t in open_trades if t.status == "open")


# ─── Option chain helpers ─────────────────────────────────────────────────────

def day_chain(df: pd.DataFrame, qdate: datetime, opt_type: str) -> pd.DataFrame:
    """Slice chain for one date + option type, applying all entry filters."""
    mask = (
        (df["quote_date"] == qdate) &
        (df["option_type"] == opt_type) &
        (df["implied_volatility"] >= IV_MIN) &
        (df["implied_volatility"] <= IV_MAX) &
        (df["iv_rank"] >= IVR_MIN) &
        (df["iv_rank"] <= IVR_MAX) &
        (df["abs_delta"] >= DELTA_MIN) &
        (df["abs_delta"] <= DELTA_MAX) &
        (df["dte"] >= DTE_MIN) &
        (df["dte"] <= DTE_MAX) &
        (df["mid"] > 0)
    )
    return df[mask].copy()


def best_expiry(expirations, qdate: datetime) -> Optional[datetime]:
    """Pick expiration closest to 38 DTE within the allowed window."""
    target = 38
    best, best_diff = None, 999
    for exp in expirations:
        dte = (exp - qdate).days
        if DTE_MIN <= dte <= DTE_MAX:
            diff = abs(dte - target)
            if diff < best_diff:
                best_diff, best = diff, exp
    return best


def short_leg(chain: pd.DataFrame, target_delta: float = 0.20) -> Optional[pd.Series]:
    if chain.empty:
        return None
    chain = chain.copy()
    chain["_dd"] = (chain["abs_delta"] - target_delta).abs()
    return chain.nsmallest(1, "_dd").iloc[0]


def long_leg(df: pd.DataFrame, short: pd.Series, opt_type: str) -> Optional[pd.Series]:
    """Nearest OTM strike beyond the short leg (same expiry, same type)."""
    same = df[
        (df["quote_date"] == short["quote_date"]) &
        (df["expiration"] == short["expiration"]) &
        (df["option_type"] == opt_type) &
        (df["mid"] > 0)
    ]
    if opt_type == "P":
        cands = same[same["strike"] < short["strike"]].nlargest(1, "strike")
    else:
        cands = same[same["strike"] > short["strike"]].nsmallest(1, "strike")
    return cands.iloc[0] if not cands.empty else None


# ─── Trade builders ───────────────────────────────────────────────────────────

def build_iron_condor(
    df: pd.DataFrame, symbol: str, qdate: datetime,
    capital: float, open_trades: List[Trade]
) -> Optional[Trade]:

    puts  = day_chain(df, qdate, "P")
    calls = day_chain(df, qdate, "C")
    if puts.empty or calls.empty:
        return None

    exp = best_expiry(
        set(puts["expiration"].unique()) & set(calls["expiration"].unique()), qdate
    )
    if exp is None:
        return None

    sp = short_leg(puts[puts["expiration"] == exp])
    if sp is None: return None
    lp = long_leg(df, sp, "P")
    if lp is None: return None

    sc = short_leg(calls[calls["expiration"] == exp])
    if sc is None: return None
    lc = long_leg(df, sc, "C")
    if lc is None: return None

    net_credit   = sp["mid"] + sc["mid"] - lp["mid"] - lc["mid"]
    if net_credit <= 0:
        return None

    put_width    = sp["strike"] - lp["strike"]
    call_width   = lc["strike"] - sc["strike"]
    spread_width = min(put_width, call_width)
    max_loss_ps  = spread_width - net_credit       # per share
    if max_loss_ps <= 0:
        return None

    n = contracts_for(max_loss_ps, capital)
    total_risk = n * max_loss_ps * 100
    avail = capital * MAX_DEPLOY_PCT - deployed(open_trades)
    if total_risk > avail:
        n = int(avail / (max_loss_ps * 100))
    if n < 1:
        return None

    legs = [
        Leg(sp["strike"], "P", exp, -sp["mid"],  sp["delta"]),
        Leg(lp["strike"], "P", exp,  lp["mid"],  lp["delta"]),
        Leg(sc["strike"], "C", exp, -sc["mid"],  sc["delta"]),
        Leg(lc["strike"], "C", exp,  lc["mid"],  lc["delta"]),
    ]
    mp = net_credit * n * 100
    ml = max_loss_ps * n * 100
    return Trade(symbol, "iron_condor", qdate, exp, legs,
                 mp, ml, mp * TAKE_PROFIT_PCT, n, at_risk=ml)


def build_credit_spread(
    df: pd.DataFrame, symbol: str, qdate: datetime,
    spread_type: str, capital: float, open_trades: List[Trade]
) -> Optional[Trade]:
    """spread_type: 'put_credit_spread' or 'call_credit_spread'"""

    otype = "P" if spread_type == "put_credit_spread" else "C"
    chain = day_chain(df, qdate, otype)
    if chain.empty:
        return None

    exp = best_expiry(chain["expiration"].unique(), qdate)
    if exp is None:
        return None

    sl = short_leg(chain[chain["expiration"] == exp])
    if sl is None: return None
    ll = long_leg(df, sl, otype)
    if ll is None: return None

    net_credit = sl["mid"] - ll["mid"]
    if net_credit <= 0:
        return None

    width = (sl["strike"] - ll["strike"]) if otype == "P" else (ll["strike"] - sl["strike"])
    max_loss_ps = width - net_credit
    if max_loss_ps <= 0:
        return None

    n = contracts_for(max_loss_ps, capital)
    total_risk = n * max_loss_ps * 100
    avail = capital * MAX_DEPLOY_PCT - deployed(open_trades)
    if total_risk > avail:
        n = int(avail / (max_loss_ps * 100))
    if n < 1:
        return None

    legs = [
        Leg(sl["strike"], otype, exp, -sl["mid"], sl["delta"]),
        Leg(ll["strike"], otype, exp,  ll["mid"], ll["delta"]),
    ]
    mp = net_credit * n * 100
    ml = max_loss_ps * n * 100
    return Trade(symbol, spread_type, qdate, exp, legs,
                 mp, ml, mp * TAKE_PROFIT_PCT, n, at_risk=ml)


# ─── Trade management ─────────────────────────────────────────────────────────

def current_pnl(df: pd.DataFrame, trade: Trade, qdate: datetime) -> Optional[float]:
    """
    Estimate current P&L by looking up each leg's current mid price.
    Returns None if any leg can't be found in today's chain.
    """
    total = 0.0
    for leg in trade.legs:
        row = df[
            (df["quote_date"] == qdate) &
            (df["expiration"] == leg.expiration) &
            (df["strike"] == leg.strike) &
            (df["option_type"] == leg.option_type)
        ]
        if row.empty:
            return None
        cur = row.iloc[0]["mid"]
        # entry_mid < 0  →  we sold; P&L = |entry_mid| - current_mid
        # entry_mid > 0  →  we bought; P&L = current_mid - entry_mid
        if leg.entry_mid < 0:
            total += (abs(leg.entry_mid) - cur) * trade.contracts * 100
        else:
            total += (cur - leg.entry_mid) * trade.contracts * 100
    return total


def manage_trades(
    df: pd.DataFrame, open_trades: List[Trade], qdate: datetime
) -> Tuple[List[Trade], float]:
    realized = 0.0
    for t in open_trades:
        if t.status != "open":
            continue

        # Expired
        if qdate >= t.expiration:
            t.status, t.exit_date = "expired", qdate
            # Simplified: assume expires worthless (full profit)
            # A full implementation would check underlying vs strikes
            t.pnl = t.max_profit
            realized += t.pnl
            continue

        pnl = current_pnl(df, t, qdate)
        if pnl is None:
            continue

        # Take profit at 50 % of max profit
        if pnl >= t.target_pnl:
            t.status, t.exit_date, t.pnl = "win", qdate, pnl
            realized += t.pnl

    return open_trades, realized


# ─── SMA trend (Strategy 2) ───────────────────────────────────────────────────

def build_sma_table(df: pd.DataFrame) -> pd.DataFrame:
    prices = (
        df.groupby("quote_date")["underlying_price"]
        .mean()
        .reset_index()
        .sort_values("quote_date")
        .rename(columns={"underlying_price": "price"})
    )
    prices["sma_fast"] = prices["price"].rolling(SMA_FAST).mean()
    prices["sma_slow"] = prices["price"].rolling(SMA_SLOW).mean()
    return prices.set_index("quote_date")


def trend_on(sma: pd.DataFrame, qdate: datetime) -> Optional[str]:
    past = sma[sma.index <= qdate].dropna(subset=["sma_fast", "sma_slow"])
    if past.empty:
        return None
    row = past.iloc[-1]
    return "bullish" if row["sma_fast"] > row["sma_slow"] else "bearish"


# ─── Backtester ───────────────────────────────────────────────────────────────

class Backtester:
    def __init__(self, data: Dict[str, pd.DataFrame]):
        self.data       = data
        self.capital    = STARTING_CAPITAL
        self.trades:    List[Trade] = []
        self.equity:    List[Dict]  = []

    # ── Strategy 1 ──────────────────────────────────────────────────────────

    def _run_s1(self, symbol: str):
        df     = self.data[symbol]
        dates  = sorted(df["quote_date"].unique())
        open_t: List[Trade] = []

        for qdate in dates:
            open_t, pnl = manage_trades(df, open_t, qdate)
            self.capital += pnl
            done = [t for t in open_t if t.status != "open"]
            self.trades.extend(done)
            open_t = [t for t in open_t if t.status == "open"]

            trade = build_iron_condor(df, symbol, qdate, self.capital, open_t)
            if trade:
                open_t.append(trade)

            self.equity.append({"date": qdate, "capital": self.capital})

        self.trades.extend(open_t)

    # ── Strategy 2 ──────────────────────────────────────────────────────────

    def _run_s2(self, symbol: str):
        df     = self.data[symbol]
        sma    = build_sma_table(df)
        dates  = sorted(df["quote_date"].unique())
        open_t: List[Trade] = []

        for qdate in dates:
            open_t, pnl = manage_trades(df, open_t, qdate)
            self.capital += pnl
            done = [t for t in open_t if t.status != "open"]
            self.trades.extend(done)
            open_t = [t for t in open_t if t.status == "open"]

            t = trend_on(sma, qdate)
            if t is None:
                continue
            spread = "put_credit_spread" if t == "bullish" else "call_credit_spread"
            trade  = build_credit_spread(df, symbol, qdate, spread, self.capital, open_t)
            if trade:
                open_t.append(trade)

            self.equity.append({"date": qdate, "capital": self.capital})

        self.trades.extend(open_t)

    # ── Run all symbols ──────────────────────────────────────────────────────

    def run(self, strategy: int):
        label = "Iron Condors" if strategy == 1 else "Directional Credit Spreads"
        print(f"\n{'='*58}")
        print(f"  Strategy {strategy}: {label}")
        print(f"  Symbols : {', '.join(self.data.keys())}")
        print(f"  Capital : ${self.capital:,.2f}")
        print(f"{'='*58}\n")

        for sym in self.data:
            print(f"  ▸ {sym}...")
            if strategy == 1:
                self._run_s1(sym)
            else:
                self._run_s2(sym)

    # ── Report ───────────────────────────────────────────────────────────────

    def report(self, output_csv: Optional[str] = None) -> pd.DataFrame:
        if not self.trades:
            print("\n⚠️  No trades generated.")
            print("   Check that your data passes the IV/IVR/delta/DTE filters.")
            return pd.DataFrame()

        rows = []
        for t in self.trades:
            rows.append({
                "symbol":     t.symbol,
                "strategy":   t.strategy,
                "entry_date": t.entry_date.date() if hasattr(t.entry_date, "date") else t.entry_date,
                "exit_date":  t.exit_date.date()  if t.exit_date and hasattr(t.exit_date, "date") else t.exit_date,
                "expiration": t.expiration.date() if hasattr(t.expiration, "date") else t.expiration,
                "contracts":  t.contracts,
                "max_profit": round(t.max_profit, 2),
                "max_loss":   round(t.max_loss, 2),
                "pnl":        round(t.pnl, 2),
                "status":     t.status,
                "at_risk":    round(t.at_risk, 2),
            })
        df = pd.DataFrame(rows)

        closed = df[df["status"] != "open"]
        n       = len(closed)
        wins    = closed[closed["pnl"] > 0]
        losses  = closed[closed["pnl"] <= 0]
        win_rt  = len(wins) / n * 100 if n else 0
        avg_w   = wins["pnl"].mean()   if not wins.empty else 0
        avg_l   = losses["pnl"].mean() if not losses.empty else 0
        total_pnl = closed["pnl"].sum()
        pf      = abs(wins["pnl"].sum() / losses["pnl"].sum()) if not losses.empty and losses["pnl"].sum() != 0 else float("inf")

        eq = pd.DataFrame(self.equity)
        if not eq.empty:
            eq["peak"] = eq["capital"].cummax()
            eq["dd"]   = (eq["capital"] - eq["peak"]) / eq["peak"] * 100
            max_dd = eq["dd"].min()
        else:
            max_dd = 0

        final = STARTING_CAPITAL + total_pnl

        print(f"\n{'='*58}")
        print(f"  BACKTEST RESULTS")
        print(f"{'='*58}")
        print(f"  Starting Capital : ${STARTING_CAPITAL:>10,.2f}")
        print(f"  Final Capital    : ${final:>10,.2f}")
        print(f"  Total P&L        : ${total_pnl:>+10,.2f}  ({total_pnl/STARTING_CAPITAL*100:+.1f}%)")
        print(f"  Total Trades     : {n}")
        print(f"  Win Rate         : {win_rt:.1f}%")
        print(f"  Avg Win          : ${avg_w:>9,.2f}")
        print(f"  Avg Loss         : ${avg_l:>9,.2f}")
        print(f"  Profit Factor    : {pf:.2f}")
        print(f"  Max Drawdown     : {max_dd:.1f}%")
        print(f"{'='*58}\n")

        # Breakdown by symbol
        if df["symbol"].nunique() > 1:
            print("  Per-symbol breakdown:")
            for sym, g in closed.groupby("symbol"):
                sym_pnl = g["pnl"].sum()
                sym_wr  = (g["pnl"] > 0).mean() * 100
                print(f"    {sym:<6}  trades={len(g)}  win%={sym_wr:.0f}  P&L=${sym_pnl:+,.2f}")
            print()

        if output_csv:
            df.to_csv(output_csv, index=False)
            print(f"  Trade log saved → {output_csv}\n")

        return df


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Options Backtester (yfinance data edition)")
    ap.add_argument("--strategy",  type=int, choices=[1, 2], default=1,
                    help="1 = Iron Condors, 2 = Credit Spreads (default: 1)")
    ap.add_argument("--data-dir",  type=str, default="./data",
                    help="Folder containing collected data (default: ./data)")
    ap.add_argument("--symbols",   type=str, nargs="+", default=None,
                    help="Filter to specific symbols (default: all found)")
    ap.add_argument("--output",    type=str, default="backtest_results.csv",
                    help="Trade log output CSV (default: backtest_results.csv)")
    args = ap.parse_args()

    print("\n" + "="*58)
    print("  OPTIONS BACKTESTER  —  yfinance data edition")
    print("="*58)
    print(f"  Data dir : {os.path.abspath(args.data_dir)}")
    print(f"  Strategy : {args.strategy}")
    if args.symbols:
        print(f"  Symbols  : {', '.join(args.symbols)}")

    print("\nLoading data...")
    loader = Loader(args.data_dir, args.symbols)
    data   = loader.load()

    if not data:
        return

    bt = Backtester(data)
    bt.run(args.strategy)
    bt.report(args.output)


if __name__ == "__main__":
    main()
