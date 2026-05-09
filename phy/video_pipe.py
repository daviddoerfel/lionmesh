"""
video_pipe.py — H.265 Video über OFDM PHY
==========================================
TX-Node:
  Kamera/V4L2 → GStreamer H.265 Encoder → RTP → MAC → PHY → XTRX

RX-Node:
  XTRX → PHY → MAC → RTP → GStreamer H.265 Decoder → Display/RTSP-Sink

Anforderungen:
  sudo apt install python3-gst-1.0 gstreamer1.0-plugins-good \
       gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-x

Bitrate-Empfehlungen für MCS (netto nach PHY-Overhead, ~20% Reserve):
  MCS0: Steuerdaten only  (kein Video)
  MCS1: 480p @ 3 Mbps
  MCS3: 720p @ 8 Mbps
  MCS4: 1080p @ 15 Mbps
  MCS6: 1080p @ 25 Mbps  (beste Bedingungen, Kurzdistanz)

Für maximale Reichweite: MCS3, 720p, H.265, ~8 Mbps → sehr robust.
"""

import threading
import socket
import struct
import time
from typing import Optional

# GStreamer optional — ohne funktioniert nur der UDP-Modus
try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst, GLib
    Gst.init(None)
    GST_AVAILABLE = True
except ImportError:
    GST_AVAILABLE = False
    print("[Video] GStreamer nicht verfügbar → nur UDP-Modus")


# ─────────────────────────────────────────────
# RTP-Minimierung (kein vollständiges RTP)
# ─────────────────────────────────────────────

RTP_HDR = struct.Struct('!BBHII')   # V/P/X/CC/M/PT, seq, timestamp, ssrc

def rtp_pack(payload: bytes, seq: int, ts: int,
             ssrc: int = 0xDEADBEEF,
             pt: int = 96) -> bytes:
    """Minimales RTP-Header-Packing"""
    hdr = RTP_HDR.pack(
        0b10000000,        # V=2, P=0, X=0, CC=0
        0b01000000 | pt,   # M=0 (nur letztes Paket setzt M=1), PT=96
        seq & 0xFFFF,
        ts & 0xFFFFFFFF,
        ssrc
    )
    return hdr + payload


def rtp_unpack(data: bytes):
    """→ (payload, seq, timestamp) oder None"""
    if len(data) < RTP_HDR.size:
        return None
    v_p_x_cc, m_pt, seq, ts, ssrc = RTP_HDR.unpack(data[:RTP_HDR.size])
    return data[RTP_HDR.size:], seq, ts


# ─────────────────────────────────────────────
# TX-Videoquelle
# ─────────────────────────────────────────────

class VideoTX:
    """
    Liest H.265-NAL-Units aus GStreamer und fragmentiert sie
    in MAC-kompatible Pakete (max. 4000 Bytes).

    Ohne GStreamer: UDP-Empfänger für externe Quelle (z.B. rpicam).
    """

    MAX_PKT = 3800   # Bytes pro MAC-Frame (mit etwas Reserve)

    def __init__(self, send_fn,    # MAC.send_datagram
                 width:  int = 1280,
                 height: int = 720,
                 fps:    int = 25,
                 bitrate_kbps: int = 8000):
        self._send  = send_fn
        self._w     = width
        self._h     = height
        self._fps   = fps
        self._kbps  = bitrate_kbps
        self._seq   = 0
        self._ts    = 0
        self._pipe  = None
        self._udp   = None

    def start_gstreamer(self, src: str = 'v4l2src'):
        """
        Startet GStreamer-Pipeline.
        src: 'v4l2src device=/dev/video0' oder 'videotestsrc'
        """
        if not GST_AVAILABLE:
            print("[VideoTX] GStreamer nicht verfügbar")
            return False

        pipe_str = (
            f"{src} ! "
            f"video/x-raw,width={self._w},height={self._h},"
            f"framerate={self._fps}/1 ! "
            f"videoconvert ! "
            f"x265enc tune=zerolatency bitrate={self._kbps} "
            f"   speed-preset=ultrafast ! "
            f"video/x-h265,stream-format=byte-stream ! "
            f"appsink name=sink emit-signals=true max-buffers=2 drop=true"
        )

        self._pipe = Gst.parse_launch(pipe_str)
        sink = self._pipe.get_by_name('sink')
        sink.connect('new-sample', self._on_sample)
        self._pipe.set_state(Gst.State.PLAYING)
        print(f"[VideoTX] GStreamer gestartet: {self._w}×{self._h} "
              f"@{self._fps}fps {self._kbps}kbps H.265")
        return True

    def start_udp(self, port: int = 5600):
        """
        UDP-Empfänger für externe Videoquelle.
        Kompatibel mit rpicam-vid --codec h265 --inline -o udp://127.0.0.1:5600
        """
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind(('127.0.0.1', port))
        self._udp.settimeout(1.0)
        t = threading.Thread(target=self._udp_worker, daemon=True)
        t.start()
        print(f"[VideoTX] UDP-Empfänger auf Port {port}")

    def stop(self):
        if self._pipe:
            self._pipe.set_state(Gst.State.NULL)
        if self._udp:
            self._udp.close()

    def _on_sample(self, sink):
        """GStreamer Callback — neues Video-Sample"""
        sample = sink.emit('pull-sample')
        if sample:
            buf  = sample.get_buffer()
            data = buf.extract_dup(0, buf.get_size())
            self._fragment_and_send(bytes(data))
        return Gst.FlowReturn.OK

    def _udp_worker(self):
        """UDP-Empfangs-Thread"""
        buf = bytearray()
        while True:
            try:
                pkt, _ = self._udp.recvfrom(65535)
                buf.extend(pkt)
                # H.265 NAL-Unit Grenzen suchen (Start-Code 0x00000001)
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

    def _fragment_and_send(self, nal: bytes):
        """Fragmentiert NAL-Unit und sendet via MAC"""
        ts = self._ts
        self._ts = (self._ts + 90000 // self._fps) & 0xFFFFFFFF

        # Fragmentierung
        for i in range(0, len(nal), self.MAX_PKT):
            chunk  = nal[i:i+self.MAX_PKT]
            pkt    = rtp_pack(chunk, self._seq, ts)
            self._seq = (self._seq + 1) & 0xFFFF
            self._send(pkt)


# ─────────────────────────────────────────────
# RX-Videosenke
# ─────────────────────────────────────────────

class VideoRX:
    """
    Empfängt RTP-Pakete vom MAC, reassembliert NAL-Units
    und gibt sie an GStreamer oder UDP-Sink weiter.
    """

    def __init__(self,
                 display: bool = True,
                 udp_out: Optional[tuple] = None):   # ('127.0.0.1', 5601)
        self._display  = display
        self._udp_out  = udp_out
        self._pipe     = None
        self._appsrc   = None
        self._sock_out = None
        self._buf      = {}    # seq → payload (Reassembly)
        self._next_seq = 0

        if udp_out:
            self._sock_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def start(self):
        if self._display and GST_AVAILABLE:
            self._start_gstreamer()
        elif self._sock_out:
            print(f"[VideoRX] UDP-Weiterleitung → {self._udp_out}")

    def on_rx(self, mac_payload: bytes):
        """Wird vom MAC-Layer bei jedem empfangenen Datagram aufgerufen."""
        result = rtp_unpack(mac_payload)
        if result is None:
            return
        payload, seq, ts = result
        self._buf[seq] = payload

        # In-Order Ausgabe
        while self._next_seq in self._buf:
            nal = self._buf.pop(self._next_seq)
            self._next_seq = (self._next_seq + 1) & 0xFFFF
            self._output(nal)

        # Alte Pakete aus Buffer entfernen (max. 32 Pakete Jitter)
        stale = [s for s in self._buf if (self._next_seq - s) % 0x10000 > 32]
        for s in stale:
            del self._buf[s]

    def _output(self, nal: bytes):
        """NAL-Unit ausgeben"""
        if self._appsrc:
            buf = Gst.Buffer.new_wrapped(nal)
            self._appsrc.emit('push-buffer', buf)
        if self._sock_out:
            # In UDP-Chunks senden (max 60000 Bytes)
            for i in range(0, len(nal), 60000):
                self._sock_out.sendto(nal[i:i+60000], self._udp_out)

    def _start_gstreamer(self):
        if not GST_AVAILABLE:
            return
        pipe_str = (
            "appsrc name=src format=time is-live=true "
            "   caps=video/x-h265,stream-format=byte-stream,alignment=nal ! "
            "h265parse ! avdec_h265 ! videoconvert ! "
            "autovideosink sync=false"
        )
        self._pipe    = Gst.parse_launch(pipe_str)
        self._appsrc  = self._pipe.get_by_name('src')
        self._pipe.set_state(Gst.State.PLAYING)
        print("[VideoRX] GStreamer Display gestartet")

    def stop(self):
        if self._pipe:
            self._pipe.set_state(Gst.State.NULL)
        if self._sock_out:
            self._sock_out.close()


# ─────────────────────────────────────────────
# RTSP-Server (optional, für Monitoring)
# ─────────────────────────────────────────────

RTSP_PIPELINE_TX = """
v4l2src device=/dev/video0 !
video/x-raw,width=1280,height=720,framerate=25/1 !
videoconvert !
x265enc tune=zerolatency bitrate=8000 speed-preset=ultrafast !
video/x-h265,stream-format=byte-stream !
rtph265pay name=pay0 pt=96
"""

RTSP_PIPELINE_RX = """
udpsrc port=5601 !
application/x-rtp,media=video,clock-rate=90000,
  encoding-name=H265,payload=96 !
rtph265depay !
h265parse !
avdec_h265 !
videoconvert !
autovideosink
"""
