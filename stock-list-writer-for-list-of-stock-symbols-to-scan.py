import yfinance as yf
import talib
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta
import pytz
import os
import logging
import concurrent.futures
from collections import defaultdict

# Configuration
CONFIG = {
    'timezone': 'US/Eastern',
    'historical_years': 2,  # Fetch 2 years of data
    'lookback_years': [1, 2],  # Lookback periods for technical analysis
    'seasonal_years': 2,  # Years for seasonal analysis
    'rsi_period': 14,
    'rsi_overbought': 70,
    'rsi_oversold': 30,
    'macd_fast': 12,
    'macd_slow': 26,
    'macd_signal': 9,
    'vwap_window': 14,
    'bollinger_window': 20,
    'stochastic_k': 14,
    'stochastic_d': 3,
    'adx_period': 14,
    'adx_threshold': 25,
    'min_volume_increase': 1.5,
    'min_price_increase': 0.05,
    'min_seasonal_return': 0.05,
    'batch_size_fallback': 100,  # Fallback batch size
    'max_workers': 20,  # Parallel threads
    'output_file': 'list-of-stock-symbols-to-scan.txt',
    'counter_file': 's-and-p-500-list-printer-run-counter.txt',
    'log_file': 'stock_scanner.log',
    'chart_top_n': 100,  # Number of top stocks to output
}

# Set up logging
logging.basicConfig(
    filename=CONFIG['log_file'],
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Set timezone
eastern_timezone = pytz.timezone(CONFIG['timezone'])

def fetch_sector(symbol):
    """Fetch sector for a given stock symbol."""
    try:
        ticker = yf.Ticker(symbol)
        return ticker.info.get('sector', 'Unknown')
    except Exception as e:
        logging.warning(f"Failed to fetch sector for {symbol}: {e}")
        return 'Unknown'

def batch_download_data(stocks, start_date, end_date, retries=3):
    """Batch download historical data for all stocks with fallback to smaller batches."""
    valid_stocks = []
    invalid_stocks = []
    
    for attempt in range(retries):
        try:
            data = yf.download(tickers=stocks, start=start_date, end=end_date, group_by='ticker', threads=True)
            if data.empty:
                raise ValueError("Downloaded data is empty")
            # Validate data for each stock
            for stock in stocks:
                stock_data = data[stock] if stock in data else pd.DataFrame()
                if not stock_data.empty and stock_data['Close'].dropna().size > 0:
                    valid_stocks.append(stock)
                else:
                    invalid_stocks.append(stock)
                    logging.warning(f"Stock {stock} has no valid data; marked as invalid")
            return data, valid_stocks, invalid_stocks
        except Exception as e:
            logging.error(f"Batch download failed: {e}. Attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(5 * (2 ** attempt))  # Exponential backoff

    # Fallback: Split into smaller batches
    logging.warning("Falling back to smaller batch downloads")
    all_data = {}
    valid_stocks = []
    invalid_stocks = []
    
    for i in range(0, len(stocks), CONFIG['batch_size_fallback']):
        batch_stocks = stocks[i:i + CONFIG['batch_size_fallback']]
        try:
            batch_data = yf.download(tickers=batch_stocks, start=start_date, end=end_date, group_by='ticker', threads=True)
            for stock in batch_stocks:
                stock_data = batch_data[stock] if stock in batch_data else pd.DataFrame()
                if not stock_data.empty and stock_data['Close'].dropna().size > 0:
                    all_data[stock] = stock_data
                    valid_stocks.append(stock)
                else:
                    invalid_stocks.append(stock)
                    logging.warning(f"Stock {stock} has no valid data in batch {i}")
            time.sleep(2.5)  # Sleep 2.5 seconds per batch to avoid rate limits
        except Exception as e:
            logging.error(f"Batch {i} failed: {e}")
    
    return pd.concat(all_data, axis=1, keys=all_data.keys()), valid_stocks, invalid_stocks

def validate_and_clean_data(data):
    """Validate and clean data to ensure numeric types and handle NaNs."""
    if data.empty:
        return None
    
    # Ensure required columns exist
    required_columns = ['Close', 'High', 'Low', 'Volume']
    if not all(col in data.columns for col in required_columns):
        return None
    
    # Convert to numeric, coercing errors to NaN
    data = data.copy()
    for col in required_columns:
        data[col] = pd.to_numeric(data[col], errors='coerce')
    
    # Drop rows with any NaN in required columns
    data = data.dropna(subset=required_columns)
    
    # Check if enough data remains
    if len(data) < max(CONFIG['rsi_period'], CONFIG['adx_period']):
        return None
    
    return data

def calculate_technical_indicators(data):
    """Calculate technical indicators using ta-lib on recent data."""
    if data is None or len(data) < max(CONFIG['rsi_period'], CONFIG['adx_period']):
        return None

    indicators = {}
    try:
        close = np.array(data['Close'], dtype=np.float64)
        high = np.array(data['High'], dtype=np.float64)
        low = np.array(data['Low'], dtype=np.float64)
        volume = np.array(data['Volume'], dtype=np.float64)

        # Check for invalid values
        if np.any(np.isnan(close)) or np.any(np.isnan(high)) or np.any(np.isnan(low)) or np.any(np.isnan(volume)):
            return None

        # RSI
        indicators['rsi'] = talib.RSI(close, timeperiod=CONFIG['rsi_period'])

        # MACD
        indicators['macd'], indicators['macd_signal'], _ = talib.MACD(
            close, fastperiod=CONFIG['macd_fast'], slowperiod=CONFIG['macd_slow'], signalperiod=CONFIG['macd_signal']
        )

        # VWAP
        typical_price = (high + low + close) / 3
        indicators['vwap'] = talib.SMA(typical_price * volume, timeperiod=CONFIG['vwap_window']) / talib.SMA(volume, timeperiod=CONFIG['vwap_window'])

        # Bollinger Bands
        indicators['upper_band'], indicators['middle_band'], indicators['lower_band'] = talib.BBANDS(
            close, timeperiod=CONFIG['bollinger_window']
        )

        # Stochastic Oscillator
        indicators['slowk'], indicators['slowd'] = talib.STOCH(
            high, low, close, fastk_period=CONFIG['stochastic_k'], slowk_period=CONFIG['stochastic_d'], slowd_period=CONFIG['stochastic_d']
        )

        # Volume analysis
        indicators['volume_sma'] = talib.SMA(volume, timeperiod=CONFIG['vwap_window'])
        indicators['volume'] = volume

        # Additional indicators
        indicators['adx'] = talib.ADX(high, low, close, timeperiod=CONFIG['adx_period'])
        indicators['obv'] = talib.OBV(close, volume)

        return indicators
    except Exception as e:
        logging.error(f"Error in calculate_technical_indicators: {e}")
        return None

def calculate_seasonal_return(data, current_month, current_year):
    """Calculate average return for the current month over past years."""
    seasonal_returns = []
    for y in range(1, CONFIG['seasonal_years'] + 1):
        year = current_year - y
        start = f"{year}-{current_month:02d}-01"
        next_month = datetime(year, current_month, 1) + timedelta(days=32)
        end = next_month.replace(day=1) - timedelta(days=1)
        end = end.strftime("%Y-%m-%d")
        month_data = data.loc[start:end]
        if not month_data.empty and len(month_data) > 1:
            ret = (month_data['Close'].iloc[-1] - month_data['Close'].iloc[0]) / month_data['Close'].iloc[0]
            seasonal_returns.append(ret)
    return np.mean(seasonal_returns) if seasonal_returns else 0

def calculate_historical_best_month(data, current_year):
    """Find the historical best-performing month averaged over past years."""
    monthly_returns = defaultdict(list)
    for y in range(1, CONFIG['seasonal_years'] + 1):
        year = current_year - y
        for m in range(1, 13):
            start = f"{year}-{m:02d}-01"
            next_month = datetime(year, m, 1) + timedelta(days=32)
            end = next_month.replace(day=1) - timedelta(days=1)
            end = end.strftime("%Y-%m-%d")
            month_data = data.loc[start:end]
            if not month_data.empty and len(month_data) > 1:
                ret = (month_data['Close'].iloc[-1] - month_data['Close'].iloc[0]) / month_data['Close'].iloc[0]
                monthly_returns[m].append(ret)
    avg_monthly = {m: np.mean(rets) for m, rets in monthly_returns.items() if rets}
    return max(avg_monthly, key=avg_monthly.get) if avg_monthly else None

def calculate_stock_score(stock_symbol, stock_data, years_ago, current_month, current_year):
    """Calculate composite score with technical and seasonal factors."""
    if stock_data.empty:
        return None

    # Slice data for the specified lookback period
    end_date = stock_data.index[-1]
    start_date = end_date - timedelta(days=365 * years_ago)
    recent_data = stock_data.loc[start_date:end_date]
    recent_data = validate_and_clean_data(recent_data)
    if recent_data is None:
        return None

    indicators = calculate_technical_indicators(recent_data)
    if indicators is None:
        return None

    score = 0
    latest_close = recent_data['Close'].iloc[-1]
    earliest_close = recent_data['Close'].iloc[0]
    price_increase = (latest_close - earliest_close) / earliest_close

    # Price increase score
    if price_increase >= CONFIG['min_price_increase']:
        score += price_increase * 100  # Weight price increase heavily

    # RSI score
    latest_rsi = indicators['rsi'][-1]
    if CONFIG['rsi_oversold'] < latest_rsi < CONFIG['rsi_overbought']:
        score += 20  # Neutral RSI
    elif latest_rsi <= CONFIG['rsi_oversold']:
        score += 10  # Oversold may rebound

    # MACD score
    if indicators['macd'][-1] > indicators['macd_signal'][-1]:
        score += 15  # Bullish MACD crossover

    # Volume score
    latest_volume = indicators['volume'][-1]
    avg_volume = indicators['volume_sma'][-1]
    if latest_volume >= avg_volume * CONFIG['min_volume_increase']:
        score += 15  # High volume indicates strong interest

    # VWAP score
    if latest_close > indicators['vwap'][-1]:
        score += 10  # Price above VWAP is bullish

    # Bollinger Bands score
    if latest_close > indicators['upper_band'][-1]:
        score += 10  # Breakout above upper band
    elif latest_close < indicators['lower_band'][-1]:
        score += 5   # Potential reversal from lower band

    # Stochastic score
    if indicators['slowk'][-1] > indicators['slowd'][-1] and 20 < indicators['slowk'][-1] < 80:
        score += 10  # Bullish stochastic crossover in neutral zone

    # ADX score (strong trend with bullish MACD)
    if indicators['adx'][-1] > CONFIG['adx_threshold'] and indicators['macd'][-1] > 0:
        score += 20

    # OBV score (accumulation)
    if indicators['obv'][-1] > indicators['obv'][0]:
        score += 10

    # Seasonal scores (using full historical data)
    avg_seasonal_return = calculate_seasonal_return(stock_data, current_month, current_year)
    if avg_seasonal_return > CONFIG['min_seasonal_return']:
        score += avg_seasonal_return * 100

    best_month = calculate_historical_best_month(stock_data, current_year)
    if best_month == current_month:
        score += 50  # Bonus for historical best month

    return {
        'symbol': stock_symbol,
        'score': score,
        'price_increase': price_increase,
        'rsi': latest_rsi,
        'macd_bullish': indicators['macd'][-1] > indicators['macd_signal'][-1],
        'volume_ratio': latest_volume / avg_volume if avg_volume > 0 else 0,
        'adx': indicators['adx'][-1],
        'obv_increasing': indicators['obv'][-1] > indicators['obv'][0],
        'seasonal_return': avg_seasonal_return,
        'best_month_match': best_month == current_month,
        'lookback_years': years_ago
    }

def process_stock(args):
    """Process a single stock (for parallel execution)."""
    stock_symbol, stock_data, years_ago, current_month, current_year = args
    try:
        score_data = calculate_stock_score(stock_symbol, stock_data, years_ago, current_month, current_year)
        if score_data:
            logging.info(f"Processed {stock_symbol} for {years_ago} years: Score = {score_data['score']:.2f}")
        return score_data
    except Exception as e:
        logging.error(f"Error processing {stock_symbol}: {e}")
        return None

def main():
    start_time = time.time()
    logging.info("Starting stock scanner...")

    # Updated S&P 500 stock list as of August 11, 2025
    stocks = [
        'MMM', 'AOS', 'ABT', 'ABBV', 'ACN', 'ADBE', 'AMD', 'AES', 'AFL', 'A', 'APD', 'ABNB', 'AKAM', 'ALB', 'ARE', 'ALGN', 'ALLE',
        'LNT', 'ALL', 'GOOGL', 'GOOG', 'MO', 'AMZN', 'AMCR', 'AMTM', 'AEE', 'AEP', 'AXP', 'AIG', 'AMT', 'AWK', 'AMP', 'AME', 'AMGN',
        'APH', 'ADI', 'AON', 'APA', 'AAPL', 'AMAT', 'APTV', 'ACGL', 'ADM', 'ANET', 'AJG', 'AIZ', 'T', 'ATO', 'ADSK', 'ADP',
        'AZO', 'AVB', 'AVY', 'AXON', 'BKR', 'BALL', 'BAC', 'BAX', 'BDX', 'BRK-B', 'BBY', 'TECH', 'BIIB', 'BLK', 'BX', 'BK', 'BA',
        'BKNG', 'BSX', 'BMY', 'AVGO', 'BR', 'BRO', 'BF-B', 'BLDR', 'BG', 'BXP', 'CHRW', 'CDNS', 'CZR', 'CPT', 'CPB', 'COF', 'CAH',
        'KMX', 'CCL', 'CARR', 'CAT', 'CBOE', 'CBRE', 'CDW', 'CEG', 'CF', 'CFG', 'CHD', 'CI', 'CINF', 'CTAS', 'CSCO', 'C', 'CFG', 'CLX', 'CME', 'CMS', 'KO', 'CTSH', 'CL', 'CMCSA', 'CAG',
        'COP', 'ED', 'STZ', 'COO', 'CPRT', 'GLW', 'CPAY', 'CTVA', 'CSGP', 'COST', 'CTRA', 'CRWD', 'CCI', 'CSX', 'CMI', 'CVS',
        'DHR', 'DRI', 'DVA', 'DAY', 'DECK', 'DE', 'DELL', 'DAL', 'DVN', 'DXCM', 'FANG', 'DLR', 'DG', 'DLTR', 'D', 'DPZ', 'DOV',
        'DOW', 'DHI', 'DTE', 'DUK', 'DD', 'EMN', 'ETN', 'EBAY', 'ECL', 'EIX', 'EW', 'EA', 'ELV', 'EMR', 'ENPH', 'ETR', 'EOG', 'EPAM',
        'EQT', 'EFX', 'EQIX', 'EQR', 'ERIE', 'ESS', 'EL', 'EG', 'EVRG', 'ES', 'EXC', 'EXPE', 'EXPD', 'EXR', 'XOM', 'FFIV', 'FDS',
        'FICO', 'FAST', 'FRT', 'FDX', 'FIS', 'FITB', 'FSLR', 'FE', 'FI', 'F', 'FTNT', 'FTV', 'FOXA', 'FOX', 'BEN', 'FCX',
        'GRMN', 'IT', 'GE', 'GEHC', 'GEV', 'GEN', 'GNRC', 'GD', 'GIS', 'GM', 'GPC', 'GILD', 'GPN', 'GL', 'GDDY', 'GS', 'HAL', 'HIG',
        'HAS', 'HCA', 'DOC', 'HSIC', 'HSY', 'HPE', 'HLT', 'HOLX', 'HD', 'HON', 'HRL', 'HST', 'HWM', 'HPQ', 'HUBB', 'HUM',
        'HBAN', 'HII', 'IBM', 'IEX', 'IDXX', 'ITW', 'INCY', 'IR', 'PODD', 'INTC', 'ICE', 'IFF', 'IP', 'IPG', 'INTU', 'ISRG', 'IVZ',
        'INVH', 'IQV', 'IRM', 'JBHT', 'JBL', 'JKHY', 'J', 'JNJ', 'JCI', 'JPM', 'JNPR', 'K', 'KVUE', 'KDP', 'KEY', 'KEYS', 'KMB',
        'KIM', 'KMI', 'KKR', 'KLAC', 'KHC', 'KR', 'LHX', 'LH', 'LRCX', 'LW', 'LVS', 'LDOS', 'LEN', 'LLY', 'LIN', 'LYV', 'LKQ', 'LMT',
        'L', 'LOW', 'LULU', 'LYB', 'MTB', 'MPC', 'MKTX', 'MAR', 'MMC', 'MLM', 'MAS', 'MA', 'MTCH', 'MKC', 'MCD', 'MCK', 'MDT',
        'MRK', 'META', 'MET', 'MTD', 'MGM', 'MCHP', 'MU', 'MSFT', 'MAA', 'MRNA', 'MHK', 'MOH', 'TAP', 'MDLZ', 'MPWR', 'MNST', 'MCO',
        'MS', 'MOS', 'MSI', 'MSCI', 'NDAQ', 'NTAP', 'NFLX', 'NEM', 'NWSA', 'NWS', 'NEE', 'NKE', 'NI', 'NDSN', 'NSC', 'NTRS', 'NOC',
        'NCLH', 'NRG', 'NUE', 'NVDA', 'NVR', 'NXPI', 'ORLY', 'OXY', 'ODFL', 'OMC', 'ON', 'OKE', 'ORCL', 'OTIS', 'PCAR', 'PKG', 'PLTR',
        'PANW', 'PARA', 'PH', 'PAYX', 'PAYC', 'PYPL', 'PNR', 'PEP', 'PFE', 'PCG', 'PM', 'PSX', 'PNW', 'PNC', 'POOL', 'PPG', 'PPL',
        'PFG', 'PG', 'PGR', 'PLD', 'PRU', 'PEG', 'PTC', 'PSA', 'PHM', 'QRVO', 'PWR', 'QCOM', 'DGX', 'RL', 'RJF', 'RTX', 'O', 'REG',
        'REGN', 'RF', 'RSG', 'RMD', 'RVTY', 'ROK', 'ROL', 'ROP', 'ROST', 'RCL', 'SPGI', 'CRM', 'SBAC', 'SLB', 'STX', 'SRE', 'NOW',
        'SHW', 'SPG', 'SWKS', 'SJM', 'SW', 'SNA', 'SOLV', 'SO', 'LUV', 'SWK', 'SBUX', 'STT', 'STLD', 'STE', 'SYK', 'SMCI', 'SYF',
        'SNPS', 'SYY', 'TMUS', 'TROW', 'TTWO', 'TPR', 'TRGP', 'TGT', 'TEL', 'TDY', 'TER', 'TSLA', 'TXN', 'TXT', 'TMO', 'TJX',
        'TSCO', 'TT', 'TDG', 'TRV', 'TRMB', 'TFC', 'TYL', 'TSN', 'USB', 'UBER', 'UDR', 'ULTA', 'UNP', 'UAL', 'UPS', 'URI', 'UNH',
        'UHS', 'VLO', 'VTR', 'VLTO', 'VRSN', 'VRSK', 'VZ', 'VRTX', 'VTRS', 'VICI', 'V', 'VST', 'VMC', 'WRB', 'GWW', 'WAB', 'WBA',
        'WMT', 'DIS', 'WBD', 'WM', 'WAT', 'WEC', 'WFC', 'WELL', 'WST', 'WDC', 'WY', 'WMB', 'WTW', 'WYNN', 'XEL', 'XYL', 'YUM',
        'ZBRA', 'ZBH', 'ZTS', 'TTD', 'COIN', 'DASH', 'TKO', 'WSM', 'EXE', 'APO'
    ]

    # Update run counter
    counter_file = CONFIG['counter_file']
    run_count = int(open(counter_file, 'r').read()) if os.path.exists(counter_file) else 0
    run_count += 1
    with open(counter_file, 'w') as f:
        f.write(str(run_count))

    current_time = datetime.now(eastern_timezone)
    current_month = current_time.month
    current_year = current_time.year

    # Batch download all historical data
    end_date = current_time.date()
    start_date = end_date - timedelta(days=365 * CONFIG['historical_years'])
    all_stocks_data, valid_stocks, invalid_stocks = batch_download_data(stocks, start_date, end_date)

    # Log invalid stocks
    if invalid_stocks:
        logging.info(f"Invalid stocks skipped: {', '.join(invalid_stocks)}")

    # Process stocks in parallel for each lookback period
    stock_scores = []
    for years_ago in CONFIG['lookback_years']:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG['max_workers']) as executor:
            args_list = [(stock, all_stocks_data.get(stock, pd.DataFrame()), years_ago, current_month, current_year) for stock in valid_stocks]
            future_to_stock = {executor.submit(process_stock, args): args[0] for args in args_list}
            for future in concurrent.futures.as_completed(future_to_stock):
                result = future.result()
                if result and result['score'] > 0:
                    stock_scores.append(result)

    # Add sector information to scores
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG['max_workers']) as executor:
        sector_results = {executor.submit(fetch_sector, score['symbol']): score for score in stock_scores}
        for future in concurrent.futures.as_completed(sector_results):
            score = sector_results[future]
            try:
                score['sector'] = future.result()
            except Exception as e:
                logging.error(f"Error fetching sector for {score['symbol']}: {e}")
                score['sector'] = 'Unknown'

    # Define sectors to exclude
    excluded_sectors = [
        'Energy', 'Oil & Gas', 'Natural Gas', 'Utilities', 'Electricity',
        'Basic Materials', 'Financial Services', 'Financials', 'Banks', 'Insurance',
        'Consumer Cyclical', 'Healthcare', 'Medical Devices', 'Biotechnology',
        'Pharmaceuticals', 'Real Estate', 'Consumer Defensive', 'Communication Services' 
    ]

    # Sort and select top stocks with sector limit
    df_scores = pd.DataFrame(stock_scores)
    if not df_scores.empty:
        # Group by sector, take top 25 per sector, then select top 100 overall
        top_stocks = df_scores.groupby('sector').apply(lambda x: x.nlargest(25, 'score')).reset_index(drop=True)
        # Filter out excluded sectors
        top_stocks = top_stocks[~top_stocks['sector'].isin(excluded_sectors)]
        top_stocks = top_stocks.nlargest(CONFIG['chart_top_n'], 'score').to_dict('records')
    else:
        top_stocks = []

    # Write symbols to file
    with open(CONFIG['output_file'], 'w') as f:
        for stock in top_stocks:
            f.write(f"{stock['symbol']}\n")

    # Generate and print table of top stocks
    if top_stocks:
        df = pd.DataFrame(top_stocks)
        df = df[['symbol', 'sector', 'score', 'price_increase', 'rsi', 'macd_bullish', 'volume_ratio', 'adx', 'obv_increasing', 'seasonal_return', 'best_month_match', 'lookback_years']]
        print("\nTop Performing Stocks Table:")
        print(df.to_string(index=False))

    # Log performance metrics
    elapsed_time = time.time() - start_time
    logging.info(f"Processed {len(valid_stocks)} stocks in {elapsed_time:.2f} seconds")
    logging.info(f"Selected {len(top_stocks)} top stocks")

    # Schedule next run
    next_run_time = current_time.replace(hour=16, minute=15, second=0, microsecond=0) + timedelta(days=1)
    time_difference = (next_run_time - current_time).total_seconds()
    if time_difference < 0:
        next_run_time += timedelta(days=1)
        time_difference = (next_run_time - current_time).total_seconds()

    logging.info(f"Next run scheduled at: {next_run_time}")
    print(f"Next run scheduled at: {next_run_time}")
    time.sleep(time_difference)

if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            print(f"Error occurred: {e}. Restarting in 5 minutes...")
            time.sleep(300)
