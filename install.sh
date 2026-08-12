#!/bin/bash
# =============================================================================
# Unified installer for the billionaire-strategy trading bot.
# Merges the two previous scripts (install.sh + install_dependencies.sh) into
# one, and adds the packages the new ML brain requires (tensorflow, schedule,
# pandas_market_calendars, pandas). Run once on a fresh Ubuntu machine.
#
# Usage: sudo ./install.sh
# =============================================================================

set -e   # exit immediately if any command fails

echo "=========================================="
echo "  Trading bot installer -- starting up"
echo "=========================================="

# ---------------------------------------------------------------------------
# Step 1: System packages -- build tools and libraries TA-Lib needs to compile
# ---------------------------------------------------------------------------
echo ""
echo "[1/6] Installing system packages (build tools, python3.12, ssl/curl libs)..."
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

# ---------------------------------------------------------------------------
# Step 2: TA-Lib -- built from source since there's no working apt package.
# This has to happen BEFORE `pip install TA-Lib`, because the Python TA-Lib
# wheel is a thin C-extension binding that needs the underlying libta_lib.so
# already installed system-wide.
# ---------------------------------------------------------------------------
echo ""
echo "[2/6] Building and installing TA-Lib 0.6.4 from source..."

# Skip the download+build if the library is already installed from a previous run
if [ -f /usr/lib/libta_lib.so ] || [ -f /usr/local/lib/libta_lib.so ]; then
    echo "  TA-Lib library already installed; skipping rebuild."
else
    # Work in /tmp so we don't leave archive files in the user's project dir
    cd /tmp
    wget -q https://github.com/TA-Lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz
    tar -xzf ta-lib-0.6.4-src.tar.gz
    cd ta-lib-0.6.4

    if [ -f autogen.sh ]; then
        chmod +x autogen.sh
        ./autogen.sh
    fi

    ./configure --prefix=/usr
    make
    sudo make install
    sudo ldconfig

    # Verify
    if [ ! -f /usr/lib/libta_lib.so ] && [ ! -f /usr/local/lib/libta_lib.so ]; then
        echo "ERROR: TA-Lib build finished but libta_lib.so is missing." >&2
        exit 1
    fi

    # Clean up the source tree
    cd /tmp
    rm -rf ta-lib-0.6.4 ta-lib-0.6.4-src.tar.gz
    echo "  TA-Lib installed successfully."
fi

# ---------------------------------------------------------------------------
# Step 3: Python packages -- everything the bot imports. numpy first because
# TA-Lib's Python binding compiles against it during install.
# ---------------------------------------------------------------------------
echo ""
echo "[3/6] Installing core Python dependencies..."
pip3 install --no-cache-dir --upgrade pip
pip3 install --no-cache-dir numpy
pip3 install --no-cache-dir TA-Lib==0.6.4

echo ""
echo "[4/6] Installing trading, data, and scheduling packages..."
# Grouped by role so a failure is easy to diagnose. Order within each group
# doesn't matter; between groups it does (e.g. pandas before pandas_market_calendars).
pip3 install --no-cache-dir \
    pandas \
    yfinance \
    alpaca-trade-api \
    pytz \
    sqlalchemy \
    ratelimit \
    schedule \
    pandas_market_calendars

# ---------------------------------------------------------------------------
# Step 4: TensorFlow -- required for the ML brain. CPU-only build unless the
# machine has a working CUDA install; the ML brain runs comfortably on CPU
# for the daily 15,000-example training runs described in the bot's schedule.
# TensorFlow's install is large (~500MB) and slow (~1-2 minutes), so it's
# separated out to make progress obvious.
# ---------------------------------------------------------------------------
echo ""
echo "[5/6] Installing TensorFlow (large download; this can take a minute or two)..."
pip3 install --no-cache-dir tensorflow

# ---------------------------------------------------------------------------
# Step 5: Verify everything imports cleanly. Fails loudly if anything's broken
# so problems surface here, not during the bot's first live trading cycle.
# ---------------------------------------------------------------------------
echo ""
echo "[6/6] Verifying imports..."
python3 - <<'PY_VERIFY'
import sys
errors = []

def check(name, import_stmt):
    try:
        exec(import_stmt, {})
        print(f"  OK   {name}")
    except Exception as e:
        errors.append(f"{name}: {e}")
        print(f"  FAIL {name}: {e}")

check("talib",                    "import talib")
check("numpy",                    "import numpy")
check("pandas",                   "import pandas")
check("yfinance",                 "import yfinance")
check("alpaca_trade_api",         "import alpaca_trade_api")
check("pytz",                     "import pytz")
check("sqlalchemy",               "import sqlalchemy")
check("ratelimit",                "import ratelimit")
check("schedule",                 "import schedule")
check("pandas_market_calendars",  "import pandas_market_calendars")
check("tensorflow",               "import tensorflow")

if errors:
    print("")
    print(f"ERROR: {len(errors)} package(s) failed to import:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)

print("")
print("All imports verified successfully.")
PY_VERIFY

echo ""
echo "=========================================="
echo "  Install complete."
echo ""
echo "  Set your Alpaca credentials before running the bot:"
echo "    export APCA_API_KEY_ID=your_key_here"
echo "    export APCA_API_SECRET_KEY=your_secret_here"
echo "    export APCA_API_BASE_URL=https://paper-api.alpaca.markets"
echo ""
echo "  Then run:"
echo "    python3 billionaire-strategy-buy-lowest-price-stock-market-robot.py"
echo "=========================================="
