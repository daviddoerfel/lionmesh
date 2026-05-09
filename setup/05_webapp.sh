#!/bin/bash
# GATONET — Step 5: WebApp setup
# The webapp is served directly by the node daemon (FastAPI + Jinja2)
# This script just verifies the setup and opens the firewall port

set -e

echo "[GATONET] Configuring firewall for WebApp..."
# Allow port 8080 if ufw is active
if command -v ufw &> /dev/null; then
    ufw allow 8080/tcp
fi

echo "[GATONET] WebApp will be available at http://$(hostname -I | awk '{print $1}'):8080"
echo "[GATONET] WebApp setup complete."
