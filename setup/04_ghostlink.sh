#!/bin/bash
# setup/04_lionmesh.sh
# LionMesh PHY installation script
# Run AFTER 01_system.sh, 02_mesh_wifi.sh, 03_lora_meshtastic.sh
#
# Installs:
#   - Python dependencies (numpy, scipy, fastapi, uvicorn)
#   - SoapySDR + LimeSDR driver (optional, skip if wifi mode)
#   - GStreamer H.265 pipeline (optional, for video TX)
#   - lionmesh systemd service

set -e

INSTALL_DIR="/opt/lionmesh"
CONFIG_DIR="/etc/lionmesh"
SERVICE_FILE="/etc/systemd/system/lionmesh.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== LionMesh Setup ==="
echo "Repo:   $REPO_DIR"
echo "Install: $INSTALL_DIR"
echo ""

# ── Python dependencies ───────────────────────────────────────────────────────
echo "[1/5] Installing Python dependencies..."
pip3 install --break-system-packages \
    numpy scipy \
    fastapi uvicorn[standard] \
    jinja2 python-multipart \
    gpsd-py3 \
    meshtastic \
    pyserial

# ── SoapySDR + LimeSDR (optional) ────────────────────────────────────────────
echo ""
read -r -p "[2/5] Install LimeSDR/SoapySDR drivers? (y/N for wifi-only mode): " answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
    apt-get install -y \
        soapysdr-tools \
        python3-soapysdr \
        soapysdr-module-lms7 \
        || echo "  Note: soapysdr-module-xtrx install manually for XTRX"
    echo "  SoapySDR installed. Test with: SoapySDRUtil --probe"
else
    echo "  Skipping LimeSDR drivers. Radio mode will be: wifi"
fi

# ── GStreamer H.265 (optional) ────────────────────────────────────────────────
echo ""
read -r -p "[3/5] Install GStreamer H.265 video pipeline? (y/N): " answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
    apt-get install -y \
        python3-gst-1.0 \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-libav \
        gstreamer1.0-x \
        v4l-utils
    echo "  GStreamer installed."
else
    echo "  Skipping GStreamer. Video TX will be unavailable."
fi

# ── Install LionMesh ─────────────────────────────────────────────────────────
echo ""
echo "[4/5] Installing LionMesh to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r "$REPO_DIR"/* "$INSTALL_DIR/"

# Config
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/node.conf" ]; then
    cp "$REPO_DIR/config/node.conf.example" "$CONFIG_DIR/node.conf"
    echo "  Config created at $CONFIG_DIR/node.conf — edit before starting!"
else
    echo "  Config already exists at $CONFIG_DIR/node.conf — skipping"
fi

# ── Systemd service ───────────────────────────────────────────────────────────
echo ""
echo "[5/5] Installing systemd service..."

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=LionMesh Node Daemon
After=network.target gpsd.service
Wants=gpsd.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 $INSTALL_DIR/daemon/main.py --config $CONFIG_DIR/node.conf
WorkingDirectory=$INSTALL_DIR
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable lionmesh
echo "  Service installed. Start with: sudo systemctl start lionmesh"
echo "  Logs: sudo journalctl -u lionmesh -f"

echo ""
echo "=== LionMesh Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $CONFIG_DIR/node.conf"
echo "     - Set node_id, node_name, mesh_ip"
echo "     - Set [radio] mode = wifi OR lionmesh"
echo "     - If lionmesh: set freq_hz, bandwidth_hz, mcs"
echo "  2. sudo systemctl start lionmesh"
echo "  3. Open http://$(hostname -I | awk '{print $1}'):8080"
