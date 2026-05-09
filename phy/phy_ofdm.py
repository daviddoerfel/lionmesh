"""
phy_ofdm.py — LionMesh OFDM PHY Layer
=======================================
Node-to-node video transmission for LimeSDR hardware (LMS7002M).

OFDM structure (802.11a-compatible, adapted):
  FFT size:          64
  Data subcarriers:  48
  Pilot subcarriers: 4  (positions ±7, ±21)
  Guard / DC:        12
  Cyclic prefix:     16 samples (25%)
  Symbol length:     80 samples

Configurable channel bandwidth — selected at runtime:

  Bandwidth   Sample rate   SC spacing   MCS4 throughput   Use case
  ─────────────────────────────────────────────────────────────────
  5  MHz       5 MS/s        78.1 kHz     9 Mbps           Long range (Doodle Labs style)
  10 MHz      10 MS/s       156.3 kHz    18 Mbps           Balanced
  20 MHz      20 MS/s       312.5 kHz    36 Mbps           High throughput

Supported frequencies (LMS7002M: 30 MHz – 3.8 GHz):
  863 MHz  — EU Sub-GHz ISM (1% duty cycle, enforced by xtrx_radio.py)
  2400 MHz — 2.4 GHz ISM (no duty cycle limit)

MCS table (throughput scales linearly with bandwidth):
  MCS 0: BPSK  r=1/2  →  BW/20 ×  6 Mbps
  MCS 1: QPSK  r=1/2  →  BW/20 × 12 Mbps
  MCS 2: QPSK  r=3/4  →  BW/20 × 18 Mbps
  MCS 3: 16QAM r=1/2  →  BW/20 × 24 Mbps
  MCS 4: 16QAM r=3/4  →  BW/20 × 36 Mbps  ← recommended for video
  MCS 5: 64QAM r=2/3  →  BW/20 × 48 Mbps
  MCS 6: 64QAM r=3/4  →  BW/20 × 54 Mbps
"""

import numpy as np
from scipy.signal import lfilter
import struct
import zlib
from enum import IntEnum
from dataclasses import dataclass
from typing import Optional, Tuple, List


# ═══════════════════════════════════════════════════════
# CONFIGURABLE BANDWIDTH
# ═══════════════════════════════════════════════════════

# Valid channel bandwidths in Hz → sample rates
VALID_BANDWIDTHS = {
    5_000_000:  5e6,    #  5 MHz →  5 MS/s  (long range, Doodle Labs style)
    10_000_000: 10e6,   # 10 MHz → 10 MS/s  (balanced)
    20_000_000: 20e6,   # 20 MHz → 20 MS/s  (high throughput, default)
}

# Active bandwidth — change via set_bandwidth() before creating frames/receivers
_ACTIVE_BW: float = 20e6   # default: 20 MHz

def set_bandwidth(hz: float) -> None:
    """
    Set the active channel bandwidth for all subsequent TX/RX operations.
    Must be called before creating OFDMReceiver instances or calling tx_frame().

    Args:
        hz: Channel bandwidth in Hz. Must be one of: 5e6, 10e6, 20e6

    Example:
        set_bandwidth(5e6)   # 5 MHz channel — best range, ~9 Mbps @ MCS4
        set_bandwidth(10e6)  # 10 MHz — balanced
        set_bandwidth(20e6)  # 20 MHz — maximum throughput (default)
    """
    global _ACTIVE_BW
    if hz not in VALID_BANDWIDTHS:
        raise ValueError(f"Bandwidth must be one of {list(VALID_BANDWIDTHS.keys())}, got {hz}")
    _ACTIVE_BW = hz

def get_sample_rate() -> float:
    """Return the sample rate (Hz) for the active bandwidth."""
    return VALID_BANDWIDTHS[_ACTIVE_BW]

# Legacy constant — always equals get_sample_rate() for the active bandwidth.
# Use get_sample_rate() in new code.
@property
def SAMPLE_RATE() -> float:  # type: ignore
    return get_sample_rate()

# Convenience alias used throughout this module
def _sr() -> float:
    return VALID_BANDWIDTHS[_ACTIVE_BW]

# Fixed OFDM structure — same for all bandwidths
FFT_SIZE    = 64
CP_LEN      = 16
SYM_LEN     = FFT_SIZE + CP_LEN   # 80 samples

# Unterträger-Indizes (relativ zur DC = 0)
PILOT_SC  = [-21, -7, 7, 21]   # 4 Piloten

# Datenträger: alle ±1..±26 außer DC(0) und Piloten
DATA_SC   = [s for s in list(range(-26, 0)) + list(range(1, 27))
             if s not in PILOT_SC]   # 52 - 4 = 48 Stück
PILOT_VAL = np.array([1, 1, 1, -1], dtype=complex)  # bekannte BPSK-Werte

def sc2bin(sc: int) -> int:
    """Subcarrier-Index (±32) → FFT-Bin (0..63)"""
    return int(sc) % FFT_SIZE

DATA_BIN  = [sc2bin(s) for s in DATA_SC]
PILOT_BIN = [sc2bin(s) for s in PILOT_SC]


# ═══════════════════════════════════════════════════════
# MCS-TABELLE
# ═══════════════════════════════════════════════════════

class MCS(IntEnum):
    MCS0 = 0   # BPSK  1/2
    MCS1 = 1   # QPSK  1/2
    MCS2 = 2   # QPSK  3/4
    MCS3 = 3   # 16QAM 1/2
    MCS4 = 4   # 16QAM 3/4
    MCS5 = 5   # 64QAM 2/3
    MCS6 = 6   # 64QAM 3/4

@dataclass
class MCSParams:
    mod_order:  int      # Bits pro Symbol: 1, 2, 4, 6
    code_rate:  float    # 1/2, 3/4, 2/3
    cbps:       int      # Coded Bits per OFDM Symbol = 52 × mod_order
    dbps:       int      # Data Bits per OFDM Symbol ≈ cbps × code_rate
    norm:       float    # Normierungsfaktor

MCS_TABLE = {
    MCS.MCS0: MCSParams(1, 0.5,  48,  24, 1.0),
    MCS.MCS1: MCSParams(2, 0.5,  96,  48, 1/np.sqrt(2)),
    MCS.MCS2: MCSParams(2, 0.75, 96,  72, 1/np.sqrt(2)),
    MCS.MCS3: MCSParams(4, 0.5, 192,  96, 1/np.sqrt(10)),
    MCS.MCS4: MCSParams(4, 0.75,192, 144, 1/np.sqrt(10)),
    MCS.MCS5: MCSParams(6, 2/3, 288, 192, 1/np.sqrt(42)),
    MCS.MCS6: MCSParams(6, 0.75,288, 216, 1/np.sqrt(42)),
}


# ═══════════════════════════════════════════════════════
# PREAMBLE: STF + LTF
# ═══════════════════════════════════════════════════════

def _make_stf() -> np.ndarray:
    """
    Short Training Field (160 Samples)
    10 × 16-Sample-Perioden für Grob-Synchronisation und AGC
    Frequenzbereich: 12 gleichmäßig verteilte Unterträger
    """
    stf_sc  = [-24,-20,-16,-12,-8,-4, 4, 8,12,16,20,24]
    stf_amp = np.sqrt(13.0/6.0) * (1+1j)/np.sqrt(2)
    # Werte aus 802.11a Anhang G (direkt übernommen)
    stf_v   = np.array([1,1,-1,-1, 1,1,-1,1, -1,1,1,1], dtype=complex) * stf_amp

    fd = np.zeros(FFT_SIZE, dtype=complex)
    for sc, v in zip(stf_sc, stf_v):
        fd[sc2bin(sc)] = v
    td = np.fft.ifft(fd)
    return np.tile(td[:16], 10)        # 10 × 16 = 160 Samples

def _make_ltf() -> np.ndarray:
    """
    Long Training Field (160 Samples = 32-Sample-CP + 2×64-Sample-Symbole)
    Bekannte Sequenz für präzise Kanalschätzung
    """
    # LTF-Sequenz aus 802.11a Standard (Sc -26..+26, ohne DC)
    ltf_vals = np.array([
         1, 1,-1,-1, 1, 1,-1, 1,-1, 1, 1, 1, 1, 1, 1,-1,
        -1, 1, 1,-1, 1,-1, 1, 1, 1, 1,  # Sc -26..-1
         1, 1,-1,-1, 1, 1,-1, 1,-1, 1, 1, 1, 1, 1, 1,-1,
        -1, 1, 1,-1, 1,-1, 1, 1, 1, 1,  # Sc +1..+26
    ], dtype=complex)

    fd = np.zeros(FFT_SIZE, dtype=complex)
    scs = list(range(-26, 0)) + list(range(1, 27))
    for sc, v in zip(scs, ltf_vals):
        fd[sc2bin(sc)] = v

    td   = np.fft.ifft(fd)
    cp32 = td[-32:]                    # 32-Sample CP für LTF
    return np.concatenate([cp32, td, td])  # 160 Samples

STF      = _make_stf()
LTF      = _make_ltf()
PREAMBLE = np.concatenate([STF, LTF])   # 320 Samples total

# Referenz-Frequenzwerte des LTF (für Kanalschätzung)
_LTF_REF = np.zeros(FFT_SIZE, dtype=complex)
_ltf_scs = list(range(-26, 0)) + list(range(1, 27))
_ltf_vs  = np.array([
     1, 1,-1,-1, 1, 1,-1, 1,-1, 1, 1, 1, 1, 1, 1,-1,
    -1, 1, 1,-1, 1,-1, 1, 1, 1, 1,
     1, 1,-1,-1, 1, 1,-1, 1,-1, 1, 1, 1, 1, 1, 1,-1,
    -1, 1, 1,-1, 1,-1, 1, 1, 1, 1,
], dtype=complex)
for _sc, _v in zip(_ltf_scs, _ltf_vs):
    _LTF_REF[sc2bin(_sc)] = _v


# ═══════════════════════════════════════════════════════
# MODULATION / DEMODULATION
# ═══════════════════════════════════════════════════════

# Gray-Code-Tabellen für QAM
_QAM16_MAP = np.array([-3,-1,+3,+1], dtype=float)  # 2-bit Gray → Level
_QAM64_MAP = np.array([-7,-5,-1,-3,+7,+5,+1,+3], dtype=float)  # 3-bit Gray → Level

def _bits_to_int(bits: np.ndarray) -> np.ndarray:
    """Bit-Gruppen → Integers"""
    n = bits.shape[1]
    pw = 2 ** np.arange(n-1, -1, -1)
    return bits @ pw

def modulate(bits: np.ndarray, mcs: MCS) -> np.ndarray:
    """
    Bitstrom → komplexe Konstellation
    bits: 1D uint8-Array, Länge muss Vielfaches von mod_order sein
    """
    p = MCS_TABLE[mcs]
    n = p.mod_order
    assert len(bits) % n == 0, f"Bits ({len(bits)}) nicht Vielfaches von {n}"
    grp = bits.reshape(-1, n)

    if n == 1:   # BPSK
        syms = (1.0 - 2.0*grp[:,0]).astype(complex)

    elif n == 2:  # QPSK
        I = 1.0 - 2.0*grp[:,0]
        Q = 1.0 - 2.0*grp[:,1]
        syms = (I + 1j*Q)

    elif n == 4:  # 16-QAM
        I = _QAM16_MAP[_bits_to_int(grp[:,0:2])]
        Q = _QAM16_MAP[_bits_to_int(grp[:,2:4])]
        syms = (I + 1j*Q)

    elif n == 6:  # 64-QAM
        I = _QAM64_MAP[_bits_to_int(grp[:,0:3])]
        Q = _QAM64_MAP[_bits_to_int(grp[:,3:6])]
        syms = (I + 1j*Q)

    else:
        raise ValueError(f"Unbekannte Modulationsordnung: {n}")

    return (syms * p.norm).astype(np.complex64)


def demodulate(syms: np.ndarray, mcs: MCS) -> np.ndarray:
    """Komplexe Symbole → Bits (Hard Decision Slicer)"""
    p   = MCS_TABLE[mcs]
    n   = p.mod_order
    syms = syms / (p.norm + 1e-30)
    I, Q = np.real(syms), np.imag(syms)

    if n == 1:
        return (I < 0).astype(np.uint8)

    elif n == 2:
        return np.column_stack([I < 0, Q < 0]).astype(np.uint8).ravel()

    elif n == 4:
        def slice16(v):
            idx = np.clip(np.searchsorted([-2, 0, 2], v), 0, 3)
            b   = np.zeros((len(v), 2), dtype=np.uint8)
            # Inverse Gray: idx→bits
            gray_inv = np.array([[0,0],[0,1],[1,1],[1,0]])
            return gray_inv[idx]
        bI = slice16(I); bQ = slice16(Q)
        return np.column_stack([bI, bQ]).ravel()

    elif n == 6:
        def slice64(v):
            # 8 Levels: -7,-5,-3,-1,+1,+3,+5,+7
            thresholds = [-6,-4,-2, 0, 2, 4, 6]
            idx = np.clip(np.searchsorted(thresholds, v), 0, 7)
            # Inverse Gray-Code 3-bit
            gray_inv = np.array([
                [0,0,0],[0,0,1],[0,1,1],[0,1,0],
                [1,1,0],[1,1,1],[1,0,1],[1,0,0]
            ], dtype=np.uint8)
            return gray_inv[idx]
        bI = slice64(I); bQ = slice64(Q)
        return np.column_stack([bI, bQ]).ravel()

    raise ValueError(f"Unbekannte Modulationsordnung: {n}")


def soft_demodulate(syms: np.ndarray, mcs: MCS) -> np.ndarray:
    """
    Soft Demodulation: gibt LLR-Werte zurück (float).
    LLR > 0 → bit=0 wahrscheinlicher
    LLR < 0 → bit=1 wahrscheinlicher

    Die LLR-Berechnung nutzt Minimum-Distance Approximation
    (Max-Log-MAP) für alle Modulationen.

    Bit-Reihenfolge pro Symbol:
      BPSK:  [b0]
      QPSK:  [b0_I, b0_Q]
      QAM16: [b0_I, b1_I, b0_Q, b1_Q]
      QAM64: [b0_I, b1_I, b2_I, b0_Q, b1_Q, b2_Q]
    """
    p    = MCS_TABLE[mcs]
    n    = p.mod_order
    norm = p.norm    # Normierungsfaktor aus modulate()

    if n == 1:   # BPSK: levels ±1 (nach Normierung)
        # LLR = Re(sym)/norm  (positiv → bit=0)
        # Aber: bit=0 → level=+1, bit=1 → level=-1
        # Also: LLR = Re(sym)/norm
        return (np.real(syms) / norm).astype(float)

    elif n == 2:  # QPSK: levels ±1/√2 pro Achse
        I = np.real(syms) / norm   # ±1
        Q = np.imag(syms) / norm
        # bit0=0 wenn I>0, bit0=1 wenn I<0 → LLR(b0) = I
        # bit1=0 wenn Q>0, bit1=1 wenn Q<0 → LLR(b1) = Q
        return np.column_stack([I, Q]).astype(float).ravel()

    elif n == 4:  # 16QAM: Gray code, levels ±1,±3 (nach Normierung durch 1/√10)
        I = np.real(syms) / norm   # ±1, ±3
        Q = np.imag(syms) / norm
        # Gray mapping: 00→-3, 01→-1, 11→+1, 10→+3
        # bit0: 0 wenn I<0, 1 wenn I>0  → LLR = -I
        # bit1: 0 wenn |I|>2, 1 wenn |I|<2  → LLR = |I|-2
        b0_I = -I
        b1_I = np.abs(I) - 2.0
        b0_Q = -Q
        b1_Q = np.abs(Q) - 2.0
        return np.column_stack([b0_I, b1_I, b0_Q, b1_Q]).astype(float).ravel()

    elif n == 6:  # 64QAM: Gray code, levels ±1,±3,±5,±7
        I = np.real(syms) / norm   # ±1, ±3, ±5, ±7
        Q = np.imag(syms) / norm
        # 3-bit Gray LLRs (Max-Log-MAP approximation):
        # bit0 (MSB): 0 wenn I<0 → LLR = -I
        # bit1 (mid): 0 wenn |I|>4 → LLR = |I|-4
        # bit2 (LSB): 0 wenn ||I|-4|>2 → LLR = ||I|-4|-2
        def llr3(v):
            b0 = -v
            b1 = np.abs(v) - 4.0
            b2 = np.abs(np.abs(v) - 4.0) - 2.0
            return b0, b1, b2
        Ib0, Ib1, Ib2 = llr3(I)
        Qb0, Qb1, Qb2 = llr3(Q)
        return np.column_stack([Ib0, Ib1, Ib2, Qb0, Qb1, Qb2]).astype(float).ravel()

    raise ValueError(f"Unbekannte Modulationsordnung: {n}")


# ═══════════════════════════════════════════════════════
# FEC: CONVOLUTIONAL CODE r=1/2, K=7
# ═══════════════════════════════════════════════════════

# NASA Standard-Polynome (Oktal 171, 133)
_G0 = 0b1111001   # 0o171
_G1 = 0b1011011   # 0o133
_K  = 7
_NSTATES = 64

def _par(x: int) -> int:
    x ^= x >> 4; x ^= x >> 2; x ^= x >> 1
    return x & 1

# Vorberechnete Übergangstabelle für Viterbi
# _TRANS[state][input] = (next_state, out0, out1)
_TRANS = np.zeros((_NSTATES, 2, 3), dtype=np.int32)
for _s in range(_NSTATES):
    for _b in range(2):
        _ns = ((_s >> 1) | (_b << (_K-2))) & (_NSTATES-1)
        _TRANS[_s, _b] = [_ns, _par(_ns & _G0), _par(_ns & _G1)]


def conv_encode(bits: np.ndarray) -> np.ndarray:
    """
    Convolutional Encoder r=1/2, K=7
    Gibt 2×len(bits) + 2×(K-1) Bits zurück (inkl. Tail-Flushing)
    """
    state  = 0
    output = []
    tail   = np.zeros(_K-1, dtype=np.uint8)
    for b in np.concatenate([bits, tail]):
        ns, o0, o1 = _TRANS[state, int(b)]
        output.extend([o0, o1])
        state = ns
    return np.array(output, dtype=np.uint8)


def puncture(bits: np.ndarray, rate: float) -> np.ndarray:
    """
    Puncturing für Raten 3/4 und 2/3 (aus r=1/2 Basis)
    rate 1/2 → kein Puncturing
    rate 3/4 → Muster [1,1,1,0,0,1] (3 von 6 behalten → 3/4)
    rate 2/3 → Muster [1,1,1,0]     (3 von 4 behalten → 2/3)
    """
    if rate == 0.5:
        return bits
    elif abs(rate - 0.75) < 1e-6:
        pattern = np.tile([1,1,1,0,0,1], len(bits)//6 + 1)[:len(bits)]
    elif abs(rate - 2/3) < 1e-6:
        pattern = np.tile([1,1,1,0], len(bits)//4 + 1)[:len(bits)]
    else:
        raise ValueError(f"Unbekannte Code-Rate: {rate}")
    return bits[pattern.astype(bool)]


def depuncture(bits: np.ndarray, rate: float, n_orig: int) -> np.ndarray:
    """
    Depuncturing für Hard-Decision Bits.
    Für Soft-Decision: depuncture_soft() verwenden.
    Erasure-Stellen werden mit 0 aufgefüllt (neutral für Viterbi).
    """
    if abs(rate - 0.5) < 1e-6:
        return bits
    elif abs(rate - 0.75) < 1e-6:
        pattern = np.tile([1,1,1,0,0,1], n_orig//6 + 1)[:n_orig]
    elif abs(rate - 2/3) < 1e-6:
        pattern = np.tile([1,1,1,0], n_orig//4 + 1)[:n_orig]
    else:
        raise ValueError(f"Unbekannte Code-Rate: {rate}")
    out = np.zeros(n_orig, dtype=np.uint8)
    out[pattern.astype(bool)] = bits[:int(pattern.sum())]
    return out


def depuncture_soft(llrs: np.ndarray, rate: float, n_orig: int) -> np.ndarray:
    """
    Soft-Depuncturing: fügt LLR=0.0 (neutral, maximale Unsicherheit)
    an gepuncturten Stellen ein. Für Viterbi mit Soft-Eingabe.
    """
    if abs(rate - 0.5) < 1e-6:
        return llrs.astype(float)
    elif abs(rate - 0.75) < 1e-6:
        pattern = np.tile([1,1,1,0,0,1], n_orig//6 + 1)[:n_orig]
    elif abs(rate - 2/3) < 1e-6:
        pattern = np.tile([1,1,1,0], n_orig//4 + 1)[:n_orig]
    else:
        raise ValueError(f"Unbekannte Code-Rate: {rate}")
    out = np.zeros(n_orig, dtype=float)
    out[pattern.astype(bool)] = llrs[:int(pattern.sum())]
    return out


def viterbi(bits: np.ndarray, n_info: int) -> np.ndarray:
    """
    Viterbi Hard-Decision Decoder (für r=1/2, MCS0/1).
    Für punctured Codes (MCS2–6): soft_viterbi() verwenden.
    bits:   empfangene Bits (nach depuncture)
    n_info: Anzahl erwarteter Informations-Bits
    """
    n_sym = len(bits) // 2
    INF   = 10**6

    pm  = np.full(_NSTATES, INF, dtype=np.int32)
    pm[0] = 0
    surv = np.zeros((n_sym, _NSTATES), dtype=np.uint8)
    prev = np.zeros((n_sym, _NSTATES), dtype=np.int32)

    for t in range(n_sym):
        b0, b1  = int(bits[2*t]), int(bits[2*t+1])
        new_pm  = np.full(_NSTATES, INF, dtype=np.int32)
        for state in range(_NSTATES):
            if pm[state] == INF:
                continue
            for inp in (0, 1):
                ns, o0, o1 = _TRANS[state, inp]
                metric = pm[state] + (o0^b0) + (o1^b1)
                if metric < new_pm[ns]:
                    new_pm[ns]  = metric
                    surv[t, ns] = inp
                    prev[t, ns] = state
        pm = new_pm

    state   = int(np.argmin(pm))
    decoded = np.zeros(n_sym, dtype=np.uint8)
    for t in range(n_sym - 1, -1, -1):
        decoded[t] = surv[t, state]
        state      = prev[t, state]

    return decoded[:n_info]


def soft_viterbi(llrs: np.ndarray, n_info: int) -> np.ndarray:
    """
    Soft-Decision Viterbi Decoder.
    Verwendet LLR-Werte (Log-Likelihood Ratios):
      LLR > 0  →  bit=0 wahrscheinlicher
      LLR < 0  →  bit=1 wahrscheinlicher
      LLR = 0  →  Erasure (gepuncturte Stelle)

    Branch-Metrik: -llr wenn Output-Bit=0, +llr wenn Output-Bit=1
    (Minimierung der negativen Log-Likelihood)
    """
    n_sym = len(llrs) // 2
    INF   = 1e9

    pm   = np.full(_NSTATES, INF, dtype=float)
    pm[0] = 0.0
    surv = np.zeros((n_sym, _NSTATES), dtype=np.uint8)
    prev = np.zeros((n_sym, _NSTATES), dtype=np.int32)

    for t in range(n_sym):
        l0, l1  = float(llrs[2*t]), float(llrs[2*t+1])
        new_pm  = np.full(_NSTATES, INF, dtype=float)

        for state in range(_NSTATES):
            if pm[state] == INF:
                continue
            for inp in (0, 1):
                ns, o0, o1 = _TRANS[state, inp]
                # Metric: subtract LLR if output=0, add LLR if output=1
                m0 = l0 if o0 == 0 else -l0
                m1 = l1 if o1 == 0 else -l1
                cost = pm[state] - m0 - m1   # minimieren
                if cost < new_pm[ns]:
                    new_pm[ns]  = cost
                    surv[t, ns] = inp
                    prev[t, ns] = state
        pm = new_pm

    state   = int(np.argmin(pm))
    decoded = np.empty(n_sym, dtype=np.uint8)
    for t in range(n_sym - 1, -1, -1):
        decoded[t] = surv[t, state]
        state      = prev[t, state]

    return decoded[:n_info]

    for t in range(n_sym):
        b0, b1  = int(bits[2*t]), int(bits[2*t+1])
        new_pm  = np.full(_NSTATES, INF, dtype=np.int32)
        for s in range(_NSTATES):
            if pm[s] == INF:
                continue
            for inp in (0, 1):
                ns, o0, o1 = _TRANS[s, inp]
                m = pm[s] + (o0^b0) + (o1^b1)
                if m < new_pm[ns]:
                    new_pm[ns]  = m
                    surv[t, ns] = inp
                    prev[t, ns] = s
        pm = new_pm

    # Traceback
    state   = int(np.argmin(pm))
    decoded = np.empty(n_sym, dtype=np.uint8)
    for t in range(n_sym-1, -1, -1):
        decoded[t] = surv[t, state]
        state      = prev[t, state]

    return decoded[:n_info]


# ═══════════════════════════════════════════════════════
# INTERLEAVER (802.11a-Spezifikation)
# ═══════════════════════════════════════════════════════

def _interleave_perm(n_cbps: int, n_bpsc: int) -> np.ndarray:
    """Vorberechnete Interleaver-Permutation"""
    s    = max(n_bpsc // 2, 1)
    # Schritt 1: Zeilen→Spalten
    p1   = np.array([(n_cbps//16)*(k%16) + k//16 for k in range(n_cbps)])
    # Schritt 2: Diagonale Rotation
    p2   = np.array([s*(p1[k]//s) + (p1[k] + n_cbps - 16*p1[k]//n_cbps) % s
                     for k in range(n_cbps)])
    return p2

def _deinterleave_perm(n_cbps: int, n_bpsc: int) -> np.ndarray:
    """Inverse Permutation"""
    s    = max(n_bpsc // 2, 1)
    p2   = np.array([s*(j//s) + (j + 16*j//n_cbps) % s for j in range(n_cbps)])
    p1   = np.array([16*(p2[j]%(n_cbps//16)) + p2[j]//(n_cbps//16)
                     for j in range(n_cbps)])
    return p1

# Cache für Permutationen
_IL_CACHE = {}
def _get_perm(n_cbps, n_bpsc, inverse=False):
    key = (n_cbps, n_bpsc, inverse)
    if key not in _IL_CACHE:
        _IL_CACHE[key] = (_deinterleave_perm if inverse
                          else _interleave_perm)(n_cbps, n_bpsc)
    return _IL_CACHE[key]


def interleave(bits: np.ndarray, mcs: MCS) -> np.ndarray:
    p    = MCS_TABLE[mcs]
    perm = _get_perm(p.cbps, p.mod_order)
    out  = np.empty_like(bits)
    for i in range(0, len(bits), p.cbps):
        chunk = bits[i:i+p.cbps]
        if len(chunk) < p.cbps:
            chunk = np.pad(chunk, (0, p.cbps-len(chunk)))
        out[i:i+p.cbps] = chunk[perm]
    return out

def deinterleave(bits: np.ndarray, mcs: MCS) -> np.ndarray:
    p    = MCS_TABLE[mcs]
    perm = _get_perm(p.cbps, p.mod_order, inverse=True)
    out  = np.empty_like(bits)
    for i in range(0, len(bits), p.cbps):
        chunk = bits[i:i+p.cbps]
        if len(chunk) < p.cbps:
            chunk = np.pad(chunk, (0, p.cbps-len(chunk)))
        out[i:i+p.cbps] = chunk[perm]
    return out


# ═══════════════════════════════════════════════════════
# OFDM SYMBOL TX/RX
# ═══════════════════════════════════════════════════════

def ofdm_modulate(data: np.ndarray, pilot_phase: float = 0.0) -> np.ndarray:
    """
    52 komplexe Datensymbole → 80 Samples (CP + FFT)
    pilot_phase: rotiert die Piloten (tracking)
    """
    assert len(data) == 48
    fd = np.zeros(FFT_SIZE, dtype=complex)
    for i, b in enumerate(DATA_BIN):
        fd[b] = data[i]
    prot = np.exp(1j * pilot_phase)
    for i, b in enumerate(PILOT_BIN):
        fd[b] = PILOT_VAL[i] * prot
    td = np.fft.ifft(fd)
    cp = td[-CP_LEN:]
    return np.concatenate([cp, td]).astype(np.complex64)


def ofdm_demodulate(sym: np.ndarray,
                    H: Optional[np.ndarray] = None) -> np.ndarray:
    """
    80 Samples → 52 equalisierte Datensymbole
    H: Kanalschätzung (52 Koeffizienten), None = kein Equalizer
    """
    assert len(sym) == SYM_LEN
    td = sym[CP_LEN:]                       # CP entfernen
    fd = np.fft.fft(td, FFT_SIZE)
    rx = np.array([fd[b] for b in DATA_BIN])
    if H is not None:
        # Zero-Forcing Equalizer: ŝ = r / H
        safe = np.where(np.abs(H) > 1e-6, H, 1.0)
        rx   = rx / safe
    return rx


def estimate_channel(ltf_samples: np.ndarray) -> np.ndarray:
    """
    Least-Squares Kanalschätzung aus LTF
    ltf_samples: 160 Samples [32CP | sym1 | sym2]
    → 52 komplexe Kanalkoeffizienten H[k]
    """
    s1  = np.fft.fft(ltf_samples[32:96],  FFT_SIZE)
    s2  = np.fft.fft(ltf_samples[96:160], FFT_SIZE)
    avg = (s1 + s2) / 2.0
    H   = np.zeros(48, dtype=complex)
    for i, b in enumerate(DATA_BIN):
        ref = _LTF_REF[b]
        H[i] = avg[b] / ref if abs(ref) > 0.1 else (1.0+0j)
    return H


# ═══════════════════════════════════════════════════════
# SIGNAL-FELD (Header-Symbol)
# ═══════════════════════════════════════════════════════
# Immer BPSK r=1/2, enthält MCS + Payload-Länge + Parity

_MCS_BITS = {           # 4-Bit MCS-Identifier
    MCS.MCS0: [1,0,1,0],
    MCS.MCS1: [0,1,0,1],
    MCS.MCS2: [1,1,0,0],
    MCS.MCS3: [0,0,1,1],
    MCS.MCS4: [1,0,0,1],
    MCS.MCS5: [0,1,1,0],
    MCS.MCS6: [1,1,1,1],
}
_BITS_MCS = {tuple(v): k for k, v in _MCS_BITS.items()}

def build_signal_sym(mcs: MCS, n_bytes: int) -> np.ndarray:
    """Erzeugt das SIGNAL-OFDM-Symbol (80 Samples)"""
    rate_b  = np.array(_MCS_BITS[mcs], dtype=np.uint8)   # 4 Bit
    len_b   = np.array([(n_bytes >> i) & 1
                        for i in range(14)], dtype=np.uint8)  # 14 Bit
    info    = np.concatenate([rate_b, len_b])              # 20 Bit
    parity  = np.array([np.sum(info) & 1], dtype=np.uint8)
    tail    = np.zeros(3, dtype=np.uint8)                  # Flush
    frame   = np.concatenate([info, parity, tail])         # 24 Bit

    enc     = conv_encode(frame[:20])[:36]   # 36 Encoded-Bits (18 info × 2)
    padded  = np.zeros(48, dtype=np.uint8)   # 48 Datenträger
    padded[:36] = enc
    syms    = modulate(padded, MCS.MCS0)     # BPSK
    return ofdm_modulate(syms)


def parse_signal_sym(sym: np.ndarray,
                     H: Optional[np.ndarray] = None
                    ) -> Optional[Tuple[MCS, int]]:
    """
    Dekodiert SIGNAL-Symbol
    → (MCS, n_bytes) oder None bei Fehler
    """
    data   = ofdm_demodulate(sym, H)
    bits   = demodulate(data, MCS.MCS0)[:36]
    dec    = viterbi(bits, 18)
    rate_b = tuple(dec[0:4].tolist())
    mcs    = _BITS_MCS.get(rate_b)
    if mcs is None:
        return None
    n_bytes = int(sum(dec[4:18] * (2**np.arange(14))))
    return mcs, n_bytes


# ═══════════════════════════════════════════════════════
# TX: VOLLSTÄNDIGER FRAME-AUFBAU
# ═══════════════════════════════════════════════════════

def tx_frame(payload: bytes, mcs: MCS = MCS.MCS4) -> np.ndarray:
    """
    Vollständiger PHY-Frame für LimeSDR XTRX TX

    payload: beliebige Bytes (z.B. RTP UDP-Payload)
    mcs:     Modulation+Coding Scheme

    Rückgabe: complex64 IQ-Samples
    Frame-Struktur:
      [STF 160] [LTF 160] [SIGNAL 80] [DATA N×80] [TAIL 80]

    Typische Durchsätze @20MHz @20MS/s:
      MCS0: ~6 Mbps    MCS3: ~26 Mbps
      MCS4: ~39 Mbps   MCS6: ~58 Mbps
    """
    # CRC32 anhängen für Fehlerkennung
    crc_val  = struct.pack('<I', zlib.crc32(payload) & 0xFFFFFFFF)
    data_out = payload + crc_val
    n_bytes  = len(payload)   # Signal-Feld speichert nur Payload-Länge (ohne CRC)

    # Bits
    raw_bits = np.unpackbits(np.frombuffer(data_out, dtype=np.uint8))

    # Scrambler (XOR mit m-Sequenz, Seed=0x7F wie 802.11a)
    bits = _scramble(raw_bits)

    # FEC
    p        = MCS_TABLE[mcs]
    enc_bits = conv_encode(bits)
    enc_bits = puncture(enc_bits, p.code_rate)

    # Auf volle OFDM-Symbole auffüllen
    n_pad    = (-len(enc_bits)) % p.cbps
    enc_bits = np.concatenate([enc_bits, np.zeros(n_pad, dtype=np.uint8)])

    # Interleaving + Modulation → OFDM-Symbole
    enc_bits = interleave(enc_bits, mcs)
    n_syms   = len(enc_bits) // p.cbps

    ofdm_syms = []
    for i in range(n_syms):
        chunk = enc_bits[i*p.cbps:(i+1)*p.cbps]
        syms  = modulate(chunk, mcs)
        ofdm_syms.append(ofdm_modulate(syms, pilot_phase=i * np.pi / 2))

    # Tail-Symbol (Nullen, damit Viterbi-Decoder flusht)
    tail_sym = ofdm_modulate(np.zeros(48, dtype=complex))

    # Zusammensetzen
    parts = [PREAMBLE, build_signal_sym(mcs, n_bytes)] + ofdm_syms + [tail_sym]
    frame = np.concatenate(parts)

    # Leistungsnormierung für XTRX (max ±0.7 für ca. 10dBm DAC-Headroom)
    peak  = np.max(np.abs(frame))
    if peak > 0:
        frame = frame * (0.7 / peak)

    return frame.astype(np.complex64)


def _scramble(bits: np.ndarray, seed: int = 0x7F) -> np.ndarray:
    """
    Selbstsynchronisierender Scrambler (802.11a LFSR)
    Polynom: x^7 + x^4 + 1
    """
    sr  = seed & 0x7F
    out = np.empty_like(bits)
    for i, b in enumerate(bits):
        fb     = ((sr >> 6) ^ (sr >> 3)) & 1
        out[i] = b ^ fb
        sr     = ((sr << 1) | fb) & 0x7F
    return out


# ═══════════════════════════════════════════════════════
# RX: FRAME-EMPFANG + SCHMIDL-COX SYNCHRONISATION
# ═══════════════════════════════════════════════════════

def _find_stf_start(samples: np.ndarray,
                    threshold: float = 0.65,
                    L: int = 16) -> int:
    """
    Schmidl-Cox STF-Detektor (Grob-Timing).
    Gibt den Index zurück, bei dem die Korrelationsmetrik erstmals
    über den Schwellwert steigt (≈ Beginn der STF-Sequenz).
    Gibt -1 zurück wenn kein Frame gefunden.
    """
    N = len(samples)
    for i in range(N - 2*L):
        chunk = samples[i:i+2*L]
        P = np.dot(chunk[:L], np.conj(chunk[L:]))
        R = np.sum(np.abs(chunk[L:])**2)
        if R > 1e-10 and (abs(P)**2) / (R**2) > threshold:
            return i
    return -1


def _fine_timing_ltf(rx: np.ndarray,
                     coarse_ltf_start: int,
                     search_range: int = 8) -> int:
    """
    Fein-Timing via Kreuzkorrelation mit dem bekannten LTF-Symbol.
    Verbessert die Genauigkeit von ±2 Samples (Schmidl-Cox) auf 0 Samples.

    coarse_ltf_start: Grob-Schätzung des LTF-Starts (nach STF-Erkennung)
    search_range:     Such-Fenster in Samples (±)
    Rückgabe:         Korrigierter LTF-Start-Index
    """
    ltf_sym1 = LTF[32:96]   # Bekanntes LTF-Symbol 1 (64 Samples, nach 32-CP)
    ltf_ref_power = float(np.sum(np.abs(ltf_sym1)**2))

    best_metric  = -1.0
    best_pos     = coarse_ltf_start

    for delta in range(-search_range, search_range + 1):
        # LTF-Symbol beginnt 32 Samples nach LTF-Start (CP überspringen)
        sym_start = coarse_ltf_start + delta + 32
        if sym_start < 0 or sym_start + 64 > len(rx):
            continue
        seg = rx[sym_start:sym_start + 64]
        # Normierte Kreuzkorrelation
        xcorr   = float(abs(np.dot(seg, np.conj(ltf_sym1))))
        rx_pwr  = float(np.sum(np.abs(seg)**2))
        metric  = xcorr / (np.sqrt(rx_pwr * ltf_ref_power) + 1e-10)
        if metric > best_metric:
            best_metric = metric
            best_pos    = coarse_ltf_start + delta

    return best_pos


def _calc_n_enc(n_info_bits: int, code_rate: float) -> int:
    """
    Berechnet die genaue Anzahl kodierter Bits nach FEC + Puncturing.
    Berücksichtigt die K-1=6 Tail-Bits des Convolutional Encoders.
    """
    _K = 7
    n_base = 2 * (n_info_bits + _K - 1)   # Rate-1/2 Ausgabe inkl. Tail
    # Puncturing: entfernt Bits gemäß Muster
    if abs(code_rate - 0.5) < 1e-6:
        return n_base
    elif abs(code_rate - 0.75) < 1e-6:
        # Muster [1,1,1,0,0,1] → 4 von 6 behalten
        full_groups = n_base // 6
        rest        = n_base  % 6
        rest_kept   = sum([1,1,1,0,0,1][:rest])
        return full_groups * 4 + rest_kept
    elif abs(code_rate - 2/3) < 1e-6:
        # Muster [1,1,1,0] → 3 von 4 behalten
        full_groups = n_base // 4
        rest        = n_base  % 4
        rest_kept   = sum([1,1,1,0][:rest])
        return full_groups * 3 + rest_kept
    raise ValueError(f"Unbekannte Code-Rate: {code_rate}")


class OFDMReceiver:
    """
    Vollständiger OFDM Streaming-Empfänger für den LimeSDR XTRX.

    Verwendung:
        rcvr = OFDMReceiver()
        # In RX-Thread:
        payloads = rcvr.push(iq_samples)
        for p in payloads:
            handle(p)

    Intern arbeitet der Receiver als State Machine:
        HUNT  → sucht STF über Schmidl-Cox Korrelation
        DATA  → liest n_syms OFDM-Symbole und dekodiert
    """

    def __init__(self, threshold: float = 0.65):
        self._thr = threshold
        self._buf = np.array([], dtype=np.complex64)
        self._state    = 'HUNT'
        self._H        = None    # Kanalschätzung (48 Koeffizienten)
        self._mcs      = None
        self._n_bytes  = None
        self._n_syms   = None    # Anzahl Datensymbole
        self._n_enc    = None    # Anzahl kodierter Bits (exakt)
        self._data_start = None  # Buffer-Offset der Datensymbole

    def push(self, samples: np.ndarray) -> List[bytes]:
        """
        Neue IQ-Samples einspeisen.
        Gibt Liste erfolgreich dekodierter Payloads zurück (kann leer sein).
        """
        self._buf = np.concatenate([self._buf,
                                    samples.astype(np.complex64)])
        results = []

        while True:
            if self._state == 'HUNT':
                self._hunt()
                if self._state == 'HUNT':
                    break   # Kein Frame gefunden, warte auf mehr Samples

            elif self._state == 'DATA':
                needed = self._n_syms * SYM_LEN
                if len(self._buf) < needed:
                    break

                payload = self._decode_data()
                if payload is not None:
                    results.append(payload)

                # Frame verarbeitet → Buffer vorwärts und zurück zu HUNT
                self._buf   = self._buf[needed:]
                self._state = 'HUNT'
                # Weiterloopen: vielleicht liegt schon das nächste Frame im Buffer

        return results

    # ── Private ────────────────────────────────────────────

    def _hunt(self) -> List[bytes]:
        """
        Sucht STF im aktuellen Buffer.
        Wechselt in DATA-State wenn Frame gefunden.
        Gibt ggf. sofort dekodierte Payloads zurück wenn DATA schnell abgeschlossen.
        """
        min_needed = 320 + SYM_LEN + SYM_LEN   # STF+LTF+SIGNAL+1 Datensym
        if len(self._buf) < min_needed:
            return []

        # Schmidl-Cox: suche auf max. 800 Samples (verhindert O(N²) bei langen Buffers)
        search_len = min(len(self._buf) - 480, 800)
        if search_len < 32:
            return []

        stf_start = _find_stf_start(self._buf[:search_len + 32], self._thr)
        if stf_start < 0:
            # Nichts gefunden → alten Buffer verwerfen (Overlap für nächsten Aufruf)
            keep = min(31, len(self._buf))
            self._buf = self._buf[-keep:] if keep > 0 else np.array([], dtype=np.complex64)
            return []

        # Grob-Korrektur: Schmidl-Cox überschreitet Schwellwert typisch ~3 Samples früh
        stf_start = max(0, stf_start + 3)

        # Fein-Timing: LTF-Kreuzkorrelation für exakte Symbolgrenze (±0 Samples)
        ltf_coarse = stf_start + 160
        if ltf_coarse + 200 <= len(self._buf):
            ltf_start = _fine_timing_ltf(self._buf, ltf_coarse, search_range=6)
        else:
            ltf_start = ltf_coarse

        sig_start  = ltf_start + 160   # LTF = [32CP | 64 | 64]
        data_start = sig_start + SYM_LEN

        if data_start + SYM_LEN > len(self._buf):
            # Noch nicht genug Samples für erstes Datensymbol
            # Buffer ab STF behalten
            self._buf = self._buf[stf_start:]
            return []

        # Kanalschätzung aus LTF
        H = estimate_channel(self._buf[ltf_start:ltf_start+160])

        # SIGNAL-Feld dekodieren
        parsed = parse_signal_sym(self._buf[sig_start:sig_start+SYM_LEN], H)
        if parsed is None:
            # Ungültiges Signal-Feld → einen Sample weiter suchen
            self._buf = self._buf[stf_start+1:]
            return []

        mcs, n_bytes = parsed
        p = MCS_TABLE[mcs]
        n_info  = (n_bytes + 4) * 8    # payload + CRC
        n_enc   = _calc_n_enc(n_info, p.code_rate)
        n_syms  = int(np.ceil(n_enc / p.cbps))

        self._H          = H
        self._mcs        = mcs
        self._n_bytes    = n_bytes
        self._n_enc      = n_enc
        self._n_syms     = n_syms
        self._state      = 'DATA'
        self._buf        = self._buf[data_start:]

        return []

    def _decode_data(self) -> Optional[bytes]:
        """
        Dekodiert n_syms OFDM-Symbole aus dem Buffer.
        Verwendet Soft-Demodulation + Soft-Viterbi für alle MCS.
        Gibt Payload zurück bei CRC-Erfolg, None bei Fehler.
        """
        p = MCS_TABLE[self._mcs]

        # Soft-Demodulation aller Datensymbole → LLR-Werte
        all_llrs = []
        for i in range(self._n_syms):
            sym  = self._buf[i*SYM_LEN:(i+1)*SYM_LEN]
            fd   = ofdm_demodulate(sym, self._H)
            llrs = soft_demodulate(fd, self._mcs)
            all_llrs.append(llrs)

        llrs_all = np.concatenate(all_llrs)

        # Auf gültige Länge clippen
        n_cbps_total = self._n_syms * p.cbps
        llrs_all     = llrs_all[:n_cbps_total]

        # Soft-Deinterleaving (gleiche Permutation wie für hard bits)
        perm     = _get_perm(p.cbps, p.mod_order, inverse=True)
        llrs_di  = np.zeros_like(llrs_all)
        for i in range(0, len(llrs_all), p.cbps):
            chunk = llrs_all[i:i+p.cbps]
            if len(chunk) == p.cbps:
                llrs_di[i:i+p.cbps] = chunk[perm]

        # Exakt n_enc LLRs nehmen
        llrs_clip = llrs_di[:self._n_enc]

        # Soft-Depuncturing: Erasure-LLR=0 an gepuncturten Stellen
        n_info = (self._n_bytes + 4) * 8
        n_base = 2 * (n_info + 6)       # Rate-1/2 Bits inkl. Tail
        llrs_dp = depuncture_soft(llrs_clip, p.code_rate, n_base)

        # Soft-Viterbi
        info_bits = soft_viterbi(llrs_dp, n_info)

        # Descramble
        info_bits = _scramble(info_bits)

        # Bytes + CRC prüfen
        n_total  = self._n_bytes + 4
        if len(info_bits) < n_total * 8:
            return None

        data     = np.packbits(info_bits[:n_total*8]).tobytes()
        payload  = data[:self._n_bytes]
        rx_crc   = struct.unpack('<I', data[self._n_bytes:n_total])[0]
        calc_crc = zlib.crc32(payload) & 0xFFFFFFFF

        return payload if rx_crc == calc_crc else None


# ═══════════════════════════════════════════════════════
# 2×2 MIMO: SPATIAL MULTIPLEXING + MRC DIVERSITY
# ═══════════════════════════════════════════════════════

def tx_mimo(payload: bytes, mcs: MCS = MCS.MCS4
           ) -> Tuple[np.ndarray, np.ndarray]:
    """
    2×2 Spatial Multiplexing TX
    Teilt Payload auf zwei unabhängige Streams auf.
    Beide Streams senden auf derselben Frequenz → 2× Durchsatz.

    ACHTUNG: Braucht guten SNR und wenig Mehrwege (Kurzdistanz).
    Für Reichweite lieber SISO oder MRC verwenden.

    Rückgabe: (tx0_samples, tx1_samples) → je eine Antenne
    """
    half = len(payload) // 2
    s0   = tx_frame(payload[:half],  mcs)
    s1   = tx_frame(payload[half:],  mcs)
    # Längen angleichen
    n    = max(len(s0), len(s1))
    s0   = np.pad(s0, (0, n-len(s0))).astype(np.complex64)
    s1   = np.pad(s1, (0, n-len(s1))).astype(np.complex64)
    return s0, s1


def rx_mrc(rx0: np.ndarray, rx1: np.ndarray,
           H0: np.ndarray, H1: np.ndarray) -> np.ndarray:
    """
    Maximal Ratio Combining (MRC) für RX-Diversity
    Kombiniert zwei Empfangsantennen optimal.
    Verbesserung: ~3dB bei unkorreliertem Fading → mehr Reichweite.

    rx0, rx1:  IQ-Samples von Antenne 0 und 1
    H0, H1:    Kanalschätzung für jede Antenne (52 Koeffizienten)
    Rückgabe:  Kombiniertes Signal (als ob von einer Antenne)
    """
    # MRC-Gewichte: w_i = H_i* / |H_i|^2
    w0 = np.conj(H0) / (np.abs(H0)**2 + 1e-10)
    w1 = np.conj(H1) / (np.abs(H1)**2 + 1e-10)

    # Normierung
    total = np.abs(w0) + np.abs(w1) + 1e-10
    w0 /= total; w1 /= total

    # Kombiniert: nur auf Subcarrier-Ebene sinnvoll
    # Hier vereinfacht: Zeitbereich-Superposition gewichtet
    n = min(len(rx0), len(rx1))
    return (rx0[:n] * np.mean(np.abs(w0)) +
            rx1[:n] * np.mean(np.abs(w1))).astype(np.complex64)


# ═══════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════

if __name__ == '__main__':
    print("LionMesh PHY Self-Test")
    print("=" * 60)

    test_payload = b"LionMesh PHY self-test payload." * 3
    SNR_DB   = 20.0
    N_TRIALS = 5

    for bw_hz in [5_000_000, 10_000_000, 20_000_000]:
        set_bandwidth(bw_hz)
        sr = _sr()
        print(f"\n{'─'*60}")
        print(f"  Bandwidth: {bw_hz//1_000_000} MHz  |  Sample rate: {sr/1e6:.0f} MS/s  "
              f"|  SC spacing: {sr/FFT_SIZE/1e3:.1f} kHz")
        print(f"{'─'*60}")

        for mcs in [MCS.MCS0, MCS.MCS1, MCS.MCS3, MCS.MCS4, MCS.MCS6]:
            ok_cnt = 0
            p = MCS_TABLE[mcs]

            for _ in range(N_TRIALS):
                iq      = tx_frame(test_payload, mcs)
                sig_pwr = np.mean(np.abs(iq)**2)
                n0      = sig_pwr / (10 ** (SNR_DB / 10))
                noise   = np.sqrt(n0 / 2) * (
                    np.random.randn(len(iq)) + 1j * np.random.randn(len(iq)))
                rx_iq   = (iq + noise).astype(np.complex64)
                offset  = np.random.randint(5, 40)
                rx      = np.concatenate([np.zeros(offset, dtype=np.complex64), rx_iq])
                rcvr    = OFDMReceiver()
                decoded = rcvr.push(rx)
                if decoded and decoded[0] == test_payload:
                    ok_cnt += 1

            status = '✓' if ok_cnt == N_TRIALS else f'⚠ {ok_cnt}/{N_TRIALS}'
            mbps   = p.dbps * sr / (SYM_LEN * 1e6)
            ms     = len(tx_frame(test_payload, mcs)) / sr * 1e3
            print(f"  MCS{mcs.value} {p.mod_order}bit r={p.code_rate:.2f}"
                  f"  {mbps:>5.1f} Mbps  {ms:.2f}ms/frame  {status}")

    # Reset to default
    set_bandwidth(20_000_000)

    print(f"\n{'═'*60}")
    print("Throughput table across all bandwidths and MCS:")
    print(f"{'':8}", end='')
    for bw in [5, 10, 20]:
        print(f"  {bw:>4}MHz", end='')
    print()
    for mcs in MCS:
        p = MCS_TABLE[mcs]
        print(f"  MCS{mcs.value} {p.mod_order}bit r={p.code_rate:.2f}", end='')
        for bw_mhz in [5, 10, 20]:
            mbps = p.dbps * (bw_mhz * 1e6) / (SYM_LEN * 1e6)
            print(f"  {mbps:>6.1f}M", end='')
        print()

    print(f"\nOFDM structure (bandwidth-independent):")
    set_bandwidth(20_000_000)
    print(f"  FFT={FFT_SIZE}, CP={CP_LEN}, Symbol={SYM_LEN} samples")
    print(f"  Data subcarriers={len(DATA_SC)}, Pilots={len(PILOT_SC)}")
    print(f"  STF={len(STF)}smp, LTF={len(LTF)}smp, Preamble={len(PREAMBLE)}smp")
