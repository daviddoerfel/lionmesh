"""
daemon/radio.py — Radio Manager
=================================
Abstraction layer between the daemon and the actual radio hardware.

Supports two modes, selected via node.conf [radio] mode:

  wifi
    Standard 802.11 via batman-adv (Alfa adapter or built-in WiFi).
    No LimeSDR required. Mesh routing handled by the OS (batctl).
    Video: GStreamer pipeline uses the batman-adv IP mesh directly.

  lionmesh
    LionMesh OFDM PHY via LimeSDR (Mini 2 / XTRX).
    Custom PHY/MAC stack: phy_ofdm + mac_simple + xtrx_radio.
    Video: routed over the LionMesh MAC datagram interface.
    Falls back to simulation mode if no hardware is detected.

The daemon calls the same RadioManager API regardless of mode.
Switching modes requires editing node.conf and restarting.
"""

import threading
import logging
from typing import Optional, Callable
from configparser import ConfigParser

log = logging.getLogger("radio")


class RadioManager:
    """
    Unified radio interface.

    Parameters
    ----------
    cfg : ConfigParser
        Parsed node.conf. Reads [radio] section.
    on_rx : callable
        Called with (payload: bytes) for every received MAC frame.
        Used by the daemon to handle incoming data-plane messages.
    """

    def __init__(self, cfg: ConfigParser,
                 on_rx: Callable[[bytes], None]):
        self._cfg   = cfg
        self._on_rx = on_rx
        self._mode  = cfg.get("radio", "mode", fallback="wifi").strip().lower()
        self._mac   = None    # LionMesh MACLayer (lionmesh mode only)
        self._radio = None    # XTRXRadio (lionmesh mode only)
        self._running = False

        log.info(f"Radio mode: {self._mode}")

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the radio subsystem."""
        self._running = True
        if self._mode == "lionmesh":
            self._start_lionmesh()
        else:
            self._start_wifi()

    def stop(self) -> None:
        self._running = False
        if self._radio:
            try:
                self._radio.stop()
            except Exception:
                pass

    def send(self, payload: bytes, dst: int = 0xFFFF) -> None:
        """
        Send data-plane payload.
          lionmesh mode: via LionMesh MAC datagram (fire-and-forget)
          wifi mode:      UDP broadcast on batman-adv mesh interface
        """
        if self._mode == "lionmesh" and self._mac:
            self._mac.send_datagram(payload, dst=dst)
        elif self._mode == "wifi":
            self._send_udp(payload)

    def send_reliable(self, payload: bytes, dst: int = 0xFFFF) -> bool:
        """
        Reliable send with ACK (lionmesh mode only).
        Falls back to best-effort send in wifi mode.
        """
        if self._mode == "lionmesh" and self._mac:
            return self._mac.send_reliable(payload, dst=dst)
        self.send(payload, dst=dst)
        return True   # wifi: assume delivery

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def status(self) -> dict:
        """Current radio status for the API /status endpoint."""
        if self._mode == "lionmesh" and self._mac:
            st = self._mac.get_status()
            return {
                "mode":         "lionmesh",
                "mcs":          int(st["mcs"]),
                "throughput_mbps": st["mbps_est"],
                "tx_queue":     st["tx_queue"],
            }
        return {"mode": "wifi"}

    # ── LionMesh mode ─────────────────────────────────────────────────────────

    def _start_lionmesh(self) -> None:
        """Initialise LionMesh PHY + MAC via SoapySDR."""
        try:
            import sys, os
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phy'))

            from phy_ofdm import set_bandwidth, MCS
            from mac_simple import MACLayer, MACConfig
            from xtrx_radio import XTRXRadio, XTRXConfig

            bw  = self._cfg.getfloat("radio", "bandwidth_hz", fallback=5e6)
            mcs = self._cfg.getint("radio",   "mcs",          fallback=4)
            set_bandwidth(bw)

            radio_cfg = XTRXConfig(
                freq_hz      = self._cfg.getfloat("radio", "freq_hz",      fallback=863e6),
                bandwidth_hz = bw,
                tx_gain_db   = self._cfg.getfloat("radio", "tx_gain_db",   fallback=50.0),
                rx_gain_db   = self._cfg.getfloat("radio", "rx_gain_db",   fallback=40.0),
                device_args  = self._cfg.get("radio", "device_args",       fallback="driver=lime"),
            )

            self._radio = XTRXRadio(radio_cfg, rx_callback=self._lionmesh_rx)

            mac_cfg = MACConfig(
                node_addr = self._node_addr(),
                video_mcs = MCS(mcs),
                data_mcs  = MCS(max(0, mcs - 2)),
            )
            self._mac = MACLayer(mac_cfg,
                                 tx_fn  = self._radio.transmit,
                                 on_rx  = self._on_rx)
            self._radio.start()
            log.info(f"LionMesh PHY started: {bw/1e6:.0f} MHz BW, MCS{mcs}")

        except Exception as e:
            log.error(f"LionMesh init failed: {e} — falling back to wifi mode")
            self._mode = "wifi"
            self._start_wifi()

    def _lionmesh_rx(self, iq: "np.ndarray") -> None:  # type: ignore
        """IQ samples from XTRX → MAC layer."""
        if self._mac:
            self._mac.rx_push(iq)

    # ── WiFi mode ──────────────────────────────────────────────────────────────

    def _start_wifi(self) -> None:
        """Start UDP listener on batman-adv mesh interface."""
        import socket
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._udp_sock.bind(("0.0.0.0", 5700))
        self._udp_sock.settimeout(1.0)
        t = threading.Thread(target=self._udp_worker, daemon=True)
        t.start()
        log.info("WiFi mode: UDP listener on port 5700")

    def _udp_worker(self) -> None:
        while self._running:
            try:
                data, _ = self._udp_sock.recvfrom(8192)
                self._on_rx(data)
            except Exception:
                pass

    def _send_udp(self, payload: bytes) -> None:
        try:
            mesh_net = self._cfg.get("node", "mesh_network",
                                     fallback="10.41.0.0/16")
            # Broadcast to mesh subnet
            bcast = mesh_net.rsplit(".", 1)[0] + ".255"
            self._udp_sock.sendto(payload, (bcast, 5700))
        except Exception as e:
            log.warning(f"UDP send failed: {e}")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _node_addr(self) -> int:
        """Derive a 16-bit MAC address from the mesh IP last octet."""
        try:
            ip = self._cfg.get("node", "mesh_ip", fallback="10.41.0.1")
            return int(ip.split(".")[-1])
        except Exception:
            return 0x0001
