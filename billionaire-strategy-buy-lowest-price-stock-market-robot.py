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
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import SQLAlchemyError

# Load environment variables for Alpaca API
APIKEYID = os.getenv('APCA_API_KEY_ID')
APISECRETKEY = os.getenv('APCA_API_SECRET_KEY')
APIBASEURL = os.getenv('APCA_API_BASE_URL')

# Initialize the Alpaca API
api = tradeapi.REST(APIKEYID, APISECRETKEY, APIBASEURL)

# Global variables
global stocks_to_buy, today_date, today_datetime, csv_writer, csv_filename, fieldnames, price_changes, end_time
global current_price, today_date_str, qty
global price_history, last_stored, interval_map

# Configuration flags
PRINT_STOCKS_TO_BUY = False  # Set to False for faster execution
PRINT_ROBOT_STORED_BUY_AND_SELL_LIST_DATABASE = True  # Set to True to view database
PRINT_DATABASE = True  # Set to True to view stocks to sell
DEBUG = False  # Set to False for faster execution
ALL_BUY_ORDERS_ARE_1_DOLLAR = False  # When True, every buy order is a $1.00 fractional share market day order

# Set the timezone to Eastern
eastern = pytz.timezone('US/Eastern')

# Dictionary to maintain previous prices and price changes
stock_data = {}
previous_prices = {}
price_changes = {}
end_time = 0  # Initialize end_time as a global variable

price_history = {}  # symbol -> interval -> list of prices
last_stored = {}    # symbol -> interval -> last_timestamp
interval_map = {
    '1min': 60,
    '5min': 300,
    '10min': 600,
    '15min': 900,
    '30min': 1800,
    '45min': 2700,
    '60min': 3600
}  # intervals in seconds

# Define the API datetime format
api_time_format = '%Y-%m-%dT%H:%M:%S.%f-04:00'

# Thread locks for thread-safe operations
buy_sell_lock = threading.Lock()
yf_lock = threading.Lock()

# Logging configuration
logging.basicConfig(filename='trading-bot-program-logging-messages.txt', level=logging.INFO)

# Define the CSV file and fieldnames
csv_filename = 'log-file-of-buy-and-sell-signals.csv'
fieldnames = ['Date', 'Buy', 'Sell', 'Quantity', 'Symbol', 'Price Per Share']

# Initialize CSV file
with open(csv_filename, mode='w', newline='') as csv_file:
    csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
    csv_writer.writeheader()

# Define the Database Models
Base = sqlalchemy.orm.declarative_base()

class TradeHistory(Base):
    __tablename__ = 'trade_history'
    id = Column(Integer, primary_key=True)
    symbol = Column(String)
    action = Column(String)  # 'buy' or 'sell'
    quantity = Column(Float)  # Changed to Float for fractional shares
    price = Column(Float)
    date = Column(String)

class Position(Base):
    __tablename__ = 'positions'
    symbol = Column(String, primary_key=True)
    quantity = Column(Float)  # Changed to Float for fractional shares
    avg_price = Column(Float)
    purchase_date = Column(String)

# Initialize SQLAlchemy
engine = create_engine('sqlite:///trading_bot.db')
Session = sessionmaker(bind=engine)
session = Session()

# Create tables if they don't exist
Base.metadata.create_all(engine)

# Add a cache for yfinance data
data_cache = {}  # symbol -> {'timestamp': time, 'data_type': data}
CACHE_EXPIRY = 120  # 2 minutes

def get_cached_data(symbol, data_type, fetch_func, *args, **kwargs):
    print(f"Checking cache for {symbol} {data_type}...")
    key = (symbol, data_type)
    current_time = time.time()
    if key in data_cache and current_time - data_cache[key]['timestamp'] < CACHE_EXPIRY:
        print(f"Using cached {data_type} for {symbol}.")
        return data_cache[key]['data']
    else:
        print(f"Fetching new {data_type} for {symbol}...")
        data = fetch_func(*args, **kwargs)
        data_cache[key] = {'timestamp': current_time, 'data': data}
        print(f"Cached {data_type} for {symbol}.")
        return data

def stop_if_stock_market_is_closed():
    market_open_time = time2(9, 30)
    market_close_time = time2(16, 0)

    while True:
        eastern = pytz.timezone('US/Eastern')
        current_datetime = datetime.now(eastern)
        current_time = current_datetime.time()
        current_time_str = current_datetime.strftime("%A, %B %d, %Y, %I:%M:%S %p")

        if current_datetime.weekday() <= 4 and market_open_time <= current_time <= market_close_time:
            print("Market is open. Proceeding with trading operations.")
            break

        print("\n")
        print('''
        *********************************************************************************
        ************ Billionaire Buying Strategy Version ********************************
        *********************************************************************************
            2025 Edition of the Advanced Stock Market Trading Robot, Version 8 
                                https://github.com/CodeProSpecialist
                       Featuring an Accelerated Database Engine with Python 3 SQLAlchemy  
         ''')
        print(f'Current date & time (Eastern Time): {current_time_str}')
        print("Stockbot only works Monday through Friday: 9:30 am - 4:00 pm Eastern Time.")
        print("Waiting until Stock Market Hours to begin the Stockbot Trading Program.")
        print("\n")
        time.sleep(60)

def print_database_tables():
    if PRINT_DATABASE:
        positions = api.list_positions()
        show_price_percentage_change = True

        print("\nTrade History In This Robot's Database:")
        print("\n")
        print("Stock | Buy or Sell | Quantity | Avg. Price | Date ")
        print("\n")

        for record in session.query(TradeHistory).all():
            print(f"{record.symbol} | {record.action} | {record.quantity:.4f} | {record.price:.2f} | {record.date}")

        print("----------------------------------------------------------------")
        print("\n")
        print("Positions in the Database To Sell On or After the Date Shown:")
        print("\n")
        print("Stock | Quantity | Avg. Price | Date or The 1st Day This Robot Began Working ")
        print("\n")
        for record in session.query(Position).all():
            symbol, quantity, avg_price, purchase_date = record.symbol, record.quantity, record.avg_price, record.purchase_date
            purchase_date_str = purchase_date

            if show_price_percentage_change:
                current_price = get_current_price(symbol)
                percentage_change = ((current_price - avg_price) / avg_price) * 100 if current_price and avg_price else 0
                print(f"{symbol} | {quantity:.4f} | {avg_price:.2f} | {purchase_date_str} | Price Change: {percentage_change:.2f}%")
            else:
                print(f"{symbol} | {quantity:.4f} | {avg_price:.2f} | {purchase_date_str}")
        print("\n")

def get_stocks_to_trade():
    print("Loading stocks to trade from file...")
    try:
        with open('electricity-or-utility-stocks-to-buy-list.txt', 'r') as file:
            stock_symbols = [line.strip() for line in file.readlines()]
            print(f"Loaded {len(stock_symbols)} stock symbols from file.")

        if not stock_symbols:
            print("\n")
            print("********************************************************************************************************")
            print("*   Error: The file electricity-or-utility-stocks-to-buy-list.txt doesn't contain any stock symbols.   *")
            print("*   This Robot does not work until you place stock symbols in the file named:                          *")
            print("*       electricity-or-utility-stocks-to-buy-list.txt                                                  *")
            print("********************************************************************************************************")
            print("\n")
        return stock_symbols

    except FileNotFoundError:
        print("\n")
        print("****************************************************************************")
        print("*   Error: File not found: electricity-or-utility-stocks-to-buy-list.txt   *")
        print("****************************************************************************")
        print("\n")
        return []

def remove_symbol_from_trade_list(symbol):
    print(f"Removing {symbol} from trade list...")
    with open('electricity-or-utility-stocks-to-buy-list.txt', 'r') as file:
        lines = file.readlines()
    with open('electricity-or-utility-stocks-to-buy-list.txt', 'w') as file:
        for line in lines:
            if line.strip() != symbol:
                file.write(line)
    print(f"Successfully removed {symbol} from trade list.")

def get_opening_price(symbol):
    print(f"Fetching opening price for {symbol}...")
    symbol = symbol.replace('.', '-')
    stock_data = yf.Ticker(symbol)
    time.sleep(2)
    try:
        opening_price = round(float(stock_data.history(period="1d")["Open"].iloc[0].item()), 4)
        print(f"Opening price for {symbol}: ${opening_price:.4f}")
        return opening_price
    except IndexError:
        logging.error(f"Opening price not found for {symbol}.")
        print(f"Error: Opening price not found for {symbol}.")
        return None

def get_current_price(symbol, retries=3):
    print(f"Attempting to fetch current price for {symbol}...")
    for attempt in range(retries):
        try:
            return get_cached_data(symbol, 'current_price', _fetch_current_price, symbol)
        except Exception as e:
            logging.error(f"Retry {attempt+1}/{retries} failed for {symbol}: {e}")
            print(f"Retry {attempt+1}/{retries} failed for {symbol}: {e}")
            time.sleep(2 ** attempt)  # Exponential backoff
    print(f"Failed to fetch current price for {symbol} after {retries} attempts.")
    return None

def _fetch_current_price(symbol):
    with yf_lock:
        print(f"Fetching current price data for {symbol}...")
        symbol = symbol.replace('.', '-')
        eastern = pytz.timezone('US/Eastern')
        current_datetime = datetime.now(eastern)
        pre_market_start = time2(4, 0)
        pre_market_end = time2(9, 30)
        market_start = time2(9, 30)
        market_end = time2(16, 0)
        post_market_start = time2(16, 0)
        post_market_end = time2(20, 0)
        stock_data = yf.Ticker(symbol)
        time.sleep(2)
        try:
            if pre_market_start <= current_datetime.time() < pre_market_end:
                data = stock_data.history(start=current_datetime.strftime('%Y-%m-%d'), interval='1m', prepost=True)
                time.sleep(2)
                if not data.empty:
                    data.index = data.index.tz_convert(eastern)
                    pre_market_data = data.between_time(pre_market_start, pre_market_end)
                    current_price = float(pre_market_data['Close'].iloc[-1].item()) if not pre_market_data.empty else None
                    if current_price is None:
                        logging.error("Pre-market: Current Price not found, using last closing price.")
                        print("Pre-market: Current Price not found, using last closing price.")
                        last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                        time.sleep(2)
                        current_price = last_close
                else:
                    current_price = None
                    logging.error("Pre-market: Current Price not found, using last closing price.")
                    print("Pre-market: Current Price not found, using last closing price.")
                    last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                    time.sleep(2)
                    current_price = last_close
            elif market_start <= current_datetime.time() < market_end:
                data = stock_data.history(period='1d', interval='1m')
                time.sleep(2)
                if not data.empty:
                    data.index = data.index.tz_convert(eastern)
                    current_price = float(data['Close'].iloc[-1].item()) if not data.empty else None
                    if current_price is None:
                        logging.error("Market hours: Current Price not found, using last closing price.")
                        print("Market hours: Current Price not found, using last closing price.")
                        last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                        time.sleep(2)
                        current_price = last_close
                else:
                    current_price = None
                    logging.error("Market hours: Current Price not found, using last closing price.")
                    print("Market hours: Current Price not found, using last closing price.")
                    last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                    time.sleep(2)
                    current_price = last_close
            elif market_end <= current_datetime.time() < post_market_end:
                data = stock_data.history(start=current_datetime.strftime('%Y-%m-%d'), interval='1m', prepost=True)
                time.sleep(2)
                if not data.empty:
                    data.index = data.index.tz_convert(eastern)
                    post_market_data = data.between_time(post_market_start, post_market_end)
                    current_price = float(post_market_data['Close'].iloc[-1].item()) if not post_market_data.empty else None
                    if current_price is None:
                        logging.error("Post-market: Current Price not found, using last closing price.")
                        print("Post-market: Current Price not found, using last closing price.")
                        last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                        time.sleep(2)
                        current_price = last_close
                else:
                    current_price = None
                    logging.error("Post-market: Current Price not found, using last closing price.")
                    print("Post-market: Current Price not found, using last closing price.")
                    last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                    time.sleep(2)
                    current_price = last_close
            else:
                last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                time.sleep(2)
                current_price = last_close
        except Exception as e:
            logging.error(f"Error fetching current price for {symbol}: {e}")
            print(f"Error fetching current price for {symbol}: {e}")
            try:
                last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                time.sleep(2)
                current_price = last_close
            except Exception as e2:
                logging.error(f"Error fetching last closing price for {symbol}: {e2}")
                print(f"Error fetching last closing price for {symbol}: {e2}")
                current_price = None

        if current_price is None:
            error_message = f"Failed to retrieve current price for {symbol}."
            logging.error(error_message)
            print(error_message)

        current_price = round(current_price, 4) if current_price is not None else None
        print(f"Current price for {symbol}: ${current_price:.4f}" if current_price else f"Failed to retrieve current price for {symbol}.")
        return current_price

def get_atr_high_price(symbol):
    print(f"Calculating ATR high price for {symbol}...")
    symbol = symbol.replace('.', '-')
    atr_value = get_average_true_range(symbol)
    current_price = get_current_price(symbol)
    atr_high = round(current_price + 0.40 * atr_value, 4) if current_price and atr_value else None
    print(f"ATR high price for {symbol}: ${atr_high:.4f}" if atr_high else f"Failed to calculate ATR high price for {symbol}.")
    return atr_high

def get_atr_low_price(symbol):
    print(f"Calculating ATR low price for {symbol}...")
    symbol = symbol.replace('.', '-')
    atr_value = get_average_true_range(symbol)
    current_price = get_current_price(symbol)
    atr_low = round(current_price - 0.10 * atr_value, 4) if current_price and atr_value else None
    print(f"ATR low price for {symbol}: ${atr_low:.4f}" if atr_low else f"Failed to calculate ATR low price for {symbol}.")
    return atr_low

def get_average_true_range(symbol):
    print(f"Calculating ATR for {symbol}...")
    def _fetch_atr():
        symbol = symbol.replace('.', '-')
        ticker = yf.Ticker(symbol)
        time.sleep(2)
        data = ticker.history(period='30d')
        time.sleep(2)
        try:
            atr = talib.ATR(data['High'].values, data['Low'].values, data['Close'].values, timeperiod=22)
            atr_value = atr[-1]
            print(f"ATR for {symbol}: {atr_value:.4f}")
            return atr_value
        except Exception as e:
            logging.error(f"Error calculating ATR for {symbol}: {e}")
            print(f"Error calculating ATR for {symbol}: {e}")
            return None
    
    return get_cached_data(symbol, 'atr', _fetch_atr)

def is_in_uptrend(symbol):
    print(f"Checking if {symbol} is in uptrend (above 200-day SMA)...")
    symbol = symbol.replace('.', '-')
    stock_data = yf.Ticker(symbol)
    time.sleep(2)
    historical_data = stock_data.history(period='200d')
    time.sleep(2)
    if historical_data.empty or len(historical_data) < 200:
        print(f"Insufficient data for 200-day SMA for {symbol}. Assuming not in uptrend.")
        return False
    sma_200 = talib.SMA(historical_data['Close'].values, timeperiod=200)[-1]
    current_price = get_current_price(symbol)
    in_uptrend = current_price > sma_200 if current_price else False
    print(f"{symbol} {'is' if in_uptrend else 'is not'} in uptrend (Current: {current_price:.2f}, SMA200: {sma_200:.2f})")
    return in_uptrend

def get_daily_rsi(symbol):
    print(f"Calculating daily RSI for {symbol}...")
    symbol = symbol.replace('.', '-')
    stock_data = yf.Ticker(symbol)
    time.sleep(2)
    historical_data = stock_data.history(period='30d', interval='1d')
    time.sleep(2)
    if historical_data.empty:
        print(f"No daily data for {symbol}. RSI calculation failed.")
        return None
    rsi = talib.RSI(historical_data['Close'], timeperiod=14)[-1]
    rsi_value = round(rsi, 2) if not np.isnan(rsi) else None
    print(f"Daily RSI for {symbol}: {rsi_value}")
    return rsi_value

def status_printer_buy_stocks():
    print(f"\rBuy stocks function is working correctly right now. Checking stocks to buy.....", end='', flush=True)
    print()

def status_printer_sell_stocks():
    print(f"\rSell stocks function is working correctly right now. Checking stocks to sell.....", end='', flush=True)
    print()

def calculate_technical_indicators(symbol, lookback_days=90):
    print(f"Calculating technical indicators for {symbol} over {lookback_days} days...")
    symbol = symbol.replace('.', '-')
    stock_data = yf.Ticker(symbol)
    time.sleep(2)
    historical_data = stock_data.history(period=f'{lookback_days}d')
    time.sleep(2)
    short_window = 12
    long_window = 26
    signal_window = 9
    historical_data['macd'], historical_data['signal'], _ = talib.MACD(historical_data['Close'],
                                                                       fastperiod=short_window,
                                                                       slowperiod=long_window,
                                                                       signalperiod=signal_window)
    rsi_period = 14
    historical_data['rsi'] = talib.RSI(historical_data['Close'], timeperiod=rsi_period)
    historical_data['volume'] = historical_data['Volume']
    print(f"Technical indicators calculated for {symbol}.")
    return historical_data

def calculate_rsi(symbol, period=14, interval='5m'):
    print(f"Calculating RSI for {symbol} (period={period}, interval={interval})...")
    try:
        symbol = symbol.replace('.', '-')  # Adjust for yfinance compatibility
        stock_data = yf.Ticker(symbol)
        historical_data = stock_data.history(period='1d', interval=interval, prepost=True)
        time.sleep(2)
        
        if historical_data.empty or len(historical_data['Close']) < period:
            logging.error(f"Insufficient data for RSI calculation for {symbol} with {interval} interval.")
            print(f"Insufficient data for RSI calculation for {symbol}.")
            return None
        
        rsi = talib.RSI(historical_data['Close'], timeperiod=period)
        latest_rsi = rsi.iloc[-1] if not rsi.empty else None
        
        if latest_rsi is None or not np.isfinite(latest_rsi):
            logging.error(f"Invalid RSI value for {symbol}: {latest_rsi}")
            print(f"Invalid RSI value for {symbol}.")
            return None
        
        latest_rsi = round(latest_rsi, 2)
        print(f"RSI for {symbol}: {latest_rsi}")
        return latest_rsi
    
    except Exception as e:
        logging.error(f"Error calculating RSI for {symbol}: {e}")
        print(f"Error calculating RSI for {symbol}: {e}")
        return None

def print_technical_indicators(symbol, historical_data):
    print("")
    print(f"\nTechnical Indicators for {symbol}:\n")
    print(historical_data[['Close', 'macd', 'signal', 'rsi', 'volume']].tail())
    print("")

def calculate_cash_on_hand():
    print("Calculating available cash...")
    cash_available = round(float(api.get_account().cash), 2)
    print(f"Cash available: ${cash_available:.2f}")
    return cash_available

def calculate_total_symbols(stocks_to_buy):
    print("Calculating total symbols to trade...")
    total_symbols = len(stocks_to_buy)
    print(f"Total symbols: {total_symbols}")
    return total_symbols

def allocate_cash_equally(cash_available, total_symbols):
    print("Allocating cash equally among symbols...")
    max_allocation_per_symbol = 600.0  # Maximum dollar amount per stock
    allocation_per_symbol = min(max_allocation_per_symbol, cash_available / total_symbols) if total_symbols > 0 else 0
    allocation = round(allocation_per_symbol, 2)
    print(f"Allocation per symbol: ${allocation:.2f}")
    return allocation

def get_previous_price(symbol):
    print(f"Retrieving previous price for {symbol}...")
    if symbol in previous_prices:
        price = previous_prices[symbol]
        print(f"Previous price for {symbol}: ${price:.4f}")
        return price
    else:
        current_price = get_current_price(symbol)
        previous_prices[symbol] = current_price
        print(f"No previous price for {symbol} was found. Using the current price as the previous price: {current_price}")
        return current_price

def update_previous_price(symbol, current_price):
    print(f"Updating previous price for {symbol} to ${current_price:.4f}")
    previous_prices[symbol] = current_price

def run_schedule():
    print("Running schedule for pending tasks...")
    while not end_time_reached():
        schedule.run_pending()
        time.sleep(1)
    print("Schedule completed.")

def track_price_changes(symbol):
    print(f"Tracking price changes for {symbol}...")
    current_price = get_current_price(symbol)
    previous_price = get_previous_price(symbol)

    print("")
    print_technical_indicators(symbol, calculate_technical_indicators(symbol))
    print("")

    if symbol not in price_changes:
        price_changes[symbol] = {'increased': 0, 'decreased': 0}

    if current_price > previous_price:
        price_changes[symbol]['increased'] += 1
        print(f"{symbol} price just increased | current price: {current_price}")
        time.sleep(2)
    elif current_price < previous_price:
        price_changes[symbol]['decreased'] += 1
        print(f"{symbol} price just decreased | current price: {current_price}")
        time.sleep(2)
    else:
        print(f"{symbol} price has not changed | current price: {current_price}")
        time.sleep(2)
    time.sleep(1)
    update_previous_price(symbol, current_price)

def end_time_reached():
    reached = time.time() >= end_time
    print(f"Checking if end time reached: {'Yes' if reached else 'No'}")
    return reached

def get_last_price_within_past_5_minutes(symbols_to_buy):
    print("Fetching last prices within past 5 minutes for symbols...")
    results = {}
    eastern = pytz.timezone('US/Eastern')
    current_datetime = datetime.now(eastern)
    end_time = current_datetime
    start_time = end_time - timedelta(minutes=5)

    for symbol in symbols_to_buy:
        print(f"Fetching 5-minute price data for {symbol}...")
        try:
            symbol = symbol.replace('.', '-')
            data = yf.download(symbol, start=start_time, end=end_time, interval='1m', prepost=True, auto_adjust=False)
            time.sleep(2)
            if not data.empty:
                last_price = round(float(data['Close'].iloc[-1].item()), 2)
                results[symbol] = last_price
                print(f"Last price for {symbol} within 5 minutes: ${last_price:.2f}")
            else:
                results[symbol] = None
                print(f"No price data found for {symbol} within past 5 minutes.")
        except Exception as e:
            print(f"Error occurred while fetching data for {symbol}: {e}")
            logging.error(f"Error occurred while fetching data for {symbol}: {e}")
            results[symbol] = None

    return results

def get_most_recent_purchase_date(symbol):
    print(f"Retrieving most recent purchase date for {symbol}...")
    try:
        purchase_date_str = None
        order_list = []
        CHUNK_SIZE = 500
        end_time = datetime.now(pytz.UTC).isoformat()

        while True:
            print(f"Fetching orders for {symbol} until {end_time}...")
            order_chunk = api.list_orders(
                status='all',
                nested=False,
                direction='desc',
                until=end_time,
                limit=CHUNK_SIZE,
                symbols=[symbol]
            )
            if order_chunk:
                order_list.extend(order_chunk)
                end_time = (order_chunk[-1].submitted_at - timedelta(seconds=1)).isoformat()
                print(f"Fetched {len(order_chunk)} orders for {symbol}.")
            else:
                print(f"No more orders to fetch for {symbol}.")
                break

        buy_orders = [
            order for order in order_list
            if order.side == 'buy' and order.status == 'filled' and order.filled_at
        ]

        if buy_orders:
            most_recent_buy = max(buy_orders, key=lambda order: order.filled_at)
            purchase_date = most_recent_buy.filled_at.date()
            purchase_date_str = purchase_date.strftime("%Y-%m-%d")
            print(f"Most recent purchase date for {symbol}: {purchase_date_str} (from {len(buy_orders)} buy orders)")
            logging.info(f"Most recent purchase date for {symbol}: {purchase_date_str} (from {len(buy_orders)} buy orders)")
        else:
            purchase_date = datetime.now(pytz.UTC).date()
            purchase_date_str = purchase_date.strftime("%Y-%m-%d")
            print(f"No filled buy orders found for {symbol}. Using today's date: {purchase_date_str}")
            logging.warning(f"No filled buy orders found for {symbol}. Using today's date: {purchase_date_str}. Orders fetched: {len(order_list)}.")

        return purchase_date_str

    except Exception as e:
        logging.error(f"Error fetching buy orders for {symbol}: {e}")
        purchase_date = datetime.now(pytz.UTC).date()
        purchase_date_str = purchase_date.strftime("%Y-%m-%d")
        print(f"Error fetching buy orders for {symbol}: {e}. Using today's date: {purchase_date_str}")
        return purchase_date_str

def buy_stocks(bought_stocks, symbols_to_buy, buy_sell_lock):
    print("Starting buy_stocks function...")
    global symbol, current_price, buy_signal, price_history, last_stored
    if not symbols_to_buy:
        print("No stocks to buy.")
        logging.info("No stocks to buy.")
        return
    stocks_to_remove = []
    buy_signal = 0

    # Get total equity for risk calculations
    print("Fetching account equity for risk calculations...")
    account = api.get_account()
    total_equity = float(account.equity)
    print(f"Total account equity: ${total_equity:.2f}")

    # Track open positions for portfolio risk cap (max 98% equity in open positions)
    print("Checking current portfolio exposure...")
    positions = api.list_positions()
    current_exposure = sum(float(pos.market_value) for pos in positions)
    max_new_exposure = total_equity * 0.98 - current_exposure
    if max_new_exposure <= 0:
        print("Portfolio exposure limit reached. No new buys.")
        logging.info("Portfolio exposure limit reached. No new buys.")
        return
    print(f"Current exposure: ${current_exposure:.2f}, Max new exposure: ${max_new_exposure:.2f}")

    # Track processed symbols for dynamic allocation
    processed_symbols = 0
    valid_symbols = []

    # First pass: Filter valid symbols to avoid wasting allocations
    print("Filtering valid symbols for buying...")
    for symbol in symbols_to_buy:
        current_price = get_current_price(symbol)
        if current_price is None:
            print(f"No valid price data for {symbol}. Skipping.")
            logging.info(f"No valid price data for {symbol}.")
            continue
        historical_data = calculate_technical_indicators(symbol, lookback_days=5)
        if historical_data.empty:
            print(f"No historical data for {symbol}. Skipping.")
            logging.info(f"No historical data for {symbol}.")
            continue
        valid_symbols.append(symbol)
    print(f"Valid symbols to process: {valid_symbols}")

    # Check if there are valid symbols
    if not valid_symbols:
        print("No valid symbols to buy after filtering.")
        logging.info("No valid symbols to buy after filtering.")
        return

    # Process each valid symbol
    for symbol in valid_symbols:
        print(f"Processing {symbol}...")
        processed_symbols += 1
        today_date = datetime.today().date()
        today_date_str = today_date.strftime("%Y-%m-%d")
        current_datetime = datetime.now(pytz.timezone('US/Eastern'))
        current_time_str = current_datetime.strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")

        # Fetch current data
        current_price = get_current_price(symbol)
        if current_price is None:
            print(f"No valid price data for {symbol}.")
            logging.info(f"No valid price data for {symbol}.")
            continue

        # Update price history for the symbol at specified intervals
        current_timestamp = time.time()
        if symbol not in price_history:
            price_history[symbol] = {interval: [] for interval in interval_map}
            last_stored[symbol] = {interval: 0 for interval in interval_map}
        for interval, delta in interval_map.items():
            if current_timestamp - last_stored[symbol][interval] >= delta:
                price_history[symbol][interval].append(current_price)
                last_stored[symbol][interval] = current_timestamp
                print(f"Stored price {current_price} for {symbol} at {interval} interval.")
                logging.info(f"Stored price {current_price} for {symbol} at {interval} interval.")

        # Get historical data for volume, RSI, and candlesticks
        symbol_yf = symbol.replace('.', '-')
        print(f"Fetching 5-day 5-minute historical data for {symbol_yf}...")
        stock_data = yf.Ticker(symbol_yf)
        time.sleep(2)
        historical_data = stock_data.history(period='5d', interval='5m', prepost=True)
        time.sleep(2)
        if historical_data.empty or len(historical_data) < 3:
            print(f"Insufficient historical data for {symbol} (rows: {len(historical_data)}). Skipping.")
            logging.info(f"Insufficient historical data for {symbol} (rows: {len(historical_data)}).")
            continue

        # Calculate volume decrease
        print(f"Calculating volume metrics for {symbol}...")
        recent_avg_volume = historical_data['Volume'].iloc[-5:].mean() if len(historical_data) >= 5 else 0
        prior_avg_volume = historical_data['Volume'].iloc[-10:-5].mean() if len(historical_data) >= 10 else recent_avg_volume
        volume_decrease = recent_avg_volume < prior_avg_volume if len(historical_data) >= 10 else False
        current_volume = historical_data['Volume'].iloc[-1]
        print(f"{symbol}: Recent avg volume = {recent_avg_volume:.0f}, Prior avg volume = {prior_avg_volume:.0f}, Volume decrease = {volume_decrease}")

        # Calculate RSI decrease
        print(f"Calculating RSI metrics for {symbol}...")
        close_prices = historical_data['Close'].values
        rsi_series = talib.RSI(close_prices, timeperiod=14)
        rsi_decrease = False
        latest_rsi = rsi_series[-1] if len(rsi_series) > 0 else None
        if len(rsi_series) >= 10:
            recent_rsi_values = rsi_series[-5:][~np.isnan(rsi_series[-5:])]
            prior_rsi_values = rsi_series[-10:-5][~np.isnan(rsi_series[-10:-5])]
            if len(recent_rsi_values) > 0 and len(prior_rsi_values) > 0:
                recent_avg_rsi = np.mean(recent_rsi_values)
                prior_avg_rsi = np.mean(prior_rsi_values)
                rsi_decrease = recent_avg_rsi < prior_avg_rsi
            else:
                recent_avg_rsi = 0
                prior_avg_rsi = 0
        else:
            recent_avg_rsi = 0
            prior_avg_rsi = 0
        print(f"{symbol}: Latest RSI = {latest_rsi:.2f}, Recent avg RSI = {recent_avg_rsi:.2f}, Prior avg RSI = {prior_avg_rsi:.2f}, RSI decrease = {rsi_decrease}")

        # Calculate MACD
        print(f"Calculating MACD for {symbol}...")
        short_window = 12
        long_window = 26
        signal_window = 9
        macd, macd_signal, _ = talib.MACD(close_prices, fastperiod=short_window, slowperiod=long_window, signalperiod=signal_window)
        latest_macd = macd[-1] if len(macd) > 0 else None
        latest_macd_signal = macd_signal[-1] if len(macd_signal) > 0 else None
        macd_above_signal = latest_macd > latest_macd_signal if latest_macd is not None else False
        print(f"{symbol}: MACD = {latest_macd:.2f}, Signal = {latest_macd_signal:.2f}, MACD above signal = {macd_above_signal}")

        # Check price increase (for logging)
        previous_price = get_previous_price(symbol)
        price_increase = current_price > previous_price * 1.005
        print(f"{symbol}: Price increase check: Current = ${current_price:.2f}, Previous = ${previous_price:.2f}, Increase = {price_increase}")

        # Check price drop
        print(f"Checking price drop for {symbol}...")
        last_prices = get_last_price_within_past_5_minutes([symbol])
        last_price = last_prices.get(symbol)
        if last_price is None:
            try:
                last_price = round(float(stock_data.history(period='1d')['Close'].iloc[-1].item()), 4)
                time.sleep(2)
                print(f"No price found for {symbol} in past 5 minutes. Using last closing price: {last_price}")
                logging.info(f"No price found for {symbol} in past 5 minutes. Using last closing price: {last_price}")
            except Exception as e:
                print(f"Error fetching last closing price for {symbol}: {e}")
                logging.error(f"Error fetching last closing price for {symbol}: {e}")
                continue

        price_decline_threshold = last_price * (1 - 0.002)
        price_decline = current_price <= price_decline_threshold
        print(f"{symbol}: Price decline check: Current = ${current_price:.2f}, Threshold = ${price_decline_threshold:.2f}, Decline = {price_decline}")

        # Calculate short-term price trend
        short_term_trend = None
        if symbol in price_history and '5min' in price_history[symbol] and len(price_history[symbol]['5min']) >= 2:
            recent_prices = price_history[symbol]['5min'][-2:]
            short_term_trend = 'up' if recent_prices[-1] > recent_prices[-2] else 'down'
            print(f"{symbol}: Short-term price trend (5min): {short_term_trend}")
            logging.info(f"{symbol}: Short-term price trend (5min): {short_term_trend}")

        # Detect bullish reversal candlestick patterns
        print(f"Checking for bullish reversal patterns in {symbol}...")
        open_prices = historical_data['Open'].values
        high_prices = historical_data['High'].values
        low_prices = historical_data['Low'].values
        close_prices = historical_data['Close'].values

        bullish_reversal_detected = False
        reversal_candle_index = None
        detected_patterns = []
        for i in range(-1, -21, -1):
            if len(historical_data) < abs(i):
                continue
            try:
                patterns = {
                    'Hammer': talib.CDLHAMMER(open_prices[:i+1], high_prices[:i+1], low_prices[:i+1], close_prices[:i+1])[i] != 0,
                    'Bullish Engulfing': talib.CDLENGULFING(open_prices[:i+1], high_prices[:i+1], low_prices[:i+1], close_prices[:i+1])[i] > 0,
                    'Morning Star': talib.CDLMORNINGSTAR(open_prices[:i+1], high_prices[:i+1], low_prices[:i+1], close_prices[:i+1])[i] != 0,
                    'Piercing Line': talib.CDLPIERCING(open_prices[:i+1], high_prices[:i+1], low_prices[:i+1], close_prices[:i+1])[i] != 0,
                    'Three White Soldiers': talib.CDL3WHITESOLDIERS(open_prices[:i+1], high_prices[:i+1], low_prices[:i+1], close_prices[:i+1])[i] != 0,
                    'Dragonfly Doji': talib.CDLDRAGONFLYDOJI(open_prices[:i+1], high_prices[:i+1], low_prices[:i+1], close_prices[:i+1])[i] != 0,
                    'Inverted Hammer': talib.CDLINVERTEDHAMMER(open_prices[:i+1], high_prices[:i+1], low_prices[:i+1], close_prices[:i+1])[i] != 0,
                    'Tweezer Bottom': talib.CDLMATCHINGLOW(open_prices[:i+1], high_prices[:i+1], low_prices[:i+1], close_prices[:i+1])[i] != 0,
                }
                current_detected = [name for name, detected in patterns.items() if detected]
                if current_detected:
                    bullish_reversal_detected = True
                    detected_patterns = current_detected
                    reversal_candle_index = i
                    break
            except IndexError as e:
                print(f"IndexError in candlestick pattern detection for {symbol}: {e}")
                logging.error(f"IndexError in candlestick pattern detection for {symbol}: {e}")
                continue

        if detected_patterns:
            print(f"{symbol}: Detected bullish reversal patterns at candle {reversal_candle_index}: {', '.join(detected_patterns)}")
            logging.info(f"{symbol}: Detected bullish reversal patterns at candle {reversal_candle_index}: {', '.join(detected_patterns)}")
            if symbol in price_history:
                for interval, prices in price_history[symbol].items():
                    if prices:
                        print(f"{symbol}: Price history at {interval}: {prices[-5:]}")
                        logging.info(f"{symbol}: Price history at {interval}: {prices[-5:]}")
        if price_decline:
            print(f"{symbol}: Price decline >= 0.2% detected (Current price = {current_price:.2f} <= Threshold = {price_decline_threshold:.2f})")
            logging.info(f"{symbol}: Price decline >= 0.2% detected (Current price = {current_price:.2f} <= Threshold = {price_decline_threshold:.2f})")
        if volume_decrease:
            print(f"{symbol}: Volume decrease detected (Recent avg = {recent_avg_volume:.0f} < Prior avg = {prior_avg_volume:.0f})")
            logging.info(f"{symbol}: Volume decrease detected (Recent avg = {recent_avg_volume:.0f} < Prior avg = {prior_avg_volume:.0f})")
        if rsi_decrease:
            print(f"{symbol}: RSI decrease detected (Recent avg = {recent_avg_rsi:.2f} < Prior avg = {prior_avg_rsi:.2f})")
            logging.info(f"{symbol}: RSI decrease detected (Recent avg = {recent_avg_rsi:.2f} < Prior avg = {prior_avg_rsi:.2f})")

        # Add trend filter
        if not is_in_uptrend(symbol):
            print(f"{symbol}: Not in uptrend (below 200-day SMA). Skipping.")
            logging.info(f"{symbol}: Not in uptrend. Skipping.")
            continue

        # Add multi-timeframe confirmation
        daily_rsi = get_daily_rsi(symbol)
        if daily_rsi is None or daily_rsi > 50:
            print(f"{symbol}: Daily RSI not oversold ({daily_rsi}). Skipping.")
            logging.info(f"{symbol}: Daily RSI not oversold ({daily_rsi}). Skipping.")
            continue

        # Pattern-specific buy conditions with scoring
        buy_conditions_met = False
        specific_reason = ""
        score = 0
        if bullish_reversal_detected:
            score += 2
            price_stable = True
            if symbol in price_history and '5min' in price_history[symbol] and len(price_history[symbol]['5min']) >= 2:
                recent_prices = price_history[symbol]['5min'][-2:]
                price_stable = abs(recent_prices[-1] - recent_prices[-2]) / recent_prices[-2] < 0.005
                print(f"{symbol}: Price stability check (5min): {price_stable}")
                logging.info(f"{symbol}: Price stability check (5min): {price_stable}")
                if price_stable:
                    score += 1

            if macd_above_signal:
                score += 1
            if not volume_decrease:
                score += 1
            if rsi_decrease:
                score += 1
            if price_decline:
                score += 1

            for pattern in detected_patterns:
                if pattern == 'Hammer':
                    if latest_rsi < 35 and price_decline >= (last_price * 0.003):
                        score += 1
                elif pattern == 'Bullish Engulfing':
                    if recent_avg_volume > 1.5 * prior_avg_volume:
                        score += 1
                elif pattern == 'Morning Star':
                    if latest_rsi < 40:
                        score += 1
                elif pattern == 'Piercing Line':
                    if recent_avg_rsi < 40:
                        score += 1
                elif pattern == 'Three White Soldiers':
                    if not volume_decrease:
                        score += 1
                elif pattern == 'Dragonfly Doji':
                    if latest_rsi < 30:
                        score += 1
                elif pattern == 'Inverted Hammer':
                    if rsi_decrease:
                        score += 1
                elif pattern == 'Tweezer Bottom':
                    if latest_rsi < 40:
                        score += 1

            if score >= 3:
                buy_conditions_met = True
                specific_reason = f"Score: {score}, patterns: {', '.join(detected_patterns)}"

        if not buy_conditions_met:
            print(f"{symbol}: Buy score too low ({score} < 3). Skipping.")
            logging.info(f"{symbol}: Buy score too low ({score} < 3). Skipping.")
            continue

        # Determine position sizing
        if ALL_BUY_ORDERS_ARE_1_DOLLAR:
            total_cost_for_qty = 1.00
            qty = round(total_cost_for_qty / current_price, 4)
            print(f"{symbol}: Using $1.00 fractional share order mode. Qty = {qty:.4f}")
        else:
            # Volatility-based position sizing
            print(f"Calculating position size for {symbol}...")
            atr = get_average_true_range(symbol)
            if atr is None:
                print(f"No ATR for {symbol}. Skipping.")
                continue
            stop_loss_distance = 2 * atr
            risk_per_share = stop_loss_distance
            risk_amount = 0.01 * total_equity
            qty = risk_amount / risk_per_share if risk_per_share > 0 else 0
            total_cost_for_qty = qty * current_price

            # Cap by available cash and portfolio exposure
            with buy_sell_lock:
                cash_available = round(float(api.get_account().cash), 2)
                print(f"Cash available for {symbol}: ${cash_available:.2f}")
                total_cost_for_qty = min(total_cost_for_qty, cash_available - 1.00, max_new_exposure)
                if total_cost_for_qty < 1.00:
                    print(f"Insufficient risk-adjusted allocation for {symbol}.")
                    continue
                qty = round(total_cost_for_qty / current_price, 4)

            # Estimate slippage
            estimated_slippage = total_cost_for_qty * 0.001
            total_cost_for_qty -= estimated_slippage
            qty = round(total_cost_for_qty / current_price, 4)
            print(f"{symbol}: Adjusted for slippage (0.1%): Notional = ${total_cost_for_qty:.2f}, Qty = {qty:.4f}")

        # Unified cash checks
        with buy_sell_lock:
            cash_available = round(float(api.get_account().cash), 2)
        if total_cost_for_qty < 1.00:
            print(f"Order amount for {symbol} is ${total_cost_for_qty:.2f}, below minimum $1.00")
            logging.info(f"{current_time_str} Did not buy {symbol} due to order amount below $1.00")
            continue
        if cash_available < total_cost_for_qty + 1.00:
            print(f"Insufficient cash for {symbol}. Available: ${cash_available:.2f}, Required: ${total_cost_for_qty:.2f} + $1.00 minimum")
            logging.info(f"{current_time_str} Did not buy {symbol} due to insufficient cash")
            continue

        if buy_conditions_met:
            buy_signal = 1
            api_symbol = symbol.replace('-', '.')
            reason = f"bullish reversal ({', '.join(detected_patterns)}), {specific_reason}"
            print(f"Submitting buy order for {symbol}...")
            try:
                buy_order = api.submit_order(
                    symbol=api_symbol,
                    notional=total_cost_for_qty,
                    side='buy',
                    type='market',
                    time_in_force='day'
                )
                print(f"{current_time_str}, Submitted buy order for {qty:.4f} shares of {api_symbol} at {current_price:.2f} (notional: ${total_cost_for_qty:.2f}) due to {reason}")
                logging.info(f"{current_time_str} Submitted buy {qty:.4f} shares of {api_symbol} due to {reason}. RSI Decrease={rsi_decrease}, Volume Decrease={volume_decrease}, Bullish Reversal={bullish_reversal_detected}, Price Decline >= 0.2%={price_decline}")

                order_filled = False
                filled_qty = 0
                filled_price = current_price
                for _ in range(30):
                    print(f"Checking order status for {api_symbol}...")
                    order_status = api.get_order(buy_order.id)
                    if order_status.status == 'filled':
                        order_filled = True
                        filled_qty = float(order_status.filled_qty)
                        filled_price = float(order_status.filled_avg_price or current_price)
                        with buy_sell_lock:
                            cash_available = round(float(api.get_account().cash), 2)
                        actual_cost = filled_qty * filled_price
                        print(f"Order filled for {filled_qty:.4f} shares of {api_symbol} at ${filled_price:.2f}, actual cost: ${actual_cost:.2f}")
                        logging.info(f"Order filled for {filled_qty:.4f} shares of {api_symbol}, actual cost: ${actual_cost:.2f}")
                        break
                    time.sleep(2)

                if order_filled:
                    print(f"Logging trade for {api_symbol}...")
                    with open(csv_filename, mode='a', newline='') as csv_file:
                        csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                        csv_writer.writerow({
                            'Date': current_time_str,
                            'Buy': 'Buy',
                            'Quantity': filled_qty,
                            'Symbol': api_symbol,
                            'Price Per Share': filled_price
                        })
                    stocks_to_remove.append((api_symbol, filled_price, today_date_str))
                    if api.get_account().daytrade_count < 3 and not ALL_BUY_ORDERS_ARE_1_DOLLAR:
                        stop_order_id = place_trailing_stop_sell_order(api_symbol, filled_qty, filled_price)
                        if stop_order_id:
                            print(f"Trailing stop sell order placed for {api_symbol} with ID: {stop_order_id}")
                        else:
                            print(f"Failed to place trailing stop sell order for {api_symbol}")
                else:
                    print(f"Buy order not filled for {api_symbol}")
                    logging.info(f"{current_time_str} Buy order not filled for {api_symbol}")

            except tradeapi.rest.APIError as e:
                print(f"Error submitting buy order for {api_symbol}: {e}")
                logging.error(f"Error submitting buy order for {api_symbol}: {e}")
                continue

        else:
            print(f"{symbol}: Conditions not met for any detected patterns. Bullish Reversal = {bullish_reversal_detected}, Volume Decrease = {volume_decrease}, RSI Decrease = {rsi_decrease}, Price Decline >= 0.2% = {price_decline}, Price Stable = {price_stable}")
            logging.info(f"{current_time_str} Did not buy {symbol} due to Bullish Reversal = {bullish_reversal_detected}, Volume Decrease = {volume_decrease}, RSI Decrease = {rsi_decrease}, Price Decline >= 0.2% = {price_decline}, Price Stable = {price_stable}")

        update_previous_price(symbol, current_price)
        time.sleep(0.8)

    try:
        with buy_sell_lock:
            print("Updating database with buy transactions...")
            for symbol, price, date in stocks_to_remove:
                bought_stocks[symbol] = (round(price, 4), date)
                stocks_to_buy.remove(symbol.replace('.', '-'))
                remove_symbol_from_trade_list(symbol.replace('.', '-'))
                trade_history = TradeHistory(
                    symbol=symbol,
                    action='buy',
                    quantity=filled_qty,
                    price=price,
                    date=date
                )
                session.add(trade_history)
                db_position = Position(
                    symbol=symbol,
                    quantity=filled_qty,
                    avg_price=price,
                    purchase_date=date
                )
                session.add(db_position)
            session.commit()
            print("Database updated successfully.")
            refresh_after_buy()

    except SQLAlchemyError as e:
        session.rollback()
        print(f"Database error: {str(e)}")
        logging.error(f"Database error: {str(e)}")

def refresh_after_buy():
    global stocks_to_buy, bought_stocks
    print("Refreshing after buy operation...")
    time.sleep(2)
    stocks_to_buy = get_stocks_to_trade()
    bought_stocks = update_bought_stocks_from_api()
    print("Refresh complete.")

def place_trailing_stop_sell_order(symbol, qty, current_price):
    print(f"Attempting to place trailing stop sell order for {symbol}...")
    try:
        if qty != int(qty):
            print(f"Skipping trailing stop sell order for {symbol}: Fractional share quantity {qty:.4f} detected.")
            logging.error(f"Skipped trailing stop sell order for {symbol}: Fractional quantity {qty:.4f} not allowed.")
            return None

        stop_loss_percent = 1.0
        stop_loss_price = current_price * (1 - stop_loss_percent / 100)
        print(f"Placing trailing stop sell order for {qty} shares of {symbol} "
              f"with trail percent {stop_loss_percent}% (initial stop price: {stop_price:.2f})")

        stop_order = api.submit_order(
            symbol=symbol,
            qty=int(qty),
            side='sell',
            type='trailing_stop',
            trail_percent=str(stop_loss_percent),
            time_in_force='gtc'
        )

        print(f"Successfully placed trailing stop sell order for {qty} shares of {symbol} "
              f"with ID: {stop_order.id}")
        logging.info(f"Placed trailing stop sell order for {qty} shares of {symbol} with ID: {stop_order.id}")

        return stop_order.id

    except Exception as e:
        print(f"Error placing trailing stop sell order for {symbol}: {str(e)}")
        logging.error(f"Error placing trailing stop sell order for {symbol}: {str(e)}")
        return None

def update_bought_stocks_from_api():
    print("Updating bought stocks from Alpaca API...")
    positions = api.list_positions()
    bought_stocks = {}

    for position in positions:
        symbol = position.symbol
        avg_entry_price = float(position.avg_entry_price)
        quantity = float(position.qty)

        purchase_date_str = get_most_recent_purchase_date(symbol)

        try:
            db_position = session.query(Position).filter_by(symbol=symbol).one()
            db_position.quantity = quantity
            db_position.avg_price = avg_entry_price
            db_position.purchase_date = purchase_date_str
        except NoResultFound:
            db_position = Position(
                symbol=symbol,
                quantity=quantity,
                avg_price=avg_entry_price,
                purchase_date=purchase_date_str
            )
            session.add(db_position)

        bought_stocks[symbol] = (avg_entry_price, purchase_date_str)

    session.commit()
    print(f"Updated {len(bought_stocks)} bought stocks from API.")
    return bought_stocks

def sell_stocks(bought_stocks, buy_sell_lock):
    print("Starting sell_stocks function...")
    stocks_to_remove = []
    now = datetime.now(pytz.timezone('US/Eastern'))
    current_time_str = now.strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")
    today_date_str = datetime.today().date().strftime("%Y-%m-%d")
    comparison_date = datetime.today().date()

    for symbol, (bought_price, purchase_date) in bought_stocks.items():
        status_printer_sell_stocks()
        
        try:
            bought_date = datetime.strptime(purchase_date, "%Y-%m-%d").date()
        except (ValueError, TypeError) as e:
            print(f"Error parsing purchase_date for {symbol}: {purchase_date}. Skipping. Error: {e}")
            logging.error(f"Error parsing purchase_date for {symbol}: {purchase_date}. Error: {e}")
            continue

        print(f"Checking {symbol}: Purchase date = {bought_date}, Comparison date = {comparison_date}")
        logging.info(f"Checking {symbol}: Purchase date = {bought_date}, Comparison date = {comparison_date}")

        if bought_date <= comparison_date:
            current_price = get_current_price(symbol)
            if current_price is None:
                print(f"Skipping {symbol}: Could not retrieve current price.")
                logging.error(f"Skipping {symbol}: Could not retrieve current price.")
                continue

            try:
                position = api.get_position(symbol)
                bought_price = float(position.avg_entry_price)
                qty = float(position.qty)

                open_orders = api.list_orders(status='open')
                symbol_open_orders = [order for order in open_orders if order.symbol == symbol]
                
                print(f"{symbol}: Found {len(symbol_open_orders)} open orders.")
                logging.info(f"{symbol}: Found {len(symbol_open_orders)} open orders.")
                
                if symbol_open_orders:
                    print(f"There is an open sell order for {symbol}. Skipping sell order.")
                    logging.info(f"{current_time_str} Skipped sell for {symbol} due to existing open order.")
                    continue

                sell_threshold = bought_price * 1.005
                print(f"{symbol}: Current price = {current_price:.2f}, Bought price = {bought_price:.2f}, Sell threshold = {sell_threshold:.2f}")
                logging.info(f"{symbol}: Current price = {current_price:.2f}, Bought price = {bought_price:.2f}, Sell threshold = {sell_threshold:.2f}")

                if current_price >= sell_threshold:
                    print(f"Submitting sell order for {symbol}...")
                    api.submit_order(
                        symbol=symbol,
                        qty=qty,
                        side='sell',
                        type='market',
                        time_in_force='day'
                    )
                    print(f" {current_time_str}, Sold {qty:.4f} shares of {symbol} at {current_price:.2f} based on a higher selling price.")
                    logging.info(f"{current_time_str} Sold {qty:.4f} shares of {symbol} at {current_price:.2f}.")
                    
                    with open(csv_filename, mode='a', newline='') as csv_file:
                        csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                        csv_writer.writerow({
                            'Date': current_time_str,
                            'Sell': 'Sell',
                            'Quantity': qty,
                            'Symbol': symbol,
                            'Price Per Share': current_price
                        })
                    stocks_to_remove.append((symbol, qty, current_price))
                    time.sleep(2)
                else:
                    print(f"{symbol}: Price condition not met. Current price ({current_price:.2f}) < Sell threshold ({sell_threshold:.2f})")
                    logging.info(f"{symbol}: Price condition not met. Current price ({current_price:.2f}) < Sell threshold ({sell_threshold:.2f})")
                time.sleep(2)
            except Exception as e:
                print(f"Error processing sell for {symbol}: {e}")
                logging.error(f"Error processing sell for {symbol}: {e}")
        else:
            print(f"{symbol}: Not eligible for sale. Purchase date ({bought_date}) is not on or before comparison date ({comparison_date})")
            logging.info(f"{symbol}: Not eligible for sale. Purchase date ({bought_date}) is not on or before comparison date ({comparison_date})")

    try:
        with buy_sell_lock:
            print("Updating database with sell transactions...")
            for symbol, qty, current_price in stocks_to_remove:
                del bought_stocks[symbol]
                trade_history = TradeHistory(
                    symbol=symbol,
                    action='sell',
                    quantity=qty,
                    price=current_price,
                    date=today_date_str
                )
                session.add(trade_history)
                session.query(Position).filter_by(symbol=symbol).delete()
            session.commit()
            print("Database updated successfully.")
            refresh_after_sell()
    except SQLAlchemyError as e:
        session.rollback()
        print(f"Database error: {str(e)}")
        logging.error(f"Database error: {str(e)}")

def refresh_after_sell():
    global bought_stocks
    print("Refreshing after sell operation...")
    time.sleep(2)
    bought_stocks = update_bought_stocks_from_api()
    print("Refresh complete.")

def load_positions_from_database():
    print("Loading positions from database...")
    positions = session.query(Position).all()
    bought_stocks = {}
    for position in positions:
        symbol = position.symbol
        avg_price = position.avg_price
        purchase_date = position.purchase_date
        bought_stocks[symbol] = (avg_price, purchase_date)
    print(f"Loaded {len(bought_stocks)} positions from database.")
    return bought_stocks

def main():
    global stocks_to_buy
    print("Starting main trading program...")
    stocks_to_buy = get_stocks_to_trade()
    bought_stocks = load_positions_from_database()
    buy_sell_lock = threading.Lock()

    while True:
        try:
            stop_if_stock_market_is_closed()
            current_datetime = datetime.now(pytz.timezone('US/Eastern'))
            current_time_str = current_datetime.strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")

            cash_balance = round(float(api.get_account().cash), 2)
            print("------------------------------------------------------------------------------------")
            print("\n")
            print("*****************************************************")
            print("******** Billionaire Buying Strategy Version ********")
            print("*****************************************************")
            print("2025 Edition of the Advanced Stock Market Trading Robot, Version 8 ")
            print("by https://github.com/CodeProSpecialist")
            print("------------------------------------------------------------------------------------")
            print(f" {current_time_str} Cash Balance: ${cash_balance}")
            day_trade_count = api.get_account().daytrade_count
            print("\n")
            print(f"Current day trade number: {day_trade_count} out of 3 in 5 business days")
            print("\n")
            print("------------------------------------------------------------------------------------")
            print("\n")

            stocks_to_buy = get_stocks_to_trade()

            if not bought_stocks:
                bought_stocks = update_bought_stocks_from_api()

            print("Starting buy and sell threads...")
            buy_thread = threading.Thread(target=buy_stocks, args=(bought_stocks, stocks_to_buy, buy_sell_lock))
            sell_thread = threading.Thread(target=sell_stocks, args=(bought_stocks, buy_sell_lock))

            buy_thread.start()
            sell_thread.start()

            buy_thread.join()
            sell_thread.join()
            print("Buy and sell threads completed.")

            if PRINT_STOCKS_TO_BUY:
                print("\n")
                print("------------------------------------------------------------------------------------")
                print("\n")
                print("Stocks to Purchase:")
                print("\n")
                for symbol in stocks_to_buy:
                    current_price = get_current_price(symbol)
                    print(f"Symbol: {symbol} | Current Price: {current_price} ")
                    time.sleep(1)
                print("\n")
                print("------------------------------------------------------------------------------------")
                print("\n")

            if PRINT_ROBOT_STORED_BUY_AND_SELL_LIST_DATABASE:
                print_database_tables()

            if DEBUG:
                print("\n")
                print("------------------------------------------------------------------------------------")
                print("\n")
                print("Stocks to Purchase:")
                print("\n")
                for symbol in stocks_to_buy:
                    current_price = get_current_price(symbol)
                    atr_low_price = get_atr_low_price(symbol)
                    print(f"Symbol: {symbol} | Current Price: {current_price} | ATR low buy signal price: {atr_low_price}")
                print("\n")
                print("------------------------------------------------------------------------------------")
                print("\n")
                print("\nStocks to Sell:")
                print("\n")
                for symbol, _ in bought_stocks.items():
                    current_price = get_current_price(symbol)
                    atr_high_price = get_atr_high_price(symbol)
                    print(f"Symbol: {symbol} | Current Price: {current_price} | ATR high sell signal profit price: {atr_high_price}")
                print("\n")

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
    except Exception as e:
        logging.error(f"Error encountered: {e}")
        print(f"Critical error: {e}")
        session.close()
