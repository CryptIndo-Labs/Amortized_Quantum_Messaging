"""
CLI entry point for the AQM chat demo.

Usage:
    # Interactive two-terminal chat
    python -m AQM_Database.chat.cli --user alice --partner bob --priority BESTIE
    python -m AQM_Database.chat.cli --user bob --partner alice --priority BESTIE

    # Launch both terminals automatically
    python -m AQM_Database.chat.cli --demo-pair
    python -m AQM_Database.chat.cli --demo-pair --priority MATE

    # Auto demo — runs all 3 priority scenarios in one terminal
    python -m AQM_Database.chat.cli --auto

    # Benchmark only
    python -m AQM_Database.chat.cli --benchmark
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from queue import Queue, Empty

from AQM_Database.prototype import Display
from AQM_Database.aqm_shared import config
from AQM_Database.aqm_shared.context_manager import (
    DeviceContext, SCENARIO_A, SCENARIO_B, SCENARIO_C,
)
from AQM_Database.chat.session import ChatSession, run_auto_demo
from AQM_Database.chat.benchmark import run_benchmark


# ─── ANSI helpers ───

CLEAR_LINE = "\033[2K\033[G"
SAVE_POS = "\033[s"
RESTORE_POS = "\033[u"
MOVE_UP = "\033[A"

D = Display  # shorthand


SCENARIOS = {
    "1": ("Home WiFi, 80% battery", SCENARIO_A, "GOLD"),
    "2": ("Outdoor cellular, 40% battery", SCENARIO_B, "SILVER"),
    "3": ("Underground, 3% battery", SCENARIO_C, "BRONZE"),
}


def _coin_bar(status: dict[str, int]) -> str:
    """Format a compact coin status string."""
    g = status.get("GOLD", 0)
    s = status.get("SILVER", 0)
    b = status.get("BRONZE", 0)
    return (
        f"{D.YELLOW}G:{g}{D.RESET} "
        f"{D.WHITE}S:{s}{D.RESET} "
        f"{D.ORANGE}B:{b}{D.RESET}"
    )


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _print_msg_sent(user: str, text: str, tier: str, key_id: str, ctx_label: str):
    """Print a sent message bubble with lifecycle detail."""
    ts = _timestamp()
    tier_lbl = D.tier_label(tier)
    print(
        f"  {D.DIM}{ts}{D.RESET}  "
        f"{D.GREEN}{D.BOLD}{user}{D.RESET} "
        f"[{tier_lbl}] "
        f"{text}"
    )
    print(
        f"           {D.DIM}"
        f"key={key_id[:12]}…  "
        f"ctx={ctx_label}  "
        f"encrypt→publish"
        f"{D.RESET}"
    )


def _print_msg_recv(partner: str, text: str, tier: str, verified: bool,
                    key_id: str, burned: bool, ctx_label: str):
    """Print a received message bubble with lifecycle detail."""
    ts = _timestamp()
    tier_lbl = D.tier_label(tier)
    v_tag = f"{D.GREEN}✓{D.RESET}" if verified else f"{D.RED}✗{D.RESET}"
    burn_tag = f" {D.RED}burned{D.RESET}" if burned else ""

    print(
        f"  {D.DIM}{ts}{D.RESET}  "
        f"{D.CYAN}{D.BOLD}{partner}{D.RESET} "
        f"[{tier_lbl}] "
        f"{text}  "
        f"{v_tag}{burn_tag}"
    )
    print(
        f"           {D.DIM}"
        f"key={key_id[:12]}…  "
        f"ctx={ctx_label or '?'}  "
        f"decrypt→verify→burn"
        f"{D.RESET}"
    )


def _input_thread(q: Queue, prompt: str):
    """Read lines from stdin in a thread, push to queue."""
    while True:
        try:
            line = input(prompt)
            q.put(line)
        except (EOFError, KeyboardInterrupt):
            q.put(None)
            break


# ─── Interactive chat ───

async def interactive_chat(user: str, partner: str, priority: str) -> None:
    """Run an interactive chat session with real-time message display."""

    # Banner
    print(f"""
{D.CYAN}{D.BOLD}
    ╔═══════════════════════════════════════════════════╗
    ║     AQM Secure Chat — Post-Quantum Messaging      ║
    ╚═══════════════════════════════════════════════════╝
{D.RESET}""")
    print(f"  {D.BOLD}User:{D.RESET} {D.GREEN}{user}{D.RESET}  "
          f"{D.BOLD}Partner:{D.RESET} {D.CYAN}{partner}{D.RESET}  "
          f"{D.BOLD}Priority:{D.RESET} {priority}\n")

    session = ChatSession(user, partner, priority)
    await session.setup()

    # ── Phase 1: Provision ──
    print(f"  {D.MAGENTA}{D.BOLD}── Phase 1: Mint & Upload ──{D.RESET}")
    print(f"  {D.DIM}Generating keypairs, storing private keys in vault,")
    print(f"  uploading public keys to PostgreSQL server…{D.RESET}")
    minted = await session.provision()
    total_minted = sum(minted.get(t, 0) for t in ("GOLD", "SILVER", "BRONZE"))
    print(f"  {D.GREEN}✓{D.RESET} Minted {D.BOLD}{total_minted}{D.RESET} coins "
          f"({D.YELLOW}G:{minted.get('GOLD', 0)}{D.RESET} "
          f"{D.WHITE}S:{minted.get('SILVER', 0)}{D.RESET} "
          f"{D.ORANGE}B:{minted.get('BRONZE', 0)}{D.RESET}) "
          f"→ vault + server\n")

    # ── Phase 2: Fetch partner keys ──
    print(f"  {D.MAGENTA}{D.BOLD}── Phase 2: Fetch Partner Keys ──{D.RESET}")
    print(f"  {D.DIM}Waiting for {partner}'s public keys on server…{D.RESET}", end="", flush=True)

    # Animated wait
    fetch_done = False

    async def _fetch():
        nonlocal fetch_done
        result = await session.register_and_fetch(timeout=120.0, poll_interval=0.5)
        fetch_done = True
        return result

    fetch_task = asyncio.create_task(_fetch())

    # Spinner while waiting
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    i = 0
    while not fetch_done:
        print(f"\r  {D.CYAN}{spinner[i % len(spinner)]}{D.RESET} "
              f"Waiting for {partner}'s keys on server…", end="", flush=True)
        i += 1
        await asyncio.sleep(0.1)

    fetched = await fetch_task
    total_fetched = sum(fetched.values())
    print(f"\r{CLEAR_LINE}", end="")

    if total_fetched > 0:
        print(f"  {D.GREEN}✓{D.RESET} Cached {D.BOLD}{total_fetched}{D.RESET} of {partner}'s keys "
              f"({D.YELLOW}G:{fetched['GOLD']}{D.RESET} "
              f"{D.WHITE}S:{fetched['SILVER']}{D.RESET} "
              f"{D.ORANGE}B:{fetched['BRONZE']}{D.RESET}) "
              f"← server (Delete-on-Fetch)")
    else:
        print(f"  {D.YELLOW}!{D.RESET} No keys fetched "
              f"({priority} budget: {config.BUDGET_CAPS[priority]})")
        if priority == "STRANGER":
            print(f"\n  {D.RED}STRANGER contacts have zero budget — "
                  f"cannot exchange messages.{D.RESET}")
            await session.teardown()
            return

    # ── Phase 3: Chat ──
    print(f"\n  {D.MAGENTA}{D.BOLD}── Phase 3: Secure Chat ──{D.RESET}")
    coins = session.coin_status()
    print(f"  Coins available: {_coin_bar(coins)}")
    print()
    print(f"  {D.DIM}╭─────────────────────────────────────────────────────╮")
    print(f"  │  Device scenarios:                                   │")
    print(f"  │    {D.BOLD}1{D.DIM} = Home WiFi, full battery      → {D.YELLOW}GOLD{D.DIM}          │")
    print(f"  │    {D.BOLD}2{D.DIM} = Outdoor cellular              → {D.WHITE}SILVER{D.DIM}        │")
    print(f"  │    {D.BOLD}3{D.DIM} = Underground, critical battery → {D.ORANGE}BRONZE{D.DIM}        │")
    print(f"  │                                                       │")
    print(f"  │  Type a message to send (default scenario: 1)         │")
    print(f"  │  Prefix with 1/2/3 + space to pick scenario           │")
    print(f"  │  Commands: /status  /quit                              │")
    print(f"  ╰─────────────────────────────────────────────────────╯{D.RESET}")
    print()

    # Start listener
    def on_receive(**kwargs):
        # Print above the input prompt
        print(f"\r{CLEAR_LINE}", end="")
        _print_msg_recv(
            partner=partner,
            text=kwargs["plaintext"],
            tier=kwargs["tier"],
            verified=kwargs["verified"],
            key_id=kwargs["key_id"],
            burned=kwargs["burned"],
            ctx_label=kwargs.get("device_context", ""),
        )
        # Reprint prompt
        coins = session.coin_status()
        prompt = (f"  [{_coin_bar(coins)}] "
                  f"{D.BOLD}{user}>{D.RESET} ")
        print(prompt, end="", flush=True)

    session.start_listening(on_receive)
    await asyncio.sleep(0.1)

    # Input loop — use a thread so incoming messages can print in real time
    input_q: Queue = Queue()
    prompt_str = (f"  [{_coin_bar(coins)}] "
                  f"{D.BOLD}{user}>{D.RESET} ")

    input_th = threading.Thread(
        target=_input_thread, args=(input_q, prompt_str), daemon=True,
    )
    input_th.start()

    try:
        while True:
            try:
                line = input_q.get(timeout=0.2)
            except Empty:
                continue

            if line is None:
                break

            line = line.strip()
            if not line:
                # Reprint prompt with updated coins
                coins = session.coin_status()
                prompt_str = (f"  [{_coin_bar(coins)}] "
                              f"{D.BOLD}{user}>{D.RESET} ")
                continue

            if line.lower() == "/quit":
                break

            if line.lower() == "/status":
                coins = session.coin_status()
                print(f"  {D.BOLD}Coins remaining:{D.RESET} {_coin_bar(coins)}")
                vs = session.vault.get_stats()
                print(f"  {D.BOLD}Vault:{D.RESET} "
                      f"{D.YELLOW}G:{vs.active_gold}{D.RESET} "
                      f"{D.WHITE}S:{vs.active_silver}{D.RESET} "
                      f"{D.ORANGE}B:{vs.active_bronze}{D.RESET}  "
                      f"burned={vs.total_burned}")
                continue

            # Parse scenario prefix
            scenario_key = "1"
            text = line
            if len(line) > 2 and line[0] in SCENARIOS and line[1] == " ":
                scenario_key = line[0]
                text = line[2:]

            ctx_label, ctx, expected_tier = SCENARIOS[scenario_key]
            msg = session.send_message(text, ctx)

            if msg:
                _print_msg_sent(user, text, msg.coin_tier, msg.key_id, ctx_label)

                # Show fallback if it happened
                if msg.coin_tier != expected_tier:
                    print(f"           {D.YELLOW}! wanted {expected_tier} "
                          f"→ fell back to {msg.coin_tier}{D.RESET}")
            else:
                print(f"  {D.RED}✗ No coins available — all tiers exhausted{D.RESET}")

    except KeyboardInterrupt:
        pass

    finally:
        print(f"\n  {D.DIM}Cleaning up…{D.RESET}")
        session.cleanup_user_data()
        await session.cleanup_server_data()
        await session.teardown()

    print(f"\n  {D.GREEN}{D.BOLD}Session ended.{D.RESET}\n")


# ─── Demo-pair launcher ───

def _find_terminal() -> tuple[str, list[str]] | None:
    """Detect available terminal emulator and return (name, command_template).

    The template uses {title} and {cmd} placeholders.
    """
    # Prefer tmux if we're already inside a tmux session
    if os.environ.get("TMUX") and shutil.which("tmux"):
        return ("tmux", [])  # handled specially

    candidates = [
        ("gnome-terminal", ["gnome-terminal", "--title={title}", "--", "bash", "-c", "{cmd}; exec bash"]),
        ("konsole", ["konsole", "--new-tab", "-p", "tabtitle={title}", "-e", "bash", "-c", "{cmd}; exec bash"]),
        ("xfce4-terminal", ["xfce4-terminal", "--title={title}", "-e", "bash -c '{cmd}; exec bash'"]),
        ("xterm", ["xterm", "-title", "{title}", "-e", "bash -c '{cmd}; exec bash'"]),
    ]

    for name, template in candidates:
        if shutil.which(name):
            return (name, template)

    return None


def launch_demo_pair(priority: str) -> None:
    """Open two terminal windows running alice and bob."""
    python = sys.executable
    module = "AQM_Database.chat.cli"

    alice_cmd = f"{python} -m {module} --user alice --partner bob --priority {priority}"
    bob_cmd = f"{python} -m {module} --user bob --partner alice --priority {priority}"

    terminal = _find_terminal()
    if terminal is None:
        print(f"{D.RED}Error: No supported terminal emulator found.{D.RESET}")
        print(f"  Supported: gnome-terminal, konsole, xfce4-terminal, xterm, tmux")
        print(f"\n  Run manually in two terminals:")
        print(f"    Terminal 1: {alice_cmd}")
        print(f"    Terminal 2: {bob_cmd}")
        sys.exit(1)

    name, template = terminal

    print(f"""
{D.CYAN}{D.BOLD}
    ╔═══════════════════════════════════════════════════╗
    ║     AQM Chat — Launching Demo Pair                ║
    ╚═══════════════════════════════════════════════════╝
{D.RESET}""")
    print(f"  {D.BOLD}Priority:{D.RESET} {priority}")
    print(f"  {D.BOLD}Terminal:{D.RESET} {name}\n")

    if name == "tmux":
        # Split current tmux window into two panes
        print(f"  {D.GREEN}✓{D.RESET} Splitting tmux window into two panes…\n")
        subprocess.Popen(["tmux", "split-window", "-h", bob_cmd])
        # Run alice in the current pane
        os.execvp(python, [python, "-m", module,
                           "--user", "alice", "--partner", "bob",
                           "--priority", priority])
    else:
        # Launch two separate terminal windows
        for label, cmd, title in [
            ("Alice", alice_cmd, f"AQM Chat — Alice ({priority})"),
            ("Bob", bob_cmd, f"AQM Chat — Bob ({priority})"),
        ]:
            filled = [
                arg.format(title=title, cmd=cmd) for arg in template
            ]
            subprocess.Popen(filled)
            print(f"  {D.GREEN}✓{D.RESET} Launched {label}: {D.DIM}{cmd}{D.RESET}")
            time.sleep(0.3)

        print(f"\n  {D.DIM}Both terminals launched. Switch to them to chat.{D.RESET}\n")


# ─── Arg parsing ───

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AQM Chat Demo — Terminal-to-Terminal + Benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Launch both terminals automatically
  python -m AQM_Database.chat.cli --demo-pair
  python -m AQM_Database.chat.cli --demo-pair --priority MATE

  # Two-terminal interactive chat (manual)
  Terminal 1: python -m AQM_Database.chat.cli --user alice --partner bob --priority BESTIE
  Terminal 2: python -m AQM_Database.chat.cli --user bob --partner alice --priority BESTIE

  # Auto demo (all priorities, single terminal)
  python -m AQM_Database.chat.cli --auto

  # Benchmark only
  python -m AQM_Database.chat.cli --benchmark
        """,
    )
    parser.add_argument("--user", help="This user's name (e.g., alice)")
    parser.add_argument("--partner", help="Partner's name (e.g., bob)")
    parser.add_argument(
        "--priority",
        choices=["BESTIE", "MATE", "STRANGER"],
        default="BESTIE",
        help="Priority level for the partner (default: BESTIE)",
    )
    parser.add_argument(
        "--demo-pair",
        action="store_true",
        help="Launch two terminals (alice + bob) automatically",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Run auto demo (all 3 priorities, single terminal)",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Run AQM vs TLS 1.3 benchmark only",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=50,
        help="Benchmark iterations per tier (default: 50)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.demo_pair:
        launch_demo_pair(args.priority)
        return

    if args.auto:
        asyncio.run(run_auto_demo())
        return

    if args.benchmark:
        asyncio.run(run_benchmark(iterations=args.iterations))
        return

    if not args.user or not args.partner:
        print(f"{D.RED}Error: --user and --partner are required "
              f"for interactive mode{D.RESET}")
        print(f"\n  Quick start:")
        print(f"    python -m AQM_Database.chat.cli --demo-pair")
        print(f"\n  Or run manually in two terminals:")
        print(f"    Terminal 1: python -m AQM_Database.chat.cli --user alice --partner bob")
        print(f"    Terminal 2: python -m AQM_Database.chat.cli --user bob --partner alice")
        sys.exit(1)

    asyncio.run(interactive_chat(args.user, args.partner, args.priority))


if __name__ == "__main__":
    main()
