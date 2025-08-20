import time
import pytz
import yfinance as yf
from datetime import datetime, timedelta
import talib
import numpy as np
import logging
from ratelimit import limits, sleep_and_retry

# Set up logging
logging.basicConfig(
    filename='stock_filter.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Rate limit: 120 calls per minute (0.5 seconds per call)
CALLS = 120
PERIOD = 60

# Function to calculate percentage change for a given timeframe
def calculate_percentage_change(stock_data, period):
    if stock_data.empty:
        logging.warning(f"No data available for period {period}")
        return 0  # Default value when data is unavailable

    start_price = stock_data['Open'].iloc[0]
    end_price = stock_data['Close'].iloc[-1]

    percentage_change = ((end_price - start_price) / start_price) * 100
    return percentage_change

@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def fetch_stock_history(symbol, period="30d", retries=3):
    """Fetch historical stock data with rate limiting and retry logic."""
    for attempt in range(retries):
        try:
            stock = yf.Ticker(symbol)
            stock_data = stock.history(period=period)
            if stock_data.empty:
                logging.warning(f"No data retrieved for {symbol}")
                return pd.DataFrame()
            logging.info(f"Successfully fetched data for {symbol}")
            return stock_data
        except Exception as e:
            if "429" in str(e):
                logging.error(f"Rate limit exceeded for {symbol}: {e}. Attempt {attempt+1}/{retries}")
            else:
                logging.warning(f"Failed to fetch data for {symbol}: {e}. Attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(5 * (2 ** attempt))  # Exponential backoff
    logging.error(f"Failed to fetch data for {symbol} after {retries} attempts")
    return pd.DataFrame()

# Define the start and end times for when the program should run
start_time = datetime.now().replace(hour=8, minute=30, second=0, microsecond=0).time()
end_time = datetime.now().replace(hour=15, minute=59, second=0, microsecond=0).time()

# Initialize run count
run_count = 1

# Get the current date
current_date = datetime.now()

# Extract the current month from the date
current_month = current_date.month

# Extract the current year from the date
current_year = current_date.year

# Main program loop
while True:
    try:
        eastern = pytz.timezone('US/Eastern')
        now = datetime.now(eastern)
        logging.info(f"Current time: {now.strftime('%I:%M %p')} (Eastern Time)")

        if run_count == 1 or (now.weekday() in [0, 1, 2, 3, 4] and start_time <= now.time() <= end_time):
            # Increment run count
            run_count += 1
            logging.info(f"Starting run #{run_count}")

            # Read the list of stock symbols from the input file
            try:
                with open("list-of-stock-symbols-to-scan.txt", "r") as input_file:
                    stock_symbols = [line.strip() for line in input_file if line.strip()]
                logging.info(f"Read {len(stock_symbols)} stock symbols from input file")
            except FileNotFoundError:
                logging.error("Input file 'list-of-stock-symbols-to-scan.txt' not found")
                print("Input file not found. Retrying in 5 minutes...")
                time.sleep(300)
                continue

            # Initialize a list to store filtered stocks
            filtered_stocks = []

            for symbol in stock_symbols:
                logging.info(f"Processing {symbol}")
                print(f"Downloading the historical data for {symbol}...")

                # Download maximum available data for the stock
                stock_data = fetch_stock_history(symbol, period="30d")

                # Calculate percentage changes for different timeframes
                percentage_change_30_days = calculate_percentage_change(stock_data, "30d")
                percentage_change_5_days = calculate_percentage_change(stock_data.tail(5), "5d")

                # Check if the stock meets the filtering criteria
                if (
                    percentage_change_30_days > 0
                    and percentage_change_5_days > 0
                ):
                    filtered_stocks.append(symbol)
                    logging.info(f"{symbol} meets criteria: 30d change={percentage_change_30_days:.2f}%, 5d change={percentage_change_5_days:.2f}%")
                else:
                    logging.info(f"{symbol} does not meet criteria: 30d change={percentage_change_30_days:.2f}%, 5d change={percentage_change_5_days:.2f}%")

            # Write the selected stock symbols to the output file
            try:
                with open("electricity-or-utility-stocks-to-buy-list.txt", "w") as output_file:
                    for symbol in filtered_stocks:
                        output_file.write(f"{symbol}\n")
                logging.info(f"Wrote {len(filtered_stocks)} symbols to output file")
                print("")
                print("Successful stocks list updated successfully.")
                print("")
            except Exception as e:
                logging.error(f"Failed to write to output file: {e}")
                print(f"Error writing to output file: {e}")

        # Calculate the next run time
        if now.time() > end_time:
            next_run = now + timedelta(days=1, minutes=30)
            next_run = next_run.replace(hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0)
        else:
            next_run = now + timedelta(minutes=5)
            next_run = next_run.replace(second=0, microsecond=0)

        main_message = f"Next run will be soon after the time of {next_run.strftime('%I:%M %p')} (Eastern Time)."
        logging.info(main_message)
        print(main_message)
        print("")

        # Wait until after printing the list of stocks before calculating the next run time
        time_until_next_run = (next_run - now).total_seconds()
        logging.info(f"Sleeping for {time_until_next_run:.2f} seconds until next run")
        time.sleep(time_until_next_run)

    except Exception as e:
        logging.error(f"Main loop error: {e}")
        print("")
        print(f"An error occurred: {str(e)}")
        print("Restarting the program in 5 minutes...")
        print("")
        time.sleep(300)