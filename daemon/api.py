"""
daemon/api.py — LionMesh REST API
=====================================
FastAPI application served on port 8080.

Merges GATONET's node management API with LionMesh radio status.

Endpoints:
  GET  /                  WebApp (Leaflet map)
  GET  /api/status        Local node status (radio, GPS, uptime)
  GET  /api/nodes         All known nodes (from registry)
  GET  /api/radio         Radio mode + link quality
  POST /api/config        Update config (admin only)
  POST /api/video/start   Start video TX (admin only)
  POST /api/video/stop    Stop video TX (admin only)
"""

import time
import secrets
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger("api")

# These are injected by main.py after startup
_registry  = None
_radio     = None
_gps       = None
_video     = None
_cfg       = None
_start_time = time.time()

WEBAPP_DIR = Path(__file__).parent.parent / "webapp"

app = FastAPI(title="LionMesh Node API", version="1.0")
security = HTTPBasic()

app.mount("/static", StaticFiles(directory=str(WEBAPP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(WEBAPP_DIR / "templates"))


def inject(registry, radio, gps, video, cfg):
    """Called by main.py to wire up shared objects."""
    global _registry, _radio, _gps, _video, _cfg
    _registry = registry
    _radio    = radio
    _gps      = gps
    _video    = video
    _cfg      = cfg


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_admin(creds: HTTPBasicCredentials = Depends(security)):
    if not _cfg:
        raise HTTPException(status_code=503, detail="Not ready")
    pwd = _cfg.get("api", "admin_password", fallback="changeme")
    if not secrets.compare_digest(creds.password, pwd):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return creds.username


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    node_id   = _cfg.get("node", "node_id",   fallback="unknown") if _cfg else "unknown"
    node_name = _cfg.get("node", "node_name", fallback="LionMesh") if _cfg else "LionMesh"
    return templates.TemplateResponse("index.html", {
        "request":    request,
        "node_id":    node_id,
        "node_name":  node_name,
    })


@app.get("/api/status")
async def get_status():
    """Local node status — used by WebApp header."""
    uptime = int(time.time() - _start_time)
    pos    = _gps.get_position() if _gps else None
    radio  = _radio.status if _radio else {"mode": "unknown"}

    node_id   = _cfg.get("node", "node_id",   fallback="unknown") if _cfg else "unknown"
    node_name = _cfg.get("node", "node_name", fallback="LionMesh") if _cfg else "LionMesh"
    mesh_ip   = _cfg.get("node", "mesh_ip",   fallback="—") if _cfg else "—"

    return {
        "node_id":   node_id,
        "node_name": node_name,
        "mesh_ip":   mesh_ip,
        "uptime_s":  uptime,
        "gps":       pos,
        "radio":     radio,
    }


@app.get("/api/nodes")
async def get_nodes():
    """All known nodes from registry (LoRa + data plane combined)."""
    if not _registry:
        return {"nodes": []}
    return {"nodes": _registry.get_all()}


@app.get("/api/radio")
async def get_radio():
    """Detailed radio status (LionMesh mode only)."""
    if not _radio:
        return {"error": "Radio not initialised"}
    return _radio.status


class ConfigUpdate(BaseModel):
    section: str
    key:     str
    value:   str


@app.post("/api/config")
async def update_config(update: ConfigUpdate,
                        username: str = Depends(verify_admin)):
    """Update a config value at runtime (does not persist to disk)."""
    if not _cfg:
        raise HTTPException(status_code=503, detail="Config not loaded")
    if not _cfg.has_section(update.section):
        raise HTTPException(status_code=400, detail=f"Unknown section: {update.section}")
    _cfg.set(update.section, update.key, update.value)
    log.info(f"Config updated by {username}: [{update.section}] {update.key} = {update.value}")
    return {"result": "ok", "section": update.section, "key": update.key}


@app.post("/api/video/start")
async def video_start(username: str = Depends(verify_admin)):
    """Start video TX (LionMesh mode only)."""
    if not _video:
        raise HTTPException(status_code=503, detail="Video not available")
    try:
        src = _cfg.get("video", "source", fallback="test") if _cfg else "test"
        if src == "v4l2":
            dev = _cfg.get("video", "device", fallback="/dev/video0") if _cfg else "/dev/video0"
            _video.start_gstreamer(f"v4l2src device={dev}")
        else:
            _video.start_gstreamer("videotestsrc pattern=ball")
        return {"result": "started"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/video/stop")
async def video_stop(username: str = Depends(verify_admin)):
    """Stop video TX."""
    if _video:
        _video.stop()
    return {"result": "stopped"}


# ── Crypto endpoints ──────────────────────────────────────────────────────────

@app.get("/api/crypto/pubkey")
async def get_pubkey():
    """
    Return this node's Curve25519 public key (hex).
    Share this with other nodes so they can encrypt frames for this node.
    """
    crypto = _radio.status.get('crypto') if _radio else None
    if crypto is None:
        # Try getting from registry or radio directly
        if _radio and hasattr(_radio, '_mac') and _radio._mac:
            c = _radio._mac.cfg.crypto
            if c and hasattr(c, 'public_key_hex'):
                return {
                    "node_id": _cfg.get("node", "node_id", fallback="unknown")
                               if _cfg else "unknown",
                    "pubkey":  c.public_key_hex,
                }
    raise HTTPException(status_code=503, detail="Crypto not enabled or not ready")


class PeerKey(BaseModel):
    node_id:    str
    pubkey_hex: str


@app.post("/api/crypto/peers")
async def add_peer(peer: PeerKey, username: str = Depends(verify_admin)):
    """
    Register a peer node's public key.
    After this, frames sent to that node_id will be ECIES-encrypted.

    Body: {"node_id": "lionmesh-b", "pubkey_hex": "abcdef..."}
    """
    if _radio and hasattr(_radio, '_mac') and _radio._mac:
        crypto = _radio._mac.cfg.crypto
        if crypto and hasattr(crypto, 'add_peer_hex'):
            try:
                crypto.add_peer_hex(peer.node_id, peer.pubkey_hex)
                log.info(f"Peer key registered: {peer.node_id} by {username}")
                return {"result": "ok", "node_id": peer.node_id,
                        "peers": crypto.list_peers()}
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))
    raise HTTPException(status_code=503, detail="Crypto not enabled")


@app.get("/api/crypto/peers")
async def list_peers():
    """List all registered peer node IDs."""
    if _radio and hasattr(_radio, '_mac') and _radio._mac:
        crypto = _radio._mac.cfg.crypto
        if crypto and hasattr(crypto, 'list_peers'):
            return {"peers": crypto.list_peers()}
    return {"peers": []}
