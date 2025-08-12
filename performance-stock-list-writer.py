import time
import pytz
import yfinance as yf
from datetime import datetime, timedelta
import talib
import numpy as np


# Function to calculate percentage change for a given timeframe
def calculate_percentage_change(stock_data, period):
    if stock_data.empty:
        return 0  # Default value when data is unavailable

    start_price = stock_data['Open'].iloc[0]
    end_price = stock_data['Close'].iloc[-1]

    percentage_change = ((end_price - start_price) / start_price) * 100
    return percentage_change


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

        if run_count == 1 or (now.weekday() in [0, 1, 2, 3, 4] and start_time <= now.time() <= end_time):
            # Increment run count
            run_count += 1

            # Read the list of stock symbols from the input file
            with open("list-of-stock-symbols-to-scan.txt", "r") as input_file:
                stock_symbols = [line.strip() for line in input_file if line.strip()]

            # Initialize a list to store filtered stocks
            filtered_stocks = []

            for symbol in stock_symbols:
                stock = yf.Ticker(symbol)
                print(f"Downloading the historical data for {symbol}...")

                # Download maximum available data for the stock
                stock_data = stock.history(period="30d")  # Adjust the period to 30 days

                # Calculate percentage changes for different timeframes
                percentage_change_30_days = calculate_percentage_change(stock_data, "30d")
                percentage_change_5_days = calculate_percentage_change(stock_data.tail(5), "5d")  # Adjust to 5 days

                # Check if the stock meets the filtering criteria
                if (
                        percentage_change_30_days > 0
                        and percentage_change_5_days > 0
                ):
                    filtered_stocks.append(symbol)

                time.sleep(2)  # Sleep for 2 seconds

            # Write the selected stock symbols to the output file
            with open("electricity-or-utility-stocks-to-buy-list.txt", "w") as output_file:
                for symbol in filtered_stocks:
                    output_file.write(f"{symbol}\n")

            print("")
            print("Successful stocks list updated successfully.")
            print("")

        # Calculate the next run time
        if now.time() > end_time:
            next_run = now + timedelta(days=1, minutes=30)
            next_run = next_run.replace(hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0)
        else:
            next_run = now + timedelta(minutes=5)
            next_run = next_run.replace(second=0, microsecond=0)

        main_message = f"Next run will be soon after the time of {next_run.strftime('%I:%M %p')} (Eastern Time)."
        print(main_message)
        print("")

        # Wait until after printing the list of stocks before calculating the next run time
        time_until_next_run = (next_run - now).total_seconds()
        time.sleep(time_until_next_run)

    except Exception as e:
        print("")
        print(f"An error occurred: {str(e)}")
        print("Restarting the program in 5 minutes...")
        print("")
        time.sleep(300)
