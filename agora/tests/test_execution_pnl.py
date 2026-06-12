"""Unit tests for the execution + P&L money path.

Covers the SIGNED net computations in trading_platform/services/ibkr_client.py
(`place_legs_individually`, `close_position`, `close_position_legs`, and the
`_net_fill_per_share` helper) and the close-P&L formula + entry_price selection
in agora/session.py.

The IBKR functions need a live `ib` connection, so the *arithmetic* under test is
extracted/replicated here EXACTLY as written in the source and asserted against
the real bugs that were fixed (credit-spread sign flips, fake-gain P&L). The
helper logic is exercised against fake trade/fill objects built with
SimpleNamespace — no network, no ib_insync live calls.
"""
import types

import pytest


# ── Fake broker objects (mirror ib_insync's Fill / Execution / Contract / Order) ──

def _fill(conid, shares, price, side=None):
    """Build a fake Fill: .contract.conId, .execution.shares/.price/.side."""
    return types.SimpleNamespace(
        contract=types.SimpleNamespace(conId=conid),
        execution=types.SimpleNamespace(shares=float(shares), price=float(price), side=side),
    )


def _trade(fills, action=None):
    """Build a fake Trade: .fills (list) and .order.action."""
    return types.SimpleNamespace(
        fills=list(fills),
        order=types.SimpleNamespace(action=action),
    )


# ── Replicated helpers (byte-faithful to the source arithmetic) ──────────────────

def _net_fill_per_share(trade, sign_by_conid, signed=False):
    """EXACT replica of the closure in ibkr_client.place_bracket_order (line 603).

    Groups fills by conId, weights price by shares, then sums signed leg averages
    (BUY=+1 / SELL=-1 from `sign_by_conid`). signed=True -> +debit/-credit;
    signed=False -> magnitude.
    """
    by_conid: dict = {}
    for f in trade.fills:
        cid = getattr(getattr(f, "contract", None), "conId", None)
        sh = float(getattr(f.execution, "shares", 0) or 0)
        if cid is None or sh <= 0:
            continue
        acc = by_conid.setdefault(cid, [0.0, 0.0])
        acc[0] += float(f.execution.price) * sh
        acc[1] += sh
    net = 0.0
    for cid, (pxsum, shsum) in by_conid.items():
        if shsum > 0:
            net += sign_by_conid.get(cid, +1) * (pxsum / shsum)
    return round(net, 4) if signed else round(abs(net), 4)


def _net_entry_legwise(filled_trades):
    """EXACT replica of place_legs_individually's net entry (line 1083-1099).

    Returns (net_fill_price_abs, net_entry_signed). Sign by each trade's own action.
    """
    all_fills = []
    net = 0.0
    for trade in filled_trades:
        sign = +1 if trade.order.action.upper() == "BUY" else -1
        for f in trade.fills:
            all_fills.append(f)
            net += sign * float(f.execution.price)
    net_fill_price = round(abs(net), 4) if all_fills else None
    net_entry_signed = round(net, 4) if all_fills else None
    return net_fill_price, net_entry_signed


def _net_close_signed_by_side(trade):
    """EXACT replica of close_position's signed net close (line 1261-1265).

    Sums sign(side)*price where BOT=+1, else (SLD) -1.
    """
    if not trade.fills:
        return None
    return round(
        sum((+1 if f.execution.side == "BOT" else -1) * float(f.execution.price)
            for f in trade.fills),
        4,
    )


def _net_close_signed_legwise(trades):
    """EXACT replica of close_position_legs' signed net close (line 1395-1434).

    Sign by each close trade's own action (BUY=+1 / SELL=-1).
    """
    net = 0.0
    any_fill = False
    for t in trades:
        sign = +1 if t.order.action.upper() == "BUY" else -1
        for f in t.fills:
            any_fill = True
            net += sign * float(f.execution.price)
    return round(net, 4) if any_fill else None


def pnl(entry_signed, net_close_signed, contracts):
    """The close P&L formula from session.py line 4592-4593.

    realized_pnl = -(entry_signed + net_close_signed) * 100 * contracts
    entry_signed: signed per-share net (debit>0, credit<0).
    """
    return round(-(entry_signed + net_close_signed) * 100 * contracts, 2)


def _select_entry_price(net_entry_signed, signed_mid, fill_price, is_multi_leg):
    """EXACT replica of _record_position's entry_price selection (session.py 4264-4279)."""
    if net_entry_signed not in (None, 0) and (
        signed_mid == 0 or (net_entry_signed < 0) == (signed_mid < 0)
    ):
        return round(float(net_entry_signed), 4)   # real signed fill (source of truth)
    elif is_multi_leg:
        return signed_mid                            # fall back to intended mid
    else:
        return fill_price if fill_price > 0 else abs(signed_mid)


# ── Invariant 1: P&L formula self-consistency ────────────────────────────────────

class TestPnlFormula:
    def test_credit_spread_win(self):
        # Entry = -3.85 (received credit), close net = +1.50 (paid to flatten) -> +235
        assert pnl(-3.85, 1.50, 1) == 235.0

    def test_long_put_loss(self):
        # Entry = +12.85 (debit paid), close net = -10.50 (received to sell) -> -235
        assert pnl(12.85, -10.50, 1) == -235.0

    def test_credit_spread_expires_worthless(self):
        # Credit spread held to expiry, close costs 0 -> keep the full credit -> +385
        assert pnl(-3.85, 0.0, 1) == 385.0

    def test_long_call_win(self):
        # Long debit +5.00, sold for +8.00 -> close net -8.00 -> +300
        assert pnl(5.00, -8.00, 1) == 300.0

    def test_scales_with_contracts(self):
        assert pnl(-3.85, 1.50, 4) == 940.0
        assert pnl(-3.85, 1.50, 4) == pnl(-3.85, 1.50, 1) * 4

    def test_breakeven_is_zero(self):
        # Closing at exactly the entry net -> 0 P&L for both conventions.
        assert pnl(-3.85, 3.85, 1) == 0.0
        assert pnl(12.85, -12.85, 1) == 0.0

    def test_credit_spread_full_loss(self):
        # Width 5 credit spread, received -3.85, must pay back +5.00 to close -> -115
        assert pnl(-3.85, 5.00, 1) == -115.0

    def test_no_double_negation_on_credit(self):
        # Regression: the OLD (close - entry) formula booked credit closes as huge fake
        # gains (e.g. MKSI +$1,005 vs real -$235). A credit spread that LOST money must
        # produce a NEGATIVE P&L, not a fake gain.
        loss = pnl(-2.00, 4.35, 1)   # received 2.00, paid 4.35 to close
        assert loss < 0
        assert loss == -235.0


# ── Invariant 2: _net_fill_per_share signed vs abs ───────────────────────────────

class TestNetFillPerShare:
    def test_credit_spread_signed_is_negative(self):
        # SELL higher-premium short (conid 1, premium 5.00), BUY lower long (conid 2, 1.15).
        # Net received = -(5.00) + (1.15) = -3.85 -> signed negative (credit).
        sign_by_conid = {1: -1, 2: +1}
        trade = _trade([_fill(1, 1, 5.00), _fill(2, 1, 1.15)])
        signed = _net_fill_per_share(trade, sign_by_conid, signed=True)
        magnitude = _net_fill_per_share(trade, sign_by_conid, signed=False)
        assert signed == -3.85
        assert magnitude == 3.85
        assert magnitude == abs(signed)

    def test_debit_spread_signed_is_positive(self):
        # BUY long (conid 1, 7.00), SELL short (conid 2, 2.00) -> +7.00 -2.00 = +5.00 debit.
        sign_by_conid = {1: +1, 2: -1}
        trade = _trade([_fill(1, 1, 7.00), _fill(2, 1, 2.00)])
        assert _net_fill_per_share(trade, sign_by_conid, signed=True) == 5.00
        assert _net_fill_per_share(trade, sign_by_conid, signed=False) == 5.00

    def test_long_single_leg_positive(self):
        sign_by_conid = {1: +1}
        trade = _trade([_fill(1, 1, 12.85)])
        assert _net_fill_per_share(trade, sign_by_conid, signed=True) == 12.85
        assert _net_fill_per_share(trade, sign_by_conid, signed=False) == 12.85

    def test_share_weighted_average_across_partial_fills(self):
        # Same conid filled twice at different prices/sizes -> share-weighted avg, not sum.
        # (10*4 + 14*1)/5 = 10.8 for the long leg; short leg 3.00 -> signed = 10.8 - 3.00.
        sign_by_conid = {1: +1, 2: -1}
        trade = _trade([_fill(1, 4, 10.00), _fill(1, 1, 14.00), _fill(2, 5, 3.00)])
        assert _net_fill_per_share(trade, sign_by_conid, signed=True) == 7.8

    def test_zero_and_missing_shares_skipped(self):
        # A fill with 0 shares or no conId must not corrupt the net.
        sign_by_conid = {1: +1}
        good = _fill(1, 2, 6.00)
        zero = _fill(1, 0, 999.0)
        no_conid = types.SimpleNamespace(
            contract=types.SimpleNamespace(conId=None),
            execution=types.SimpleNamespace(shares=5, price=999.0, side=None),
        )
        trade = _trade([good, zero, no_conid])
        assert _net_fill_per_share(trade, sign_by_conid, signed=True) == 6.00

    def test_empty_fills_is_zero(self):
        assert _net_fill_per_share(_trade([]), {}, signed=True) == 0.0
        assert _net_fill_per_share(_trade([]), {}, signed=False) == 0.0


# ── place_legs_individually: leg-wise signed entry net ───────────────────────────

class TestLegwiseEntryNet:
    def test_credit_spread_legwise_negative(self):
        sell = _trade([_fill(1, 1, 5.00)], action="SELL")
        buy = _trade([_fill(2, 1, 1.15)], action="BUY")
        net_abs, net_signed = _net_entry_legwise([sell, buy])
        assert net_signed == -3.85      # credit -> negative
        assert net_abs == 3.85          # magnitude for slippage tracking

    def test_debit_spread_legwise_positive(self):
        buy = _trade([_fill(1, 1, 7.00)], action="BUY")
        sell = _trade([_fill(2, 1, 2.00)], action="SELL")
        net_abs, net_signed = _net_entry_legwise([buy, sell])
        assert net_signed == 5.00
        assert net_abs == 5.00

    def test_partial_fill_only_long_leg_filled(self):
        # If only the BUY leg of a credit spread fills, signed flips POSITIVE (a naked-ish
        # debit). This is exactly the condition the session-level guard must catch.
        buy = _trade([_fill(2, 1, 1.15)], action="BUY")
        _, net_signed = _net_entry_legwise([buy])
        assert net_signed == 1.15
        assert net_signed > 0

    def test_no_fills_returns_none(self):
        assert _net_entry_legwise([]) == (None, None)


# ── close_position / close_position_legs: signed net close ───────────────────────

class TestNetCloseSigned:
    def test_close_by_side_bot_positive_sld_negative(self):
        # Closing a credit spread: BUY back the short (BOT 1.20 paid), SELL the long
        # (SLD 0.30 received) -> net = +1.20 - 0.30 = +0.90 paid to flatten.
        trade = _trade([_fill(1, 1, 1.20, side="BOT"), _fill(2, 1, 0.30, side="SLD")])
        assert _net_close_signed_by_side(trade) == 0.90

    def test_close_by_side_net_credit_negative(self):
        # Closing a long: SELL it (SLD, receive 8.00) -> net = -8.00 (credit received).
        trade = _trade([_fill(1, 1, 8.00, side="SLD")])
        assert _net_close_signed_by_side(trade) == -8.00

    def test_close_by_side_empty_none(self):
        assert _net_close_signed_by_side(_trade([])) is None

    def test_close_legwise_sign_by_action(self):
        # Leg-by-leg close: BUY to cover short (pay 1.50), SELL the long (receive 0.10).
        buy = _trade([_fill(1, 1, 1.50)], action="BUY")
        sell = _trade([_fill(2, 1, 0.10)], action="SELL")
        assert _net_close_signed_legwise([buy, sell]) == 1.40

    def test_close_legwise_preserves_sign_not_abs(self):
        # Regression: the OLD code stored abs(avg_price), losing the sign and making the
        # (close-entry) formula book credit-spread closes as huge fake gains. A net CREDIT
        # received to close must stay NEGATIVE.
        sell_long = _trade([_fill(1, 1, 9.00)], action="SELL")
        buy_short = _trade([_fill(2, 1, 1.00)], action="BUY")
        net = _net_close_signed_legwise([sell_long, buy_short])
        assert net == -8.00
        assert net < 0

    def test_close_legwise_empty_none(self):
        assert _net_close_signed_legwise([]) is None


# ── Invariant 3: entry_price selection sign-agreement guard ─────────────────────

class TestEntryPriceSelection:
    def test_uses_real_fill_when_sign_agrees_credit(self):
        # Intended credit mid = -3.85; real fill = -3.90 (also credit) -> use real fill.
        signed_mid = -3.85
        out = _select_entry_price(net_entry_signed=-3.90, signed_mid=signed_mid,
                                  fill_price=3.90, is_multi_leg=True)
        assert out == -3.90

    def test_uses_real_fill_when_sign_agrees_debit(self):
        out = _select_entry_price(net_entry_signed=5.10, signed_mid=5.00,
                                  fill_price=5.10, is_multi_leg=True)
        assert out == 5.10

    def test_falls_back_when_sign_disagrees(self):
        # Real fill flipped POSITIVE (partial filled only the long leg) but the strategy
        # is a CREDIT spread (signed_mid < 0). The guard must REJECT the flipped fill and
        # fall back to the intended mid so the recorded sign matches the strategy label.
        signed_mid = -3.85
        out = _select_entry_price(net_entry_signed=1.15, signed_mid=signed_mid,
                                  fill_price=1.15, is_multi_leg=True)
        assert out == signed_mid          # NOT 1.15 — sign protected
        assert out < 0

    def test_falls_back_when_debit_fill_flips_negative(self):
        # Debit intent (positive) but fill came back negative -> reject, keep debit mid.
        signed_mid = 5.00
        out = _select_entry_price(net_entry_signed=-1.20, signed_mid=signed_mid,
                                  fill_price=1.20, is_multi_leg=True)
        assert out == 5.00
        assert out > 0

    def test_none_signed_falls_back_multileg(self):
        out = _select_entry_price(net_entry_signed=None, signed_mid=-3.85,
                                  fill_price=3.85, is_multi_leg=True)
        assert out == -3.85

    def test_zero_signed_falls_back_multileg(self):
        # net_entry_signed == 0 is treated as "no usable fill" by the `not in (None, 0)` guard.
        out = _select_entry_price(net_entry_signed=0, signed_mid=-3.85,
                                  fill_price=3.85, is_multi_leg=True)
        assert out == -3.85

    def test_single_leg_uses_fill_price(self):
        # Single-leg long with a positive fill -> use the fill price directly.
        out = _select_entry_price(net_entry_signed=0, signed_mid=12.85,
                                  fill_price=12.90, is_multi_leg=False)
        assert out == 12.90

    def test_single_leg_no_fill_uses_abs_mid(self):
        out = _select_entry_price(net_entry_signed=None, signed_mid=12.85,
                                  fill_price=0.0, is_multi_leg=False)
        assert out == 12.85


# ── End-to-end consistency: selected entry + signed close -> correct P&L ─────────

class TestEndToEndConsistency:
    def test_credit_spread_round_trip_win(self):
        # Entry: credit spread, real fill -3.85 (sign agrees) -> stored -3.85.
        entry = _select_entry_price(net_entry_signed=-3.85, signed_mid=-3.85,
                                    fill_price=3.85, is_multi_leg=True)
        # Close: pay +1.50 to flatten (from signed net close).
        close = _trade([_fill(1, 1, 1.50, side="BOT"), _fill(2, 1, 0.0, side="SLD")])
        net_close = _net_close_signed_by_side(close)
        assert pnl(entry, net_close, 1) == 235.0

    def test_long_put_round_trip_loss(self):
        entry = _select_entry_price(net_entry_signed=12.85, signed_mid=12.85,
                                    fill_price=12.85, is_multi_leg=False)
        # Sell the put to close -> receive 10.50 -> signed close -10.50.
        close = _trade([_fill(1, 1, 10.50, side="SLD")])
        net_close = _net_close_signed_by_side(close)
        assert pnl(entry, net_close, 1) == -235.0

    def test_guard_fallback_still_books_correct_sign(self):
        # Partial entry flipped the fill positive; guard kept the -3.85 credit mid. A
        # worthless expiry (close 0) must still book the full credit as a GAIN.
        entry = _select_entry_price(net_entry_signed=1.15, signed_mid=-3.85,
                                    fill_price=1.15, is_multi_leg=True)
        assert entry == -3.85
        assert pnl(entry, 0.0, 1) == 385.0
