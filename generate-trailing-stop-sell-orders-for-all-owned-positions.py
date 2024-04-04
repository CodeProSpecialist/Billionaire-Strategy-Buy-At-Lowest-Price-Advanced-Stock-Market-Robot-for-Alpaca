import os
import alpaca_trade_api as tradeapi

# Load environment variables for Alpaca API
APIKEYID = os.getenv('APCA_API_KEY_ID')
APISECRETKEY = os.getenv('APCA_API_SECRET_KEY')
APIBASEURL = os.getenv('APCA_API_BASE_URL')

# Initialize the Alpaca API
api = tradeapi.REST(APIKEYID, APISECRETKEY, APIBASEURL)

def place_trailing_stop_sell_order(symbol, sell_quantity, current_price):
    try:
        stop_loss_percent = 1.00   # You can adjust this percentage based on your strategy
        stop_loss_price = current_price * (1 - stop_loss_percent / 100)

        stop_order = api.submit_order(
            symbol=symbol,
            qty=int(sell_quantity),  # Convert to whole number
            side='sell',
            type='trailing_stop',
            trail_percent=stop_loss_percent,
            time_in_force='gtc'
        )

        print(f"Placed trailing stop sell order for {int(sell_quantity)} shares of {symbol} at {stop_loss_price}")
        return stop_order.id

    except Exception as e:
        print(f"Error placing trailing stop sell order for {symbol}: {str(e)}")
        return None

def main():
    # Get the list of owned positions
    positions = api.list_positions()

    for position in positions:
        symbol = position.symbol
        sell_quantity = float(position.qty)  # Convert to float first
        current_price = float(position.current_price)

        # Check if there are existing sell orders for the current position
        #existing_sell_orders = api.list_orders(symbol=symbol, side='sell', status='open')
        #if not existing_sell_orders:
            # No existing sell orders, proceed with placing trailing stop sell order
        stop_order_id = place_trailing_stop_sell_order(symbol, sell_quantity, current_price)
        if stop_order_id:
            print(f"Trailing stop sell order placed for {symbol} with ID: {stop_order_id}")

if __name__ == "__main__":
    main()
