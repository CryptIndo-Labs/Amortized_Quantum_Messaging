"""Tests for chat.protocol — message envelope, encrypt/decrypt, serialization."""

import base64
import json
import os
import uuid

from AQM_Database.chat.protocol import (
    ChatMessage,
    channel_for,
    simulate_encrypt,
    simulate_decrypt,
    build_message,
    serialize,
    deserialize,
    CHANNEL_PREFIX,
)


# ─── channel_for ───

def test_channel_for_format():
    uid = "abc-123"
    assert channel_for(uid) == f"{CHANNEL_PREFIX}:{uid}"


def test_channel_for_uuid():
    uid = str(uuid.uuid4())
    ch = channel_for(uid)
    assert ch.startswith(CHANNEL_PREFIX + ":")
    assert uid in ch


# ─── simulate_encrypt / simulate_decrypt ───

def test_encrypt_decrypt_roundtrip():
    pk = os.urandom(32)
    plaintext = "Hello, quantum world!"
    ct = simulate_encrypt(plaintext, pk)
    decrypted, valid = simulate_decrypt(ct, pk)
    assert decrypted == plaintext
    assert valid is True


def test_decrypt_wrong_key_fails_verification():
    pk1 = os.urandom(32)
    pk2 = os.urandom(32)
    ct = simulate_encrypt("secret", pk1)
    decrypted, valid = simulate_decrypt(ct, pk2)
    # Plaintext is still readable (simulation), but tag check fails
    assert decrypted == "secret"
    assert valid is False


def test_encrypt_produces_tag_plus_plaintext():
    pk = os.urandom(64)
    ct = simulate_encrypt("test", pk)
    # 32-byte SHA-256 tag + plaintext bytes
    assert len(ct) == 32 + len("test".encode("utf-8"))


def test_decrypt_short_ciphertext():
    plaintext, valid = simulate_decrypt(b"short", os.urandom(32))
    assert plaintext == ""
    assert valid is False


def test_encrypt_empty_string():
    pk = os.urandom(32)
    ct = simulate_encrypt("", pk)
    decrypted, valid = simulate_decrypt(ct, pk)
    assert decrypted == ""
    assert valid is True


def test_encrypt_unicode():
    pk = os.urandom(32)
    text = "quantum"
    ct = simulate_encrypt(text, pk)
    decrypted, valid = simulate_decrypt(ct, pk)
    assert decrypted == text
    assert valid is True


# ─── build_message ───

def test_build_message_fields():
    pk = os.urandom(1184)
    msg = build_message(
        sender_id="alice-uuid",
        recipient_id="bob-uuid",
        coin_tier="GOLD",
        key_id="key-001",
        public_key=pk,
        plaintext="Hello Bob!",
        device_context="Home WiFi",
    )
    assert isinstance(msg, ChatMessage)
    assert msg.sender_id == "alice-uuid"
    assert msg.recipient_id == "bob-uuid"
    assert msg.coin_tier == "GOLD"
    assert msg.key_id == "key-001"
    assert msg.device_context == "Home WiFi"
    # Verify base64 fields are decodable
    assert base64.b64decode(msg.public_key_b64) == pk
    ct = base64.b64decode(msg.ciphertext_b64)
    assert len(ct) > 32


# ─── serialize / deserialize ───

def test_serialize_deserialize_roundtrip():
    pk = os.urandom(32)
    msg = build_message(
        sender_id="alice",
        recipient_id="bob",
        coin_tier="SILVER",
        key_id="key-002",
        public_key=pk,
        plaintext="test message",
    )
    data = serialize(msg)
    assert isinstance(data, str)
    parsed = json.loads(data)
    assert parsed["sender_id"] == "alice"

    restored = deserialize(data)
    assert restored.msg_id == msg.msg_id
    assert restored.sender_id == msg.sender_id
    assert restored.ciphertext_b64 == msg.ciphertext_b64
    assert restored.plaintext_hash == msg.plaintext_hash


def test_deserialize_preserves_all_fields():
    pk = os.urandom(32)
    msg = build_message(
        sender_id="s",
        recipient_id="r",
        coin_tier="BRONZE",
        key_id="k",
        public_key=pk,
        plaintext="p",
        device_context="ctx",
    )
    restored = deserialize(serialize(msg))
    assert restored.coin_tier == "BRONZE"
    assert restored.device_context == "ctx"
    assert restored.timestamp == msg.timestamp
