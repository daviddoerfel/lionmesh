"""
video_pipe.py — H.265 Video Pipeline with Adaptive Bitrate
============================================================
TX-Node:
  Camera/V4L2 → GStreamer H.265 Encoder → RTP → MAC → PHY → LimeSDR

RX-Node:
  LimeSDR → PHY → MAC → RTP → GStreamer H.265 Decoder → Display/UDP

Adaptive Bitrate (ABR):
  VideoTX monitors the MAC layer's current MCS and adjusts the
  H.265 encoder bitrate dynamically. When the link degrades and
  MCS drops, the encoder reduces bitrate before the MAC queue
  fills up — preventing stutter and dropped frames.

  MCS → bitrate mapping (with 40% headroom below PHY throughput):
    MCS0  1.5 Mbps PHY → no video (control only)
    MCS1  3.0 Mbps PHY → 480p  @ 1.2 Mbps
    MCS2  4.5 Mbps PHY → 480p  @ 1.8 Mbps
    MCS3  6.0 Mbps PHY → 720p  @ 2.4 Mbps
    MCS4  9.0 Mbps PHY → 720p  @ 3.6 Mbps  ← default
    MCS5 12.0 Mbps PHY → 1080p @ 4.8 Mbps
    MCS6 13.5 Mbps PHY → 1080p @ 5.4 Mbps

  These values assume 5 MHz bandwidth. Scale proportionally for 10/20 MHz.

Requirements:
  sudo apt install python3-gst-1.0 gstreamer1.0-plugins-good \\
       gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-x
"""

import threading
import socket
import struct
import time
import logging
from typing import Optional, Callable

log = logging.getLogger("video")

# GStreamer — optional
try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst, GLib
    Gst.init(None)
    GST_AVAILABLE = True
except ImportError:
    GST_AVAILABLE = False
    log.warning("GStreamer not available — UDP mode only")


# ─────────────────────────────────────────────
# MCS → Video profile mapping
# ─────────────────────────────────────────────

# Each entry: (bitrate_kbps, width, height)
# Bitrate = ~40% below PHY throughput at 5 MHz BW (headroom for overhead + retransmits)
# Scale bitrate proportionally for 10 MHz (×2) or 20 MHz (×4)
MCS_VIDEO_PROFILE = {
    0: None,                    # 1.5 Mbps — too low for video, control only
    1: (1200,  854,  480),      # 3.0 Mbps → 480p @ 1.2 Mbps
    2: (1800,  854,  480),      # 4.5 Mbps → 480p @ 1.8 Mbps
    3: (2400, 1280,  720),      # 6.0 Mbps → 720p @ 2.4 Mbps
    4: (3600, 1280,  720),      # 9.0 Mbps → 720p @ 3.6 Mbps  ← default
    5: (4800, 1920, 1080),      # 12.0 Mbps → 1080p @ 4.8 Mbps
    6: (5400, 1920, 1080),      # 13.5 Mbps → 1080p @ 5.4 Mbps
}


def mcs_to_bitrate(mcs: int, bw_hz: float = 5e6) -> Optional[int]:
    """
    Returns target encoder bitrate in kbps for a given MCS and bandwidth.
    Returns None if MCS is too low for video.
    Scales linearly with bandwidth (5/10/20 MHz).
    """
    profile = MCS_VIDEO_PROFILE.get(mcs)
    if profile is None:
        return None
    base_kbps = profile[0]
    scale = bw_hz / 5e6
    return int(base_kbps * scale)


# ─────────────────────────────────────────────
# Minimal RTP packing
# ─────────────────────────────────────────────

RTP_HDR = struct.Struct('!BBHII')


def rtp_pack(payload: bytes, seq: int, ts: int,
             ssrc: int = 0xDEADBEEF, pt: int = 96) -> bytes:
    hdr = RTP_HDR.pack(
        0b10000000,
        0b01000000 | pt,
        seq & 0xFFFF,
        ts & 0xFFFFFFFF,
        ssrc
    )
    return hdr + payload


def rtp_unpack(data: bytes):
    if len(data) < RTP_HDR.size:
        return None
    v_p_x_cc, m_pt, seq, ts, ssrc = RTP_HDR.unpack(data[:RTP_HDR.size])
    return data[RTP_HDR.size:], seq, ts


# ─────────────────────────────────────────────
# TX Video source
# ─────────────────────────────────────────────

class VideoTX:
    """
    H.265 video transmitter with adaptive bitrate.

    Monitors the MAC layer's current MCS via get_mcs_fn() and adjusts
    the GStreamer encoder bitrate dynamically when the link changes.

    Usage:
        tx = VideoTX(
            send_fn    = mac.send_datagram,
            get_mcs_fn = lambda: mac.get_status()['mcs'],
        )
        tx.start_gstreamer('v4l2src device=/dev/video0')
    """

    MAX_PKT   = 3800   # bytes per RTP fragment
    ABR_CHECK = 2.0    # seconds between MCS checks

    def __init__(self,
                 send_fn:     Callable[[bytes], None],
                 get_mcs_fn:  Optional[Callable[[], int]] = None,
                 width:       int   = 1280,
                 height:      int   = 720,
                 fps:         int   = 25,
                 bitrate_kbps: int  = 3600,
                 bw_hz:       float = 5e6):
        self._send        = send_fn
        self._get_mcs     = get_mcs_fn
        self._w           = width
        self._h           = height
        self._fps         = fps
        self._kbps        = bitrate_kbps
        self._bw_hz       = bw_hz
        self._seq         = 0
        self._ts          = 0
        self._pipe        = None
        self._encoder     = None   # GStreamer x265enc element
        self._udp         = None
        self._current_mcs = None
        self._abr_thread  = None
        self._running     = False

    # ── Public API ──────────────────────────────────────────

    def start_gstreamer(self, src: str = 'v4l2src') -> bool:
        """
        Start GStreamer H.265 pipeline.
        src: GStreamer source element string, e.g.:
          'v4l2src device=/dev/video0'
          'videotestsrc pattern=ball'
        """
        if not GST_AVAILABLE:
            log.error("GStreamer not available")
            return False

        pipe_str = (
            f"{src} ! "
            f"video/x-raw,width={self._w},height={self._h},"
            f"framerate={self._fps}/1 ! "
            f"videoconvert ! "
            f"x265enc name=enc tune=zerolatency bitrate={self._kbps} "
            f"    speed-preset=ultrafast key-int-max=30 ! "
            f"video/x-h265,stream-format=byte-stream ! "
            f"appsink name=sink emit-signals=true max-buffers=2 drop=true"
        )

        self._pipe    = Gst.parse_launch(pipe_str)
        self._encoder = self._pipe.get_by_name('enc')
        sink          = self._pipe.get_by_name('sink')
        sink.connect('new-sample', self._on_sample)
        self._pipe.set_state(Gst.State.PLAYING)
        self._running = True

        # Start adaptive bitrate monitor thread
        if self._get_mcs is not None:
            self._abr_thread = threading.Thread(
                target=self._abr_loop, daemon=True)
            self._abr_thread.start()
            log.info(f"VideoTX started with ABR: {self._w}×{self._h} "
                     f"@{self._fps}fps, monitoring MCS")
        else:
            log.info(f"VideoTX started (fixed bitrate {self._kbps} kbps): "
                     f"{self._w}×{self._h} @{self._fps}fps")
        return True

    def start_udp(self, port: int = 5600) -> None:
        """
        UDP receiver for external video source.
        Compatible with: rpicam-vid --codec h265 --inline -o udp://127.0.0.1:5600
        """
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind(('127.0.0.1', port))
        self._udp.settimeout(1.0)
        self._running = True
        threading.Thread(target=self._udp_worker, daemon=True).start()
        log.info(f"VideoTX UDP receiver on port {port}")

    def set_bitrate(self, kbps: int) -> None:
        """
        Manually set encoder bitrate at runtime.
        Called automatically by the ABR loop; can also be called externally.
        """
        if self._encoder is None:
            self._kbps = kbps
            return
        if abs(kbps - self._kbps) < 100:
            return   # change too small, skip
        self._kbps = kbps
        self._encoder.set_property('bitrate', kbps)
        log.info(f"[ABR] Encoder bitrate → {kbps} kbps")

    def stop(self) -> None:
        self._running = False
        if self._pipe:
            self._pipe.set_state(Gst.State.NULL)
        if self._udp:
            self._udp.close()

    # ── Adaptive Bitrate Loop ────────────────────────────────

    def _abr_loop(self) -> None:
        """
        Polls MAC MCS every ABR_CHECK seconds.
        Adjusts encoder bitrate when MCS changes.
        """
        while self._running:
            time.sleep(self.ABR_CHECK)
            try:
                mcs = int(self._get_mcs())
                if mcs == self._current_mcs:
                    continue

                new_kbps = mcs_to_bitrate(mcs, self._bw_hz)
                self._current_mcs = mcs

                if new_kbps is None:
                    # MCS0 — link too weak for video, pause encoder
                    log.warning(f"[ABR] MCS{mcs} — link too weak for video, "
                                f"pausing encoder")
                    if self._pipe:
                        self._pipe.set_state(Gst.State.PAUSED)
                else:
                    # Resume if paused
                    if self._pipe:
                        self._pipe.set_state(Gst.State.PLAYING)
                    self.set_bitrate(new_kbps)
                    log.info(f"[ABR] MCS{mcs} → {new_kbps} kbps")

            except Exception as e:
                log.debug(f"ABR loop error: {e}")

    # ── GStreamer / UDP internals ─────────────────────────────

    def _on_sample(self, sink):
        sample = sink.emit('pull-sample')
        if sample:
            buf  = sample.get_buffer()
            data = buf.extract_dup(0, buf.get_size())
            self._fragment_and_send(bytes(data))
        return Gst.FlowReturn.OK

    def _udp_worker(self) -> None:
        buf = bytearray()
        while self._running:
            try:
                pkt, _ = self._udp.recvfrom(65535)
                buf.extend(pkt)
                while len(buf) > 4:
                    idx = buf.find(b'\x00\x00\x00\x01', 1)
                    if idx < 0:
                        break
                    nal = bytes(buf[:idx])
                    buf = buf[idx:]
                    if len(nal) > 4:
                        self._fragment_and_send(nal)
            except (socket.timeout, OSError):
                pass

    def _fragment_and_send(self, nal: bytes) -> None:
        ts = self._ts
        self._ts = (self._ts + 90000 // self._fps) & 0xFFFFFFFF
        for i in range(0, len(nal), self.MAX_PKT):
            chunk = nal[i:i+self.MAX_PKT]
            pkt   = rtp_pack(chunk, self._seq, ts)
            self._seq = (self._seq + 1) & 0xFFFF
            self._send(pkt)


# ─────────────────────────────────────────────
# RX Video sink
# ─────────────────────────────────────────────

class VideoRX:
    """
    Receives RTP packets from the MAC layer, reassembles NAL units,
    and feeds them to GStreamer or forwards via UDP.
    """

    def __init__(self,
                 display:  bool            = True,
                 udp_out:  Optional[tuple] = None):
        self._display  = display
        self._udp_out  = udp_out
        self._pipe     = None
        self._appsrc   = None
        self._sock_out = None
        self._buf      = {}
        self._next_seq = 0

        if udp_out:
            self._sock_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def start(self) -> None:
        if self._display and GST_AVAILABLE:
            self._start_gstreamer()
        elif self._sock_out:
            log.info(f"VideoRX forwarding to {self._udp_out}")

    def on_rx(self, mac_payload: bytes) -> None:
        result = rtp_unpack(mac_payload)
        if result is None:
            return
        payload, seq, ts = result
        self._buf[seq] = payload

        while self._next_seq in self._buf:
            nal = self._buf.pop(self._next_seq)
            self._next_seq = (self._next_seq + 1) & 0xFFFF
            self._output(nal)

        stale = [s for s in self._buf
                 if (self._next_seq - s) % 0x10000 > 32]
        for s in stale:
            del self._buf[s]

    def _output(self, nal: bytes) -> None:
        if self._appsrc:
            buf = Gst.Buffer.new_wrapped(nal)
            self._appsrc.emit('push-buffer', buf)
        if self._sock_out:
            for i in range(0, len(nal), 60000):
                self._sock_out.sendto(nal[i:i+60000], self._udp_out)

    def _start_gstreamer(self) -> None:
        if not GST_AVAILABLE:
            return
        pipe_str = (
            "appsrc name=src format=time is-live=true "
            "   caps=video/x-h265,stream-format=byte-stream,alignment=nal ! "
            "h265parse ! avdec_h265 ! videoconvert ! "
            "autovideosink sync=false"
        )
        self._pipe   = Gst.parse_launch(pipe_str)
        self._appsrc = self._pipe.get_by_name('src')
        self._pipe.set_state(Gst.State.PLAYING)
        log.info("VideoRX GStreamer display started")

    def stop(self) -> None:
        if self._pipe:
            self._pipe.set_state(Gst.State.NULL)
        if self._sock_out:
            self._sock_out.close()
