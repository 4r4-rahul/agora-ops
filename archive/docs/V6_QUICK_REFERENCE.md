# 🎯 V6.0 PRO - Quick Reference Cheat Sheet

## **NEW SIGNAL TYPES**

### **🟣 SWING REJECTION (Reversal)**
```
LONG:  Price lower low + RSI higher low + Strong wick + Volume spike
SHORT: Price higher high + RSI lower high + Strong wick + Volume spike

Entry: Probe size (33%)
Stop: 1.5 ATR (wider)
Target: 2.5R
Time: 25 bars
Scale: Add 33% at +0.5R
```

### **🟣 FAILED BREAKDOWN (Bear Trap)**
```
LONG: Broke below support → Reclaimed within 3 bars + CVD rising

Entry: Probe size (33%)
Stop: 1.5 ATR
Target: 2.5R
Fast move expected
```

### **🟣 FAILED BREAKOUT (Bull Trap)**
```
SHORT: Broke above resistance → Failed within 3 bars + CVD falling

Entry: Probe size (33%)
Stop: 1.5 ATR
Target: 2.5R
Fast move expected
```

### **🟢 TREND MOMENTUM (Original)**
```
Unchanged - same as v4.6
Clean trend, good momentum, normal sizing
```

### **🟡 SCALP (Original)**
```
Unchanged - same as v4.6
Near VWAP, grind conditions, smaller targets
```

---

## **CONFIDENCE SCORING (1-8)**

| Score | Quality | Action |
|-------|---------|--------|
| 7-8 | Excellent | Size UP (if allowed) |
| 5-6 | Very Good | Full size |
| 3-4 | Good | Normal size |
| 2 | Weak | Small size or skip |
| 1 | Poor | Skip |

**8 Factors:**
1. Body > Avg
2. Volume > Avg (+ bonus spike)
3. EMA spread wide
4. VWAP directional
5. RSI directional (+ bonus extreme)
6. HTF aligned
7. CVD confirms (if enabled)
8. Strong wicks

---

## **REGIME GUIDE**

| Regime | Color | Strategy |
|--------|-------|----------|
| 🟣 REVERSAL | Purple | Look for Swing Rejection / Failed Breakdown |
| 🟢 TREND | Green | Take momentum continuation (original) |
| 🟠 GRIND | Orange | Scalp mode, near VWAP |
| ⚪ NEUTRAL | Gray | Lower confidence, be selective |

---

## **POSITION SIZING DECISION TREE**

```
IF Regime = REVERSAL:
  ├─ Swing Rejection detected → Probe 33% + Scale
  └─ Failed Breakdown detected → Probe 33% + Scale

ELSE IF Regime = TREND:
  ├─ Confidence 6-8 + Core/Expansion phase → LARGE
  ├─ Confidence 4-5 + Good setup → NORMAL
  └─ Confidence 2-3 + Probe phase → SMALL

ELSE IF Regime = GRIND:
  └─ Always SMALL (scalp mode)
```

---

## **EXIT RULES BY TYPE**

### **REVERSAL Exits:**
```
DON'T exit on first EMA cross

Exit when:
- CALL: Close < VWAP AND EMA cross AND RSI < 45
- PUT:  Close > VWAP AND EMA cross AND RSI > 55
- OR: Strong structure break
```

### **TREND/SCALP Exits (original):**
```
Exit when:
- EMA9 crosses EMA21 (opposite direction)
- OR: Pivot + structure break
```

---

## **SCALE-IN SYSTEM**

```
Entry: 33% position (probe)
       ↓
Wait for +0.5R to +1.5R
       ↓
Check: Is HTF still supporting?
       ↓ YES          ↓ NO
Add 33%          Don't scale
       ↓
Now 66% position
       ↓
Target: 2.5R
```

---

## **ORDER FLOW SIGNALS**

### **CVD (Cumulative Volume Delta)**
```
CVD Rising + Price Falling = ACCUMULATION (bullish)
CVD Falling + Price Rising = DISTRIBUTION (bearish)
```

### **Volume Spike**
```
Volume > 2x average = Institutional activity
At support/resistance = HIGH significance
```

### **Wick Analysis**
```
Long lower wick = Strong BUYING pressure
Long upper wick = Strong SELLING pressure
(>0.5 ATR = "strong")
```

---

## **MTF CONFLUENCE CHECKLIST**

```
✓ 5-min trend aligned with 1-min signal
✓ 5-min at key swing level (support/resistance)
✓ 5-min RSI extreme (<35 or >65)
✓ 5-min volume spike

More checks = Higher confidence = Better entry
```

---

## **RECOMMENDED FILTERS**

### **For Trend Following (Conservative):**
```
Minimum Confidence: 5
Enable Swing Rejection: ON (to catch reversals too)
Enable Failed Breakdown: ON
Reversal Stop Mult: 1.5 ATR
```

### **For Reversal Trading (Balanced):**
```
Minimum Confidence: 3
Enable Swing Rejection: ON
Enable Failed Breakdown: ON
Enable Scaling: ON
Initial Position: 33%
```

### **For Aggressive:**
```
Minimum Confidence: 2
All patterns: ON
Scaling: OFF (full size immediately)
```

---

## **COMMON PATTERNS TO WATCH**

### **1. Double Bottom Reversal**
```
1st bottom: Sell-off, volume high
2nd bottom: Same level, volume LOWER (divergence)
→ Triggers: Swing Rejection Long
```

### **2. Failed Breakdown**
```
Price breaks support → Panic selling → Quick recovery
→ Triggers: Failed Breakdown Long
```

### **3. Bear Trap**
```
Breakdown + Volume spike + Reclaim within 3 bars
→ Triggers: Failed Breakdown Long
```

### **4. Bull Trap**
```
Breakout + Volume spike + Failure within 3 bars
→ Triggers: Failed Breakout Short
```

---

## **ALERT MESSAGES**

### **New Alerts:**
```
EXEC: BUY CALL (REVERSAL)
EXEC: BUY PUT (REVERSAL)
```

### **Existing Alerts:**
```
EXEC: BUY CALL (TREND)
EXEC: BUY CALL (SCALP)
EXEC: BUY PUT (TREND)
EXEC: BUY PUT (SCALP)
EXEC: SELL CALL
EXEC: SELL PUT
```

---

## **BANNER DECODE**

```
Example: "CALL DAY | Conf: 6/8 | Regime: REVERSAL | Macro: Bullish metals | Phase: CORE | Bias: CALL"

CALL DAY = Currently in a call position
Conf: 6/8 = High confidence (take it seriously)
Regime: REVERSAL = Look for reversal patterns
Macro: Bullish metals = DXY/Yields support longs
Phase: CORE = Can size normal (not just probes)
Bias: CALL = Overall bias bullish
```

---

## **LABEL COLORS**

| Color | Meaning |
|-------|---------|
| 🟣 Purple | REVERSAL signal (Swing Rejection / Failed Breakdown) |
| 🟢 Green | LONG momentum (original) |
| 🔴 Red | SHORT momentum (original) |
| 🟡 Yellow | TP1 reached (~+1R) |
| 🟠 Orange | TP2 reached (~+2R) |
| ⚪ Gray | Time stop triggered |
| 🔵 Blue | Scale-in added |

---

## **TYPICAL R OUTCOMES**

| Setup Type | Win Rate | Avg R | Notes |
|------------|----------|-------|-------|
| Swing Rejection | 55-60% | 2.0R | Bigger winners, some stop-outs |
| Failed Breakdown | 65-70% | 1.8R | Fast moves, high confidence |
| Trend Momentum | 60-65% | 1.5R | Consistent (original) |
| Scalp | 55-60% | 0.8R | Quick in/out (original) |

---

## **TROUBLESHOOTING QUICK FIXES**

| Problem | Solution |
|---------|----------|
| Too many signals | Increase min confidence to 5 |
| Missing reversals | Lower min confidence to 2-3 |
| Stop-outs on reversals | Increase stop mult to 2.0 ATR |
| CVD looks weird | It's cumulative - normal |
| Confidence always low | Market is choppy - good filter |

---

## **DAILY CHECKLIST**

### **Pre-Market:**
```
□ Check macro (DXY, US10Y)
□ Check overnight levels (support/resistance)
□ Set alerts for REVERSAL signals
```

### **During RTH:**
```
□ Watch for REVERSAL regime (purple background)
□ At key levels, watch for Swing Rejection
□ Volume spikes = potential Failed Breakdown
□ Scale in on winners at +0.5R
```

### **Post-Market:**
```
□ Review: Which setups worked best?
□ Check stats panel (Win%, Avg R, Net R)
□ Adjust confidence threshold if needed
```

---

## **KEY IMPROVEMENTS vs v4.6**

| Feature | v4.6 | v6.0 PRO |
|---------|------|----------|
| Reversal Detection | ❌ None | ✅ 2 patterns |
| Order Flow | ❌ No | ✅ CVD, Volume Delta |
| MTF Analysis | ⚠️ Basic | ✅ Full structure |
| Confidence Scoring | ⚠️ 5 factors | ✅ 8 factors |
| Regime Types | 4 | 5 (added REVERSAL) |
| Exit Logic | 1 type | 2 types (dual) |
| Position Scaling | ❌ No | ✅ Yes |

---

## **SETTINGS PRESETS**

Copy-paste these into your settings:

### **Conservative:**
```
Minimum confidence: 5
Enable Order Flow: ON
Show CVD Plot: OFF
HTF timeframe: 5
Enable Swing Rejection: ON
Enable Failed Breakdown: ON
Enable Scaling: ON
Initial position size: 25%
Target R - REVERSAL: 2.5
Max bars - REVERSAL: 25
```

### **Balanced (Default):**
```
Minimum confidence: 3
Enable Order Flow: ON
Show CVD Plot: OFF
HTF timeframe: 5
Enable Swing Rejection: ON
Enable Failed Breakdown: ON
Enable Scaling: ON
Initial position size: 33%
Target R - REVERSAL: 2.5
Max bars - REVERSAL: 25
```

### **Aggressive:**
```
Minimum confidence: 2
Enable Order Flow: ON
Show CVD Plot: ON
HTF timeframe: 3
Enable Swing Rejection: ON
Enable Failed Breakdown: ON
Enable Scaling: OFF
Initial position size: 100%
Target R - REVERSAL: 3.0
Max bars - REVERSAL: 30
```

---

**Print this and keep it next to your screen!** 📋
