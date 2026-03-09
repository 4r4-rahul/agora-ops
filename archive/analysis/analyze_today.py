#!/usr/bin/env python3
import csv

# Read CSV
with open('BATS_MSTR, 1-37.csv', 'r') as f:
    reader = csv.DictReader(f)
    data = list(reader)

# Find trades
trades = []
in_trade = False
entry_bar = None
entry_price = None
direction = None

for i, row in enumerate(data):
    bar_num = i + 1
    close = float(row['close'])
    
    # Check for entry signals
    if row['Buy Call marker'] == '1':
        if in_trade:
            print(f"ERROR: Already in trade at bar {bar_num}")
        else:
            in_trade = True
            direction = 'CALL'
            entry_bar = bar_num
            entry_price = close
            trades.append({
                'type': 'CALL',
                'entry_bar': bar_num,
                'entry_price': close,
                'entry_cci': float(row['CCI']),
                'entry_adx': float(row['ADX']),
                'entry_volume': float(row['Volume'])
            })
    
    elif row['Buy Put marker'] == '1':
        if in_trade:
            print(f"ERROR: Already in trade at bar {bar_num}")
        else:
            in_trade = True
            direction = 'PUT'
            entry_bar = bar_num
            entry_price = close
            trades.append({
                'type': 'PUT',
                'entry_bar': bar_num,
                'entry_price': close,
                'entry_cci': float(row['CCI']),
                'entry_adx': float(row['ADX']),
                'entry_volume': float(row['Volume'])
            })
    
    # Check for exit signals
    elif row['Sell Call marker'] == '1' and in_trade and direction == 'CALL':
        pnl = close - entry_price
        pnl_pct = (pnl / entry_price) * 100
        bars_held = bar_num - entry_bar
        trades[-1].update({
            'exit_bar': bar_num,
            'exit_price': close,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'bars_held': bars_held,
            'result': 'WIN' if pnl > 0 else 'LOSS'
        })
        in_trade = False
    
    elif row['Sell Put marker'] == '1' and in_trade and direction == 'PUT':
        pnl = entry_price - close
        pnl_pct = (pnl / entry_price) * 100
        bars_held = bar_num - entry_bar
        trades[-1].update({
            'exit_bar': bar_num,
            'exit_price': close,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'bars_held': bars_held,
            'result': 'WIN' if pnl > 0 else 'LOSS'
        })
        in_trade = False

# Print analysis
print(f"\n=== February 9, 2026 - MSTR Trading Analysis ===\n")
print(f"Total bars in session: {len(data)} (≈{len(data)//60:.1f} hours)\n")

# Market characteristics
print("=== MARKET CHARACTERISTICS ===")
open_price = float(data[0]['open'])
close_price = float(data[-1]['close'])
high_price = max(float(r['high']) for r in data)
low_price = min(float(r['low']) for r in data)
range_move = high_price - low_price
range_pct = (range_move / open_price) * 100

print(f"Open:  ${open_price:.2f}")
print(f"High:  ${high_price:.2f}")
print(f"Low:   ${low_price:.2f}")
print(f"Close: ${close_price:.2f}")
print(f"Range: ${range_move:.2f} ({range_pct:.2f}%)")
print(f"Daily: ${close_price - open_price:.2f} ({(close_price/open_price-1)*100:.2f}%)")

avg_volume = sum(float(r['Volume']) for r in data) / len(data)
avg_cci = sum(float(r['CCI']) for r in data) / len(data)
avg_adx = sum(float(r['ADX']) for r in data) / len(data)

print(f"\nAvg Volume: {avg_volume:.0f}")
print(f"Avg CCI: {avg_cci:.1f}")
print(f"Avg ADX: {avg_adx:.2f}")

# Count volatility spikes
high_vol_bars = sum(1 for r in data if float(r['Volume']) > 20000)
extreme_cci_bars = sum(1 for r in data if abs(float(r['CCI'])) > 150)

print(f"\nHigh volume bars (>20k): {high_vol_bars} ({high_vol_bars/len(data)*100:.1f}%)")
print(f"Extreme CCI bars (|CCI|>150): {extreme_cci_bars} ({extreme_cci_bars/len(data)*100:.1f}%)")

# Trade analysis
print(f"\n=== TRADE ANALYSIS ===")
print(f"Total trades: {len(trades)}")

closed_trades = [t for t in trades if 'exit_price' in t]
open_trades = [t for t in trades if 'exit_price' not in t]

print(f"Closed trades: {len(closed_trades)}")
print(f"Still open: {len(open_trades)}")

if closed_trades:
    wins = [t for t in closed_trades if t['pnl'] > 0]
    losses = [t for t in closed_trades if t['pnl'] <= 0]
    
    print(f"\nWINS: {len(wins)} ({len(wins)/len(closed_trades)*100:.1f}%)")
    print(f"LOSSES: {len(losses)} ({len(losses)/len(closed_trades)*100:.1f}%)")
    
    total_pnl = sum(t['pnl'] for t in closed_trades)
    total_pnl_pct = sum(t['pnl_pct'] for t in closed_trades)
    avg_pnl = total_pnl / len(closed_trades)
    
    print(f"\nTotal P/L: ${total_pnl:.2f} ({total_pnl_pct:.2f}%)")
    print(f"Avg P/L: ${avg_pnl:.2f}")
    
    if wins:
        avg_win = sum(t['pnl'] for t in wins) / len(wins)
        print(f"Avg WIN: ${avg_win:.2f}")
    if losses:
        avg_loss = sum(t['pnl'] for t in losses) / len(losses)
        print(f"Avg LOSS: ${avg_loss:.2f}")
    
    # Detailed trade list
    print(f"\n=== DETAILED TRADES ===")
    for i, t in enumerate(closed_trades, 1):
        result_symbol = "✓" if t['result'] == 'WIN' else "✗"
        print(f"\nTrade {i} ({t['type']}): {result_symbol} {t['result']}")
        print(f"  Entry: Bar {t['entry_bar']} @ ${t['entry_price']:.2f}")
        print(f"  Exit:  Bar {t['exit_bar']} @ ${t['exit_price']:.2f}")
        print(f"  P/L: ${t['pnl']:.2f} ({t['pnl_pct']:.2f}%) in {t['bars_held']} bars")
        print(f"  Entry conditions: CCI={t['entry_cci']:.1f}, ADX={t['entry_adx']:.2f}, Vol={t['entry_volume']:.0f}")

if open_trades:
    print(f"\n=== OPEN TRADES ===")
    for t in open_trades:
        current_price = float(data[-1]['close'])
        if t['type'] == 'CALL':
            unrealized = current_price - t['entry_price']
        else:
            unrealized = t['entry_price'] - current_price
        unrealized_pct = (unrealized / t['entry_price']) * 100
        print(f"  {t['type']} entry @ ${t['entry_price']:.2f}, current ${current_price:.2f}")
        print(f"    Unrealized: ${unrealized:.2f} ({unrealized_pct:.2f}%)")
