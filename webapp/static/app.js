// GhostLink WebApp
// Polls /api/status, /api/nodes, /api/radio every 5-10 seconds.
// Updates Leaflet map, node list, radio stats, and video panel.

'use strict';

// ── Map setup ─────────────────────────────────────────────────────────────────
const map = L.map('map', { zoomControl: true }).setView([49.6, 6.1], 11);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap',
    maxZoom: 18,
}).addTo(map);

const markers    = {};   // node_id → L.Marker
let   selfNodeId = null;

function makeIcon(type) {
    return L.divIcon({
        className: '',
        html: `<div class="ghost-marker ${type}"></div>`,
        iconSize:   [12, 12],
        iconAnchor: [6, 6],
        popupAnchor:[0, -8],
    });
}

// ── Utility ───────────────────────────────────────────────────────────────────
function fmtUptime(s) {
    if (s == null) return '—';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return `${h}h ${m}m`;
}

function fmtGps(pos) {
    if (!pos || pos.lat == null) return 'No fix';
    return `${pos.lat.toFixed(4)}, ${pos.lon.toFixed(4)}`;
}

function mcsColor(mcs) {
    if (mcs == null) return '#555';
    if (mcs <= 1)   return '#40c4ff';   // blue  = low
    if (mcs <= 3)   return '#ffaa00';   // amber = mid
    return '#00e676';                    // green = high
}

function mcsBarWidth(mcs) {
    if (mcs == null) return 0;
    return Math.round((mcs / 6) * 100);
}

// ── Status polling ────────────────────────────────────────────────────────────
async function fetchStatus() {
    try {
        const res  = await fetch('/api/status');
        const data = await res.json();

        selfNodeId = data.node_id;

        // Header
        document.getElementById('node-id').textContent = data.node_name || data.node_id || '—';
        setOnline(true);

        // Local panel
        document.getElementById('s-ip').textContent     = data.mesh_ip || '—';
        document.getElementById('s-gps').textContent    = fmtGps(data.gps);
        document.getElementById('s-uptime').textContent = fmtUptime(data.uptime_s);

        // Radio mode badge
        const radio = data.radio || {};
        const badge = document.getElementById('radio-mode-badge');
        const mode  = (radio.mode || 'wifi').toLowerCase();
        badge.textContent = mode === 'ghostlink' ? 'GhostLink PHY' : 'WiFi';
        badge.className   = `radio-mode-badge ${mode}`;

        // Radio panel
        document.getElementById('r-mode').textContent  = mode;
        document.getElementById('r-mcs').textContent   = radio.mcs  != null ? `MCS${radio.mcs}` : '—';
        document.getElementById('r-speed').textContent = radio.throughput_mbps != null
            ? `${radio.throughput_mbps} Mbps` : '—';
        document.getElementById('r-txq').textContent   = radio.tx_queue != null
            ? radio.tx_queue : '—';

        // MCS bar
        const bar = document.getElementById('mcs-bar');
        bar.style.width      = mcsBarWidth(radio.mcs) + '%';
        bar.style.background = mcsColor(radio.mcs);

        // Color-code speed
        const speedEl = document.getElementById('r-speed');
        if (radio.throughput_mbps != null) {
            speedEl.className = 'stat-val ' + (radio.throughput_mbps >= 6 ? 'green' : 'amber');
        }

    } catch {
        setOnline(false);
    }
}

function setOnline(online) {
    const dot  = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    dot.className  = 'status-dot ' + (online ? 'online' : 'offline');
    text.textContent = online ? 'Online' : 'Offline';
}

// ── Nodes polling ─────────────────────────────────────────────────────────────
async function fetchNodes() {
    try {
        const res  = await fetch('/api/nodes');
        const data = await res.json();
        const nodes = data.nodes || [];

        document.getElementById('node-count').textContent = nodes.length;
        renderNodeList(nodes);
        renderNodeMarkers(nodes);
    } catch (e) {
        console.warn('fetchNodes failed:', e);
    }
}

function renderNodeList(nodes) {
    const ul = document.getElementById('node-list');
    ul.innerHTML = '';

    if (!nodes.length) {
        ul.innerHTML = '<li style="color:var(--text2);padding:0.4rem 0;font-size:0.75rem">No nodes discovered</li>';
        return;
    }

    nodes.forEach(node => {
        const li = document.createElement('li');
        const isSelf   = node.node_id === selfNodeId;
        const isOnline = node.online;

        const tags = [];
        if (node.radio_mode === 'ghostlink' && node.mcs != null)
            tags.push(`<span class="node-tag mcs">MCS${node.mcs}</span>`);
        if (node.throughput_mbps != null)
            tags.push(`<span class="node-tag speed">${node.throughput_mbps} Mbps</span>`);
        if (node.lora_rssi != null)
            tags.push(`<span class="node-tag lora">LoRa ${node.lora_rssi} dBm</span>`);
        if (node.lat != null)
            tags.push(`<span class="node-tag gps">${node.lat.toFixed(4)}, ${node.lon.toFixed(4)}</span>`);

        li.innerHTML = `
            <div class="node-name">
                <div class="node-dot ${isOnline ? 'online' : 'offline'}"></div>
                ${node.node_name || node.node_id}
                ${isSelf ? '<span style="color:var(--text2);font-size:0.65rem">(this node)</span>' : ''}
            </div>
            <div class="node-meta">${tags.join('')}</div>
        `;

        // Click: fly to node on map
        if (node.lat != null && node.lon != null) {
            li.addEventListener('click', () => {
                map.flyTo([node.lat, node.lon], 14, { duration: 0.8 });
                markers[node.node_id]?.openPopup();
            });
        }

        ul.appendChild(li);
    });
}

function renderNodeMarkers(nodes) {
    const seen = new Set();

    nodes.forEach(node => {
        if (node.lat == null || node.lon == null) return;
        seen.add(node.node_id);

        const isSelf   = node.node_id === selfNodeId;
        const isOnline = node.online;
        const iconType = isSelf ? 'self' : (isOnline ? '' : 'offline');

        const popupHtml = `
            <b>${node.node_name || node.node_id}</b><br>
            ${node.mesh_ip || ''}<br>
            ${node.radio_mode || ''} ${node.mcs != null ? '· MCS' + node.mcs : ''}
            ${node.throughput_mbps != null ? '· ' + node.throughput_mbps + ' Mbps' : ''}<br>
            ${node.lora_rssi != null ? 'LoRa ' + node.lora_rssi + ' dBm' : ''}
        `;

        if (markers[node.node_id]) {
            markers[node.node_id]
                .setLatLng([node.lat, node.lon])
                .setIcon(makeIcon(iconType))
                .setPopupContent(popupHtml);
        } else {
            markers[node.node_id] = L.marker([node.lat, node.lon], {
                icon: makeIcon(iconType),
            })
            .addTo(map)
            .bindPopup(popupHtml);
        }
    });

    // Remove markers for nodes no longer in the list
    Object.keys(markers).forEach(id => {
        if (!seen.has(id)) {
            map.removeLayer(markers[id]);
            delete markers[id];
        }
    });
}

// ── Video panel ───────────────────────────────────────────────────────────────
let mediaSource = null;

document.getElementById('btn-video-start').addEventListener('click', async () => {
    try {
        const res = await fetch('/api/video/start', {
            method: 'POST',
            headers: { 'Authorization': 'Basic ' + btoa(':' + prompt('Admin password:')) }
        });
        const d = await res.json();
        if (d.result === 'started') {
            document.getElementById('video-overlay').classList.add('hidden');
        } else {
            alert('Failed: ' + JSON.stringify(d));
        }
    } catch (e) { alert('Error: ' + e); }
});

document.getElementById('btn-video-stop').addEventListener('click', async () => {
    await fetch('/api/video/stop', {
        method: 'POST',
        headers: { 'Authorization': 'Basic ' + btoa(':' + prompt('Admin password:')) }
    });
    const v = document.getElementById('video-feed');
    v.srcObject = null;
    document.getElementById('video-overlay').classList.remove('hidden');
});

document.getElementById('btn-rx-connect').addEventListener('click', () => {
    // For UDP/RTSP RX: we can't play udp:// natively in a browser.
    // Display the URL — user should open in VLC or mpv.
    const url = document.getElementById('rtsp-url').value.trim();
    if (!url) return;
    alert(`Open in VLC or mpv:\n${url}\n\nExample:\nvlc ${url}\nmpv ${url}`);
});

// ── Poll ──────────────────────────────────────────────────────────────────────
fetchStatus();
fetchNodes();

setInterval(fetchStatus,  5_000);
setInterval(fetchNodes,  10_000);
