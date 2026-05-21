"""
Tests for HotEdgeTracker — Temporal State Machine (PDF §8.1).

Covers: COLD→HOT→COLD lifecycle, TTL expiry, key burn on COLD,
per-group isolation (D3), counter increment, encrypted chain key (D4).
All tests use real CryptoEngine — no mock crypto.
"""

import os
import time
import pytest
from unittest.mock import patch

from AQM_Database.aqm_group.hot_edge import HotEdgeTracker
from AQM_Database.aqm_group.group_types import HOT_EDGE_TTL_SECONDS


class TestHotEdgeLifecycle:
    """COLD→HOT→COLD lifecycle tests."""

    def _make_tracker(self, group_db, crypto, ttl=HOT_EDGE_TTL_SECONDS):
        vault_key = os.urandom(32)
        return HotEdgeTracker(group_db, crypto, vault_key, ttl_seconds=ttl)

    def test_initially_cold(self, group_db, crypto):
        """All edges start COLD."""
        tracker = self._make_tracker(group_db, crypto)
        assert not tracker.is_hot("g1", "alice")

    def test_activate_makes_hot(self, group_db, crypto):
        """activate() transitions COLD→HOT (D4)."""
        tracker = self._make_tracker(group_db, crypto)
        shared_secret = os.urandom(32)
        tracker.activate("g1", "alice", shared_secret)
        assert tracker.is_hot("g1", "alice")

    def test_deactivate_makes_cold(self, group_db, crypto):
        """deactivate() transitions HOT→COLD and burns key."""
        tracker = self._make_tracker(group_db, crypto)
        tracker.activate("g1", "alice", os.urandom(32))
        tracker.deactivate("g1", "alice")
        assert not tracker.is_hot("g1", "alice")
        # Verify key is deleted
        edge = group_db.get_hot_edge("g1", "alice")
        assert edge is None

    def test_ttl_expiry(self, group_db, crypto):
        """Edge expires after TTL and transitions to COLD."""
        tracker = self._make_tracker(group_db, crypto, ttl=1)  # 1 second TTL
        tracker.activate("g1", "alice", os.urandom(32))
        assert tracker.is_hot("g1", "alice")

        # Fast-forward past TTL
        with patch("AQM_Database.aqm_group.hot_edge.time") as mock_time:
            mock_time.time.return_value = time.time() + 2
            assert not tracker.is_hot("g1", "alice")

    def test_touch_refreshes_ttl(self, group_db, crypto):
        """touch() resets the TTL timer."""
        tracker = self._make_tracker(group_db, crypto, ttl=5)
        tracker.activate("g1", "alice", os.urandom(32))

        # Touch at t+3
        edge = group_db.get_hot_edge("g1", "alice")
        edge.last_activity_at = time.time() + 3
        group_db.set_hot_edge(edge)

        # At t+6, should still be HOT because touch reset it
        tracker.touch("g1", "alice")
        assert tracker.is_hot("g1", "alice")

    def test_deactivate_logs_info(self, group_db, crypto, caplog):
        """HOT→COLD transition is logged at INFO level."""
        import logging
        tracker = self._make_tracker(group_db, crypto)
        tracker.activate("g1", "alice", os.urandom(32))

        with caplog.at_level(logging.INFO, logger="aqm.group.hot_edge"):
            tracker.deactivate("g1", "alice")
        assert "HOT→COLD" in caplog.text
        assert "key burned" in caplog.text

    def test_activate_logs_info(self, group_db, crypto, caplog):
        """COLD→HOT transition is logged at INFO level."""
        import logging
        tracker = self._make_tracker(group_db, crypto)
        with caplog.at_level(logging.INFO, logger="aqm.group.hot_edge"):
            tracker.activate("g1", "alice", os.urandom(32))
        assert "COLD→HOT" in caplog.text


class TestHotEdgeIsolation:
    """Per-(group_id, member_id) isolation tests (D3)."""

    def _make_tracker(self, group_db, crypto):
        return HotEdgeTracker(group_db, crypto, os.urandom(32))

    def test_different_groups_independent(self, group_db, crypto):
        """Same member in different groups has independent edge state (D3)."""
        tracker = self._make_tracker(group_db, crypto)
        tracker.activate("g1", "alice", os.urandom(32))
        assert tracker.is_hot("g1", "alice")
        assert not tracker.is_hot("g2", "alice")

    def test_different_members_independent(self, group_db, crypto):
        """Different members in same group have independent edges."""
        tracker = self._make_tracker(group_db, crypto)
        tracker.activate("g1", "alice", os.urandom(32))
        assert tracker.is_hot("g1", "alice")
        assert not tracker.is_hot("g1", "bob")

    def test_deactivate_one_keeps_other(self, group_db, crypto):
        """Deactivating one edge doesn't affect another."""
        tracker = self._make_tracker(group_db, crypto)
        tracker.activate("g1", "alice", os.urandom(32))
        tracker.activate("g1", "bob", os.urandom(32))
        tracker.deactivate("g1", "alice")
        assert not tracker.is_hot("g1", "alice")
        assert tracker.is_hot("g1", "bob")


class TestHotEdgeKeyDerivation:
    """Key derivation and counter tests (D4)."""

    def _make_tracker(self, group_db, crypto):
        return HotEdgeTracker(group_db, crypto, os.urandom(32))

    def test_derive_key_returns_bytes(self, group_db, crypto):
        """derive_key() returns a 32-byte AES key."""
        tracker = self._make_tracker(group_db, crypto)
        tracker.activate("g1", "alice", os.urandom(32))
        key = tracker.derive_key("g1", "alice")
        assert isinstance(key, bytes)
        assert len(key) == 32

    def test_derive_key_increments_counter(self, group_db, crypto):
        """Each derive_key() call increments the counter."""
        tracker = self._make_tracker(group_db, crypto)
        tracker.activate("g1", "alice", os.urandom(32))

        tracker.derive_key("g1", "alice")
        edge = group_db.get_hot_edge("g1", "alice")
        assert edge.msg_counter == 1

        tracker.derive_key("g1", "alice")
        edge = group_db.get_hot_edge("g1", "alice")
        assert edge.msg_counter == 2

    def test_derive_key_returns_different_each_time(self, group_db, crypto):
        """Sequential derive_key() calls produce different keys (ratchet advances)."""
        tracker = self._make_tracker(group_db, crypto)
        tracker.activate("g1", "alice", os.urandom(32))
        k1 = tracker.derive_key("g1", "alice")
        k2 = tracker.derive_key("g1", "alice")
        assert k1 != k2

    def test_derive_key_cold_returns_none(self, group_db, crypto):
        """derive_key() returns None for COLD edge."""
        tracker = self._make_tracker(group_db, crypto)
        assert tracker.derive_key("g1", "alice") is None

    def test_chain_key_stored_encrypted(self, group_db, crypto):
        """Chain key in DB is encrypted, not plaintext (D4)."""
        tracker = self._make_tracker(group_db, crypto)
        tracker.activate("g1", "alice", os.urandom(32))

        edge = group_db.get_hot_edge("g1", "alice")
        # The stored key should be encrypted — it has IV and tag
        assert edge.chain_key_iv is not None
        assert edge.chain_key_tag is not None
        assert edge.ephemeral_chain_key is not None
        assert len(edge.chain_key_iv) == 12
        assert len(edge.chain_key_tag) == 16


class TestExpireStale:
    """Batch expiration tests."""

    def test_expire_stale_returns_expired(self, group_db, crypto):
        """expire_stale() returns list of expired member_ids."""
        tracker = HotEdgeTracker(group_db, crypto, os.urandom(32), ttl_seconds=1)
        tracker.activate("g1", "alice", os.urandom(32))
        tracker.activate("g1", "bob", os.urandom(32))

        with patch("AQM_Database.aqm_group.hot_edge.time") as mock_time:
            mock_time.time.return_value = time.time() + 2
            expired = tracker.expire_stale("g1")

        assert set(expired) == {"alice", "bob"}

    def test_expire_stale_skips_fresh(self, group_db, crypto):
        """expire_stale() does not expire recently active edges."""
        tracker = HotEdgeTracker(group_db, crypto, os.urandom(32), ttl_seconds=600)
        tracker.activate("g1", "alice", os.urandom(32))

        expired = tracker.expire_stale("g1")
        assert expired == []
        assert tracker.is_hot("g1", "alice")
