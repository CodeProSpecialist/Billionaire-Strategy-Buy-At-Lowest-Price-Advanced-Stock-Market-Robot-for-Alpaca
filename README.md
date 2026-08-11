**Billionaire-Strategy-Buy-At-Lowest-Price-Advanced-Stock-Market-Robot-for-Alpaca**

**2026 Edition – Advanced Stock Market Trading Robot, Version 9**  
(Billionaire Buying Strategy)

This is an upgraded, fully automated Alpaca trading robot that implements a disciplined “buy at the lowest reasonable price” strategy. The core idea remains the same: you cannot control the eventual sell price, so the robot focuses on high-quality technical entries while managing risk, exits, and portfolio exposure with modern margin-account rules.

### Major 2026 Upgrades (as of August 10, 2026)
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

### Recommended Use
Best suited for quieter market days (Monday / Tuesday). For strong bull-market environments many traders prefer a more aggressive “Bull Market” robot.

### How the Strategy Works

**Stock Universe**  
The robot reads symbols from `electricity-or-utility-stocks-to-buy-list.txt` (one symbol per line). Despite the historical file name, it can trade any liquid stocks you place there (commonly S&P 500 names selected by a separate scanner).

**Buy Logic (high-quality dip / reversal entries)**  
A candidate must pass **all** of the following before it is even ranked:

1. Price above its 200-day SMA (uptrend filter).
2. Daily RSI ≤ 50 (not overbought).
3. Multi-timeframe confirmation: 60-minute trend bullish **and** 5-minute bar showing a bounce.
4. Not inside a 2-day earnings blackout window.
5. At least one bullish candlestick reversal pattern on the most recent bar (Hammer, Bullish Engulfing, Morning Star, Piercing Line, Three White Soldiers, Dragonfly Doji, Inverted Hammer, Tweezer Bottom, etc.).
6. A weighted buy score that meets or exceeds the current regime’s dynamic threshold.

**Scoring signals** (weights change by market regime):
- Bullish candlestick pattern(s)
- RSI < 50 and/or falling
- Volume holding or expanding
- MACD above signal line
- Recent price dip
- Pattern-specific confirmation bonuses
- Price stability
- Relative strength vs SPY (bonus)

Candidates are ranked by score ÷ ATR% (reward-to-risk). Only the top `MAX_NEW_POSITIONS_PER_CYCLE` (default 3) are actually bought each cycle.

**Position Sizing**
- Risk-based (default 1 % of equity risked per trade using 2 × ATR).
- Hard cap of `$MAX_ALLOCATION_PER_SYMBOL` (default $600).
- Respects overall portfolio exposure limit and available buying power.
- Fractional shares supported.
- Optional “$1 test mode”.

**Exit Logic**
- **Peak-following Profit Monitor** (default on): Arms after a small gain (ATR-scaled), then trails a high-water mark. Sells on a controlled give-back from the peak while staying above a hard floor.
- **Scaled / tranche exits** (default on): Automatically sells fixed percentages of the *original* position size at +1 % and +2 %, then moves the remainder’s floor to breakeven.
- Optional broker-side 1 % trailing stop (default **off** – the monitor is finer-grained).
- Pre-market profit sweep (≈9:25 AM ET) and pre-close profit sweep (≈3:45 PM ET) that sell any individually profitable positions via a 3-step escalation (MOO/market → aggressive limit → market).
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
- CSV trade log + detailed text log.
- Robust error handling, retries, and timeouts so a single hung API call cannot freeze the whole process.

### Installation & Operation (Ubuntu 24.04 LTS recommended)

```bash
bash install.sh
bash install_dependencies.sh
```

Add your Alpaca keys to the bottom of `~/.bashrc` (paper trading recommended first):

```bash
export APCA_API_KEY_ID='your_key'
export APCA_API_SECRET_KEY='your_secret'
export APCA_API_BASE_URL='https://paper-api.alpaca.markets'
```

Then open three terminals:

1. `python3 stock-list-writer-for-list-of-stock-symbols-to-scan.py`
2. `python3 performance-stock-list-writer.py`   ← keeps the buy list high-quality  
   (If the performance stock list writer is not returning very many stocks, you can use `python3 auto-copy-stock-list-writer.py` instead.)
3. `python3 billionaire-strategy-buy-lowest-price-stock-market-robot.py`

You only need at least one valid symbol in `electricity-or-utility-stocks-to-buy-list.txt`. The robot will wait patiently for proper technical setups.

### Important Notes
- Manual buys or sells performed on the broker website are automatically detected and reflected in the local database on the next restart or cycle.
- No overnight holding requirement. Same-day round-trips are fully supported under the 2026 margin rules.

### Disclaimer

This software is not affiliated with or endorsed by Alpaca Securities, LLC. It aims to be a valuable tool for stock market trading, but all trading involves risks. Use it responsibly and consider seeking advice from financial professionals.

Remember that all trading involves risks. The ability to successfully implement these strategies depends on both market conditions and individual skills and knowledge. As such, trading should only be done with funds that you can afford to lose. Always do thorough research before making investment decisions, and consider consulting with a financial advisor. This is use-at-your-own-risk software. This software does not include any warranty or guarantees other than the useful tasks that may or may not work as intended for the software application end user. The software developer shall not be held liable for any financial losses or damages that occur as a result of using this software for any reason to the fullest extent of the law. Using this software is your agreement to these terms. This software is designed to be helpful and useful to the end user.

Happy (and disciplined) trading.