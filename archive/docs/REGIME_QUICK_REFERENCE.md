# 🎯 Regime-Adaptive Trading Quick Reference

**One-page guide for live trading with the adaptive system**

---

## 📊 Regime Classification (Real-Time)

### 🟢 TRENDING (Score 60-100)
**Characteristics:**
- EMA 9/21 crosses: 0-1 in last 30 bars
- Range efficiency > 60%
- ATR ratio < 1.1 (smooth volatility)
- ADX > 30

**Trading Rules:**
- Position size: **0.3× initial** (cautious start)
- **Scale to 1.0× at bar 10 if profitable**
- Profit target: **1.5%**
- Stop loss: 1.0% (wider)
- Max hold: **60 bars**
- Exit: Patient, EMA cross + VWAP loss

**Why:** Trends take time to develop. Start small, scale up if working.

---

### 🔴 WHIPSAW (Score 0-30)
**Characteristics:**
- EMA 9/21 crosses: 3+ in last 30 bars
- Range efficiency < 25%
- ATR ratio > 1.3 (volatile)
- ADX < 20

**Trading Rules:**
- Position size: **Full size** (1.0-1.5×)
- Profit target: **0.3%** (5× smaller!)
- Stop loss: 0.4% (tight)
- Max hold: **15 bars**
- Exit: CCI crosses 0 (momentum shift)

**Entry Quality Gates (ALL REQUIRED):**
- ✅ CCI < -100 (oversold) OR > 100 (overbought)
- ✅ Price position 10-40% in 5-bar range **(CRITICAL!)**
- ✅ Close within -0.50% of EMA 9
- ✅ Close within -0.60% of EMA 20
- ✅ Lower wick > 30%
- ✅ Body < 70%
- ✅ At 5-bar swing low

**Why:** Whipsaws profit from fast reversals. Full size, fast exit.

---

### ⚠️ EXTREME WHIPSAW (Score < 20)
**Alert:** Disaster zone detected!

**Trading Rules:**
- **BLOCK ALL TRENDING ENTRIES** ❌
- Only extreme CCI reversals (< -100 or > 100)
- Must pass ALL whipsaw quality gates
- Reduce position size to 0.5× even if A-tier
- Consider sitting out until regime improves

**Why:** Score < 20 = extreme chop. Bar 217 was score=10 before crash.

---

### 🟡 NEUTRAL (Score 31-59)
**Characteristics:**
- Transitioning between trending/whipsaw
- Mixed signals
- ADX 20-30

**Trading Rules:**
- Position size: Normal (A/B/C tiers)
- C-tier threshold: **40 instead of 30** (fewer trades)
- Exit: Requires stronger confirmation
- Focus on A/B quality only

**Why:** Uncertain conditions = selective trading.

---

## 🎯 Reading the Banner

```
Regime: 🔴 WHIPSAW (25/100) ⚠️ EXTREME | MSTR: 45 (B)
```

**Decode:**
- 🔴 = WHIPSAW mode active
- (25/100) = Score 25 (near WHIPSAW threshold of 30)
- ⚠️ EXTREME = Score < 20 (danger zone)
- MSTR: 45 = Opportunity score is 45
- (B) = B-tier trade (1.0× size in whipsaw, 0.3× in trending)

---

## 📈 Position Sizing Cheat Sheet

| Regime | Tier | Initial Size | Scale-Up | Final Size |
|--------|------|--------------|----------|------------|
| **TRENDING** | A | 0.45× | Bar 10 if +R | 1.5× |
| **TRENDING** | B | 0.30× | Bar 10 if +R | 1.0× |
| **TRENDING** | C | 0.15× | Bar 10 if +R | 0.5× |
| **WHIPSAW** | A | 1.5× | No scale | 1.5× |
| **WHIPSAW** | B | 1.0× | No scale | 1.0× |
| **WHIPSAW** | C | 0.5× | No scale | 0.5× |
| **EXTREME** | ANY | 0.5× | No scale | 0.5× |
| **NEUTRAL** | A/B | Normal | As usual | Normal |
| **NEUTRAL** | C | **SKIP** | - | - |

---

## ⏱️ Exit Timing by Regime

| Regime | Avg Winner Hold | Target | Max Hold | Exit Signal |
|--------|----------------|--------|----------|-------------|
| **WHIPSAW** | **1 bar** | 0.3% | 15 bars | CCI crosses 0 |
| **TRENDING** | 30+ bars | 1.5% | 60 bars | EMA cross + VWAP |
| **NEUTRAL** | Variable | Standard | Standard | Confirmation needed |

---

## 🚨 Critical Filters (WHIPSAW ONLY)

### Price Position (793% Importance!)

```
Position = (Close - 5-bar Low) / (5-bar High - 5-bar Low)
```

| Position | Action | Why |
|----------|--------|-----|
| **> 40%** | ❌ BLOCK | Too high, not at support |
| **10-40%** | ✅ TRADE | Value zone, accumulation |
| **< 10%** | ❌ BLOCK | Breaking lower, continued selling |

**Examples:**
- Position = 19% → ✅ TRADE (winner avg)
- Position = -3% → ❌ BLOCK (loser avg)
- Position = 35% → ✅ TRADE (value zone)
- Position = 5% → ❌ BLOCK (breaking support)

---

## 📋 Pre-Trade Checklist

### Before Every Trade:

**1. Check Regime**
- [ ] Banner shows current regime (🟢/🔴/🟡)
- [ ] Score is NOT < 20 (extreme)
- [ ] I know the position sizing rules for this regime

**2. Whipsaw Trades Only**
- [ ] CCI < -100 or > 100
- [ ] Price position 10-40% ✅ CRITICAL
- [ ] Near EMA 9 (-0.50% to 0%)
- [ ] Near EMA 20 (-0.60% to 0%)
- [ ] Lower wick > 30%
- [ ] Body < 70%
- [ ] At 5-bar swing low

**3. Position Sizing**
- [ ] Trending = 0.3× initial (scale up at bar 10)
- [ ] Whipsaw = full size
- [ ] Extreme = 0.5× max
- [ ] Neutral = C-tier threshold 40

**4. Exit Plan**
- [ ] Whipsaw = 0.3% target, 15 bars max
- [ ] Trending = 1.5% target, 60 bars max
- [ ] CCI exit signal set (whipsaw only)
- [ ] Max loss limit set

---

## 🎓 Common Mistakes

### ❌ Don't Do This:
1. **Ignore price position filter** → "CCI looks good, I'll trade"
   - Result: 60% of these trades lose (price breaking lower)
   
2. **Use trending exits in whipsaw** → "I'll wait for EMA cross"
   - Result: Profit evaporates (whipsaw winners exit at 1 bar avg)
   
3. **Full size in trending mode** → "This looks like a good trend"
   - Result: -$9.76 loss on Feb 9 (should be -$2.60 @ 0.3×)
   
4. **Trade during extreme whipsaw** → "It's oversold, time to buy"
   - Result: Bar 217 disaster (score=10, crash followed)
   
5. **Forget to scale trending winners** → "I'll stay at 0.3×"
   - Result: Miss 70% of potential profit on winners

### ✅ Do This Instead:
1. **Check price position first** → "Is it 10-40%? Yes → proceed"
2. **Use regime-specific exits** → "Whipsaw = CCI shift, Trending = patient"
3. **Start small in trends** → "0.3× initial, scale if working"
4. **Respect extreme warnings** → "Score < 20 = sit out or 0.5× max"
5. **Scale up trending winners** → "Bar 10 + profitable = scale to full"

---

## 📊 Expected Results (Feb 9 Baseline)

| Mode | Trades/Day | Win Rate | Avg P&L |
|------|-----------|----------|---------|
| **Whipsaw** | 8-12 | **60%** | **+$1.50** ✅ |
| **Trending** | 3-5 | 45% | +$0.50 |
| **Mixed Day** | 10-15 | **55%** | **+$2.00** |
| **Extreme Day** | 2-4 | 35% | -$0.50 |

**Important:**
- Whipsaw mode is NOW PROFITABLE (winner filters work!)
- Trending mode losses reduced 73% (0.3× sizing)
- Mixed days = best performance (multiple regime types)
- Extreme days = defensive (survival mode)

---

## 🔧 Troubleshooting

### "Too many trades blocked in whipsaw"
- Check price position: Is it < 10% (breaking lower)?
- This is CORRECT behavior (60% of < 10% trades lose)
- Only trade 10-40% zone (value/accumulation)

### "Trending trades exiting too early"
- Check if using whipsaw exits (CCI) instead of trending exits
- Trending needs EMA cross + VWAP loss + RSI shift
- Be patient: 60 bars max hold (not 15)

### "Not scaling up trending winners"
- Set alert at bar 10
- Check if trade is profitable (exitCurrentR > 0)
- Manually increase size from 0.3× to 1.0×

### "Regime keeps changing"
- This is normal around transitions (see Feb 9 bars 210-230)
- 8 changes in 20 bars = volatile market
- Trade the current regime, not the previous one
- Consider sitting out if changing every 2-3 bars

---

## 📱 Quick Actions

**Trending Mode Active:**
1. Reduce size to 0.3×
2. Set 1.5% target
3. Set 60-bar time stop
4. Note bar 10 for scale-up check

**Whipsaw Mode Active:**
1. Check ALL 7 quality filters
2. Full size if A/B-tier
3. Set 0.3% target
4. Set 15-bar time stop
5. Exit on CCI crosses 0

**Extreme Warning Appears:**
1. Pause all trending entries
2. Only extreme CCI reversals
3. 0.5× max size
4. Consider sitting out

---

## 🎯 Success Metrics

Track these weekly:

**By Regime:**
- Whipsaw WR: Target **60%** (baseline 50%)
- Trending WR: Target **45%** (baseline 30%)
- Neutral WR: Target **40%** (baseline 35%)

**Position Sizing:**
- Trending: Started 0.3×, scaled up on __% of winners
- Whipsaw: Used full size on __% of trades
- Extreme: Reduced to 0.5× on __% of dangerous setups

**Filters:**
- Price position: Blocked __% of < 10% trades (target: 100%)
- Quality gates: Passed __% in whipsaw (target: 30-40%)
- Regime accuracy: Detected regime correctly __% (target: 80%+)

---

## 📞 Need Help?

**Documentation:**
- Full implementation: `REGIME_ADAPTIVE_IMPLEMENTATION.md`
- Original analysis: `A_B_TIER_FAILURE_ANALYSIS.md`
- This guide: `REGIME_QUICK_REFERENCE.md`

**Key Principle:**  
> "Trade the market in front of you, not the market you wish you had."

**Regime-adaptive trading = Survival first, profits second.**

---

*Last updated: Feb 9, 2026*  
*Based on validated MSTR analysis (644 bars)*  
*System ready for live trading ✅*
