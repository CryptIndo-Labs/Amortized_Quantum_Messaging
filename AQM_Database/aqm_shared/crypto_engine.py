"""
Post-quantum crypto engine for AQM.

Requires liboqs-python + pynacl. No fallbacks. No mocks.
If either dependency is missing, ImportError crashes immediately.
"""

import os

import uuid
from ast import Bytes
from dataclasses import dataclass

import oqs                # Kyber-768 KEM + Dilithium-3 — REQUIRED
import nacl.signing       # Ed25519 — REQUIRED
import nacl.public        # X25519 — REQUIRED
from nacl.exceptions import BadSignatureError
from nacl import bindings
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from AQM_Database.aqm_shared import config
from AQM_Database.aqm_shared.errors import InvalidCoinCategoryError

# ─── Key sizes (bytes) ───

KYBER768_PK_SIZE = 1184
KYBER768_SK_SIZE = 2400
X25519_PK_SIZE = 32
X25519_SK_SIZE = 32
ED25519_SIG_SIZE = 64

@dataclass
class MintedCoinBundle:
    """All artifacts produced by minting a single coin."""
    key_id: str
    coin_category: str
    public_key: bytes
    secret_key: bytes
    signature: bytes


def generate_keypair_silver() -> tuple[bytes, bytes]:
    with oqs.KeyEncapsulation("Kyber768") as kem:
        public_key = kem.generate_keypair()
        secret_key = kem.export_secret_key()
        return bytes(public_key), bytes(secret_key)


def generate_keypair_gold() -> tuple[bytes, bytes]:
    with oqs.KeyEncapsulation("Kyber768") as kem:
        public_key = kem.generate_keypair()
        private_key = kem.export_secret_key()
        return bytes(public_key), bytes(private_key)


class CryptoEngine:
    """Key generation and signing. Real crypto only."""

    def __init__(self):
        self._signing_key = nacl.signing.SigningKey.generate()

    def generate_keypair_bronze(self) -> tuple[bytes, bytes]:
        sk = self._signing_key
        return bytes(sk.public_key), bytes(sk)

    def sign_dilithium(self , data:bytes , signing_key : bytes) -> bytes:
        with oqs.Signature("Dilithium3") as sig:
            signature = sig.sign(data)
            return signature

    def verify_dilithium(self , data:bytes , signature : bytes) -> bool:
        with oqs.Signature("Dilithium3") as sig:
            return sig.verify(data, signature)

    def sign_ed25519(self , data:bytes , signing_key : nacl.signing.SigningKey) -> bytes:
        return signing_key.sign(data)

    def verify_ed25519(self , data:bytes , signature : bytes) -> bool:
        verify_key = nacl.signing.VerifyKey(signature)
        try :
            return bool(verify_key.verify(data))
        except :
            raise BadSignatureError("Signature verification failed")


    def kem_encapsulate(self , public_key : bytes) -> tuple[bytes,bytes]:
        with oqs.KeyEncapsulation("Kyber768") as client:
            ciphertext, shared_secret = client.encap_secret(public_key)
            return ciphertext, shared_secret

    def kem_decapsulate(self , ciphertext:bytes , secret_key : bytes) -> bytes:
        with oqs.KeyEncapsulation("Kyber768") as server:
            server.secret_key = secret_key
            shared_secret = server.decap_secret(ciphertext)
            return shared_secret

    def dh_exchange(self , my_secret : bytes , their_public : bytes) -> bytes:
        shared_secret = nacl.bindings.crypto_scalarmult(my_secret, their_public)
        return shared_secret

    def encrypt_aeed(self , plaintext : bytes , key : bytes , aad:bytes) -> bytes:
        """
        AES-256-GCM encryption.
        Returns: nonce (12 bytes) || ciphertext || tag (16 bytes)
        """
        aesgcm = AESGCM(key)
        nonce = os.urandom(16)
        ct_tag = aesgcm.encrypt(nonce, plaintext)
        return nonce + ct_tag

    def decrypt_aeed(self , ciphertext : bytes , key : bytes , aad:bytes) -> bytes:
        """
        AES-256-GCM decryption.
        Input: nonce (12 bytes) || ciphertext || tag (16 bytes)
        Returns: plaintext
        """
        nonce = ciphertext[:12]
        ct_tag = ciphertext[12:]
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ct_tag)
        return plaintext


    def mint_coin(self , tier:str) -> MintedCoinBundle:
        if()



def mint_coin(engine: CryptoEngine, coin_category: str) -> MintedCoinBundle:
    """Generate a full coin: keypair + signature."""
    if coin_category not in config.VALID_COIN_CATEGORIES:
        raise InvalidCoinCategoryError(coin_category)

    key_id = str(uuid.uuid4())
    pk, sk = engine.generate_keypair(coin_category)
    sig = engine.sign_key(pk, coin_category)

    return MintedCoinBundle(
        key_id=key_id,
        coin_category=coin_category,
        public_key=pk,
        secret_key=sk,
        signature=sig,
    )