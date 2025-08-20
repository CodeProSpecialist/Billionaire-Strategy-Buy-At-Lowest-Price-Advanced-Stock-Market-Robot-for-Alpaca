#!/bin/bash

# Exit on any error
set -e

# Step 1: Update package lists and install essential build tools and dependencies
echo "Installing build tools and dependencies..."
sudo apt-get update
sudo apt-get install -y \
    build-essential \
    wget \
    git \
    automake \
    autoconf \
    libtool \
    python3.12 \
    python3.12-dev \
    python3-pip \
    libcurl4-openssl-dev \
    libssl-dev \
    zlib1g-dev

# Step 2: Download TA-Lib 0.6.4 source
echo "Downloading TA-Lib 0.6.4..."
wget https://github.com/TA-Lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz

# Step 3: Extract and build TA-Lib
echo "Extracting and building TA-Lib..."
tar -xzf ta-lib-0.6.4-src.tar.gz
cd ta-lib-0.6.4

# Check if autogen.sh exists before running it
if [ -f autogen.sh ]; then
    echo "Running autogen.sh..."
    chmod +x autogen.sh
    ./autogen.sh
else
    echo "autogen.sh not found, proceeding without it..."
fi

# Run configure and build
./configure --prefix=/usr
make
sudo make install

# Step 4: Update linker cache
echo "Updating linker cache..."
sudo ldconfig

# Step 5: Verify TA-Lib installation
if [ -f /usr/lib/libta_lib.so ]; then
    echo "TA-Lib library installed successfully at /usr/lib/libta_lib.so"
else
    echo "Error: TA-Lib library not found at /usr/lib/libta_lib.so"
    exit 1
fi

# Step 6: Install Python dependencies
echo "Installing Python dependencies..."
pip3 install --no-cache-dir numpy
pip3 install --no-cache-dir TA-Lib==0.6.4
pip3 install --no-cache-dir alpaca-trade-api pytz yfinance sqlalchemy ratelimit

# Step 7: Verify Python TA-Lib installation
echo "Verifying Python TA-Lib installation..."
python3 -c "import talib; print('TA-Lib version:', talib.__version__)" || {
    echo "Error: Python TA-Lib installation failed"
    exit 1
}

# Step 8: Verify other Python dependencies
echo "Verifying other Python dependencies..."
python3 -c "import alpaca_trade_api, pytz, yfinance, sqlalchemy; print('All additional dependencies imported successfully')" || {
    echo "Error: One or more Python dependencies failed to install"
    exit 1
}

# Step 9: Clean up
echo "Cleaning up..."
cd ..
rm -rf ta-lib-0.6.4 ta-lib-0.6.4-src.tar.gz

echo "TA-Lib and all Python dependencies installed successfully!"
