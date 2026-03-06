#!/usr/bin/env python3
"""
AQM Demo Runner — Preflight checks + prototype execution.

Usage:
    python demo.py              Run the full 4-phase lifecycle demo
    python demo.py --check      Only run preflight checks, don't start demo
    python demo.py --tests      Run the full test suite (171 tests)
    python demo.py --all        Run tests first, then demo
    python demo.py --chat       Run two-user chat demo (all priority scenarios)
    python demo.py --demo-pair  Launch two terminals for interactive chat (default: BESTIE)
    python demo.py --demo-pair --priority MATE     Demo with MATE priority
    python demo.py --demo-pair --priority STRANGER  Demo with STRANGER handshake
    python demo.py --chat-bench Run AQM vs TLS 1.3 benchmark

Requires:
    - conda activate aqm-db
    - Docker containers running:  cd AQM_Database && docker compose up -d
"""

import sys
import argparse
import subprocess
import socket

# ─── ANSI helpers ───

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"

def ok(msg):   print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg): print(f"  {RED}✗{RESET} {msg}")
def info(msg): print(f"  {CYAN}→{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}!{RESET} {msg}")


# ─── Preflight checks ───

def check_port(host, port, label):
    """Check if a TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=2):
            ok(f"{label} is reachable at {host}:{port}")
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        fail(f"{label} is NOT reachable at {host}:{port}")
        return False


def check_import(module, label):
    """Check if a Python module can be imported."""
    try:
        __import__(module)
        ok(f"{label} importable")
        return True
    except ImportError as e:
        fail(f"{label} import failed: {e}")
        return False


def preflight():
    """Run all preflight checks. Returns True if everything passes."""
    print(f"\n{CYAN}{BOLD}  Preflight Checks{RESET}")
    print(f"  {'─' * 40}\n")

    results = []

    # Python packages
    info("Checking Python packages…")
    results.append(check_import("redis", "redis-py"))
    results.append(check_import("asyncpg", "asyncpg"))
    results.append(check_import("fakeredis", "fakeredis"))
    results.append(check_import("nacl", "pynacl"))

    # Optional PQC backend
    try:
        __import__("oqs")
        ok("liboqs-python importable (full post-quantum)")
    except ImportError:
        warn("liboqs-python not found — Kyber-768 will use urandom mock (OK for demo)")

    print()

    # Infrastructure
    info("Checking Docker services…")
    results.append(check_port("localhost", 6379, "Redis"))
    results.append(check_port("localhost", 5433, "PostgreSQL"))

    print()

    # AQM package
    info("Checking AQM package…")
    results.append(check_import("AQM_Database.aqm_db.vault", "SecureVault"))
    results.append(check_import("AQM_Database.aqm_db.inventory", "SmartInventory"))
    results.append(check_import("AQM_Database.aqm_server.coin_inventory", "CoinInventoryServer"))
    results.append(check_import("AQM_Database.bridge", "Bridge"))
    results.append(check_import("AQM_Database.aqm_shared.crypto_engine", "CryptoEngine"))
    results.append(check_import("AQM_Database.aqm_shared.context_manager", "ContextManager"))

    print()

    # Crypto backend summary
    from AQM_Database.aqm_shared.crypto_engine import CryptoEngine
    engine = CryptoEngine()
    info(f"Crypto backend: {BOLD}{engine.backend}{RESET}")

    print()
    all_ok = all(results)
    if all_ok:
        ok(f"{BOLD}All preflight checks passed{RESET}")
    else:
        fail(f"{BOLD}Some checks failed — fix the issues above before running the demo{RESET}")

    print()
    return all_ok


# ─── Test runner ───

def run_tests():
    """Run the full test suite across all three packages."""
    print(f"\n{CYAN}{BOLD}  Running Full Test Suite{RESET}")
    print(f"  {'─' * 40}\n")

    suites = [
        ("Shared (crypto + context)", "AQM_Database/aqm_shared/tests/", False),
        ("Redis (vault + inventory + gc)", "AQM_Database/aqm_db/tests/", False),
        ("Server (PostgreSQL + bridge)", "AQM_Database/aqm_server/tests/", True),
        ("Chat (protocol + session + benchmark)", "AQM_Database/chat/tests/", True),
    ]

    total_passed = 0
    total_failed = 0

    for label, path, needs_docker in suites:
        print(f"  {BOLD}── {label} ──{RESET}")
        if needs_docker:
            if not check_port("localhost", 5433, "PostgreSQL"):
                warn(f"Skipping {label} — PostgreSQL not available")
                print()
                continue

        result = subprocess.run(
            [sys.executable, "-m", "pytest", path, "-v", "--tb=short"],
            capture_output=False,
        )

        if result.returncode == 0:
            ok(f"{label}: all passed")
        else:
            fail(f"{label}: some tests failed (exit code {result.returncode})")
            total_failed += 1

        print()

    return total_failed == 0


# ─── Prototype demo ───

def run_demo():
    """Run the 4-phase prototype demo."""
    import asyncio
    from AQM_Database.prototype import main
    asyncio.run(main())


# ─── Entry point ───

def parse_args():
    parser = argparse.ArgumentParser(
        description="AQM Prototype Demo Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python demo.py              Run full demo (preflight + 4-phase lifecycle)
  python demo.py --check      Only run preflight checks
  python demo.py --tests      Run the full test suite
  python demo.py --all        Run tests first, then demo
  python demo.py --chat       Run two-user chat demo (all priority scenarios)
  python demo.py --demo-pair  Launch two terminals (default: BESTIE)
  python demo.py --demo-pair --priority MATE      MATE with SILVER ceiling
  python demo.py --demo-pair --priority STRANGER  STRANGER handshake demo
  python demo.py --chat-bench Run AQM vs TLS 1.3 benchmark
        """,
    )
    parser.add_argument("--check", action="store_true", help="Only run preflight checks")
    parser.add_argument("--tests", action="store_true", help="Run the full test suite")
    parser.add_argument("--all", action="store_true", help="Run tests first, then demo")
    parser.add_argument("--chat", action="store_true", help="Run two-user chat demo (all priorities)")
    parser.add_argument("--demo-pair", action="store_true", help="Launch two terminals for interactive chat")
    parser.add_argument("--priority", choices=["BESTIE", "MATE", "STRANGER"],
                        default="BESTIE", help="Priority for --demo-pair (default: BESTIE)")
    parser.add_argument("--chat-bench", action="store_true", help="Run AQM vs TLS 1.3 benchmark")
    return parser.parse_args()


def main():
    args = parse_args()

    print(f"""
{CYAN}{BOLD}    ┌────────────────────────────────────────────────────┐
    │   AQM — Amortized Quantum Messaging                │
    │   Post-Quantum Key Lifecycle Demo Runner           │
    └────────────────────────────────────────────────────┘{RESET}
    """)

    if args.check:
        ok_flag = preflight()
        sys.exit(0 if ok_flag else 1)

    if args.chat:
        if not preflight():
            fail("Preflight failed — aborting")
            sys.exit(1)
        import asyncio
        from AQM_Database.chat.session import run_auto_demo
        asyncio.run(run_auto_demo())
        sys.exit(0)

    if args.demo_pair:
        if not preflight():
            fail("Preflight failed — aborting")
            sys.exit(1)
        from AQM_Database.chat.cli import launch_demo_pair
        launch_demo_pair(args.priority)
        sys.exit(0)

    if args.chat_bench:
        if not preflight():
            fail("Preflight failed — aborting")
            sys.exit(1)
        import asyncio
        from AQM_Database.chat.benchmark import run_benchmark
        asyncio.run(run_benchmark())
        sys.exit(0)

    if args.tests:
        preflight()
        ok_flag = run_tests()
        sys.exit(0 if ok_flag else 1)

    if args.all:
        if not preflight():
            fail("Preflight failed — aborting")
            sys.exit(1)
        if not run_tests():
            fail("Tests failed — aborting demo")
            sys.exit(1)
        print(f"\n{CYAN}{BOLD}  All tests passed — starting demo…{RESET}\n")
        run_demo()
        sys.exit(0)

    # Default: preflight + demo
    if not preflight():
        fail("Preflight failed — fix the issues above first")
        print(f"\n  {DIM}Hint: cd AQM_Database && docker compose up -d{RESET}\n")
        sys.exit(1)

    run_demo()


if __name__ == "__main__":
    main()
