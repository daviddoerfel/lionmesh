#!/bin/bash
# GATONET — Step 2: WiFi mesh setup (802.11s + batman-adv)
# Requires node.conf to be present in /etc/gatonet/node.conf

set -e

CONF=/etc/gatonet/node.conf

if [ ! -f "$CONF" ]; then
    echo "[ERROR] node.conf not found at $CONF"
    exit 1
fi

# Read config values
IFACE=$(grep "wifi_interface" $CONF | cut -d= -f2 | tr -d ' ')
SSID=$(grep "mesh_ssid" $CONF | cut -d= -f2 | tr -d ' ')
CHANNEL=$(grep "mesh_channel_2g" $CONF | cut -d= -f2 | tr -d ' ')
MESH_IP=$(grep "mesh_ip" $CONF | cut -d= -f2 | tr -d ' ')

echo "[GATONET] Configuring $IFACE for 802.11s mesh..."

# Bring interface down
ip link set $IFACE down
iw dev $IFACE set type mesh

# Create mesh configuration
cat > /etc/gatonet/mesh.conf << MESHEOF
network={
    ssid="$SSID"
    mode=5
    frequency=2437
    key_mgmt=NONE
}
MESHEOF

echo "[GATONET] Setting up batman-adv on $IFACE..."
ip link set $IFACE up
batctl if add $IFACE
ip link set bat0 up
ip addr add $MESH_IP/16 dev bat0

echo "[GATONET] WiFi mesh setup complete."
