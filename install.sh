#!/bin/bash

echo "Starting setup..."

# Check if python3 is installed
if ! command -v python3 &> /dev/null
then
    echo "Python3 could not be found. Attempting to install..."
    sudo apt update
    sudo apt install -y python3-all
fi

# Check if pip3 is installed
if ! command -v pip3 &> /dev/null
then
    echo "pip3 could not be found. Attempting to install..."
    sudo apt update
    sudo apt install -y python3-pip

    # Install Python packages
    echo "Installing Python packages..."

    pip3 install yfinance alpaca-trade-api sqlalchemy pytz ta-lib schedule

fi

# Install TA-Lib dependencies
echo "Installing TA-Lib dependencies ..."

sudo apt-get install libatlas-base-dev gfortran -y

# Download and install TA-Lib
echo "Downloading TA-Lib..."
wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz
tar -xzvf ta-lib-0.4.0-src.tar.gz
cd ta-lib/
echo "Configuring TA-Lib..."
./configure --prefix=/usr
echo "Installing TA-Lib..."
make
sudo make install

cd ..
rm -r ta-lib
rm ta-lib-0.4.0-src.tar.gz

echo "All done! You can now run your Python script."

# Inform the user about the virtual environment
echo "Your Python commands in the directory named My-Python-Virtual-Environment-Packages will be the Python commands that run this installed virtual environment's Python programs."

echo "just open the folder named My-Python-Virtual-Environment-Packages/venv/bin and run pip3 to install anything else with the command ./pip3 and then run python3 with the command ./python3  "
