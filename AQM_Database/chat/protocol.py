"""
Chat protocol — message envelope, encrypt/decrypt, channel naming.

ChatMessage carries all metadata needed for the AQM lifecycle demonstration:
sender/recipient IDs, the consumed coin (tier + key_id + public key),
ciphertext, and a plaintext hash for receiver-side verification.

Encryption: NaCl SecretBox (XSalsa20-Poly1305 AEAD) with a symmetric key
derived from SHA-256(public_key).  Key agreement is simplified — in production
this would be Kyber KEM (Gold/Silver) or X25519 DH (Bronze).
"""

import json
import uuid
import time
import hashlib
import base64
from dataclasses import dataclass, asdict

try:
    import nacl.secret
    import nacl.exceptions
    _HAS_NACL = True
except ImportError:
    _HAS_NACL = False


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


def encrypt_message(plaintext: str, public_key: bytes) -> bytes:
    """Encrypt plaintext with real AEAD.

    Uses NaCl SecretBox (XSalsa20-Poly1305) with key = SHA-256(pk).
    Returns nonce (24 B) + ciphertext + MAC (16 B).
    Falls back to SHA-256 tag if pynacl is unavailable.
    """
    key = hashlib.sha256(public_key).digest()
    pt_bytes = plaintext.encode("utf-8")

    if _HAS_NACL:
        box = nacl.secret.SecretBox(key)
        return bytes(box.encrypt(pt_bytes))

    # Fallback: SHA-256 tag (no confidentiality)
    tag = hashlib.sha256(public_key + pt_bytes).digest()
    return tag + pt_bytes


def decrypt_message(ciphertext: bytes, public_key: bytes) -> tuple[str, bool]:
    """Decrypt ciphertext with real AEAD.

    Returns (plaintext_str, verified).
    Falls back to SHA-256 tag check if pynacl is unavailable.
    """
    key = hashlib.sha256(public_key).digest()

    if _HAS_NACL:
        try:
            box = nacl.secret.SecretBox(key)
            pt_bytes = box.decrypt(ciphertext)
            return (pt_bytes.decode("utf-8"), True)
        except nacl.exceptions.CryptoError:
            return ("", False)

    # Fallback: SHA-256 tag verification
    if len(ciphertext) < 32:
        return ("", False)
    tag = ciphertext[:32]
    pt_bytes = ciphertext[32:]
    expected_tag = hashlib.sha256(public_key + pt_bytes).digest()
    return (pt_bytes.decode("utf-8", errors="replace"), tag == expected_tag)


# Backwards-compatible aliases
simulate_encrypt = encrypt_message
simulate_decrypt = decrypt_message


def build_message(
    sender_id: str,
    recipient_id: str,
    coin_tier: str,
    key_id: str,
    public_key: bytes,
    plaintext: str,
    device_context: str = "",
) -> ChatMessage:
    """Build a ChatMessage with real AEAD encryption."""
    ciphertext = encrypt_message(plaintext, public_key)
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
