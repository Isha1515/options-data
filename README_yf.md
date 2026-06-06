# Options Backtester — yfinance Data Edition

Backtest two options-selling strategies using data you collect
yourself for free with `collect_options_data.py` + `yfinance`.

---

## Files in this folder

| File | Purpose |
|------|---------|
| `collect_options_data.py` | Run daily after market close to build your dataset |
| `options_backtest_yf.py` | Backtest your strategies once you have enough data |
| `setup_schedule.py` | Auto-schedules the collector as a daily cron/task |
| `README.md` | This file |

---

## Quick Start

### 1 — Install dependencies

```bash
pip install yfinance pandas numpy scipy
```

### 2 — Start collecting data (run once now, then daily)

```bash
python collect_options_data.py
```

This saves one CSV per symbol per day into `./data/SYMBOL/YYYY-MM-DD.csv`.

### 3 — Automate the daily collection

```bash
python setup_schedule.py
```

Follow the printed instructions for your OS (cron on Mac/Linux,
Task Scheduler on Windows).

### 4 — Run the backtest once you have data

```bash
# Strategy 1: Iron Condors
python options_backtest_yf.py --strategy 1

# Strategy 2: Directional Credit Spreads
python options_backtest_yf.py --strategy 2

# Both strategies, specific symbols only
python options_backtest_yf.py --strategy 1 --symbols AAPL MSFT NVDA

# Custom data folder and output file
python options_backtest_yf.py --strategy 2 --data-dir ./mydata --output s2_trades.csv
```

---

## Data format

`collect_options_data.py` saves CSVs with these columns.
`options_backtest_yf.py` reads exactly this format — no conversion needed.

| Column | Description |
|--------|-------------|
| `symbol` | Ticker (AAPL, MSFT, …) |
| `quote_date` | Trading date (YYYY-MM-DD) |
| `expiration` | Option expiry date (YYYY-MM-DD) |
| `strike` | Strike price |
| `option_type` | C (call) or P (put) |
| `bid` / `ask` / `mid` | Option prices |
| `implied_volatility` | IV in percent (e.g. 35.0 = 35%) |
| `delta` | Black-Scholes delta (calls positive, puts negative) |
| `gamma` / `theta` / `vega` | Other Greeks |
| `dte` | Days to expiration at quote date |
| `underlying_price` | Stock price at quote date |
| `volume` / `open_interest` | Liquidity fields |
| `iv_rank` | IV Rank 0–100 (builds up over time as history grows) |

---

## Strategy rules

### Strategy 1 — Iron Condors

Sell an OTM put spread **and** an OTM call spread on the same expiration.

| Parameter | Value |
|-----------|-------|
| Structure | Short OTM put spread + short OTM call spread |
| IV filter | 30 – 80% |
| IV Rank filter | 15 – 20 |
| Short leg delta | 0.15 – 0.25 (absolute value) |
| DTE at entry | 30 – 45 days |
| Target expiry | ~38 DTE (midpoint) |
| Exit rule | Close at 50% of max profit |
| Loss rule | Hold to expiration (simplified) |

### Strategy 2 — Directional Credit Spreads

Sell the spread direction that matches the trend.

| Condition | Trade |
|-----------|-------|
| 20-day SMA **above** 50-day SMA | Sell **put** credit spread (bullish) |
| 50-day SMA **above** 20-day SMA | Sell **call** credit spread (bearish) |

All other parameters (IV, IVR, delta, DTE, exit) same as Strategy 1.

---

## Account & position sizing rules

| Rule | Value |
|------|-------|
| Starting capital | $5,000 |
| Max risk per trade | 3% of current capital |
| Max capital deployed at once | 55% |
| Contracts | Auto-sized to satisfy both rules above |
| Exit | 50% of max profit (take profit) |

**Example at $5,000:**
- Max risk per trade = $150 (3%)
- Max deployed = $2,750 (55%)
- If a spread's max loss is $75/share → 2 contracts ($150 risk)

---

## How IV Rank builds up

IV Rank compares today's IV to the past 52 weeks of IV.
The collector saves daily average IV, and the backtest reads
that history to compute the rank.

| Days collected | IV Rank accuracy |
|----------------|-----------------|
| < 20 days | N/A — shown as blank |
| 20 – 60 days | Partial (limited window) |
| ~90 days | Usable for backtesting |
| ~252 days | Full 52-week IV Rank |

The strategy filters require IV Rank 15–20, so trades will only
appear once enough history exists. **This is correct behaviour**,
not a bug.

---

## Realistic timeline

| When | What to do |
|------|-----------|
| Day 1 | Run `collect_options_data.py`, verify CSVs look right |
| Week 1 | Check data is being saved daily by the scheduler |
| Month 1 | Run a first backtest (few trades due to IV Rank gap) |
| Month 3 | Meaningful backtest with solid IV Rank history |
| Month 6+ | Full statistical sample across multiple market cycles |

---

## Troubleshooting

**"No trades generated"**
- IV Rank is likely still N/A (need ~20+ trading days of history)
- Check that your data has IV between 30–80% and delta 0.15–0.25
- Run: `python -c "import pandas as pd; df=pd.read_csv('data/AAPL/latest.csv'); print(df[['implied_volatility','iv_rank','delta','dte']].describe())"`

**"No CSV files found"**
- Make sure `collect_options_data.py` has run at least once
- Check the `--data-dir` path matches where data was saved

**Yahoo Finance rate limit / empty data**
- Yahoo occasionally blocks requests; wait and retry
- The collector has a 2-second pause between symbols to reduce this
- Try running outside market hours (before 9am or after 5pm ET)

**Delta looks wrong**
- Puts should have negative delta (−0.15 to −0.25)
- Calls should have positive delta (+0.15 to +0.25)
- The backtest uses `abs_delta` internally, so sign doesn't matter for filtering
