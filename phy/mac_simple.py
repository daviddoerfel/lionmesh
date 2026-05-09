"""
mac_simple.py — Minimaler MAC-Layer für XTRX OFDM PHY
======================================================
Aufgaben:
  - Frame-Framing mit Adressen + Sequenznummern
  - Stop-and-Wait ARQ (einfach und robust)
  - CSMA: Kanal-Sensing vor TX
  - Datagram-Modus für Video (kein ARQ, low latency)

Frame-Format (Bytes):
  [SYNC 2] [TYPE 1] [SEQ 2] [SRC 2] [DST 2] [LEN 2] [PAYLOAD N] [FCS 2]
  Gesamt-Overhead: 13 Bytes

Typen:
  0x01 DATA  - Datagram (kein ACK erwartet, für Video)
  0x02 AREQ  - Daten mit ACK-Request
  0x03 ACK   - Bestätigung
  0x04 NACK  - Negative Bestätigung (MCS-Downgrade)
  0x05 PROBE - Link-Quality Probe (kein Payload)
"""

import struct
import time
import queue
import threading
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, Callable
from enum import IntEnum

from phy_ofdm import MCS, tx_frame, OFDMReceiver, MCS_TABLE, SAMPLE_RATE


# ─────────────────────────────────────────────
# Frame-Definitionen
# ─────────────────────────────────────────────

SYNC_WORD    = 0xA55A   # 2 Bytes Sync
BCAST_ADDR   = 0xFFFF   # Broadcast-Adresse
MAX_PAYLOAD  = 4096     # Bytes pro Frame
HDR_FMT      = '>HBHHH'   # sync, type, seq, src, dst, len → 9 Bytes
HDR_SIZE     = struct.calcsize(HDR_FMT)  # = 9
FCS_SIZE     = 2
FRAME_OVERHEAD = HDR_SIZE + 2 + FCS_SIZE  # +2 für LEN-Feld

class FType(IntEnum):
    DATA  = 0x01
    AREQ  = 0x02
    ACK   = 0x03
    NACK  = 0x04
    PROBE = 0x05


@dataclass
class MACFrame:
    ftype:   FType
    seq:     int
    src:     int
    dst:     int
    payload: bytes = b''

    def encode(self) -> bytes:
        """Serialisiert Frame inkl. FCS"""
        hdr = struct.pack(HDR_FMT,
                          SYNC_WORD,
                          int(self.ftype),
                          self.seq & 0xFFFF,
                          self.src & 0xFFFF,
                          self.dst & 0xFFFF)
        ln  = struct.pack('>H', len(self.payload))
        body = hdr + ln + self.payload
        fcs  = self._crc16(body)
        return body + struct.pack('>H', fcs)

    @staticmethod
    def decode(data: bytes) -> Optional['MACFrame']:
        """Deserialisiert und verifiziert Frame"""
        min_sz = HDR_SIZE + 2 + FCS_SIZE
        if len(data) < min_sz:
            return None
        fcs_recv = struct.unpack('>H', data[-FCS_SIZE:])[0]
        if MACFrame._crc16(data[:-FCS_SIZE]) != fcs_recv:
            return None
        sync, ftype, seq, src, dst = struct.unpack(HDR_FMT, data[:HDR_SIZE])
        if sync != SYNC_WORD:
            return None
        n_pay = struct.unpack('>H', data[HDR_SIZE:HDR_SIZE+2])[0]
        pay   = data[HDR_SIZE+2:HDR_SIZE+2+n_pay]
        if len(pay) < n_pay:
            return None
        try:
            ft = FType(ftype)
        except ValueError:
            return None
        return MACFrame(ft, seq, src, dst, pay)

    @staticmethod
    def _crc16(data: bytes) -> int:
        """CRC-16/CCITT-FALSE"""
        crc = 0xFFFF
        for b in data:
            crc ^= b << 8
            for _ in range(8):
                crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
        return crc & 0xFFFF


# ─────────────────────────────────────────────
# Adaptive MCS-Steuerung (AMC)
# ─────────────────────────────────────────────

class AdaptiveMCS:
    """
    Einfache Link-Adaptation basierend auf ACK/NACK-Statistik.
    Erhöht MCS bei gutem Kanal, senkt bei Fehlern.
    """
    def __init__(self, start: MCS = MCS.MCS4):
        self.mcs        = start
        self._ok        = 0
        self._fail      = 0
        self._up_thresh = 10   # Erhöhe MCS nach N erfolgreichen Frames
        self._dn_thresh = 2    # Senke MCS nach N Fehlern

    def ack(self):
        self._ok   += 1
        self._fail  = 0
        if self._ok >= self._up_thresh and self.mcs < MCS.MCS6:
            self.mcs = MCS(self.mcs + 1)
            self._ok = 0
            print(f"[AMC] MCS erhöht → MCS{self.mcs}")

    def nack(self):
        self._fail += 1
        self._ok    = 0
        if self._fail >= self._dn_thresh and self.mcs > MCS.MCS0:
            self.mcs = MCS(self.mcs - 1)
            self._fail = 0
            print(f"[AMC] MCS gesenkt → MCS{self.mcs}")

    @property
    def mbps(self) -> float:
        p = MCS_TABLE[self.mcs]
        return p.dbps * SAMPLE_RATE / (80 * 1e6)


# ─────────────────────────────────────────────
# MAC-Schicht
# ─────────────────────────────────────────────

@dataclass
class MACConfig:
    node_addr:    int   = 0x0001   # Eigene Adresse
    ack_timeout:  float = 0.1      # Sekunden
    max_retries:  int   = 3
    video_mcs:    MCS   = MCS.MCS4 # MCS für Video-Datagrams (kein ARQ)
    data_mcs:     MCS   = MCS.MCS3 # Start-MCS für ARQ-Daten


class MACLayer:
    """
    MAC-Layer: sitzt zwischen Anwendung und PHY.

    TX-Pfad:
      send_datagram(payload) → sofort senden, kein ACK (Video)
      send_reliable(payload) → ARQ mit ACK/Retry

    RX-Pfad:
      Callback: on_rx(payload: bytes) → wird für jeden empfangenen Frame aufgerufen
    """

    def __init__(self,
                 config: MACConfig,
                 tx_fn: Callable[[np.ndarray], None],   # IQ → SDR senden
                 on_rx: Callable[[bytes], None]):        # Payload → App
        self.cfg      = config
        self._tx_fn   = tx_fn
        self._on_rx   = on_rx
        self._seq     = 0
        self._amc     = AdaptiveMCS(config.data_mcs)
        self._pending : Optional[MACFrame] = None
        self._ack_evt  = threading.Event()
        self._rx_lock  = threading.Lock()
        self._phy_rx   = OFDMReceiver()

        # TX-Queue für Hintergrund-Thread
        self._tx_q = queue.Queue(maxsize=64)
        self._tx_thread = threading.Thread(target=self._tx_worker,
                                           daemon=True)
        self._tx_thread.start()

    # ── Public API ─────────────────────────────

    def send_datagram(self, payload: bytes,
                      dst: int = BCAST_ADDR) -> None:
        """
        Ungesichertes Datagram — für Video-RTP-Pakete.
        Non-blocking, dropped wenn Queue voll.
        """
        if len(payload) > MAX_PAYLOAD:
            # Fragmentierung nötig → vereinfacht: nur warnen
            print(f"[MAC] Warnung: Payload {len(payload)}B > {MAX_PAYLOAD}B")
            payload = payload[:MAX_PAYLOAD]
        frame = MACFrame(FType.DATA, self._next_seq(), self.cfg.node_addr,
                         dst, payload)
        try:
            self._tx_q.put_nowait(('DATA', frame, self.cfg.video_mcs))
        except queue.Full:
            pass   # Video-Frame droppen bei Stau

    def send_reliable(self, payload: bytes,
                      dst: int = BCAST_ADDR) -> bool:
        """
        Gesicherter Transfer mit ARQ.
        Blockiert bis ACK oder Timeout.
        Gibt True bei Erfolg zurück.
        """
        frame = MACFrame(FType.AREQ, self._next_seq(),
                         self.cfg.node_addr, dst, payload)
        for attempt in range(self.cfg.max_retries):
            self._pending = frame
            self._ack_evt.clear()
            self._tx_fn(tx_frame(frame.encode(), self._amc.mcs))

            if self._ack_evt.wait(timeout=self.cfg.ack_timeout):
                self._amc.ack()
                return True
            print(f"[MAC] Timeout Versuch {attempt+1}/{self.cfg.max_retries}")

        self._amc.nack()
        self._pending = None
        return False

    def rx_push(self, iq_samples: np.ndarray) -> None:
        """
        IQ-Samples vom SDR einspeisen (aus RX-Thread aufrufen).
        """
        with self._rx_lock:
            payloads = self._phy_rx.push(iq_samples)
        for raw in payloads:
            self._handle_rx(raw)

    # ── Internes ───────────────────────────────

    def _next_seq(self) -> int:
        s = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        return s

    def _tx_worker(self):
        while True:
            item = self._tx_q.get()
            kind, frame, mcs = item
            iq = tx_frame(frame.encode(), mcs)
            self._tx_fn(iq)
            self._tx_q.task_done()

    def _handle_rx(self, raw: bytes) -> None:
        frame = MACFrame.decode(raw)
        if frame is None:
            return

        # Nur für uns oder Broadcast
        if frame.dst not in (self.cfg.node_addr, BCAST_ADDR):
            return

        if frame.ftype == FType.DATA:
            # Datagram → direkt an App
            self._on_rx(frame.payload)

        elif frame.ftype == FType.AREQ:
            # Gesicherter Frame → ACK senden + an App
            ack = MACFrame(FType.ACK, frame.seq,
                           self.cfg.node_addr, frame.src)
            self._tx_fn(tx_frame(ack.encode(), MCS.MCS0))
            self._on_rx(frame.payload)

        elif frame.ftype == FType.ACK:
            # ACK für pending Frame
            if (self._pending is not None and
                    frame.seq == self._pending.seq):
                self._pending = None
                self._ack_evt.set()

        elif frame.ftype == FType.NACK:
            # NACK → MCS-Downgrade, aber kein Auto-Retry hier
            self._amc.nack()
            if (self._pending is not None and
                    frame.seq == self._pending.seq):
                self._ack_evt.set()   # TX-Thread entscheidet über Retry

        elif frame.ftype == FType.PROBE:
            # Link-Quality Probe → echo mit ACK
            ack = MACFrame(FType.ACK, frame.seq,
                           self.cfg.node_addr, frame.src)
            self._tx_fn(tx_frame(ack.encode(), MCS.MCS0))

    def get_status(self) -> dict:
        return {
            'addr':     hex(self.cfg.node_addr),
            'mcs':      self._amc.mcs,
            'mbps_est': round(self._amc.mbps, 1),
            'tx_queue': self._tx_q.qsize(),
        }
