import threading
import logging
import csv
import os
import time
import schedule
from datetime import datetime, timedelta, date
from datetime import time as time2
import alpaca_trade_api as tradeapi
import pytz
import numpy as np
from collections import deque
import talib
import yfinance as yf
import sqlalchemy
from sqlalchemy import create_engine, Column, Integer, String, Float, event
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.exc import SQLAlchemyError
from ratelimit import limits, sleep_and_retry
import pandas_market_calendars as mcal

# ANSI color codes for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"

APIKEYID = os.getenv('APCA_API_KEY_ID')
APISECRETKEY = os.getenv('APCA_API_SECRET_KEY')
APIBASEURL = os.getenv('APCA_API_BASE_URL')

api = tradeapi.REST(APIKEYID, APISECRETKEY, APIBASEURL)

global symbols_to_buy
global price_history, last_stored, interval_map

# ---------------- Configuration flags ----------------
PRINT_SYMBOLS_TO_BUY = False
PRINT_ROBOT_STORED_BUY_AND_SELL_LIST_DATABASE = True
PRINT_DATABASE = True
DEBUG = False
ALL_BUY_ORDERS_ARE_1_DOLLAR = False
FRACTIONAL_BUY_ORDERS = True

# ---------------- 2026 Margin Account Rules ----------------
# The legacy FINRA Pattern Day Trader rule (4 round-trips / 5 business days,
# $25k minimum equity) is no longer enforced by this robot.
# Instead we operate under margin-account risk controls:
ACCOUNT_MODE = 'margin'          # 'margin' or 'cash'
UNLIMITED_DAY_TRADES = True      # No PDT round-trip counting
MAX_PORTFOLIO_EXPOSURE_PCT = 0.98    # of equity (buying power aware)
MAX_LEVERAGE = 1.0               # 1.0 = no borrowing. Raise to 2.0 for Reg-T intraday.
RISK_PER_TRADE_PCT = 0.01        # 1% of equity risked per position
MAX_ALLOCATION_PER_SYMBOL = 600.0
MIN_ORDER_NOTIONAL = 1.00
CASH_BUFFER = 1.00
MAINTENANCE_MARGIN_FLOOR_PCT = 0.30  # abort new buys if equity/market_value dips below

# ---------------- Exit strategy ----------------
# Two exits can act on the same shares. A GTC trailing stop RESERVES shares at
# the broker, so a later take-profit sell can only touch the unreserved fraction
# unless the stop is cancelled first (see cancel_open_sell_orders).
#
# IMPORTANT: the trailing stop and the profit monitor are redundant and the stop
# is COARSER. A 1% trailing stop fires long before the monitor's 0.2% giveback
# ever triggers, so leaving both on means the broker-side stop wins every race
# and the peak-following logic never actually runs. USE_TRAILING_STOP therefore
# defaults to False when the monitor is enabled.
USE_TRAILING_STOP = False        # broker-side 1% trailing stop (coarse)
TRAIL_PERCENT = 1.0
TAKE_PROFIT_PCT = 1.005          # +0.5% flat target, only used if monitor is off

# ---------------- Profit Monitor (peak-following exit) ----------------
# There is NO holding-period restriction: a position can be sold the same second
# it is bought. PDT is retired, so same-day round trips are unrestricted.
#
# Rather than dumping at the first tick over +0.5%, the monitor ARMS at that
# level and then follows price up, tracking a high-water mark. It sells when
# price pulls back from the peak by GIVEBACK_PCT, so a run to +3% is captured
# instead of being cut at +0.5%.
USE_PROFIT_MONITOR = True
ARM_PROFIT_PCT = 0.005           # +0.5% -> monitor arms and begins following
PEAK_GIVEBACK_PCT = 0.002        # sell after a 0.2% pullback from the peak
HARD_FLOOR_PCT = 0.001           # never sell armed positions below +0.1% net
MONITOR_STALE_SECS = 900         # drop peak state unseen for 15m (position gone)

# ---------------- Threading ----------------
# Worker threads are joined with a timeout so a hung API call in one thread
# cannot freeze the main loop indefinitely.
THREAD_JOIN_TIMEOUT = 180

eastern = pytz.timezone('US/Eastern')

stock_data = {}
previous_prices = {}
price_changes = {}

price_history = {}
last_stored = {}
interval_map = {
    '1min': 60, '5min': 300, '10min': 600, '15min': 900,
    '30min': 1800, '45min': 2700, '60min': 3600
}

buy_sell_lock = threading.Lock()
yf_lock = threading.Lock()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(filename=os.path.join(_BASE_DIR, 'trading-bot-program-logging-messages.txt'),
                    level=logging.INFO)

# BUGFIX: relative path meant the trade log followed the launch directory, same
# as the .db issue. Anchor it to the script directory.
csv_filename = os.path.join(_BASE_DIR, 'log-file-of-buy-and-sell-signals.csv')
fieldnames = ['Date', 'Buy', 'Sell', 'Quantity', 'Symbol', 'Price Per Share']

if not os.path.exists(csv_filename):
    with open(csv_filename, mode='w', newline='') as csv_file:
        csv.DictWriter(csv_file, fieldnames=fieldnames).writeheader()

Base = sqlalchemy.orm.declarative_base()


class TradeHistory(Base):
    __tablename__ = 'trade_history'
    id = Column(Integer, primary_key=True)
    symbols = Column(String)
    action = Column(String)
    quantity = Column(Float)
    price = Column(Float)
    date = Column(String)


class Position(Base):
    __tablename__ = 'positions'
    symbols = Column(String, primary_key=True)
    quantity = Column(Float)
    avg_price = Column(Float)
    purchase_date = Column(String)


# ---------------- Database ----------------
# BUGFIX: the path was relative ('sqlite:///trading_bot.db'), so the DB was
# created in whatever directory the program was launched from. Starting the bot
# from a different cwd silently opened a DIFFERENT, EMPTY database -- which
# looks exactly like "the .db stopped working after a restart". Anchor it to the
# script's own directory so it is always the same file.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_bot.db')
print(f"Using database: {DB_PATH}")

engine = create_engine(
    f'sqlite:///{DB_PATH}',
    connect_args={
        'check_same_thread': False,
        # BUGFIX: with two writer threads, lock contention raised
        # "database is locked" and the write was lost. Wait instead of failing.
        'timeout': 30,
    },
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    """
    BUGFIX: default journal mode gives poor durability and concurrency for a
    two-thread writer. WAL allows a reader alongside a writer and survives an
    ungraceful kill (e.g. Ctrl-C mid-write) without corrupting the file.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")      # crash-safe, concurrent reads
    cur.execute("PRAGMA synchronous=FULL")      # fsync on commit; survives power loss
    cur.execute("PRAGMA busy_timeout=30000")    # wait 30s for locks, don't error
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


# BUGFIX: buy_stocks and sell_stocks run as concurrent threads and previously
# shared one module-level Session. SQLAlchemy Sessions are NOT thread-safe, and
# both threads call session.query() outside any lock, so this could corrupt the
# identity map or raise intermittent InvalidRequestError once positions existed.
# scoped_session hands each thread its own session behind the same API.
Session = scoped_session(sessionmaker(bind=engine))
session = Session
Base.metadata.create_all(engine)

data_cache = {}
# BUGFIX: data_cache is read AND written by both worker threads with no
# synchronization. Guard it.
_cache_lock = threading.Lock()
CACHE_EXPIRY = 120                 # default: intraday prices

# Tiered TTLs. Daily-bar data (200-day SMA, daily RSI, 22-period ATR) barely
# moves during a session, but was being refetched every cycle -- the single
# biggest consumer of the yfinance budget. 16 symbols x 5 requests = 80/cycle
# against a 55/min cap would throttle every cold pass. Caching daily series for
# 30 minutes keeps steady-state well under the cap.
CACHE_TTLS = {
    'current_price': 120,          # 2m  - needs to be fresh
    'atr': 1800,                   # 30m - 22-period daily ATR
    'uptrend': 1800,               # 30m - 200-day SMA
    'daily_rsi': 1800,             # 30m - 14-period daily RSI
    'history_90d': 900,            # 15m - daily candles for scoring
}

CALLS = 60
PERIOD = 60

# ---------------- yfinance rate limiting ----------------
# yfinance guidance: 60 req/min (1/sec) is very safe, 120/min (2/sec) usually
# safe, plus a 0.5-1s delay BETWEEN requests. Batch where practical.
#
# BUGFIX: @limits creates a SEPARATE counter per decorated function -- they do
# NOT share a budget. Five different functions call yfinance
# (_fetch_current_price, _fetch_atr, is_in_uptrend, get_daily_rsi,
# calculate_technical_indicators), each decorated @limits(calls=60, period=60),
# which permitted 5 x 60 = 300 yfinance calls/minute. buy_stocks also called
# yf.Ticker(...).history() directly with NO limit at all.
#
# Every yfinance request now passes through ONE shared gate, so the cap is a real
# 60/min across the whole process regardless of which thread or function calls it.
YF_CALLS_PER_MIN = 55        # under the 60/min "very safe" guidance
YF_MIN_INTERVAL = 0.6        # seconds between requests, per yfinance guidance


class _YFGate:
    """
    Process-wide gate for yfinance. Thread-safe; shared by both workers.

    Enforces BOTH:
      - a rolling 60s ceiling (YF_CALLS_PER_MIN), and
      - a minimum spacing between consecutive requests (YF_MIN_INTERVAL).

    BUGFIX: the rolling window ALONE let all 55 requests fire in the same
    millisecond and then stall 60s (measured: 55 requests in 0.000s). The average
    is technically legal but that burst is exactly what triggers throttling --
    yfinance guidance asks for a 0.5-1s delay BETWEEN requests. Now enforced.
    """

    def __init__(self, calls_per_min, min_interval=0.0):
        self.capacity = calls_per_min
        self.min_interval = min_interval
        self._times = deque()
        self._last_call = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        """Block until it is polite to issue the next yfinance request."""
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= 60.0:
                    self._times.popleft()

                since_last = now - self._last_call
                if self.min_interval and since_last < self.min_interval:
                    wait = self.min_interval - since_last          # spacing
                elif len(self._times) >= self.capacity:
                    wait = 60.0 - (now - self._times[0]) + 0.01    # window cap
                    logging.info(f"yfinance gate: at {self.capacity}/min cap, waiting {wait:.1f}s")
                else:
                    self._times.append(now)
                    self._last_call = now
                    return
            # Sleep OUTSIDE the lock so other threads can drain expired slots.
            time.sleep(min(wait, 5.0))

    def used(self):
        with self._lock:
            now = time.monotonic()
            while self._times and now - self._times[0] >= 60.0:
                self._times.popleft()
            return len(self._times)


yf_gate = _YFGate(YF_CALLS_PER_MIN, YF_MIN_INTERVAL)


def yf_history(symbol, **kwargs):
    """
    The ONLY way this program should make a single-symbol yfinance call. Routing
    every request through one function makes the shared cap enforceable.
    Prefer yf_download_batch() when fetching the same period for many symbols.
    """
    yf_gate.acquire()
    with yf_lock:
        return yf.Ticker(to_yf(symbol)).history(**kwargs)


def yf_download_batch(symbols, **kwargs):
    """
    Batched multi-symbol fetch. yfinance guidance: prefer ONE
    yf.download(["AAPL","MSFT",...]) over N per-ticker requests, since batching
    dramatically reduces throttling risk.

    Returns {symbol: DataFrame}. Symbols that came back empty are omitted.
    Consumes ONE slot from the gate regardless of symbol count.
    """
    syms = [to_yf(s) for s in symbols]
    if not syms:
        return {}

    yf_gate.acquire()
    with yf_lock:
        raw = yf.download(syms, group_by='ticker', progress=False,
                          auto_adjust=False, threads=False, **kwargs)

    out = {}
    for orig, ys in zip(symbols, syms):
        try:
            # Multi-symbol returns column MultiIndex; single symbol returns flat.
            df = raw[ys] if len(syms) > 1 else raw
            if df is not None and not df.empty and not df['Close'].isna().all():
                out[orig] = df.dropna(how='all')
        except (KeyError, TypeError):
            continue
    return out


def prewarm_daily_cache(symbols):
    """
    Fetch a year of daily bars for every symbol in ONE batched request and seed
    the cache with the derived 200-day SMA, daily RSI and ATR.

    PERF: previously each symbol cost 3 separate yfinance requests for these
    (is_in_uptrend 1y + get_daily_rsi 60d + _fetch_atr 60d). For 16 symbols that
    was 48 requests; batched it is 1. All three are daily series, so one 1y pull
    serves all of them.
    """
    if not symbols:
        return

    # Skip entirely if every symbol still has warm daily entries -- otherwise the
    # batched call itself would waste a gate slot every cycle.
    now = time.time()
    ttl = CACHE_TTLS.get('uptrend', 1800)
    with _cache_lock:
        stale = [s for s in symbols
                 if now - data_cache.get((s, 'uptrend'), {}).get('timestamp', 0) >= ttl]
    if not stale:
        return

    try:
        batch = yf_download_batch(stale, period='1y', interval='1d')
    except Exception as e:
        logging.warning(f"Batched daily prewarm failed ({e}); falling back to per-symbol fetches.")
        return

    seeded = 0
    for sym, df in batch.items():
        try:
            close = df['Close'].values
            entries = {}

            if len(close) >= 200:
                sma = talib.SMA(close, timeperiod=200)[-1]
                if np.isfinite(sma):
                    entries['uptrend'] = float(sma)

            if len(close) >= 15:
                r = talib.RSI(close, timeperiod=14)[-1]
                if np.isfinite(r):
                    entries['daily_rsi'] = round(float(r), 2)

            if len(df) >= 23:
                atr = talib.ATR(df['High'].values, df['Low'].values, close, timeperiod=22)[-1]
                if np.isfinite(atr) and atr > 0:
                    entries['atr'] = float(atr)

            if len(df) >= 40:
                entries['history_90d'] = df.tail(90)

            with _cache_lock:
                for k, v in entries.items():
                    data_cache[(sym, k)] = {'timestamp': now, 'data': v}
            if entries:
                seeded += 1
        except Exception as e:
            logging.warning(f"Prewarm: could not derive indicators for {sym}: {e}")

    print(f"Prewarmed daily cache for {seeded}/{len(stale)} symbols in 1 batched request "
          f"(~{len(stale) * 3} individual requests avoided).")


# ---------------- Symbol helpers (BUGFIX: consistent normalization) ----------------
def to_yf(sym):
    """yfinance uses dashes for share classes: BRK.B -> BRK-B"""
    return sym.strip().upper().replace('.', '-')


def to_alpaca(sym):
    """Alpaca uses dots: BRK-B -> BRK.B"""
    return sym.strip().upper().replace('-', '.')


# BUGFIX: get_cached_data used to be @sleep_and_retry/@limits decorated. That was
# wrong twice over:
#   1. A CACHE HIT -- which makes no network call at all -- still consumed a slot
#      from the shared 60/min budget.
#   2. It NESTED: get_current_price -> get_cached_data -> _fetch_current_price,
#      all three rate-limited, so ONE price lookup burned THREE slots. With ~16
#      symbols the budget was exhausted mid-cycle, and sleep_and_retry then SLEEPS
#      THE CALLING THREAD for up to a full 60s window -- while sell_stocks held a
#      per-symbol claim, locking buy_stocks out of that symbol the entire time.
#      (Verified: 70 calls against the real limiter block for exactly 60.0s.)
# The cache layer does no I/O, so it is no longer rate-limited. Only the real
# network fetchers below are.
def get_cached_data(symbols, data_type, fetch_func, *args, **kwargs):
    key = (symbols, data_type)
    ttl = CACHE_TTLS.get(data_type, CACHE_EXPIRY)
    now = time.time()

    with _cache_lock:
        entry = data_cache.get(key)
        if entry and now - entry['timestamp'] < ttl:
            return entry['data']

    # Fetch OUTSIDE the cache lock: fetch_func is rate-limited and can block for
    # a full window. Holding _cache_lock across it would stall every cache reader
    # in both threads.
    data = fetch_func(*args, **kwargs)

    with _cache_lock:
        data_cache[key] = {'timestamp': time.time(), 'data': data}
    return data


def stop_if_stock_market_is_closed():
    nyse = mcal.get_calendar('NYSE')
    while True:
        current_datetime = datetime.now(eastern)
        current_time_str = current_datetime.strftime("%A, %B %d, %Y, %I:%M:%S %p")
        sched = nyse.schedule(start_date=current_datetime.date(), end_date=current_datetime.date())

        if not sched.empty:
            market_open = sched.iloc[0]['market_open'].astimezone(eastern)
            market_close = sched.iloc[0]['market_close'].astimezone(eastern)
            if market_open <= current_datetime <= market_close:
                print("Market is open. Proceeding with trading operations.")
                logging.info(f"{current_time_str}: Market is open.")
                return
            msg = f"Market is closed. Open hours: {market_open.strftime('%I:%M %p')} - {market_close.strftime('%I:%M %p')}"
        else:
            msg = "Market is closed today (holiday or weekend)."

        print('''
        *********************************************************************************
        ************ Billionaire Buying Strategy Version ********************************
        *********************************************************************************
            2026 Edition of the Advanced Stock Market Trading Robot, Version 9
                        https://github.com/CodeProSpecialist
               Margin Account Rules Engine - No PDT Round-Trip Limits
        ''')
        print(f'Current date & time (Eastern Time): {current_time_str}')
        print(msg)
        print("Waiting until Stock Market Hours to begin the Stockbot Trading Program.\n")
        logging.info(f"{current_time_str}: {msg}")
        time.sleep(60)


def print_database_tables():
    if not PRINT_DATABASE:
        return
    print("\nTrade History In This Robot's Database:\n")
    print("Stock | Buy or Sell | Quantity | Avg. Price | Date \n")
    for record in session.query(TradeHistory).all():
        print(f"{record.symbols} | {record.action} | {record.quantity:.4f} | {record.price:.2f} | {record.date}")

    print("----------------------------------------------------------------\n")
    print("Positions in the Database To Sell On or After the Date Shown:\n")
    print("Stock | Quantity | Avg. Price | Date \n")
    for record in session.query(Position).all():
        cp = get_current_price(record.symbols)
        # BUGFIX: guard against None current price / zero avg price
        if cp is not None and record.avg_price:
            pct = ((cp - record.avg_price) / record.avg_price) * 100
            color = GREEN if pct >= 0 else RED
            print(f"{record.symbols} | {record.quantity:.4f} | {record.avg_price:.2f} | "
                  f"{record.purchase_date} | Price Change: {color}{pct:.2f}%{RESET}")
        else:
            print(f"{record.symbols} | {record.quantity:.4f} | {record.avg_price:.2f} | {record.purchase_date}")
    print("\n")


def get_symbols_to_buy():
    try:
        with open('electricity-or-utility-stocks-to-buy-list.txt', 'r') as file:
            symbols = [to_yf(line) for line in file if line.strip()]
        if not symbols:
            print("\n****  Error: stocks-to-buy-list.txt contains no stock symbols.  ****\n")
        return symbols
    except FileNotFoundError:
        print("\n****  Error: File not found: electricity-or-utility-stocks-to-buy-list.txt  ****\n")
        return []


def remove_symbols_from_trade_list(symbol):
    """BUGFIX: normalize both sides before comparing so BRK.B/BRK-B match."""
    target = to_yf(symbol)
    try:
        with open('electricity-or-utility-stocks-to-buy-list.txt', 'r') as file:
            lines = file.readlines()
        with open('electricity-or-utility-stocks-to-buy-list.txt', 'w') as file:
            for line in lines:
                if line.strip() and to_yf(line) != target:
                    file.write(line)
        print(f"Removed {target} from trade list.")
    except FileNotFoundError:
        pass


# BUGFIX: this wrapper was also @limits decorated on top of get_cached_data and
# _fetch_current_price. Only _fetch_current_price actually touches the network,
# so it is the only layer that should consume the budget.
def get_current_price(symbols, retries=3):
    for attempt in range(retries):
        try:
            price = get_cached_data(symbols, 'current_price', _fetch_current_price, symbols)
            if price is not None:
                return price
        except Exception as e:
            logging.error(f"Retry {attempt + 1}/{retries} failed for {symbols}: {e}")
            time.sleep(2 ** attempt)
    return None


def _last_close(symbol):
    """BUGFIX: took a pre-built Ticker and called .history() on it directly,
    bypassing the rate gate. It is a fallback path, so on a bad day it fired for
    EVERY symbol -- doubling real yfinance traffic invisibly. Now gated."""
    try:
        h = yf_history(symbol, period='1d')
        if h.empty:
            return None
        return float(h['Close'].iloc[-1])
    except Exception:
        return None


def _fetch_current_price(symbols):
    # BUGFIX: was @limits decorated on its own counter AND held yf_lock across
    # every call, including the fallback. Rate limiting now lives in yf_gate
    # (one shared budget), and yf_lock is taken inside yf_history per request
    # rather than held across two sequential network calls.
    yf_symbol = to_yf(symbols)
    now = datetime.now(eastern)
    t = now.time()
    current_price = None
    try:
        if time2(4, 0) <= t < time2(20, 0):
            prepost = not (time2(9, 30) <= t < time2(16, 0))
            data = yf_history(symbols, period='1d', interval='1m', prepost=prepost)
            if not data.empty:
                current_price = float(data['Close'].iloc[-1])
        if current_price is None:
            current_price = _last_close(symbols)
    except Exception as e:
        logging.error(f"Error fetching current price for {yf_symbol}: {e}")
        current_price = _last_close(symbols)

    if current_price is None:
        logging.error(f"Failed to retrieve current price for {yf_symbol}.")
        return None
    return round(current_price, 4)


def _fetch_atr(sym):
    """BUGFIX: was a nested closure with NO rate limit of its own. All yfinance
    traffic now goes through the shared yf_gate instead of a per-function
    @limits counter, so the 60/min cap is real across the whole process."""
    yf_symbol = to_yf(sym)
    data = yf_history(sym, period='60d')
    try:
        if len(data) < 23:
            return None
        atr = talib.ATR(data['High'].values, data['Low'].values, data['Close'].values, timeperiod=22)
        val = atr[-1]
        # BUGFIX: reject NaN/zero ATR which produced div-by-zero position sizes
        if val is None or not np.isfinite(val) or val <= 0:
            return None
        return float(val)
    except Exception as e:
        logging.error(f"Error calculating ATR for {yf_symbol}: {e}")
        return None


# BUGFIX: same nesting problem -- this wrapper only delegates to get_cached_data,
# whose _fetch_atr does the real network call. Don't double-count the budget.
def get_average_true_range(symbols):
    return get_cached_data(symbols, 'atr', _fetch_atr, symbols)


def get_atr_high_price(sym):
    atr = get_average_true_range(sym)
    cp = get_current_price(sym)
    return round(cp + 0.40 * atr, 4) if cp and atr else None


def get_atr_low_price(sym):
    atr = get_average_true_range(sym)
    cp = get_current_price(sym)
    return round(cp - 0.10 * atr, 4) if cp and atr else None


def is_in_uptrend(symbols_to_buy):
    # BUGFIX: refetched a full 1y of daily bars EVERY cycle to compute a
    # 200-day SMA that moves once a day. Now cached for 30m (CACHE_TTLS).
    yf_symbol = to_yf(symbols_to_buy)

    def _fetch_sma(sym):
        h = yf_history(sym, period='1y')
        if h.empty or len(h) < 200:
            return None
        return float(talib.SMA(h['Close'].values, timeperiod=200)[-1])

    sma_200 = get_cached_data(symbols_to_buy, 'uptrend', _fetch_sma, symbols_to_buy)
    if sma_200 is None:
        return False
    cp = get_current_price(symbols_to_buy)
    if cp is None or not np.isfinite(sma_200):
        return False
    return cp > sma_200


def get_daily_rsi(symbols_to_buy):
    # BUGFIX: refetched 60d of daily bars every cycle for a daily RSI. Cached 30m.
    def _fetch_rsi(sym):
        h = yf_history(sym, period='60d', interval='1d')
        if h.empty or len(h) < 15:
            return None
        r = talib.RSI(h['Close'].values, timeperiod=14)[-1]
        return round(float(r), 2) if np.isfinite(r) else None

    return get_cached_data(symbols_to_buy, 'daily_rsi', _fetch_rsi, symbols_to_buy)


def calculate_technical_indicators(symbols, lookback_days=90):
    yf_symbol = to_yf(symbols)
    hist = yf_history(symbols, period=f'{lookback_days}d')
    if hist.empty or len(hist) < 35:
        return hist
    hist['macd'], hist['signal'], _ = talib.MACD(hist['Close'].values, fastperiod=12, slowperiod=26, signalperiod=9)
    hist['rsi'] = talib.RSI(hist['Close'].values, timeperiod=14)
    hist['volume'] = hist['Volume']
    return hist


def print_technical_indicators(symbols, historical_data):
    if historical_data is None or historical_data.empty:
        return
    cols = [c for c in ['Close', 'macd', 'signal', 'rsi', 'volume'] if c in historical_data.columns]
    print(f"\nTechnical Indicators for {symbols}:\n")
    print(historical_data[cols].tail())
    print("")


def get_previous_price(symbols):
    if symbols in previous_prices:
        return previous_prices[symbols]
    cp = get_current_price(symbols)
    if cp is not None:
        previous_prices[symbols] = cp
    return cp


def update_previous_price(symbols, current_price):
    if current_price is not None:
        previous_prices[symbols] = current_price


# ---------------- 2026 Margin rules engine ----------------
def get_margin_state():
    """Replaces PDT checks with margin-account health metrics."""
    acct = api.get_account()
    equity = float(acct.equity)
    last_equity = float(acct.last_equity or equity)
    cash = float(acct.cash)
    buying_power = float(acct.buying_power)
    dt_bp = float(getattr(acct, 'daytrading_buying_power', 0) or 0)
    rt_bp = float(getattr(acct, 'regt_buying_power', 0) or 0)
    long_mv = float(getattr(acct, 'long_market_value', 0) or 0)
    maint = float(getattr(acct, 'maintenance_margin', 0) or 0)

    # Effective purchasing power under our own leverage cap, not FINRA's PDT rule.
    if ACCOUNT_MODE == 'margin':
        effective_bp = min(buying_power, equity * MAX_LEVERAGE)
    else:
        effective_bp = min(cash, equity * MAX_LEVERAGE)

    margin_ratio = (equity / long_mv) if long_mv > 0 else 1.0
    healthy = margin_ratio >= MAINTENANCE_MARGIN_FLOOR_PCT

    return {
        'equity': equity, 'last_equity': last_equity, 'cash': cash,
        'buying_power': buying_power, 'daytrading_buying_power': dt_bp,
        'regt_buying_power': rt_bp, 'long_market_value': long_mv,
        'maintenance_margin': maint, 'effective_bp': effective_bp,
        'margin_ratio': margin_ratio, 'healthy': healthy,
        'trading_blocked': bool(acct.trading_blocked),
        'account_blocked': bool(acct.account_blocked),
    }


def day_trades_allowed():
    """2026 rules: no PDT round-trip counter. Only broker-level blocks matter."""
    if UNLIMITED_DAY_TRADES:
        st = get_margin_state()
        return not (st['trading_blocked'] or st['account_blocked'])
    return True


def compute_buy_score(df, current_price, previous_price, last_price):
    """
    BUGFIX: score is computed once, in one place, from clean booleans.
    Previously `score` was accumulated in two disconnected blocks with
    contradictory thresholds (`< 3` then `>= 3` with a `< 4` message).
    """
    close = df['Close'].values
    open_ = df['Open'].values
    high = df['High'].values
    low = df['Low'].values

    reasons = []
    score = 0

    # --- Candlestick bullish reversal detection (most recent bar only) ---
    pattern_funcs = {
        'Hammer': talib.CDLHAMMER,
        'Bullish Engulfing': talib.CDLENGULFING,
        'Morning Star': talib.CDLMORNINGSTAR,
        'Piercing Line': talib.CDLPIERCING,
        'Three White Soldiers': talib.CDL3WHITESOLDIERS,
        'Dragonfly Doji': talib.CDLDRAGONFLYDOJI,
        'Inverted Hammer': talib.CDLINVERTEDHAMMER,
        'Tweezer Bottom': talib.CDLMATCHINGLOW,
    }
    detected = []
    for name, fn in pattern_funcs.items():
        try:
            res = fn(open_, high, low, close)
            # BUGFIX: require a BULLISH (>0) signal. The original accepted
            # `!= 0`, which let bearish (-100) prints count as buy signals.
            if len(res) and res[-1] > 0:
                detected.append(name)
        except Exception:
            continue

    if detected:
        score += 2
        reasons.append(f"patterns={','.join(detected)}")

    # --- RSI ---
    rsi_series = talib.RSI(close, timeperiod=14)
    latest_rsi = float(rsi_series[-1]) if len(rsi_series) and np.isfinite(rsi_series[-1]) else None
    rsi_decrease = False
    recent_avg_rsi = prior_avg_rsi = 0.0
    if len(rsi_series) >= 10:
        recent = rsi_series[-5:][np.isfinite(rsi_series[-5:])]
        prior = rsi_series[-10:-5][np.isfinite(rsi_series[-10:-5])]
        if len(recent) and len(prior):
            recent_avg_rsi, prior_avg_rsi = float(np.mean(recent)), float(np.mean(prior))
            rsi_decrease = recent_avg_rsi < prior_avg_rsi
    if latest_rsi is not None and latest_rsi < 50:
        score += 1
        reasons.append(f"rsi={latest_rsi:.1f}<50")
    if rsi_decrease:
        score += 1
        reasons.append("rsi_falling")

    # --- Volume ---
    recent_avg_volume = float(df['Volume'].iloc[-5:].mean()) if len(df) >= 5 else 0.0
    prior_avg_volume = float(df['Volume'].iloc[-10:-5].mean()) if len(df) >= 10 else recent_avg_volume
    volume_decrease = recent_avg_volume < prior_avg_volume if len(df) >= 10 else False
    if not volume_decrease:
        score += 1
        reasons.append("volume_holding")

    # --- MACD ---
    macd, macd_signal, _ = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
    macd_above_signal = False
    if len(macd) and np.isfinite(macd[-1]) and np.isfinite(macd_signal[-1]):
        macd_above_signal = macd[-1] > macd_signal[-1]
    if macd_above_signal:
        score += 1
        reasons.append("macd>signal")

    # --- Price decline (BUGFIX: numeric magnitude, not a bool compared to a price) ---
    decline_pct = (last_price - current_price) / last_price if last_price else 0.0
    price_decline = decline_pct >= 0.002
    if price_decline:
        score += 1
        reasons.append(f"dip={decline_pct*100:.2f}%")

    # --- Pattern-specific confirmations ---
    for p in detected:
        if p == 'Hammer' and latest_rsi is not None and latest_rsi < 35 and decline_pct >= 0.003:
            score += 1
        elif p == 'Bullish Engulfing' and prior_avg_volume and recent_avg_volume > 1.5 * prior_avg_volume:
            score += 1
        elif p == 'Morning Star' and latest_rsi is not None and latest_rsi < 40:
            score += 1
        elif p == 'Piercing Line' and recent_avg_rsi and recent_avg_rsi < 40:
            score += 1
        elif p == 'Three White Soldiers' and not volume_decrease:
            score += 1
        elif p == 'Dragonfly Doji' and latest_rsi is not None and latest_rsi < 30:
            score += 1
        elif p == 'Inverted Hammer' and rsi_decrease:
            score += 1
        elif p == 'Tweezer Bottom' and latest_rsi is not None and latest_rsi < 40:
            score += 1

    return {
        'score': score, 'detected': detected, 'reasons': reasons,
        'latest_rsi': latest_rsi, 'rsi_decrease': rsi_decrease,
        'volume_decrease': volume_decrease, 'macd_above_signal': macd_above_signal,
        'price_decline': price_decline, 'decline_pct': decline_pct,
    }


BUY_SCORE_THRESHOLD = 4


def buy_stocks(symbols_to_buy_list, lock):
    print("Starting buy_stocks function...")
    if not symbols_to_buy_list:
        logging.info("No symbols to buy.")
        return

    # BUGFIX: carry qty per-symbol. Previously the DB write used a single
    # leaked `filled_qty` from the last loop iteration for EVERY position.
    filled_records = []  # (alpaca_symbol, yf_symbol, qty, price, date_str)

    st = get_margin_state()
    if st['trading_blocked'] or st['account_blocked']:
        print("Account is blocked by the broker. No buys.")
        return
    if not st['healthy']:
        print(f"Margin health low (equity/long_mv = {st['margin_ratio']:.2f} < "
              f"{MAINTENANCE_MARGIN_FLOOR_PCT:.2f}). No new buys.")
        logging.warning("Margin maintenance floor breached; buys suspended.")
        return

    total_equity = st['equity']
    current_exposure = st['long_market_value']
    max_new_exposure = min(
        total_equity * MAX_PORTFOLIO_EXPOSURE_PCT - current_exposure,
        st['effective_bp'] - CASH_BUFFER,
    )
    if max_new_exposure <= MIN_ORDER_NOTIONAL:
        print("Exposure / buying-power limit reached. No new buys.")
        return
    print(f"Equity ${total_equity:,.2f} | Exposure ${current_exposure:,.2f} | "
          f"Effective BP ${st['effective_bp']:,.2f} | Headroom ${max_new_exposure:,.2f}")

    today_date_str = datetime.now(eastern).date().strftime("%Y-%m-%d")

    for symbol in list(symbols_to_buy_list):
        yf_symbol = to_yf(symbol)
        api_symbol = to_alpaca(symbol)
        now_str = datetime.now(eastern).strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")

        # BUGFIX: per-symbol claim. Without it, buy_stocks could fill and add a
        # position for a symbol that sell_stocks was concurrently deciding to
        # exit, and both threads would race on the same broker position.
        if not position_book.claim(api_symbol):
            print(f"{yf_symbol}: busy in another thread this cycle. Skipping.")
            continue
        try:
            current_price = get_current_price(symbol)
            if current_price is None or current_price <= 0:
                continue

            # Track rolling price history
            ts = time.time()
            if symbol not in price_history:
                price_history[symbol] = {i: [] for i in interval_map}
                last_stored[symbol] = {i: 0 for i in interval_map}
            for interval, delta in interval_map.items():
                if ts - last_stored[symbol][interval] >= delta:
                    price_history[symbol][interval].append(current_price)
                    price_history[symbol][interval] = price_history[symbol][interval][-50:]
                    last_stored[symbol][interval] = ts

            # NOTE: the 90d candle fetch used to happen here, before the SMA/RSI
            # gates below. It now runs only for symbols that survive them.

            # --- Trend + multi-timeframe filters ---
            # PERF: these run BEFORE the 90d candle fetch. Both are cached for
            # 30m, so on a warm cache they cost zero yfinance requests and reject
            # most symbols for free. Fetching the 90d history first (as before)
            # meant paying a request for every symbol that was about to be cut.
            if not is_in_uptrend(symbol):
                print(f"{yf_symbol}: below 200-day SMA. Skipping.")
                update_previous_price(symbol, current_price)
                continue

            daily_rsi = get_daily_rsi(symbol)
            if daily_rsi is None or daily_rsi > 50:
                print(f"{yf_symbol}: daily RSI not oversold ({daily_rsi}). Skipping.")
                update_previous_price(symbol, current_price)
                continue

            # Only survivors pay for the 90d candle history.
            df = get_cached_data(symbol, 'history_90d',
                                 lambda s: yf_history(s, period="90d"), symbol)
            # BUGFIX: MACD(26,9) and RSI(14) need ~35 bars. Original required
            # only 3 rows, producing all-NaN indicators that silently scored 0.
            if df.empty or len(df) < 40:
                print(f"{yf_symbol}: insufficient history ({len(df)} bars). Skipping.")
                continue

            previous_price = get_previous_price(symbol) or current_price
            last_price = float(df['Close'].iloc[-1])

            sig = compute_buy_score(df, current_price, previous_price, last_price)

            # Price-stability bonus (BUGFIX: defined unconditionally, so the
            # `else` logging branch can no longer raise NameError)
            price_stable = True
            hist5 = price_history.get(symbol, {}).get('5min', [])
            if len(hist5) >= 2 and hist5[-2]:
                price_stable = abs(hist5[-1] - hist5[-2]) / hist5[-2] < 0.005
                if price_stable:
                    sig['score'] += 1

            if not sig['detected']:
                print(f"{yf_symbol}: no bullish reversal pattern. Score {sig['score']}. Skipping.")
                update_previous_price(symbol, current_price)
                continue

            if sig['score'] < BUY_SCORE_THRESHOLD:
                print(f"{yf_symbol}: score {sig['score']} < {BUY_SCORE_THRESHOLD}. "
                      f"[{'; '.join(sig['reasons'])}] Skipping.")
                logging.info(f"{now_str} Skipped {yf_symbol}: score {sig['score']}")
                update_previous_price(symbol, current_price)
                continue

            # ---------------- Position sizing ----------------
            if ALL_BUY_ORDERS_ARE_1_DOLLAR:
                notional = MIN_ORDER_NOTIONAL
            else:
                atr = get_average_true_range(symbol)
                if atr is None:
                    print(f"{yf_symbol}: no valid ATR. Skipping.")
                    continue
                risk_per_share = 2 * atr
                risk_amount = RISK_PER_TRADE_PCT * total_equity
                notional = (risk_amount / risk_per_share) * current_price

                with lock:
                    cash_available = float(api.get_account().cash)
                headroom = min(
                    MAX_ALLOCATION_PER_SYMBOL,
                    max_new_exposure,
                    (cash_available - CASH_BUFFER) if ACCOUNT_MODE == 'cash' else max_new_exposure,
                )

                # BUGFIX: on small accounts, risk-based sizing always lands below the
                # broker's $1 notional floor, so every trade was silently discarded.
                # Round up to the floor when headroom allows; only skip if it doesn't.
                # NOTE: rounding up intentionally exceeds RISK_PER_TRADE_PCT. See
                # MIN_EQUITY_TO_TRADE if you'd rather halt than over-risk.
                if notional < MIN_ORDER_NOTIONAL:
                    if headroom >= MIN_ORDER_NOTIONAL:
                        actual_risk_pct = (MIN_ORDER_NOTIONAL / current_price * risk_per_share) / total_equity * 100
                        print(f"{yf_symbol}: risk-sized ${notional:.2f} < ${MIN_ORDER_NOTIONAL:.2f} floor; "
                              f"rounding up (risk becomes {actual_risk_pct:.2f}% of equity)")
                        notional = MIN_ORDER_NOTIONAL
                    else:
                        print(f"{yf_symbol}: headroom ${headroom:.2f} < ${MIN_ORDER_NOTIONAL:.2f} minimum. Skipping.")
                        continue
                else:
                    # Slippage haircut
                    notional = min(notional, headroom) * 0.999

            notional = round(notional, 2)
            if notional < MIN_ORDER_NOTIONAL:
                print(f"{yf_symbol}: notional ${notional:.2f} below ${MIN_ORDER_NOTIONAL:.2f} minimum. Skipping.")
                continue

            with lock:
                bp = float(api.get_account().buying_power)
            if bp < notional + CASH_BUFFER:
                print(f"{yf_symbol}: insufficient buying power (${bp:.2f} < ${notional + CASH_BUFFER:.2f}).")
                continue

            if not day_trades_allowed():
                print("Broker has blocked trading on this account.")
                break

            qty_est = round(notional / current_price, 4)
            reason = f"score={sig['score']} [{'; '.join(sig['reasons'])}]"
            print(f"Submitting buy: {api_symbol} ~{qty_est:.4f} sh @ ${current_price:.2f} "
                  f"(notional ${notional:.2f}) | {reason}")

            try:
                buy_order = api.submit_order(
                    symbol=api_symbol,
                    notional=notional,
                    side='buy',
                    type='market',
                    time_in_force='day',
                )
                logging.info(f"{now_str} Submitted buy {api_symbol} notional ${notional:.2f}: {reason}")

                filled_qty = 0.0
                filled_price = current_price
                terminal = False
                for _ in range(30):
                    try:
                        o = api.get_order(buy_order.id)
                    except Exception as e:
                        # BUGFIX: a transient network error during polling used to
                        # escape the APIError handler and kill the whole buy loop,
                        # skipping the DB persist for every prior fill in this pass.
                        logging.warning(f"{api_symbol}: poll error ({e}); retrying.")
                        time.sleep(2)
                        continue

                    # BUGFIX: track partial fills. The old code only broke on exactly
                    # 'filled', so a partially_filled order polled out and was logged
                    # as "not filled" -- while the shares were actually owned, with no
                    # DB row, no stop, and no trade history. Silent orphan position.
                    filled_qty = float(o.filled_qty or 0)
                    if o.filled_avg_price:
                        filled_price = float(o.filled_avg_price)

                    if o.status == 'filled':
                        terminal = True
                        break
                    if o.status in ('canceled', 'expired', 'rejected'):
                        print(f"{api_symbol}: order {o.status} (filled {filled_qty:.4f} before stopping).")
                        logging.warning(f"{api_symbol}: order {o.status}, partial qty {filled_qty:.4f}.")
                        terminal = True
                        break
                    time.sleep(2)

                # BUGFIX: cancel a still-open order that never reached a terminal
                # state, so it can't fill later behind our back and leave the broker
                # holding shares this bot has no record of.
                if not terminal:
                    try:
                        api.cancel_order(buy_order.id)
                        logging.warning(f"{api_symbol}: buy order timed out after 60s; cancel requested.")
                        print(f"{api_symbol}: order timed out, cancel requested.")
                        time.sleep(2)
                        o = api.get_order(buy_order.id)
                        filled_qty = float(o.filled_qty or 0)
                        if o.filled_avg_price:
                            filled_price = float(o.filled_avg_price)
                    except Exception as e:
                        logging.error(f"{api_symbol}: cancel/re-check failed: {e}")

                # Any qty actually acquired is recorded, whether the order completed
                # fully, partially, or was cancelled mid-flight.
                if filled_qty > 0:
                    print(f"Filled {filled_qty:.4f} sh of {api_symbol} @ "
                          f"{GREEN}${filled_price:.2f}{RESET} (cost ${filled_qty * filled_price:.2f})")
                    with open(csv_filename, mode='a', newline='') as f:
                        csv.DictWriter(f, fieldnames=fieldnames).writerow({
                            'Date': now_str, 'Buy': 'Buy', 'Sell': '',
                            'Quantity': filled_qty, 'Symbol': api_symbol,
                            'Price Per Share': filled_price,
                        })
                    filled_records.append((api_symbol, yf_symbol, filled_qty, filled_price, today_date_str))

                    if USE_TRAILING_STOP and not ALL_BUY_ORDERS_ARE_1_DOLLAR:
                        sid = place_trailing_stop_sell_order(api_symbol, filled_qty, filled_price)
                        print(f"Trailing stop for {api_symbol}: {sid or 'not placed (see log)'}")
                else:
                    print(f"Buy order not filled for {api_symbol}")
                    logging.info(f"{now_str} Buy order not filled for {api_symbol}")

            except tradeapi.rest.APIError as e:
                print(f"Error submitting buy order for {api_symbol}: {e}")
                logging.error(f"Error submitting buy order for {api_symbol}: {e}")
                continue
            except Exception as e:
                # BUGFIX: catch-all so one unexpected failure can't abort the loop
                # and discard already-filled records awaiting persist.
                print(f"Unexpected error handling buy for {api_symbol}: {e}")
                logging.error(f"Unexpected error handling buy for {api_symbol}: {e}")
                continue

            update_previous_price(symbol, current_price)
            time.sleep(0.8)
        finally:
            # BUGFIX: always release, including on every `continue` path and on
            # exception, or the symbol stays locked out of trading forever.
            position_book.release(api_symbol)

    # ---------------- Persist fills ----------------
    if not filled_records:
        return
    try:
        with lock:
            for api_symbol, yf_symbol, qty, price, dstr in filled_records:
                # BUGFIX: mutate the shared PositionBook in place instead of a
                # by-reference dict that refresh_* would later rebind away.
                position_book.upsert(api_symbol, round(price, 4), dstr)
                if yf_symbol in symbols_to_buy_list:
                    symbols_to_buy_list.remove(yf_symbol)   # BUGFIX: guarded remove
                remove_symbols_from_trade_list(yf_symbol)

                session.add(TradeHistory(symbols=api_symbol, action='buy',
                                         quantity=qty, price=price, date=dstr))
                # BUGFIX: merge instead of add — a re-buy of an existing symbol
                # previously raised an IntegrityError on the primary key.
                existing = session.query(Position).filter_by(symbols=api_symbol).one_or_none()
                if existing:
                    total_qty = existing.quantity + qty
                    existing.avg_price = ((existing.avg_price * existing.quantity) + (price * qty)) / total_qty
                    existing.quantity = total_qty
                    existing.purchase_date = dstr
                else:
                    session.add(Position(symbols=api_symbol, quantity=qty,
                                         avg_price=price, purchase_date=dstr))
            session.commit()
        print("Database updated successfully.")
    except SQLAlchemyError as e:
        session.rollback()
        print(f"Database error: {e}")
        logging.error(f"Database error: {e}")
        return

    # BUGFIX: refresh_after_buy() used to run INSIDE `with lock`. It sleeps 2s
    # and then makes blocking API calls (list_positions plus a paginated order
    # lookup per symbol), holding the mutex for tens of seconds and serializing
    # both worker threads. Now called after the lock is released.
    refresh_after_buy()


def refresh_after_buy():
    # BUGFIX: no longer rebinds globals. symbols_to_buy is refreshed by main()
    # each cycle, and the position view is mutated in place so the other thread
    # keeps seeing the same object.
    time.sleep(2)
    position_book.replace_all(update_symbols_to_sell_from_api())


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def place_trailing_stop_sell_order(symbol, qty, current_price, retries=3):
    """
    Places a trailing stop on the whole-share portion. Alpaca does not accept
    fractional qty for trailing_stop orders, so any fractional remainder is left
    to the sell_stocks take-profit logic.

    BUGFIX: previously a failed stop just printed and moved on, leaving an
    unprotected position. Now retries with backoff and escalates on give-up.
    """
    whole = int(qty)
    if whole < 1:
        logging.info(f"{symbol}: qty {qty:.4f} < 1 whole share; trailing stop not supported "
                     f"by broker for fractional qty. Managed by sell_stocks instead.")
        return None

    for attempt in range(retries):
        try:
            stop_order = api.submit_order(
                symbol=symbol,
                qty=whole,
                side='sell',
                type='trailing_stop',
                trail_percent=str(TRAIL_PERCENT),
                time_in_force='gtc',
            )
            logging.info(f"Placed trailing stop ({TRAIL_PERCENT}%) for {whole} sh of {symbol}: {stop_order.id}")
            return stop_order.id
        except Exception as e:
            logging.error(f"Trailing stop attempt {attempt + 1}/{retries} failed for {symbol}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    # Give-up path: the position is live and unprotected. Make that loud.
    msg = (f"CRITICAL: could not place trailing stop for {whole} sh of {symbol} after "
           f"{retries} attempts. POSITION IS UNPROTECTED - exit relies on take-profit only.")
    print(f"{RED}{msg}{RESET}")
    logging.critical(msg)
    return None


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def cancel_open_sell_orders(symbol):
    """
    Cancel resting sell orders (e.g. the GTC trailing stop) so a take-profit can
    sell the full position.

    BUGFIX: no cancel logic existed at all. A GTC trailing stop reserves shares
    at the broker, so sell_stocks could only ever offload the unreserved
    fraction -- the whole-share portion could never exit on the profit target.

    Returns True if it is safe to proceed with a full-size sell.
    """
    try:
        open_sells = [o for o in api.list_orders(status='open') if o.symbol == symbol and o.side == 'sell']
    except Exception as e:
        logging.error(f"{symbol}: could not list open orders: {e}")
        return False

    if not open_sells:
        return True

    for o in open_sells:
        try:
            api.cancel_order(o.id)
            logging.info(f"{symbol}: cancelled resting sell order {o.id} ({o.type}) to free shares.")
        except Exception as e:
            logging.error(f"{symbol}: failed to cancel sell order {o.id}: {e}")
            return False

    # Cancellation is asynchronous; wait for the broker to release the shares.
    for _ in range(10):
        time.sleep(1)
        try:
            still_open = [o for o in api.list_orders(status='open')
                          if o.symbol == symbol and o.side == 'sell']
            if not still_open:
                return True
        except Exception as e:
            logging.error(f"{symbol}: error confirming cancellation: {e}")
            return False

    logging.warning(f"{symbol}: sell orders still open after cancel; skipping this cycle.")
    return False


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def get_most_recent_purchase_date(symbol):
    try:
        order_list = []
        CHUNK_SIZE = 500
        until = datetime.now(pytz.UTC).isoformat()
        # BUGFIX: unbounded while-loop could paginate forever on a busy account.
        for _ in range(10):
            chunk = api.list_orders(status='all', nested=False, direction='desc',
                                    until=until, limit=CHUNK_SIZE, symbols=[symbol])
            if not chunk:
                break
            order_list.extend(chunk)
            until = (chunk[-1].submitted_at - timedelta(seconds=1)).isoformat()
            if len(chunk) < CHUNK_SIZE:
                break

        buys = [o for o in order_list if o.side == 'buy' and o.status == 'filled' and o.filled_at]
        if buys:
            d = max(buys, key=lambda o: o.filled_at).filled_at.date()
            return d.strftime("%Y-%m-%d")
    except Exception as e:
        logging.error(f"Error fetching buy orders for {symbol}: {e}")
    return datetime.now(eastern).date().strftime("%Y-%m-%d")


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def update_symbols_to_sell_from_api():
    positions = api.list_positions()
    d = {}
    live = set()
    for p in positions:
        sym = p.symbol
        live.add(sym)
        avg = float(p.avg_entry_price)
        qty = float(p.qty)
        pdate = get_most_recent_purchase_date(sym)
        row = session.query(Position).filter_by(symbols=sym).one_or_none()
        if row:
            row.quantity, row.avg_price, row.purchase_date = qty, avg, pdate
        else:
            session.add(Position(symbols=sym, quantity=qty, avg_price=avg, purchase_date=pdate))
        d[sym] = (avg, pdate)

    # BUGFIX: prune DB rows for positions that no longer exist at the broker,
    # otherwise sell_stocks kept trying to sell phantom holdings forever.
    for row in session.query(Position).all():
        if row.symbols not in live:
            session.delete(row)

    session.commit()
    return d


class PositionBook:
    """
    Thread-safe owner of the shared position view.

    BUGFIX (rebind race): main() passed `symbols_to_sell_dict` BY REFERENCE to
    both worker threads, and refresh_after_buy()/refresh_after_sell() then did
    `global symbols_to_sell_dict; symbols_to_sell_dict = {...}` -- REBINDING the
    global to a brand-new dict. The threads kept references to the OLD object,
    so every update after the first refresh was silently discarded and the two
    threads mutated different dicts. This class is never rebound; it mutates one
    dict in place under its own lock, so all readers see the same state.

    BUGFIX (per-symbol races): buy_stocks and sell_stocks could both act on the
    same symbol concurrently (a fill landing while sell was deciding to exit).
    claim()/release() give a per-symbol mutex so only one side touches a symbol
    at a time.
    """

    def __init__(self):
        self._data = {}                       # symbol -> (avg_price, purchase_date)
        self._lock = threading.RLock()
        self._claims = set()                  # symbols currently owned by a thread
        self._claims_cv = threading.Condition(self._lock)

    # ---- snapshot / read ----
    def snapshot(self):
        """Stable copy for iteration. Never iterate the live dict."""
        with self._lock:
            return dict(self._data)

    def get(self, symbol):
        with self._lock:
            return self._data.get(symbol)

    def symbols(self):
        with self._lock:
            return set(self._data)

    def __len__(self):
        with self._lock:
            return len(self._data)

    # ---- mutate in place (never rebind) ----
    def upsert(self, symbol, avg_price, purchase_date):
        with self._lock:
            self._data[symbol] = (avg_price, purchase_date)

    def remove(self, symbol):
        with self._lock:
            self._data.pop(symbol, None)

    def replace_all(self, mapping):
        """Refresh contents WITHOUT rebinding the object other threads hold."""
        with self._lock:
            self._data.clear()
            self._data.update(mapping)

    # ---- per-symbol claim ----
    def claim(self, symbol, timeout=0):
        """Try to take exclusive ownership of a symbol. False if already claimed."""
        with self._claims_cv:
            if symbol in self._claims:
                return False
            self._claims.add(symbol)
            return True

    def release(self, symbol):
        with self._claims_cv:
            self._claims.discard(symbol)
            self._claims_cv.notify_all()


# Single shared instance. Referenced directly by both threads; never reassigned.
position_book = PositionBook()


class ProfitMonitorEngine:
    """
    Peak-following exit. Instead of selling at the first tick above +0.5%, this
    arms at that level and then follows price to its high-water mark, selling
    only once price gives back PEAK_GIVEBACK_PCT from the peak.

    States per symbol:
      watching -> below the arm threshold, do nothing
      armed    -> above arm threshold, tracking peak_price
      exit     -> pulled back from peak, sell now

    There is no holding-period gate: a position can arm and exit the same
    second it was bought.
    """

    def __init__(self):
        self._state = {}          # symbol -> dict(peak_price, armed_at, last_seen)
        self._lock = threading.Lock()

    def evaluate(self, symbol, entry_price, current_price):
        """Returns (should_sell: bool, info: dict) for logging/telemetry."""
        now = time.time()
        if not entry_price or entry_price <= 0 or not current_price or current_price <= 0:
            return False, {'state': 'invalid'}

        gain = (current_price - entry_price) / entry_price

        with self._lock:
            st = self._state.get(symbol)

            # Not yet armed: wait for +ARM_PROFIT_PCT.
            if st is None:
                if gain < ARM_PROFIT_PCT:
                    return False, {'state': 'watching', 'gain_pct': gain * 100,
                                   'arm_at_pct': ARM_PROFIT_PCT * 100}
                self._state[symbol] = {'peak_price': current_price,
                                       'armed_at': now, 'last_seen': now}
                return False, {'state': 'armed', 'gain_pct': gain * 100,
                               'peak_price': current_price, 'peak_gain_pct': gain * 100}

            # Already armed: ratchet the peak upward, never down.
            st['last_seen'] = now
            if current_price > st['peak_price']:
                st['peak_price'] = current_price
                return False, {'state': 'new_peak', 'gain_pct': gain * 100,
                               'peak_price': st['peak_price'], 'peak_gain_pct': gain * 100}

            peak = st['peak_price']
            peak_gain = (peak - entry_price) / entry_price
            giveback = (peak - current_price) / peak

            info = {'state': 'following', 'gain_pct': gain * 100,
                    'peak_price': peak, 'peak_gain_pct': peak_gain * 100,
                    'giveback_pct': giveback * 100}

            # Pulled back enough from peak -> exit, but never give back the
            # whole move: require the position still be profitably above floor.
            if giveback >= PEAK_GIVEBACK_PCT and gain >= HARD_FLOOR_PCT:
                info['state'] = 'exit'
                return True, info

            # Collapsed below the hard floor after arming: cut it here rather
            # than round-trip a winner into a loser.
            if gain < HARD_FLOOR_PCT:
                info['state'] = 'exit_floor'
                return True, info

            return False, info

    def clear(self, symbol):
        with self._lock:
            self._state.pop(symbol, None)

    def prune(self, live_symbols):
        """Drop state for positions that no longer exist or went stale."""
        now = time.time()
        with self._lock:
            for sym in list(self._state):
                if sym not in live_symbols or (now - self._state[sym]['last_seen']) > MONITOR_STALE_SECS:
                    self._state.pop(sym, None)

    def snapshot(self):
        with self._lock:
            return {s: dict(v) for s, v in self._state.items()}


profit_monitor = ProfitMonitorEngine()


def sell_stocks(lock):
    print("Starting sell_stocks function...")
    to_remove = []
    now_str = datetime.now(eastern).strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")
    today_date_str = datetime.now(eastern).date().strftime("%Y-%m-%d")

    # NO holding-period gate. PDT is retired under the 2026 margin rules, so a
    # position may be sold the same second it was bought. purchase_date is now
    # recorded for reporting only.
    profit_monitor.prune(position_book.symbols())

    # BUGFIX: iterate a SNAPSHOT. Previously this walked the live shared dict
    # while buy_stocks mutated it -> "dictionary changed size during iteration".
    for symbol, (bought_price, purchase_date) in position_book.snapshot().items():
        # BUGFIX: per-symbol claim stops buy_stocks and sell_stocks from acting
        # on the same symbol at once (a fill landing mid-exit-decision).
        if not position_book.claim(symbol):
            print(f"{symbol}: busy in another thread this cycle. Skipping.")
            continue
        try:
            current_price = get_current_price(symbol)
            if current_price is None:
                continue

            position = api.get_position(symbol)
            bought_price = float(position.avg_entry_price)
            qty = float(position.qty)

            # ---------------- Exit decision ----------------
            if USE_PROFIT_MONITOR:
                should_sell, info = profit_monitor.evaluate(symbol, bought_price, current_price)
                state = info.get('state')
                if state in ('watching',):
                    print(f"{symbol}: {info['gain_pct']:+.2f}% (arms at "
                          f"+{info['arm_at_pct']:.2f}%). Holding.")
                    continue
                if state in ('armed', 'new_peak'):
                    print(f"{symbol}: {GREEN}{info['gain_pct']:+.2f}%{RESET} "
                          f"peak ${info['peak_price']:.2f} — following.")
                    continue
                if state == 'following' and not should_sell:
                    print(f"{symbol}: {GREEN}{info['gain_pct']:+.2f}%{RESET} "
                          f"peak +{info['peak_gain_pct']:.2f}% "
                          f"(giveback {info['giveback_pct']:.2f}% of "
                          f"{PEAK_GIVEBACK_PCT*100:.2f}%). Following.")
                    continue
                if not should_sell:
                    continue
                if state == 'exit_floor':
                    reason = (f"dropped to {info['gain_pct']:+.2f}% after peaking "
                              f"+{info['peak_gain_pct']:.2f}% — cutting at floor")
                else:
                    reason = (f"peaked +{info['peak_gain_pct']:.2f}%, gave back "
                              f"{info['giveback_pct']:.2f}% — taking {info['gain_pct']:+.2f}%")
            else:
                sell_threshold = bought_price * TAKE_PROFIT_PCT
                if current_price < sell_threshold:
                    print(f"{symbol}: {RED}${current_price:.2f}{RESET} < target ${sell_threshold:.2f}. Holding.")
                    continue
                reason = f"hit +{(TAKE_PROFIT_PCT-1)*100:.2f}% target"

            # BUGFIX: cancel the resting trailing stop BEFORE selling. It reserves
            # shares at the broker, so without this the take-profit could only ever
            # sell the unreserved fraction and the whole-share portion was stuck.
            if not cancel_open_sell_orders(symbol):
                print(f"{symbol}: could not clear resting sell orders. Skipping this cycle.")
                continue

            # Re-read the position after cancellation: qty_available now reflects
            # the freed shares, and the position may have changed size.
            try:
                position = api.get_position(symbol)
            except Exception as e:
                print(f"{symbol}: position gone after cancel ({e}). Skipping.")
                logging.info(f"{symbol}: position no longer exists after cancel: {e}")
                continue

            qty = float(position.qty)
            qty_available = float(getattr(position, 'qty_available', qty) or qty)
            # BUGFIX: sell exactly what the broker says is sellable. Rounding a
            # fractional qty to 4dp could exceed the real position and be rejected.
            sell_qty = min(qty, qty_available)
            if sell_qty <= 0:
                print(f"{symbol}: nothing available to sell. Skipping.")
                continue

            print(f"Selling {sell_qty} sh of {symbol} @ {GREEN}${current_price:.2f}{RESET} "
                  f"(entry ${bought_price:.2f}) — {reason}")
            sell_order = api.submit_order(symbol=symbol, qty=str(sell_qty), side='sell',
                                          type='market', time_in_force='day')
            logging.info(f"{now_str} Submitted sell {sell_qty} sh of {symbol} at ~{current_price:.2f}: {reason}")

            # BUGFIX: the original never confirmed the sell filled -- it deleted the
            # DB row immediately, so a rejected sell silently desynced the DB from
            # the broker and the bot believed it was flat while still holding shares.
            sold_qty = 0.0
            sold_price = current_price
            for _ in range(15):
                try:
                    so = api.get_order(sell_order.id)
                except Exception as e:
                    logging.warning(f"{symbol}: sell poll error ({e}); retrying.")
                    time.sleep(2)
                    continue
                sold_qty = float(so.filled_qty or 0)
                if so.filled_avg_price:
                    sold_price = float(so.filled_avg_price)
                if so.status == 'filled':
                    break
                if so.status in ('canceled', 'expired', 'rejected'):
                    logging.warning(f"{symbol}: sell order {so.status}, filled {sold_qty:.4f}.")
                    break
                time.sleep(2)

            if sold_qty <= 0:
                print(f"{symbol}: sell did not fill. Position retained.")
                logging.warning(f"{now_str} Sell not filled for {symbol}; DB row retained.")
                continue

            print(f"Sold {sold_qty:.4f} sh of {symbol} @ {GREEN}${sold_price:.2f}{RESET}")
            logging.info(f"{now_str} Sold {sold_qty:.4f} sh of {symbol} at {sold_price:.2f}")

            with open(csv_filename, mode='a', newline='') as f:
                csv.DictWriter(f, fieldnames=fieldnames).writerow({
                    'Date': now_str, 'Buy': '', 'Sell': 'Sell',
                    'Quantity': sold_qty, 'Symbol': symbol,
                    'Price Per Share': sold_price,
                })
            to_remove.append((symbol, sold_qty, sold_price))

        except Exception as e:
            print(f"Error processing sell for {symbol}: {e}")
            logging.error(f"Error processing sell for {symbol}: {e}")
        finally:
            # BUGFIX: always release the claim, even on the `continue` paths and
            # on exception, or the symbol is permanently locked out of trading.
            position_book.release(symbol)

    if not to_remove:
        return
    try:
        with lock:
            for symbol, qty, price in to_remove:
                session.add(TradeHistory(symbols=symbol, action='sell',
                                         quantity=qty, price=price, date=today_date_str))
                # BUGFIX: a partial sell used to delete the whole Position row,
                # making the bot forget shares it still owned. Decrement instead,
                # and only remove the row when the position is actually closed.
                row = session.query(Position).filter_by(symbols=symbol).one_or_none()
                if row and (row.quantity - qty) > 1e-6:
                    row.quantity -= qty
                    print(f"{symbol}: partial sell, {row.quantity:.4f} sh still held.")
                else:
                    session.query(Position).filter_by(symbols=symbol).delete()
                    position_book.remove(symbol)
                    # Reset peak tracking so a later re-buy starts a fresh run
                    # rather than inheriting the old position's high-water mark.
                    profit_monitor.clear(symbol)
            session.commit()
    except SQLAlchemyError as e:
        session.rollback()
        print(f"Database error: {e}")
        logging.error(f"Database error: {e}")
        return

    # BUGFIX: refresh_after_sell() used to run INSIDE `with lock`. It makes
    # blocking API calls (list_positions + a paginated order lookup per symbol),
    # holding the mutex for tens of seconds and serializing both threads. It is
    # now called after the lock is released.
    refresh_after_sell()


def refresh_after_sell():
    # BUGFIX: no longer rebinds a global. replace_all() mutates the single shared
    # PositionBook in place, so both threads keep seeing the same object.
    position_book.replace_all(update_symbols_to_sell_from_api())


def load_positions_from_database():
    return {p.symbols: (p.avg_price, p.purchase_date) for p in session.query(Position).all()}


def reconcile_positions_on_startup():
    """
    Alpaca is the single source of truth. The local .db is only a cache.

    BUGFIX: main() previously did `load_positions_from_database()` and then only
    called the API `if not symbols_to_sell_dict` -- i.e. it ONLY synced when the
    DB was empty. A non-empty stale DB was therefore NEVER reconciled, so after a
    restart the bot would:
      - try to sell phantom positions closed while it was down (endless
        "position does not exist" errors), and
      - be blind to positions opened by hand or by another process.

    On startup we now:
      1. Pull live positions from Alpaca.
      2. DELETE any DB row with no matching live position.
      3. Insert/update rows for every live position (correcting drifted qty and
         avg_price, since the broker's numbers are authoritative).
      4. Re-arm the profit monitor so an in-flight winner keeps following its
         peak across the restart instead of dumping at the first tick.

    Raises on API failure: starting up on an unverified DB is more dangerous
    than not starting at all.
    """
    print("\n--- Reconciling local database against Alpaca positions ---")

    try:
        live_positions = api.list_positions()
    except Exception as e:
        # Do NOT silently fall back to the stale DB.
        msg = f"FATAL: cannot reach Alpaca to reconcile positions on startup: {e}"
        print(f"{RED}{msg}{RESET}")
        logging.critical(msg)
        raise

    live = {}
    for p in live_positions:
        try:
            live[p.symbol] = {'qty': float(p.qty), 'avg_price': float(p.avg_entry_price)}
        except (TypeError, ValueError) as e:
            logging.error(f"Skipping malformed position {getattr(p, 'symbol', '?')}: {e}")

    db_rows = {r.symbols: r for r in session.query(Position).all()}

    # --- 1. Drop DB rows Alpaca does not know about ---
    orphans = [s for s in db_rows if s not in live]
    for sym in orphans:
        row = db_rows[sym]
        print(f"  {RED}REMOVED{RESET} {sym}: in local DB ({row.quantity:.4f} sh @ "
              f"${row.avg_price:.2f}) but NOT held at Alpaca — deleting stale row.")
        logging.warning(f"Startup reconcile: deleting stale DB position {sym} "
                        f"(qty={row.quantity}, avg={row.avg_price}); not present at broker.")
        session.delete(row)
        profit_monitor.clear(sym)

    # --- 2. Insert/correct rows for live positions ---
    result = {}
    for sym, info in live.items():
        qty, avg = info['qty'], info['avg_price']
        row = db_rows.get(sym)

        if row is None:
            pdate = get_most_recent_purchase_date(sym)
            print(f"  {GREEN}ADDED{RESET}   {sym}: held at Alpaca ({qty:.4f} sh @ "
                  f"${avg:.2f}) but missing locally — inserting.")
            logging.warning(f"Startup reconcile: adding untracked broker position {sym}.")
            session.add(Position(symbols=sym, quantity=qty, avg_price=avg, purchase_date=pdate))
        else:
            pdate = row.purchase_date or get_most_recent_purchase_date(sym)
            drift_qty = abs(row.quantity - qty) > 1e-6
            drift_avg = abs(row.avg_price - avg) > 0.005
            if drift_qty or drift_avg:
                print(f"  {RED}CORRECTED{RESET} {sym}: DB had {row.quantity:.4f} sh @ "
                      f"${row.avg_price:.2f}, broker says {qty:.4f} sh @ ${avg:.2f}.")
                logging.warning(f"Startup reconcile: correcting {sym} to broker values.")
            else:
                print(f"  {GREEN}OK{RESET}      {sym}: {qty:.4f} sh @ ${avg:.2f}")
            row.quantity, row.avg_price, row.purchase_date = qty, avg, pdate

        result[sym] = (avg, pdate)

    try:
        session.commit()
    except SQLAlchemyError as e:
        session.rollback()
        logging.critical(f"Startup reconcile commit failed: {e}")
        raise

    # --- 3. Re-arm the profit monitor for positions already in profit ---
    # Without this, a position that had run to +3% before the restart would lose
    # its peak and exit at the next +0.5% tick, giving back the whole move.
    if USE_PROFIT_MONITOR:
        for sym, (avg, _pdate) in result.items():
            cp = get_current_price(sym)
            if cp is None:
                continue
            gain = (cp - avg) / avg if avg else 0
            if gain >= ARM_PROFIT_PCT:
                # Seed the peak at the current price. The true pre-restart peak is
                # unknowable, so this conservatively restarts the ratchet from here.
                profit_monitor.evaluate(sym, avg, cp)
                print(f"  Re-armed profit monitor for {sym} at {gain*100:+.2f}% "
                      f"(peak reset to current price).")

    kept, removed, added = len(result), len(orphans), len([s for s in live if s not in db_rows])
    summary = f"Reconcile complete: {kept} live position(s), {removed} stale row(s) deleted, {added} added."
    print(f"--- {summary} ---\n")
    logging.info(summary)
    return result


def _run_and_release(fn, *args):
    """
    Thread entry point. scoped_session gives each thread its own Session, which
    must be released when the thread finishes or its DB connection leaks.
    Also stops an unhandled exception in a worker from dying silently.
    """
    try:
        fn(*args)
    except Exception as e:
        print(f"Unhandled error in {fn.__name__}: {e}")
        logging.exception(f"Unhandled error in {fn.__name__}: {e}")
    finally:
        Session.remove()


def main():
    global symbols_to_buy
    print("Starting main trading program...")
    symbols_to_buy = get_symbols_to_buy()

    # BUGFIX: was `load_positions_from_database()`, which trusted the stale .db
    # on restart. Alpaca is authoritative; reconcile before touching anything.
    position_book.replace_all(reconcile_positions_on_startup())

    # BUGFIX: main() created a fresh `lock = threading.Lock()` while the
    # module-level buy_sell_lock sat unused, which was confusing and made it easy
    # to reintroduce a second, non-shared mutex. Use the one module-level lock.
    lock = buy_sell_lock

    while True:
        try:
            stop_if_stock_market_is_closed()
            now_str = datetime.now(eastern).strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")
            st = get_margin_state()

            print("------------------------------------------------------------------------------------\n")
            print("*****************************************************")
            print("******** Billionaire Buying Strategy Version ********")
            print("*****************************************************")
            print("2026 Edition of the Advanced Stock Market Trading Robot, Version 9")
            print("by https://github.com/CodeProSpecialist")
            print("------------------------------------------------------------------------------------")
            print(f" {now_str} Cash Balance: ${st['cash']:,.2f}")
            print(f" Equity: ${st['equity']:,.2f} | Buying Power: ${st['buying_power']:,.2f} | "
                  f"Effective BP (leverage cap {MAX_LEVERAGE:.1f}x): ${st['effective_bp']:,.2f}")
            print(f" Day-trading BP: ${st['daytrading_buying_power']:,.2f} | Reg-T BP: ${st['regt_buying_power']:,.2f}")
            print(f" Margin health (equity/long_mv): {st['margin_ratio']:.2f} "
                  f"(floor {MAINTENANCE_MARGIN_FLOOR_PCT:.2f}) -> "
                  f"{GREEN + 'OK' + RESET if st['healthy'] else RED + 'BREACHED' + RESET}")
            print(f" Account mode: {ACCOUNT_MODE} | Day trades: "
                  f"{'UNLIMITED (2026 margin rules - PDT retired)' if UNLIMITED_DAY_TRADES else 'limited'}")
            print("------------------------------------------------------------------------------------\n")

            symbols_to_buy = get_symbols_to_buy()

            # PERF: one batched yf.download() seeds the daily SMA/RSI/ATR cache
            # for every symbol. Without it each symbol costs 3 separate yfinance
            # requests (48 for 16 symbols); batched it is 1. Cheap no-op when the
            # 30m cache TTLs are still warm.
            prewarm_daily_cache(symbols_to_buy)

            # BUGFIX: was `if not symbols_to_sell_dict:` -- the API resync only ran
            # when the dict was EMPTY, so a populated-but-stale view was never
            # corrected. Resync every cycle, in place, before starting threads.
            position_book.replace_all(update_symbols_to_sell_from_api())

            buy_thread = threading.Thread(target=_run_and_release,
                                          args=(buy_stocks, symbols_to_buy, lock),
                                          name='buy')
            sell_thread = threading.Thread(target=_run_and_release,
                                           args=(sell_stocks, lock),
                                           name='sell')
            buy_thread.start()
            sell_thread.start()
            # BUGFIX: bound the join. Without a timeout, a worker wedged on a
            # hung API call would freeze the main loop forever with no output.
            buy_thread.join(timeout=THREAD_JOIN_TIMEOUT)
            sell_thread.join(timeout=THREAD_JOIN_TIMEOUT)
            for t in (buy_thread, sell_thread):
                if t.is_alive():
                    msg = (f"WARNING: {t.name}_stocks thread still running after "
                           f"{THREAD_JOIN_TIMEOUT}s; continuing without it. It holds no "
                           f"lock indefinitely, but check for a hung API call.")
                    print(f"{RED}{msg}{RESET}")
                    logging.error(msg)

            if PRINT_SYMBOLS_TO_BUY:
                print("\nSymbols to Purchase:\n")
                # BUGFIX: original shadowed the `symbols_to_buy` list with the
                # loop variable, destroying the list after the first pass.
                for sym in symbols_to_buy:
                    cp = get_current_price(sym)
                    if cp is None:
                        continue
                    prev = get_previous_price(sym) or cp
                    print(f"Symbol: {sym} | Current Price: {GREEN if cp > prev else RED}${cp:.2f}{RESET}")
                print("")

            if PRINT_ROBOT_STORED_BUY_AND_SELL_LIST_DATABASE:
                print_database_tables()

            if DEBUG:
                print("\nSymbols to Purchase:\n")
                for sym in symbols_to_buy:
                    cp = get_current_price(sym)
                    lo = get_atr_low_price(sym)
                    if cp is None:
                        continue
                    prev = get_previous_price(sym) or cp
                    lo_s = f"${lo:.2f}" if lo else "n/a"
                    print(f"Symbol: {sym} | Current: {GREEN if cp > prev else RED}${cp:.2f}{RESET} | ATR low: {lo_s}")
                print("\nSymbols to Sell:\n")
                for sym in sorted(position_book.symbols()):
                    cp = get_current_price(sym)
                    hi = get_atr_high_price(sym)
                    if cp is None:
                        continue
                    prev = get_previous_price(sym) or cp
                    hi_s = f"${hi:.2f}" if hi else "n/a"
                    print(f"Symbol: {sym} | Current: {GREEN if cp > prev else RED}${cp:.2f}{RESET} | ATR high: {hi_s}")
                print("")

            print("Waiting 1 minute before checking price data again........")
            time.sleep(60)

        except Exception as e:
            logging.error(f"Error encountered: {e}")
            print(f"Error encountered in main loop: {e}")
            time.sleep(120)


if __name__ == '__main__':
    try:
        print("Initializing trading bot...")
        main()
    except KeyboardInterrupt:
        print("\nShutting down.")
    except Exception as e:
        logging.error(f"Error encountered: {e}")
        print(f"Critical error: {e}")
    finally:
        Session.remove()
