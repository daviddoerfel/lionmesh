# LionMesh Status

This document separates implemented functionality, experimental functionality, and future work.

LionMesh is currently an experimental field mesh platform. The WiFi mesh path is the first practical deployment target. The custom SDR / OFDM data plane is under active development and must be treated as experimental until measured on real hardware.

---

## Validation matrix

| Capability | Status | Evidence needed before claiming operational readiness |
|---|---|---|
| Repository structure | Implemented | Public repository with documented folders |
| Python dependencies | Implemented | `pip install -r requirements.txt` succeeds |
| Node daemon | Prototype | Local run log and `/api/status` response |
| WebApp | Prototype | Screenshot from real node |
| Node registry | Prototype | Multi-node `/api/nodes` output |
| WiFi mesh mode | First deployable target | Two Raspberry Pi nodes over `batman-adv` |
| LoRa / Meshtastic control plane | Prototype / integration target | Real GPS position exchange between nodes |
| OFDM PHY simulation | Experimental | Self-test output committed to docs |
| LimeSDR loopback | Not yet validated | Loopback packet-loss and throughput report |
| LimeSDR over-the-air short range | Not yet validated | Controlled 2.4 GHz test with legal power |
| SDR video transport | Not yet field-proven | End-to-end video test report |
| Multi-hop SDR routing | Future work | Multi-node route validation |
| Security hardening | Future work | Threat model, auth, firewall, encryption checklist |
| Regulatory validation | Operator responsibility | Country-specific approval / compliance review |

---

## Current maturity level

### Stable enough to document

- Project architecture
- Raspberry Pi node concept
- WiFi mesh as first practical data plane
- LoRa / Meshtastic as control-plane concept
- Local WebApp / API concept
- OFDM PHY design and simulation path

### Experimental

- Custom OFDM PHY
- Adaptive MCS behavior
- H.265 video over SDR data plane
- LimeSDR Mini 2 / XTRX operation
- 863 MHz wideband SDR operation

### Not yet claimed

- Operational tactical MANET replacement
- Guaranteed range or throughput
- Production-grade security
- Legal licence-free 863 MHz wideband operation
- Multi-hop SDR routing comparable to commercial MANET radios

---

## Recommended demo milestones

### Demo 1 — Local software demo

Goal: show that the daemon and WebApp start correctly.

Evidence:

```text
python daemon/main.py --config config/node.conf.example
curl http://localhost:8080/api/status
```

Add:

- terminal screenshot
- WebApp screenshot
- `/api/status` output

---

### Demo 2 — Two-node WiFi mesh

Goal: validate the first practical data plane without SDR complexity.

Hardware:

- 2× Raspberry Pi 4B / CM4
- 2× WiFi adapters
- battery power optional

Evidence:

- `batctl n`
- ping between mesh IPs
- `/api/nodes` from both sides
- WebApp screenshot showing both nodes
- video/IP traffic test if available

---

### Demo 3 — LoRa control-plane test

Goal: validate node discovery and GPS position broadcast.

Evidence:

- Meshtastic packet reception
- node position update in registry
- RSSI/SNR values
- screenshot of WebApp map with two nodes

---

### Demo 4 — SDR loopback

Goal: validate the custom PHY before any field claim.

Evidence:

- LimeSDR loopback setup photo
- center frequency
- sample rate
- bandwidth
- MCS
- packet count
- packet-error rate
- measured throughput

---

### Demo 5 — Controlled 2.4 GHz SDR over-the-air test

Goal: validate short-range SDR operation in a controlled, legal setup.

Evidence:

- location and distance
- antenna type
- transmit power setting
- frequency and bandwidth
- packet-error rate
- measured throughput
- spectrum screenshot if possible

---

## Wording rules for the README and public communication

Use cautious wording until measured evidence exists.

Prefer:

- "experimental"
- "prototype"
- "target throughput"
- "simulation result"
- "planned"
- "under validation"
- "first deployable path is WiFi mesh"

Avoid until proven:

- "field-proven"
- "operational tactical MANET"
- "guaranteed range"
- "secure by default"
- "legal everywhere"
- "production-ready SDR video"

---

## Honest one-line description

LionMesh is an experimental open-source field mesh platform combining LoRa-based control-plane discovery with WiFi or SDR-based data-plane communication; WiFi mesh is the first practical mode, while the custom OFDM SDR PHY is under active experimental development.
