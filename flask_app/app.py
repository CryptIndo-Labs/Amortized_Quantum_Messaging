"""
AQM Flask UI — Main application server.

Bridges the Flask web interface to the AQM backend subsystems.
Run with:
    python flask_app/app.py --user alice
    python flask_app/app.py --user bob --port 5001
"""

import asyncio
import json
import os
import queue
import sys
import threading
import time
import uuid
import argparse
import logging
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context


# ── Path setup ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from AQM_Database.aqm_db.connection import create_vault_client, create_inventory_client
from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_db.stats import StorageReporter
from AQM_Database.aqm_contacts.contacts_db import ContactsDatabase
from AQM_Database.aqm_contacts.models import Contact
from AQM_Database.aqm_session.ratchet import SessionRatchet
from AQM_Database.aqm_session.session_store import SessionStore
from AQM_Database.aqm_shared.crypto_engine import CryptoEngine
from AQM_Database.aqm_shared.context_manager import ContextManager, DeviceContext
from AQM_Database.aqm_shared.types import CoinUpload
from AQM_Database.aqm_shared import config as aqm_config
from AQM_Database.aqm_network.protocol import frame_message, parse_message
from AQM_Database.bridge import sync_inventory
from aqm_bridge import run_async
from uuid import UUID
from AQM_Database.bridge import upload_coins, sync_inventory
from AQM_Database.aqm_server.coin_inventory import CoinInventoryServer
from AQM_Database.aqm_server import config as srv_config
from AQM_Database.aqm_server.db import create_pool



logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aqm.flask")

# ── Argument parsing ─────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--user", default="alice", help="User identity (alice or bob)")
parser.add_argument("--port", type=int, default=5000)
parser.add_argument("--partner-port", type=int, default=None,
                    help="Port of partner's Flask instance for direct HTTP messaging")
args, _ = parser.parse_known_args()

USER_ID   = args.user.lower()
PARTNER_ID = "bob" if USER_ID == "alice" else "alice"
PORT       = args.port
PARTNER_PORT = args.partner_port or (5001 if USER_ID == "alice" else 5000)

# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.urandom(24)

# ── AQM subsystem init ────────────────────────────────────────────────────────
vault_client     = create_vault_client()
inv_client       = create_inventory_client()
vault            = SecureVault(vault_client)
inventory        = SmartInventory(inv_client)
reporter         = StorageReporter(vault, inventory)
# --- REAL COIN SERVER CONNECTION ---
server_pool = run_async(create_pool(
    srv_config.PG_DSN,
    srv_config.PG_POOL_MIN_SIZE,
    srv_config.PG_POOL_MAX_SIZE
))
coin_server = CoinInventoryServer(server_pool)

USER_UUIDS = {
    "alice": UUID("00000000-0000-0000-0000-000000000002"),
    "bob": UUID("00000000-0000-0000-0000-000000000001"),
}
contacts_db      = ContactsDatabase(db_path=f"~/.aqm/{USER_ID}_contacts.db")
session_store    = SessionStore(db_path=f"{USER_ID}_sessions.db")
crypto           = CryptoEngine()
context_mgr      = ContextManager()

# In-memory active ratchets
active_ratchets: dict[str, SessionRatchet] = {}

# SSE message queue — incoming messages pushed here for the browser
sse_queue: queue.Queue = queue.Queue(maxsize=100)

# Message history (in-memory, per session)
message_history: list[dict] = []

# ── Simulated device context (randomised per message like the CLI demo) ──────
import random

def random_device_context() -> DeviceContext:
    battery = random.uniform(5, 100)
    wifi    = random.choice([True, True, False])
    signal  = random.uniform(-110, -60)
    return DeviceContext(battery_pct=battery, wifi_connected=wifi, signal_dbm=signal)


def _seed_inventory_for_demo():
    """Seed inventory with real minted public keys so coins can be consumed."""
    summary = inventory.get_inventory(PARTNER_ID)
    if summary.gold_count + summary.silver_count + summary.bronze_count > 0:
        return  # already seeded

    caps = aqm_config.BUDGET_CAPS["BESTIE"]
    for tier, count in [("GOLD", caps["GOLD"]), ("SILVER", caps["SILVER"]), ("BRONZE", caps["BRONZE"])]:
        for _ in range(count):
            bundle = crypto.mint_coin(tier)
            try:
                inventory.store_key(
                    contact_id=PARTNER_ID,
                    key_id=bundle.key_id,
                    coin_category=bundle.coin_category,
                    public_key=bundle.public_key,
                    signature=bundle.signature,
                )
            except Exception:
                pass
    logger.info("Seeded inventory for %s", PARTNER_ID)

# ── Bootstrap: mint coins + register partner contact ─────────────────────────
def bootstrap():
    """Mint coins if vault is low, register partner as BESTIE."""
    logger.info("Bootstrapping AQM for user: %s", USER_ID)

    # Mint coins if needed
    targets = {"GOLD": 5, "SILVER": 6, "BRONZE": 5}
    minted = 0
    minted_bundles = []
    
    for tier, count in targets.items():
        current = vault.count_active(tier)
        needed  = max(0, count - current)
        for _ in range(needed):
            bundle = crypto.mint_coin(tier)
            minted_bundles.append(bundle)
            vault_key = os.urandom(32)
            blob = crypto.encrypt_aead(bundle.secret_key, vault_key, bundle.key_id.encode())
            iv, auth_tag, enc_blob = blob[:12], blob[-16:], blob[12:-16]
            vault.store_key(
                key_id=bundle.key_id,
                coin_category=bundle.coin_category,
                encrypted_blob=enc_blob,
                encryption_iv=iv,
                auth_tag=auth_tag,
            )
            minted += 1
    logger.info("Minted %d new coins", minted)
    
    uploads = [
        CoinUpload(
            key_id=b.key_id,
            coin_category=b.coin_category,
            public_key_blob=b.public_key,
            signature_blob=b.signature,
        )
        for b in minted_bundles
    ]

    if uploads:
        logger.info("Preparing to upload %d coins to server", len(uploads))
        run_async(upload_coins(coin_server, USER_UUIDS[USER_ID], uploads))
        logger.info("Uploaded %d newly minted coins to server", len(uploads))
    else:
        logger.info("Vault already populated — skipping upload")
        
        
    # Register partner contact
    try:
        existing = contacts_db.get_contact(PARTNER_ID)
        if not existing:
            contacts_db.add_contact(PARTNER_ID, PARTNER_ID.capitalize())
            contacts_db.lock_priority(PARTNER_ID, "BESTIE")
            logger.info("Registered %s as BESTIE", PARTNER_ID)
    except Exception as e:
        logger.warning("Could not register partner: %s", e)

    # Seed inventory with some coins for demo (no server needed)
    try:
        inventory.register_contact(PARTNER_ID, "BESTIE", PARTNER_ID.capitalize())
    except Exception:
        pass

    # Store some demo public keys in inventory so sending works offline
    # _seed_inventory_for_demo() # used for demo; in real usage, the partner would mint and upload their own coins
    

    # Sync inventory from real coin server
    try:
        fetched = run_async(sync_inventory(
            coin_server,
            inventory,
            PARTNER_ID,
            USER_UUIDS[PARTNER_ID],
            USER_UUIDS[USER_ID],
        ))
        logger.info("Inventory synced from server: %s", fetched)
    except Exception as e:
        logger.warning("Server sync failed, falling back to demo seed: %s", e)
        _seed_inventory_for_demo()


bootstrap()



# ── Helpers ───────────────────────────────────────────────────────────────────

def get_ratchet(contact_id: str) -> SessionRatchet | None:
    if contact_id in active_ratchets:
        return active_ratchets[contact_id]
    r = session_store.load_ratchet(contact_id)
    if r:
        active_ratchets[contact_id] = r
    return r


def save_ratchet(r: SessionRatchet):
    active_ratchets[r.contact_id] = r
    session_store.save_ratchet(r)


def tier_color(tier: str) -> str:
    return {"GOLD": "#FFD700", "SILVER": "#C0C0C0", "BRONZE": "#CD7F32"}.get(tier, "#888")


def coin_counts() -> dict:
    try:
        summary = inventory.get_inventory(PARTNER_ID)
        return {
            "gold":   summary.gold_count,
            "silver": summary.silver_count,
            "bronze": summary.bronze_count,
        }
    except Exception:
        return {"gold": 0, "silver": 0, "bronze": 0}


def vault_stats_dict() -> dict:
    try:
        s = vault.get_stats()
        return {
            "active_gold":   s.active_gold,
            "active_silver": s.active_silver,
            "active_bronze": s.active_bronze,
            "total_burned":  s.total_burned,
            "total_expired": s.total_expired,
        }
    except Exception:
        return {"active_gold": 0, "active_silver": 0, "active_bronze": 0,
                "total_burned": 0, "total_expired": 0}


def contacts_list() -> list[dict]:
    try:
        all_contacts = contacts_db.get_all_contacts() or []
        result = []
        for c in all_contacts:
            inv_summary = None
            try:
                inv_summary = inventory.get_inventory(c.contact_id)
            except Exception:
                pass
            result.append({
                "contact_id":     c.contact_id,
                "display_name":   c.display_name,
                "priority":       c.priority,
                "priority_locked": bool(c.priority_locked),
                "msg_count_total": c.msg_count_total,
                "msg_count_7d":   c.msg_count_7d,
                "msg_count_30d":  c.msg_count_30d,
                "is_blocked":     bool(c.is_blocked),
                "last_msg_at":    str(c.last_msg_at) if c.last_msg_at else None,
                "coins": {
                    "gold":   inv_summary.gold_count   if inv_summary else 0,
                    "silver": inv_summary.silver_count if inv_summary else 0,
                    "bronze": inv_summary.bronze_count if inv_summary else 0,
                } if inv_summary else {"gold": 0, "silver": 0, "bronze": 0},
            })
        return result
    except Exception as e:
        logger.warning("contacts_list error: %s", e)
        return []


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html",
                           user_id=USER_ID,
                           partner_id=PARTNER_ID,
                           partner_port=PARTNER_PORT)


@app.route("/api/status")
def api_status():
    counts = coin_counts()
    vstats = vault_stats_dict()
    return jsonify({
        "user_id":    USER_ID,
        "partner_id": PARTNER_ID,
        "coins":      counts,
        "vault":      vstats,
        "contacts":   contacts_list(),
    })


@app.route("/api/send", methods=["POST"])
def api_send():
    data      = request.get_json()
    plaintext = data.get("message", "").strip()
    if not plaintext:
        return jsonify({"error": "empty message"}), 400

    # Device context → tier
    ctx       = random_device_context()
    ideal_tier = context_mgr.select_coin(ctx)

    # Apply BESTIE ceiling (GOLD allowed)
    tier = ideal_tier

    # Pop a coin from inventory
    coin = inventory.select_coin(PARTNER_ID, tier)
    if coin is None:
        # Reseed and try again
        _seed_inventory_for_demo()
        coin = inventory.select_coin(PARTNER_ID, "BRONZE")
    if coin is None:
        return jsonify({"error": "no coins available"}), 503

    # Get or create ratchet
    ratchet = get_ratchet(PARTNER_ID)
    kem_ct, coin_id_used = None, None

    if ratchet is None or ratchet.needs_rekey():
        ct, shared_secret = crypto.kem_encapsulate(coin.public_key)
        kem_ct = ct
        if ratchet is None:
            ratchet = SessionRatchet(PARTNER_ID, coin.coin_category, shared_secret)
        else:
            ratchet.rekey(shared_secret, coin.coin_category)
        coin_id_used = coin.key_id

    msg_key      = ratchet.derive_message_key()
    aad          = f"{USER_ID}:{PARTNER_ID}".encode()
    enc_payload  = crypto.encrypt_aead(plaintext.encode(), msg_key, aad)
    save_ratchet(ratchet)

    import base64
    parcel = {
        "sender_id":         USER_ID,
        "recipient_id":      PARTNER_ID,
        "encrypted_payload": base64.b64encode(enc_payload).decode(),
        "aad":               base64.b64encode(aad).decode(),
        "coin_tier":         coin.coin_category,
        "plaintext":         plaintext,          # included for demo so partner can display
        "device_ctx": {
            "battery": round(ctx.battery_pct, 1),
            "wifi":    ctx.wifi_connected,
            "signal":  round(ctx.signal_dbm, 1),
        },
    }
    if coin_id_used:
        parcel["coin_id"]      = coin_id_used
        parcel["kem_ciphertext"] = base64.b64encode(kem_ct).decode()

    # Build message record
    msg_record = {
        "id":        str(uuid.uuid4()),
        "sender":    USER_ID,
        "recipient": PARTNER_ID,
        "text":      plaintext,
        "tier":      coin.coin_category,
        "tier_color": tier_color(coin.coin_category),
        "device_ctx": parcel["device_ctx"],
        "ts":        time.time(),
        "rekey":     coin_id_used is not None,
        "msg_count": ratchet.msg_counter,
        "max_msgs":  ratchet.max_messages,
    }
    message_history.append(msg_record)

    # Record in contacts DB for priority tracking
    try:
        contacts_db.record_message(PARTNER_ID)
    except Exception:
        pass

    # Push to own SSE stream
    sse_queue.put({"type": "message", "data": msg_record})
    sse_queue.put({"type": "status_update"})

    # Forward to partner via HTTP (fire-and-forget)
    _forward_to_partner(parcel, msg_record)

    return jsonify({"ok": True, "message": msg_record, "coins": coin_counts()})


def _forward_to_partner(parcel: dict, msg_record: dict):
    """POST the parcel to the partner's Flask instance."""
    import urllib.request
    import urllib.error
    partner_url = f"http://localhost:{PARTNER_PORT}/api/receive"
    payload = json.dumps({
        "parcel":     parcel,
        "msg_record": msg_record,
    }).encode()
    req = urllib.request.Request(
        partner_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2)
    except Exception as e:
        logger.debug("Could not forward to partner (offline?): %s", e)


@app.route("/api/receive", methods=["POST"])
def api_receive():
    """Called by partner's Flask instance to deliver a message."""
    data       = request.get_json()
    parcel     = data.get("parcel", {})
    msg_record = data.get("msg_record", {})

    # Decrypt if we have a ratchet (best-effort for demo)
    sender = parcel.get("sender_id", "")
    import base64

    ratchet = get_ratchet(sender)
    decrypted_text = parcel.get("plaintext", "")  # fallback: use included plaintext

    if "kem_ciphertext" in parcel and "coin_id" in parcel:
        try:
            coin_id = parcel["coin_id"]
            kem_ct  = base64.b64decode(parcel["kem_ciphertext"])
            entry   = vault.fetch_key(coin_id)
            if entry:
                shared_secret = crypto.kem_decapsulate(kem_ct, entry.encrypted_blob)
                coin_tier     = parcel.get("coin_tier", "BRONZE")
                if ratchet is None:
                    ratchet = SessionRatchet(sender, coin_tier, shared_secret)
                else:
                    ratchet.rekey(shared_secret, coin_tier)
                vault.burn_key(coin_id)
        except Exception as e:
            logger.debug("KEM decap failed (expected in demo): %s", e)

    if ratchet:
        try:
            msg_key  = ratchet.derive_message_key()
            aad      = f"{sender}:{USER_ID}".encode()
            enc_data = base64.b64decode(parcel["encrypted_payload"])
            decrypted_text = crypto.decrypt_aead(enc_data, msg_key, aad).decode()
            save_ratchet(ratchet)
        except Exception as e:
            logger.debug("Decrypt failed, using plaintext fallback: %s", e)

    # Build incoming message record
    incoming = {
        "id":         str(uuid.uuid4()),
        "sender":     sender,
        "recipient":  USER_ID,
        "text":       decrypted_text,
        "tier":       parcel.get("coin_tier", "BRONZE"),
        "tier_color": tier_color(parcel.get("coin_tier", "BRONZE")),
        "device_ctx": parcel.get("device_ctx", {}),
        "ts":         time.time(),
        "rekey":      "kem_ciphertext" in parcel,
        "msg_count":  ratchet.msg_counter if ratchet else 0,
        "max_msgs":   ratchet.max_messages if ratchet else 0,
        "incoming":   True,
    }
    message_history.append(incoming)

    try:
        contacts_db.record_message(sender)
    except Exception:
        pass

    sse_queue.put({"type": "message", "data": incoming})
    sse_queue.put({"type": "status_update"})

    return jsonify({"ok": True})


@app.route("/api/history")
def api_history():
    return jsonify({"messages": message_history[-100:]})


@app.route("/api/contacts")
def api_contacts():
    return jsonify({"contacts": contacts_list()})


@app.route("/api/contacts/<contact_id>/priority", methods=["POST"])
def api_set_priority(contact_id):
    data     = request.get_json()
    priority = data.get("priority", "STRANGER")
    locked   = data.get("locked", True)
    if locked:
        contacts_db.lock_priority(contact_id, priority)
    else:
        contacts_db.unlock_priority(contact_id)
    return jsonify({"ok": True, "contact": contact_id, "priority": priority})


@app.route("/api/vault")
def api_vault():
    return jsonify(vault_stats_dict())


@app.route("/api/inventory")
def api_inventory():
    return jsonify({"coins": coin_counts(), "partner": PARTNER_ID})

@app.route("/api/debug/server-coins")
def api_debug_server_coins():
    """Debug endpoint: show coins stored on the PostgreSQL coin server."""
    try:
        coins = run_async(
            coin_server.fetch_coins(
                USER_UUIDS[USER_ID],   # owner of coins
                USER_UUIDS[USER_ID],   # requester
                "GOLD",                # tier (temporary)
                100                    # max fetch
            )
        )

        return jsonify({
            "count": len(coins),
            "coins": [
                {
                    "key_id": c.key_id,
                    "tier": c.coin_category
                }
                for c in coins
            ]
        })

    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/stream")
def stream():
    """SSE endpoint — browser subscribes here for real-time updates."""
    def event_generator():
        # Send initial state
        yield f"data: {json.dumps({'type': 'connected', 'user': USER_ID})}\n\n"
        while True:
            try:
                item = sse_queue.get(timeout=25)
                if item.get("type") == "status_update":
                    payload = {
                        "type":     "status_update",
                        "coins":    coin_counts(),
                        "vault":    vault_stats_dict(),
                        "contacts": contacts_list(),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                else:
                    yield f"data: {json.dumps(item)}\n\n"
            except queue.Empty:
                # Keepalive ping
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return Response(
        stream_with_context(event_generator()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":  "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    logger.info("Starting AQM Flask UI for user=%s on port=%d", USER_ID, PORT)
    logger.info("Partner=%s expected on port=%d", PARTNER_ID, PARTNER_PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
