"""
AQM per-tier timing + TLS 1.3 handshake comparison benchmark.

AQM measurement (per tier, N iterations):
    mint_coin() + vault.store_key() + server.upload_coins() +
    fetch_and_cache() + inventory.select_coin() +
    encrypt_message() + decrypt_message() + vault.burn_key()
    → total ms

TLS 1.3 measurement (N iterations):
    Ephemeral self-signed cert via subprocess/openssl,
    loopback TLS server in thread, measure ssl.wrap_socket() handshake.
"""

import asyncio
import os
import ssl
import socket
import statistics
import subprocess
import tempfile
import threading
import time
from typing import Optional
from uuid import UUID, uuid4

from AQM_Database.aqm_shared import config
from AQM_Database.aqm_shared.crypto_engine import CryptoEngine, mint_coin
from AQM_Database.aqm_shared.types import CoinUpload
from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_server import config as srv_config
from AQM_Database.aqm_server.coin_inventory import CoinInventoryServer
from AQM_Database.bridge import upload_coins, fetch_and_cache
from AQM_Database.chat.protocol import encrypt_message, decrypt_message
from AQM_Database.prototype import Display


BENCHMARK_ITERATIONS = 50


def _generate_self_signed_cert(tmpdir: str) -> tuple[str, str]:
    """Generate an ephemeral self-signed cert+key pair for TLS benchmark."""
    cert_path = os.path.join(tmpdir, "cert.pem")
    key_path = os.path.join(tmpdir, "key.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-keyout", key_path, "-out", cert_path,
            "-days", "1", "-nodes",
            "-subj", "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert_path, key_path


def _measure_tls_handshake(
    cert_path: str,
    key_path: str,
    iterations: int = BENCHMARK_ITERATIONS,
) -> list[float]:
    """Measure TLS 1.3 handshake times on loopback.

    Returns list of durations in milliseconds.
    """
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    server_ctx.load_cert_chain(cert_path, key_path)

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE

    # Find a free port
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    # Start TLS server in a thread
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", port))
    server_sock.listen(iterations + 5)
    server_sock.settimeout(10)

    stop_event = threading.Event()

    def _server_loop():
        while not stop_event.is_set():
            try:
                conn, _ = server_sock.accept()
                tls_conn = server_ctx.wrap_socket(conn, server_side=True)
                tls_conn.close()
            except (socket.timeout, OSError):
                pass

    t = threading.Thread(target=_server_loop, daemon=True)
    t.start()

    durations = []
    for _ in range(iterations):
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.connect(("127.0.0.1", port))

        start = time.perf_counter()
        tls_sock = client_ctx.wrap_socket(raw, server_hostname="localhost")
        elapsed_ms = (time.perf_counter() - start) * 1000
        durations.append(elapsed_ms)

        tls_sock.close()

    stop_event.set()
    server_sock.close()
    t.join(timeout=2)

    return durations


async def _measure_aqm_tier(
    tier: str,
    vault: SecureVault,
    vault_client,
    inventory: SmartInventory,
    inv_client,
    server: CoinInventoryServer,
    engine: CryptoEngine,
    iterations: int = BENCHMARK_ITERATIONS,
) -> list[float]:
    """Measure full AQM lifecycle for one tier.

    Returns list of durations in milliseconds.
    """
    user_id = uuid4()
    requester_id = uuid4()
    contact_id = "bench_contact"

    durations = []

    for i in range(iterations):
        start = time.perf_counter()

        # 1. Mint
        bundle = mint_coin(engine, tier)

        # 2. Store private key in vault
        vault.store_key(
            key_id=bundle.key_id,
            coin_category=bundle.coin_category,
            encrypted_blob=bundle.encrypted_blob,
            encryption_iv=bundle.encryption_iv,
            auth_tag=bundle.auth_tag,
        )

        # 3. Upload public key to server
        await upload_coins(server, user_id, [CoinUpload(
            key_id=bundle.key_id,
            coin_category=bundle.coin_category,
            public_key_blob=bundle.public_key,
            signature_blob=bundle.signature,
        )])

        # 4. Register contact (only first iteration)
        if i == 0:
            inventory.register_contact(contact_id, "BESTIE", "Bench")

        # 5. Fetch & cache from server
        await fetch_and_cache(
            server, inventory, contact_id,
            user_id, requester_id, tier, 1,
        )

        # 6. Select coin from inventory
        entry = inventory.select_coin(contact_id, tier)

        # 7. Encrypt
        ciphertext = encrypt_message("benchmark payload", entry.public_key)

        # 8. Decrypt
        plaintext, _ = decrypt_message(ciphertext, entry.public_key)

        # 9. Burn private key
        vault.burn_key(bundle.key_id)

        elapsed_ms = (time.perf_counter() - start) * 1000
        durations.append(elapsed_ms)

    # Cleanup
    for t in ("GOLD", "SILVER", "BRONZE"):
        inv_client.delete(f"{config.INV_IDX_PREFIX}:{contact_id}:{t}")
    inv_client.delete(f"{config.INV_META_PREFIX}:{contact_id}")

    cursor = 0
    while True:
        cursor, keys = inv_client.scan(
            cursor=cursor,
            match=f"{config.INV_KEY_PREFIX}:{contact_id}:*",
            count=100,
        )
        if keys:
            inv_client.delete(*keys)
        if cursor == 0:
            break

    cursor = 0
    while True:
        cursor, keys = vault_client.scan(
            cursor=cursor,
            match=f"{config.VAULT_KEY_PREFIX}:*",
            count=100,
        )
        if keys:
            vault_client.delete(*keys)
        if cursor == 0:
            break
    vault_client.delete(config.VAULT_STATS_KEY)

    async with server.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM coin_inventory WHERE user_id = $1", user_id,
        )

    return durations


async def _measure_aqm_per_message(
    tier: str,
    vault: SecureVault,
    vault_client,
    inventory: SmartInventory,
    inv_client,
    server: CoinInventoryServer,
    engine: CryptoEngine,
    iterations: int = BENCHMARK_ITERATIONS,
) -> list[float]:
    """Measure AQM per-message cost with pre-minted coins.

    Pre-provisions all coins before timing starts.  The timed loop
    measures ONLY: select_coin + encrypt + decrypt + burn_key.
    Uses one contact per coin to avoid budget-cap limits.

    Returns list of durations in milliseconds.
    """
    user_id = uuid4()
    requester_id = uuid4()

    # ─── Pre-provision (NOT timed) ───
    contact_ids = []
    for i in range(iterations):
        cid = f"bench_msg_{i}"
        contact_ids.append(cid)

        bundle = mint_coin(engine, tier)

        vault.store_key(
            key_id=bundle.key_id,
            coin_category=bundle.coin_category,
            encrypted_blob=bundle.encrypted_blob,
            encryption_iv=bundle.encryption_iv,
            auth_tag=bundle.auth_tag,
        )

        await upload_coins(server, user_id, [CoinUpload(
            key_id=bundle.key_id,
            coin_category=bundle.coin_category,
            public_key_blob=bundle.public_key,
            signature_blob=bundle.signature,
        )])

        inventory.register_contact(cid, "BESTIE", f"BenchMsg{i}")

        await fetch_and_cache(
            server, inventory, cid,
            user_id, requester_id, tier, 1,
        )

    # ─── Timed loop: per-message cost only ───
    durations = []
    for i in range(iterations):
        cid = contact_ids[i]

        start = time.perf_counter()

        entry = inventory.select_coin(cid, tier)
        ciphertext = encrypt_message("benchmark payload", entry.public_key)
        _, _ = decrypt_message(ciphertext, entry.public_key)
        vault.burn_key(entry.key_id)

        elapsed_ms = (time.perf_counter() - start) * 1000
        durations.append(elapsed_ms)

    # ─── Cleanup ───
    for cid in contact_ids:
        for t in ("GOLD", "SILVER", "BRONZE"):
            inv_client.delete(f"{config.INV_IDX_PREFIX}:{cid}:{t}")
        inv_client.delete(f"{config.INV_META_PREFIX}:{cid}")
        cursor = 0
        while True:
            cursor, keys = inv_client.scan(
                cursor=cursor,
                match=f"{config.INV_KEY_PREFIX}:{cid}:*",
                count=100,
            )
            if keys:
                inv_client.delete(*keys)
            if cursor == 0:
                break

    cursor = 0
    while True:
        cursor, keys = vault_client.scan(
            cursor=cursor,
            match=f"{config.VAULT_KEY_PREFIX}:*",
            count=100,
        )
        if keys:
            vault_client.delete(*keys)
        if cursor == 0:
            break
    vault_client.delete(config.VAULT_STATS_KEY)

    async with server.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM coin_inventory WHERE user_id = $1", user_id,
        )

    return durations


def _stats(durations: list[float]) -> dict[str, float]:
    """Compute mean, median, p95 from a list of durations."""
    s = sorted(durations)
    return {
        "mean": statistics.mean(s),
        "median": statistics.median(s),
        "p95": s[int(len(s) * 0.95)] if len(s) >= 2 else s[-1],
    }


def format_benchmark_table(results: dict, per_msg_results: dict = None) -> str:
    """Format benchmark results as an ANSI table string."""
    D = Display

    col_w = 16
    tiers = ["GOLD", "SILVER", "BRONZE"]
    headers = ["Metric"] + [f"AQM {t}" for t in tiers] + ["TLS 1.3"]

    lines = []
    header_line = "".join(f"{h:<{col_w}}" for h in headers)
    lines.append(f"  {D.BOLD}{header_line}{D.RESET}")
    lines.append(f"  {'─' * (col_w * len(headers))}")

    lines.append(f"  {D.BOLD}  Full Lifecycle (includes minting){D.RESET}")

    for metric_key, label in [("mean", "Mean latency"), ("median", "Median"), ("p95", "P95")]:
        row = [f"{label:<{col_w}}"]
        for tier in tiers:
            val = results.get(tier, {}).get(metric_key, 0)
            row.append(f"{val:>{col_w - 4}.2f} ms ")
        tls_val = results.get("tls", {}).get(metric_key, 0)
        row.append(f"{tls_val:>{col_w - 4}.2f} ms ")
        lines.append(f"  {''.join(row)}")

    if per_msg_results:
        lines.append(f"  {'─' * (col_w * len(headers))}")
        lines.append(f"  {D.BOLD}  Per-Message (pre-minted coins){D.RESET}")

        for metric_key, label in [("mean", "Mean latency"), ("median", "Median"), ("p95", "P95")]:
            row = [f"{label:<{col_w}}"]
            for tier in tiers:
                val = per_msg_results.get(tier, {}).get(metric_key, 0)
                row.append(f"{val:>{col_w - 4}.2f} ms ")
            tls_val = results.get("tls", {}).get(metric_key, 0)
            row.append(f"{tls_val:>{col_w - 4}.2f} ms ")
            lines.append(f"  {''.join(row)}")

    lines.append(f"  {'─' * (col_w * len(headers))}")

    # Static info rows
    sizes = {"GOLD": "3,604 B", "SILVER": "1,248 B", "BRONZE": "96 B"}
    pq = {"GOLD": "Yes", "SILVER": "Partial", "BRONZE": "No"}

    size_row = [f"{'Bytes exchanged':<{col_w}}"]
    for tier in tiers:
        size_row.append(f"{sizes[tier]:>{col_w}}")
    size_row.append(f"{'~3,200 B':>{col_w}}")
    lines.append(f"  {''.join(size_row)}")

    pq_row = [f"{'PQ-resistant':<{col_w}}"]
    for tier in tiers:
        pq_row.append(f"{pq[tier]:>{col_w}}")
    pq_row.append(f"{'No':>{col_w}}")
    lines.append(f"  {''.join(pq_row)}")

    life_row = [f"{'Key lifetime':<{col_w}}"]
    for _ in tiers:
        life_row.append(f"{'One-time':>{col_w}}")
    life_row.append(f"{'Session':>{col_w}}")
    lines.append(f"  {''.join(life_row)}")

    return "\n".join(lines)


async def run_benchmark(
    vault_client=None,
    inv_client=None,
    pool=None,
    iterations: int = BENCHMARK_ITERATIONS,
) -> dict:
    """Run the full AQM + TLS benchmark suite.

    Returns results dict with keys: GOLD, SILVER, BRONZE, tls — each
    containing mean, median, p95.
    """
    from AQM_Database.aqm_db.connection import create_vault_client, create_inventory_client
    from AQM_Database.aqm_server.db import create_pool, close_pool

    own_pool = False
    if vault_client is None:
        vault_client = create_vault_client()
    if inv_client is None:
        inv_client = create_inventory_client()
    if pool is None:
        pool = await create_pool(
            srv_config.PG_DSN,
            srv_config.PG_POOL_MIN_SIZE,
            srv_config.PG_POOL_MAX_SIZE,
        )
        own_pool = True

    vault = SecureVault(vault_client)
    inventory = SmartInventory(inv_client)
    server = CoinInventoryServer(pool)
    engine = CryptoEngine()

    results = {}

    Display.phase_header(1, "AQM Full Lifecycle Benchmark")
    Display.arrow(f"Crypto backend: {engine.backend}")
    Display.arrow(f"Iterations per tier: {iterations}\n")

    for tier in ("GOLD", "SILVER", "BRONZE"):
        Display.arrow(f"Benchmarking {Display.tier_label(tier)}…")
        durations = await _measure_aqm_tier(
            tier, vault, vault_client, inventory, inv_client,
            server, engine, iterations,
        )
        results[tier] = _stats(durations)
        Display.success(
            f"{Display.tier_label(tier)}: "
            f"mean={results[tier]['mean']:.2f}ms  "
            f"median={results[tier]['median']:.2f}ms  "
            f"p95={results[tier]['p95']:.2f}ms"
        )

    per_msg_results = {}

    Display.phase_header(2, "AQM Per-Message Benchmark")
    Display.arrow("Coins pre-minted & cached before timing.")
    Display.arrow("Measures: select_coin + encrypt + decrypt + burn_key")
    Display.arrow(f"Iterations per tier: {iterations}\n")

    for tier in ("GOLD", "SILVER", "BRONZE"):
        Display.arrow(f"Benchmarking {Display.tier_label(tier)} per-message…")
        durations = await _measure_aqm_per_message(
            tier, vault, vault_client, inventory, inv_client,
            server, engine, iterations,
        )
        per_msg_results[tier] = _stats(durations)
        Display.success(
            f"{Display.tier_label(tier)}: "
            f"mean={per_msg_results[tier]['mean']:.2f}ms  "
            f"median={per_msg_results[tier]['median']:.2f}ms  "
            f"p95={per_msg_results[tier]['p95']:.2f}ms"
        )

    Display.phase_header(3, "TLS 1.3 Benchmark")
    Display.arrow(f"Iterations: {iterations}\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path, key_path = _generate_self_signed_cert(tmpdir)
        Display.arrow("Generated ephemeral self-signed cert")

        tls_durations = _measure_tls_handshake(cert_path, key_path, iterations)
        results["tls"] = _stats(tls_durations)
        Display.success(
            f"TLS 1.3: "
            f"mean={results['tls']['mean']:.2f}ms  "
            f"median={results['tls']['median']:.2f}ms  "
            f"p95={results['tls']['p95']:.2f}ms"
        )

    Display.phase_header(4, "Comparison Table")
    print(format_benchmark_table(results, per_msg_results))
    print()

    # Cleanup
    vault_client.close()
    inv_client.close()
    if own_pool:
        await close_pool()

    results["per_msg"] = per_msg_results
    return results