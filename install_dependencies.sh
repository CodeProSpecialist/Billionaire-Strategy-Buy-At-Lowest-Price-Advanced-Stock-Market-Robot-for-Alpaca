#!/bin/bash

# Install required Python packages for the trading bot
echo "Installing Python dependencies using pip3..."

# List of packages to install
packages=(
    schedule
    alpaca-trade-api
    pytz
    numpy
    TA-Lib
    yfinance
    sqlalchemy
    ratelimit
    pandas_market_calendars
)

# Loop through each package and install it
for package in "${packages[@]}"; do
    echo "Installing $package..."
    pip3 install $package
    if [ $? -eq 0 ]; then
        echo "$package installed successfully."
    else
        echo "Failed to install $package."
        exit 1
    fi
done

echo "All dependencies installed successfully."
