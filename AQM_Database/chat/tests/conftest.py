import os
import pytest
import pytest_asyncio
import asyncpg
import fakeredis

from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_server.coin_inventory import CoinInventoryServer
from AQM_Database.aqm_shared.crypto_engine import CryptoEngine

TEST_DSN = os.environ.get(
    "AQM_TEST_DSN",
    "postgresql://aqm_user:aqm_dev_password@localhost:5433/aqm_test",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS coin_inventory(
    record_id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    key_id VARCHAR(36) NOT NULL,

    coin_category VARCHAR(6) NOT NULL CHECK ( coin_category IN ('GOLD' , 'SILVER' , 'BRONZE') ),
    public_key_blob BYTEA NOT NULL,
    signature_blob BYTEA NOT NULL,

    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_by UUID DEFAULT NULL,
    fetched_at TIMESTAMPTZ DEFAULT NULL,

    CONSTRAINT uq_user_key UNIQUE (user_id , key_id)
);

CREATE INDEX IF NOT EXISTS idx_coin_lookup
    ON coin_inventory (user_id , coin_category , uploaded_at ASC)
    WHERE fetched_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_coin_expiry
    ON coin_inventory (uploaded_at)
    WHERE fetched_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_coin_hard_delete
    ON coin_inventory (fetched_at)
    WHERE fetched_by IS NOT NULL;
"""


# ─── Fakeredis fixtures (no Docker) ───

@pytest.fixture
def fake_vault_client():
    r = fakeredis.FakeRedis()
    yield r
    r.flushdb()
    r.close()


@pytest.fixture
def fake_inv_client():
    r = fakeredis.FakeRedis()
    yield r
    r.flushdb()
    r.close()


@pytest.fixture
def vault(fake_vault_client):
    return SecureVault(fake_vault_client)


@pytest.fixture
def inventory(fake_inv_client):
    return SmartInventory(fake_inv_client)


@pytest.fixture
def engine():
    return CryptoEngine()


# ─── PostgreSQL fixtures (needs Docker) ───

@pytest_asyncio.fixture(scope="session")
async def _create_test_db():
    sys_dsn = TEST_DSN.rsplit("/", 1)[0] + "/aqm"
    conn = await asyncpg.connect(sys_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = 'aqm_test'"
        )
        if not exists:
            await conn.execute("CREATE DATABASE aqm_test")
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def pg_pool(_create_test_db):
    p = await asyncpg.create_pool(TEST_DSN, min_size=2, max_size=20)
    async with p.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    yield p
    await p.close()


@pytest_asyncio.fixture(autouse=True)
async def _truncate(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE coin_inventory RESTART IDENTITY")


@pytest.fixture
def server(pg_pool) -> CoinInventoryServer:
    return CoinInventoryServer(pg_pool)
