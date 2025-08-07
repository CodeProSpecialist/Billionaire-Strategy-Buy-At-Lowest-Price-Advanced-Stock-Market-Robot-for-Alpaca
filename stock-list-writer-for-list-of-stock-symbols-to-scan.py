import yfinance as yf
import time
from datetime import datetime, timedelta
import os
import pytz

# Set the Eastern timezone as global for yfinance to access
eastern_timezone = pytz.timezone('US/Eastern')


def calculate_largest_price_increase(stock_symbol, years_ago):
    stock = yf.Ticker(stock_symbol)
    current_time = datetime.now(eastern_timezone)
    target_time = current_time - timedelta(days=365 * years_ago)
    largest_increase = -float('inf')
    best_month = None

    print("")
    print(" Stock List Writer Program for the list of stock symbols to scan ")
    print("")
    print("Reading the file s-and-p-500-large-list-of-stocks.txt ")
    print("")
    print("Looking for the best Historical Data prices for this Month in the S&P 500")
    print("")
    print("Searching through the entire S&P 500 for the past 2 years. ")
    print("")
    print("Please be patient. This could take more than 7 hours........")
    print("")

    for month in range(1, 13):
        last_day_of_month = (target_time.replace(day=1, month=month) + timedelta(days=32)).replace(day=1) - timedelta(
            days=1)
        start_date = f"{target_time.year}-{month:02d}-01"
        end_date = f"{last_day_of_month.year}-{last_day_of_month.month:02d}-{last_day_of_month.day:02d}"

        # Format the datetime as specified
        formatted_date = current_time.strftime("%A, %B %d, %Y, %I:%M %p")
        print(f"Eastern Time: {formatted_date} - Downloading Stock Information for {stock_symbol} ({years_ago} years ago)")

        historical_data = stock.history(start=start_date, end=end_date)
        time.sleep(1)

        if not historical_data.empty:
            price_increase = (historical_data["Close"].iloc[-1] - historical_data["Close"].iloc[0]) / \
                             historical_data["Close"].iloc[0]
            if price_increase > largest_increase:
                largest_increase = price_increase
                best_month = month

    return best_month


def main():
    # Read the list of stocks from the input file
    with open("s-and-p-500-large-list-of-stocks.txt", "r") as input_file:
        stocks = input_file.read().splitlines()

    # Check if a counter file exists indicating how many times the script has run
    counter_file_path = "s-and-p-500-list-printer-run-counter.txt"

    if os.path.exists(counter_file_path):
        with open(counter_file_path, "r") as counter_file:
            run_count = int(counter_file.read())
    else:
        run_count = 0

    # Increment the run count
    run_count += 1

    # Write the updated run count to the counter file
    with open(counter_file_path, "w") as counter_file:
        counter_file.write(str(run_count))

    # Get the current date and time
    current_time = datetime.now()

    # Get the current month
    current_month = current_time.month

    # Define a list to store the stock symbols to scan
    stock_symbols_to_scan = []

    # Calculate the best month for 1 year ago and 2 years ago for each stock
    for stock in stocks:
        best_month_1_year_ago = calculate_largest_price_increase(stock, 1)
        best_month_2_years_ago = calculate_largest_price_increase(stock, 2)

        if best_month_1_year_ago == current_month or best_month_2_years_ago == current_month:
            stock_symbols_to_scan.append(stock.upper())

    # Write the stock symbols to scan to the output file for the current month's best stocks
    with open("list-of-stock-symbols-to-scan.txt", "w") as output_file:
        for stock_symbol in stock_symbols_to_scan:
            output_file.write(stock_symbol + '\n')

    # Print the next run time
    next_run_time = current_time + timedelta(days=1)
    next_run_time = next_run_time.replace(hour=16, minute=15, second=0, microsecond=0)

    # If this is the first run, there's no need to sleep
    if run_count > 1:
        # Calculate the time difference until the next run
        time_difference = next_run_time - current_time

        # Check if the target time is in the past, and if so, add one day to the target time
        if time_difference.total_seconds() < 0:
            next_run_time += timedelta(days=1)
            time_difference = next_run_time - current_time
        print(f"Next run time: {next_run_time}")
        # Sleep for the calculated time difference
        time.sleep(time_difference.total_seconds())

    print(f"Next run time: {next_run_time}")


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print(f"Error occurred: {e}")
            print("Restarting in 5 minutes...")
            time.sleep(300)  # Sleep for 5 minutes before restarting
