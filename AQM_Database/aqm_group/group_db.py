"""
Client-side SQLite storage for AQM Group Chat (PDF §8.1).

Stores groups, membership, hot edge state, and message history locally.
Server is zero-knowledge (D10) — all message content stays on-device.
Hot edge chain keys are stored encrypted (D4), never plaintext.
Thread-safe via internal lock — required for Flask threaded mode.
"""

import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional

from AQM_Database.aqm_group.group_types import (
    GroupInfo,
    GroupMemberInfo,
    HotEdgeState,
    HOT_EDGE_TTL_SECONDS,
)


class GroupDatabase:
    """
    SQLite database for group chat state.

    Why: the server is zero-knowledge (D10), so all group metadata,
    membership, hot edge state, and message history must be stored locally.
    Thread-safe via internal lock — required for Flask threaded mode.
    """

    def __init__(self, db_path: str = "~/.aqm/groups.db"):
        db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.connection = sqlite3.connect(db_path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        self.cursor = self.connection.cursor()
        self._create_tables()

    def _create_tables(self):
        """Create all group chat tables. Idempotent."""
        self.cursor.executescript("""
            CREATE TABLE IF NOT EXISTS groups (
                group_id    TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                my_role     TEXT NOT NULL DEFAULT 'MEMBER'
                            CHECK (my_role IN ('ADMIN', 'MEMBER')),
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS group_members (
                group_id     TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
                member_id    TEXT NOT NULL,
                display_name TEXT NOT NULL,
                priority     TEXT NOT NULL DEFAULT 'STRANGER'
                             CHECK (priority IN ('BESTIE', 'MATE', 'STRANGER')),
                joined_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, member_id)
            );

            CREATE TABLE IF NOT EXISTS group_hot_edges (
                group_id              TEXT NOT NULL,
                member_id             TEXT NOT NULL,
                state                 TEXT NOT NULL DEFAULT 'COLD'
                                      CHECK (state IN ('HOT', 'COLD')),
                last_activity_at      REAL,
                ephemeral_chain_key   BLOB,
                chain_key_iv          BLOB,
                chain_key_tag         BLOB,
                msg_counter           INTEGER DEFAULT 0,
                PRIMARY KEY (group_id, member_id)
            );

            CREATE TABLE IF NOT EXISTS group_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
                sender_id   TEXT NOT NULL,
                plaintext   TEXT NOT NULL,
                tier_used   TEXT,
                hot_path    BOOLEAN DEFAULT 0,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_group_members
                ON group_members (group_id);
            CREATE INDEX IF NOT EXISTS idx_group_messages_ts
                ON group_messages (group_id, received_at);

            CREATE TABLE IF NOT EXISTS group_peer_kem_secrets (
                group_id     TEXT NOT NULL,
                peer_id      TEXT NOT NULL,
                ciphertext   BLOB NOT NULL,
                PRIMARY KEY (group_id, peer_id)
            );
        """)
        self.connection.commit()

    # ── Group CRUD ─────────────────────────────────────────────────────

    def create_group(self, group_id: str, name: str, role: str = "ADMIN") -> GroupInfo:
        """
        Create a new group. Why: D5 — creator is ADMIN, Phase I creator-only.
        """
        now = datetime.now().isoformat()
        with self._lock:
            self.cursor.execute(
                "INSERT INTO groups (group_id, name, my_role, created_at) VALUES (?, ?, ?, ?)",
                (group_id, name, role, now),
            )
            self.connection.commit()
        return GroupInfo(group_id=group_id, name=name, my_role=role, created_at=now)

    def get_group(self, group_id: str) -> Optional[GroupInfo]:
        """Fetch a single group by ID."""
        with self._lock:
            self.cursor.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,))
            row = self.cursor.fetchone()
        if not row:
            return None
        return GroupInfo(group_id=row[0], name=row[1], my_role=row[2], created_at=row[3])

    def get_all_groups(self) -> list[GroupInfo]:
        """List all groups this device belongs to."""
        with self._lock:
            self.cursor.execute("SELECT * FROM groups ORDER BY created_at DESC")
            rows = self.cursor.fetchall()
        return [
            GroupInfo(group_id=r[0], name=r[1], my_role=r[2], created_at=r[3])
            for r in rows
        ]

    # ── Membership ─────────────────────────────────────────────────────

    def add_member(self, group_id: str, member_id: str, display_name: str,
                   priority: str = "STRANGER") -> GroupMemberInfo:
        """
        Add a member to a group. Why: D1 — tier assignment is sender-local,
        stored here for display only. Re-evaluated from contacts_db at send time (G1).
        """
        with self._lock:
            self.cursor.execute(
                "INSERT OR REPLACE INTO group_members "
                "(group_id, member_id, display_name, priority) VALUES (?, ?, ?, ?)",
                (group_id, member_id, display_name, priority),
            )
            self.connection.commit()
        return GroupMemberInfo(
            group_id=group_id, member_id=member_id,
            display_name=display_name, priority=priority,
        )

    def get_members(self, group_id: str) -> list[GroupMemberInfo]:
        """Get all members of a group."""
        with self._lock:
            self.cursor.execute(
                "SELECT group_id, member_id, display_name, priority "
                "FROM group_members WHERE group_id = ?",
                (group_id,),
            )
            rows = self.cursor.fetchall()
        return [
            GroupMemberInfo(group_id=r[0], member_id=r[1], display_name=r[2], priority=r[3])
            for r in rows
        ]

    def get_member(self, group_id: str, member_id: str) -> Optional[GroupMemberInfo]:
        """Get a single member."""
        with self._lock:
            self.cursor.execute(
                "SELECT group_id, member_id, display_name, priority "
                "FROM group_members WHERE group_id = ? AND member_id = ?",
                (group_id, member_id),
            )
            row = self.cursor.fetchone()
        if not row:
            return None
        return GroupMemberInfo(group_id=row[0], member_id=row[1],
                              display_name=row[2], priority=row[3])

    # ── Hot Edge State ─────────────────────────────────────────────────

    def get_hot_edge(self, group_id: str, member_id: str) -> Optional[HotEdgeState]:
        """
        Fetch hot edge state for a (group_id, member_id) pair.
        Why: D3 — edges are per-(group, member), not global.
        """
        with self._lock:
            self.cursor.execute(
                "SELECT group_id, member_id, state, last_activity_at, "
                "ephemeral_chain_key, chain_key_iv, chain_key_tag, msg_counter "
                "FROM group_hot_edges WHERE group_id = ? AND member_id = ?",
                (group_id, member_id),
            )
            row = self.cursor.fetchone()
        if not row:
            return None
        return HotEdgeState(
            group_id=row[0], member_id=row[1], state=row[2],
            last_activity_at=row[3], ephemeral_chain_key=row[4],
            chain_key_iv=row[5], chain_key_tag=row[6], msg_counter=row[7],
        )

    def set_hot_edge(self, edge: HotEdgeState) -> None:
        """
        Upsert hot edge state. Why: D4 — chain key must be stored encrypted.
        Caller is responsible for encrypting ephemeral_chain_key before passing.
        """
        with self._lock:
            self.cursor.execute(
                "INSERT OR REPLACE INTO group_hot_edges "
                "(group_id, member_id, state, last_activity_at, "
                "ephemeral_chain_key, chain_key_iv, chain_key_tag, msg_counter) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (edge.group_id, edge.member_id, edge.state, edge.last_activity_at,
                 edge.ephemeral_chain_key, edge.chain_key_iv, edge.chain_key_tag,
                 edge.msg_counter),
            )
            self.connection.commit()

    def delete_hot_edge(self, group_id: str, member_id: str) -> None:
        """
        Delete hot edge on HOT→COLD transition. Why: D4 — ephemeral key burned.
        """
        with self._lock:
            self.cursor.execute(
                "DELETE FROM group_hot_edges WHERE group_id = ? AND member_id = ?",
                (group_id, member_id),
            )
            self.connection.commit()

    def store_peer_kem_ciphertext(self, group_id: str, peer_id: str, ciphertext: bytes) -> None:
        """Persist AEAD blob for last KEM shared secret toward peer (encrypted at rest)."""
        with self._lock:
            self.cursor.execute(
                "INSERT OR REPLACE INTO group_peer_kem_secrets (group_id, peer_id, ciphertext) "
                "VALUES (?, ?, ?)",
                (group_id, peer_id, ciphertext),
            )
            self.connection.commit()

    def get_peer_kem_ciphertext(self, group_id: str, peer_id: str) -> Optional[bytes]:
        with self._lock:
            self.cursor.execute(
                "SELECT ciphertext FROM group_peer_kem_secrets WHERE group_id = ? AND peer_id = ?",
                (group_id, peer_id),
            )
            row = self.cursor.fetchone()
        return row[0] if row else None

    def delete_peer_kem_ciphertext(self, group_id: str, peer_id: str) -> None:
        with self._lock:
            self.cursor.execute(
                "DELETE FROM group_peer_kem_secrets WHERE group_id = ? AND peer_id = ?",
                (group_id, peer_id),
            )
            self.connection.commit()

    def get_all_hot_edges(self, group_id: str) -> list[HotEdgeState]:
        """Get all hot edges for a group (for batch checks)."""
        with self._lock:
            self.cursor.execute(
                "SELECT group_id, member_id, state, last_activity_at, "
                "ephemeral_chain_key, chain_key_iv, chain_key_tag, msg_counter "
                "FROM group_hot_edges WHERE group_id = ? AND state = 'HOT'",
                (group_id,),
            )
            rows = self.cursor.fetchall()
        return [
            HotEdgeState(
                group_id=r[0], member_id=r[1], state=r[2],
                last_activity_at=r[3], ephemeral_chain_key=r[4],
                chain_key_iv=r[5], chain_key_tag=r[6], msg_counter=r[7],
            )
            for r in rows
        ]

    # ── Message History ────────────────────────────────────────────────

    def record_message(self, group_id: str, sender_id: str, plaintext: str,
                       tier_used: str = None, hot_path: bool = False) -> int:
        """
        Store a group message locally. Why: D10 — client-only history,
        server is zero-knowledge.
        Returns the row id.
        """
        with self._lock:
            self.cursor.execute(
                "INSERT INTO group_messages (group_id, sender_id, plaintext, tier_used, hot_path) "
                "VALUES (?, ?, ?, ?, ?)",
                (group_id, sender_id, plaintext, tier_used, int(hot_path)),
            )
            self.connection.commit()
            return self.cursor.lastrowid

    def get_messages(self, group_id: str, limit: int = 100) -> list[dict]:
        """Fetch recent messages for a group."""
        with self._lock:
            self.cursor.execute(
                "SELECT id, group_id, sender_id, plaintext, tier_used, hot_path, received_at "
                "FROM group_messages WHERE group_id = ? ORDER BY received_at DESC LIMIT ?",
                (group_id, limit),
            )
            rows = self.cursor.fetchall()
        return [
            {
                "id": r[0], "group_id": r[1], "sender_id": r[2],
                "plaintext": r[3], "tier_used": r[4],
                "hot_path": bool(r[5]), "received_at": r[6],
            }
            for r in rows
        ]
