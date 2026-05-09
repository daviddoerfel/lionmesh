"""
xtrx_radio.py — LimeSDR XTRX Hardware-Interface
================================================
Abstraktionsschicht über SoapySDR für den XTRX.

XTRX-Spezifika:
  - Mini-PCIe Interface (PCIe oder USB-Adapter)
  - LMS7002M: 30 MHz – 3.8 GHz, 2T2R MIMO
  - Artix-7 FPGA (Xilinx XC7A50T)
  - Ausgangsleistung: typ. 0..10 dBm (ohne PA)
  - Samplingrate: bis 120 MS/s (SISO), 90 MS/s (MIMO)
  - Für 5 GHz: externer Upconverter nötig (nicht hier)

Frequenzen:
  863 MHz  → EU Sub-GHz ISM
  2400 MHz → 2.4 GHz ISM

Hinweis: Ohne SoapySDR-Installation läuft das Modul im
         SIMULATION-Modus (für Entwicklung/Test ohne Hardware).
"""

import numpy as np
import threading
import time
import queue
from typing import Optional, Callable, Tuple
from dataclasses import dataclass


# ─────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────

@dataclass
class XTRXConfig:
    # Frequency in Hz:
    freq_hz:      float = 863e6        # 863 MHz EU Sub-GHz ISM (long range)
    # freq_hz:    float = 2400e6       # 2.4 GHz ISM (higher throughput)

    # Channel bandwidth — must match phy_ofdm.set_bandwidth()
    # 5 MHz  → best range, ~9 Mbps @ MCS4   (default, Doodle Labs style)
    # 10 MHz → balanced,  ~18 Mbps @ MCS4
    # 20 MHz → max speed, ~36 Mbps @ MCS4
    bandwidth_hz: float = 5e6

    tx_gain_db:   float = 50.0         # dB (LMS7002M: 0..73 dB)
    rx_gain_db:   float = 40.0         # dB
    mimo:         bool  = False        # True: both RX/TX chains active
    device_args:  str   = "driver=lime"  # "driver=xtrx" for LimeSDR XTRX
    rx_buf_size:  int   = 4096         # samples per RX buffer

    @property
    def sample_rate(self) -> float:
        return self.bandwidth_hz

    @property
    def bandwidth(self) -> float:
        return self.bandwidth_hz


# ─────────────────────────────────────────────
# Radio-Interface
# ─────────────────────────────────────────────

class XTRXRadio:
    """
    Thin Wrapper um SoapySDR für den LimeSDR XTRX.

    Wird SoapySDR nicht gefunden, wechselt automatisch
    in den Simulationsmodus (Loopback TX→RX mit AWGN).
    """

    def __init__(self, cfg: XTRXConfig,
                 rx_callback: Callable[[np.ndarray], None]):
        self.cfg         = cfg
        self._rx_cb      = rx_callback
        self._tx_q       = queue.Queue(maxsize=128)
        self._running    = False
        self._sdr        = None
        self._sim_mode   = False
        self._tx_stream  = None
        self._rx_stream  = None

        self._init_hardware()

    def _init_hardware(self):
        """Initialisiert XTRX via SoapySDR oder fällt auf Simulation zurück."""
        try:
            import SoapySDR
            from SoapySDR import SOAPY_SDR_TX, SOAPY_SDR_RX, SOAPY_SDR_CF32

            sdr = SoapySDR.Device({'driver': 'xtrx'} if 'xtrx' in self.cfg.device_args
                                   else self.cfg.device_args)

            channels = [0, 1] if self.cfg.mimo else [0]

            for ch in channels:
                # TX konfigurieren
                sdr.setSampleRate(SOAPY_SDR_TX, ch, self.cfg.sample_rate)
                sdr.setFrequency(SOAPY_SDR_TX,  ch, self.cfg.freq_hz)
                sdr.setBandwidth(SOAPY_SDR_TX,  ch, self.cfg.bandwidth)
                sdr.setGain(SOAPY_SDR_TX,       ch, self.cfg.tx_gain_db)

                # RX konfigurieren
                sdr.setSampleRate(SOAPY_SDR_RX, ch, self.cfg.sample_rate)
                sdr.setFrequency(SOAPY_SDR_RX,  ch, self.cfg.freq_hz)
                sdr.setBandwidth(SOAPY_SDR_RX,  ch, self.cfg.bandwidth)
                sdr.setGain(SOAPY_SDR_RX,       ch, self.cfg.rx_gain_db)

            # Streams öffnen
            fmt = SOAPY_SDR_CF32
            self._tx_stream = sdr.setupStream(SOAPY_SDR_TX, fmt, channels)
            self._rx_stream = sdr.setupStream(SOAPY_SDR_RX, fmt, channels)

            self._sdr      = sdr
            self._SoapySDR = SoapySDR
            print(f"[XTRX] Hardware initialisiert: "
                  f"{self.cfg.freq_hz/1e6:.0f} MHz, "
                  f"{self.cfg.sample_rate/1e6:.0f} MS/s, "
                  f"{'MIMO' if self.cfg.mimo else 'SISO'}")

        except (ImportError, Exception) as e:
            print(f"[XTRX] SoapySDR nicht verfügbar ({e}) → Simulationsmodus")
            self._sim_mode = True

    def start(self):
        """Startet TX/RX-Threads."""
        self._running = True
        if not self._sim_mode and self._sdr:
            self._sdr.activateStream(self._tx_stream)
            self._sdr.activateStream(self._rx_stream)

        self._rx_thread = threading.Thread(target=self._rx_worker, daemon=True)
        self._tx_thread = threading.Thread(target=self._tx_worker, daemon=True)
        self._rx_thread.start()
        self._tx_thread.start()
        print("[XTRX] Gestartet")

    def stop(self):
        """Stoppt alle Threads und gibt Ressourcen frei."""
        self._running = False
        if not self._sim_mode and self._sdr:
            self._sdr.deactivateStream(self._tx_stream)
            self._sdr.deactivateStream(self._rx_stream)
            self._sdr.closeStream(self._tx_stream)
            self._sdr.closeStream(self._rx_stream)
        print("[XTRX] Gestoppt")

    def transmit(self, iq: np.ndarray):
        """
        IQ-Samples zur TX-Queue hinzufügen.
        Non-blocking — dropped bei vollem Buffer.
        """
        try:
            self._tx_q.put_nowait(iq.astype(np.complex64))
        except queue.Full:
            print("[XTRX] TX-Buffer voll, Frame gedroppt")

    # ── Interne Worker-Threads ─────────────────

    def _tx_worker(self):
        """TX: IQ-Samples aus Queue an SDR senden."""
        if self._sim_mode:
            # Simulation: TX-Samples direkt an RX-Callback mit AWGN
            while self._running:
                try:
                    iq = self._tx_q.get(timeout=0.05)
                    # AWGN-Kanal simulieren (30 dB SNR)
                    snr  = 10 ** (30 / 10)
                    pwr  = np.mean(np.abs(iq)**2)
                    n0   = pwr / snr
                    noise = np.sqrt(n0/2) * (
                        np.random.randn(len(iq)) +
                        1j * np.random.randn(len(iq)))
                    rx = (iq + noise.astype(np.complex64))
                    # Kleiner Delay simuliert Übertragungszeit
                    time.sleep(len(iq) / self.cfg.sample_rate)
                    self._rx_cb(rx)
                except queue.Empty:
                    pass
            return

        # Hardware-Modus
        while self._running:
            try:
                iq = self._tx_q.get(timeout=0.05)
            except queue.Empty:
                continue

            # In Chunks senden
            chunk = 1024
            for i in range(0, len(iq), chunk):
                buf = [iq[i:i+chunk]]   # Liste für SISO; [ch0, ch1] für MIMO
                self._sdr.writeStream(
                    self._tx_stream, buf, len(buf[0]),
                    timeoutUs=1_000_000)

    def _rx_worker(self):
        """RX: Samples vom SDR lesen und an Callback weitergeben."""
        if self._sim_mode:
            # Im Simulationsmodus macht _tx_worker den Loopback
            while self._running:
                time.sleep(0.1)
            return

        buf = np.zeros(self.cfg.rx_buf_size, dtype=np.complex64)
        while self._running:
            ret = self._sdr.readStream(
                self._rx_stream, [buf], len(buf),
                timeoutUs=1_000_000)
            if ret.ret > 0:
                self._rx_cb(buf[:ret.ret].copy())

    def set_frequency(self, freq_hz: float):
        """Frequenzwechsel zur Laufzeit (863 MHz ↔ 2400 MHz)."""
        self.cfg.freq_hz = freq_hz
        if not self._sim_mode and self._sdr:
            S = self._SoapySDR
            for ch in ([0, 1] if self.cfg.mimo else [0]):
                self._sdr.setFrequency(S.SOAPY_SDR_TX, ch, freq_hz)
                self._sdr.setFrequency(S.SOAPY_SDR_RX, ch, freq_hz)
        print(f"[XTRX] Frequenz → {freq_hz/1e6:.3f} MHz")

    def set_tx_gain(self, gain_db: float):
        """TX-Gain anpassen (0..73 dB für LMS7002M)."""
        self.cfg.tx_gain_db = gain_db
        if not self._sim_mode and self._sdr:
            S = self._SoapySDR
            for ch in ([0, 1] if self.cfg.mimo else [0]):
                self._sdr.setGain(S.SOAPY_SDR_TX, ch, gain_db)


# ─────────────────────────────────────────────
# Frequenz-Agility: 863 MHz ↔ 2.4 GHz
# ─────────────────────────────────────────────

FREQ_863  = 863e6    # EU Sub-GHz — bessere Reichweite, 1% Duty Cycle
FREQ_2400 = 2400e6   # 2.4 GHz — mehr Bandbreite, kürzere Reichweite

class DualBandRadio:
    """
    Wechselt zwischen 863 MHz (Langstrecke) und 2.4 GHz (Video) automatisch.

    Logik:
      - Standard: 863 MHz für Mesh-Control und Signalisierung
      - Video-Burst: 2.4 GHz für hohen Durchsatz
      - Auto-Switch basierend auf Queue-Tiefe und SNR

    WICHTIG EU-Regulierung:
      863 MHz: max. 1% Duty Cycle (= max. 36 ms pro 3.6 Sekunden Fenster)
      2.4 GHz: keine Duty-Cycle-Einschränkung (nur max. 100 mW EIRP)
    """

    def __init__(self, cfg: XTRXConfig,
                 rx_callback: Callable[[np.ndarray], None]):
        self._radio    = XTRXRadio(cfg, rx_callback)
        self._current  = cfg.freq_hz
        self._tx_time_863 = 0.0   # Für Duty-Cycle-Tracking
        self._window_start = time.time()

    def start(self):
        self._radio.start()

    def stop(self):
        self._radio.stop()

    def tx_863(self, iq: np.ndarray):
        """
        Sendet auf 863 MHz mit Duty-Cycle-Überwachung.
        Gibt False zurück wenn Duty Cycle erschöpft.
        """
        # 1% Duty Cycle: max. 36ms pro 3.6s Fenster
        now = time.time()
        if now - self._window_start > 3.6:
            self._window_start  = now
            self._tx_time_863   = 0.0

        tx_duration = len(iq) / self._radio.cfg.sample_rate
        if self._tx_time_863 + tx_duration > 0.036:
            return False  # Duty Cycle erschöpft

        if self._current != FREQ_863:
            self._radio.set_frequency(FREQ_863)
            self._current = FREQ_863

        self._radio.transmit(iq)
        self._tx_time_863 += tx_duration
        return True

    def tx_2400(self, iq: np.ndarray):
        """Sendet auf 2.4 GHz (kein Duty-Cycle-Limit)."""
        if self._current != FREQ_2400:
            self._radio.set_frequency(FREQ_2400)
            self._current = FREQ_2400
        self._radio.transmit(iq)
        return True
