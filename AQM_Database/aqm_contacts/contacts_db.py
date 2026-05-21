import sqlite3
import os

from AQM_Database.aqm_contacts.models import Contact
from datetime import datetime
from dataclasses import dataclass, astuple, fields
from AQM_Database.aqm_shared import config


class ContactsDatabase:
    def __init__(self, db_path: str = "~/.aqm/contacts.db"):
        db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.connection = sqlite3.connect(db_path, check_same_thread=False)
        self.connection.execute('PRAGMA foreign_keys = ON')
        self.cursor = self.connection.cursor()

        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                contact_id         TEXT PRIMARY KEY,
                display_name       TEXT NOT NULL,
                priority           TEXT NOT NULL DEFAULT 'STRANGER'
                                   CHECK (priority IN ('BESTIE', 'MATE', 'STRANGER')),
                public_signing_key BLOB,
                first_seen_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_msg_at        TIMESTAMP,
                msg_count_total    INTEGER DEFAULT 0,
                msg_count_7d       INTEGER DEFAULT 0,
                msg_count_30d      INTEGER DEFAULT 0,
                priority_locked    BOOLEAN DEFAULT 0,
                is_blocked         BOOLEAN DEFAULT 0,
                my_burn_count      INTEGER DEFAULT 0,
                their_burn_count   INTEGER DEFAULT 0
            )
        """)

        # Migration: add burn columns to existing DBs that don't have them
        existing_cols = {
            row[1] for row in
            self.cursor.execute("PRAGMA table_info(contacts)").fetchall()
        }
        if "my_burn_count" not in existing_cols:
            self.cursor.execute(
                "ALTER TABLE contacts ADD COLUMN my_burn_count INTEGER DEFAULT 0"
            )
        if "their_burn_count" not in existing_cols:
            self.cursor.execute(
                "ALTER TABLE contacts ADD COLUMN their_burn_count INTEGER DEFAULT 0"
            )

        # Message log — kept for display / history, no longer drives promotion
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS message_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id TEXT NOT NULL
                           REFERENCES contacts(contact_id) ON DELETE CASCADE,
                direction  TEXT NOT NULL DEFAULT 'SENT'
                           CHECK (direction IN ('SENT', 'RECEIVED')),
                timestamp  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migration: add direction column if missing
        try:
            self.cursor.execute("SELECT direction FROM message_log LIMIT 1")
        except sqlite3.OperationalError:
            self.cursor.execute(
                "ALTER TABLE message_log ADD COLUMN direction TEXT NOT NULL DEFAULT 'SENT' "
                "CHECK (direction IN ('SENT', 'RECEIVED'))"
            )

        self.cursor.executescript("""
            CREATE INDEX IF NOT EXISTS idx_priority    ON contacts    (priority);
            CREATE INDEX IF NOT EXISTS idx_last_msg    ON contacts    (last_msg_at);
            CREATE INDEX IF NOT EXISTS idx_msg_log_ts  ON message_log (contact_id, timestamp);
        """)

        self.connection.commit()

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def add_contact(self, contact_id: str, display_name: str,
                    signing_key: bytes = None) -> Contact:
        contact = Contact(
            contact_id=contact_id,
            display_name=display_name,
            priority='STRANGER',
            public_signing_key=signing_key,
            first_seen_at=datetime.now(),
            last_msg_at=None,
            msg_count_total=0,
            msg_count_7d=0,
            msg_count_30d=0,
            priority_locked=False,
            is_blocked=False,
            my_burn_count=0,
            their_burn_count=0,
        )

        contact_tuple = astuple(contact)
        field_names   = [f.name for f in fields(Contact)]
        columns       = ', '.join(field_names)
        placeholders  = ', '.join(['?'] * len(field_names))

        self.cursor.execute(
            f"INSERT INTO contacts ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(contact_id) DO UPDATE SET display_name=excluded.display_name",
            contact_tuple,
        )
        self.connection.commit()
        return contact

    def remove_contact(self, contact_id: str) -> bool:
        if contact_id is None:
            return False
        self.cursor.execute("DELETE FROM contacts WHERE contact_id = ?", (contact_id,))
        self.connection.commit()
        return self.cursor.rowcount > 0

    def get_contact(self, contact_id: str) -> Contact | None:
        if contact_id is None:
            return None
        self.cursor.execute("SELECT * FROM contacts WHERE contact_id = ?", (contact_id,))
        row = self.cursor.fetchone()
        return Contact(*row) if row else None

    def _extract_contacts(self, rows) -> list[Contact]:
        return [Contact(*row) for row in rows]

    def get_contacts_by_priority(self, priority: str) -> list[Contact] | None:
        if priority not in config.VALID_PRIORITIES:
            return None
        self.cursor.execute("SELECT * FROM contacts WHERE priority = ?", (priority,))
        return self._extract_contacts(self.cursor.fetchall())

    def get_all_contacts(self) -> list[Contact] | None:
        self.cursor.execute("SELECT * FROM contacts")
        return self._extract_contacts(self.cursor.fetchall())

    # ── Message recording (display only — does NOT drive promotion) ───────────

    def record_message(self, contact_id: str, direction: str = "SENT") -> Contact:
        """Record a message for display/history. Priority is driven by record_burn()."""
        if direction not in ("SENT", "RECEIVED"):
            raise ValueError(f"Invalid direction: {direction}. Must be SENT or RECEIVED.")

        now = datetime.now()
        self.cursor.execute(
            "INSERT INTO message_log (contact_id, direction, timestamp) VALUES (?, ?, ?)",
            (contact_id, direction, now),
        )
        self.cursor.execute(
            "UPDATE contacts SET msg_count_total = msg_count_total + 1, last_msg_at = ? "
            "WHERE contact_id = ?",
            (now, contact_id),
        )

        # Rolling counts kept for display
        self.cursor.execute("""
            UPDATE contacts SET
                msg_count_7d = 2 * MIN(
                    (SELECT COUNT(*) FROM message_log
                     WHERE contact_id = ? AND direction = 'SENT'
                     AND timestamp > datetime('now', '-7 days')),
                    (SELECT COUNT(*) FROM message_log
                     WHERE contact_id = ? AND direction = 'RECEIVED'
                     AND timestamp > datetime('now', '-7 days'))
                ),
                msg_count_30d = 2 * MIN(
                    (SELECT COUNT(*) FROM message_log
                     WHERE contact_id = ? AND direction = 'SENT'
                     AND timestamp > datetime('now', '-30 days')),
                    (SELECT COUNT(*) FROM message_log
                     WHERE contact_id = ? AND direction = 'RECEIVED'
                     AND timestamp > datetime('now', '-30 days'))
                )
            WHERE contact_id = ?
        """, (contact_id, contact_id, contact_id, contact_id, contact_id))

        self.connection.commit()
        # Note: no _recompute_priority here — promotion is burn-driven only
        return self.get_contact(contact_id)

    # ── Burn-based promotion ──────────────────────────────────────────────────

    def record_burn(self, contact_id: str, side: str) -> Contact | None:
        """
        Record a coin burn event and recompute priority.

        side='mine'   — I just rekeyed sending to this contact.
                        Increments my_burn_count.
        side='theirs' — I just received a rekey parcel from this contact.
                        Increments their_burn_count.

        Promotion rules (both sides must reach the threshold):
            STRANGER → MATE:   my_burn_count >= 2 AND their_burn_count >= 2
            MATE → BESTIE:     my_burn_count >= 3 AND their_burn_count >= 3
        """
        if contact_id is None or side not in ("mine", "theirs"):
            return None

        col = "my_burn_count" if side == "mine" else "their_burn_count"
        self.cursor.execute(
            f"UPDATE contacts SET {col} = {col} + 1 WHERE contact_id = ?",
            (contact_id,),
        )
        self.connection.commit()

        self._recompute_priority(contact_id)
        return self.get_contact(contact_id)

    def _recompute_priority(self, contact_id: str) -> str | None:
        contact = self.get_contact(contact_id)
        if not contact:
            return None
        if contact.priority_locked:
            return None  # user pinned

        my    = contact.my_burn_count
        their = contact.their_burn_count

        # Both sides must have burned at least N coins with each other
        if my >= 3 and their >= 3:
            new_priority = "BESTIE"
        elif my >= 2 and their >= 2:
            new_priority = "MATE"
        else:
            new_priority = "STRANGER"

        # Priority only ever goes up — no demotion
        priority_rank = {"STRANGER": 0, "MATE": 1, "BESTIE": 2}
        if priority_rank.get(new_priority, 0) > priority_rank.get(contact.priority, 0):
            self.cursor.execute(
                "UPDATE contacts SET priority = ? WHERE contact_id = ?",
                (new_priority, contact_id),
            )
            self.connection.commit()
            return new_priority
        return None

    # ── Remaining helpers ─────────────────────────────────────────────────────

    def refresh_rolling_counts(self) -> int:
        """Prune old message_log rows and refresh rolling display counts."""
        self.cursor.execute(
            "DELETE FROM message_log WHERE timestamp < datetime('now', '-30 days')"
        )
        contacts = self.get_all_contacts()
        updates = 0
        for contact in contacts:
            cid = contact.contact_id
            self.cursor.execute("""
                UPDATE contacts SET
                    msg_count_7d = 2 * MIN(
                        (SELECT COUNT(*) FROM message_log
                         WHERE contact_id = ? AND direction = 'SENT'
                         AND timestamp > datetime('now', '-7 days')),
                        (SELECT COUNT(*) FROM message_log
                         WHERE contact_id = ? AND direction = 'RECEIVED'
                         AND timestamp > datetime('now', '-7 days'))
                    ),
                    msg_count_30d = 2 * MIN(
                        (SELECT COUNT(*) FROM message_log
                         WHERE contact_id = ? AND direction = 'SENT'
                         AND timestamp > datetime('now', '-30 days')),
                        (SELECT COUNT(*) FROM message_log
                         WHERE contact_id = ? AND direction = 'RECEIVED'
                         AND timestamp > datetime('now', '-30 days'))
                    )
                WHERE contact_id = ?
            """, (cid, cid, cid, cid, cid))
        self.connection.commit()
        return updates

    def lock_priority(self, contact_id: str, priority: str) -> Contact | None:
        if contact_id is None:
            return None
        if priority not in config.VALID_PRIORITIES:
            return None
        self.cursor.execute(
            "UPDATE contacts SET priority = ?, priority_locked = 1 WHERE contact_id = ?",
            (priority, contact_id),
        )
        self.connection.commit()
        return self.get_contact(contact_id)

    def unlock_priority(self, contact_id: str) -> Contact | None:
        if contact_id is None:
            return None
        self.cursor.execute(
            "UPDATE contacts SET priority_locked = 0 WHERE contact_id = ?",
            (contact_id,),
        )
        self.connection.commit()
        return self.get_contact(contact_id)

    def get_inactive_contacts(self, days: int = 30) -> list[Contact] | None:
        modifier = f'-{days} days'
        self.cursor.execute(
            "SELECT * FROM contacts WHERE last_msg_at < datetime('now', ?) "
            "OR last_msg_at IS NULL",
            (modifier,),
        )
        return self._extract_contacts(self.cursor.fetchall())

    def block_contact(self, contact_id: str) -> None:
        self.cursor.execute(
            "UPDATE contacts SET is_blocked = 1 WHERE contact_id = ?", (contact_id,)
        )
        self.connection.commit()

    def search_contact(self, query: str) -> list[Contact]:
        self.cursor.execute(
            "SELECT * FROM contacts WHERE display_name LIKE ?", (f'{query}%',)
        )
        return self._extract_contacts(self.cursor.fetchall())