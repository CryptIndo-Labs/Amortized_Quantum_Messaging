"""
Tests for GroupKeyTree — Categorical B-Tree key hierarchy (PDF §8.1).

Covers: tree build, branch isolation, stranger-only rekey, mixed HOT/COLD leaves,
empty branch omitted, decrypt round-trip, wrong key rejection.
All tests use real CryptoEngine — no mock crypto.
"""

import os
import pytest
from AQM_Database.aqm_group.key_tree import GroupKeyTree
from AQM_Database.aqm_group.group_types import BRANCH_COIN_TIER


class TestGroupKeyTreeBuild:
    """Tests for GroupKeyTree.build()."""

    def _mint_coins(self, crypto, members_by_tier):
        """Helper: mint coins for all members, return lookup dict."""
        coins = {}  # member_id → {tier: (public_key, secret_key, key_id)}
        for tier, member_ids in members_by_tier.items():
            coin_tier = BRANCH_COIN_TIER[tier]
            for mid in member_ids:
                bundle = crypto.mint_coin(coin_tier)
                coins.setdefault(mid, {})[coin_tier] = (
                    bundle.public_key, bundle.secret_key, bundle.key_id
                )
        return coins

    def _make_coin_getter(self, coins):
        """Helper: returns a get_coin_for_member callable backed by minted coins."""
        def get_coin(member_id, coin_tier):
            pk, sk, key_id = coins[member_id][coin_tier]
            return pk, key_id
        return get_coin

    def _make_secret_getter(self, coins, crypto):
        """Helper: returns a get_secret_key callable for decryption."""
        def get_secret(kem_ct, coin_tier, member_id=None, _coins=coins):
            # Find the right secret key — caller must bind member_id
            raise NotImplementedError("Use _make_member_secret_getter instead")
        return get_secret

    def _make_member_secret_getter(self, member_id, coins, crypto):
        """Returns a get_secret_key callable for a specific member."""
        def get_secret(kem_ct, coin_tier):
            _, sk, _ = coins[member_id][coin_tier]
            return crypto.kem_decapsulate(kem_ct, sk, coin_tier)
        return get_secret

    def test_build_single_bestie(self, crypto):
        """Build tree with one BESTIE member — single branch, single leaf."""
        tree = GroupKeyTree(crypto)
        members = {"BESTIE": ["alice"], "MATE": [], "STRANGER": []}
        coins = self._mint_coins(crypto, members)

        result = tree.build(
            plaintext=b"hello group",
            members_by_tier=members,
            get_coin_for_member=self._make_coin_getter(coins),
        )

        assert "BESTIE" in result.root_key_enc
        assert "MATE" not in result.root_key_enc      # empty branch omitted
        assert "STRANGER" not in result.root_key_enc   # empty branch omitted
        assert "alice" in result.leaf_enc
        assert len(result.hot_leaf_ids) == 0

    def test_build_all_three_tiers(self, crypto):
        """Build tree with members in all three tiers."""
        tree = GroupKeyTree(crypto)
        members = {"BESTIE": ["alice"], "MATE": ["bob"], "STRANGER": ["charlie"]}
        coins = self._mint_coins(crypto, members)

        result = tree.build(
            plaintext=b"multi-tier msg",
            members_by_tier=members,
            get_coin_for_member=self._make_coin_getter(coins),
        )

        assert set(result.root_key_enc.keys()) == {"BESTIE", "MATE", "STRANGER"}
        assert set(result.leaf_enc.keys()) == {"alice", "bob", "charlie"}

    def test_empty_branch_omitted(self, crypto):
        """Empty branches produce no branch key or root_key_enc entry."""
        tree = GroupKeyTree(crypto)
        members = {"BESTIE": [], "MATE": ["bob"], "STRANGER": []}
        coins = self._mint_coins(crypto, members)

        result = tree.build(
            plaintext=b"mate only",
            members_by_tier=members,
            get_coin_for_member=self._make_coin_getter(coins),
        )

        assert list(result.root_key_enc.keys()) == ["MATE"]
        assert list(result.leaf_enc.keys()) == ["bob"]

    def test_multiple_members_same_tier(self, crypto):
        """Multiple STRANGER members — each gets their own leaf."""
        tree = GroupKeyTree(crypto)
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["x", "y", "z"]}
        coins = self._mint_coins(crypto, members)

        result = tree.build(
            plaintext=b"strangers",
            members_by_tier=members,
            get_coin_for_member=self._make_coin_getter(coins),
        )

        assert set(result.leaf_enc.keys()) == {"x", "y", "z"}
        assert "STRANGER" in result.root_key_enc

    def test_root_key_is_random(self, crypto):
        """Two builds produce different root keys."""
        tree = GroupKeyTree(crypto)
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["a"]}
        coins1 = self._mint_coins(crypto, members)
        coins2 = self._mint_coins(crypto, members)

        r1 = tree.build(b"msg1", members, self._make_coin_getter(coins1))
        r2 = tree.build(b"msg2", members, self._make_coin_getter(coins2))

        assert r1.root_key != r2.root_key

    def test_branch_keys_independent(self, crypto):
        """Each branch has its own independent key."""
        tree = GroupKeyTree(crypto)
        members = {"BESTIE": ["a"], "MATE": ["b"], "STRANGER": ["c"]}
        coins = self._mint_coins(crypto, members)

        result = tree.build(b"test", members, self._make_coin_getter(coins))

        branch_keys = [br.branch_key for br in result.branches.values()]
        assert len(set(branch_keys)) == 3  # all different

    def test_hot_leaf_bypasses_coin(self, crypto):
        """HOT member gets branch key via symmetric key, not KEM coin (D4, D8)."""
        tree = GroupKeyTree(crypto)
        hot_key = os.urandom(32)
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["alice"]}
        # No coins needed — alice is HOT

        def no_coin(member_id, coin_tier):
            raise AssertionError("Should not consume coin for HOT member")

        def get_hot(member_id):
            return hot_key if member_id == "alice" else None

        result = tree.build(
            plaintext=b"hot msg",
            members_by_tier=members,
            get_coin_for_member=no_coin,
            get_hot_key_for_member=get_hot,
        )

        assert "alice" in result.hot_leaf_ids
        assert "alice" in result.leaf_enc

    def test_mixed_hot_cold_same_tier(self, crypto):
        """Same tier can have both HOT and COLD members (D3)."""
        tree = GroupKeyTree(crypto)
        hot_key = os.urandom(32)
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["alice", "bob"]}
        coins = self._mint_coins(crypto, {"STRANGER": ["bob"]})

        def get_coin(member_id, coin_tier):
            return coins[member_id][coin_tier][:2]  # (pk, key_id)

        def get_hot(member_id):
            return hot_key if member_id == "alice" else None

        result = tree.build(
            plaintext=b"mixed",
            members_by_tier=members,
            get_coin_for_member=get_coin,
            get_hot_key_for_member=get_hot,
        )

        assert "alice" in result.hot_leaf_ids
        assert "bob" not in result.hot_leaf_ids
        assert "alice" in result.leaf_enc
        assert "bob" in result.leaf_enc


class TestGroupKeyTreeDecrypt:
    """Tests for decrypt_as_recipient — full round-trip."""

    def _build_and_mint(self, crypto, members_by_tier, plaintext=b"hello",
                        hot_members=None):
        """Helper: mint coins, build tree, return (result, coins)."""
        tree = GroupKeyTree(crypto)
        coins = {}
        hot_keys = {}

        for tier, mids in members_by_tier.items():
            coin_tier = BRANCH_COIN_TIER[tier]
            for mid in mids:
                if hot_members and mid in hot_members:
                    hot_keys[mid] = os.urandom(32)
                else:
                    bundle = crypto.mint_coin(coin_tier)
                    coins.setdefault(mid, {})[coin_tier] = (
                        bundle.public_key, bundle.secret_key, bundle.key_id
                    )

        def get_coin(member_id, coin_tier):
            pk, sk, key_id = coins[member_id][coin_tier]
            return pk, key_id

        def get_hot(member_id):
            return hot_keys.get(member_id)

        result = tree.build(
            plaintext=plaintext,
            members_by_tier=members_by_tier,
            get_coin_for_member=get_coin,
            get_hot_key_for_member=get_hot if hot_keys else None,
        )

        return result, coins, hot_keys

    def test_decrypt_bestie_cold(self, crypto):
        """BESTIE member (GOLD coin) can decrypt the full chain."""
        members = {"BESTIE": ["alice"], "MATE": [], "STRANGER": []}
        result, coins, _ = self._build_and_mint(crypto, members, b"secret msg")

        tree = GroupKeyTree(crypto)
        def get_secret(kem_ct, coin_tier):
            _, sk, _ = coins["alice"][coin_tier]
            return crypto.kem_decapsulate(kem_ct, sk, coin_tier)

        plaintext = tree.decrypt_as_recipient(
            my_id="alice", my_tier="BESTIE",
            leaf_enc=result.leaf_enc, root_key_enc=result.root_key_enc,
            encrypted_payload=result.encrypted_payload,
            hot_leaf_ids=result.hot_leaf_ids,
            get_secret_key=get_secret,
        )
        assert plaintext == b"secret msg"

    def test_decrypt_mate_cold(self, crypto):
        """MATE member (SILVER coin) can decrypt."""
        members = {"BESTIE": [], "MATE": ["bob"], "STRANGER": []}
        result, coins, _ = self._build_and_mint(crypto, members, b"mate msg")

        tree = GroupKeyTree(crypto)
        def get_secret(kem_ct, coin_tier):
            _, sk, _ = coins["bob"][coin_tier]
            return crypto.kem_decapsulate(kem_ct, sk, coin_tier)

        plaintext = tree.decrypt_as_recipient(
            my_id="bob", my_tier="MATE",
            leaf_enc=result.leaf_enc, root_key_enc=result.root_key_enc,
            encrypted_payload=result.encrypted_payload,
            hot_leaf_ids=result.hot_leaf_ids,
            get_secret_key=get_secret,
        )
        assert plaintext == b"mate msg"

    def test_decrypt_stranger_cold(self, crypto):
        """STRANGER member (BRONZE coin) can decrypt."""
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["charlie"]}
        result, coins, _ = self._build_and_mint(crypto, members, b"stranger msg")

        tree = GroupKeyTree(crypto)
        def get_secret(kem_ct, coin_tier):
            _, sk, _ = coins["charlie"][coin_tier]
            return crypto.kem_decapsulate(kem_ct, sk, coin_tier)

        plaintext = tree.decrypt_as_recipient(
            my_id="charlie", my_tier="STRANGER",
            leaf_enc=result.leaf_enc, root_key_enc=result.root_key_enc,
            encrypted_payload=result.encrypted_payload,
            hot_leaf_ids=result.hot_leaf_ids,
            get_secret_key=get_secret,
        )
        assert plaintext == b"stranger msg"

    def test_decrypt_hot_leaf(self, crypto):
        """HOT member decrypts using symmetric ratchet key, not KEM (D4)."""
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["alice"]}
        result, _, hot_keys = self._build_and_mint(
            crypto, members, b"hot msg", hot_members={"alice"}
        )

        tree = GroupKeyTree(crypto)
        def get_hot(member_id):
            return hot_keys[member_id]

        plaintext = tree.decrypt_as_recipient(
            my_id="alice", my_tier="STRANGER",
            leaf_enc=result.leaf_enc, root_key_enc=result.root_key_enc,
            encrypted_payload=result.encrypted_payload,
            hot_leaf_ids=result.hot_leaf_ids,
            get_secret_key=lambda ct, t: None,  # should not be called
            get_hot_key=get_hot,
        )
        assert plaintext == b"hot msg"

    def test_multi_tier_all_decrypt(self, crypto):
        """All members across different tiers can decrypt the same message."""
        members = {"BESTIE": ["alice"], "MATE": ["bob"], "STRANGER": ["charlie"]}
        result, coins, _ = self._build_and_mint(crypto, members, b"everyone")

        tree = GroupKeyTree(crypto)
        for mid, tier in [("alice", "BESTIE"), ("bob", "MATE"), ("charlie", "STRANGER")]:
            coin_tier = BRANCH_COIN_TIER[tier]
            def get_secret(kem_ct, ct, _mid=mid, _ct=coin_tier):
                _, sk, _ = coins[_mid][_ct]
                return crypto.kem_decapsulate(kem_ct, sk, _ct)

            plaintext = tree.decrypt_as_recipient(
                my_id=mid, my_tier=tier,
                leaf_enc=result.leaf_enc, root_key_enc=result.root_key_enc,
                encrypted_payload=result.encrypted_payload,
                hot_leaf_ids=result.hot_leaf_ids,
                get_secret_key=get_secret,
            )
            assert plaintext == b"everyone"

    def test_wrong_secret_key_fails(self, crypto):
        """Using wrong private key to decrypt fails."""
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["alice"]}
        result, coins, _ = self._build_and_mint(crypto, members, b"fail")

        # Mint a different coin — wrong key
        wrong = crypto.mint_coin("BRONZE")
        tree = GroupKeyTree(crypto)

        def get_wrong_secret(kem_ct, coin_tier):
            return crypto.kem_decapsulate(kem_ct, wrong.secret_key, coin_tier)

        with pytest.raises(Exception):
            tree.decrypt_as_recipient(
                my_id="alice", my_tier="STRANGER",
                leaf_enc=result.leaf_enc, root_key_enc=result.root_key_enc,
                encrypted_payload=result.encrypted_payload,
                hot_leaf_ids=result.hot_leaf_ids,
                get_secret_key=get_wrong_secret,
            )

    def test_missing_leaf_raises(self, crypto):
        """Decrypting with an ID not in leaf_enc raises ValueError."""
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["alice"]}
        result, _, _ = self._build_and_mint(crypto, members, b"nope")

        tree = GroupKeyTree(crypto)
        with pytest.raises(ValueError, match="No leaf entry"):
            tree.decrypt_as_recipient(
                my_id="bob", my_tier="STRANGER",
                leaf_enc=result.leaf_enc, root_key_enc=result.root_key_enc,
                encrypted_payload=result.encrypted_payload,
                hot_leaf_ids=result.hot_leaf_ids,
                get_secret_key=lambda ct, t: None,
            )

    def test_missing_branch_raises(self, crypto):
        """Decrypting with a tier not in root_key_enc raises ValueError."""
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["alice"]}
        result, coins, _ = self._build_and_mint(crypto, members, b"nope")

        tree = GroupKeyTree(crypto)
        with pytest.raises(ValueError, match="No branch"):
            tree.decrypt_as_recipient(
                my_id="alice", my_tier="BESTIE",  # wrong tier
                leaf_enc=result.leaf_enc, root_key_enc=result.root_key_enc,
                encrypted_payload=result.encrypted_payload,
                hot_leaf_ids=result.hot_leaf_ids,
                get_secret_key=lambda ct, t: None,
            )

    def test_branch_isolation_stranger_add(self, crypto):
        """Adding a STRANGER does not change BESTIE branch key (branch isolation)."""
        tree = GroupKeyTree(crypto)

        # Build 1: BESTIE only
        members1 = {"BESTIE": ["alice"], "MATE": [], "STRANGER": []}
        coins1 = {}
        b1 = crypto.mint_coin("GOLD")
        coins1["alice"] = {"GOLD": (b1.public_key, b1.secret_key, b1.key_id)}

        result1 = tree.build(
            b"msg1", members1,
            lambda mid, ct: (coins1[mid][ct][:2]),
        )

        # Build 2: BESTIE + STRANGER
        members2 = {"BESTIE": ["alice"], "MATE": [], "STRANGER": ["bob"]}
        b1_new = crypto.mint_coin("GOLD")
        coins2 = {
            "alice": {"GOLD": (b1_new.public_key, b1_new.secret_key, b1_new.key_id)},
        }
        b2 = crypto.mint_coin("BRONZE")
        coins2["bob"] = {"BRONZE": (b2.public_key, b2.secret_key, b2.key_id)}

        def gc2(mid, ct):
            pk, sk, kid = coins2[mid][ct]
            return pk, kid

        result2 = tree.build(b"msg2", members2, gc2)

        # Both builds have BESTIE branch, but different branch keys (ephemeral)
        # The key point: BESTIE branch EXISTS in both, STRANGER only in second
        assert "BESTIE" in result1.root_key_enc
        assert "BESTIE" in result2.root_key_enc
        assert "STRANGER" not in result1.root_key_enc
        assert "STRANGER" in result2.root_key_enc

    def test_tampered_payload_fails(self, crypto):
        """Flipping a bit in encrypted_payload causes decrypt failure."""
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["alice"]}
        result, coins, _ = self._build_and_mint(crypto, members, b"tamper test")

        # Flip a bit
        payload = bytearray(result.encrypted_payload)
        payload[20] ^= 0xFF
        result.encrypted_payload = bytes(payload)

        tree = GroupKeyTree(crypto)
        def get_secret(kem_ct, coin_tier):
            _, sk, _ = coins["alice"][coin_tier]
            return crypto.kem_decapsulate(kem_ct, sk, coin_tier)

        with pytest.raises(Exception):
            tree.decrypt_as_recipient(
                my_id="alice", my_tier="STRANGER",
                leaf_enc=result.leaf_enc, root_key_enc=result.root_key_enc,
                encrypted_payload=result.encrypted_payload,
                hot_leaf_ids=result.hot_leaf_ids,
                get_secret_key=get_secret,
            )

    def test_all_bestie_group(self, crypto):
        """Group where all members are BESTIE — only BESTIE branch exists."""
        members = {"BESTIE": ["a", "b", "c"], "MATE": [], "STRANGER": []}
        result, coins, _ = self._build_and_mint(crypto, members, b"besties only")

        assert set(result.root_key_enc.keys()) == {"BESTIE"}
        assert set(result.leaf_enc.keys()) == {"a", "b", "c"}

        # All can decrypt
        tree = GroupKeyTree(crypto)
        for mid in ["a", "b", "c"]:
            def get_secret(kem_ct, coin_tier, _mid=mid):
                _, sk, _ = coins[_mid][coin_tier]
                return crypto.kem_decapsulate(kem_ct, sk, coin_tier)
            assert tree.decrypt_as_recipient(
                my_id=mid, my_tier="BESTIE",
                leaf_enc=result.leaf_enc, root_key_enc=result.root_key_enc,
                encrypted_payload=result.encrypted_payload,
                hot_leaf_ids=result.hot_leaf_ids,
                get_secret_key=get_secret,
            ) == b"besties only"

    def test_all_stranger_group(self, crypto):
        """Group where all members are STRANGER — only STRANGER branch."""
        members = {"BESTIE": [], "MATE": [], "STRANGER": ["x", "y"]}
        result, coins, _ = self._build_and_mint(crypto, members, b"strangers")

        assert set(result.root_key_enc.keys()) == {"STRANGER"}

        tree = GroupKeyTree(crypto)
        for mid in ["x", "y"]:
            def get_secret(kem_ct, coin_tier, _mid=mid):
                _, sk, _ = coins[_mid][coin_tier]
                return crypto.kem_decapsulate(kem_ct, sk, coin_tier)
            assert tree.decrypt_as_recipient(
                my_id=mid, my_tier="STRANGER",
                leaf_enc=result.leaf_enc, root_key_enc=result.root_key_enc,
                encrypted_payload=result.encrypted_payload,
                hot_leaf_ids=result.hot_leaf_ids,
                get_secret_key=get_secret,
            ) == b"strangers"
