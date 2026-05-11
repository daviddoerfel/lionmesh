"""
mac_simple.py — LionMesh MAC Layer
=====================================
Tasks:
  - Frame framing with addresses and sequence numbers
  - Stop-and-wait ARQ for reliable transfers
  - Datagram mode for video (no ARQ, low latency)
  - Three-priority TX queue (Doodle Labs URLLC principle):
      PRIO 0 — Control  : ACK, NACK, PROBE  (highest priority, <1ms)
      PRIO 1 — Video    : DATA frames        (real-time, drop on full)
      PRIO 2 — Reliable : AREQ frames        (best-effort, retried)

Frame format (bytes):
  [SYNC 2] [TYPE 1] [SEQ 2] [SRC 2] [DST 2] [LEN 2] [PAYLOAD N] [FCS 2]
  Total overhead: 13 bytes

Frame types:
  0x01 DATA  - Datagram (no ACK, video RTP packets)
  0x02 AREQ  - Reliable data (ACK requested)
  0x03 ACK   - Acknowledgement
  0x04 NACK  - Negative ACK (triggers MCS downgrade)
  0x05 PROBE - Link-quality probe (no payload)
"""

import struct
import time
import queue
import threading
import numpy as np
from dataclasses import dataclass
from typing import Optional, Callable
from enum import IntEnum

from phy_ofdm import MCS, tx_frame, OFDMReceiver, MCS_TABLE, _sr


# ─────────────────────────────────────────────
# Frame definitions
# ─────────────────────────────────────────────

SYNC_WORD      = 0xA55A
BCAST_ADDR     = 0xFFFF
MAX_PAYLOAD    = 4096
HDR_FMT        = '>HBHHH'   # sync, type, seq, src, dst
HDR_SIZE       = struct.calcsize(HDR_FMT)   # 9 bytes
FCS_SIZE       = 2
FRAME_OVERHEAD = HDR_SIZE + 2 + FCS_SIZE    # +2 for LEN field

# TX queue priorities
PRIO_CONTROL = 0   # ACK / NACK / PROBE — always goes first
PRIO_VIDEO   = 1   # DATA datagrams — real-time, drop on congestion
PRIO_DATA    = 2   # AREQ reliable — can wait

# Fragmentation constants
FRAG_HDR_FMT  = '>HBB'          # frag_id(2), frag_index(1), frag_total(1)
FRAG_HDR_SIZE = struct.calcsize(FRAG_HDR_FMT)   # 4 bytes
FRAG_PAYLOAD  = MAX_PAYLOAD - FRAG_HDR_SIZE      # usable bytes per fragment
MAX_FRAGS     = 255              # max fragments per message


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
        hdr  = struct.pack(HDR_FMT,
                           SYNC_WORD,
                           int(self.ftype),
                           self.seq & 0xFFFF,
                           self.src & 0xFFFF,
                           self.dst & 0xFFFF)
        ln   = struct.pack('>H', len(self.payload))
        body = hdr + ln + self.payload
        fcs  = self._crc16(body)
        return body + struct.pack('>H', fcs)

    @staticmethod
    def decode(data: bytes) -> Optional['MACFrame']:
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
        crc = 0xFFFF
        for b in data:
            crc ^= b << 8
            for _ in range(8):
                crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
        return crc & 0xFFFF


# ─────────────────────────────────────────────
# Fragmentation / Reassembly
# ─────────────────────────────────────────────

def fragment(payload: bytes, frag_id: int) -> list:
    """
    Split a large payload into MAC-sized fragments.

    Fragment header (4 bytes prepended to each fragment):
      [frag_id 2B] [frag_index 1B] [frag_total 1B]

    frag_id    : unique ID per original message (wraps at 65535)
    frag_index : 0-based index of this fragment
    frag_total : total number of fragments

    Returns list of bytes objects, each ≤ MAX_PAYLOAD.
    Payloads that fit in one fragment are returned as-is with a
    single-fragment header (frag_total=1) for uniform handling.
    """
    chunks = [payload[i:i+FRAG_PAYLOAD]
              for i in range(0, len(payload), FRAG_PAYLOAD)]
    if len(chunks) > MAX_FRAGS:
        raise ValueError(f"Payload too large: {len(payload)} bytes requires "
                         f"{len(chunks)} fragments (max {MAX_FRAGS})")
    total = len(chunks)
    frags = []
    for idx, chunk in enumerate(chunks):
        hdr  = struct.pack(FRAG_HDR_FMT, frag_id & 0xFFFF, idx, total)
        frags.append(hdr + chunk)
    return frags


class ReassemblyBuffer:
    """
    Reassembles fragmented messages from received MAC frames.

    Usage:
        buf = ReassemblyBuffer()
        complete = buf.add(fragment_payload)
        if complete:
            handle(complete)

    Incomplete messages are held for max_age_s seconds, then discarded.
    """

    def __init__(self, max_age_s: float = 2.0):
        self._max_age = max_age_s
        # key: frag_id → {'total': N, 'frags': {idx: bytes}, 'ts': float}
        self._pending: dict = {}

    def add(self, payload: bytes) -> Optional[bytes]:
        """
        Add a received fragment payload.
        Returns the complete reassembled message if all fragments arrived,
        or None if still waiting for more.
        """
        if len(payload) < FRAG_HDR_SIZE:
            return None

        frag_id, idx, total = struct.unpack(FRAG_HDR_FMT,
                                             payload[:FRAG_HDR_SIZE])
        data = payload[FRAG_HDR_SIZE:]

        # Single-fragment message — return immediately
        if total == 1:
            return data

        # Multi-fragment — store and check for completion
        now = time.monotonic()
        if frag_id not in self._pending:
            self._pending[frag_id] = {'total': total, 'frags': {}, 'ts': now}

        entry = self._pending[frag_id]
        entry['frags'][idx] = data
        entry['ts'] = now

        # Expire stale entries
        self._expire()

        if len(entry['frags']) == entry['total']:
            complete = b''.join(entry['frags'][i]
                                for i in range(entry['total']))
            del self._pending[frag_id]
            return complete

        return None

    def _expire(self) -> None:
        now   = time.monotonic()
        stale = [k for k, v in self._pending.items()
                 if now - v['ts'] > self._max_age]
        for k in stale:
            del self._pending[k]


# ─────────────────────────────────────────────
# Adaptive MCS (AMC)
# ─────────────────────────────────────────────

class AdaptiveMCS:
    def __init__(self, start: MCS = MCS.MCS4):
        self.mcs        = start
        self._ok        = 0
        self._fail      = 0
        self._up_thresh = 10
        self._dn_thresh = 2

    def ack(self):
        self._ok  += 1; self._fail = 0
        if self._ok >= self._up_thresh and self.mcs < MCS.MCS6:
            self.mcs = MCS(self.mcs + 1); self._ok = 0
            print(f"[AMC] MCS up → MCS{self.mcs}")

    def nack(self):
        self._fail += 1; self._ok = 0
        if self._fail >= self._dn_thresh and self.mcs > MCS.MCS0:
            self.mcs = MCS(self.mcs - 1); self._fail = 0
            print(f"[AMC] MCS down → MCS{self.mcs}")

    @property
    def mbps(self) -> float:
        return MCS_TABLE[self.mcs].dbps * _sr() / (80 * 1e6)


# ─────────────────────────────────────────────
# Priority TX Queue
# ─────────────────────────────────────────────

class PriorityTXQueue:
    """
    Three-level priority queue for MAC TX scheduling.

    Priority order (lowest number = highest priority):
      0 — Control frames (ACK, NACK, PROBE) — always dequeued first
      1 — Video datagrams (DATA)             — real-time, drop when full
      2 — Reliable data (AREQ)               — can wait

    This mirrors the Doodle Labs Differentiated Services approach:
    control/C&C traffic is never delayed by video backlog.
    """

    def __init__(self,
                 maxsize_video:   int = 16,
                 maxsize_control: int = 32,
                 maxsize_data:    int = 32):
        self._queues = {
            PRIO_CONTROL: queue.Queue(maxsize=maxsize_control),
            PRIO_VIDEO:   queue.Queue(maxsize=maxsize_video),
            PRIO_DATA:    queue.Queue(maxsize=maxsize_data),
        }
        self._not_empty = threading.Event()

    def put(self, item, priority: int, block: bool = True) -> bool:
        """
        Enqueue an item at the given priority.
        Returns False (and drops) if the queue is full and block=False.
        """
        try:
            self._queues[priority].put(item, block=block)
            self._not_empty.set()
            return True
        except queue.Full:
            return False

    def get(self, timeout: float = 1.0):
        """
        Dequeue the highest-priority available item.
        Blocks up to timeout seconds.
        """
        deadline = time.monotonic() + timeout
        while True:
            # Check queues in priority order
            for prio in (PRIO_CONTROL, PRIO_VIDEO, PRIO_DATA):
                try:
                    item = self._queues[prio].get_nowait()
                    return item
                except queue.Empty:
                    continue

            # All queues empty — wait briefly
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            self._not_empty.wait(timeout=min(0.005, remaining))
            self._not_empty.clear()

    def qsize(self) -> dict:
        return {
            'control': self._queues[PRIO_CONTROL].qsize(),
            'video':   self._queues[PRIO_VIDEO].qsize(),
            'data':    self._queues[PRIO_DATA].qsize(),
        }


# ─────────────────────────────────────────────
# MAC Layer
# ─────────────────────────────────────────────

@dataclass
class MACConfig:
    node_addr:    int   = 0x0001
    ack_timeout:  float = 0.1       # seconds
    max_retries:  int   = 3
    video_mcs:    MCS   = MCS.MCS4  # MCS for video datagrams
    data_mcs:     MCS   = MCS.MCS3  # starting MCS for reliable data


class MACLayer:
    """
    MAC layer between application and PHY.

    TX path:
      send_datagram(payload)  → fire-and-forget, PRIO_VIDEO queue
      send_reliable(payload)  → ARQ with ACK/retry, PRIO_DATA queue
      ACK/NACK/PROBE          → always PRIO_CONTROL queue (preempts video)

    RX path:
      rx_push(iq) → OFDMReceiver → _handle_rx → on_rx callback
    """

    def __init__(self,
                 config: MACConfig,
                 tx_fn:  Callable[[np.ndarray], None],
                 on_rx:  Callable[[bytes], None]):
        self.cfg       = config
        self._tx_fn    = tx_fn
        self._on_rx    = on_rx
        self._seq      = 0
        self._frag_id  = 0                  # fragment message ID counter
        self._amc      = AdaptiveMCS(config.data_mcs)
        self._pending  : Optional[MACFrame] = None
        self._ack_evt  = threading.Event()
        self._rx_lock  = threading.Lock()
        self._phy_rx   = OFDMReceiver()
        self._reassembly = ReassemblyBuffer()  # defragmentation buffer

        self._tx_q     = PriorityTXQueue()
        self._tx_thread = threading.Thread(target=self._tx_worker, daemon=True)
        self._tx_thread.start()

    # ── Public API ─────────────────────────────────────────

    def send_datagram(self, payload: bytes, dst: int = BCAST_ADDR) -> None:
        """
        Unreliable datagram — for video RTP packets.
        Automatically fragments payloads larger than FRAG_PAYLOAD bytes.
        Non-blocking. Fragments dropped silently if video queue is full.
        """
        frags = fragment(payload, self._frag_id)
        self._frag_id = (self._frag_id + 1) & 0xFFFF

        for frag_data in frags:
            frame = MACFrame(FType.DATA, self._next_seq(),
                             self.cfg.node_addr, dst, frag_data)
            self._tx_q.put((frame, self.cfg.video_mcs),
                           PRIO_VIDEO, block=False)

    def send_reliable(self, payload: bytes, dst: int = BCAST_ADDR) -> bool:
        """
        Reliable transfer with stop-and-wait ARQ.
        Blocks until ACK received or max retries exceeded.
        Enqueued at PRIO_DATA — yields to video and control traffic.
        """
        frame = MACFrame(FType.AREQ, self._next_seq(),
                         self.cfg.node_addr, dst, payload)
        for attempt in range(self.cfg.max_retries):
            self._pending = frame
            self._ack_evt.clear()
            # Reliable frames go via the priority queue too
            self._tx_q.put((frame, self._amc.mcs), PRIO_DATA)

            if self._ack_evt.wait(timeout=self.cfg.ack_timeout):
                self._amc.ack()
                return True
            print(f"[MAC] Timeout attempt {attempt+1}/{self.cfg.max_retries}")

        self._amc.nack()
        self._pending = None
        return False

    def rx_push(self, iq_samples: np.ndarray) -> None:
        with self._rx_lock:
            payloads = self._phy_rx.push(iq_samples)
        for raw in payloads:
            self._handle_rx(raw)

    def get_status(self) -> dict:
        qs = self._tx_q.qsize()
        return {
            'addr':       hex(self.cfg.node_addr),
            'mcs':        self._amc.mcs,
            'mbps_est':   round(self._amc.mbps, 1),
            'tx_control': qs['control'],
            'tx_video':   qs['video'],
            'tx_data':    qs['data'],
        }

    # ── Internal ───────────────────────────────────────────

    def _next_seq(self) -> int:
        s = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        return s

    def _tx_worker(self):
        """
        Single TX thread — dequeues in priority order.
        Control frames always jump ahead of video and data.
        """
        while True:
            try:
                frame, mcs = self._tx_q.get(timeout=1.0)
                iq = tx_frame(frame.encode(), mcs)
                self._tx_fn(iq)
            except queue.Empty:
                continue

    def _send_control(self, frame: MACFrame, mcs: MCS = MCS.MCS0) -> None:
        """Enqueue a control frame at highest priority."""
        self._tx_q.put((frame, mcs), PRIO_CONTROL, block=False)

    def _handle_rx(self, raw: bytes) -> None:
        frame = MACFrame.decode(raw)
        if frame is None:
            return
        if frame.dst not in (self.cfg.node_addr, BCAST_ADDR):
            return

        if frame.ftype == FType.DATA:
            # Pass through reassembly — handles both single and multi-fragment
            complete = self._reassembly.add(frame.payload)
            if complete is not None:
                self._on_rx(complete)

        elif frame.ftype == FType.AREQ:
            # Send ACK immediately at PRIO_CONTROL
            ack = MACFrame(FType.ACK, frame.seq, self.cfg.node_addr, frame.src)
            self._send_control(ack)
            self._on_rx(frame.payload)

        elif frame.ftype == FType.ACK:
            if self._pending and frame.seq == self._pending.seq:
                self._pending = None
                self._ack_evt.set()

        elif frame.ftype == FType.NACK:
            self._amc.nack()
            if self._pending and frame.seq == self._pending.seq:
                self._ack_evt.set()

        elif frame.ftype == FType.PROBE:
            ack = MACFrame(FType.ACK, frame.seq, self.cfg.node_addr, frame.src)
            self._send_control(ack)
