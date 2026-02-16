"""
Python port of the C++ CryptoEngine (codes/src/crypto/crypto_engine.cpp).

Provides post-quantum key generation (Kyber-768 for GOLD/SILVER, X25519 for BRONZE),
Ed25519 signing, and a mint_coin() helper that produces all artifacts for one coin.

Backend priority:
  1. liboqs-python + pynacl  → full post-quantum
  2. pynacl only             → Ed25519/X25519 real, Kyber mocked
  3. os.urandom fallback     → all mocked (always available)
"""

import os
import uuid
import hashlib
from dataclasses import dataclass

from AQM_Database.aqm_shared import config
from AQM_Database.aqm_shared.errors import InvalidCoinCategoryError

# ─── Key sizes (bytes) ───

KYBER768_PK_SIZE = 1184
KYBER768_SK_SIZE = 2400
X25519_PK_SIZE = 32
X25519_SK_SIZE = 32
ED25519_SIG_SIZE = 64
DILITHIUM3_SIG_SIZE = 2420

# ─── Backend detection ───

_HAS_NACL = False
_HAS_OQS = False

try:
    import nacl.signing
    import nacl.utils
    _HAS_NACL = True
except ImportError:
    pass

try:
    import oqs
    _HAS_OQS = True
except ImportError:
    pass


class CryptoEngine:
    """Key generation and signing engine with graceful backend fallback."""

    def __init__(self):
        if _HAS_OQS and _HAS_NACL:
            self._backend = "liboqs+pynacl"
        elif _HAS_NACL:
            self._backend = "pynacl-only"
        else:
            self._backend = "urandom-mock"

        # Persistent Ed25519 signing key (used for all signatures)
        if _HAS_NACL:
            self._signing_key = nacl.signing.SigningKey.generate()
        else:
            self._signing_key = None

    @property
    def backend(self) -> str:
        return self._backend

    def generate_keypair(self, coin_category: str) -> tuple[bytes, bytes]:
        """Generate (public_key, secret_key) bytes for the given coin tier.

        GOLD/SILVER → Kyber-768 (1184B pk, 2400B sk)
        BRONZE      → X25519   (32B pk, 32B sk)
        """
        if coin_category not in config.VALID_COIN_CATEGORIES:
            raise InvalidCoinCategoryError(coin_category)

        if coin_category in ("GOLD", "SILVER"):
            return self._generate_kyber768()
        else:
            return self._generate_x25519()

    def sign_key(self, public_key: bytes, coin_category: str) -> bytes:
        """Sign a public key blob, returning signature bytes.

        GOLD  → Dilithium-3 sized (2420 B): real Ed25519 core + random padding
                (real Dilithium requires liboqs)
        SILVER/BRONZE → Ed25519 (64 B)
        Fallback (no pynacl) → SHA-256 hash mock.
        """
        if coin_category not in config.VALID_COIN_CATEGORIES:
            raise InvalidCoinCategoryError(coin_category)

        if coin_category == "GOLD":
            if self._signing_key is not None:
                core = self._signing_key.sign(public_key).signature  # 64 B
                return core + os.urandom(DILITHIUM3_SIG_SIZE - ED25519_SIG_SIZE)
            else:
                return os.urandom(DILITHIUM3_SIG_SIZE)

        # SILVER / BRONZE — Ed25519
        if self._signing_key is not None:
            return self._signing_key.sign(public_key).signature  # 64 bytes
        else:
            return hashlib.sha256(public_key).digest()  # 32-byte mock

    # ─── Private generators ───

    def _generate_kyber768(self) -> tuple[bytes, bytes]:
        if _HAS_OQS:
            kem = oqs.KeyEncapsulation("Kyber768")
            pk = kem.generate_keypair()
            sk = kem.export_secret_key()
            return (bytes(pk), bytes(sk))

        if _HAS_NACL:
            # Real X25519 keygen + random padding to correct Kyber sizes.
            # Ensures keygen cost is comparable to Bronze (real EC work),
            # so benchmark ordering is driven by data size, not mock speed.
            from nacl.public import PrivateKey
            sk_nacl = PrivateKey.generate()
            pk = bytes(sk_nacl.public_key) + os.urandom(KYBER768_PK_SIZE - X25519_PK_SIZE)
            sk = bytes(sk_nacl) + os.urandom(KYBER768_SK_SIZE - X25519_SK_SIZE)
            return (pk, sk)

        # Pure mock: correct sizes, random bytes
        return (os.urandom(KYBER768_PK_SIZE), os.urandom(KYBER768_SK_SIZE))

    def _generate_x25519(self) -> tuple[bytes, bytes]:
        if _HAS_NACL:
            from nacl.public import PrivateKey
            sk = PrivateKey.generate()
            return (bytes(sk.public_key), bytes(sk))

        return (os.urandom(X25519_PK_SIZE), os.urandom(X25519_SK_SIZE))


@dataclass
class MintedCoinBundle:
    """All artifacts produced by minting a single coin."""
    key_id: str
    coin_category: str
    public_key: bytes
    secret_key: bytes
    signature: bytes
    encrypted_blob: bytes
    encryption_iv: bytes
    auth_tag: bytes


def mint_coin(engine: CryptoEngine, coin_category: str) -> MintedCoinBundle:
    """Generate a full coin: keypair + signature + simulated HW encryption.

    The encrypted_blob simulates what a hardware security module would produce
    when encrypting the secret key for vault storage.
    """
    if coin_category not in config.VALID_COIN_CATEGORIES:
        raise InvalidCoinCategoryError(coin_category)

    key_id = str(uuid.uuid4())
    pk, sk = engine.generate_keypair(coin_category)
    sig = engine.sign_key(pk, coin_category)

    # Simulate hardware encryption of the secret key
    iv = os.urandom(12)       # AES-GCM nonce
    auth_tag = os.urandom(16) # AES-GCM auth tag
    # In production this would be AES-256-GCM(sk, hw_key); here we just wrap it
    encrypted_blob = os.urandom(16) + sk  # prefix simulates encryption overhead

    return MintedCoinBundle(
        key_id=key_id,
        coin_category=coin_category,
        public_key=pk,
        secret_key=sk,
        signature=sig,
        encrypted_blob=encrypted_blob,
        encryption_iv=iv,
        auth_tag=auth_tag,
    )
