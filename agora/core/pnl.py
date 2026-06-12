"""Single source of truth for the money formulas — pure, importable, no I/O.

Both production (agora/session.py) and the unit tests import these, so a change to the
P&L arithmetic CANNOT silently drift past a green replica test (the gap the C-suite test
review flagged on 2026-06-12). Sign convention throughout:
  entry_price / net_*  are SIGNED per-share nets: +debit paid / -credit received.
"""
from __future__ import annotations


def realized_pnl(entry_price_signed: float, net_close_signed: float, contracts: int) -> float:
    """Realized P&L in dollars from the signed entry net and the signed close net.

    A credit spread (entry negative) bought back cheaper, or a long/debit sold higher,
    both yield a profit. This is the ONLY correct close formula; the discredited
    (close_price - entry_price) form inverted credit-spread P&L and produced the
    +$2,693 -> -$3,248.50 restatement.
    """
    return round(-(entry_price_signed + net_close_signed) * 100 * contracts, 2)


def signed_mid_from_total(entry_debit_credit: float, contracts: int) -> float:
    """The intended SIGNED per-share net from the total credit/debit dollars."""
    return entry_debit_credit / max(1, contracts * 100)


def select_entry_price(net_entry_signed, signed_mid: float,
                       fill_price: float, is_multi_leg: bool) -> float:
    """Choose the recorded entry_price.

    Prefer the REAL signed fill (`net_entry_signed`) when its sign agrees with the intended
    structure (`signed_mid`); fall back to the intended mid when a partial fill flipped the
    sign (e.g. only one leg of a credit spread filled, making the net positive), so the
    recorded sign always matches the strategy label.
    """
    if net_entry_signed not in (None, 0) and (
        signed_mid == 0 or (net_entry_signed < 0) == (signed_mid < 0)
    ):
        return round(float(net_entry_signed), 4)
    if is_multi_leg:
        return signed_mid
    return fill_price if fill_price > 0 else abs(signed_mid)


def startup_sync_close(entry_price_signed: float, fill_price: float, contracts: int,
                       max_gain_dollars: float) -> tuple[float, float, bool]:
    """Compute a TWS startup-sync close from a BAG **SLD** (sell-to-close) fill.

    A SLD combo received `fill_price` as credit, so the signed net close cost is -fill_price.
    Returns (close_price, realized_pnl, should_book). Refuses to book a structurally-impossible
    close so it never writes an MKSI-class FAKE GAIN to the books on restart:
      - entry_price_signed < 0 (a CREDIT spread): its real close is a BOT, not a SLD — a SLD
        matched here is a structure/ticker mismatch, so DON'T book (this is the MKSI case).
      - pnl exceeds 1.2x the position's own max gain: impossible for a real close → DON'T book.
    Skipping is safe: the position healer / manual reconciliation handles it. Booking fiction
    is the harm we are guarding against.
    """
    net_close_signed = -float(fill_price)
    close_price = round(abs(net_close_signed), 4)
    pnl = realized_pnl(entry_price_signed, net_close_signed, contracts)
    mg = abs(max_gain_dollars or 0.0)
    structurally_sane = entry_price_signed >= 0          # SLD only legitimately closes debit/long
    within_max_gain   = not (mg > 0 and pnl > mg * 1.2)
    should_book = structurally_sane and within_max_gain
    return close_price, pnl, should_book
