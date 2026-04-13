"""
Tests for group parcel builder and parser (PDF §8.1).

Covers: build+parse roundtrip, tamper detection, signature roundtrip,
hot_leaf_ids roundtrip, header correctness (D2), empty branch exclusion.
All tests use real CryptoEngine — no mock crypto.
"""

import os
import json
import time
import pytest

from AQM_Database.aqm_group.group_parcel import (
    build_parcel, parse_parcel, make_header, make_inner,
)
from AQM_Database.aqm_group.group_types import GroupParcelHeader, GroupParcelInner
from AQM_Database.aqm_group.key_tree import GroupKeyTree
from AQM_Database.aqm_group.group_types import BRANCH_COIN_TIER


class TestParcelRoundTrip:
    """Build → serialize → deserialize → verify tests."""

    def test_header_roundtrip(self):
        """Header fields survive serialization."""
        header = make_header("g1", "sender", ["alice", "bob"])
        inner = make_inner(
            root_key_enc={"STRANGER": b"enc_root"},
            leaf_enc={"alice": b"leaf_a", "bob": b"leaf_b"},
            hot_leaf_ids=[],
            encrypted_payload=b"payload",
        )
        raw = build_parcel(header, inner)
        h2, _ = parse_parcel(raw)

        assert h2.version == 2
        assert h2.group_id == "g1"
        assert h2.sender_id == "sender"
        assert set(h2.recipient_ids) == {"alice", "bob"}

    def test_inner_roundtrip(self):
        """Inner B-Tree fields survive serialization."""
        inner = make_inner(
            root_key_enc={"BESTIE": os.urandom(48), "STRANGER": os.urandom(48)},
            leaf_enc={"a": os.urandom(64), "b": os.urandom(64)},
            hot_leaf_ids=["a"],
            encrypted_payload=os.urandom(128),
            sender_signature=os.urandom(64),
        )
        header = make_header("g1", "sender", ["a", "b"])
        raw = build_parcel(header, inner)
        _, i2 = parse_parcel(raw)

        assert set(i2.root_key_enc.keys()) == {"BESTIE", "STRANGER"}
        assert i2.root_key_enc["BESTIE"] == inner.root_key_enc["BESTIE"]
        assert i2.leaf_enc["a"] == inner.leaf_enc["a"]
        assert i2.hot_leaf_ids == ["a"]
        assert i2.hot_leaf_counters == {}
        assert i2.encrypted_payload == inner.encrypted_payload
        assert i2.sender_signature == inner.sender_signature

    def test_hot_leaf_counters_roundtrip(self):
        """hot_leaf_counters dict survives serialization."""
        inner = make_inner(
            root_key_enc={"STRANGER": b"r"},
            leaf_enc={"a": b"l"},
            hot_leaf_ids=["a"],
            hot_leaf_counters={"a": 2},
            encrypted_payload=b"p",
        )
        header = make_header("g1", "s", ["a"])
        _, i2 = parse_parcel(build_parcel(header, inner))
        assert i2.hot_leaf_counters == {"a": 2}

    def test_hot_leaf_ids_roundtrip(self):
        """hot_leaf_ids list survives serialization."""
        inner = make_inner(
            root_key_enc={"MATE": b"r"},
            leaf_enc={"x": b"lx", "y": b"ly", "z": b"lz"},
            hot_leaf_ids=["x", "z"],
            encrypted_payload=b"p",
        )
        header = make_header("g1", "s", ["x", "y", "z"])
        _, i2 = parse_parcel(build_parcel(header, inner))
        assert i2.hot_leaf_ids == ["x", "z"]

    def test_empty_hot_leaf_ids(self):
        """Empty hot_leaf_ids survives."""
        inner = make_inner(
            root_key_enc={"STRANGER": b"r"}, leaf_enc={"a": b"l"},
            hot_leaf_ids=[], encrypted_payload=b"p",
        )
        header = make_header("g1", "s", ["a"])
        _, i2 = parse_parcel(build_parcel(header, inner))
        assert i2.hot_leaf_ids == []

    def test_sender_excluded_from_recipients(self):
        """D2: sender is not in recipient_ids."""
        header = make_header("g1", "sender", ["alice", "bob"])
        assert "sender" not in header.recipient_ids

    def test_version_is_2(self):
        """Group parcels use version 2 to distinguish from 1:1."""
        header = make_header("g1", "s", ["a"])
        assert header.version == 2

    def test_timestamp_is_set(self):
        """make_header sets a timestamp."""
        before = time.time()
        header = make_header("g1", "s", ["a"])
        after = time.time()
        assert before <= header.timestamp <= after


class TestParcelWireFormat:
    """Wire format and validation tests."""

    def test_wire_is_valid_json(self):
        """Serialized parcel is valid JSON."""
        inner = make_inner(
            root_key_enc={"STRANGER": b"r"}, leaf_enc={"a": b"l"},
            hot_leaf_ids=[], encrypted_payload=b"p",
        )
        header = make_header("g1", "s", ["a"])
        raw = build_parcel(header, inner)
        data = json.loads(raw)
        assert data["msg_type"] == "GROUP_PARCEL"

    def test_wrong_msg_type_raises(self):
        """Parsing a non-GROUP_PARCEL message raises ValueError."""
        raw = json.dumps({"msg_type": "PARCEL", "header": {}, "inner": {}})
        with pytest.raises(ValueError, match="Expected GROUP_PARCEL"):
            parse_parcel(raw)

    def test_invalid_json_raises(self):
        """Parsing invalid JSON raises."""
        with pytest.raises(json.JSONDecodeError):
            parse_parcel("not json")

    def test_binary_blobs_base64_encoded(self):
        """Binary fields in inner are base64-encoded in wire format."""
        payload = os.urandom(100)
        inner = make_inner(
            root_key_enc={"BESTIE": os.urandom(48)},
            leaf_enc={"a": os.urandom(64)},
            hot_leaf_ids=[],
            encrypted_payload=payload,
        )
        header = make_header("g1", "s", ["a"])
        raw = build_parcel(header, inner)
        data = json.loads(raw)

        # Verify base64 strings are present (not raw bytes)
        assert isinstance(data["inner"]["encrypted_payload"], str)
        assert isinstance(data["inner"]["leaf_enc"]["a"], str)


class TestParcelWithKeyTree:
    """Integration: KeyTree build → parcel serialize → parse → decrypt."""

    def test_full_roundtrip_cold(self, crypto):
        """Full cycle: build B-Tree → serialize parcel → parse → decrypt (COLD)."""
        tree = GroupKeyTree(crypto)
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["alice", "bob"]}

        # Mint coins
        coins = {}
        for mid in ["alice", "bob"]:
            bundle = crypto.mint_coin("BRONZE")
            coins[mid] = {"BRONZE": (bundle.public_key, bundle.secret_key, bundle.key_id)}

        def get_coin(mid, ct):
            pk, sk, kid = coins[mid][ct]
            return pk, kid

        # Build tree
        result = tree.build(b"group message", members, get_coin)

        # Build parcel
        header = make_header("g1", "sender", ["alice", "bob"])
        inner = make_inner(
            root_key_enc=result.root_key_enc,
            leaf_enc=result.leaf_enc,
            hot_leaf_ids=result.hot_leaf_ids,
            encrypted_payload=result.encrypted_payload,
        )

        # Serialize → parse
        raw = build_parcel(header, inner)
        h2, i2 = parse_parcel(raw)

        # Decrypt as alice
        def get_secret(kem_ct, coin_tier):
            _, sk, _ = coins["alice"][coin_tier]
            return crypto.kem_decapsulate(kem_ct, sk, coin_tier)

        plaintext = tree.decrypt_as_recipient(
            my_id="alice", my_tier="STRANGER",
            leaf_enc=i2.leaf_enc, root_key_enc=i2.root_key_enc,
            encrypted_payload=i2.encrypted_payload,
            hot_leaf_ids=i2.hot_leaf_ids,
            get_secret_key=get_secret,
        )
        assert plaintext == b"group message"

    def test_full_roundtrip_hot(self, crypto):
        """Full cycle with HOT leaf — no coin consumed."""
        tree = GroupKeyTree(crypto)
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["alice"]}
        hot_key = os.urandom(32)

        def no_coin(mid, ct):
            raise AssertionError("No coin for HOT")

        def get_hot(mid):
            return hot_key if mid == "alice" else None

        result = tree.build(b"hot group msg", members, no_coin, get_hot)

        header = make_header("g1", "sender", ["alice"])
        inner = make_inner(
            root_key_enc=result.root_key_enc,
            leaf_enc=result.leaf_enc,
            hot_leaf_ids=result.hot_leaf_ids,
            encrypted_payload=result.encrypted_payload,
        )

        raw = build_parcel(header, inner)
        _, i2 = parse_parcel(raw)

        plaintext = tree.decrypt_as_recipient(
            my_id="alice", my_tier="STRANGER",
            leaf_enc=i2.leaf_enc, root_key_enc=i2.root_key_enc,
            encrypted_payload=i2.encrypted_payload,
            hot_leaf_ids=i2.hot_leaf_ids,
            get_secret_key=lambda ct, t: None,
            get_hot_key=lambda mid: hot_key,
        )
        assert plaintext == b"hot group msg"

    def test_tampered_inner_fails(self, crypto):
        """Flipping bits in serialized inner causes decrypt failure."""
        tree = GroupKeyTree(crypto)
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["alice"]}
        bundle = crypto.mint_coin("BRONZE")
        coins = {"alice": {"BRONZE": (bundle.public_key, bundle.secret_key, bundle.key_id)}}

        def get_coin(mid, ct):
            return coins[mid][ct][0], coins[mid][ct][2]

        result = tree.build(b"tamper test", members, get_coin)

        header = make_header("g1", "sender", ["alice"])
        inner = make_inner(
            root_key_enc=result.root_key_enc,
            leaf_enc=result.leaf_enc,
            hot_leaf_ids=result.hot_leaf_ids,
            encrypted_payload=result.encrypted_payload,
        )

        raw = build_parcel(header, inner)
        # Tamper with the wire format
        data = json.loads(raw)
        # Corrupt the encrypted payload
        import base64
        bad_payload = bytearray(base64.b64decode(data["inner"]["encrypted_payload"]))
        bad_payload[10] ^= 0xFF
        data["inner"]["encrypted_payload"] = base64.b64encode(bytes(bad_payload)).decode()
        raw = json.dumps(data)

        _, i2 = parse_parcel(raw)

        def get_secret(kem_ct, coin_tier):
            return crypto.kem_decapsulate(kem_ct, bundle.secret_key, coin_tier)

        with pytest.raises(Exception):
            tree.decrypt_as_recipient(
                my_id="alice", my_tier="STRANGER",
                leaf_enc=i2.leaf_enc, root_key_enc=i2.root_key_enc,
                encrypted_payload=i2.encrypted_payload,
                hot_leaf_ids=i2.hot_leaf_ids,
                get_secret_key=get_secret,
            )
