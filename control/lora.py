# GATONET Node Daemon — LoRa / Meshtastic Interface
# Handles node discovery, GPS broadcasts, signaling, fallback messaging

import meshtastic
import meshtastic.serial_interface

class LoraManager:
    def __init__(self, port: str):
        self.port = port
        self.interface = None

    def connect(self):
        self.interface = meshtastic.serial_interface.SerialInterface(self.port)

    def send_position(self, lat: float, lon: float, alt: float = 0):
        # TODO: broadcast GPS position over LoRa mesh
        pass

    def send_message(self, text: str):
        if self.interface:
            self.interface.sendText(text)

    def on_receive(self, packet, interface):
        # TODO: handle incoming LoRa packets
        pass
