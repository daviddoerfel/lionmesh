# GhostLink

**Open-source field mesh radio for off-grid video communication.**  
No infrastructure. No proprietary hardware. No GNU Radio required.

GhostLink fuses two projects:
- **GhostLink PHY** — custom OFDM transceiver (LimeSDR, 863/2400 MHz, 5–54 Mbps)
- **GATONET** — field mesh management (LoRa control plane, GPS, batman-adv, WebApp)

The result is a complete node: LoRa for discovery and GPS, GhostLink PHY or standard WiFi for HD video, and a web interface showing all nodes on a Leaflet map.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      WebApp :8080                           │
│           Leaflet map · Node list · Video panel             │
├─────────────────────────────────────────────────────────────┤
│                    Node Daemon (FastAPI)                     │
│        REST API · Node registry · Band switching            │
├───────────────────────────┬─────────────────────────────────┤
│     Control plane         │        Data plane               │
│     LoRa 868 MHz          │   GhostLink OFDM PHY            │
│     Meshtastic            │   OR standard WiFi (batman-adv) │
│     GPS broadcast         │   LimeSDR · 5/10/20 MHz BW      │
│     Node discovery        │   MCS 0–6 · H.265 video         │
│     Fallback messaging    │   863 MHz / 2400 MHz            │
├───────────────────────────┼─────────────────────────────────┤
│  Seeed WM1302 LoRa HAT   │  LimeSDR Mini 2 / XTRX          │
│  u-blox GPS module        │  OR Alfa WiFi adapter           │
└───────────────────────────┴─────────────────────────────────┘
```

---

## Hardware (per node)

| Component | Description | Required |
|---|---|---|
| Raspberry Pi 4B or CM4 | Compute | Always |
| Seeed WM1302 LoRa HAT | Control plane 868 MHz | Always |
| u-blox GPS module | Position | Always |
| LimeSDR Mini 2 or XTRX | GhostLink data plane | Mode: ghostlink |
| Alfa USB WiFi adapter | Standard WiFi data plane | Mode: wifi |
| UPS / battery board | Field power | Always |
| 3D-printed enclosure | [OpenNot5 case](https://makerworld.com/de/models/2128181-openmanet-radio-case) | Recommended |

**The LimeSDR is optional.** Set `mode = wifi` in `node.conf` to run with a standard WiFi adapter — everything works except the custom OFDM PHY.

---

## Radio modes

| Mode | Hardware | Range | Throughput @ MCS4 | Video |
|---|---|---|---|---|
| `wifi` | Alfa adapter | 200–500 m | ~50 Mbps | ✓ |
| `ghostlink` | LimeSDR, 5 MHz BW | 1–3 km | 9 Mbps | ✓ |
| `ghostlink` | LimeSDR, 20 MHz BW | 300–800 m | 36 Mbps | ✓ |

---

## MCS table (GhostLink PHY)

| MCS | Modulation | Rate | 5 MHz | 10 MHz | 20 MHz | Min SNR |
|-----|-----------|------|-------|--------|--------|---------|
| 0 | BPSK | 1/2 | 1.5 Mbps | 3 Mbps | 6 Mbps | 8 dB |
| 2 | QPSK | 3/4 | 4.5 Mbps | 9 Mbps | 18 Mbps | 13 dB |
| **4** | **16-QAM** | **3/4** | **9 Mbps** | **18 Mbps** | **36 Mbps** | **19 dB** |
| 6 | 64-QAM | 3/4 | 13.5 Mbps | 27 Mbps | 54 Mbps | 25 dB |

Default: **863 MHz · 5 MHz BW · MCS 4 = 9 Mbps** — enough for 720p H.265, ~2 km range.

---

## Repository structure

```
ghostlink/
├── phy/                  GhostLink OFDM PHY (from GhostLink)
│   ├── phy_ofdm.py       TX/RX, sync, channel estimation, soft Viterbi
│   ├── mac_simple.py     MAC layer — ARQ, datagram mode, adaptive MCS
│   ├── xtrx_radio.py     SoapySDR interface — LimeSDR hardware + sim mode
│   └── video_pipe.py     H.265 GStreamer pipeline + RTP fragmentation
│
├── control/              Control plane (from GATONET)
│   ├── lora.py           Meshtastic interface — discovery, GPS broadcast
│   ├── gps.py            gpsd position reader
│   └── mesh.py           batman-adv status and band switching
│
├── daemon/               Node daemon (fusion layer — new)
│   ├── main.py           Entry point — orchestrates all subsystems
│   ├── api.py            FastAPI REST API + WebApp serving
│   ├── radio.py          Radio abstraction — GhostLink PHY or WiFi
│   └── registry.py       In-memory node registry (LoRa + data plane)
│
├── webapp/               Web interface (from GATONET, extended)
│   ├── templates/        index.html — Leaflet map + node list + video panel
│   └── static/           app.js, style.css
│
├── config/
│   └── node.conf.example Configuration template
│
├── setup/                Installation scripts (run in order)
│   ├── 01_system.sh
│   ├── 02_mesh_wifi.sh
│   ├── 03_lora_meshtastic.sh
│   ├── 04_ghostlink.sh   GhostLink PHY + daemon + systemd service
│   └── 05_webapp.sh
│
└── requirements.txt
```

---

## Setup

Run on a fresh Raspberry Pi OS Lite (64-bit):

```bash
git clone https://github.com/YOUR_USERNAME/ghostlink
cd ghostlink

sudo bash setup/01_system.sh
sudo bash setup/02_mesh_wifi.sh
sudo bash setup/03_lora_meshtastic.sh
sudo bash setup/04_ghostlink.sh    # interactive — asks about LimeSDR + GStreamer
```

Edit your config:

```bash
sudo nano /etc/ghostlink/node.conf
```

Key settings:

```ini
[node]
node_id   = ghostlink-a
node_name = GHOST-ALPHA
mesh_ip   = 10.41.0.1

[radio]
mode         = wifi        # wifi or ghostlink
bandwidth_hz = 5000000
mcs          = 4
freq_hz      = 863000000
```

Start the daemon:

```bash
sudo systemctl start ghostlink
sudo journalctl -u ghostlink -f
```

Open the WebApp from any node in the mesh:

```
http://<node-ip>:8080
```

---

## PHY self-test (no hardware required)

```bash
pip install numpy scipy
python phy/phy_ofdm.py
```

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Local node status (GPS, radio, uptime) |
| GET | `/api/nodes` | All known nodes |
| GET | `/api/radio` | Radio link quality (GhostLink mode) |
| POST | `/api/video/start` | Start video TX (admin) |
| POST | `/api/video/stop` | Stop video TX (admin) |
| POST | `/api/config` | Update config at runtime (admin) |

---

## Roadmap

- [x] GhostLink OFDM PHY — MCS 0–6, soft Viterbi, LTF fine timing
- [x] Configurable bandwidth — 5 / 10 / 20 MHz
- [x] LoRa control plane — GPS broadcast, node discovery (Meshtastic)
- [x] Node registry — fuses LoRa + data plane node data
- [x] Radio abstraction — GhostLink PHY or WiFi, same API
- [x] FastAPI daemon — REST API, node management
- [x] WebApp — Leaflet map, node list with MCS/RSSI, video panel
- [x] Systemd service + setup scripts
- [ ] CFO (carrier frequency offset) tracking loop
- [ ] IQ imbalance correction
- [ ] 2×2 MIMO spatial multiplexing
- [ ] batman-adv integration for GhostLink data plane
- [ ] Hardware validation on real LimeSDR
- [ ] Video RX in browser (WebRTC bridge)

---

## License

MIT License — Copyright (c) 2026 David Doerfel  
Free to use for emergency services, public safety, and field operations.

## Regulatory

**863 MHz (EU):** 1% duty cycle under ETSI EN 300 220. Enforced automatically by `xtrx_radio.py`.  
**2400 MHz:** No duty cycle limit. Max 100 mW EIRP (ETSI EN 300 328).
