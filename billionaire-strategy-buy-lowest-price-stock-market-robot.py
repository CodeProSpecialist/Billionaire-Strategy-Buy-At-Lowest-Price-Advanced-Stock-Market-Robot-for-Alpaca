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
import talib
import yfinance as yf
import sqlalchemy
from sqlalchemy import create_engine, Column, Integer, String, Float
from sqlalchemy.orm import sessionmaker
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

global symbols_to_buy, symbols_to_sell_dict
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

logging.basicConfig(filename='trading-bot-program-logging-messages.txt', level=logging.INFO)

csv_filename = 'log-file-of-buy-and-sell-signals.csv'
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


engine = create_engine('sqlite:///trading_bot.db')
Session = sessionmaker(bind=engine)
session = Session()
Base.metadata.create_all(engine)

data_cache = {}
CACHE_EXPIRY = 120

CALLS = 60
PERIOD = 60


# ---------------- Symbol helpers (BUGFIX: consistent normalization) ----------------
def to_yf(sym):
    """yfinance uses dashes for share classes: BRK.B -> BRK-B"""
    return sym.strip().upper().replace('.', '-')


def to_alpaca(sym):
    """Alpaca uses dots: BRK-B -> BRK.B"""
    return sym.strip().upper().replace('-', '.')


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def get_cached_data(symbols, data_type, fetch_func, *args, **kwargs):
    key = (symbols, data_type)
    current_time = time.time()
    if key in data_cache and current_time - data_cache[key]['timestamp'] < CACHE_EXPIRY:
        return data_cache[key]['data']
    data = fetch_func(*args, **kwargs)
    data_cache[key] = {'timestamp': current_time, 'data': data}
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


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
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


def _last_close(ticker):
    try:
        h = ticker.history(period='1d')
        if h.empty:
            return None
        return float(h['Close'].iloc[-1])
    except Exception:
        return None


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def _fetch_current_price(symbols):
    with yf_lock:
        yf_symbol = to_yf(symbols)
        now = datetime.now(eastern)
        t = now.time()
        ticker = yf.Ticker(yf_symbol)
        current_price = None
        try:
            if time2(4, 0) <= t < time2(20, 0):
                prepost = not (time2(9, 30) <= t < time2(16, 0))
                data = ticker.history(period='1d', interval='1m', prepost=prepost)
                if not data.empty:
                    current_price = float(data['Close'].iloc[-1])
            if current_price is None:
                current_price = _last_close(ticker)
        except Exception as e:
            logging.error(f"Error fetching current price for {yf_symbol}: {e}")
            current_price = _last_close(ticker)

        if current_price is None:
            logging.error(f"Failed to retrieve current price for {yf_symbol}.")
            return None
        return round(current_price, 4)


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def get_average_true_range(symbols):
    def _fetch_atr(sym):
        yf_symbol = to_yf(sym)
        data = yf.Ticker(yf_symbol).history(period='60d')
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

    return get_cached_data(symbols, 'atr', _fetch_atr, symbols)


def get_atr_high_price(sym):
    atr = get_average_true_range(sym)
    cp = get_current_price(sym)
    return round(cp + 0.40 * atr, 4) if cp and atr else None


def get_atr_low_price(sym):
    atr = get_average_true_range(sym)
    cp = get_current_price(sym)
    return round(cp - 0.10 * atr, 4) if cp and atr else None


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def is_in_uptrend(symbols_to_buy):
    yf_symbol = to_yf(symbols_to_buy)
    hist = yf.Ticker(yf_symbol).history(period='1y')
    if hist.empty or len(hist) < 200:
        return False
    sma_200 = talib.SMA(hist['Close'].values, timeperiod=200)[-1]
    cp = get_current_price(symbols_to_buy)
    if cp is None or not np.isfinite(sma_200):
        return False
    return cp > sma_200


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def get_daily_rsi(symbols_to_buy):
    yf_symbol = to_yf(symbols_to_buy)
    hist = yf.Ticker(yf_symbol).history(period='60d', interval='1d')
    if hist.empty or len(hist) < 15:
        return None
    rsi = talib.RSI(hist['Close'].values, timeperiod=14)[-1]
    return round(float(rsi), 2) if np.isfinite(rsi) else None


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def calculate_technical_indicators(symbols, lookback_days=90):
    yf_symbol = to_yf(symbols)
    hist = yf.Ticker(yf_symbol).history(period=f'{lookback_days}d')
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


def buy_stocks(symbols_to_sell_dict, symbols_to_buy_list, lock):
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

        df = yf.Ticker(yf_symbol).history(period="90d")
        # BUGFIX: MACD(26,9) and RSI(14) need ~35 bars. Original required
        # only 3 rows, producing all-NaN indicators that silently scored 0.
        if df.empty or len(df) < 40:
            print(f"{yf_symbol}: insufficient history ({len(df)} bars). Skipping.")
            continue

        previous_price = get_previous_price(symbol) or current_price
        last_price = float(df['Close'].iloc[-1])

        # --- Trend + multi-timeframe filters (cheap gates first) ---
        if not is_in_uptrend(symbol):
            print(f"{yf_symbol}: below 200-day SMA. Skipping.")
            update_previous_price(symbol, current_price)
            continue

        daily_rsi = get_daily_rsi(symbol)
        if daily_rsi is None or daily_rsi > 50:
            print(f"{yf_symbol}: daily RSI not oversold ({daily_rsi}). Skipping.")
            update_previous_price(symbol, current_price)
            continue

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
            notional = 1.00
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
            notional = min(
                notional,
                MAX_ALLOCATION_PER_SYMBOL,
                max_new_exposure,
                (cash_available - CASH_BUFFER) if ACCOUNT_MODE == 'cash' else max_new_exposure,
            )
            # Slippage haircut
            notional *= 0.999

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
            order_filled = False
            for _ in range(30):
                o = api.get_order(buy_order.id)
                if o.status == 'filled':
                    order_filled = True
                    filled_qty = float(o.filled_qty)
                    filled_price = float(o.filled_avg_price or current_price)
                    break
                # BUGFIX: bail out of the poll loop on terminal states instead
                # of burning 60 seconds waiting on a dead order.
                if o.status in ('canceled', 'expired', 'rejected'):
                    print(f"{api_symbol}: order {o.status}.")
                    logging.warning(f"{api_symbol}: order {o.status}.")
                    break
                time.sleep(2)

            if order_filled and filled_qty > 0:
                print(f"Filled {filled_qty:.4f} sh of {api_symbol} @ "
                      f"{GREEN}${filled_price:.2f}{RESET} (cost ${filled_qty * filled_price:.2f})")
                with open(csv_filename, mode='a', newline='') as f:
                    csv.DictWriter(f, fieldnames=fieldnames).writerow({
                        'Date': now_str, 'Buy': 'Buy', 'Sell': '',
                        'Quantity': filled_qty, 'Symbol': api_symbol,
                        'Price Per Share': filled_price,
                    })
                filled_records.append((api_symbol, yf_symbol, filled_qty, filled_price, today_date_str))

                # BUGFIX: PDT gate removed. Trailing stop no longer refuses
                # fractional quantities — it now uses a notional-safe path.
                if not ALL_BUY_ORDERS_ARE_1_DOLLAR:
                    sid = place_trailing_stop_sell_order(api_symbol, filled_qty, filled_price)
                    print(f"Trailing stop for {api_symbol}: {sid or 'not placed (see log)'}")
            else:
                print(f"Buy order not filled for {api_symbol}")
                logging.info(f"{now_str} Buy order not filled for {api_symbol}")

        except tradeapi.rest.APIError as e:
            print(f"Error submitting buy order for {api_symbol}: {e}")
            logging.error(f"Error submitting buy order for {api_symbol}: {e}")
            continue

        update_previous_price(symbol, current_price)
        time.sleep(0.8)

    # ---------------- Persist fills ----------------
    if not filled_records:
        return
    try:
        with lock:
            for api_symbol, yf_symbol, qty, price, dstr in filled_records:
                symbols_to_sell_dict[api_symbol] = (round(price, 4), dstr)
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
        refresh_after_buy()
    except SQLAlchemyError as e:
        session.rollback()
        print(f"Database error: {e}")
        logging.error(f"Database error: {e}")


def refresh_after_buy():
    global symbols_to_buy, symbols_to_sell_dict
    time.sleep(2)
    symbols_to_buy = get_symbols_to_buy()
    symbols_to_sell_dict = update_symbols_to_sell_from_api()


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def place_trailing_stop_sell_order(symbol, qty, current_price):
    """
    BUGFIX: the original rejected any fractional qty — but every buy is a
    notional (fractional) order, so a stop was NEVER placed. We now place a
    trailing stop on the whole-share portion and leave the fractional
    remainder to the sell_stocks take-profit logic.
    Also: the old `daytrade_count < 3` PDT gate is gone.
    """
    try:
        whole = int(qty)
        if whole < 1:
            logging.info(f"{symbol}: qty {qty:.4f} < 1 share; trailing stop not supported by broker. "
                         f"Managed by sell_stocks instead.")
            return None

        trail_percent = 1.0
        stop_order = api.submit_order(
            symbol=symbol,
            qty=whole,
            side='sell',
            type='trailing_stop',
            trail_percent=str(trail_percent),
            time_in_force='gtc',
        )
        logging.info(f"Placed trailing stop ({trail_percent}%) for {whole} sh of {symbol}: {stop_order.id}")
        return stop_order.id
    except Exception as e:
        print(f"Error placing trailing stop for {symbol}: {e}")
        logging.error(f"Error placing trailing stop for {symbol}: {e}")
        return None


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


def sell_stocks(symbols_to_sell_dict, lock):
    print("Starting sell_stocks function...")
    to_remove = []
    now_str = datetime.now(eastern).strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")
    today_date_str = datetime.now(eastern).date().strftime("%Y-%m-%d")
    comparison_date = datetime.now(eastern).date()

    # 2026 margin rules: same-day round trips are permitted. The old code
    # implicitly deferred sales to avoid PDT flags; that gate is removed.
    for symbol, (bought_price, purchase_date) in list(symbols_to_sell_dict.items()):
        try:
            bought_date = datetime.strptime(purchase_date, "%Y-%m-%d").date()
        except (ValueError, TypeError) as e:
            logging.error(f"Bad purchase_date for {symbol}: {purchase_date} ({e})")
            continue

        if bought_date > comparison_date:
            continue

        current_price = get_current_price(symbol)
        if current_price is None:
            continue

        try:
            position = api.get_position(symbol)
            bought_price = float(position.avg_entry_price)
            qty = float(position.qty)
            qty_available = float(getattr(position, 'qty_available', qty) or qty)

            open_orders = [o for o in api.list_orders(status='open') if o.symbol == symbol]
            # BUGFIX: only a resting SELL blocks a new sell. The original
            # counted ANY open order (including the buy that just went in).
            open_sells = [o for o in open_orders if o.side == 'sell']

            sell_threshold = bought_price * 1.005
            if current_price < sell_threshold:
                print(f"{symbol}: {RED}${current_price:.2f}{RESET} < target ${sell_threshold:.2f}. Holding.")
                continue

            if open_sells:
                # A trailing stop is resting on the whole-share portion.
                # Sell only the fractional remainder that isn't held by it.
                held = sum(float(o.qty or 0) for o in open_sells)
                sellable = max(0.0, min(qty_available, qty - held))
                if sellable < 1e-4:
                    print(f"{symbol}: all shares held by open sell orders. Skipping.")
                    continue
                qty = round(sellable, 4)

            print(f"Selling {qty:.4f} sh of {symbol} @ {GREEN}${current_price:.2f}{RESET} "
                  f"(entry ${bought_price:.2f}, target ${sell_threshold:.2f})")
            api.submit_order(symbol=symbol, qty=qty, side='sell',
                             type='market', time_in_force='day')
            logging.info(f"{now_str} Sold {qty:.4f} sh of {symbol} at {current_price:.2f}")

            with open(csv_filename, mode='a', newline='') as f:
                csv.DictWriter(f, fieldnames=fieldnames).writerow({
                    'Date': now_str, 'Buy': '', 'Sell': 'Sell',
                    'Quantity': qty, 'Symbol': symbol,
                    'Price Per Share': current_price,
                })
            to_remove.append((symbol, qty, current_price))

        except Exception as e:
            print(f"Error processing sell for {symbol}: {e}")
            logging.error(f"Error processing sell for {symbol}: {e}")

    if not to_remove:
        return
    try:
        with lock:
            for symbol, qty, price in to_remove:
                symbols_to_sell_dict.pop(symbol, None)   # BUGFIX: safe pop
                session.add(TradeHistory(symbols=symbol, action='sell',
                                         quantity=qty, price=price, date=today_date_str))
                session.query(Position).filter_by(symbols=symbol).delete()
            session.commit()
        refresh_after_sell()
    except SQLAlchemyError as e:
        session.rollback()
        print(f"Database error: {e}")
        logging.error(f"Database error: {e}")


def refresh_after_sell():
    global symbols_to_sell_dict
    symbols_to_sell_dict = update_symbols_to_sell_from_api()


def load_positions_from_database():
    return {p.symbols: (p.avg_price, p.purchase_date) for p in session.query(Position).all()}


def main():
    global symbols_to_buy, symbols_to_sell_dict
    print("Starting main trading program...")
    symbols_to_buy = get_symbols_to_buy()
    symbols_to_sell_dict = load_positions_from_database()
    lock = threading.Lock()

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
            if not symbols_to_sell_dict:
                symbols_to_sell_dict = update_symbols_to_sell_from_api()

            buy_thread = threading.Thread(target=buy_stocks,
                                          args=(symbols_to_sell_dict, symbols_to_buy, lock))
            sell_thread = threading.Thread(target=sell_stocks,
                                           args=(symbols_to_sell_dict, lock))
            buy_thread.start()
            sell_thread.start()
            buy_thread.join()
            sell_thread.join()

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
                for sym in list(symbols_to_sell_dict.keys()):
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
        session.close()
