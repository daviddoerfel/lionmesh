"""
daemon/registry.py — Node Registry
====================================
Central in-memory store for all known nodes in the mesh.

A node entry is created/updated from two sources:
  - LoRa control plane  → position, discovery, last-seen
  - LionMesh data plane → link quality, MCS, throughput

Both sources write to the same NodeEntry. The API and WebApp
read from here to render the Leaflet map and node list.
"""

import time
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class NodeEntry:
    # Identity
    node_id:   str  = ""
    node_name: str  = ""
    mesh_ip:   str  = ""

    # Position (from LoRa GPS broadcast)
    lat:  Optional[float] = None
    lon:  Optional[float] = None
    alt:  Optional[float] = None

    # Control plane
    lora_rssi:     Optional[int]   = None
    last_lora_seen: Optional[float] = None   # epoch seconds

    # Data plane (LionMesh or WiFi)
    radio_mode:    str            = "unknown"   # "lionmesh" or "wifi"
    mcs:           Optional[int]  = None
    throughput_mbps: Optional[float] = None
    wifi_rssi:     Optional[int]  = None
    last_data_seen: Optional[float] = None

    @property
    def online(self) -> bool:
        """Node is considered online if seen on either plane within 90 seconds."""
        now = time.time()
        lora_ok = self.last_lora_seen and (now - self.last_lora_seen) < 90
        data_ok = self.last_data_seen and (now - self.last_data_seen) < 90
        return bool(lora_ok or data_ok)

    def to_dict(self) -> dict:
        return {
            "node_id":        self.node_id,
            "node_name":      self.node_name,
            "mesh_ip":        self.mesh_ip,
            "lat":            self.lat,
            "lon":            self.lon,
            "alt":            self.alt,
            "online":         self.online,
            "lora_rssi":      self.lora_rssi,
            "radio_mode":     self.radio_mode,
            "mcs":            self.mcs,
            "throughput_mbps": self.throughput_mbps,
            "wifi_rssi":      self.wifi_rssi,
            "last_seen":      max(
                self.last_lora_seen or 0,
                self.last_data_seen or 0
            ) or None,
        }


class NodeRegistry:
    """Thread-safe registry of all known nodes."""

    def __init__(self):
        self._nodes: Dict[str, NodeEntry] = {}
        self._lock  = threading.Lock()

    def update_from_lora(self, node_id: str, node_name: str,
                         lat: float, lon: float, alt: float,
                         rssi: int, mesh_ip: str = "") -> None:
        """Called by LoRaManager when a GPS broadcast is received."""
        with self._lock:
            n = self._nodes.setdefault(node_id, NodeEntry(node_id=node_id))
            n.node_name    = node_name
            n.lat          = lat
            n.lon          = lon
            n.alt          = alt
            n.lora_rssi    = rssi
            n.last_lora_seen = time.time()
            if mesh_ip:
                n.mesh_ip = mesh_ip

    def update_from_data_plane(self, node_id: str, radio_mode: str,
                               mcs: Optional[int] = None,
                               throughput_mbps: Optional[float] = None,
                               wifi_rssi: Optional[int] = None) -> None:
        """Called by RadioManager when a data-plane heartbeat is received."""
        with self._lock:
            n = self._nodes.setdefault(node_id, NodeEntry(node_id=node_id))
            n.radio_mode     = radio_mode
            n.mcs            = mcs
            n.throughput_mbps = throughput_mbps
            n.wifi_rssi      = wifi_rssi
            n.last_data_seen = time.time()

    def get_all(self) -> list:
        with self._lock:
            return [n.to_dict() for n in self._nodes.values()]

    def get(self, node_id: str) -> Optional[NodeEntry]:
        with self._lock:
            return self._nodes.get(node_id)

    def remove_stale(self, max_age_s: float = 300) -> None:
        """Remove nodes not seen on either plane for max_age_s seconds."""
        now = time.time()
        with self._lock:
            stale = [
                nid for nid, n in self._nodes.items()
                if max(n.last_lora_seen or 0, n.last_data_seen or 0) < now - max_age_s
            ]
            for nid in stale:
                del self._nodes[nid]
