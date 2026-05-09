# GATONET Node Daemon — WiFi Mesh Manager
# Handles batman-adv status monitoring and band switching

import subprocess

def get_mesh_neighbors():
    """Return current batman-adv neighbor list."""
    try:
        result = subprocess.run(["batctl", "n"], capture_output=True, text=True)
        return result.stdout
    except Exception as e:
        return str(e)

def get_mesh_topology():
    """Return current batman-adv routing table."""
    try:
        result = subprocess.run(["batctl", "o"], capture_output=True, text=True)
        return result.stdout
    except Exception as e:
        return str(e)

def switch_band(interface: str, band: str):
    """Switch WiFi band. band = '2g' or '5g'"""
    # TODO: implement band switching logic
    pass
