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

# import warnings     # comment out this line to utilize warnings.filterwarnings

# warnings.filterwarnings('ignore')     # comment out this line to display more error messages.

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
# Default value POSITION_DATES_AS_YESTERDAY_OPTION = False
POSITION_DATES_AS_YESTERDAY_OPTION = False  # keep this as False to not change the dates of owned stocks

# set the timezone to Eastern
eastern = pytz.timezone('US/Eastern')

# Dictionary to maintain previous prices and price increase and price decrease counts
stock_data = {}

# Dictionary to store previous prices for symbols
previous_prices = {}

end_time = 0  # Initialize end_time as a global variable

# Define the API datetime format
api_time_format = '%Y-%m-%dT%H:%M:%S.%f-04:00'

# the below variable was recommended by Artificial Intelligence
buy_sell_lock = threading.Lock()

logging.basicConfig(filename='trading-bot-program-logging-messages.txt', level=logging.INFO)

# Define the CSV file and fieldnames
csv_filename = 'log-file-of-buy-and-sell-signals.csv'
fieldnames = ['Date', 'Buy', 'Sell', 'Quantity', 'Symbol', 'Price Per Share']

# Open the CSV file for writing and set up a CSV writer
with open(csv_filename, mode='w', newline='') as csv_file:
    csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)

    # Write the header row
    csv_writer.writeheader()

# Define the Database Models
# Newer Data Base Model code below
Base = sqlalchemy.orm.declarative_base()


class TradeHistory(Base):
    __tablename__ = 'trade_history'
    id = Column(Integer, primary_key=True)
    symbol = Column(String)
    action = Column(String)  # 'buy' or 'sell'
    quantity = Column(Integer)
    price = Column(Float)
    date = Column(String)
    # the above date is string data format in the database


class Position(Base):
    __tablename__ = 'positions'
    symbol = Column(String, primary_key=True)
    quantity = Column(Integer)
    avg_price = Column(Float)
    purchase_date = Column(String)
    # the above date is string data format in the database


# Initialize SQLAlchemy
engine = create_engine('sqlite:///trading_bot.db')
Session = sessionmaker(bind=engine)
session = Session()

# Create tables if not exist
Base.metadata.create_all(engine)

def stop_if_stock_market_is_closed():
    # Check if the current time is within the stock market hours
    # Set the stock market open and close times
    market_open_time = time2(9, 30)
    market_close_time = time2(16, 0)

    while True:
        # Get the current time in Eastern Time
        eastern = pytz.timezone('US/Eastern')
        now = datetime.now(eastern)
        current_time = now.time()

        # Check if the current time is within market hours
        if now.weekday() <= 4 and market_open_time <= current_time <= market_close_time:
            break

        print("\n")
        print('''
                        
        *********************************************************************************
        ************ Billionaire Buying Strategy Version ********************************
        *********************************************************************************
            2023 Edition of the Advanced Stock Market Trading Robot, Version 2 
           _____   __                   __             ____            __            __ 
          / ___/  / /_  ____   _____   / /__          / __ \  ____    / /_   ____   / /_
          \__ \  / __/ / __ \ / ___/  / //_/         / /_/ / / __ \  / __ \ / __ \ / __/
         ___/ / / /_  / /_/ // /__   / ,<           / _, _/ / /_/ / / /_/ // /_/ // /_  
        /____/  \__/  \____/ \___/  /_/|_|         /_/ |_|  \____/ /_.___/ \____/ \__/  

                                                  https://github.com/CodeProSpecialist

                       Featuring an An Accelerated Database Engine with Python 3 SQLAlchemy  

         ''')
        print(f'Current date & time (Eastern Time): {now.strftime("%A, %B %d, %Y, %I:%M:%S %p")}')
        print("Stockbot only works Monday through Friday: 9:30 am - 4:00 pm Eastern Time.")
        print("Waiting until Stock Market Hours to begin the Stockbot Trading Program.")
        print("\n")
        print("\n")
        time.sleep(60)  # Sleep for 1 minute and check again. Keep this under the p in print.



def print_database_tables():
    if PRINT_DATABASE:
        positions = api.list_positions()
        show_price_percentage_change = True  # set to true to view % price changes

        # Print TradeHistory table
        print("\nTrade History In This Robot's Database:")
        print("\n")
        print("Stock | Buy or Sell | Quantity | Avg. Price | Date ")
        print("\n")

        for record in session.query(TradeHistory).all():
            print(f"{record.symbol} | {record.action} | {record.quantity} | {record.price:.2f} | {record.date}")

        print("----------------------------------------------------------------")
        # Print Position table
        print("\n")
        print("Positions in the Database To Sell 1 or More Days After the Date Shown:")
        print("\n")
        print("Stock | Quantity | Avg. Price | Date or The 1st Day This Robot Began Working ")
        print("\n")
        for record in session.query(Position).all():
            symbol, quantity, avg_price, purchase_date = record.symbol, record.quantity, record.avg_price, record.purchase_date

            # Format purchase_date to show 0 digits after the decimal point
            purchase_date_str = purchase_date  # this is already correct string data format

            # Calculate percentage change if show_price_percentage_change is True
            if show_price_percentage_change:
                current_price = get_current_price(symbol)  # Replace with your actual method to get current price
                percentage_change = ((current_price - avg_price) / avg_price) * 100
                print(
                    f"{symbol} | {quantity} | {avg_price:.2f} | {purchase_date_str} | Price Change: {percentage_change:.2f}%")
            else:
                print(f"{symbol} | {quantity} | {avg_price:.2f} | {purchase_date_str}")
        print("\n")


def get_stocks_to_trade():
    try:
        with open('electricity-or-utility-stocks-to-buy-list.txt', 'r') as file:
            stock_symbols = [line.strip() for line in file.readlines()]

        if not stock_symbols:  # keep this under the w in with
            print("\n")
            print(
                "********************************************************************************************************")
            print(
                "*   Error: The file electricity-or-utility-stocks-to-buy-list.txt doesn't contain any stock symbols.   *")
            print(
                "*   This Robot does not work until you place stock symbols in the file named:                          *")
            print(
                "*       electricity-or-utility-stocks-to-buy-list.txt                                                  *")
            print(
                "********************************************************************************************************")
            print("\n")

        return stock_symbols  # keep this under the i in if

    except FileNotFoundError:  # keep this under the t in try
        print("\n")
        print("****************************************************************************")
        print("*   Error: File not found: electricity-or-utility-stocks-to-buy-list.txt   *")
        print("****************************************************************************")
        print("\n")
        return []  # keep this under the p in print


def remove_symbol_from_trade_list(symbol):
    with open('electricity-or-utility-stocks-to-buy-list.txt', 'r') as file:
        lines = file.readlines()
    with open('electricity-or-utility-stocks-to-buy-list.txt', 'w') as file:
        for line in lines:
            if line.strip() != symbol:
                file.write(line)  # keep this under the i in line


def get_opening_price(symbol):
    stock_data = yf.Ticker(symbol)
    try:
        # Fetch the stock data for today and get the opening price
        opening_price = round(stock_data.history(period="1d")["Open"].iloc[0], 4)
        return opening_price
    except IndexError:
        # Handle the case where the stock data is not available
        logging.error(f"Opening price not found for {symbol}.")
        return None



def get_current_price(symbol):
    stock_data = yf.Ticker(symbol)
    return round(stock_data.history(period='1d')['Close'].iloc[0], 4)


def get_atr_high_price(symbol):
    atr_value = get_average_true_range(symbol)
    current_price = get_current_price(symbol)
    return round(current_price + 0.40 * atr_value, 4)


def get_atr_low_price(symbol):
    atr_value = get_average_true_range(symbol)
    current_price = get_current_price(symbol)
    return round(current_price - 0.10 * atr_value, 4)


def get_average_true_range(symbol):
    ticker = yf.Ticker(symbol)
    data = ticker.history(period='30d')
    atr = talib.ATR(data['High'].values, data['Low'].values, data['Close'].values, timeperiod=22)
    return atr[-1]


def status_printer_buy_stocks():
    print(f"\rBuy stocks function is working correctly right now. Checking stocks to buy.....", end='', flush=True)
    # After the loop, print a newline character to move to the next line with the print command below.
    print()  # keep this under the s in status_printer_buy_stocks()


def status_printer_sell_stocks():
    print(f"\rSell stocks function is working correctly right now. Checking stocks to sell.....", end='', flush=True)
    # After the loop, print a newline character to move to the next line with the print command below.
    print()  # keep this under the s in status_printer_sell_stocks()


# Function to calculate MACD, RSI, and Volume
def calculate_technical_indicators(symbol, lookback_days=90):
    stock_data = yf.Ticker(symbol)
    historical_data = stock_data.history(period=f'{lookback_days}d')

    # Calculate MACD
    short_window = 12
    long_window = 26
    signal_window = 9
    historical_data['macd'], historical_data['signal'], _ = talib.MACD(historical_data['Close'],
                                                                       fastperiod=short_window,
                                                                       slowperiod=long_window,
                                                                       signalperiod=signal_window)

    # Calculate RSI
    rsi_period = 14
    historical_data['rsi'] = talib.RSI(historical_data['Close'], timeperiod=rsi_period)

    # Calculate Volume
    historical_data['volume'] = historical_data['Volume']

    return historical_data


# Function to print technical indicators
def print_technical_indicators(symbol, historical_data):
    print("")
    print(f"\nTechnical Indicators for {symbol}:\n")
    print(historical_data[['Close', 'macd', 'signal', 'rsi', 'volume']].tail())
    print("")


def calculate_cash_on_hand():
    # Calculate the total cash on hand
    cash_available = round(float(api.get_account().cash), 2)
    return cash_available


def calculate_total_symbols(stocks_to_buy):
    # Calculate the total number of symbols in "stocks_to_buy"
    total_symbols = len(stocks_to_buy)
    return total_symbols


def allocate_cash_equally(cash_available, total_symbols):
    # Make the total quantity of stocks_to_buy as close to $600 as possible and buy no more than $600 per stock symbol
    max_allocation_per_symbol = 600  # how much cash to spend per stock purchase
    allocation_per_symbol = min(max_allocation_per_symbol, cash_available) / total_symbols
    return allocation_per_symbol


def get_previous_price(symbol):
    # Check if the symbol has a previous price
    if symbol in previous_prices:
        return previous_prices[symbol]
    else:
        # If no previous price is available, fetch the current price and use it as the previous price
        current_price = get_current_price(symbol)  # Fetch the current price
        previous_prices[symbol] = current_price  # Set it as the previous price
        print(
            f"No previous price for {symbol} was found. Using the current price as the previous price: {current_price}")
        return current_price


# Function to update previous prices
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
    # Print the values of the technical indicators
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
    time.sleep(1)  # Wait 1 - 3 seconds per price check


def end_time_reached():
    return time.time() >= end_time


def buy_stocks(bought_stocks, stocks_to_buy, buy_sell_lock):
    stocks_to_remove = []

    for symbol in stocks_to_buy:
        today_date = datetime.today().date()
        opening_price = get_opening_price(symbol)

        if opening_price is not None:  # Check if opening price is fetched successfully
            current_price = get_current_price(symbol)
            cash_available = round(float(api.get_account().cash), 2)
            qty_of_one_stock = 1
            now = datetime.now(pytz.timezone('US/Eastern'))
            current_time_str = now.strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")
            total_cost_for_qty = current_price * qty_of_one_stock
            factor_to_subtract = 0.99     # -1.00% decrease as a decimal is the number 0.99
            profit_buy_price_setting = opening_price * factor_to_subtract

            status_printer_buy_stocks()

            if (cash_available >= total_cost_for_qty and current_price <= profit_buy_price_setting):
                api.submit_order(symbol=symbol, qty=qty_of_one_stock, side='buy', type='market', time_in_force='day')
                print(f" {current_time_str} , Bought {qty_of_one_stock} shares of {symbol} at {current_price}")
                logging.info(f"{current_time_str} Buy {qty_of_one_stock} shares of {symbol}.")
                print("")
                with open(csv_filename, mode='a', newline='') as csv_file:
                    csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                    csv_writer.writerow(
                        {'Date': current_time_str, 'Buy': 'Buy', 'Quantity': qty_of_one_stock, 'Symbol': symbol,
                         'Price Per Share': current_price})

                stocks_to_remove.append((symbol, current_price, today_date))
                time.sleep(2)

            time.sleep(0.8)

        else:
            print(f"Failed to fetch opening price for {symbol}.")
            logging.error(f"Failed to fetch opening price for {symbol}.")
        time.sleep(0.5)     # keep the t in time just under the "e" in else.

    time.sleep(0.8)

    try:
        with buy_sell_lock:
            for symbol, price, date in stocks_to_remove:
                bought_stocks[symbol] = (round(price, 4), date)
                stocks_to_buy.remove(symbol)
                remove_symbol_from_trade_list(symbol)
                trade_history = TradeHistory(symbol=symbol, action='buy', quantity=qty_of_one_stock, price=price, date=date)
                session.add(trade_history)
                db_position = Position(symbol=symbol, quantity=qty_of_one_stock, avg_price=price, purchase_date=date)
                session.add(db_position)

            session.commit()
            refresh_after_buy()
    except SQLAlchemyError as e:
        session.rollback()
        # Handle the error or log it


def refresh_after_buy():
    global stocks_to_buy, bought_stocks
    time.sleep(2)
    stocks_to_buy = get_stocks_to_trade()
    bought_stocks = update_bought_stocks_from_api()


# Modify the update_bought_stocks_from_api function to use the correct purchase date
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
                db_position.purchase_date = yesterday.strftime("%Y-%m-%d")  # Use the provided date format
        except NoResultFound:
            purchase_date = yesterday if POSITION_DATES_AS_YESTERDAY_OPTION and run_counter < 1 else datetime.today()
            purchase_date_str = purchase_date.strftime("%Y-%m-%d")  # Convert datetime to string
            db_position = Position(symbol=symbol, quantity=position.qty, avg_price=avg_entry_price,
                                   purchase_date=purchase_date_str)  # Use the provided date format
            session.add(db_position)

        bought_stocks[symbol] = (avg_entry_price, db_position.purchase_date)

    with open(run_counter_file, "w") as f:
        f.write(str(run_counter))

    session.commit()
    return bought_stocks

def sell_stocks(bought_stocks, buy_sell_lock):
    stocks_to_remove = []

    # below time and date are only used in the logging file
    now = datetime.now(pytz.timezone('US/Eastern'))
    current_time_str = now.strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")

    # below date is used in the database and to sell stocks
    extracted_date_from_today_date = datetime.today().date()

    for symbol, (bought_price, purchase_date) in bought_stocks.items():

        status_printer_sell_stocks()  # keep this under the "s" in "for symbol"

        # Convert today_date and bought_date to text strings
        today_date_str = extracted_date_from_today_date.strftime("%Y-%m-%d")
        bought_date_str = purchase_date  # already string data format

        # the rest of the code goes by purchase_date instead of bought_date

        # print("today_date_str = ", symbol, today_date_str)  # uncomment to print variable date to debug as same date

        # print("bought_date_str = ", symbol, bought_date_str)  # uncomment to print variable date to debug as same date

        # Check if the stock was purchased at least one day before today
        # if bought_date_str < today_date_str:

        if bought_date_str < today_date_str:  # keep under the "s" in "for symbol"
            current_price = get_current_price(symbol)  # keep this under the "o" in "bought"
            position = api.get_position(symbol)  # keep this under the "o" in "bought"
            bought_price = float(position.avg_entry_price)  # keep this under the "o" in "bought"

            # Check if there is an open sell order for the symbol
            open_orders = api.list_orders(status='open', symbol=symbol)
            if open_orders:
                print(f"There is an open sell order for {symbol}. Skipping sell order.")
                continue  # Skip to the next iteration if there's an open sell order

            # Never calculate ATR for a buy price or sell price because it is too slow. 1 second per stock.
            # Sell stocks if the current price is more than 0.5% higher than the purchase price.
            if current_price >= bought_price * 1.005:  # keep this under the "o" in "bought"
                qty = api.get_position(symbol).qty
                api.submit_order(symbol=symbol, qty=qty, side='sell', type='market', time_in_force='day')
                print(f" {current_time_str}, Sold {qty} shares of {symbol} at {current_price} based on a higher selling price. ")
                logging.info(f"{current_time_str} Sell {qty} shares of {symbol} based on a higher selling price. ") 
                with open(csv_filename, mode='a', newline='') as csv_file:
                    csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                    csv_writer.writerow(
                        {'Date': current_time_str, 'Sell': 'Sell', 'Quantity': qty, 'Symbol': symbol,
                         'Price Per Share': current_price})

                stocks_to_remove.append(symbol)  # Append symbols to remove. keep this under the w in with open

                time.sleep(2)  # keep this under the s in stocks

            time.sleep(0.5)  # keep this under the i in if current_price. this stops after checking each stock price
        time.sleep(0.5)
    # I might not need the extra sleep command below
    # keep the below time.sleep(1) below the f in "for symbol"
    time.sleep(1)  # wait 1 - 3 seconds to not move too fast for the stock price data rate limit.

    try:  # keep this under the s in "sell stocks"
        with buy_sell_lock:
            for symbol in stocks_to_remove:
                del bought_stocks[symbol]
                trade_history = TradeHistory(symbol=symbol, action='sell', quantity=qty, price=current_price,
                                             date=today_date_str)  # Use the provided "string data" date format
                session.add(trade_history)
                session.query(Position).filter_by(symbol=symbol).delete()
            session.commit()
            refresh_after_sell()
    except SQLAlchemyError as e:  # keep this under the t in "try"
        session.rollback()
        # Handle the error or log it


def refresh_after_sell():
    global bought_stocks
    time.sleep(2)
    bought_stocks = update_bought_stocks_from_api()


def main():
    global stocks_to_buy

    stocks_to_buy = get_stocks_to_trade()
    bought_stocks = load_positions_from_database()
    buy_sell_lock = threading.Lock()

    while True:  # keep this under the m in main
        try:
            stop_if_stock_market_is_closed()  # comment this line to debug the Python code
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
            print(f"  {current_time_str} Cash Balance: ${cash_balance}")
            day_trade_count = api.get_account().daytrade_count
            print("\n")
            print(f"Current day trade number: {day_trade_count} out of 3 in 5 business days")
            print("\n")
            print("\n")
            print("------------------------------------------------------------------------------------")
            print("\n")

            stocks_to_buy = get_stocks_to_trade()

            if not bought_stocks:
                bought_stocks = update_bought_stocks_from_api()

            # the below threads will run the buy_stocks and the sell_stocks functions at the same time
            # in parallel to buy and sell more without taking more time than necessary.
            # keep the below python code below the i in if not bought stocks
            # Create threads for buy_stocks and sell_stocks
            buy_thread = threading.Thread(target=buy_stocks, args=(bought_stocks, stocks_to_buy, buy_sell_lock))
            sell_thread = threading.Thread(target=sell_stocks, args=(bought_stocks, buy_sell_lock))

            # Start both threads
            buy_thread.start()
            sell_thread.start()

            # Wait for both threads to finish
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
                    time.sleep(1)  # wait 1 second to not move too fast for the stock data rate limit.
                print("\n")
                print("------------------------------------------------------------------------------------")
                print("\n")

            if PRINT_ROBOT_STORED_BUY_AND_SELL_LIST_DATABASE:
                print_database_tables()

            # account = api.get_account()
            # print(account)   # uncomment to print Alpaca Account details to debug the software

            if DEBUG:
                print("\n")
                print("------------------------------------------------------------------------------------")
                print("\n")
                print("Stocks to Purchase:")
                print("\n")
                for symbol in stocks_to_buy:
                    current_price = get_current_price(symbol)
                    atr_low_price = get_atr_low_price(symbol)
                    print(
                        f"Symbol: {symbol} | Current Price: {current_price} | ATR low buy signal price: {atr_low_price}")

                print("\n")
                print("------------------------------------------------------------------------------------")
                print("\n")
                print("\nStocks to Sell:")
                print("\n")
                for symbol, _ in bought_stocks.items():
                    current_price = get_current_price(symbol)
                    atr_high_price = get_atr_high_price(symbol)
                    print(
                        f"Symbol: {symbol} | Current Price: {current_price} | ATR high sell signal profit price: {atr_high_price}")

                print("\n")

            # keep the below time.sleep(60) to 60 seconds because yfinance api
            # will stop the stock data feed for the reason of exceeding the rate limit or from this program being too fast.
            print("Waiting 30 seconds before checking price data again........")
            time.sleep(30)  # keep this under the i in if

        except Exception as e:
            logging.error(f"Error encountered: {e}")
            time.sleep(120)  # keep this under the l in logging


def load_positions_from_database():
    positions = session.query(Position).all()
    bought_stocks = {}
    for position in positions:
        symbol = position.symbol
        avg_price = position.avg_price
        initial_api_returned_purchase_date = position.purchase_date
        # the purchase date below is changed to string data format
        purchase_date = initial_api_returned_purchase_date.strftime("%Y-%m-%d")
        bought_stocks[symbol] = (avg_price, purchase_date)
    return bought_stocks


if __name__ == '__main__':  # keep this to the far left side.
    try:
        main()  # keep this under the e in name

    except Exception as e:  # keep this under the t in try
        logging.error(f"Error encountered: {e}")  # keep this under the p in except
        session.close()  # keep this under the l in logging
