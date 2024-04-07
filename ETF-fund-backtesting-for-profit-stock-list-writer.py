import yfinance as yf
from datetime import datetime, timedelta
import time
import pytz


# Function to check if it's within the specified market hours
def is_market_hours(current_time):
    market_open_time = current_time.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close_time = current_time.replace(hour=16, minute=0, second=0, microsecond=0)

    return current_time.weekday() < 5 and market_open_time <= current_time <= market_close_time


# Function to check if a stock has increased in value by 10%
def has_increased_by_10_percent(start_value, end_value):
    return (end_value - start_value) / start_value >= 0.1


# Function to get the current date and time in US/Eastern timezone
def get_current_time():
    eastern_tz = pytz.timezone('US/Eastern')
    now = datetime.now(eastern_tz)
    return now


# Function to calculate the end time considering weekdays and market hours
def calculate_end_time(current_time):
    market_open_time = current_time.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close_time = current_time.replace(hour=16, minute=0, second=0, microsecond=0)

    # If the current time is after the market close, set end_time to the next market open time
    if current_time >= market_close_time:
        end_time = market_open_time + timedelta(days=1)
    else:
        end_time = market_close_time

    return end_time


# Function to calculate the start time as 14 days before the end time
def calculate_start_time(end_time):
    return end_time - timedelta(days=14)


# Function to handle errors and restart the program
def handle_error():
    print("An error occurred. Restarting the program in 5 seconds.....")
    time.sleep(5)


# Initialize end_time outside the loop
end_time = calculate_end_time(get_current_time())

while True:
    try:
        # Get the current time
        current_time = get_current_time()
        print("")
        # Display the date header
        formatted_current_time = current_time.strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")
        print(formatted_current_time)
        print("")
        # Check if it's within the specified market hours
        # if is_market_hours(current_time):     # uncomment this line to only run during market hours.
        if 1 == 1:  # comment out this line if you uncomment the line above.

            print("Running the program.....\n")

            # Calculate the end time considering weekdays and market hours
            end_time = calculate_end_time(current_time)

            # Calculate the start time as 14 days before the end time
            start_time = calculate_start_time(end_time)

            print("")
            print(f"Start Time: {start_time.strftime('%Y-%m-%d %H:%M')}")
            print(f"End Time: {end_time.strftime('%Y-%m-%d %H:%M')}")
            print("")

            # Read the list of stock symbols from the input file
            with open("list-of-stock-symbols-to-scan.txt", "r") as file:
                stock_symbols = file.read().splitlines()

            # Initialize a list to store symbols that meet the criteria
            symbols_to_write = []

            # Fetch stock data and append symbols that have increased by 10% or more to the list
            for symbol in stock_symbols:
                stock = yf.Ticker(symbol)
                print(f"Downloading the historical data for {symbol}.....")

                # Download last 14 days of data for the stock
                stock_data = stock.history(start=start_time, end=end_time)

                # Example backtesting logic (replace with your own)
                if not stock_data.empty:
                    start_price = stock_data["Close"].iloc[0]
                    end_price = stock_data["Close"].iloc[-1]

                    # Check if the stock has increased in value by 10%
                    if has_increased_by_10_percent(start_price, end_price):
                        # Print the backtesting details with formatted dates and 2 digits after the decimal point
                        print(
                            f"Backtesting Dates: {start_time.strftime('%Y-%m-%d %H:%M')} to {end_time.strftime('%Y-%m-%d %H:%M')}")
                        print(f"Stock Symbol: {symbol}")
                        print(f"Start Price Value: {start_price:.2f}")
                        print(f"End Price Value: {end_price:.2f}")

                        # Calculate total price change and percentage change
                        total_price_change = end_price - start_price
                        percentage_change = (total_price_change / start_price) * 100

                        print(f"Total Price Change: {total_price_change:.2f}")
                        print(f"Percentage Change: {percentage_change:.2f}%\n")

                        # Append the symbol to the list
                        symbols_to_write.append(symbol)

                # Introduce a delay before moving on to the next stock symbol
                time.sleep(2)

            # Write all symbols to the output file
            with open("electricity-or-utility-stocks-to-buy-list.txt", "w") as output_file:
                # Erase the output file before writing to it
                pass

                print("")
                print("Writing the list of stocks: ")

                # Write all symbols to the output file
                for symbol in symbols_to_write:
                    output_file.write(f"{symbol}\n")
                    print(f"{symbol}")

            # Calculate the next market close time
            end_time = calculate_end_time(current_time)

            print("")
            print("Sleeping for 30 seconds.....")
            print("")
            # Sleep for 30 seconds
            time.sleep(30)

            # Optionally, you can remove the line below if you want to display the time again after waking up
            # print(f"Next run scheduled at {end_time}. Resuming program...\n")

            # Print a message to inform the user that the program has completed for the current iteration
            print("Program completed for the current iteration.\n")

        else:
            print("The program is not running outside market hours. Waiting for the next market open.....\n")

    except KeyboardInterrupt:
        print("Program terminated by user.")
        break
    except Exception as e:
        handle_error()
        continue
