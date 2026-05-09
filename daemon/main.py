"""
daemon/main.py — GhostLink Node Daemon
========================================
Entry point. Orchestrates all subsystems:

  Control plane  → LoRaManager  (868 MHz, Meshtastic, GPS broadcast)
  Position       → GpsManager   (gpsd)
  Data plane     → RadioManager (GhostLink PHY or WiFi, selectable)
  Node registry  → NodeRegistry (in-memory, updated by both planes)
  REST API       → FastAPI on port 8080

Startup sequence:
  1. Load config (node.conf)
  2. Start GPS
  3. Start Radio (GhostLink or WiFi)
  4. Start LoRa (Meshtastic)
  5. Start GPS broadcast loop (LoRa, every 30s)
  6. Start stale-node cleanup loop (every 60s)
  7. Start FastAPI (uvicorn, blocking)

Usage:
  python daemon/main.py [--config /path/to/node.conf]
"""

import sys
import time
import signal
import logging
import threading
import argparse
from configparser import ConfigParser
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

# ── Imports ───────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from daemon.registry import NodeRegistry
from daemon.radio    import RadioManager
from daemon.api      import app as fastapi_app, inject as api_inject


def load_config(path: str) -> ConfigParser:
    cfg = ConfigParser()
    if not cfg.read(path):
        log.warning(f"Config not found at {path} — using defaults")
    return cfg


# ── GPS broadcast loop ────────────────────────────────────────────────────────

def gps_broadcast_loop(gps, lora, registry, cfg, stop_evt: threading.Event):
    """
    Every gps_broadcast_interval seconds:
      1. Read current position from gpsd
      2. Broadcast via LoRa (so other nodes know our position)
      3. Update our own entry in the registry
    """
    interval = cfg.getint("gps", "gps_broadcast_interval", fallback=30)
    node_id   = cfg.get("node", "node_id",   fallback="unknown")
    node_name = cfg.get("node", "node_name", fallback="GhostLink")
    mesh_ip   = cfg.get("node", "mesh_ip",   fallback="10.41.0.1")

    while not stop_evt.wait(interval):
        pos = gps.get_position() if gps else None
        if pos and lora:
            try:
                lora.send_position(pos["lat"], pos["lon"], pos.get("alt", 0))
            except Exception as e:
                log.warning(f"LoRa GPS broadcast failed: {e}")

        # Update our own node in registry
        if pos:
            registry.update_from_lora(
                node_id   = node_id,
                node_name = node_name,
                lat       = pos["lat"],
                lon       = pos["lon"],
                alt       = pos.get("alt", 0),
                rssi      = 0,
                mesh_ip   = mesh_ip,
            )


# ── Stale node cleanup ────────────────────────────────────────────────────────

def cleanup_loop(registry: NodeRegistry, stop_evt: threading.Event):
    while not stop_evt.wait(60):
        registry.remove_stale(max_age_s=300)


# ── Data plane RX handler ─────────────────────────────────────────────────────

def on_data_rx(payload: bytes, registry: NodeRegistry):
    """
    Called for every received data-plane frame.
    Parses simple heartbeat messages (JSON) and updates the registry.
    Actual video/file payloads are handled by higher layers.
    """
    import json
    try:
        msg = json.loads(payload)
        if msg.get("type") == "heartbeat":
            registry.update_from_data_plane(
                node_id       = msg["node_id"],
                radio_mode    = msg.get("radio_mode", "unknown"),
                mcs           = msg.get("mcs"),
                throughput_mbps = msg.get("throughput_mbps"),
            )
    except Exception:
        pass   # Non-JSON payload — video or other data, ignore here


# ── Heartbeat sender ──────────────────────────────────────────────────────────

def heartbeat_loop(radio: RadioManager, cfg: ConfigParser,
                   stop_evt: threading.Event):
    """Broadcast a JSON heartbeat on the data plane every 15 seconds."""
    import json
    node_id = cfg.get("node", "node_id", fallback="unknown")

    while not stop_evt.wait(15):
        status = radio.status
        msg = json.dumps({
            "type":            "heartbeat",
            "node_id":         node_id,
            "radio_mode":      status.get("mode", "unknown"),
            "mcs":             status.get("mcs"),
            "throughput_mbps": status.get("throughput_mbps"),
        }).encode()
        try:
            radio.send(msg)
        except Exception as e:
            log.warning(f"Heartbeat send failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GhostLink Node Daemon")
    parser.add_argument("--config", default="config/node.conf",
                        help="Path to node.conf")
    args = parser.parse_args()

    cfg = load_config(args.config)
    stop_evt = threading.Event()

    # ── Shutdown handler ──
    def shutdown(sig, frame):
        log.info("Shutting down...")
        stop_evt.set()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Registry ──
    registry = NodeRegistry()
    log.info("Node registry initialised")

    # ── GPS ──
    gps = None
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "control"))
        from control.gps import GpsManager
        gps = GpsManager()
        gps.connect()
        log.info("GPS connected")
    except Exception as e:
        log.warning(f"GPS unavailable: {e}")

    # ── Radio (GhostLink PHY or WiFi) ──
    radio = RadioManager(
        cfg    = cfg,
        on_rx  = lambda payload: on_data_rx(payload, registry),
    )
    radio.start()

    # ── LoRa ──
    lora = None
    try:
        from control.lora import LoraManager
        lora_port = cfg.get("lora", "lora_port", fallback="/dev/ttyAMA0")
        lora = LoraManager(port=lora_port)
        lora.connect()
        log.info(f"LoRa connected on {lora_port}")

        # Wire up incoming LoRa packets → registry
        def on_lora_rx(packet, interface):
            try:
                pos = packet.get("decoded", {}).get("position", {})
                node_id = str(packet.get("fromId", "unknown"))
                if pos.get("latitude"):
                    registry.update_from_lora(
                        node_id   = node_id,
                        node_name = packet.get("decoded", {}).get("user", {}).get("longName", node_id),
                        lat       = pos["latitude"],
                        lon       = pos["longitude"],
                        alt       = pos.get("altitude", 0),
                        rssi      = packet.get("rxRssi", 0),
                    )
            except Exception as err:
                log.debug(f"LoRa packet parse error: {err}")

        if lora.interface:
            lora.interface.localNode.onReceive = on_lora_rx

    except Exception as e:
        log.warning(f"LoRa unavailable: {e} — control plane disabled")

    # ── Video (GhostLink mode, if enabled) ──
    video = None
    if radio.mode == "ghostlink":
        try:
            from phy.video_pipe import VideoTX
            video = VideoTX(
                send_fn       = radio.send,
                width         = cfg.getint("video", "width",        fallback=1280),
                height        = cfg.getint("video", "height",       fallback=720),
                fps           = cfg.getint("video", "fps",          fallback=25),
                bitrate_kbps  = cfg.getint("video", "bitrate_kbps", fallback=6000),
            )
            if cfg.getboolean("video", "enabled", fallback=False):
                src = cfg.get("video", "source", fallback="test")
                if src == "v4l2":
                    dev = cfg.get("video", "device", fallback="/dev/video0")
                    video.start_gstreamer(f"v4l2src device={dev}")
                else:
                    video.start_gstreamer("videotestsrc pattern=ball")
                log.info("Video TX started")
        except Exception as e:
            log.warning(f"Video unavailable: {e}")

    # ── Background loops ──
    threads = [
        threading.Thread(target=gps_broadcast_loop,
                         args=(gps, lora, registry, cfg, stop_evt), daemon=True),
        threading.Thread(target=cleanup_loop,
                         args=(registry, stop_evt), daemon=True),
        threading.Thread(target=heartbeat_loop,
                         args=(radio, cfg, stop_evt), daemon=True),
    ]
    for t in threads:
        t.start()

    # ── Inject into API ──
    api_inject(registry=registry, radio=radio, gps=gps, video=video, cfg=cfg)

    # ── Start FastAPI ──
    import uvicorn
    port = cfg.getint("api", "api_port", fallback=8080)
    log.info(f"WebApp starting on http://0.0.0.0:{port}")
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
