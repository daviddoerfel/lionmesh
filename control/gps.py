# GATONET Node Daemon — GPS Manager
# Reads position from gpsd and provides it to LoRa and WebApp

import gpsd

class GpsManager:
    def __init__(self):
        self.connected = False

    def connect(self):
        gpsd.connect()
        self.connected = True

    def get_position(self):
        if not self.connected:
            return None
        try:
            packet = gpsd.get_current()
            return {
                "lat": packet.lat,
                "lon": packet.lon,
                "alt": packet.alt,
                "speed": packet.speed(),
                "time": packet.time,
            }
        except Exception:
            return None
