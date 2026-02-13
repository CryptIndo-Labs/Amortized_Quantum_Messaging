"""
Chat protocol — message envelope, simulated encrypt/decrypt, channel naming.

ChatMessage carries all metadata needed for the AQM lifecycle demonstration:
sender/recipient IDs, the consumed coin (tier + key_id + public key), simulated
ciphertext, and a plaintext hash for receiver-side verification.

Simulated encryption: SHA-256(pk || plaintext) tag (32B) prepended to plaintext.
In production this would be Kyber KEM + AES-GCM.
"""

import json
import uuid
import time
import hashlib
import base64
from dataclasses import dataclass, asdict


CHANNEL_PREFIX = "aqm:chat"


@dataclass
class ChatMessage:
    msg_id: str
    sender_id: str
    recipient_id: str
    timestamp: float
    coin_tier: str
    key_id: str
    public_key_b64: str
    ciphertext_b64: str
    plaintext_hash: str
    device_context: str


def channel_for(user_id: str) -> str:
    """Return the Redis pub/sub channel name for a user."""
    return f"{CHANNEL_PREFIX}:{user_id}"


def simulate_encrypt(plaintext: str, public_key: bytes) -> bytes:
    """Simulate post-quantum encryption.

    Returns: SHA-256(pk || plaintext) (32B) + UTF-8 plaintext bytes.
    """
    pt_bytes = plaintext.encode("utf-8")
    tag = hashlib.sha256(public_key + pt_bytes).digest()
    return tag + pt_bytes


def simulate_decrypt(ciphertext: bytes, public_key: bytes) -> tuple[str, bool]:
    """Simulate post-quantum decryption.

    Returns (plaintext_str, tag_valid).
    The tag is verified by recomputing SHA-256(pk || plaintext).
    """
    if len(ciphertext) < 32:
        return ("", False)
    tag = ciphertext[:32]
    pt_bytes = ciphertext[32:]
    expected_tag = hashlib.sha256(public_key + pt_bytes).digest()
    plaintext = pt_bytes.decode("utf-8", errors="replace")
    return (plaintext, tag == expected_tag)


def build_message(
    sender_id: str,
    recipient_id: str,
    coin_tier: str,
    key_id: str,
    public_key: bytes,
    plaintext: str,
    device_context: str = "",
) -> ChatMessage:
    """Build a ChatMessage with simulated encryption."""
    ciphertext = simulate_encrypt(plaintext, public_key)
    plaintext_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    return ChatMessage(
        msg_id=str(uuid.uuid4()),
        sender_id=sender_id,
        recipient_id=recipient_id,
        timestamp=time.time(),
        coin_tier=coin_tier,
        key_id=key_id,
        public_key_b64=base64.b64encode(public_key).decode("ascii"),
        ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
        plaintext_hash=plaintext_hash,
        device_context=device_context,
    )


def serialize(msg: ChatMessage) -> str:
    """Serialize a ChatMessage to JSON string."""
    return json.dumps(asdict(msg))


def deserialize(data: str) -> ChatMessage:
    """Deserialize a JSON string to ChatMessage."""
    d = json.loads(data)
    return ChatMessage(**d)
