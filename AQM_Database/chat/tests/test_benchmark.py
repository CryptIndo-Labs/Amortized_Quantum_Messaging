"""Tests for chat.benchmark — AQM timing + TLS comparison."""

import os
import tempfile
import pytest
import pytest_asyncio

from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.chat.benchmark import (
    _stats,
    format_benchmark_table,
    _generate_self_signed_cert,
    _measure_tls_handshake,
    _measure_aqm_tier,
    _measure_aqm_per_message,
)
from AQM_Database.aqm_shared.crypto_engine import CryptoEngine

pytestmark = pytest.mark.asyncio


# ─── _stats helper ───

def test_stats_basic():
    durations = [1.0, 2.0, 3.0, 4.0, 5.0]
    s = _stats(durations)
    assert s["mean"] == 3.0
    assert s["median"] == 3.0
    assert s["p95"] == 5.0


def test_stats_single_value():
    s = _stats([42.0])
    assert s["mean"] == 42.0
    assert s["median"] == 42.0
    assert s["p95"] == 42.0


# ─── format_benchmark_table ───

def test_format_table_contains_headers():
    results = {
        "GOLD": {"mean": 10.0, "median": 9.5, "p95": 15.0},
        "SILVER": {"mean": 5.0, "median": 4.5, "p95": 8.0},
        "BRONZE": {"mean": 1.0, "median": 0.9, "p95": 2.0},
        "tls": {"mean": 3.0, "median": 2.8, "p95": 5.0},
    }
    table = format_benchmark_table(results)
    assert "AQM GOLD" in table
    assert "AQM SILVER" in table
    assert "AQM BRONZE" in table
    assert "TLS 1.3" in table
    assert "Mean latency" in table
    assert "PQ-resistant" in table


def test_format_table_contains_values():
    results = {
        "GOLD": {"mean": 12.34, "median": 11.0, "p95": 20.0},
        "SILVER": {"mean": 5.67, "median": 5.0, "p95": 9.0},
        "BRONZE": {"mean": 1.23, "median": 1.0, "p95": 2.0},
        "tls": {"mean": 3.45, "median": 3.0, "p95": 6.0},
    }
    table = format_benchmark_table(results)
    assert "12.34" in table
    assert "5.67" in table
    assert "1.23" in table
    assert "3.45" in table


# ─── TLS benchmark (no Docker needed) ───

def test_tls_handshake_runs():
    with tempfile.TemporaryDirectory() as tmpdir:
        cert, key = _generate_self_signed_cert(tmpdir)
        durations = _measure_tls_handshake(cert, key, iterations=5)
        assert len(durations) == 5
        assert all(d > 0 for d in durations)


# ─── AQM tier benchmark (needs Docker) ───

async def test_aqm_bronze_benchmark(fake_vault_client, fake_inv_client, server, pg_pool):
    vault = SecureVault(fake_vault_client)
    inventory = SmartInventory(fake_inv_client)
    engine = CryptoEngine()

    durations = await _measure_aqm_tier(
        "BRONZE", vault, fake_vault_client, inventory, fake_inv_client,
        server, engine, iterations=3,
    )
    assert len(durations) == 3
    assert all(d > 0 for d in durations)


async def test_aqm_bronze_per_message(fake_vault_client, fake_inv_client, server, pg_pool):
    vault = SecureVault(fake_vault_client)
    inventory = SmartInventory(fake_inv_client)
    engine = CryptoEngine()

    durations = await _measure_aqm_per_message(
        "BRONZE", vault, fake_vault_client, inventory, fake_inv_client,
        server, engine, iterations=3,
    )
    assert len(durations) == 3
    assert all(d > 0 for d in durations)


def test_format_table_with_per_msg_results():
    results = {
        "GOLD": {"mean": 10.0, "median": 9.5, "p95": 15.0},
        "SILVER": {"mean": 5.0, "median": 4.5, "p95": 8.0},
        "BRONZE": {"mean": 1.0, "median": 0.9, "p95": 2.0},
        "tls": {"mean": 3.0, "median": 2.8, "p95": 5.0},
    }
    per_msg = {
        "GOLD": {"mean": 0.8, "median": 0.7, "p95": 1.2},
        "SILVER": {"mean": 0.5, "median": 0.4, "p95": 0.9},
        "BRONZE": {"mean": 0.3, "median": 0.25, "p95": 0.6},
    }
    table = format_benchmark_table(results, per_msg)
    assert "Per-Message" in table
    assert "0.80" in table
