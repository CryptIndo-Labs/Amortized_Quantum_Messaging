"""
Shared test fixtures for aqm_group tests.

All tests run without Docker, WebSocket server, or live Redis (G7).
Uses fakeredis for inventory, in-memory SQLite for group_db and contacts_db.
Real CryptoEngine — no mock crypto.
"""

import pytest
import fakeredis

from AQM_Database.aqm_shared.crypto_engine import CryptoEngine
from AQM_Database.aqm_group.group_db import GroupDatabase
from AQM_Database.aqm_contacts.contacts_db import ContactsDatabase
from AQM_Database.aqm_db.inventory import SmartInventory


@pytest.fixture
def crypto():
    """Real CryptoEngine instance — no mocks."""
    return CryptoEngine()


@pytest.fixture
def group_db(tmp_path):
    """In-memory-like SQLite group database (uses tmp_path for isolation)."""
    return GroupDatabase(db_path=str(tmp_path / "test_groups.db"))


@pytest.fixture
def contacts_db(tmp_path):
    """Isolated contacts database for tier lookups (D1, G1)."""
    return ContactsDatabase(db_path=str(tmp_path / "test_contacts.db"))


@pytest.fixture
def fake_redis_server():
    """Shared fakeredis server for concurrency tests."""
    return fakeredis.FakeServer()


@pytest.fixture
def inventory(fake_redis_server):
    """SmartInventory backed by fakeredis."""
    client = fakeredis.FakeRedis(server=fake_redis_server, decode_responses=False)
    return SmartInventory(client)
