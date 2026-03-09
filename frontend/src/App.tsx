import React, { useEffect, useState } from 'react'

type Regime = 'GREEN' | 'YELLOW' | 'RED'

type StockScore = {
  symbol: string
  score: number
  direction: string
  passed: boolean
  regime: Regime
  vix: number
  atr_pct: number
  size_multiplier: number
  preferred_strategy: string
  reason: string
}

type OptionLeg = {
  side: 'buy' | 'sell'
  type: 'call' | 'put'
  expiry: string
  strike: number
  qty: number
}

type OptionPlan = {
  symbol: string
  structure: string
  legs: OptionLeg[]
  entry_debit: number
  max_loss: number
  max_profit: number
  pop_pct: number
  horizon_days: number
  dte?: number
  // Credit spread fields
  strategy: string
  short_strike: number
  long_strike: number
  credit_per_contract: number
  num_contracts: number
  total_credit: number
  total_max_loss: number
  width: number
  short_delta: number
  short_iv: number
  expiry: string
  regime: string
  vix: number
}

type Health = {
  status: string
  equity_data_provider: string
  options_data_provider: string
  broker_provider: string
}

type LLMExplanation = {
  symbol: string
  summary: string
  risk: string
  time_comment: string
}

type MetricsSummary = {
  date: string
  trades: number
  pnl_today: number
  pnl_week: number
  pnl_month: number
  open_positions: number
  consecutive_losses: number
  in_recovery: boolean
  can_trade: boolean
  can_trade_reason: string
  max_drawdown_30d: number
  peak_equity: number
  current_equity: number
  positions: any[]
  closed_trades_today: any[]
}

type ExecutionEntry = {
  ts: string
  message: string
  details?: any
}

type Tab = 'scores' | 'plans' | 'log' | 'insights'

const DEFAULT_API_BASE =
  (import.meta.env.VITE_API_BASE as string) || 'http://localhost:8000'

async function api<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${base}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init
  })
  if (!res.ok) {
    const txt = await res.text()
    throw new Error(txt || res.statusText)
  }
  return res.json()
}

export default function App() {
  const [apiBase, setApiBase] = useState<string>(() => localStorage.getItem('apiBase') || DEFAULT_API_BASE)
  const [symbolsInput, setSymbolsInput] = useState<string>('SPY, QQQ')
  const [equity, setEquity] = useState<number>(50000)
  const [horizon, setHorizon] = useState<number>(0)

  const [scores, setScores] = useState<StockScore[] | null>(null)
  const [plans, setPlans] = useState<OptionPlan[] | null>(null)
  const [health, setHealth] = useState<Health | null>(null)

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<Tab>('scores')
  const [log, setLog] = useState<ExecutionEntry[]>([])

  const [metrics, setMetrics] = useState<MetricsSummary | null>(null)
  const [explanations, setExplanations] = useState<LLMExplanation[] | null>(null)

  useEffect(() => {
    localStorage.setItem('apiBase', apiBase)
  }, [apiBase])

  useEffect(() => {
    (async () => {
      try {
        const h = await api<Health>(apiBase, '/health')
        setHealth(h)
      } catch (e: any) {
        setError(e.message)
      }
    })()
  }, [apiBase])

  const pushLog = (message: string, details?: any) => {
    setLog(prev => [
      { ts: new Date().toISOString(), message, details },
      ...prev
    ])
  }

  const handleScore = async () => {
    setBusy(true); setError(null); setPlans(null); setActiveTab('scores')
    try {
      const syms = symbolsInput.split(',').map(s => s.trim().toUpperCase()).filter(Boolean)
      const data = await api<StockScore[]>(apiBase, '/score/intraday', {
        method: 'POST',
        body: JSON.stringify({ symbols: syms })
      })
      setScores(data)
      pushLog(`Scored ${data.length} symbols.`, { symbols: syms })
    } catch (e:any) {
      setError(e.message); pushLog('Error scoring', { error:e.message })
    } finally { setBusy(false) }
  }

  const handlePlanOptions = async () => {
    setBusy(true); setError(null); setActiveTab('plans')
    try {
      const syms = symbolsInput.split(',').map(s => s.trim().toUpperCase()).filter(Boolean)
      const data = await api<OptionPlan[]>(apiBase, '/options/plan', {
        method: 'POST',
        body: JSON.stringify({ symbols: syms, equity, horizon_days: horizon })
      })
      setPlans(data)
      pushLog(`Built plans for ${data.length} symbols.`, { symbols: syms, equity, horizon })
    } catch (e:any) {
      setError(e.message); pushLog('Error building plans', { error:e.message })
    } finally { setBusy(false) }
  }

  const handlePlaceOptions = async () => {
    if (!plans || !plans.length) return
    setBusy(true); setError(null)
    try {
      const res = await api<{orders:any[]}>(apiBase, '/options/place', {
        method: 'POST',
        body: JSON.stringify({ plans })
      })
      pushLog(`Placed ${res.orders.length} order(s).`, res.orders)
      alert(`Placed ${res.orders.length} order(s). Check IBKR TWS for status.`)
      setActiveTab('log')
    } catch (e:any) {
      setError(e.message); pushLog('Error placing orders', { error:e.message })
    } finally { setBusy(false) }
  }

  const fetchMetrics = async () => {
    try {
      const data = await api<MetricsSummary>(apiBase, '/metrics/summary')
      setMetrics(data); pushLog('Fetched metrics summary.', data)
    } catch (e:any) {
      setError(e.message); pushLog('Error fetching metrics', { error:e.message })
    }
  }

  const handleExplainPlans = async () => {
    if (!plans || !plans.length) { setError('No plans to explain'); return }
    setBusy(true); setError(null); setActiveTab('insights')
    try {
      const data = await api<LLMExplanation[]>(apiBase, '/llm/explain-plans', {
        method: 'POST',
        body: JSON.stringify({ plans })
      })
      setExplanations(data)
      pushLog(`Got ${data.length} explanations.`, data)
    } catch (e:any) {
      setError(e.message); pushLog('Error explaining plans', { error:e.message })
    } finally { setBusy(false) }
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-50">
      <header className="border-b border-slate-800 px-4 py-3 flex justify-between">
        <div>
          <div className="font-semibold text-sm">0DTE Credit Spread Console</div>
          <div className="text-xs text-slate-400">VIX regime · ATR sizing · IBKR execution</div>
        </div>
        <div className="text-xs text-slate-400">
          Backend: <span className="font-semibold">{health?.status || 'unknown'}</span>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 py-4 grid md:grid-cols-[280px,1fr] gap-4">
        <section className="space-y-3 border border-slate-800 rounded-xl p-3 bg-slate-900/70">
          <div>
            <label className="text-xs text-slate-300">API Base</label>
            <input className="w-full mt-1 text-xs px-2 py-1 rounded bg-slate-950 border border-slate-700"
              value={apiBase} onChange={e=>setApiBase(e.target.value)} />
          </div>
          <div>
            <label className="text-xs text-slate-300">Symbols</label>
            <textarea className="w-full mt-1 text-xs px-2 py-1 rounded bg-slate-950 border border-slate-700"
              rows={2}
              value={symbolsInput} onChange={e=>setSymbolsInput(e.target.value)} />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-xs text-slate-300">Equity ($)</label>
              <input type="number" className="w-full mt-1 text-xs px-2 py-1 rounded bg-slate-950 border border-slate-700"
                value={equity} onChange={e=>setEquity(parseFloat(e.target.value)||0)} />
            </div>
            <div>
              <label className="text-xs text-slate-300">Horizon (DTE)</label>
              <input type="number" className="w-full mt-1 text-xs px-2 py-1 rounded bg-slate-950 border border-slate-700"
                value={horizon} onChange={e=>setHorizon(parseInt(e.target.value)||0)} />
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <button onClick={handleScore} disabled={busy}
              className="text-xs px-3 py-1 rounded bg-emerald-500 text-slate-950 disabled:bg-slate-700">
              Score
            </button>
            <button onClick={handlePlanOptions} disabled={busy}
              className="text-xs px-3 py-1 rounded bg-cyan-500 text-slate-950 disabled:bg-slate-700">
              Plan
            </button>
            <button onClick={handlePlaceOptions} disabled={busy || !plans || !plans.length}
              className="text-xs px-3 py-1 rounded bg-indigo-500 text-slate-50 disabled:bg-slate-700">
              Place Order
            </button>
            <button onClick={handleExplainPlans} disabled={busy || !plans || !plans.length}
              className="text-xs px-3 py-1 rounded bg-fuchsia-500 text-slate-50 disabled:bg-slate-700">
              Explain (LLM)
            </button>
          </div>
          {busy && <div className="text-xs text-slate-400">Working…</div>}
          {error && <div className="text-xs text-rose-300 border border-rose-500/40 rounded px-2 py-1">{error}</div>}
        </section>

        <section className="space-y-3">
          <div className="flex gap-2 text-xs">
            {(['scores','plans','log','insights'] as Tab[]).map(tab => (
              <button key={tab}
                onClick={()=>{
                  setActiveTab(tab)
                  if (tab==='insights' && !metrics) fetchMetrics()
                }}
                className={`px-3 py-1 rounded-full border ${activeTab===tab ? 'bg-slate-50 text-slate-900' : 'border-slate-700 text-slate-300'}`}
              >
                {tab}
              </button>
            ))}
          </div>

          <div className="border border-slate-800 rounded-xl p-3 bg-slate-900/70 text-xs min-h-[260px]">
            {activeTab === 'scores' && (
              !scores ? <div className="text-slate-400">No scores yet. Click <strong>Score</strong> to fetch VIX regime + ATR analysis.</div> :
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-slate-400 border-b border-slate-800">
                    <th className="text-left">#</th><th className="text-left">Symbol</th><th>Score</th><th>Regime</th><th>VIX</th><th>ATR%</th><th>Size</th><th>Strategy</th><th>OK</th>
                  </tr>
                </thead>
                <tbody>
                  {scores.map((s,i)=>(
                    <tr key={s.symbol} className="border-b border-slate-800/60">
                      <td>{i+1}</td>
                      <td className="font-mono">{s.symbol}</td>
                      <td className="text-center">{s.score.toFixed(2)}</td>
                      <td className="text-center">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                          s.regime === 'GREEN' ? 'bg-emerald-500/20 text-emerald-400' :
                          s.regime === 'YELLOW' ? 'bg-amber-500/20 text-amber-400' :
                          'bg-rose-500/20 text-rose-400'
                        }`}>{s.regime}</span>
                      </td>
                      <td className="text-center">{s.vix.toFixed(1)}</td>
                      <td className="text-center">{(s.atr_pct*100).toFixed(1)}%</td>
                      <td className="text-center">{s.size_multiplier.toFixed(1)}x</td>
                      <td className="text-center font-mono text-[10px]">{s.preferred_strategy || '—'}</td>
                      <td className="text-center">{s.passed ? <span className="text-emerald-400">✓</span>:<span className="text-rose-400">✗</span>}</td>
                    </tr>
                  ))}
                </tbody>
                {scores.length > 0 && scores[0].reason && (
                  <tfoot>
                    <tr><td colSpan={9} className="pt-2 text-slate-400">
                      {scores.map(s => <div key={s.symbol}><span className="font-mono">{s.symbol}</span>: {s.reason}</div>)}
                    </td></tr>
                  </tfoot>
                )}
              </table>
            )}

            {activeTab === 'plans' && (
              !plans ? <div className="text-slate-400">No plans yet. Click <strong>Plan</strong> to generate credit spread plans.</div> :
              <div className="space-y-2 max-h-72 overflow-y-auto pr-1">
                {plans.map((p,idx)=>(
                  <div key={idx} className="border border-slate-800 rounded-lg p-2">
                    <div className="flex justify-between items-center">
                      <div className="flex items-center gap-2">
                        <span className="font-mono font-bold">{p.symbol}</span>
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                          p.regime === 'GREEN' ? 'bg-emerald-500/20 text-emerald-400' :
                          p.regime === 'YELLOW' ? 'bg-amber-500/20 text-amber-400' :
                          'bg-rose-500/20 text-rose-400'
                        }`}>{p.regime}</span>
                        <span className="text-slate-400 text-[10px]">VIX {p.vix.toFixed(1)}</span>
                      </div>
                      <span className="text-cyan-400 font-mono">{p.strategy || p.structure}</span>
                    </div>
                    <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 text-slate-300">
                      <div>Short: <span className="font-mono text-slate-50">${p.short_strike}</span> (Δ {p.short_delta.toFixed(2)}, IV {(p.short_iv*100).toFixed(0)}%)</div>
                      <div>Long: <span className="font-mono text-slate-50">${p.long_strike}</span></div>
                      <div>Width: <span className="font-mono">${p.width.toFixed(0)}</span></div>
                      <div>Contracts: <span className="font-mono text-slate-50">{p.num_contracts}</span></div>
                      <div>Credit: <span className="font-mono text-emerald-400">${p.credit_per_contract.toFixed(2)}</span>/ct → <span className="font-mono text-emerald-400">${p.total_credit.toFixed(2)}</span></div>
                      <div>Max Loss: <span className="font-mono text-rose-400">${p.total_max_loss.toFixed(2)}</span></div>
                      {p.expiry && <div>Expiry: <span className="font-mono">{p.expiry}</span></div>}
                      {p.pop_pct > 0 && <div>PoP: <span className="font-mono text-cyan-400">{p.pop_pct.toFixed(1)}%</span></div>}
                    </div>
                    {p.legs && p.legs.length > 0 && (
                      <div className="mt-1 text-[10px] text-slate-500">
                        {p.legs.map((l,i)=>(
                          <span key={i} className="mr-2">{l.side.toUpperCase()} {l.qty}x {l.strike} {l.type} {l.expiry}</span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {activeTab === 'log' && (
              !log.length ? <div className="text-slate-400">No activity yet.</div> :
              <div className="space-y-1 max-h-72 overflow-y-auto pr-1">
                {log.map((e,i)=>(
                  <div key={i} className="border border-slate-800 rounded px-2 py-1">
                    <div className="text-[10px] text-slate-500">{new Date(e.ts).toLocaleString()}</div>
                    <div>{e.message}</div>
                  </div>
                ))}
              </div>
            )}

            {activeTab === 'insights' && (
              <div className="space-y-2">
                {!metrics ? (
                  <div className="text-slate-400">No metrics loaded yet. Switch to this tab to auto-fetch.</div>
                ) : (
                  <>
                    {/* Trading status banner */}
                    <div className={`rounded-lg px-3 py-2 text-xs font-bold flex justify-between items-center ${
                      metrics.can_trade ? 'bg-emerald-500/10 border border-emerald-500/30 text-emerald-400' :
                      'bg-rose-500/10 border border-rose-500/30 text-rose-400'
                    }`}>
                      <span>{metrics.can_trade ? '● TRADING ENABLED' : '● TRADING HALTED'}</span>
                      <span className="font-normal text-slate-400">{metrics.can_trade_reason}</span>
                    </div>
                    {metrics.in_recovery && (
                      <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg px-3 py-1 text-[11px] text-amber-400">
                        ⚠ Recovery Mode · {metrics.consecutive_losses} consecutive losses
                      </div>
                    )}
                    {/* P&L grid */}
                    <div className="grid grid-cols-3 gap-2 text-center">
                      <div className="border border-slate-800 rounded-lg p-2">
                        <div className="text-[10px] text-slate-400">Today</div>
                        <div className={`text-lg font-mono font-bold ${metrics.pnl_today >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                          ${metrics.pnl_today.toFixed(0)}
                        </div>
                      </div>
                      <div className="border border-slate-800 rounded-lg p-2">
                        <div className="text-[10px] text-slate-400">Week</div>
                        <div className={`text-lg font-mono font-bold ${metrics.pnl_week >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                          ${metrics.pnl_week.toFixed(0)}
                        </div>
                      </div>
                      <div className="border border-slate-800 rounded-lg p-2">
                        <div className="text-[10px] text-slate-400">Month</div>
                        <div className={`text-lg font-mono font-bold ${metrics.pnl_month >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                          ${metrics.pnl_month.toFixed(0)}
                        </div>
                      </div>
                    </div>
                    {/* Stats row */}
                    <div className="grid grid-cols-2 gap-2 text-[11px]">
                      <div>Date: {metrics.date}</div>
                      <div>Trades today: {metrics.trades}</div>
                      <div>Open positions: {metrics.open_positions}</div>
                      <div>Consec. losses: {metrics.consecutive_losses}</div>
                      <div>30d drawdown: {(metrics.max_drawdown_30d*100).toFixed(1)}%</div>
                      <div>Peak equity: ${metrics.peak_equity.toFixed(0)}</div>
                    </div>
                    {/* Open positions */}
                    {metrics.positions.length > 0 && (
                      <div className="border-t border-slate-800 pt-2">
                        <div className="text-[11px] text-slate-400 mb-1 font-semibold">Open Positions</div>
                        <div className="space-y-1">
                          {metrics.positions.map((pos: any, i: number) => (
                            <div key={i} className="border border-slate-800 rounded px-2 py-1 text-[10px] flex justify-between">
                              <span className="font-mono">{pos.symbol} {pos.short_strike}/{pos.long_strike}</span>
                              <span className={pos.unrealized_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                                ${pos.unrealized_pnl?.toFixed(2) ?? '—'}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {/* Closed trades today */}
                    {metrics.closed_trades_today.length > 0 && (
                      <div className="border-t border-slate-800 pt-2">
                        <div className="text-[11px] text-slate-400 mb-1 font-semibold">Closed Today</div>
                        <div className="space-y-1">
                          {metrics.closed_trades_today.map((t: any, i: number) => (
                            <div key={i} className="border border-slate-800 rounded px-2 py-1 text-[10px] flex justify-between">
                              <span className="font-mono">{t.symbol}</span>
                              <span className={t.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}>${t.pnl?.toFixed(2) ?? '—'}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </>
                )}
                <div className="border-t border-slate-800 pt-2">
                  {!explanations ? (
                    <div className="text-slate-400">No LLM explanations yet.</div>
                  ) : (
                    <div className="space-y-1 max-h-40 overflow-y-auto pr-1">
                      {explanations.map((ex,i)=>(
                        <div key={i} className="border border-slate-800 rounded px-2 py-1">
                          <div className="font-mono text-[11px]">{ex.symbol}</div>
                          <div>Summary: {ex.summary}</div>
                          <div>Risk: {ex.risk}</div>
                          <div>Time: {ex.time_comment}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  )
}
