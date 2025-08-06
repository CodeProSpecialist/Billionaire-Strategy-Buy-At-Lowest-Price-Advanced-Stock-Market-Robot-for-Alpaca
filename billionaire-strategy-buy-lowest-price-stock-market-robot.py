import threading
import logging
import csv
import os, sys
import time
import schedule
from datetime import datetime, timedelta, date
from datetime import time as time2
import alpaca_trade_api as tradeapi
import pytz
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

global stocks_to_buy, today_date, today_datetime, csv_writer, csv_filename, fieldnames, price_changes, end_time
global current_price, today_date_str, qty

# the below will print the list of stocks to buy and their prices when True.
PRINT_STOCKS_TO_BUY = False  # keep this as False for the robot to work faster.

# the below will print the Robot's personal buy and sell database when True.
PRINT_ROBOT_STORED_BUY_AND_SELL_LIST_DATABASE = True  # keep this as False for the robot to work faster.

# the below will print the stocks to sell when True.
PRINT_DATABASE = True  # keep this as True to view the stocks to sell. False for a faster robot.

# the below will print the Average True Range of stock prices when True.
DEBUG = False  # this robot works faster when this is False.

# the below Permission variable will allow all owned position shares to sell today when True on the first run.
POSITION_DATES_AS_YESTERDAY_OPTION = False  # keep this as False to not change the dates of owned stocks

# set the timezone to Eastern
eastern = pytz.timezone('US/Eastern')

# Dictionary to maintain previous prices and price increase and decrease counts
stock_data = {}

# Dictionary to store previous prices for symbols
previous_prices = {}

end_time = 0  # Initialize end_time as a global variable

# Define the API datetime format
api_time_format = '%Y-%m-%dT%H:%M:%S.%f-04:00'

# the below variable was recommended by Artificial Intelligence
buy_sell_lock = threading.Lock()
yf_lock = threading.Lock()  # Added for thread-safe yfinance calls

logging.basicConfig(filename='trading-bot-program-logging-messages.txt', level=logging.INFO)

# Define the CSV file and fieldnames
csv_filename = 'log-file-of-buy-and-sell-signals.csv'
fieldnames = ['Date', 'Buy', 'Sell', 'Quantity', 'Symbol', 'Price Per Share']

# Open the CSV file for writing and set up a CSV writer
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
    quantity = Column(Integer)
    price = Column(Float)
    date = Column(String)

class Position(Base):
    __tablename__ = 'positions'
    symbol = Column(String, primary_key=True)
    quantity = Column(Integer)
    avg_price = Column(Float)
    purchase_date = Column(String)

# Initialize SQLAlchemy
engine = create_engine('sqlite:///trading_bot.db')
Session = sessionmaker(bind=engine)
session = Session()

# Create tables if not exist
Base.metadata.create_all(engine)

def stop_if_stock_market_is_closed():
    market_open_time = time2(9, 30)
    market_close_time = time2(16, 0)

    while True:
        eastern = pytz.timezone('US/Eastern')
        now = datetime.now(eastern)
        current_time = now.time()

        if now.weekday() <= 4 and market_open_time <= current_time <= market_close_time:
            break

        print("\n")
        print('''
        *********************************************************************************
        ************ Billionaire Buying Strategy Version ********************************
        *********************************************************************************
            2023 Edition of the Advanced Stock Market Trading Robot, Version 2 

                                https://github.com/CodeProSpecialist
                       Featuring an An Accelerated Database Engine with Python 3 SQLAlchemy  
         ''')
        print(f'Current date & time (Eastern Time): {now.strftime("%A, %B %d, %Y, %I:%M:%S %p")}')
        print("Stockbot only works Monday through Friday: 9:30 am - 4:00 pm Eastern Time.")
        print("Waiting until Stock Market Hours to begin the Stockbot Trading Program.")
        print("\n")
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
            print(f"{record.symbol} | {record.action} | {record.quantity} | {record.price:.2f} | {record.date}")

        print("----------------------------------------------------------------")
        print("\n")
        print("Positions in the Database To Sell 1 or More Days After the Date Shown:")
        print("\n")
        print("Stock | Quantity | Avg. Price | Date or The 1st Day This Robot Began Working ")
        print("\n")
        for record in session.query(Position).all():
            symbol, quantity, avg_price, purchase_date = record.symbol, record.quantity, record.avg_price, record.purchase_date
            purchase_date_str = purchase_date

            if show_price_percentage_change:
                current_price = get_current_price(symbol)
                percentage_change = ((current_price - avg_price) / avg_price) * 100
                print(f"{symbol} | {quantity} | {avg_price:.2f} | {purchase_date_str} | Price Change: {percentage_change:.2f}%")
            else:
                print(f"{symbol} | {quantity} | {avg_price:.2f} | {purchase_date_str}")
        print("\n")

def get_stocks_to_trade():
    try:
        with open('electricity-or-utility-stocks-to-buy-list.txt', 'r') as file:
            stock_symbols = [line.strip() for line in file.readlines()]

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
    with open('electricity-or-utility-stocks-to-buy-list.txt', 'r') as file:
        lines = file.readlines()
    with open('electricity-or-utility-stocks-to-buy-list.txt', 'w') as file:
        for line in lines:
            if line.strip() != symbol:
                file.write(line)

def get_opening_price(symbol):
    symbol = symbol.replace('.', '-')
    stock_data = yf.Ticker(symbol)
    try:
        opening_price = round(float(stock_data.history(period="1d")["Open"].iloc[0].item()), 4)  # Use .item()
        return opening_price
    except IndexError:
        logging.error(f"Opening price not found for {symbol}.")
        return None

def get_current_price(symbol):
    with yf_lock:
        symbol = symbol.replace('.', '-')
        eastern = pytz.timezone('US/Eastern')
        now = datetime.now(eastern)
        pre_market_start = time2(4, 0)
        pre_market_end = time2(9, 30)
        market_start = time2(9, 30)
        market_end = time2(16, 0)
        post_market_start = time2(16, 0)
        post_market_end = time2(20, 0)
        stock_data = yf.Ticker(symbol)
        time.sleep(1.5)  # Add 1.5-second delay for yfinance lookup
        try:
            if pre_market_start <= now.time() < market_start:
                data = stock_data.history(start=now.strftime('%Y-%m-%d'), interval='1m', prepost=True)
                time.sleep(1.5)  # Additional delay for this specific lookup
                if not data.empty:
                    data.index = data.index.tz_convert(eastern)
                    pre_market_data = data.between_time(pre_market_start, pre_market_end)
                    current_price = float(pre_market_data['Close'].iloc[-1].item()) if not pre_market_data.empty else None
                    if current_price is None:
                        logging.error("Pre-market: Current Price not found, using last closing price.")
                        print("Pre-market: Current Price not found, using last closing price.")
                        last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                        time.sleep(1.5)  # Delay for last closing price lookup
                        current_price = last_close
                else:
                    current_price = None
                    logging.error("Pre-market: Current Price not found, using last closing price.")
                    print("Pre-market: Current Price not found, using last closing price.")
                    last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                    time.sleep(1.5)  # Delay for last closing price lookup
                    current_price = last_close
            elif market_start <= now.time() < market_end:
                data = stock_data.history(period='1d', interval='1m')
                time.sleep(1.5)  # Additional delay for this specific lookup
                if not data.empty:
                    data.index = data.index.tz_convert(eastern)
                    current_price = float(data['Close'].iloc[-1].item()) if not data.empty else None
                    if current_price is None:
                        logging.error("Market hours: Current Price not found, using last closing price.")
                        print("Market hours: Current Price not found, using last closing price.")
                        last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                        time.sleep(1.5)  # Delay for last closing price lookup
                        current_price = last_close
                else:
                    current_price = None
                    logging.error("Market hours: Current Price not found, using last closing price.")
                    print("Market hours: Current Price not found, using last closing price.")
                    last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                    time.sleep(1.5)  # Delay for last closing price lookup
                    current_price = last_close
            elif market_end <= now.time() < post_market_end:
                data = stock_data.history(start=now.strftime('%Y-%m-%d'), interval='1m', prepost=True)
                time.sleep(1.5)  # Additional delay for this specific lookup
                if not data.empty:
                    data.index = data.index.tz_convert(eastern)
                    post_market_data = data.between_time(post_market_start, post_market_end)
                    current_price = float(post_market_data['Close'].iloc[-1].item()) if not post_market_data.empty else None
                    if current_price is None:
                        logging.error("Post-market: Current Price not found, using last closing price.")
                        print("Post-market: Current Price not found, using last closing price.")
                        last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                        time.sleep(1.5)  # Delay for last closing price lookup
                        current_price = last_close
                else:
                    current_price = None
                    logging.error("Post-market: Current Price not found, using last closing price.")
                    print("Post-market: Current Price not found, using last closing price.")
                    last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                    time.sleep(1.5)  # Delay for last closing price lookup
                    current_price = last_close
            else:
                last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                time.sleep(1.5)  # Delay for last closing price lookup
                current_price = last_close
        except Exception as e:
            logging.error(f"Error fetching current price for {symbol}: {e}")
            print(f"Error fetching current price for {symbol}: {e}")
            try:
                last_close = float(stock_data.history(period='1d')['Close'].iloc[-1].item())
                time.sleep(1.5)  # Delay for last closing price lookup
                current_price = last_close
            except Exception as e2:
                logging.error(f"Error fetching last closing price for {symbol}: {e2}")
                print(f"Error fetching last closing price for {symbol}: {e2}")
                current_price = None

        if current_price is None:
            error_message = f"Failed to retrieve current price for {symbol}."
            logging.error(error_message)
            print(error_message)

        return round(current_price, 4) if current_price is not None else None

def get_atr_high_price(symbol):
    symbol = symbol.replace('.', '-')
    atr_value = get_average_true_range(symbol)
    current_price = get_current_price(symbol)
    return round(current_price + 0.40 * atr_value, 4)

def get_atr_low_price(symbol):
    symbol = symbol.replace('.', '-')
    atr_value = get_average_true_range(symbol)
    current_price = get_current_price(symbol)
    return round(current_price - 0.10 * atr_value, 4)

def get_average_true_range(symbol):
    symbol = symbol.replace('.', '-')
    ticker = yf.Ticker(symbol)
    data = ticker.history(period='30d')
    atr = talib.ATR(data['High'].values, data['Low'].values, data['Close'].values, timeperiod=22)
    return atr[-1]

def status_printer_buy_stocks():
    print(f"\rBuy stocks function is working correctly right now. Checking stocks to buy.....", end='', flush=True)
    print()

def status_printer_sell_stocks():
    print(f"\rSell stocks function is working correctly right now. Checking stocks to sell.....", end='', flush=True)
    print()

def calculate_technical_indicators(symbol, lookback_days=90):
    symbol = symbol.replace('.', '-')
    stock_data = yf.Ticker(symbol)
    historical_data = stock_data.history(period=f'{lookback_days}d')
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
    return historical_data

def print_technical_indicators(symbol, historical_data):
    print("")
    print(f"\nTechnical Indicators for {symbol}:\n")
    print(historical_data[['Close', 'macd', 'signal', 'rsi', 'volume']].tail())
    print("")

def calculate_cash_on_hand():
    cash_available = round(float(api.get_account().cash), 2)
    return cash_available

def calculate_total_symbols(stocks_to_buy):
    total_symbols = len(stocks_to_buy)
    return total_symbols

def allocate_cash_equally(cash_available, total_symbols):
    max_allocation_per_symbol = 600
    allocation_per_symbol = min(max_allocation_per_symbol, cash_available) / total_symbols
    return allocation_per_symbol

def get_previous_price(symbol):
    if symbol in previous_prices:
        return previous_prices[symbol]
    else:
        current_price = get_current_price(symbol)
        previous_prices[symbol] = current_price
        print(f"No previous price for {symbol} was found. Using the current price as the previous price: {current_price}")
        return current_price

def update_previous_price(symbol, current_price):
    previous_prices[symbol] = current_price

def run_schedule():
    while not end_time_reached():
        schedule.run_pending()
        time.sleep(1)

def track_price_changes(symbol):
    current_price = get_current_price(symbol)
    previous_price = get_previous_price(symbol)

    print("")
    print_technical_indicators(symbol, calculate_technical_indicators(symbol))
    print("")

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
    return time.time() >= end_time

def get_last_price_within_past_5_minutes(symbols_to_buy):
    results = {}
    eastern = pytz.timezone('US/Eastern')
    end_time = datetime.now(eastern)
    start_time = end_time - timedelta(minutes=5)

    for symbol in symbols_to_buy:
        try:
            symbol = symbol.replace('.', '-')
            data = yf.download(symbol, start=start_time, end=end_time, interval='1m', prepost=True)
            time.sleep(1)
            if not data.empty:
                last_price = round(float(data['Close'].iloc[-1].item()), 2)  # Use .item()
                results[symbol] = last_price
            else:
                results[symbol] = None
        except Exception as e:
            print(f"Error occurred while fetching data for {symbol}: {e}")
            logging.error(f"Error occurred while fetching data for {symbol}: {e}")
            results[symbol] = None

    return results

def buy_stocks(bought_stocks, symbols_to_buy, buy_sell_lock):
    global symbol, current_price, qty_of_one_stock, buy_signal
    if not symbols_to_buy:
        print("No stocks to buy.")
        logging.info("No stocks to buy.")
        return
    stocks_to_remove = []
    buy_signal = 0

    for symbol in symbols_to_buy:
        today_date = datetime.today().date()
        today_date_str = today_date.strftime("%Y-%m-%d")
        opening_price = get_opening_price(symbol)
        last_prices = get_last_price_within_past_5_minutes([symbol])

        if last_prices is not None and symbol in last_prices:
            current_price = get_current_price(symbol)
            cash_available = round(float(api.get_account().cash), 2)
            qty_of_one_stock = 1
            now = datetime.now(pytz.timezone('US/Eastern'))
            current_time_str = now.strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")

            # Check if last price is valid (not None)
            last_price = last_prices.get(symbol)
            if last_price is None:
                # Fallback to last closing price if no price within past 5 minutes
                try:
                    symbol_for_yf = symbol.replace('.', '-')
                    stock_data = yf.Ticker(symbol_for_yf)
                    last_price = round(float(stock_data.history(period='1d')['Close'].iloc[-1].item()), 4)  # Use .item()
                    print(f"No price found for {symbol} in past 5 minutes. Using last closing price: {last_price}")
                    logging.info(f"No price found for {symbol} in past 5 minutes. Using last closing price: {last_price}")
                except Exception as e:
                    print(f"Error fetching last closing price for {symbol}: {e}")
                    logging.error(f"Error fetching last closing price for {symbol}: {e}")
                    continue  # Skip to next symbol if no price is available

            if last_price is not None:
                total_cost_for_qty = current_price * qty_of_one_stock
                factor_to_subtract = 0.998
                starting_price_to_compare = round(float(last_price) * factor_to_subtract, 2)  # Ensure float

                print(f"{symbol}: Current price = ${current_price:.2f}, Starting price to compare = ${starting_price_to_compare:.2f}")
                status_printer_buy_stocks()

                if cash_available >= total_cost_for_qty and current_price <= starting_price_to_compare:
                    buy_signal = 1
                    api_symbol = symbol.replace('-', '.')
                    buy_order = api.submit_order(
                        symbol=api_symbol,
                        qty=qty_of_one_stock,
                        side='buy',
                        type='market',
                        time_in_force='day'
                    )
                    print(f"Last price for {api_symbol} did decrease within the past 6 minutes or the conditions to buy shares were favorable.")
                    print(f" {current_time_str}, Bought {qty_of_one_stock} shares of {api_symbol} at {current_price}")
                    logging.info(f"{current_time_str} Buy {qty_of_one_stock} shares of {api_symbol}.")

                    with open(csv_filename, mode='a', newline='') as csv_file:
                        csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                        csv_writer.writerow({
                            'Date': current_time_str,
                            'Buy': 'Buy',
                            'Quantity': qty_of_one_stock,
                            'Symbol': api_symbol,
                            'Price Per Share': current_price
                        })

                    stocks_to_remove.append((api_symbol, current_price, today_date_str))

                    order_filled = False
                    for _ in range(30):
                        order_status = api.get_order(buy_order.id)
                        if order_status.status == 'filled':
                            order_filled = True
                            break
                        time.sleep(2)

                    if order_filled and api.get_account().daytrade_count < 3:
                        stop_order_id = place_trailing_stop_sell_order(api_symbol, qty_of_one_stock, current_price)
                        if stop_order_id:
                            print(f"Trailing stop sell order placed for {api_symbol} with ID: {stop_order_id}")
                        else:
                            print(f"Failed to place trailing stop sell order for {api_symbol}")
                    else:
                        print(f"Buy order not filled or day trade limit reached for {api_symbol}")

                else:
                    print(f"Last price for {symbol} did not decrease within the past 6 minutes or the conditions to buy shares were not favorable.")
                    logging.info(f"{current_time_str} Did not buy {symbol} due to insufficient cash or unfavorable price movement.")

                time.sleep(0.8)
            else:
                print(f"No valid price data for {symbol}.")
                logging.info(f"No valid price data for {symbol}.")

            time.sleep(0.5)

    try:
        with buy_sell_lock:
            for symbol, price, date in stocks_to_remove:
                bought_stocks[symbol] = (round(price, 4), date)
                stocks_to_buy.remove(symbol.replace('.', '-'))
                remove_symbol_from_trade_list(symbol.replace('.', '-'))
                trade_history = TradeHistory(
                    symbol=symbol,
                    action='buy',
                    quantity=qty_of_one_stock,
                    price=price,
                    date=date
                )
                session.add(trade_history)
                db_position = Position(
                    symbol=symbol,
                    quantity=qty_of_one_stock,
                    avg_price=price,
                    purchase_date=date
                )
                session.add(db_position)
            session.commit()
            refresh_after_buy()

    except SQLAlchemyError as e:
        session.rollback()
        print(f"Database error: {str(e)}")
        logging.error(f"Database error: {str(e)}")

def refresh_after_buy():
    global stocks_to_buy, bought_stocks
    time.sleep(2)
    stocks_to_buy = get_stocks_to_trade()
    bought_stocks = update_bought_stocks_from_api()

def place_trailing_stop_sell_order(symbol, qty_of_one_stock, current_price):
    try:
        stop_loss_percent = 1.0
        stop_loss_price = current_price * (1 - stop_loss_percent / 100)
        print(f"Attempting to place trailing stop sell order for {qty_of_one_stock} shares of {symbol} "
              f"with trail percent {stop_loss_percent}% (initial stop price: {stop_loss_price})")

        stop_order = api.submit_order(
            symbol=symbol,
            qty=qty_of_one_stock,
            side='sell',
            type='trailing_stop',
            trail_percent=stop_loss_percent,
            time_in_force='gtc'
        )

        print(f"Successfully placed trailing stop sell order for {qty_of_one_stock} shares of {symbol} "
              f"with ID: {stop_order.id}")
        logging.info(f"Placed trailing stop sell order for {qty_of_one_stock} shares of {symbol} with ID: {stop_order.id}")

        return stop_order.id

    except Exception as e:
        print(f"Error placing trailing stop sell order for {symbol}: {str(e)}")
        logging.error(f"Error placing trailing stop sell order for {symbol}: {str(e)}")
        return None

def update_bought_stocks_from_api():
    positions = api.list_positions()
    bought_stocks = {}

    yesterday = datetime.today() - timedelta(days=1)
    run_counter_file = "trading_bot_run_counter.txt"

    if not os.path.exists(run_counter_file):
        with open(run_counter_file, "w") as f:
            f.write("0")
        run_counter = 0
    else:
        with open(run_counter_file, "r") as f:
            run_counter = int(f.read())
        run_counter += 1

    for position in positions:
        symbol = position.symbol
        avg_entry_price = float(position.avg_entry_price)

        try:
            db_position = session.query(Position).filter_by(symbol=symbol).one()
            db_position.quantity = position.qty
            db_position.avg_price = avg_entry_price

            if POSITION_DATES_AS_YESTERDAY_OPTION and run_counter < 1:
                db_position.purchase_date = yesterday.strftime("%Y-%m-%d")
        except NoResultFound:
            purchase_date = yesterday if POSITION_DATES_AS_YESTERDAY_OPTION and run_counter < 1 else datetime.today()
            purchase_date_str = purchase_date.strftime("%Y-%m-%d")
            db_position = Position(symbol=symbol, quantity=position.qty, avg_price=avg_entry_price,
                                   purchase_date=purchase_date_str)
            session.add(db_position)

        bought_stocks[symbol] = (avg_entry_price, db_position.purchase_date)

    with open(run_counter_file, "w") as f:
        f.write(str(run_counter))

    session.commit()
    return bought_stocks

def sell_stocks(bought_stocks, buy_sell_lock):
    stocks_to_remove = []
    now = datetime.now(pytz.timezone('US/Eastern'))
    current_time_str = now.strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")
    extracted_date_from_today_date = datetime.today().date()

    for symbol, (bought_price, purchase_date) in bought_stocks.items():
        status_printer_sell_stocks()
        today_date_str = extracted_date_from_today_date.strftime("%Y-%m-%d")
        bought_date_str = purchase_date

        if bought_date_str < today_date_str:
            current_price = get_current_price(symbol)
            position = api.get_position(symbol)
            bought_price = float(position.avg_entry_price)

            open_orders = api.list_orders(status='open', symbol=symbol)
            if open_orders:
                print(f"There is an open sell order for {symbol}. Skipping sell order.")
                continue

            if current_price >= bought_price * 1.005:
                qty = api.get_position(symbol).qty
                api.submit_order(symbol=symbol, qty=qty, side='sell', type='market', time_in_force='day')
                print(f" {current_time_str}, Sold {qty} shares of {symbol} at {current_price} based on a higher selling price.")
                with open(csv_filename, mode='a', newline='') as csv_file:
                    csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                    csv_writer.writerow({
                        'Date': current_time_str,
                        'Sell': 'Sell',
                        'Quantity': qty,
                        'Symbol': symbol,
                        'Price Per Share': current_price
                    })
                stocks_to_remove.append(symbol)
                time.sleep(2)
            time.sleep(2)

    try:
        with buy_sell_lock:
            for symbol in stocks_to_remove:
                del bought_stocks[symbol]
                trade_history = TradeHistory(symbol=symbol, action='sell', quantity=qty, price=current_price,
                                             date=today_date_str)
                session.add(trade_history)
                session.query(Position).filter_by(symbol=symbol).delete()
            session.commit()
            refresh_after_sell()
    except SQLAlchemyError as e:
        session.rollback()
        logging.error(f"Database error: {str(e)}")

def refresh_after_sell():
    global bought_stocks
    time.sleep(2)
    bought_stocks = update_bought_stocks_from_api()

def load_positions_from_database():
    positions = session.query(Position).all()
    bought_stocks = {}
    for position in positions:
        symbol = position.symbol
        avg_price = position.avg_price
        initial_api_returned_purchase_date = position.purchase_date
        purchase_date = initial_api_returned_purchase_date
        bought_stocks[symbol] = (avg_price, purchase_date)
    return bought_stocks

def main():
    global stocks_to_buy
    stocks_to_buy = get_stocks_to_trade()
    bought_stocks = load_positions_from_database()
    buy_sell_lock = threading.Lock()

    while True:
        try:
            stop_if_stock_market_is_closed()
            now = datetime.now(pytz.timezone('US/Eastern'))
            current_time_str = now.strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")

            cash_balance = round(float(api.get_account().cash), 2)
            print("------------------------------------------------------------------------------------")
            print("\n")
            print("*****************************************************")
            print("******** Billionaire Buying Strategy Version ********")
            print("*****************************************************")
            print("2023 Edition of the Advanced Stock Market Trading Robot, Version 2 ")
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

            buy_thread = threading.Thread(target=buy_stocks, args=(bought_stocks, stocks_to_buy, buy_sell_lock))
            sell_thread = threading.Thread(target=sell_stocks, args=(bought_stocks, buy_sell_lock))

            buy_thread.start()
            sell_thread.start()

            buy_thread.join()
            sell_thread.join()

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
            time.sleep(120)

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logging.error(f"Error encountered: {e}")
        session.close()
