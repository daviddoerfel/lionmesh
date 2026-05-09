#!/bin/bash
# GATONET — Step 1: System preparation
# Run as root on a fresh Raspberry Pi OS Lite (64-bit)

set -e

echo "[GATONET] Updating system..."
apt-get update && apt-get upgrade -y

echo "[GATONET] Installing dependencies..."
apt-get install -y \
    python3 python3-pip python3-venv \
    gpsd gpsd-clients \
    iw wireless-tools \
    batctl \
    git curl wget \
    hostapd \
    net-tools \
    build-essential

echo "[GATONET] Enabling SPI for WM1302..."
raspi-config nonint do_spi 0

echo "[GATONET] Enabling UART for GPS..."
raspi-config nonint do_serial_hw 0
raspi-config nonint do_serial_cons 1

echo "[GATONET] Loading batman-adv kernel module..."
modprobe batman-adv
echo "batman-adv" >> /etc/modules

echo "[GATONET] System preparation complete. Please reboot."
