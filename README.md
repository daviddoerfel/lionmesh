<p align="center">
# LionMesh
</p>

<p align="center">
  <img src="assets/lionmesh.png" alt="LionMesh tactical mesh radio logo" width="260">
</p>

<p align="center">
  <strong>Tactical field mesh radio for resilient off-grid video communication.</strong>
</p>

<p align="center">
No infrastructure. No proprietary MANET radio. No GNU Radio required.

![License: MIT](https://img.shields.io/badge/License-MIT-mutedwhite)
![Python](https://img.shields.io/badge/Python-3.x-electriccyan)
![Hardware](https://img.shields.io/badge/Hardware-Raspberry%20Pi%20%7C%20LimeSDR%20%7C%20LoRa-neonteal)

</p>

---

## What is LionMesh?

LionMesh is a self-contained tactical mesh radio system designed for field operations where no infrastructure exists — disaster relief, search and rescue, civil protection, tactical training, remote deployments, and resilient field communications.

It runs on Raspberry Pi hardware and combines two independent communication layers:

- **Control plane** — LoRa 868 MHz via Meshtastic. Handles GPS position broadcasting, node discovery, and fallback messaging. Always active, very low power, and suitable for multi-kilometre field coverage.
- **Data plane** — Either a custom OFDM PHY over LimeSDR, or standard 802.11 WiFi via batman-adv. Carries HD video, files, telemetry, and the management web interface.

A web interface accessible from any node in the mesh displays all nodes on a live map, GPS positions, link quality, radio state, and video feeds.

LionMesh is built for serious field use: simple deployment, local operation, open architecture, and transparent radio behaviour.

---

## Why LionMesh?

Commercial MANET radios such as Persistent Systems MPU5 or Silvus StreamCaster provide advanced tactical mesh networking, but they are expensive, closed systems, and often difficult to modify or study.

LionMesh is an open-source attempt to close part of that gap with accessible hardware, transparent code, and an architecture that can be studied, modified, and deployed by technical users.

The LionMesh PHY is a custom OFDM transceiver written from scratch in Python. It implements the full signal-processing chain:

- Schmidl-Cox synchronisation
- least-squares channel estimation
- zero-forcing equalisation
- convolutional FEC with soft-decision Viterbi decoding
- adaptive modulation from BPSK to 64-QAM
- bandwidth modes of 5 / 10 / 20 MHz

No GNU Radio.  
No pre-built SDR stack.  
No black-box MANET firmware.

---

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                        WebApp  :8080                            │
│             Leaflet map · Node list · Video panel               │
├─────────────────────────────────────────────────────────────────┤
│                     Node Daemon  (FastAPI)                      │
│         REST API · Node registry · Band switching               │
├───────────────────────────┬─────────────────────────────────────┤
│      Control plane        │          Data plane                 │
│                           │                                     │
│  LoRa 868 MHz             │  LionMesh OFDM PHY                  │
│  Meshtastic firmware      │    LimeSDR Mini 2 or XTRX           │
│  GPS position broadcast   │    863 MHz / 2400 MHz               │
│  Node discovery           │    5 / 10 / 20 MHz bandwidth        │
│  Fallback text messaging  │    MCS 0–6 · up to 54 Mbps          │
│                           │    H.265 video · RTP                │
│                           │                                     │
│                           │  — or —                             │
│                           │                                     │
│                           │  Standard WiFi (batman-adv)         │
│                           │    Any 802.11 adapter               │
│                           │    No LimeSDR required              │
├───────────────────────────┼─────────────────────────────────────┤
│  Seeed WM1302 LoRa HAT    │  LimeSDR Mini 2 / XTRX              │
│  u-blox GPS module        │  — or — Alfa WiFi adapter           │
└───────────────────────────┴─────────────────────────────────────┘
```

Both planes operate independently. Losing the WiFi or LimeSDR data link does not affect LoRa, and losing LoRa does not disable the data plane.

---

## Hardware

### Per node

| Component | Purpose | Required |
|---|---|---|
| Raspberry Pi 4B or CM4 | Main compute | Always |
| Seeed WM1302 LoRa HAT | Control plane 868 MHz | Always |
| u-blox GPS module | Position tracking | Always |
| UPS / battery board | Field power | Always |
| LimeSDR Mini 2 or XTRX | LionMesh data plane | Mode: `lionmesh` |
| Alfa USB WiFi adapter | Standard WiFi data plane | Mode: `wifi` |
| 3D-printed enclosure | Field housing | Recommended |

The LimeSDR is optional. Set `mode = wifi` in `node.conf` to run without a LimeSDR. The full system — LoRa control plane, GPS, node registry, WebApp, and batman-adv mesh — works with a standard WiFi adapter.

The LionMesh OFDM PHY is only used in `mode = lionmesh`.

---

## Radio modes

| Mode | Frequency | Bandwidth | Regulatory status | Range with omni antennas | MCS4 video |
|---|---:|---:|---|---:|---|
| `wifi` | 2.4 / 5 GHz | — | Legal | 100–400 m | ~20 Mbps net |
| `lionmesh` | 2400 MHz | 5 MHz | Legal | 800 m–2 km | 9 Mbps / 720p |
| `lionmesh` | 2400 MHz | 10 MHz | Legal | 400 m–1 km | 18 Mbps / 1080p |
| `lionmesh` | 2400 MHz | 20 MHz | Legal | 200–500 m | 36 Mbps / 1080p |
| `lionmesh` | 863 MHz | 5 MHz | Regulatory grey zone | 2–5 km | 9 Mbps / 720p |

The 5 MHz channel gives a +6 dB SNR gain over 20 MHz at the same TX power — roughly twice the range.

Recommended general-use profile:

```text
2400 MHz · 5 MHz BW · MCS 4 → 9 Mbps net throughput
```

Maximum experimental range profile:

```text
863 MHz · 5 MHz BW · MCS 4 → 9 Mbps net throughput
```

Use 863 MHz wideband operation only in authorised deployments, emergency-service contexts, controlled tests, or other scenarios where the operator has confirmed regulatory permission.

---

## LionMesh PHY

### OFDM parameters

| Parameter | Value |
|---|---:|
| FFT size | 64 |
| Data subcarriers | 48 |
| Pilot subcarriers | 4, positions ±7 and ±21 |
| Cyclic prefix | 16 samples, 25% |
| Symbol duration | 80 samples |
| Sample rate | 5 / 10 / 20 MS/s |
| Subcarrier spacing | 78.1 / 156.3 / 312.5 kHz |

### MCS table

Throughput scales linearly with bandwidth. All values are net data rate after FEC overhead.

| MCS | Modulation | Code rate | Min SNR | 5 MHz | 10 MHz | 20 MHz |
|---:|---|---:|---:|---:|---:|---:|
| 0 | BPSK | 1/2 | 8 dB | 1.5 Mbps | 3 Mbps | 6 Mbps |
| 1 | QPSK | 1/2 | 10 dB | 3 Mbps | 6 Mbps | 12 Mbps |
| 2 | QPSK | 3/4 | 13 dB | 4.5 Mbps | 9 Mbps | 18 Mbps |
| 3 | 16-QAM | 1/2 | 16 dB | 6 Mbps | 12 Mbps | 24 Mbps |
| 4 | 16-QAM | 3/4 | 19 dB | 9 Mbps | 18 Mbps | 36 Mbps |
| 5 | 64-QAM | 2/3 | 23 dB | 12 Mbps | 24 Mbps | 48 Mbps |
| 6 | 64-QAM | 3/4 | 25 dB | 13.5 Mbps | 27 Mbps | 54 Mbps |

Recommended default:

```text
2400 MHz · 5 MHz BW · MCS 4 → 9 Mbps
```

This is sufficient for 720p H.265 at 6–8 Mbps.

### Signal-processing pipeline

```text
TX path:
  Payload → CRC32 → Scrambler → Conv. encoder (K=7) → Puncturing
  → Bit interleaver → QAM modulator → OFDM (IFFT + CP) → Preamble → IQ

RX path:
  IQ → Schmidl-Cox coarse sync → LTF cross-correlation fine timing
  → LS channel estimation → ZF equaliser → Soft LLR demodulation
  → Bit deinterleaver → Soft Viterbi decoder → Descrambler → CRC check
```

---

## Software stack

```text
lionmesh/
├── phy/                    LionMesh OFDM PHY
│   ├── phy_ofdm.py         TX/RX, sync, channel estimation, FEC, Viterbi
│   ├── mac_simple.py       MAC — ARQ, datagram mode, adaptive MCS
│   ├── xtrx_radio.py       SoapySDR interface + AWGN simulation fallback
│   └── video_pipe.py       H.265 GStreamer pipeline + RTP fragmentation
│
├── control/                Control plane
│   ├── lora.py             Meshtastic interface — GPS broadcast, discovery
│   ├── gps.py              gpsd position reader
│   └── mesh.py             batman-adv status and band switching
│
├── daemon/                 Node daemon
│   ├── main.py             Entry point — orchestrates all subsystems
│   ├── api.py              FastAPI REST API + WebApp serving
│   ├── radio.py            Radio abstraction — LionMesh PHY or WiFi
│   └── registry.py         In-memory node registry
│
├── webapp/                 Web interface
│   ├── templates/          index.html — Leaflet map + node list + video
│   └── static/             app.js, style.css
│
├── config/
│   └── node.conf.example   Configuration template
│
└── setup/                  Raspberry Pi setup scripts
    ├── 01_system.sh
    ├── 02_mesh_wifi.sh
    ├── 03_lora_meshtastic.sh
    ├── 04_lionmesh.sh
    └── 05_webapp.sh
```

---

## Setup

### Requirements

- Raspberry Pi 4B or CM4
- Raspberry Pi OS Lite 64-bit
- Internet connection for initial setup
- LoRa control hardware
- Optional LimeSDR Mini 2 / XTRX for the custom OFDM data plane

### Installation

```bash
git clone https://github.com/daviddoerfel/lionmesh
cd lionmesh

sudo bash setup/01_system.sh
sudo bash setup/02_mesh_wifi.sh
sudo bash setup/03_lora_meshtastic.sh
sudo bash setup/04_lionmesh.sh
```

If the repository is still hosted under the old name during migration:

```bash
git clone https://github.com/daviddoerfel/ghostlink
cd ghostlink
```

### Configuration

```bash
sudo nano /etc/lionmesh/node.conf
```

Minimum required settings:

```ini
[node]
node_id   = lionmesh-a
node_name = LION-ALPHA
mesh_ip   = 10.41.0.1

[radio]
mode         = wifi
bandwidth_hz = 5000000
mcs          = 4
freq_hz      = 2400000000

[lora]
lora_port = /dev/ttyAMA0
```

Recommended node names:

```text
LION-ALPHA
LION-BRAVO
LION-CHARLIE
LION-DELTA
LION-ECHO
```

### Start

```bash
sudo systemctl start lionmesh
sudo systemctl status lionmesh
sudo journalctl -u lionmesh -f
```

During migration from the old project name, the systemd service may still be called:

```bash
sudo systemctl start ghostlink
sudo systemctl status ghostlink
```

### Access

Open from any device in the mesh:

```text
http://<node-ip>:8080
```

---

## Python dependencies

```bash
pip install numpy scipy fastapi uvicorn jinja2 python-multipart meshtastic pyserial gpsd-py3
```

Hardware drivers via apt:

```bash
# LimeSDR Mini 2 / USB
sudo apt install python3-soapysdr soapysdr-module-lms7

# LimeSDR XTRX
sudo apt install python3-soapysdr soapysdr-module-xtrx

# H.265 video pipeline
sudo apt install python3-gst-1.0 gstreamer1.0-plugins-bad gstreamer1.0-libav
```

---

## PHY self-test

Runs a full TX-to-RX simulation across all bandwidths and MCS levels. No hardware required.

```bash
pip install numpy scipy
python phy/phy_ofdm.py
```

Example output:

```text
LionMesh PHY Self-Test
════════════════════════════════════════════════════════════
  Bandwidth: 5 MHz  |  Sample rate: 5 MS/s  |  SC spacing: 78.1 kHz
  ────────────────────────────────────────────────────────────────────
  MCS0  BPSK   r=0.50   1.5 Mbps   ✓
  MCS4  16QAM  r=0.75   9.0 Mbps   ✓
  MCS6  64QAM  r=0.75  13.5 Mbps   ✓

  Bandwidth: 20 MHz  |  Sample rate: 20 MS/s  |  SC spacing: 312.5 kHz
  ────────────────────────────────────────────────────────────────────
  MCS0  BPSK   r=0.50   6.0 Mbps   ✓
  MCS4  16QAM  r=0.75  36.0 Mbps   ✓
  MCS6  64QAM  r=0.75  54.0 Mbps   ✓
```

---

## API reference

All endpoints are served by the node daemon on port 8080.

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| GET | `/api/status` | — | Local node: GPS, radio mode, uptime |
| GET | `/api/nodes` | — | All known nodes from LoRa and data plane |
| GET | `/api/radio` | — | Radio link quality in LionMesh mode |
| POST | `/api/video/start` | Admin | Start video TX |
| POST | `/api/video/stop` | Admin | Stop video TX |
| POST | `/api/config` | Admin | Update config at runtime |

Example:

```bash
curl http://10.41.0.1:8080/api/nodes
```

```json
{
  "nodes": [
    {
      "node_id": "lionmesh-b",
      "node_name": "LION-BRAVO",
      "mesh_ip": "10.41.0.2",
      "lat": 49.6298,
      "lon": 6.1187,
      "online": true,
      "radio_mode": "lionmesh",
      "mcs": 4,
      "throughput_mbps": 9.0,
      "lora_rssi": -82
    }
  ]
}
```

---

## IP addressing

All nodes share a flat `/16` mesh network over batman-adv.

| Range | Usage |
|---|---|
| `10.41.0.0/16` | batman-adv mesh |
| `10.41.0.1` | Node A |
| `10.41.0.2` | Node B |
| `10.41.0.x` | Additional nodes |

Assign `mesh_ip` statically in `node.conf`.

The WebApp is reachable at each node's mesh IP on port 8080.

---

## Roadmap

### Implemented / planned core features

- OFDM PHY with MCS 0–6
- Soft-decision Viterbi decoding
- LTF fine timing
- Configurable channel bandwidth: 5 / 10 / 20 MHz
- LoRa control plane with GPS broadcast and node discovery
- Meshtastic integration
- Node registry fusing LoRa and data-plane data
- Radio abstraction: LionMesh PHY or WiFi
- FastAPI node daemon
- WebApp with Leaflet map, MCS/RSSI display, and video panel
- Systemd service and automated setup scripts

### Future work

- Carrier frequency offset tracking loop
- IQ imbalance correction
- Pilot-based phase tracking
- 2×2 MIMO spatial multiplexing
- MRC receive diversity combining
- Hardware validation on real LimeSDR hardware
- batman-adv integration for LionMesh data-plane routing
- WebRTC bridge for in-browser video RX
- Link-health observability dashboard
- Tactical field deployment profiles
- Encrypted control interface
- Offline documentation bundle

---

## Regulatory notice

LionMesh is an experimental open-source radio project.

The operator is responsible for ensuring legal operation in their jurisdiction.

### 2400 MHz

The 2.4 GHz ISM band is the recommended frequency range for general testing and development.

### 863 MHz

EU Sub-GHz ISM operation is governed by ETSI rules, including duty-cycle and bandwidth constraints. Wideband OFDM operation at 863 MHz may fall outside normal licence-free operation.

Use only in authorised contexts, emergency-service environments, controlled test setups, or other scenarios where the operator has confirmed permission.

### 5 GHz

5 GHz is not supported by the LMS7002M chipset used by LimeSDR Mini 2 / XTRX.

---

## Security notice

LionMesh is designed for field communications and experimentation.

Before operational use, review and harden:

- admin password handling
- API exposure
- mesh access control
- local firewall rules
- video stream access
- physical device security
- encryption requirements
- radio regulatory compliance

Do not expose the node daemon directly to the public internet.

---

## License

MIT License — Copyright (c) 2026 David Doerfel

Free to use, modify, and distribute — including for emergency services, civil protection, public-safety research, and technical education.

---

