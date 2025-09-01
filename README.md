# Billionaire-Strategy-Buy-At-Lowest-Price-Advanced-Stock-Market-Robot-for-Alpaca

This is the Billionaire Strategy for Buying Great Stocks at the Lowest Price becuase you can never expect that the Sell Price is a profit, although you have the most control over the Buy Price. 

***** Upgrade to the newest version of this Python Robot today because some Python code updates were finished and some errors were recently fixed on September 1, 2025. *****
 
I recommend this robot for the slowest stock market days like Monday and Tuesday. Then for more Bull Market days like Wednesday, Thursday, and Friday, the Bull Market Trading Robot is the best Robot for that type of Stock Market environment. 

    This buy low strategy was working to make a profit no matter how bad the stocks were moving. 
The only thing that a personal can control is the fact of buying at the lowest price possible. That is the strategy and only robots seem to have the patience to wait all day to buy at the lowest prices.  
( The Billionaire Stock Market Strategy is that there is no guarantee that the stock will sell at a higher price, and so you can only control the action of buying at the lowest price possible. ) 

***************************************************************************************
This is my Original, Most Successful Advanced Stock Market Trading Robot that I brought back from the season of Autumn in 2023 since it was even more profitable 
with its "Billionaire buy at low price strategy."  

***************************************************************************************

( Recommended Operating System: Ubuntu 24.04 LTS Linux )

How does the Billionaire Strategy Stock Market Trading Program Work?

The stock market trading robot automates buying and selling decisions for S&P 500 stocks listed in electricity-or-utility-stocks-to-buy-list.txt, which is populated by a separate script filtering S&P 500 stocks based on performance and technical criteria. Despite the file name, the robot trades across the broad S&P 500 market, leveraging a robust stock selection and trading strategy to maximize profitability. Below are the key features driving its potential profitability.

Advanced Stock Selection
Performance-Based Filtering: The selection script filters S&P 500 stocks (e.g., AAPL, MSFT, NVDA) requiring positive percentage changes over 30 days and 5 days, ensuring short-term and medium-term momentum.

Comprehensive Technical Scoring: Stocks are scored using multiple indicators:

RSI (14-period, neutral 30–70 or oversold ≤ 30), MACD (12, 26, 9), VWAP (14-period), Bollinger Bands (20-period), Stochastic Oscillator (14, 3), ADX (14-period, > 25 for strong trends), and OBV (accumulation).

Price increases ≥ 5% over 1- or 2-year lookbacks, seasonal returns > 5% for the current month, and bonuses for stocks in their historically best-performing month.

Sector Diversification: Limits selections to the top 5 stocks per sector, then picks the top 30 overall, ensuring a diversified S&P 500 portfolio written to electricity-or-utility-stocks-to-buy-list.txt.

Efficient Processing: Uses parallel processing (up to 20 threads) to analyze hundreds of S&P 500 stocks quickly, minimizing delays in stock selection.

Technical Trading Strategy
Buy Signals: The robot buys when RSI ≥ 65 (strong momentum) or the current price is 0.2% below the recent price (0.998 × last price), targeting dips in S&P 500 stocks.

Trend and Volatility Adaptation: Uses MACD to confirm trends and ATR to set dynamic buy (current price - 0.10 × ATR) and sell (current price + 0.40 × ATR) signals, adapting to S&P 500 stock volatility.

Profit-Taking Discipline: Sells stocks held for at least one day when the price exceeds 0.5% above the buy price, locking in small, consistent gains.

Automated Execution
Fractional Shares: Executes market orders via Alpaca’s API with notional values (up to $600 per stock), allowing precise capital allocation across S&P 500 stocks.

Trailing Stops: Places 1% trailing stop-loss orders for whole-share quantities to secure profits or limit losses, though fractional shares lack this protection due to API constraints.

Risk Management
Cash Allocation: Distributes cash equally (max $600 per stock) while maintaining a $1.00 minimum balance, preventing overexposure in the S&P 500.

Day Trade Compliance: Limits day trades to 3 in 5 business days, ensuring regulatory compliance and continuous trading.

Error Resilience: Handles API or data errors with try-except blocks, logging issues, and pausing for 120 seconds (trading) or 300 seconds (stock selection) to recover, ensuring operational stability.

Real-Time Monitoring
Price Updates: Retrieves real-time prices from a market data source during pre-market, regular, and post-market hours, with fallbacks to last closing prices, ensuring reliable decisions for S&P 500 stocks.

Market Hours Focus: Operates only during market hours (9:30 AM–4:00 PM Eastern, Monday–Friday), avoiding low-liquidity periods.

Data Persistence and Logging
SQLite Database: Tracks trade history and positions in trading_bot.db using SQLAlchemy, ensuring accurate multi-day position management for S&P 500 stocks.

CSV Logging: Logs trades in log-file-of-buy-and-sell-signals.csv and stock selection in stock_scanner.log, enabling performance analysis and strategy refinement.

Thread Safety: Uses buy_sell_lock to prevent race conditions during concurrent buy/sell operations.

Execution Efficiency
Multithreading: Employs separate threads for buying and selling, enabling simultaneous trade execution to capture S&P 500 market opportunities.

Batch Data Retrieval: Stock selection uses batch downloads with fallback to smaller batches, reducing API rate-limit risks.

Transparency and Debugging
Configurable Outputs: Flags (PRINT_STOCKS_TO_BUY, PRINT_DATABASE, DEBUG) display stock lists, technical indicators, and database contents for monitoring S&P 500 stock performance.

Stock Selection Table: Outputs a table of top stocks with metrics (score, RSI, volume ratio, etc.), enhancing transparency.

Financial Oversight: Displays cash balance and day trade counts for user awareness.

Profitability Drivers

High-Potential Stocks: The rigorous selection process identifies S&P 500 stocks with strong momentum, technical signals, and seasonal performance, increasing profitable trade likelihood.

Market Adaptability: RSI, MACD, and ATR in trading, combined with diverse indicators in selection, capture S&P 500 opportunities across sectors.

Risk Mitigation: Trailing stops, cash limits, and compliance checks reduce losses in the volatile S&P 500 market.

Automation: Multithreading and real-time data enable rapid, disciplined execution, critical for S&P 500 market movements.

Reliability: Error handling, retries, and data persistence ensure continuous operation and accurate position tracking.


***** This program will only work if you have at least 1 stock symbol in the electricity-or-utility-stocks-to-buy-list.txt because of the functionality of the python code to analyze stocks to buy at a future time. Otherwise, you will most likely see errors in the log-file-of-buy-and-sell-signals.txt. A new database file will need to be created if you started this robot without owning any stocks. Delete the database file named trading_bot.db before restarting the stockbot if the stockbot was running without any owned stock positions. Stop and Start the Stock Trading Robot after you have purchased at least 1 share of stock to create a new database file. I recommend loggin in to your stock broker's website to initially purchase at least 1 stock position. Deciding to manually sell stocks more quickly before tomorrow can also easily be done on your Broker's website for those occassional situations where your stock selling needs to be done today instead of tomorrow.

***** If you purchased or sold stocks through your stock broker's website or through other services, then the following steps must be repeated before this Stock Trading Robot can import the changes in your stock market portfolio and create a new database. The following steps should also fix the errors related to not being able to store a year-month-day for the stock position in the database; that are caused by stock trading without this stock market robot making the stock trades. ***** I have found that this stock market robot is not 100% fully initialized to sell stocks tomorrow until it has bought or sold at least 1 share of stock and the share of stock has been listed under "Trade History In This Robot's Database." Making stock trades without using this stock market robot will also cause an error of not selling your stocks tomorrow unless you perform the following steps:

 1.) Place 8 to 28 stock symbols to buy in the file named:     electricity-or-utility-stocks-to-buy-list.txt
    2.) Stop the python3 program named:      billionaire-strategy-buy-lowest-price-stock-market-robot.py
    3.) Delete the trading_bot.db file
    4.) restart this Robot with the command:      python3 billionaire-strategy-buy-lowest-price-stock-market-robot.py

    Caution: If you buy or sell stocks without using this stock market trading robot, 
    then this stock market robot will need the steps 1 thru 4, that are shown above, repeated and you will need to wait 
    an additional 24 or more hours before the stock market robot begins to be fully initialized to sell your stocks. 
    It is usually going to be an additional 24 hour waiting time unless the stocks are not in 
    a profitable price range to be sold. 

    There is a feature to allow yesterday's stocks to be sold today 
    after the trading_bot.db database file has been deleted and is brand new. 
    You set the following variable to True instead of False. The variable is: 
                 
          POSITION_DATES_AS_YESTERDAY_OPTION = False  
          
          After changing the above variable to = True, 
          you delete the files named:
          
          trading_bot_run_counter.txt and trading_bot.db
          
          Then you can sell the stocks today that 
          were purchased yesterday after deleting and creating a new trading_bot.db file.  
          Then, after running this stock robot the first time,  
          it will update the dates of the owned positions to yesterday's date.   


You can modify the python script to make DEBUG = True and this will print out your stocks with the price information. Printing out the stock information slows down this python program, and it is recommended to change debug back to:
DEBUG = False

Use a python code IDE like Pycharm to edit this python code.

This python code is currently programmed to spend less money during a stock market recession by buying only 1 to 4 shares of stock at a time per stock symbol. If you want to buy different quantities of stocks, then you can edit the python code. To buy 20 shares of stock, in the program section "def buy_stocks()", locate qty_of_one_stock = 1 and change the buy order code qty_of_one_stock to look as shown below:

qty_of_one_stock = 20

This stock market robot works best if you purchase approximately 10 to 15 different stocks at only 1 to 4 shares per stock because the stocks are sold really soon when the price is at a profitable position to sell the stock. This Stock Trading Robot has a strategy to buy stocks today for selling tomorrow because this allows for much more stock trading activity to take place within the stock trading rules of day trading 3 maximum times in 5 business days.

Any stocks purchased today will not begin to sell until tomorrow or until a future day when the stock price increases during stock market trading hours, Monday through Friday.

This is an Advanced buying and selling Python 3 Trading Robot to monitor a stock market symbol or a number of stock symbols that you place in the file "electricity-or-utility-stocks-to-buy-list.txt". Only place one stock symbol on each line.

To install:

The below install commands are ONLY for a Desktop or Laptop Computer x86_64 type of install. ***** Open a command line terminal from this folder location and type:

bash install.sh

bash install_dependencies.sh

Do the following with a non-root user account: After placing your alpaca keys at the bottom of /home/nameofyourhomefolderhere/.bashrc you simply run the command in a command terminal like:

You will need 3 command line terminals open to fully operate the Advanced Stock Market Trading Robot because one terminal window is the robot and the other 2 terminal windows are for updating the list of stocks to buy with the most successful stocks. To select different stocks to buy and allow up to 24 hours for the stocks list to update, edit the list of stocks in the file named "list-of-stock-symbols-to-scan.txt". To immediatly select different stock symbols to buy, then edit the list of stocks in the file named "electricity-or-utility-stocks-to-buy-list.txt" and also in the file named "list-of-stock-symbols-to-scan.txt".

open a command line terminal and run the following command:

python3 stock-list-writer-for-list-of-stock-symbols-to-scan.py

open a second command line terminal and run the following command:

python3 performance-stock-list-writer.py

open a third command line terminal and run the following command:

python3 billionaire-strategy-buy-lowest-price-stock-market-robot.py

The performance-stock-list-writer.py python program will make sure that only successful stocks are purchased by the Advanced Stock Market Trading Robot.

Disclaimer:

This software is not affiliated with or endorsed Alpaca Securities, LLC. It aims to be a valuable tool for stock market trading, but all trading involves risks. Use it responsibly and consider seeking advice from financial professionals.

Ready to elevate your trading game? Download the 2023 Edition of the Advanced Stock Market Trading Robot, Version 2, and get started today!

Important: Don't forget to regularly update your list of stocks to buy and keep an eye on the market conditions. Happy trading!

Remember that all trading involves risks. The ability to successfully implement these strategies depends on both market conditions and individual skills and knowledge. As such, trading should only be done with funds that you can afford to lose. Always do thorough research before making investment decisions, and consider consulting with a financial advisor. This is use at your own risk software. This software does not include any warranty or guarantees other than the useful tasks that may or may not work as intended for the software application end user. The software developer shall not be held liable for any financial losses or damages that occur as a result of using this software for any reason to the fullest extent of the law. Using this software is your agreement to these terms. This software is designed to be helpful and useful to the end user.

Place your alpaca code keys in the location: /home/name-of-your-home-folder/.bashrc Be careful to not delete the entire .bashrc file. Just add the 4 lines to the bottom of the .bashrc text file in your home folder, then save the file. .bashrc is a hidden folder because it has the dot ( . ) in front of the name. Remember that the " # " pound character will make that line unavailable. To be helpful, I will comment out the real money account for someone to begin with an account that does not risk using real money. The URL with the word "paper" does not use real money. The other URL uses real money. Making changes here requires you to reboot your computer or logout and login to apply the changes.

The 4 lines to add to the bottom of .bashrc are:

export APCA_API_KEY_ID='zxzxzxzxzxzxzxzxzxzxz'

export APCA_API_SECRET_KEY='zxzxzxzxzxzxzxzxzxzxzxzxzxzxzxzxzxzxzxzx'

#export APCA_API_BASE_URL='https://api.alpaca.markets'

export APCA_API_BASE_URL='https://paper-api.alpaca.markets'
