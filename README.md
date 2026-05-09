<p align="center">
  <img src="assets/lionmesh.png" alt="LionMesh logo" width="220">
</p>

# LionMesh

**Experimental open-source field mesh platform for off-grid video and data communication.**

No infrastructure. No proprietary radio firmware. No GNU Radio dependency.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)]()
[![Status](https://img.shields.io/badge/status-experimental-orange.svg)](STATUS.md)
[![Hardware](https://img.shields.io/badge/hardware-Raspberry%20Pi%20%7C%20WiFi%20%7C%20LimeSDR-purple.svg)]()

---

## What is LionMesh?

LionMesh is an experimental open-source field mesh platform for situations where no fixed communication infrastructure is available: disaster relief, search and rescue, civil protection, tactical training, remote deployments, technical education, and resilient field communication experiments.

The project combines two independent communication layers:

- **Control plane** — LoRa 868 MHz via Meshtastic. Used for GPS position broadcasting, node discovery, fallback messaging, and low-bandwidth coordination.
- **Data plane** — either standard WiFi mesh via `batman-adv`, or an experimental custom OFDM PHY over LimeSDR hardware.

A local web interface served by each node displays known nodes, GPS positions, link/radio state, and video-related status information.

LionMesh is designed to be understandable, modifiable, and field-testable by technical users. The current focus is credibility, reproducibility, and step-by-step validation rather than claiming finished MANET-radio performance.

---

## Current project status

LionMesh is **not yet a finished operational tactical MANET**. It is a research and prototyping platform.

| Area | Status |
|---|---|
| Raspberry Pi node daemon | Implemented / prototype |
| WebApp and local REST API | Implemented / prototype |
| WiFi mesh mode via `batman-adv` | Intended first deployable mode |
| LoRa / Meshtastic control plane | Implemented conceptually, hardware validation required |
| OFDM PHY simulation | Implemented / experimental |
| LimeSDR over-the-air validation | Not yet field-proven |
| Video over custom SDR PHY | Experimental / not field-proven |
| Multi-node routing over custom SDR PHY | Future work |
| Operational security hardening | Future work |

For the detailed validation matrix, see [STATUS.md](STATUS.md).

---

## Why LionMesh?

Commercial tactical MANET radios such as Silvus StreamCaster or Persistent Systems MPU5 are powerful, but they are expensive, closed systems and difficult to study or modify.

LionMesh explores a transparent alternative using accessible hardware and open software:

- Raspberry Pi as compute platform
- Meshtastic / LoRa as resilient low-bandwidth control plane
- WiFi mesh as practical first data plane
- LimeSDR as experimental SDR data plane
- Web-based observability for node state, position, and radio information

The goal is not to instantly replace commercial MANET radios. The goal is to build a reproducible open platform that can be studied, tested, extended, and improved.

---

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                        WebApp  :8080                            │
│             Leaflet map · Node list · Video panel               │
├─────────────────────────────────────────────────────────────────┤
│                     Node Daemon  (FastAPI)                      │
│         REST API · Node registry · Radio/status API             │
├───────────────────────────┬─────────────────────────────────────┤
│      Control plane        │          Data plane                 │
│                           │                                     │
│  LoRa 868 MHz             │  Standard WiFi mesh                 │
│  Meshtastic firmware      │  batman-adv / 802.11                │
│  GPS position broadcast   │  first practical deployment mode    │
│  Node discovery           │                                     │
│  Fallback text messaging  │  — or —                             │
│                           │                                     │
│                           │  Experimental LionMesh OFDM PHY     │
│                           │  LimeSDR Mini 2 / XTRX              │
│                           │  2.4 GHz recommended for testing    │
│                           │  863 MHz only with authorisation    │
└───────────────────────────┴─────────────────────────────────────┘
```

Both planes are intended to operate independently. Losing the WiFi or SDR data link should not disable the LoRa control plane, and losing LoRa should not disable the data plane.

---

## Recommended development path

The recommended validation path is deliberately conservative:

1. **Local daemon test** — run the FastAPI node daemon and WebApp on one machine.
2. **Two-node WiFi mesh** — validate `batman-adv`, node registry, API, and WebApp using standard WiFi hardware.
3. **LoRa control plane** — validate Meshtastic GPS discovery between real nodes.
4. **PHY simulation** — run OFDM self-tests without hardware.
5. **SDR loopback** — test the LionMesh PHY with controlled LimeSDR loopback.
6. **Short-range SDR over-the-air test** — validate CFO tracking, timing, IQ correction, and packet recovery.
7. **Field tests** — document real ranges, throughput, packet loss, antenna setup, and regulatory constraints.

---

## Hardware

### Per node

| Component | Purpose | Required |
|---|---|---|
| Raspberry Pi 4B or CM4 | Main compute | Yes |
| Seeed WM1302 LoRa HAT or compatible Meshtastic node | Control plane | Recommended |
| GPS module | Position tracking | Recommended |
| UPS / battery board | Field power | Recommended |
| Alfa USB WiFi adapter or suitable WiFi interface | WiFi data plane | For `wifi` mode |
| LimeSDR Mini 2 or XTRX | Experimental SDR data plane | For `lionmesh` mode |
| 3D-printed or rugged enclosure | Field housing | Recommended |

The LimeSDR is optional. Set `mode = wifi` in `node.conf` to run without SDR hardware.

---

## Radio modes

| Mode | Frequency | Status | Typical use |
|---|---:|---|---|
| `wifi` | 2.4 / 5 GHz | Practical first test mode | WebApp, telemetry, video over IP mesh |
| `lionmesh` | 2.4 GHz | Experimental SDR mode | Controlled SDR development |
| `lionmesh` | 863 MHz | Restricted / authorisation required | Controlled tests only |

Range and throughput depend heavily on antenna type, legal transmit power, channel bandwidth, local noise, Fresnel clearance, hardware quality, and implementation maturity. All SDR performance numbers should be treated as engineering targets until measured and documented in real field tests.

---

## LionMesh PHY

The experimental SDR PHY is a custom OFDM transceiver written in Python. It is intended for learning, testing, and future field validation.

Current / target signal-processing blocks include:

- Schmidl-Cox synchronisation
- least-squares channel estimation
- zero-forcing equalisation
- convolutional FEC with Viterbi decoding
- adaptive modulation from BPSK to 64-QAM
- configurable bandwidth profiles

### OFDM parameters

| Parameter | Value |
|---|---:|
| FFT size | 64 |
| Data subcarriers | 48 |
| Pilot subcarriers | 4 |
| Cyclic prefix | 16 samples |
| Sample rate | 5 / 10 / 20 MS/s |

### MCS table

The following table is a design / simulation target. Real-world values require hardware validation.

| MCS | Modulation | Code rate | 5 MHz target | 10 MHz target | 20 MHz target |
|---:|---|---:|---:|---:|---:|
| 0 | BPSK | 1/2 | 1.5 Mbps | 3 Mbps | 6 Mbps |
| 1 | QPSK | 1/2 | 3 Mbps | 6 Mbps | 12 Mbps |
| 2 | QPSK | 3/4 | 4.5 Mbps | 9 Mbps | 18 Mbps |
| 3 | 16-QAM | 1/2 | 6 Mbps | 12 Mbps | 24 Mbps |
| 4 | 16-QAM | 3/4 | 9 Mbps | 18 Mbps | 36 Mbps |
| 5 | 64-QAM | 2/3 | 12 Mbps | 24 Mbps | 48 Mbps |
| 6 | 64-QAM | 3/4 | 13.5 Mbps | 27 Mbps | 54 Mbps |

---

## Software stack

```text
lionmesh/
├── phy/                    Experimental LionMesh OFDM PHY
│   ├── phy_ofdm.py         TX/RX, sync, channel estimation, FEC, Viterbi
│   ├── mac_simple.py       MAC / datagram layer
│   ├── xtrx_radio.py       SoapySDR interface + simulation fallback
│   └── video_pipe.py       Video pipeline experiments
│
├── control/                Control plane
│   ├── lora.py             Meshtastic interface
│   ├── gps.py              gpsd position reader
│   └── mesh.py             batman-adv status helpers
│
├── daemon/                 Node daemon
│   ├── main.py             Entry point
│   ├── api.py              FastAPI REST API + WebApp serving
│   ├── radio.py            Radio abstraction: LionMesh PHY or WiFi
│   └── registry.py         Node registry
│
├── webapp/                 Web interface
│   ├── templates/          HTML templates
│   └── static/             JavaScript and CSS
│
├── config/
│   └── node.conf.example   Configuration template
│
└── setup/                  Raspberry Pi setup scripts
```

---

## Setup

### Requirements

- Raspberry Pi 4B or CM4
- Raspberry Pi OS Lite 64-bit
- Python 3.10+
- Internet connection for initial setup
- WiFi adapter for first practical mesh tests
- Optional LoRa / Meshtastic hardware
- Optional LimeSDR Mini 2 / XTRX for experimental SDR tests

### Installation

```bash
git clone https://github.com/daviddoerfel/lionmesh
cd lionmesh

sudo bash setup/01_system.sh
sudo bash setup/02_mesh_wifi.sh
sudo bash setup/03_lora_meshtastic.sh
sudo bash setup/04_lionmesh.sh
```

### Configuration

```bash
sudo nano /etc/lionmesh/node.conf
```

Minimum example:

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

### Start

```bash
sudo systemctl start lionmesh
sudo systemctl status lionmesh
sudo journalctl -u lionmesh -f
```

### Access

Open from any device in the mesh:

```text
http://<node-ip>:8080
```

---

## PHY self-test

The PHY self-test runs a TX-to-RX simulation. No SDR hardware is required.

```bash
pip install -r requirements.txt
python phy/phy_ofdm.py
```

---

## API reference

All endpoints are served by the node daemon on port `8080`.

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| GET | `/` | No | WebApp |
| GET | `/api/status` | No | Local node status |
| GET | `/api/nodes` | No | Known nodes |
| GET | `/api/radio` | No | Radio status |
| POST | `/api/config` | Basic auth | Runtime config update |
| POST | `/api/video/start` | Basic auth | Start video TX if available |
| POST | `/api/video/stop` | Basic auth | Stop video TX |

Example:

```bash
curl http://10.41.0.1:8080/api/nodes
```

---

## Screenshots

Screenshots should be added after a real local or field demo:

```text
assets/screenshots/webapp-node-map.png
assets/screenshots/webapp-radio-status.png
assets/screenshots/field-node-build.jpg
```

Recommended screenshot evidence:

- two-node WiFi mesh running
- WebApp node map
- `/api/status` output
- `/api/nodes` output
- field hardware build
- SDR loopback or spectrum capture once available

---

## Roadmap

### Near-term credibility work

- [ ] Remove remaining legacy project-name references
- [ ] Add screenshots from a real local demo
- [ ] Add two-node WiFi mesh test report
- [ ] Add Meshtastic discovery test report
- [ ] Add LimeSDR loopback test report
- [ ] Add measured throughput and packet-loss table
- [ ] Add antenna and power documentation

### SDR / PHY work

- [ ] Carrier frequency offset tracking loop
- [ ] IQ imbalance correction
- [ ] Pilot-based phase tracking
- [ ] Hardware validation on real LimeSDR hardware
- [ ] Short-range over-the-air SDR packet test
- [ ] Field measurements with legal 2.4 GHz configuration
- [ ] batman-adv integration for custom SDR data plane
- [ ] WebRTC bridge for in-browser video RX

### Security / operational hardening

- [ ] Replace default admin password flow
- [ ] Add API authentication model
- [ ] Add local firewall guidance
- [ ] Add encrypted control interface
- [ ] Add offline documentation bundle
- [ ] Add deployment checklist

---

## Regulatory notice

LionMesh is an experimental open-source radio project. The operator is responsible for ensuring legal operation in their jurisdiction.

### 2.4 GHz

The 2.4 GHz ISM band is the recommended frequency range for general development and testing, subject to local power, bandwidth, antenna, and duty-cycle rules.

### 863 MHz / 868 MHz

EU Sub-GHz ISM operation is governed by ETSI and national rules, including duty-cycle, channel, bandwidth, and effective radiated power constraints. Wideband OFDM operation around 863 MHz may fall outside normal licence-free operation.

Use 863 MHz wideband SDR operation only in authorised contexts, controlled test environments, emergency-service environments with appropriate permission, or other scenarios where the operator has confirmed legal authorisation.

### 5 GHz

5 GHz operation is not supported by the LMS7002M chipset used by LimeSDR Mini 2 / XTRX.

---

## Security notice

LionMesh is not hardened for hostile networks by default. Before operational use, review and harden:

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
