"""Tests for the single-source-of-truth money formulas in agora/core/pnl.py.

These import the REAL production functions (which agora/session.py now calls), so a change
to the P&L arithmetic fails the build instead of silently drifting past a replica — the
coupling gap the C-suite test review flagged. Covers the close formula, the entry sign-guard,
and the TWS startup-sync close (the live bug the review caught).
"""
import pytest

from agora.core.pnl import (
    realized_pnl,
    signed_mid_from_total,
    select_entry_price,
    startup_sync_close,
)


# ── realized_pnl ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("entry,close,contracts,expected", [
    (-3.85,  1.50, 1,  235.0),   # credit spread: collected 3.85, bought back 1.50 → +235
    (12.85, -10.50, 1, -235.0),  # long put: paid 12.85, sold 10.50 → -235
    (-3.85,  0.0,  1,  385.0),   # credit spread expires worthless → keep full credit
    (-3.85,  1.50, 3,  705.0),   # scales with contracts
    (5.00,  -5.00, 2,    0.0),   # breakeven
])
def test_realized_pnl(entry, close, contracts, expected):
    assert realized_pnl(entry, close, contracts) == expected


def test_realized_pnl_credit_loss_is_negative():
    # The regression: a credit spread bought back for MORE than the credit is a LOSS,
    # not the old fake full-credit gain.
    assert realized_pnl(-3.85, 6.20, 1) == -235.0   # MKSI: real -235, not +385


# ── signed_mid_from_total ─────────────────────────────────────────────────────
@pytest.mark.parametrize("total,contracts,expected", [
    (-385.0, 1, -3.85),    # credit: total credit / (1*100)
    (1285.0, 1, 12.85),    # debit
    (-770.0, 2, -3.85),    # 2 contracts
    (0.0,    1,  0.0),
])
def test_signed_mid_from_total(total, contracts, expected):
    assert signed_mid_from_total(total, contracts) == pytest.approx(expected)


# ── select_entry_price (the partial-fill sign guard) ──────────────────────────
def test_entry_uses_real_fill_when_sign_agrees_credit():
    assert select_entry_price(-3.85, -3.80, 0.0, True) == -3.85


def test_entry_uses_real_fill_when_sign_agrees_debit():
    assert select_entry_price(12.85, 12.50, 0.0, False) == 12.85


def test_entry_falls_back_when_sign_flips_on_partial():
    # A partial filled only the long leg of a credit spread → net positive, disagrees with
    # the intended negative mid → fall back to the signed mid so the recorded sign is right.
    assert select_entry_price(2.0, -3.85, 0.0, True) == -3.85


def test_entry_none_multileg_uses_mid():
    assert select_entry_price(None, -1.47, 0.0, True) == -1.47


def test_entry_none_single_leg_uses_fill_then_mid():
    assert select_entry_price(None, 7.84, 7.90, False) == 7.90    # real fill
    assert select_entry_price(None, -7.84, 0.0, False) == 7.84    # no fill → abs(mid)


def test_entry_zero_signed_treated_as_no_fill():
    assert select_entry_price(0, -1.47, 0.0, True) == -1.47


# ── startup_sync_close (the live bug the C-suite caught) ──────────────────────
def test_startup_sync_long_books_correct_pnl():
    # Long/debit (entry +5.00) sold to close (SLD) at 7.00 → +$200, books.
    close_price, pnl, should_book = startup_sync_close(5.00, 7.00, 1, 0.0)
    assert should_book is True
    assert pnl == 200.0
    assert close_price == 7.0


def test_startup_sync_credit_spread_SLD_does_not_book_fake_gain():
    # MKSI-class: a credit spread (entry -3.85) is closed via a BOT, never a SLD. A SLD
    # matched here would imply a fat fake gain — the guard must REFUSE to book it.
    close_price, pnl, should_book = startup_sync_close(-3.85, 1.50, 1, 385.0)
    assert should_book is False


def test_startup_sync_debit_spread_within_max_gain_books():
    # Debit spread (entry +2.00, max gain $300) closed at 4.50 → +$250 ≤ max → books.
    close_price, pnl, should_book = startup_sync_close(2.00, 4.50, 1, 300.0)
    assert should_book is True
    assert pnl == 250.0


def test_startup_sync_pnl_above_max_gain_backstop_skips():
    # Even a debit position: a P&L far above its own max gain is impossible → skip.
    close_price, pnl, should_book = startup_sync_close(2.00, 12.00, 1, 300.0)
    assert pnl == 1000.0
    assert should_book is False     # 1000 > 1.2 * 300


def test_startup_sync_close_price_is_abs_magnitude():
    close_price, _, _ = startup_sync_close(5.00, 7.00, 1, 0.0)
    assert close_price >= 0
