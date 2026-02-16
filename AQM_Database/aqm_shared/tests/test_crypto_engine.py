"""Tests for CryptoEngine, MintedCoinBundle, and mint_coin()."""

import pytest

from AQM_Database.aqm_shared.crypto_engine import (
    CryptoEngine,
    MintedCoinBundle,
    mint_coin,
    KYBER768_PK_SIZE,
    KYBER768_SK_SIZE,
    X25519_PK_SIZE,
    X25519_SK_SIZE,
    ED25519_SIG_SIZE,
    DILITHIUM3_SIG_SIZE,
)
from AQM_Database.aqm_shared.errors import InvalidCoinCategoryError


@pytest.fixture
def engine():
    return CryptoEngine()


# ─── Backend detection ───

def test_backend_is_string(engine):
    assert engine.backend in ("liboqs+pynacl", "pynacl-only", "urandom-mock")


# ─── Key generation — sizes ───

def test_gold_keypair_sizes(engine):
    pk, sk = engine.generate_keypair("GOLD")
    assert len(pk) == KYBER768_PK_SIZE
    assert len(sk) == KYBER768_SK_SIZE


def test_silver_keypair_sizes(engine):
    pk, sk = engine.generate_keypair("SILVER")
    assert len(pk) == KYBER768_PK_SIZE
    assert len(sk) == KYBER768_SK_SIZE


def test_bronze_keypair_sizes(engine):
    pk, sk = engine.generate_keypair("BRONZE")
    assert len(pk) == X25519_PK_SIZE
    assert len(sk) == X25519_SK_SIZE


# ─── Key generation — types ───

def test_keypair_returns_bytes(engine):
    pk, sk = engine.generate_keypair("GOLD")
    assert isinstance(pk, bytes)
    assert isinstance(sk, bytes)


# ─── Key generation — uniqueness ───

def test_gold_keypairs_are_unique(engine):
    pk1, sk1 = engine.generate_keypair("GOLD")
    pk2, sk2 = engine.generate_keypair("GOLD")
    assert pk1 != pk2
    assert sk1 != sk2


def test_bronze_keypairs_are_unique(engine):
    pk1, sk1 = engine.generate_keypair("BRONZE")
    pk2, sk2 = engine.generate_keypair("BRONZE")
    assert pk1 != pk2
    assert sk1 != sk2


# ─── Key generation — validation ───

def test_generate_keypair_rejects_invalid_category(engine):
    with pytest.raises(InvalidCoinCategoryError):
        engine.generate_keypair("PLATINUM")


# ─── Signing ───

def test_sign_key_gold_dilithium_size(engine):
    pk, _ = engine.generate_keypair("GOLD")
    sig = engine.sign_key(pk, "GOLD")
    assert isinstance(sig, bytes)
    assert len(sig) == DILITHIUM3_SIG_SIZE


def test_sign_key_silver_ed25519_size(engine):
    pk, _ = engine.generate_keypair("SILVER")
    sig = engine.sign_key(pk, "SILVER")
    assert isinstance(sig, bytes)
    if engine.backend == "urandom-mock":
        assert len(sig) == 32  # SHA-256 fallback
    else:
        assert len(sig) == ED25519_SIG_SIZE


def test_sign_key_bronze_deterministic(engine):
    pk, _ = engine.generate_keypair("BRONZE")
    sig1 = engine.sign_key(pk, "BRONZE")
    sig2 = engine.sign_key(pk, "BRONZE")
    # Ed25519 is deterministic; urandom-mock (SHA-256) also is
    assert sig1 == sig2


def test_sign_key_rejects_invalid_category(engine):
    with pytest.raises(InvalidCoinCategoryError):
        engine.sign_key(b"fake", "DIAMOND")


# ─── mint_coin() ───

def test_mint_coin_returns_bundle(engine):
    bundle = mint_coin(engine, "GOLD")
    assert isinstance(bundle, MintedCoinBundle)
    assert bundle.coin_category == "GOLD"
    assert len(bundle.key_id) == 36  # UUID format
    assert len(bundle.public_key) == KYBER768_PK_SIZE
    assert len(bundle.encryption_iv) == 12
    assert len(bundle.auth_tag) == 16


def test_mint_coin_rejects_invalid_category(engine):
    with pytest.raises(InvalidCoinCategoryError):
        mint_coin(engine, "INVALID")


def test_mint_coin_bundles_are_unique(engine):
    b1 = mint_coin(engine, "SILVER")
    b2 = mint_coin(engine, "SILVER")
    assert b1.key_id != b2.key_id
    assert b1.public_key != b2.public_key
