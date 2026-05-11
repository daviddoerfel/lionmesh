"""
crypto.py — LionMesh Asymmetric Encryption Layer
==================================================
Per-node Public/Private Key encryption on MAC frame level.

Design:
  - Each node has a Curve25519 keypair (generated once, stored on disk)
  - TX encrypts a frame using the RECIPIENT's public key
  - RX decrypts using its own private key
  - No shared secret needed — only public keys are exchanged

Scheme: ECIES (Elliptic Curve Integrated Encryption Scheme)
  1. TX generates an ephemeral Curve25519 keypair
  2. ECDH(ephemeral_private, recipient_public) → shared_secret
  3. HKDF(shared_secret) → 32-byte AES key
  4. AES-256-GCM encrypt(payload)
  5. Frame: [ephemeral_pubkey 32B] [IV 12B] [ciphertext] [tag 16B]
  6. RX: ECDH(own_private, ephemeral_pubkey) → same shared_secret → decrypt

Frame overhead: 32 + 12 + 16 = 60 bytes per frame.

Broadcast frames (dst=0xFFFF):
  Encrypted with a deployment-wide Group Key (AES-256-GCM, PSK-derived).
  All nodes share this group key for broadcast/multicast traffic.

Key storage:
  /etc/lionmesh/keys/node.private  (600, never leaves node)
  /etc/lionmesh/keys/node.public   (644, shared with peers)
  /etc/lionmesh/keys/peers/        (one .pub file per known node)

Usage:
  from crypto import NodeCrypto
  crypto = NodeCrypto.load_or_generate('/etc/lionmesh/keys')
  crypto.add_peer('node-b', peer_pubkey_bytes)

  # Encrypt for a specific node
  encrypted = crypto.encrypt(payload, recipient_id='node-b')

  # Decrypt (auto-detects unicast vs broadcast)
  plaintext = crypto.decrypt(encrypted)
"""

import os
import hashlib
import logging
from pathlib import Path
from typing import Optional, Dict

log = logging.getLogger("crypto")

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes, serialization
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    log.error("pip install cryptography")


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

PUBKEY_SIZE    = 32   # Curve25519 public key
IV_SIZE        = 12   # AES-GCM nonce
TAG_SIZE       = 16   # AES-GCM authentication tag
UNICAST_OVERHEAD  = PUBKEY_SIZE + IV_SIZE + TAG_SIZE   # 60 bytes
BROADCAST_OVERHEAD = IV_SIZE + TAG_SIZE                # 28 bytes

# Frame type prefix (1 byte)
PREFIX_UNICAST   = b'\x01'
PREFIX_BROADCAST = b'\x02'

PBKDF2_ITER = 100_000


# ─────────────────────────────────────────────
# Node Crypto
# ─────────────────────────────────────────────

class NodeCrypto:
    """
    Per-node asymmetric encryption using Curve25519 + AES-256-GCM.

    Each node has:
      - Its own private/public keypair (Curve25519)
      - A registry of peer public keys (keyed by node_id)
      - A shared group key for broadcast frames

    All operations are thread-safe (stateless encryption).
    """

    def __init__(self,
                 private_key: 'X25519PrivateKey',
                 group_key:   bytes,
                 node_id:     str = ""):
        self._private  = private_key
        self._public   = private_key.public_key()
        self._peers:   Dict[str, 'X25519PublicKey'] = {}
        self._group    = AESGCM(group_key)
        self._node_id  = node_id

        pubkey_bytes = self._public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)
        fp = hashlib.sha256(pubkey_bytes).hexdigest()[:8]
        log.info(f"NodeCrypto ready — node_id={node_id} "
                 f"pubkey_fingerprint={fp}...")

    # ── Class methods ──────────────────────────────────────

    @classmethod
    def generate(cls, node_id: str = "", group_psk: str = "",
                 group_salt: str = "lionmesh-group") -> 'NodeCrypto':
        """Generate a new random keypair."""
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography not installed")
        private = X25519PrivateKey.generate()
        group   = cls._derive_group_key(group_psk, group_salt)
        return cls(private, group, node_id)

    @classmethod
    def load_or_generate(cls, key_dir: str,
                         node_id:    str = "",
                         group_psk:  str = "",
                         group_salt: str = "lionmesh-group") -> 'NodeCrypto':
        """
        Load keypair from disk, or generate and save if not found.

        key_dir structure:
          key_dir/node.private   — raw 32-byte private key (mode 600)
          key_dir/node.public    — raw 32-byte public key  (mode 644)
          key_dir/peers/         — peer public keys
        """
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography not installed")

        key_dir   = Path(key_dir)
        priv_path = key_dir / "node.private"
        pub_path  = key_dir / "node.public"

        key_dir.mkdir(parents=True, exist_ok=True)
        (key_dir / "peers").mkdir(exist_ok=True)

        if priv_path.exists():
            raw  = priv_path.read_bytes()
            priv = X25519PrivateKey.from_private_bytes(raw)
            log.info(f"Loaded keypair from {key_dir}")
        else:
            priv = X25519PrivateKey.generate()
            priv_path.write_bytes(
                priv.private_bytes(serialization.Encoding.Raw,
                                   serialization.PrivateFormat.Raw,
                                   serialization.NoEncryption()))
            priv_path.chmod(0o600)
            pub  = priv.public_key()
            pub_path.write_bytes(
                pub.public_bytes(serialization.Encoding.Raw,
                                 serialization.PublicFormat.Raw))
            pub_path.chmod(0o644)
            log.info(f"Generated new keypair → {key_dir}")

        # Load peer public keys
        group  = cls._derive_group_key(group_psk, group_salt)
        crypto = cls(priv, group, node_id)

        peers_dir = key_dir / "peers"
        for f in peers_dir.glob("*.pub"):
            peer_id = f.stem
            try:
                peer_pub = X25519PublicKey.from_public_bytes(f.read_bytes())
                crypto._peers[peer_id] = peer_pub
                log.debug(f"Loaded peer key: {peer_id}")
            except Exception as e:
                log.warning(f"Failed to load peer key {f}: {e}")

        return crypto

    @staticmethod
    def _derive_group_key(psk: str, salt: str) -> bytes:
        """Derive 32-byte group key from PSK via PBKDF2."""
        kdf = PBKDF2HMAC(
            algorithm  = hashes.SHA256(),
            length     = 32,
            salt       = salt.encode(),
            iterations = PBKDF2_ITER,
        )
        return kdf.derive(psk.encode() if psk else b"lionmesh-default-group-key")

    # ── Public key management ──────────────────────────────

    @property
    def public_key_bytes(self) -> bytes:
        """Raw 32-byte Curve25519 public key — share with peers."""
        return self._public.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw)

    @property
    def public_key_hex(self) -> str:
        return self.public_key_bytes.hex()

    def add_peer(self, node_id: str, pubkey_bytes: bytes) -> None:
        """Register a peer's public key."""
        self._peers[node_id] = X25519PublicKey.from_public_bytes(pubkey_bytes)
        log.info(f"Peer registered: {node_id} "
                 f"(fp={hashlib.sha256(pubkey_bytes).hexdigest()[:8]})")

    def add_peer_hex(self, node_id: str, pubkey_hex: str) -> None:
        """Register a peer's public key from hex string."""
        self.add_peer(node_id, bytes.fromhex(pubkey_hex))

    def save_peer(self, node_id: str, pubkey_bytes: bytes,
                  key_dir: str) -> None:
        """Save a peer public key to disk."""
        path = Path(key_dir) / "peers" / f"{node_id}.pub"
        path.write_bytes(pubkey_bytes)
        self.add_peer(node_id, pubkey_bytes)

    def list_peers(self) -> list:
        return list(self._peers.keys())

    # ── Encryption ─────────────────────────────────────────

    def encrypt(self, plaintext: bytes,
                recipient_id: Optional[str] = None) -> bytes:
        """
        Encrypt a frame payload.

        recipient_id=None or 'broadcast' → group key (AES-GCM, 28B overhead)
        recipient_id='node-b'            → ECIES unicast (60B overhead)

        Returns prefixed ciphertext ready to embed in MACFrame.payload.
        """
        if recipient_id is None or recipient_id == 'broadcast':
            return PREFIX_BROADCAST + self._encrypt_group(plaintext)
        else:
            if recipient_id not in self._peers:
                raise KeyError(f"Unknown peer: {recipient_id}. "
                               f"Call add_peer() first.")
            return PREFIX_UNICAST + self._encrypt_ecies(
                plaintext, self._peers[recipient_id])

    def decrypt(self, data: bytes) -> Optional[bytes]:
        """
        Decrypt a frame payload.
        Returns plaintext on success, None on authentication failure.
        """
        if not data:
            return None

        prefix = data[:1]
        body   = data[1:]

        if prefix == PREFIX_BROADCAST:
            return self._decrypt_group(body)
        elif prefix == PREFIX_UNICAST:
            return self._decrypt_ecies(body)
        else:
            # Unencrypted passthrough (crypto disabled on sender)
            return data

    # ── ECIES (unicast) ────────────────────────────────────

    def _encrypt_ecies(self, plaintext: bytes,
                       recipient_pub: 'X25519PublicKey') -> bytes:
        """
        ECIES encrypt:
          ephemeral keypair → ECDH → HKDF → AES-256-GCM
        Output: [ephemeral_pubkey 32B] [IV 12B] [ciphertext + tag N+16B]
        """
        # Ephemeral keypair (new per frame)
        eph_priv = X25519PrivateKey.generate()
        eph_pub  = eph_priv.public_key()

        # ECDH shared secret
        shared = eph_priv.exchange(recipient_pub)

        # HKDF key derivation
        aes_key = HKDF(
            algorithm = hashes.SHA256(),
            length    = 32,
            salt      = None,
            info      = b'lionmesh-ecies-v1',
        ).derive(shared)

        # Encrypt
        iv         = os.urandom(IV_SIZE)
        ciphertext = AESGCM(aes_key).encrypt(iv, plaintext, None)

        # Serialise ephemeral public key
        eph_pub_bytes = eph_pub.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

        return eph_pub_bytes + iv + ciphertext

    def _decrypt_ecies(self, data: bytes) -> Optional[bytes]:
        """
        ECIES decrypt:
          extract ephemeral pubkey → ECDH with own private → HKDF → AES-GCM
        """
        if len(data) < PUBKEY_SIZE + IV_SIZE + TAG_SIZE:
            return None

        eph_pub_bytes = data[:PUBKEY_SIZE]
        iv            = data[PUBKEY_SIZE:PUBKEY_SIZE+IV_SIZE]
        ciphertext    = data[PUBKEY_SIZE+IV_SIZE:]

        try:
            eph_pub = X25519PublicKey.from_public_bytes(eph_pub_bytes)
            shared  = self._private.exchange(eph_pub)
            aes_key = HKDF(
                algorithm = hashes.SHA256(),
                length    = 32,
                salt      = None,
                info      = b'lionmesh-ecies-v1',
            ).derive(shared)
            return AESGCM(aes_key).decrypt(iv, ciphertext, None)
        except Exception:
            log.warning("ECIES: decryption failed — dropped")
            return None

    # ── Group key (broadcast) ──────────────────────────────

    def _encrypt_group(self, plaintext: bytes) -> bytes:
        """AES-256-GCM with group key. [IV 12B] [ciphertext + tag N+16B]"""
        iv = os.urandom(IV_SIZE)
        return iv + self._group.encrypt(iv, plaintext, None)

    def _decrypt_group(self, data: bytes) -> Optional[bytes]:
        """Decrypt group-key frame."""
        if len(data) < IV_SIZE + TAG_SIZE:
            return None
        iv, ct = data[:IV_SIZE], data[IV_SIZE:]
        try:
            return self._group.decrypt(iv, ct, None)
        except Exception:
            log.warning("Group key: authentication failed — dropped")
            return None


# ─────────────────────────────────────────────
# Config helper
# ─────────────────────────────────────────────

def crypto_from_config(cfg) -> Optional[NodeCrypto]:
    """
    Create a NodeCrypto instance from node.conf.

    [crypto]
    enabled    = true
    key_dir    = /etc/lionmesh/keys
    node_id    = lionmesh-a
    group_psk  = shared-broadcast-passphrase
    group_salt = lionmesh-ppdr-lux-2026
    """
    if not cfg.has_section('crypto'):
        return None
    if not cfg.getboolean('crypto', 'enabled', fallback=False):
        return None
    if not CRYPTO_AVAILABLE:
        log.error("Encryption enabled but cryptography not installed")
        return None

    key_dir    = cfg.get('crypto', 'key_dir',    fallback='/etc/lionmesh/keys')
    node_id    = cfg.get('node',   'node_id',    fallback='unknown')
    group_psk  = cfg.get('crypto', 'group_psk',  fallback='')
    group_salt = cfg.get('crypto', 'group_salt', fallback='lionmesh-ppdr')

    return NodeCrypto.load_or_generate(
        key_dir    = key_dir,
        node_id    = node_id,
        group_psk  = group_psk,
        group_salt = group_salt,
    )


# ─────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────

if __name__ == '__main__':
    print("LionMesh Crypto Self-Test (Asymmetric)")
    print("=" * 50)

    if not CRYPTO_AVAILABLE:
        print("✗ pip install cryptography")
        exit(1)

    # Generate two nodes
    node_a = NodeCrypto.generate(node_id='node-a', group_psk='ppdr-group-key')
    node_b = NodeCrypto.generate(node_id='node-b', group_psk='ppdr-group-key')

    # Exchange public keys
    node_a.add_peer('node-b', node_b.public_key_bytes)
    node_b.add_peer('node-a', node_a.public_key_bytes)

    msg = b'LionMesh PPDR encrypted unicast frame' * 3

    # Test 1: unicast A→B
    enc = node_a.encrypt(msg, recipient_id='node-b')
    dec = node_b.decrypt(enc)
    assert dec == msg
    print(f"✓ Unicast A→B: {len(msg)}B → {len(enc)}B "
          f"(overhead={len(enc)-len(msg)}B)")

    # Test 2: unicast B→A
    enc2 = node_b.encrypt(msg, recipient_id='node-a')
    dec2 = node_a.decrypt(enc2)
    assert dec2 == msg
    print(f"✓ Unicast B→A: roundtrip OK")

    # Test 3: broadcast (group key)
    enc3 = node_a.encrypt(msg, recipient_id='broadcast')
    dec3 = node_b.decrypt(enc3)
    assert dec3 == msg
    print(f"✓ Broadcast (group key): {len(msg)}B → {len(enc3)}B "
          f"(overhead={len(enc3)-len(msg)}B)")

    # Test 4: wrong node cannot decrypt unicast
    node_c = NodeCrypto.generate(node_id='node-c', group_psk='ppdr-group-key')
    result = node_c.decrypt(enc)
    assert result is None
    print("✓ Node-C cannot decrypt A→B unicast (ECIES)")

    # Test 5: wrong group key cannot decrypt broadcast
    node_evil = NodeCrypto.generate(node_id='evil', group_psk='wrong-key')
    result2 = node_evil.decrypt(enc3)
    assert result2 is None
    print("✓ Wrong group key cannot decrypt broadcast")

    # Test 6: each frame has unique ephemeral key
    enc_1 = node_a.encrypt(msg, recipient_id='node-b')
    enc_2 = node_a.encrypt(msg, recipient_id='node-b')
    assert enc_1 != enc_2
    print("✓ Unique ephemeral key per frame (forward secrecy)")

    print()
    print(f"Unicast overhead:   {UNICAST_OVERHEAD}B "
          f"(32B ephemeral pubkey + 12B IV + 16B tag)")
    print(f"Broadcast overhead: {BROADCAST_OVERHEAD}B "
          f"(12B IV + 16B tag)")
    print(f"Curve25519 + HKDF-SHA256 + AES-256-GCM")
