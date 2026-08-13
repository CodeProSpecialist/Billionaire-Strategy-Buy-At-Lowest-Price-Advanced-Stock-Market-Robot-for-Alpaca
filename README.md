**Billionaire-Strategy-Buy-At-Lowest-Price-Advanced-Stock-Market-Robot-for-Alpaca**

**2026 Edition – Advanced Stock Market Trading Robot, Version 10**
(Billionaire Buying Strategy)

This is an upgraded, fully automated Alpaca trading robot that implements a disciplined "buy at the lowest reasonable price" strategy. The core idea remains the same: you cannot control the eventual sell price, so the robot focuses on high-quality technical entries while managing risk, exits, and portfolio exposure with modern margin-account rules.

### Major 2026 Upgrades (as of August 13, 2026)
- **Fully integrated in-process stock scanner.** The S&P 500 scanner is no longer a separate script and no longer writes any text files. It runs inside the bot itself, produces a Python list of top-ranked candidate symbols, and stores it in an in-memory `SYMBOLS_TO_BUY_LIST` cache. The scanner runs synchronously on first startup (to populate the cache) and refreshes once per day on a background thread at 16:15 ET. **Purchases no longer remove symbols from the list** — the daily-refreshed universe stays intact all session, so the same candidate can be re-evaluated as conditions change.
- **Real VWAP everywhere.** Both the scanner's daily-bar VWAP and the buy loop's intraday VWAP are now textbook cumulative, volume-weighted, session/period-anchored values (`Σ(TypicalPrice × Volume) / Σ(Volume)`). The daily scanner uses an anchored VWAP over each lookback slice instead of the old rolling `SMA(TP·V)/SMA(V)` VWMA. The intraday distance signal now pulls real 1-minute OHLCV bars from yfinance and computes the true session-anchored VWAP that brokers and charting packages draw — replacing the earlier price-only arithmetic mean of 1-minute samples. Cached for 60 seconds per symbol so it refreshes with each new 1-minute bar without hammering the rate limiter.
- **TensorFlow ML brain**: a Conv1D → LSTM → LSTM → Dense sequence model that adds a small +/- adjustment to the buy score once enough closed live trades exist. Trained on 2 years of daily bars pulled from public market-data sources over the same in-memory candidate universe the bot actually trades. First run pretrains on ~2,500 examples; a scheduled daily run at 17:00 ET (must finish by 07:45 ET) trains on ~15,000 examples per night, capped at a total lifetime of 20,000 pretraining examples. After the cap, the daily 17:00 slot switches to maintenance training on the past 24 hours of live win/loss outcomes only. The ML output is capped at ±1.5 score points and never fires until at least 60 live closed trades exist — it is never a hard gate.
- **Real ATR-based hard stop-loss** that fires independently of the profit monitor: any position that drops past 2×ATR (floored at −3%) is force-sold via the same reliable escalation chain the sweeps use, regardless of whether the profit monitor has armed yet. Position sizing now references the same multiplier the stop actually enforces, so the "1% risk per trade" target is real, not fictional.
- **Intraday-pullback signal** added alongside the daily-close dip measurement — the bot can now recognize a pullback from today's intraday high, not just a decline vs. yesterday's close.
- **Peak-based ATR giveback**: the profit monitor's give-back allowance now scales with the position's actual peak gain, so a strong trend gets proportionally more room to breathe before the exit fires (with the arm-based calculation kept as a floor).
- **Empirical expected-return ranking** when enough trade history exists: candidates are ranked by the bot's own real historical average outcome at each score bucket, not just raw score / ATR%.
- **Cancel-first sweep behavior**: every pre-market and pre-close sweep now cancels any pre-existing open sell orders on the symbol first, so the sweep always owns the full position instead of only the shares not already tied up in a stale resting order.
- **Fill-confirmed scale-outs**: scale-out tranches now poll to terminal status before marking the stage complete and raising the profit floor to breakeven — a rejected, delayed, or canceled order is no longer silently treated as a completed sale.
- **Tightened MOO submission window** (9:25:00 – 9:27:30 ET) with a defense-in-depth guard inside the escalation chain, so a slow loop tick can't accidentally submit an OPG order past Alpaca's ~9:28 AM cutoff.
- **No more PDT / day-trade counting.** The robot operates under margin-account risk controls only. Unlimited same-day round-trips are allowed.
- Local SQLite database is automatically reconciled against live Alpaca positions on every startup. Stale rows are removed, missing positions are added, and quantities/average prices are corrected to match the broker.
- Market-regime awareness (Bull / Sideways / Bear / Panic) using VIX + SPY 20/50-day SMAs.
- Dynamic, auto-adapting buy-score thresholds and signal weights that learn from closed-trade outcomes (with hard safety guardrails).
- Multi-timeframe confirmation (daily + 60-minute + 5-minute).
- Volatility-scaled profit targets (ATR-based arming and give-back).
- Peak-following profit monitor + optional scaled (tranche) exits.
- Pre-market (≈9:25 AM ET) and pre-close (≈3:45 PM ET) profit sweeps with a 3-step order-escalation chain.
- Portfolio-level liquidation when remaining positions are net profitable.
- Heavy caching, batched data downloads, and a strict shared rate-limit gate.
- Thread-safe position book with per-symbol claims so buy and sell threads cannot race.

### How the Strategy Works

**Stock Universe**
The robot maintains its candidate universe entirely in memory. On first startup it runs the built-in scanner synchronously against the full S&P 500 list, ranks every symbol on 1-year and 2-year lookbacks using RSI, MACD, real anchored VWAP, Bollinger Bands, Stochastic, ADX, OBV, seasonal returns, and historical best-month bonuses, applies a sector cap and excluded-sector filter, and stores the top ~100 in the module-level `SYMBOLS_TO_BUY_LIST`. That list is refreshed once per day at 16:15 ET on a background daemon thread and is never mutated by trading activity — no text files are written or read at any point.

**Buy Logic (high-quality dip / reversal entries)**
A candidate must pass **all** of the following before it is even ranked:

1. Price above its 200-day SMA (uptrend filter).
2. Daily RSI ≤ 50 (not overbought).
3. Multi-timeframe confirmation: 60-minute trend bullish **and** 5-minute bar showing a bounce.
4. Not inside a 2-day earnings blackout window.
5. At least one bullish candlestick reversal pattern on the most recent bar (Hammer, Bullish Engulfing, Morning Star, Piercing Line, Three White Soldiers, Dragonfly Doji, Inverted Hammer, Tweezer Bottom, etc.).
6. A weighted buy score that meets or exceeds the current regime's dynamic threshold.

**Scoring signals** (weights change by market regime):
- Bullish candlestick pattern(s)
- RSI < 50 and/or falling
- Volume holding or expanding
- MACD above signal line
- Recent price dip (both daily-close and intraday-peak based)
- Distance below real session-anchored intraday VWAP
- Pattern-specific confirmation bonuses
- Price stability
- Relative strength vs SPY (bonus)
- ML brain adjustment (once ≥60 live trades on record; capped at ±1.5 points)

Candidates are ranked by empirical expected return ÷ ATR% (or raw score ÷ ATR% before enough history accumulates). Only the top `MAX_NEW_POSITIONS_PER_CYCLE` (default 3) are actually bought each cycle.

**Position Sizing**
- Risk-based (default 1% of equity risked per trade using 2 × ATR — the same multiplier the hard stop-loss actually enforces).
- Hard cap of `$MAX_ALLOCATION_PER_SYMBOL` (default $600).
- Respects overall portfolio exposure limit and available buying power.
- Fractional shares supported.
- Optional "$1 test mode".

**Exit Logic**
- **Hard stop-loss** (default on): Any position past −2×ATR (floored at −3%) is force-sold immediately via the escalation chain, independently of the profit monitor's armed state.
- **Peak-following Profit Monitor** (default on): Arms after a small gain (ATR-scaled), then trails a high-water mark. Give-back allowance scales with the actual peak gain so a strong trend gets more room. Sells on a controlled give-back from the peak while staying above a hard floor.
- **Scaled / tranche exits** (default on): Automatically sells fixed percentages of the *original* position size at +1% and +2%, then moves the remainder's floor to breakeven. Each tranche is polled to a confirmed fill before the stage is marked complete.
- Pre-market profit sweep (≈9:25 AM ET) and pre-close profit sweep (≈3:45 PM ET) that sell any individually profitable positions via a 3-step escalation (MOO/market → aggressive limit → market). Pre-existing sell orders on the symbol are always cancelled first so the sweep owns the full position.
- Portfolio-level liquidation: if the remaining positions are still net profitable as a group, they can all be closed together.

**Risk & Account Controls**
- Margin health floor (equity / long market value).
- Maximum portfolio exposure and leverage caps.
- Cash buffer.
- Full order confirmation and partial-fill handling.
- Automatic cancellation of resting sell orders before taking profit so the full position can be sold.

**Data & Reliability**
- SQLite database (`trading_bot.db`) with WAL mode, proper locking, and automatic reconciliation on every start.
- Trade history + feature snapshot table so the robot can learn which entry conditions actually produced profits.
- Adaptive parameter store that persists learned thresholds/weights across restarts.
- ML brain model file (`ml_brain_model/model.keras`) that persists across restarts and continues learning from new live outcomes.
- CSV trade log + detailed text log.
- Robust error handling, retries, and timeouts so a single hung API call cannot freeze the whole process.

### Installation & Operation (Ubuntu 24.04 LTS recommended)

A single unified installer handles everything — system build tools, TA-Lib from source, and every Python package the bot needs including TensorFlow:

```bash
sudo bash install.sh
```

Add your Alpaca keys to the bottom of `~/.bashrc` (paper trading recommended first):

```bash
export APCA_API_KEY_ID='your_key'
export APCA_API_SECRET_KEY='your_secret'
export APCA_API_BASE_URL='https://paper-api.alpaca.markets'
```

Then run the bot with a **single command** — the scanner is now built in, so no companion scripts are needed:

```bash
python3 billionaire-strategy-buy-lowest-price-stock-market-robot.py
```

On first launch the bot will pause briefly to run the initial scanner pass, populate `SYMBOLS_TO_BUY_LIST` in memory, and then enter the trading loop. From that point on the candidate universe refreshes itself once per day at 16:15 ET on a background thread — no separate terminals, no text files, no manual list maintenance.

### Important Notes
- Manual buys or sells performed on the broker website are automatically detected and reflected in the local database on the next restart or cycle.
- No overnight holding requirement. Same-day round-trips are fully supported under the 2026 margin rules.
- The ML brain trains overnight (17:00 – 07:45 ET) so it never competes with the live bot for market-data rate-limit headroom during trading hours.

### Disclaimer

This software is not affiliated with or endorsed by Alpaca Securities, LLC. It aims to be a valuable tool for stock market trading, but all trading involves risks. Use it responsibly and consider seeking advice from financial professionals.

Remember that all trading involves risks. The ability to successfully implement these strategies depends on both market conditions and individual skills and knowledge. As such, trading should only be done with funds that you can afford to lose. Always do thorough research before making investment decisions, and consider consulting with a financial advisor. This is use-at-your-own-risk software. This software does not include any warranty or guarantees other than the useful tasks that may or may not work as intended for the software application end user. The software developer shall not be held liable for any financial losses or damages that occur as a result of using this software for any reason to the fullest extent of the law. Using this software is your agreement to these terms. This software is designed to be helpful and useful to the end user.

Happy (and disciplined) trading.
