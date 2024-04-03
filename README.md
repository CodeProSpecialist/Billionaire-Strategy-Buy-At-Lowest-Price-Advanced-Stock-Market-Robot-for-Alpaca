# Billionaire-Strategy-Buy-At-Lowest-Price-Advanced-Stock-Market-Robot-for-Alpaca
This is the Billionaire Strategy for Buying Great Stocks at the Lowest Price becuase you can never expect that the Sell Price is a profit, although you have the most control over the Buy Price. 

***************************************************************************************
This is my Original, Most Successful Advanced Stock Market Trading Robot that I brought back from September 2023 since it was even more profitable 
with its buy at low price strategy. 

***************************************************************************************

***** This program will only work if you have at least 1 stock symbol in the electricity-or-utility-stocks-to-buy-list.txt because of the functionality of the python code to analyze stocks to buy at a future time. Otherwise, you will most likely see errors in the log-file-of-buy-and-sell-signals.txt. A new database file will need to be created if you started this robot without owning any stocks. Delete the database file named trading_bot.db before restarting the stockbot if the stockbot was running without any owned stock positions. Stop and Start the Stock Trading Robot after you have purchased at least 1 share of stock to create a new database file. I recommend linking your Alpaca Stock Trading Account with TradingView to purchase at least 1 stock position. Deciding to manually sell stocks more quickly before tomorrow can also easily be done on TradingView or on the Alpaca website for those occassional situations where your stock selling needs to be done today instead of tomorrow.

***** If you purchased or sold stocks through Alpaca's website or through other services, then the following steps must be repeated before this Stock Trading Robot can import the changes in your stock market portfolio and create a new database. The following steps should also fix the errors related to not being able to store a year-month-day for the stock position in the database; that are caused by stock trading without this stock market robot making the stock trades. ***** I have found that this stock market robot is not 100% fully initialized to sell stocks tomorrow until it has bought or sold at least 1 share of stock and the share of stock has been listed under "Trade History In This Robot's Database." Making stock trades without using this stock market robot will also cause an error of not selling your stocks tomorrow unless you perform the following steps:

 1.) Place 8 to 28 stock symbols to buy in the file named:     electricity-or-utility-stocks-to-buy-list.txt
    2.) Stop the python3 program named:      buy-and-automatically-sell-for-a-profit-robot.py
    3.) Delete the trading_bot.db file
    4.) restart this Robot with the command:      python3 buy-and-automatically-sell-for-a-profit-robot.py

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

You should be the root user when installing the Python software. ***** The below install commands are ONLY for a Desktop or Laptop Computer x86_64 type of install. ***** Open a command line terminal from this folder location and type:

sh install.sh

Do the following with a non-root user account: After placing your alpaca keys at the bottom of /home/nameofyourhomefolderhere/.bashrc you simply run the command in a command terminal like:

You will need 2 command line terminals open to fully operate the Advanced Stock Market Trading Robot because one terminal window is the robot and the other terminal window is for updating the list of stocks to buy with the most successful energy or electric utility company stocks. To select different stocks to buy and allow up to 24 hours for the stocks list to update, edit the list of stocks in the file named "list-of-stock-symbols-to-scan.txt". To immediatly select different stock symbols to buy, then edit the list of stocks in the file named "electricity-or-utility-stocks-to-buy-list.txt" and also in the file named "list-of-stock-symbols-to-scan.txt".

python3 buy-and-automatically-sell-for-a-profit-robot.py

open a second command line terminal and run the following command:

python3 performance-stock-list-writer.py

The performance-stock-list-writer.py python program will make sure that only successful stocks are purchased by the Advanced Stock Market Trading Robot.

Disclaimer:

This software is not affiliated with or endorsed by TradingView or Alpaca Securities, LLC. It aims to be a valuable tool for stock market trading, but all trading involves risks. Use it responsibly and consider seeking advice from financial professionals.

Ready to elevate your trading game? Download the 2023 Edition of the Advanced Stock Market Trading Robot, Version 2, and get started today!

Important: Don't forget to regularly update your list of stocks to buy and keep an eye on the market conditions. Happy trading!

Remember that all trading involves risks. The ability to successfully implement these strategies depends on both market conditions and individual skills and knowledge. As such, trading should only be done with funds that you can afford to lose. Always do thorough research before making investment decisions, and consider consulting with a financial advisor. This is use at your own risk software. This software does not include any warranty or guarantees other than the useful tasks that may or may not work as intended for the software application end user. The software developer shall not be held liable for any financial losses or damages that occur as a result of using this software for any reason to the fullest extent of the law. Using this software is your agreement to these terms. This software is designed to be helpful and useful to the end user.

Place your alpaca code keys in the location: /home/name-of-your-home-folder/.bashrc Be careful to not delete the entire .bashrc file. Just add the 4 lines to the bottom of the .bashrc text file in your home folder, then save the file. .bashrc is a hidden folder because it has the dot ( . ) in front of the name. Remember that the " # " pound character will make that line unavailable. To be helpful, I will comment out the real money account for someone to begin with an account that does not risk using real money. The URL with the word "paper" does not use real money. The other URL uses real money. Making changes here requires you to reboot your computer or logout and login to apply the changes.

The 4 lines to add to the bottom of .bashrc are:

export APCA_API_KEY_ID='zxzxzxzxzxzxzxzxzxzxz'

export APCA_API_SECRET_KEY='zxzxzxzxzxzxzxzxzxzxzxzxzxzxzxzxzxzxzxzx'

#export APCA_API_BASE_URL='https://api.alpaca.markets'

export APCA_API_BASE_URL='https://paper-api.alpaca.markets'
