"""
Output formatting utilities — rich terminal output for institutional-style reports.
"""

from datetime import datetime, date
from typing import List, Dict, Any, Optional


# ─────────────────────────────────────────────────────────────────────
# ANSI Color Codes
# ─────────────────────────────────────────────────────────────────────

class C:
    """ANSI color constants."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"


def header(title: str, subtitle: str = "", width: int = 78) -> str:
    """Format a section header."""
    lines = []
    lines.append(f"\n{C.BOLD}{C.CYAN}{'═' * width}{C.RESET}")
    lines.append(f"{C.BOLD}{C.WHITE}  {title}{C.RESET}")
    if subtitle:
        lines.append(f"{C.DIM}  {subtitle}{C.RESET}")
    lines.append(f"{C.CYAN}{'═' * width}{C.RESET}")
    return "\n".join(lines)


def sub_header(title: str, width: int = 78) -> str:
    """Format a sub-section header."""
    return f"\n{C.BOLD}{C.BLUE}{'─' * width}{C.RESET}\n{C.BOLD}  {title}{C.RESET}\n{C.BLUE}{'─' * width}{C.RESET}"


def verdict_badge(verdict: str) -> str:
    """Format a regime verdict badge."""
    if verdict.upper() == "GREEN":
        return f"{C.BG_GREEN}{C.BOLD}{C.WHITE}  ✅ GREEN — SELL PREMIUM  {C.RESET}"
    elif verdict.upper() == "YELLOW":
        return f"{C.BG_YELLOW}{C.BOLD}{C.WHITE}  ⚠️  YELLOW — SELL CONSERVATIVELY  {C.RESET}"
    elif verdict.upper() == "RED":
        return f"{C.BG_RED}{C.BOLD}{C.WHITE}  🛑 RED — SIT IN CASH  {C.RESET}"
    return f"{C.DIM}  {verdict}  {C.RESET}"


def kv(key: str, value: Any, color: str = "", width: int = 35) -> str:
    """Format a key-value pair."""
    v = f"{color}{value}{C.RESET}" if color else str(value)
    return f"  {C.DIM}{key:<{width}}{C.RESET} {v}"


def pnl(value: float, label: str = "") -> str:
    """Format a P&L value with color."""
    prefix = label + " " if label else ""
    if value > 0:
        return f"{prefix}{C.GREEN}+${value:,.2f}{C.RESET}"
    elif value < 0:
        return f"{prefix}{C.RED}-${abs(value):,.2f}{C.RESET}"
    return f"{prefix}${value:,.2f}"


def pct(value: float, label: str = "") -> str:
    """Format a percentage with color."""
    prefix = label + " " if label else ""
    if value > 0:
        return f"{prefix}{C.GREEN}+{value:.1f}%{C.RESET}"
    elif value < 0:
        return f"{prefix}{C.RED}{value:.1f}%{C.RESET}"
    return f"{prefix}{value:.1f}%"


def bar(value: float, max_val: float, width: int = 30, fill: str = "█", empty: str = "░") -> str:
    """Create a progress bar."""
    filled = int(width * min(value / max_val, 1.0)) if max_val > 0 else 0
    return f"{C.GREEN}{fill * filled}{C.DIM}{empty * (width - filled)}{C.RESET}"


def table(headers: List[str], rows: List[List[str]], col_widths: Optional[List[int]] = None) -> str:
    """Format a simple table."""
    if not col_widths:
        col_widths = [max(len(str(h)), max((len(str(r[i])) if i < len(r) else 0) for r in rows) if rows else 0) + 2
                      for i, h in enumerate(headers)]

    lines = []
    # Header
    hdr = "  "
    sep = "  "
    for i, h in enumerate(headers):
        w = col_widths[i] if i < len(col_widths) else 12
        hdr += f"{C.BOLD}{h:<{w}}{C.RESET}"
        sep += f"{'─' * w}"
    lines.append(hdr)
    lines.append(f"{C.DIM}{sep}{C.RESET}")

    # Rows
    for row in rows:
        line = "  "
        for i, cell in enumerate(row):
            w = col_widths[i] if i < len(col_widths) else 12
            line += f"{str(cell):<{w}}"
        lines.append(line)

    return "\n".join(lines)


def trade_ticket(ticket_data: Dict[str, Any]) -> str:
    """Format a complete trade ticket."""
    lines = []
    lines.append(header(
        f"📋 TRADE TICKET — {ticket_data.get('strategy', 'Credit Spread')}",
        f"{ticket_data.get('underlying', 'SPX')} | {ticket_data.get('date', date.today())}"
    ))

    lines.append(sub_header("POSITION"))
    lines.append(kv("Underlying", ticket_data.get("underlying", "SPX")))
    lines.append(kv("Expiration", ticket_data.get("expiration", date.today())))
    lines.append(kv("Strategy", ticket_data.get("strategy", "")))

    if "short_put" in ticket_data:
        lines.append(f"\n  {C.BOLD}Put Credit Spread:{C.RESET}")
        lines.append(kv("  Short Put", f"{ticket_data['short_put']} (Δ {ticket_data.get('short_put_delta', '')})", C.RED))
        lines.append(kv("  Long Put", f"{ticket_data['long_put']}", C.GREEN))

    if "short_call" in ticket_data:
        lines.append(f"\n  {C.BOLD}Call Credit Spread:{C.RESET}")
        lines.append(kv("  Short Call", f"{ticket_data['short_call']} (Δ {ticket_data.get('short_call_delta', '')})", C.RED))
        lines.append(kv("  Long Call", f"{ticket_data['long_call']}", C.GREEN))

    lines.append(sub_header("PRICING"))
    lines.append(kv("Credit per contract", f"${ticket_data.get('credit', 0):.2f}", C.GREEN))
    lines.append(kv("Max loss per contract", f"${ticket_data.get('max_loss', 0):.2f}", C.RED))
    lines.append(kv("Contracts", ticket_data.get("contracts", 1)))
    lines.append(kv("Total credit", f"${ticket_data.get('total_credit', 0):.2f}", C.GREEN))
    lines.append(kv("Total max loss", f"${ticket_data.get('total_max_loss', 0):.2f}", C.RED))
    lines.append(kv("Reward:Risk", f"1:{ticket_data.get('risk_reward', 0):.1f}"))

    lines.append(sub_header("PROBABILITY"))
    prob = ticket_data.get("prob_profit", 0)
    lines.append(kv("Probability of profit", f"{prob:.1f}%", C.GREEN if prob > 75 else C.YELLOW))
    lines.append(kv("Breakeven(s)", ticket_data.get("breakevens", "")))

    lines.append(sub_header("MANAGEMENT RULES"))
    lines.append(kv("Entry window", ticket_data.get("entry_window", "")))
    lines.append(kv("Stop-loss", ticket_data.get("stop_loss", "")))
    lines.append(kv("Profit target", ticket_data.get("profit_target", "")))
    lines.append(kv("Time exit", ticket_data.get("time_exit", "")))

    if "notes" in ticket_data:
        lines.append(f"\n  {C.DIM}📝 {ticket_data['notes']}{C.RESET}")

    lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
    return "\n".join(lines)


def theta_dashboard(data: Dict[str, Any]) -> str:
    """Format a theta decay dashboard."""
    lines = []
    lines.append(header("⏰ THETA DECAY DASHBOARD", "Hourly Income Projection"))

    lines.append(sub_header("PORTFOLIO THETA"))
    lines.append(kv("Daily portfolio theta", pnl(data.get("portfolio_theta", 0))))
    lines.append(kv("Theta-to-Delta ratio", f"{data.get('theta_delta_ratio', 0):.2f}"))

    if "hourly" in data:
        lines.append(sub_header("HOURLY DECAY CURVE"))
        max_decay = max(data["hourly"].values()) if data["hourly"] else 1
        for time_str, decay_val in data["hourly"].items():
            lines.append(f"  {time_str}  {bar(decay_val, max_decay, 25)}  ${decay_val:.2f}")

    lines.append(sub_header("INCOME PROJECTIONS"))
    lines.append(kv("Daily", pnl(data.get("daily", 0))))
    lines.append(kv("Weekly", pnl(data.get("weekly", 0))))
    lines.append(kv("Monthly", pnl(data.get("monthly", 0))))

    if "compounding" in data:
        lines.append(sub_header("COMPOUNDING PROJECTION"))
        for period, value in data["compounding"].items():
            lines.append(kv(period, f"${value:,.2f}"))

    lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
    return "\n".join(lines)


def risk_checklist(items: List[Dict[str, Any]]) -> str:
    """Format a risk management checklist."""
    lines = []
    lines.append(sub_header("🛡️ DAILY RISK CHECKLIST"))
    for item in items:
        status = item.get("status", "unknown")
        if status == "pass":
            icon = f"{C.GREEN}✅{C.RESET}"
        elif status == "warn":
            icon = f"{C.YELLOW}⚠️ {C.RESET}"
        elif status == "fail":
            icon = f"{C.RED}❌{C.RESET}"
        else:
            icon = "  "
        lines.append(f"  {icon} {item.get('label', '')}: {item.get('value', '')}")
    return "\n".join(lines)
