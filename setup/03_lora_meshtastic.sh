#!/bin/bash
# GATONET — Step 3: LoRa / Meshtastic setup

set -e

echo "[GATONET] Installing Meshtastic Python library..."
pip3 install meshtastic --break-system-packages

echo "[GATONET] Configuring GPSD..."
cat > /etc/default/gpsd << GPSEOF
DEVICES="/dev/ttyAMA0"
GPSD_OPTIONS="-n"
START_DAEMON="true"
USBAUTO="false"
GPSEOF

systemctl enable gpsd
systemctl start gpsd

echo "[GATONET] LoRa / Meshtastic setup complete."
