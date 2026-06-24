#!/usr/bin/env python
"""
scripts/preflight.py — AGORA live-trading readiness check.

Validates configuration, port alignment, position-sizing math, IBKR
connectivity, and database integrity before flipping TRADING_MODE=live.

Exit codes:
  0 — all checks passed (green to launch)
  1 — one or more checks failed (do not launch)

Usage:
  python scripts/preflight.py
  python scripts/preflight.py --mode live   # override trading_mode for check
"""

from __future__ import annotations

import argparse
import os
import socket
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agora.core.config import AgoraSettings

sys.path.insert(0, str(Path(__file__).parent.parent))

PASS_MARK = "  [PASS]"  # noqa: S105 — display label, not a credential
FAIL_MARK = "  [FAIL]"
WARN_MARK = "  [WARN]"
INFO_MARK = "  [INFO]"

_results: list[tuple[str, str, str]] = []   # (level, name, detail)


def _record(level: str, name: str, detail: str = "") -> None:
    _results.append((level, name, detail))
    mark = {"PASS": PASS_MARK, "FAIL": FAIL_MARK, "WARN": WARN_MARK, "INFO": INFO_MARK}[level]
    line = f"{mark}  {name}"
    if detail:
        line += f"\n         {detail}"
    print(line)


def chk_pass(name: str, detail: str = "") -> None:
    _record("PASS", name, detail)


def chk_fail(name: str, detail: str = "") -> None:
    _record("FAIL", name, detail)


def chk_warn(name: str, detail: str = "") -> None:
    _record("WARN", name, detail)


def chk_info(name: str, detail: str = "") -> None:
    _record("INFO", name, detail)


# ── 1. Settings load ──────────────────────────────────────────────────────────

def check_settings() -> AgoraSettings | None:
    print("\n  Settings & Environment")
    print("  " + "─" * 50)

    env_file = Path(".env")
    if env_file.exists():
        chk_pass(".env file found", str(env_file.resolve()))
    else:
        chk_warn(".env file not found", "Settings will fall back to environment variables")

    try:
        from agora.core.config import AgoraSettings
        s = AgoraSettings()
        chk_pass("AgoraSettings loaded")
        return s
    except Exception as exc:
        chk_fail("AgoraSettings failed to load", str(exc))
        return None


# ── 2. API key ────────────────────────────────────────────────────────────────

def check_api_key(s: AgoraSettings) -> None:
    print("\n  API Keys")
    print("  " + "─" * 50)

    key = s.anthropic_api_key
    if key and key.startswith("sk-ant-"):
        chk_pass("ANTHROPIC_API_KEY set", f"sk-ant-...{key[-6:]}")
    elif key:
        chk_warn("ANTHROPIC_API_KEY set but unusual prefix", f"starts with '{key[:10]}...'")
    else:
        chk_fail("ANTHROPIC_API_KEY missing or empty")


# ── 3. Mode / port alignment ──────────────────────────────────────────────────

def check_mode_port(s: AgoraSettings, mode_override: str | None) -> None:
    print("\n  Trading Mode & IBKR Port")
    print("  " + "─" * 50)

    mode = mode_override or s.trading_mode
    port = s.ibkr_port

    chk_info(f"trading_mode = {mode}", f"ibkr_port = {port}")

    if mode == "live" and port == 7496:
        chk_pass("Port matches live mode", "7496 = TWS live")
    elif mode == "live" and port == 7497:
        chk_fail("Port mismatch for live mode", "ibkr_port=7497 is paper TWS; live needs 7496")
    elif mode == "paper" and port == 7497:
        chk_pass("Port matches paper mode", "7497 = TWS paper")
    elif mode == "paper" and port == 7496:
        chk_warn("Paper mode using live port", "ibkr_port=7496 on paper mode — intentional?")
    else:
        chk_warn(f"Non-standard port {port}", "Expected 7496 (live) or 7497 (paper)")

    if mode == "live":
        chk_warn("TRADING_MODE=live", "Real money orders will be submitted to IBKR")


# ── 4. Position sizing vs AUM ─────────────────────────────────────────────────

def check_sizing(s: AgoraSettings) -> None:
    print("\n  Position Sizing vs AUM")
    print("  " + "─" * 50)

    aum = s.account_size
    max_risk = s.max_risk_per_trade          # account_size × max_position_size_pct
    daily_loss = s.daily_loss_limit_dollars  # account_size × daily_loss_limit_pct
    gross_exposure = s.max_open_positions * max_risk

    chk_info(f"account_size = ${aum:,.0f}")
    chk_info(f"max_risk_per_trade = ${max_risk:.0f}  ({s.max_position_size_pct*100:.1f}% of AUM)")
    chk_info(f"daily_loss_limit = ${daily_loss:.0f}  ({s.daily_loss_limit_pct*100:.1f}% of AUM)")
    chk_info(f"max_open_positions = {s.max_open_positions}  →  gross exposure = ${gross_exposure:,.0f}")

    # Sanity: max risk per trade ≤ 2% AUM
    if s.max_position_size_pct <= 0.02:
        chk_pass(f"Per-trade risk ≤ 2% AUM  (${max_risk:.0f})")
    else:
        chk_fail(f"Per-trade risk > 2% AUM  ({s.max_position_size_pct*100:.1f}%)",
                 "Reduce max_position_size_pct to ≤ 0.02")

    # Sanity: gross exposure ≤ 25% AUM
    gross_pct = gross_exposure / aum * 100
    if gross_pct <= 25:
        chk_pass(f"Gross exposure ≤ 25% AUM  (${gross_exposure:,.0f} = {gross_pct:.1f}%)")
    else:
        chk_warn(f"Gross exposure > 25% AUM  ({gross_pct:.1f}%)",
                 "Consider reducing max_open_positions or max_position_size_pct")

    # GTC combo limit ≤ max_open_positions
    if s.gtc_max_open_combo_orders <= s.max_open_positions:
        chk_pass(f"gtc_max_open_combo_orders ({s.gtc_max_open_combo_orders}) ≤ max_open_positions ({s.max_open_positions})")
    else:
        chk_fail(f"gtc_max_open_combo_orders ({s.gtc_max_open_combo_orders}) > max_open_positions ({s.max_open_positions})",
                 "Hard combo gate can never be reached — set gtc_max_open_combo_orders ≤ max_open_positions")

    # risk_per_trade_dollars vs max_risk_per_trade
    if s.risk_per_trade_dollars <= max_risk:
        chk_pass(f"risk_per_trade_dollars (${s.risk_per_trade_dollars:.0f}) ≤ max_risk_per_trade (${max_risk:.0f})")
    else:
        chk_fail(f"risk_per_trade_dollars (${s.risk_per_trade_dollars:.0f}) > max_risk_per_trade (${max_risk:.0f})",
                 "Sizing fields inconsistent — reduce risk_per_trade_dollars")


# ── 5. IBKR connectivity ──────────────────────────────────────────────────────

def check_ibkr(s: AgoraSettings) -> None:
    print("\n  IBKR TWS Connectivity")
    print("  " + "─" * 50)

    host = s.ibkr_host
    port = s.ibkr_port
    timeout = 3.0

    try:
        with socket.create_connection((host, port), timeout=timeout):
            chk_pass(f"TWS reachable at {host}:{port}")
    except TimeoutError:
        chk_fail(f"TWS not reachable at {host}:{port}", f"Connection timed out after {timeout:.0f}s — is TWS running?")
    except ConnectionRefusedError:
        chk_fail(f"TWS not reachable at {host}:{port}", "Connection refused — TWS may not be running or API not enabled")
    except OSError as exc:
        chk_fail(f"TWS connectivity error at {host}:{port}", str(exc))


# ── 6. Database ───────────────────────────────────────────────────────────────

_REQUIRED_TABLES = [
    "trade_records",
    "decision_chains",
    "pillar_pauses",
]


def check_database(s: AgoraSettings) -> None:
    print("\n  Database")
    print("  " + "─" * 50)

    db_path = Path(s.db_path)
    chk_info(f"db_path = {db_path.resolve()}")

    # Parent dir writable
    parent = db_path.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
            chk_pass(f"Created db directory: {parent}")
        except OSError as exc:
            chk_fail(f"Cannot create db directory: {parent}", str(exc))
            return
    else:
        if os.access(parent, os.W_OK):
            chk_pass(f"DB directory writable: {parent}")
        else:
            chk_fail(f"DB directory not writable: {parent}")
            return

    # File exists and can be opened
    if db_path.exists():
        chk_pass("DB file exists")
    else:
        chk_warn("DB file not found", "Will be created on first session start")
        return

    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            existing = {r[0] for r in rows}
    except sqlite3.Error as exc:
        chk_fail("Cannot open DB", str(exc))
        return

    chk_info(f"Tables found: {', '.join(sorted(existing)) or '(none)'}")

    for table in _REQUIRED_TABLES:
        if table in existing:
            try:
                count = sqlite3.connect(str(db_path)).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                chk_pass(f"Table '{table}' exists", f"{count} rows")
            except sqlite3.Error:
                chk_pass(f"Table '{table}' exists")
        else:
            chk_warn(f"Table '{table}' not yet created", "Will be created on first session start")


# ── 7. File system paths ──────────────────────────────────────────────────────

def check_paths(s: AgoraSettings) -> None:
    print("\n  File System Paths")
    print("  " + "─" * 50)

    paths_to_check = [
        ("iv_cache_dir",       s.iv_cache_dir,      True,  "IV cache"),
        ("audit_log_path",     s.audit_log_path,     False, "Audit log"),
        ("shadow_book_path",   s.shadow_book_path,   False, "Shadow book"),
    ]

    for _field, path, is_dir, label in paths_to_check:
        path = Path(path)
        parent = path if is_dir else path.parent
        if parent.exists():
            if os.access(parent, os.W_OK):
                chk_pass(f"{label} directory writable", str(parent))
            else:
                chk_fail(f"{label} directory not writable", str(parent))
        else:
            try:
                parent.mkdir(parents=True, exist_ok=True)
                chk_pass(f"Created {label} directory", str(parent))
            except OSError as exc:
                chk_fail(f"Cannot create {label} directory", str(exc))


# ── Summary ───────────────────────────────────────────────────────────────────

def _summary() -> int:
    failures = [r for r in _results if r[0] == "FAIL"]
    warnings = [r for r in _results if r[0] == "WARN"]
    passes   = [r for r in _results if r[0] == "PASS"]

    print("\n  " + "═" * 52)
    print("  PREFLIGHT SUMMARY")
    print(f"  {len(passes)} passed  |  {len(warnings)} warnings  |  {len(failures)} failed")
    print("  " + "═" * 52)

    if failures:
        print("\n  FAILURES (must fix before going live):")
        for _, name, detail in failures:
            print(f"    {FAIL_MARK}  {name}")
            if detail:
                print(f"           {detail}")
        print()
        return 1

    if warnings:
        print("\n  Warnings (review before going live):")
        for _, name, _ in warnings:
            print(f"    {WARN_MARK}  {name}")

    print("\n  All critical checks passed.  System is ready to launch.\n")
    return 0


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="AGORA pre-flight readiness check")
    parser.add_argument("--mode", choices=["paper", "live"], default=None,
                        help="Override trading_mode for port alignment check")
    args = parser.parse_args()

    print()
    print("  " + "═" * 52)
    print("  AGORA PRE-FLIGHT CHECK")
    print("  " + "═" * 52)

    s = check_settings()
    if s is None:
        print("\n  Cannot continue — settings failed to load.\n")
        sys.exit(1)

    check_api_key(s)
    check_mode_port(s, args.mode)
    check_sizing(s)
    check_ibkr(s)
    check_database(s)
    check_paths(s)

    sys.exit(_summary())


if __name__ == "__main__":
    main()
