import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import {
  XAxis, YAxis, Tooltip, ResponsiveContainer, Area, AreaChart
} from 'recharts'
import {
  Activity, Zap, Shield, Brain, TrendingUp, ChevronDown, ChevronRight,
  GitBranch, Eye, CheckCircle, XCircle, BarChart3, Target, Radio,
  ArrowDown, Cpu, Users, Briefcase, Globe, AlertTriangle, X, Play,
  DollarSign, Calendar, Loader2
} from 'lucide-react'

const API_BASE = 'http://localhost:8000'

// ─── Mock Data ───────────────────────────────────────────────────────────────

const AGENTS = [
  // Layer 1: Macro
  { name: 'Central Bank', layer: 'MACRO', sharpe: 1.42, weight: 1.85, status: 'ACTIVE', lastMutation: '2026-02-18' },
  { name: 'Geopolitical', layer: 'MACRO', sharpe: 0.31, weight: 0.45, status: 'MUTATING', lastMutation: '2026-03-10' },
  { name: 'China', layer: 'MACRO', sharpe: 0.89, weight: 1.20, status: 'ACTIVE', lastMutation: '2026-01-25' },
  { name: 'Dollar', layer: 'MACRO', sharpe: 1.15, weight: 1.55, status: 'ACTIVE', lastMutation: '2026-02-05' },
  { name: 'Yield Curve', layer: 'MACRO', sharpe: 1.28, weight: 1.70, status: 'ACTIVE', lastMutation: '2026-02-12' },
  { name: 'Commodities', layer: 'MACRO', sharpe: 0.73, weight: 0.95, status: 'ACTIVE', lastMutation: '2026-01-30' },
  { name: 'Volatility', layer: 'MACRO', sharpe: 1.05, weight: 1.40, status: 'ACTIVE', lastMutation: '2026-02-08' },
  { name: 'Emerging Mkts', layer: 'MACRO', sharpe: 0.62, weight: 0.80, status: 'COOLDOWN', lastMutation: '2026-03-05' },
  { name: 'News Sentiment', layer: 'MACRO', sharpe: 0.48, weight: 0.60, status: 'ACTIVE', lastMutation: '2026-02-20' },
  { name: 'Inst. Flow', layer: 'MACRO', sharpe: 0.95, weight: 1.30, status: 'ACTIVE', lastMutation: '2026-02-01' },
  // Layer 2: Sector Desks
  { name: 'Semiconductor', layer: 'SECTOR', sharpe: 1.65, weight: 2.10, status: 'ACTIVE', lastMutation: '2026-02-22' },
  { name: 'Energy', layer: 'SECTOR', sharpe: 1.35, weight: 1.75, status: 'ACTIVE', lastMutation: '2026-02-15' },
  { name: 'Biotech', layer: 'SECTOR', sharpe: 0.55, weight: 0.70, status: 'ACTIVE', lastMutation: '2026-03-01' },
  { name: 'Consumer', layer: 'SECTOR', sharpe: 0.82, weight: 1.10, status: 'ACTIVE', lastMutation: '2026-01-28' },
  { name: 'Industrials', layer: 'SECTOR', sharpe: 0.91, weight: 1.25, status: 'ACTIVE', lastMutation: '2026-02-10' },
  { name: 'Financials', layer: 'SECTOR', sharpe: 1.18, weight: 1.60, status: 'ACTIVE', lastMutation: '2026-02-17' },
  { name: 'Rel. Mapper', layer: 'SECTOR', sharpe: 0.70, weight: 0.90, status: 'ACTIVE', lastMutation: '2026-02-03' },
  // Layer 3: Superinvestors
  { name: 'Druckenmiller', layer: 'SUPER', sharpe: 1.52, weight: 2.00, status: 'ACTIVE', lastMutation: '2026-02-25' },
  { name: 'Aschenbrenner', layer: 'SUPER', sharpe: 1.38, weight: 1.80, status: 'ACTIVE', lastMutation: '2026-02-20' },
  { name: 'Baker', layer: 'SUPER', sharpe: 0.45, weight: 0.55, status: 'COOLDOWN', lastMutation: '2026-03-08' },
  { name: 'Ackman', layer: 'SUPER', sharpe: 1.10, weight: 1.45, status: 'ACTIVE', lastMutation: '2026-02-14' },
  // Layer 4: Decision
  { name: 'CRO', layer: 'DECISION', sharpe: 1.25, weight: 1.65, status: 'ACTIVE', lastMutation: '2026-02-28' },
  { name: 'Alpha Discovery', layer: 'DECISION', sharpe: 0.88, weight: 1.15, status: 'ACTIVE', lastMutation: '2026-02-06' },
  { name: 'Execution', layer: 'DECISION', sharpe: 1.30, weight: 1.72, status: 'ACTIVE', lastMutation: '2026-02-11' },
  { name: 'CIO', layer: 'DECISION', sharpe: 1.58, weight: 2.20, status: 'ACTIVE', lastMutation: '2026-02-26' },
]

const EQUITY_DATA = (() => {
  const data = []
  let nav = 1000000
  const start = new Date('2026-01-02')
  for (let i = 0; i < 50; i++) {
    const d = new Date(start)
    d.setDate(d.getDate() + i)
    if (d.getDay() === 0 || d.getDay() === 6) continue
    const change = (Math.random() - 0.47) * 0.012
    nav *= (1 + change)
    data.push({
      date: d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      nav: Math.round(nav),
      drawdown: Math.round((nav / 1000000 - 1) * 10000) / 100
    })
  }
  return data
})()

const NEWS_ITEMS = [
  'MACRO: Fed holds rates at 5.25% — CPI cooling trend intact',
  'SEMI: NVDA momentum continues, SMH +2.3% WoW',
  'RISK: CRO flagged 4 correlated longs in tech sector',
  'AUTORESEARCH: Geopolitical agent prompt mutated — targeting Sharpe +0.31',
  'EXECUTION: 12 trades sized and submitted, gross exposure 0.87x',
  'MACRO: DXY breaks 119 — dollar strength pressuring EM',
  'ENERGY: Crude +4.2% on Iran tensions, SLB conviction raised',
  'SUPER: Druckenmiller model shifts to defensive, 60% cash signal',
  'BIOTECH: XBI -3.1%, Baker agent enters cooldown after poor calls',
  'REGIME: Composite score -0.42 — RISK_OFF declared for Mar 3-7',
]

// ─── Components ──────────────────────────────────────────────────────────────

function Starfield() {
  const stars = useMemo(() =>
    Array.from({ length: 120 }, (_, i) => ({
      id: i,
      x: Math.random() * 100,
      y: Math.random() * 100,
      size: Math.random() * 2 + 0.5,
      delay: Math.random() * 5,
      duration: Math.random() * 3 + 2,
    })), []
  )

  return (
    <div className="absolute inset-0 overflow-hidden pointer-events-none">
      {stars.map(s => (
        <div
          key={s.id}
          className="absolute rounded-full bg-white"
          style={{
            left: `${s.x}%`,
            top: `${s.y}%`,
            width: `${s.size}px`,
            height: `${s.size}px`,
            animation: `twinkle ${s.duration}s ease-in-out ${s.delay}s infinite`,
          }}
        />
      ))}
      <div
        className="absolute inset-0"
        style={{
          backgroundImage: `
            linear-gradient(rgba(255,255,255,0.02) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,255,255,0.02) 1px, transparent 1px)
          `,
          backgroundSize: '60px 60px',
        }}
      />
    </div>
  )
}

function LivePill() {
  return (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium"
      style={{ background: 'rgba(0,255,136,0.1)', color: 'var(--green)', border: '1px solid rgba(0,255,136,0.2)' }}>
      <span className="w-2 h-2 rounded-full" style={{
        background: 'var(--green)',
        animation: 'pulse-green 2s ease-in-out infinite',
      }} />
      LIVE
    </span>
  )
}

function SectionTitle({ children, icon: Icon }) {
  return (
    <div className="flex items-center gap-3 mb-6">
      {Icon && <Icon size={20} style={{ color: 'var(--green)' }} />}
      <h2 className="text-xl font-semibold tracking-tight" style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>
        {children}
      </h2>
      <div className="flex-1 h-px" style={{ background: 'var(--border)' }} />
    </div>
  )
}

function StatCard({ label, value, sub, color }) {
  return (
    <div className="p-4 rounded-lg" style={{ background: 'var(--bg-card)', border: '1px solid var(--border)' }}>
      <div className="text-xs uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
        {label}
      </div>
      <div className="text-2xl font-bold" style={{
        fontFamily: 'var(--font-mono)',
        color: color || 'var(--text-primary)',
        animation: color === 'var(--green)' ? 'glow-green 3s ease-in-out infinite' : undefined
      }}>
        {value}
      </div>
      {sub && <div className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>{sub}</div>}
    </div>
  )
}

// ─── Backtest Modal ──────────────────────────────────────────────────────────

function BacktestModal({ isOpen, onClose }) {
  const [startDate, setStartDate] = useState('2026-03-03')
  const [endDate, setEndDate] = useState('2026-03-07')
  const [cash, setCash] = useState(1000000)
  const [status, setStatus] = useState('idle') // idle | running | completed | error
  const [progress, setProgress] = useState([])
  const [currentDay, setCurrentDay] = useState(0)
  const [totalDays, setTotalDays] = useState(0)
  const [currentLayer, setCurrentLayer] = useState(null)
  const [liveEquity, setLiveEquity] = useState([])
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const logRef = useRef(null)

  const addLog = useCallback((msg, type = 'info') => {
    setProgress(prev => [...prev, { msg, type, ts: new Date().toLocaleTimeString() }])
  }, [])

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [progress])

  const runBacktest = async () => {
    setStatus('running')
    setProgress([])
    setLiveEquity([])
    setResult(null)
    setError(null)
    setCurrentDay(0)
    setTotalDays(0)

    try {
      const res = await fetch(`${API_BASE}/api/backtest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start_date: startDate, end_date: endDate, initial_cash: cash }),
      })

      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Failed to start backtest')
      }

      const { run_id } = await res.json()
      addLog(`Backtest started (run: ${run_id})`)

      // Connect to SSE stream
      const evtSource = new EventSource(`${API_BASE}/api/backtest/${run_id}/stream`)

      evtSource.addEventListener('started', (e) => {
        const data = JSON.parse(e.data)
        addLog(`Backtest: ${data.start} to ${data.end}, $${data.cash.toLocaleString()}`)
      })

      evtSource.addEventListener('info', (e) => {
        const data = JSON.parse(e.data)
        setTotalDays(data.total_days)
        addLog(data.message)
      })

      evtSource.addEventListener('day_start', (e) => {
        const data = JSON.parse(e.data)
        setCurrentDay(data.day)
        addLog(`Day ${data.day}/${data.total}: ${data.date}`, 'day')
      })

      evtSource.addEventListener('layer', (e) => {
        const data = JSON.parse(e.data)
        setCurrentLayer(data)
        const emoji = { 1: '🌍', 2: '📊', 3: '🧠', 4: '🎯' }[data.layer] || '▸'
        addLog(`  ${emoji} Layer ${data.layer} — ${data.name}: ${data.status}${data.regime ? ` (${data.regime})` : ''}`, 'layer')
      })

      evtSource.addEventListener('layer_log', (e) => {
        const data = JSON.parse(e.data)
        addLog(`  ${data.message}`, 'layer')
      })

      evtSource.addEventListener('agent_start', (e) => {
        const data = JSON.parse(e.data)
        const icons = { 1: '🌍', 2: '📊', 3: '🧠', 4: '🎯' }
        addLog(`    ${icons[data.layer] || '▸'} ${data.agent} running...`, 'agent')
      })

      evtSource.addEventListener('agent_done', (e) => {
        const data = JSON.parse(e.data)
        let detail = ''
        if (data.signal) detail = ` → ${data.signal} (conv: ${data.conviction})`
        else if (data.picks) detail = ` → ${data.picks} picks`
        addLog(`    ✓ ${data.agent} complete${detail}`, 'agent_done')
      })

      evtSource.addEventListener('trade', (e) => {
        const data = JSON.parse(e.data)
        addLog(`  💰 ${data.message}`, 'trade')
      })

      evtSource.addEventListener('day_complete', (e) => {
        const data = JSON.parse(e.data)
        setLiveEquity(prev => [...prev, { date: data.date.slice(5), nav: data.nav }])
        const arrow = data.nav >= cash ? '↑' : '↓'
        addLog(`  ✓ ${data.regime} | ${data.trades} trades | NAV: $${data.nav.toLocaleString()} ${arrow}`, 'success')
        if (data.trades_detail?.length > 0) {
          data.trades_detail.forEach(t => {
            addLog(`    ${t.action} ${t.ticker} × ${t.shares} @ $${t.price.toFixed(2)}`, 'trade')
          })
        }
      })

      evtSource.addEventListener('completed', (e) => {
        const data = JSON.parse(e.data)
        setResult(data)
        setStatus('completed')
        addLog(`Backtest complete: ${data.total_return_pct > 0 ? '+' : ''}${data.total_return_pct.toFixed(2)}%`, 'complete')
        evtSource.close()
      })

      evtSource.addEventListener('error', (e) => {
        try {
          const data = JSON.parse(e.data)
          setError(data.message)
          addLog(`Error: ${data.message}`, 'error')
        } catch {
          addLog('Connection error', 'error')
        }
        setStatus('error')
        evtSource.close()
      })

      evtSource.onerror = () => {
        if (status === 'running') {
          setStatus('error')
          setError('Connection to server lost')
          addLog('Connection lost', 'error')
        }
        evtSource.close()
      }

    } catch (e) {
      setError(e.message)
      setStatus('error')
      addLog(`Error: ${e.message}`, 'error')
    }
  }

  if (!isOpen) return null

  const logColors = {
    info: 'var(--text-muted)',
    day: 'var(--blue)',
    layer: 'var(--text-muted)',
    agent: '#c084fc',
    agent_done: 'var(--green)',
    success: 'var(--green)',
    trade: 'var(--amber)',
    complete: 'var(--green)',
    error: 'var(--red)',
  }

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center" style={{ background: 'rgba(0,0,0,0.8)', backdropFilter: 'blur(4px)' }}>
      <div className="w-full max-w-3xl max-h-[90vh] overflow-hidden rounded-xl flex flex-col"
        style={{ background: 'var(--bg-card)', border: '1px solid var(--border)' }}>

        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4" style={{ borderBottom: '1px solid var(--border)' }}>
          <div className="flex items-center gap-3">
            <Zap size={18} style={{ color: 'var(--green)' }} />
            <span className="font-bold text-sm tracking-wider" style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>
              RUN BACKTEST
            </span>
          </div>
          <button onClick={onClose} className="p-1 rounded hover:bg-white/5 cursor-pointer transition-colors">
            <X size={18} style={{ color: 'var(--text-muted)' }} />
          </button>
        </div>

        {/* Config */}
        {status === 'idle' && (
          <div className="p-6 space-y-4">
            <div className="grid grid-cols-3 gap-4">
              <div>
                <label className="block text-xs uppercase tracking-wider mb-2" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                  <Calendar size={12} className="inline mr-1" /> Start Date
                </label>
                <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg text-sm outline-none"
                  style={{ background: 'var(--bg-primary)', border: '1px solid var(--border)', color: 'var(--text-primary)', fontFamily: 'var(--font-mono)' }} />
              </div>
              <div>
                <label className="block text-xs uppercase tracking-wider mb-2" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                  <Calendar size={12} className="inline mr-1" /> End Date
                </label>
                <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg text-sm outline-none"
                  style={{ background: 'var(--bg-primary)', border: '1px solid var(--border)', color: 'var(--text-primary)', fontFamily: 'var(--font-mono)' }} />
              </div>
              <div>
                <label className="block text-xs uppercase tracking-wider mb-2" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                  <DollarSign size={12} className="inline mr-1" /> Initial Cash
                </label>
                <input type="number" value={cash} onChange={e => setCash(Number(e.target.value))}
                  className="w-full px-3 py-2 rounded-lg text-sm outline-none"
                  style={{ background: 'var(--bg-primary)', border: '1px solid var(--border)', color: 'var(--text-primary)', fontFamily: 'var(--font-mono)' }} />
              </div>
            </div>

            <div className="flex items-center gap-3 p-3 rounded-lg text-xs" style={{ background: 'rgba(59,130,246,0.08)', border: '1px solid rgba(59,130,246,0.15)', color: 'var(--blue)', fontFamily: 'var(--font-mono)' }}>
              <Activity size={14} />
              <span>25 agents will run per day. Each day costs ~$1-2 in API calls (Claude Sonnet). Ensure ANTHROPIC_API_KEY is set.</span>
            </div>

            <button onClick={runBacktest}
              className="w-full py-3 rounded-lg font-medium text-sm transition-all hover:scale-[1.01] cursor-pointer flex items-center justify-center gap-2"
              style={{ background: 'var(--green)', color: '#07070f', fontFamily: 'var(--font-mono)', boxShadow: '0 0 24px rgba(0,255,136,0.2)' }}>
              <Play size={16} /> Launch Backtest
            </button>
          </div>
        )}

        {/* Progress */}
        {status !== 'idle' && (
          <div className="flex-1 overflow-hidden flex flex-col p-6 gap-4">
            {/* Progress bar */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                  {status === 'running' && (
                    <span className="inline-flex items-center gap-1.5">
                      <Loader2 size={12} className="animate-spin" /> Day {currentDay}/{totalDays}
                      {currentLayer && <span style={{ color: currentLayer.layer <= 2 ? 'var(--blue)' : 'var(--green)' }}> — L{currentLayer.layer} {currentLayer.name}</span>}
                    </span>
                  )}
                  {status === 'completed' && <span style={{ color: 'var(--green)' }}>Completed</span>}
                  {status === 'error' && <span style={{ color: 'var(--red)' }}>Failed</span>}
                </span>
                {totalDays > 0 && (
                  <span className="text-xs" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                    {Math.round((currentDay / totalDays) * 100)}%
                  </span>
                )}
              </div>
              <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.05)' }}>
                <div className="h-full rounded-full transition-all duration-500" style={{
                  width: `${totalDays > 0 ? (currentDay / totalDays) * 100 : 0}%`,
                  background: status === 'error' ? 'var(--red)' : status === 'completed' ? 'var(--green)' : 'linear-gradient(90deg, var(--blue), var(--green))',
                }} />
              </div>
            </div>

            {/* Live equity mini-chart */}
            {liveEquity.length > 1 && (
              <div className="h-24 rounded-lg p-2" style={{ background: 'var(--bg-primary)', border: '1px solid var(--border)' }}>
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={liveEquity}>
                    <defs>
                      <linearGradient id="liveGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#00ff88" stopOpacity={0.2} />
                        <stop offset="100%" stopColor="#00ff88" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <Area type="monotone" dataKey="nav" stroke="#00ff88" strokeWidth={1.5} fill="url(#liveGrad)" dot={false} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}

            {/* Log output */}
            <div ref={logRef} className="flex-1 overflow-y-auto rounded-lg p-4 space-y-0.5 min-h-[200px] max-h-[300px]"
              style={{ background: 'var(--bg-primary)', border: '1px solid var(--border)' }}>
              {progress.map((p, i) => (
                <div key={i} className="text-xs leading-relaxed" style={{ fontFamily: 'var(--font-mono)', color: logColors[p.type] || 'var(--text-muted)' }}>
                  <span style={{ color: 'rgba(255,255,255,0.2)' }}>{p.ts}</span> {p.msg}
                </div>
              ))}
              {status === 'running' && (
                <div className="text-xs" style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>
                  <span className="inline-block animate-pulse">▌</span>
                </div>
              )}
            </div>

            {/* Result summary */}
            {result && (
              <div className="grid grid-cols-3 gap-3">
                <div className="p-3 rounded-lg text-center" style={{ background: 'var(--bg-primary)', border: '1px solid var(--border)' }}>
                  <div className="text-xs mb-1" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>Return</div>
                  <div className="text-lg font-bold" style={{
                    fontFamily: 'var(--font-mono)',
                    color: result.total_return_pct >= 0 ? 'var(--green)' : 'var(--red)',
                  }}>
                    {result.total_return_pct >= 0 ? '+' : ''}{result.total_return_pct.toFixed(2)}%
                  </div>
                </div>
                <div className="p-3 rounded-lg text-center" style={{ background: 'var(--bg-primary)', border: '1px solid var(--border)' }}>
                  <div className="text-xs mb-1" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>Final NAV</div>
                  <div className="text-lg font-bold" style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>
                    ${result.final_nav.toLocaleString()}
                  </div>
                </div>
                <div className="p-3 rounded-lg text-center" style={{ background: 'var(--bg-primary)', border: '1px solid var(--border)' }}>
                  <div className="text-xs mb-1" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>Days</div>
                  <div className="text-lg font-bold" style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>
                    {result.trading_days}
                  </div>
                </div>
              </div>
            )}

            {/* Actions */}
            <div className="flex gap-3">
              {status === 'completed' && (
                <button onClick={() => { setStatus('idle'); setProgress([]); setLiveEquity([]); setResult(null); }}
                  className="flex-1 py-2.5 rounded-lg text-sm cursor-pointer transition-all hover:scale-[1.01] flex items-center justify-center gap-2"
                  style={{ background: 'var(--green)', color: '#07070f', fontFamily: 'var(--font-mono)' }}>
                  <Play size={14} /> Run Another
                </button>
              )}
              {status === 'error' && (
                <button onClick={() => { setStatus('idle'); setProgress([]); setError(null); }}
                  className="flex-1 py-2.5 rounded-lg text-sm cursor-pointer transition-all hover:scale-[1.01] flex items-center justify-center gap-2"
                  style={{ background: 'var(--amber)', color: '#07070f', fontFamily: 'var(--font-mono)' }}>
                  Retry
                </button>
              )}
              <button onClick={onClose}
                className="px-6 py-2.5 rounded-lg text-sm cursor-pointer transition-colors"
                style={{ border: '1px solid var(--border)', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                Close
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Hero Section ────────────────────────────────────────────────────────────

function Hero() {
  const [showBacktest, setShowBacktest] = useState(false)

  return (
    <section className="relative min-h-screen flex flex-col items-center justify-center px-6 overflow-hidden">
      <Starfield />
      <BacktestModal isOpen={showBacktest} onClose={() => setShowBacktest(false)} />
      <div className="relative z-10 max-w-4xl mx-auto text-center">
        {/* Nav bar */}
        <nav className="fixed top-0 left-0 right-0 z-50 flex items-center justify-between px-8 py-4"
          style={{ background: 'rgba(7,7,15,0.85)', backdropFilter: 'blur(12px)', borderBottom: '1px solid var(--border)' }}>
          <div className="flex items-center gap-4">
            <span className="text-xl font-bold tracking-widest" style={{ fontFamily: 'var(--font-mono)', color: 'var(--green)' }}>
              ARGOS
            </span>
            <span className="text-xs px-2 py-0.5 rounded" style={{ background: 'rgba(255,255,255,0.05)', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
              v2.1
            </span>
            <LivePill />
          </div>
          <div className="flex items-center gap-6 text-sm" style={{ color: 'var(--text-muted)' }}>
            <a href="#architecture" className="hover:text-white transition-colors">Architecture</a>
            <a href="#agents" className="hover:text-white transition-colors">Agents</a>
            <a href="#performance" className="hover:text-white transition-colors">Performance</a>
          </div>
        </nav>

        <div className="mt-8">
          <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-xs mb-8"
            style={{ background: 'rgba(0,255,136,0.06)', border: '1px solid rgba(0,255,136,0.15)', color: 'var(--green)', fontFamily: 'var(--font-mono)' }}>
            <Radio size={12} />
            25 AGENTS ONLINE — REGIME: RISK_OFF
          </div>

          <h1 className="text-5xl md:text-6xl lg:text-7xl font-bold tracking-tight leading-tight mb-6"
            style={{ fontFamily: 'var(--font-sans)' }}>
            <span style={{ color: 'var(--text-primary)' }}>25 AI Agents. </span>
            <span style={{ color: 'var(--green)' }}>One Sharpe Ratio.</span>
            <br />
            <span style={{ color: 'var(--text-primary)' }}>Self-Improving.</span>
          </h1>

          <p className="text-lg max-w-2xl mx-auto mb-10 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            Karpathy's autoresearch methodology applied to financial markets.{' '}
            <span style={{ color: 'var(--text-primary)' }}>Prompts are weights.</span>{' '}
            <span style={{ color: 'var(--text-primary)' }}>Markets are the loss function.</span>
          </p>

          <div className="flex items-center justify-center gap-4">
            <button className="px-6 py-3 rounded-lg font-medium text-sm transition-all hover:scale-105 cursor-pointer"
              onClick={() => setShowBacktest(true)}
              style={{
                background: 'var(--green)',
                color: '#07070f',
                fontFamily: 'var(--font-mono)',
                boxShadow: '0 0 24px rgba(0,255,136,0.25)',
              }}>
              <span className="flex items-center gap-2"><Zap size={16} /> Run Backtest</span>
            </button>
            <button className="px-6 py-3 rounded-lg font-medium text-sm transition-all hover:scale-105 cursor-pointer"
              onClick={() => document.getElementById('architecture')?.scrollIntoView({ behavior: 'smooth' })}
              style={{
                background: 'transparent',
                color: 'var(--text-primary)',
                border: '1px solid var(--border)',
                fontFamily: 'var(--font-mono)',
              }}>
              <span className="flex items-center gap-2"><Eye size={16} /> View Agent Network</span>
            </button>
          </div>
        </div>

        <div className="absolute bottom-8 left-1/2 -translate-x-1/2 flex flex-col items-center gap-2" style={{ color: 'var(--text-muted)' }}>
          <span className="text-xs" style={{ fontFamily: 'var(--font-mono)' }}>SCROLL</span>
          <ChevronDown size={16} style={{ animation: 'flow-down 1.5s ease-in-out infinite' }} />
        </div>
      </div>
    </section>
  )
}

// ─── Architecture Visualizer ─────────────────────────────────────────────────

const LAYERS = [
  {
    name: 'MACRO',
    subtitle: '10 Agents — Parallel',
    color: 'var(--blue)',
    icon: Globe,
    output: 'RISK_ON / RISK_OFF / NEUTRAL',
    agents: ['Central Bank', 'Geopolitical', 'China', 'Dollar', 'Yield Curve', 'Commodities', 'Volatility', 'Emerging Markets', 'News Sentiment', 'Inst. Flow'],
  },
  {
    name: 'SECTOR DESKS',
    subtitle: '7 Agents — Parallel',
    color: 'var(--amber)',
    icon: BarChart3,
    output: 'LONG / SHORT picks per sector',
    agents: ['Semiconductor', 'Energy', 'Biotech', 'Consumer', 'Industrials', 'Financials', 'Relationship Mapper'],
  },
  {
    name: 'SUPERINVESTORS',
    subtitle: '4 Agents — Parallel',
    color: '#c084fc',
    icon: Users,
    output: 'Filtered picks through investment philosophy',
    agents: ['Druckenmiller', 'Aschenbrenner', 'Baker', 'Ackman'],
  },
  {
    name: 'DECISION',
    subtitle: '4 Agents — Sequential',
    color: 'var(--green)',
    icon: Target,
    output: 'Final portfolio actions',
    agents: ['CRO (Risk)', 'Alpha Discovery', 'Autonomous Execution', 'CIO (Final)'],
  },
]

function ArchitectureVisualizer() {
  const [expanded, setExpanded] = useState(null)

  return (
    <section id="architecture" className="px-6 py-20 max-w-5xl mx-auto">
      <SectionTitle icon={Cpu}>PIPELINE ARCHITECTURE</SectionTitle>
      <p className="text-sm mb-10" style={{ color: 'var(--text-muted)' }}>
        Each trading day, data flows through 4 sequential layers. 25 agents analyze, filter, and synthesize signals into portfolio actions.
      </p>

      <div className="flex flex-col items-center gap-0">
        {LAYERS.map((layer, i) => (
          <div key={layer.name} className="flex flex-col items-center w-full max-w-2xl">
            {i > 0 && (
              <div className="flex flex-col items-center py-2">
                <ArrowDown size={20} style={{ color: 'var(--text-muted)', animation: 'flow-down 1.5s ease-in-out infinite', animationDelay: `${i * 0.3}s` }} />
              </div>
            )}

            <div
              className="w-full rounded-xl p-5 cursor-pointer transition-all hover:scale-[1.01]"
              style={{
                background: 'var(--bg-card)',
                border: `1px solid ${expanded === i ? layer.color : 'var(--border)'}`,
                boxShadow: expanded === i ? `0 0 20px ${layer.color}22` : 'none',
              }}
              onClick={() => setExpanded(expanded === i ? null : i)}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div className="w-8 h-8 rounded-lg flex items-center justify-center" style={{ background: `${layer.color}15` }}>
                    <layer.icon size={16} style={{ color: layer.color }} />
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-sm tracking-wider" style={{ fontFamily: 'var(--font-mono)', color: layer.color }}>
                        LAYER {i + 1} — {layer.name}
                      </span>
                    </div>
                    <span className="text-xs" style={{ color: 'var(--text-muted)' }}>{layer.subtitle}</span>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-xs px-2 py-1 rounded hidden md:inline" style={{ background: 'rgba(255,255,255,0.04)', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                    {layer.output}
                  </span>
                  {expanded === i ? <ChevronDown size={16} style={{ color: 'var(--text-muted)' }} /> : <ChevronRight size={16} style={{ color: 'var(--text-muted)' }} />}
                </div>
              </div>

              {expanded === i && (
                <div className="mt-4 pt-4 flex flex-wrap gap-2" style={{ borderTop: '1px solid var(--border)' }}>
                  {layer.agents.map(agent => {
                    const agentData = AGENTS.find(a => a.name === agent || a.name.startsWith(agent.split(' ')[0]))
                    const dotColor = agentData?.status === 'MUTATING' ? 'var(--amber)' : agentData?.status === 'COOLDOWN' ? 'var(--red)' : 'var(--green)'
                    return (
                      <span key={agent} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs"
                        style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid var(--border)', fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>
                        <span className="w-1.5 h-1.5 rounded-full" style={{ background: dotColor }} />
                        {agent}
                      </span>
                    )
                  })}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

// ─── Autoresearch Loop ───────────────────────────────────────────────────────

const LOOP_STEPS = [
  { label: 'Identify Worst', desc: 'Lowest rolling Sharpe', color: 'var(--red)', icon: AlertTriangle },
  { label: 'Mutate Prompt', desc: 'Claude generates edit', color: 'var(--amber)', icon: Brain },
  { label: 'Git Branch', desc: 'Feature branch created', color: 'var(--blue)', icon: GitBranch },
  { label: 'Observe 5 Days', desc: 'Track performance', color: '#c084fc', icon: Eye },
  { label: 'Keep or Revert', desc: 'Sharpe improved?', color: 'var(--green)', icon: CheckCircle },
]

function AutoresearchLoop() {
  const [activeStep, setActiveStep] = useState(0)

  useEffect(() => {
    const interval = setInterval(() => {
      setActiveStep(s => (s + 1) % LOOP_STEPS.length)
    }, 2500)
    return () => clearInterval(interval)
  }, [])

  const cx = 200, cy = 200, r = 140

  return (
    <section className="px-6 py-20 max-w-5xl mx-auto">
      <SectionTitle icon={Brain}>AUTORESEARCH ENGINE</SectionTitle>
      <p className="text-sm mb-2" style={{ color: 'var(--text-muted)' }}>
        The core innovation — automatic prompt evolution measured by Sharpe ratio.
      </p>
      <p className="text-xs mb-10 italic" style={{ color: 'var(--amber)', fontFamily: 'var(--font-mono)' }}>
        "Prompts are the weights. Sharpe is the loss function."
      </p>

      <div className="flex flex-col lg:flex-row items-center gap-12">
        <div className="relative flex-shrink-0" style={{ width: 400, height: 400 }}>
          <svg viewBox="0 0 400 400" className="w-full h-full">
            <circle cx={cx} cy={cy} r={r} fill="none" stroke="rgba(255,255,255,0.04)" strokeWidth="2" />

            <circle cx={cx} cy={cy} r={r} fill="none"
              stroke={LOOP_STEPS[activeStep].color}
              strokeWidth="2"
              strokeDasharray={`${2 * Math.PI * r * ((activeStep + 1) / LOOP_STEPS.length)} ${2 * Math.PI * r}`}
              strokeLinecap="round"
              transform={`rotate(-90 ${cx} ${cy})`}
              style={{ transition: 'stroke-dasharray 0.5s ease, stroke 0.5s ease', opacity: 0.4 }}
            />

            {LOOP_STEPS.map((step, i) => {
              const angle = (i / LOOP_STEPS.length) * 2 * Math.PI - Math.PI / 2
              const x = cx + r * Math.cos(angle)
              const y = cy + r * Math.sin(angle)
              const isActive = i === activeStep
              return (
                <g key={i}>
                  <line x1={cx} y1={cy} x2={x} y2={y} stroke="rgba(255,255,255,0.04)" strokeWidth="1" />
                  <circle cx={x} cy={y} r={isActive ? 28 : 22}
                    fill={isActive ? `${step.color}22` : 'var(--bg-card)'}
                    stroke={isActive ? step.color : 'var(--border)'}
                    strokeWidth={isActive ? 2 : 1}
                    style={{ transition: 'all 0.5s ease' }}
                  />
                  <text x={x} y={y + 1} textAnchor="middle" dominantBaseline="middle"
                    fill={isActive ? step.color : 'var(--text-muted)'}
                    fontSize="12" fontFamily="var(--font-mono)" fontWeight="bold">
                    {i + 1}
                  </text>
                </g>
              )
            })}

            <text x={cx} y={cy - 8} textAnchor="middle" fill="var(--text-primary)" fontSize="11" fontFamily="var(--font-mono)" fontWeight="bold">
              AUTORESEARCH
            </text>
            <text x={cx} y={cy + 10} textAnchor="middle" fill="var(--text-muted)" fontSize="9" fontFamily="var(--font-mono)">
              LOOP
            </text>
          </svg>

          <div className="absolute inset-0 pointer-events-none" style={{
            animation: 'rotate-loop 12.5s linear infinite',
          }}>
            <div className="absolute w-3 h-3 rounded-full" style={{
              top: '8%',
              left: '50%',
              transform: 'translateX(-50%)',
              background: LOOP_STEPS[activeStep].color,
              boxShadow: `0 0 16px ${LOOP_STEPS[activeStep].color}`,
              transition: 'background 0.5s, box-shadow 0.5s',
            }} />
          </div>
        </div>

        <div className="flex-1 space-y-3">
          {LOOP_STEPS.map((step, i) => {
            const isActive = i === activeStep
            const StepIcon = step.icon
            return (
              <div key={i} className="flex items-center gap-4 p-3 rounded-lg transition-all"
                style={{
                  background: isActive ? `${step.color}08` : 'transparent',
                  border: `1px solid ${isActive ? `${step.color}30` : 'transparent'}`,
                }}>
                <div className="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0"
                  style={{ background: isActive ? `${step.color}20` : 'rgba(255,255,255,0.03)' }}>
                  <StepIcon size={14} style={{ color: isActive ? step.color : 'var(--text-muted)' }} />
                </div>
                <div>
                  <div className="text-sm font-medium" style={{ fontFamily: 'var(--font-mono)', color: isActive ? step.color : 'var(--text-primary)' }}>
                    {step.label}
                  </div>
                  <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{step.desc}</div>
                </div>
                {i === 4 && (
                  <div className="ml-auto flex gap-2">
                    <span className="text-xs px-2 py-0.5 rounded" style={{ background: 'rgba(0,255,136,0.1)', color: 'var(--green)', fontFamily: 'var(--font-mono)' }}>MERGE</span>
                    <span className="text-xs px-2 py-0.5 rounded" style={{ background: 'rgba(239,68,68,0.1)', color: 'var(--red)', fontFamily: 'var(--font-mono)' }}>REVERT</span>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}

// ─── Agent Scoreboard ────────────────────────────────────────────────────────

function AgentScoreboard() {
  const sorted = useMemo(() => [...AGENTS].sort((a, b) => b.sharpe - a.sharpe), [])
  const worst = sorted[sorted.length - 1]

  const sharpeColor = (s) => s > 1 ? 'var(--green)' : s > 0 ? 'var(--amber)' : 'var(--red)'
  const statusBadge = (status) => {
    const styles = {
      ACTIVE: { bg: 'rgba(0,255,136,0.1)', color: 'var(--green)' },
      COOLDOWN: { bg: 'rgba(59,130,246,0.1)', color: 'var(--blue)' },
      MUTATING: { bg: 'rgba(245,158,11,0.1)', color: 'var(--amber)' },
    }
    const s = styles[status]
    return (
      <span className="text-xs px-2 py-0.5 rounded" style={{ background: s.bg, color: s.color, fontFamily: 'var(--font-mono)' }}>
        {status}
      </span>
    )
  }

  const layerLabel = (layer) => {
    const colors = { MACRO: 'var(--blue)', SECTOR: 'var(--amber)', SUPER: '#c084fc', DECISION: 'var(--green)' }
    return (
      <span className="text-xs" style={{ color: colors[layer], fontFamily: 'var(--font-mono)' }}>{layer}</span>
    )
  }

  return (
    <section id="agents" className="px-6 py-20 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <SectionTitle icon={Activity}>AGENT SCOREBOARD</SectionTitle>
        <span className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs"
          style={{
            background: 'rgba(245,158,11,0.1)',
            color: 'var(--amber)',
            border: '1px solid rgba(245,158,11,0.2)',
            fontFamily: 'var(--font-mono)',
            animation: 'glow-amber 3s ease-in-out infinite',
          }}>
          <Zap size={12} /> AUTORESEARCH ACTIVE
        </span>
      </div>

      <div className="rounded-xl overflow-hidden" style={{ background: 'var(--bg-card)', border: '1px solid var(--border)' }}>
        <div className="grid gap-2 px-5 py-3 text-xs uppercase tracking-wider"
          style={{
            gridTemplateColumns: '1fr 80px 100px 180px 90px 100px',
            color: 'var(--text-muted)',
            fontFamily: 'var(--font-mono)',
            borderBottom: '1px solid var(--border)',
          }}>
          <span>Agent</span>
          <span>Layer</span>
          <span className="text-right">Sharpe</span>
          <span>Weight</span>
          <span>Status</span>
          <span>Last Mut.</span>
        </div>

        {sorted.map((agent, i) => {
          const isTop = i === 0
          const isBottom = agent === worst
          const weightPct = ((agent.weight - 0.3) / (2.5 - 0.3)) * 100

          return (
            <div key={agent.name}
              className="grid gap-2 px-5 py-3 items-center transition-colors hover:bg-white/[0.02]"
              style={{
                gridTemplateColumns: '1fr 80px 100px 180px 90px 100px',
                borderBottom: '1px solid var(--border)',
                background: isTop ? 'rgba(0,255,136,0.03)' : isBottom ? 'rgba(245,158,11,0.03)' : 'transparent',
              }}>
              <div className="flex items-center gap-2">
                {isTop && <span className="text-xs" style={{ color: 'var(--green)' }}>&#9733;</span>}
                {isBottom && <span className="text-xs" style={{ color: 'var(--amber)' }}>&#9888;</span>}
                <span className="text-sm font-medium" style={{
                  fontFamily: 'var(--font-mono)',
                  color: isTop ? 'var(--green)' : isBottom ? 'var(--amber)' : 'var(--text-primary)',
                }}>
                  {agent.name}
                </span>
              </div>

              {layerLabel(agent.layer)}

              <span className="text-sm font-bold text-right" style={{
                fontFamily: 'var(--font-mono)',
                color: sharpeColor(agent.sharpe),
              }}>
                {agent.sharpe.toFixed(2)}
              </span>

              <div className="flex items-center gap-2">
                <div className="flex-1 h-1.5 rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.05)' }}>
                  <div className="h-full rounded-full transition-all" style={{
                    width: `${weightPct}%`,
                    background: `linear-gradient(90deg, var(--blue), ${agent.weight > 1.5 ? 'var(--green)' : 'var(--blue)'})`,
                  }} />
                </div>
                <span className="text-xs w-8 text-right" style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>
                  {agent.weight.toFixed(1)}
                </span>
              </div>

              {statusBadge(agent.status)}

              <span className="text-xs" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                {agent.lastMutation.slice(5)}
              </span>
            </div>
          )
        })}
      </div>
    </section>
  )
}

// ─── Performance Panel ───────────────────────────────────────────────────────

function PerformancePanel() {
  const lastNav = EQUITY_DATA[EQUITY_DATA.length - 1].nav
  const totalReturn = ((lastNav - 1000000) / 1000000 * 100).toFixed(2)
  const maxDD = Math.min(...EQUITY_DATA.map(d => d.drawdown)).toFixed(2)

  return (
    <section id="performance" className="px-6 py-20 max-w-6xl mx-auto">
      <SectionTitle icon={TrendingUp}>PERFORMANCE</SectionTitle>
      <div className="text-xs mb-8" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
        Backtest: 2026-01-02 &rarr; 2026-03-07 &middot; $1,000,000 initial capital
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
        <StatCard label="Total Return" value={`${totalReturn > 0 ? '+' : ''}${totalReturn}%`} color={totalReturn > 0 ? 'var(--green)' : 'var(--red)'} sub="Compounded" />
        <StatCard label="Sharpe Ratio" value="1.24" color="var(--green)" sub="Per-rec basis, ddof=1" />
        <StatCard label="Max Drawdown" value={`${maxDD}%`} color="var(--red)" sub="Running peak" />
        <StatCard label="Win Rate" value="64.2%" color="var(--text-primary)" sub="128 / 199 scored recs" />
      </div>

      <div className="grid grid-cols-2 gap-4 mb-8">
        <div className="p-4 rounded-lg" style={{ background: 'var(--bg-card)', border: '1px solid var(--border)' }}>
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs uppercase tracking-wider" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>Gross Exposure</span>
            <span className="text-sm font-bold" style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>0.87x</span>
          </div>
          <div className="h-2 rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.05)' }}>
            <div className="h-full rounded-full" style={{ width: '58%', background: 'linear-gradient(90deg, var(--blue), var(--green))' }} />
          </div>
          <div className="text-xs mt-1 text-right" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>max 1.5x</div>
        </div>
        <div className="p-4 rounded-lg" style={{ background: 'var(--bg-card)', border: '1px solid var(--border)' }}>
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs uppercase tracking-wider" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>Net Exposure</span>
            <span className="text-sm font-bold" style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>0.42x</span>
          </div>
          <div className="h-2 rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.05)' }}>
            <div className="h-full rounded-full" style={{ width: '52%', background: 'linear-gradient(90deg, var(--amber), var(--green))' }} />
          </div>
          <div className="text-xs mt-1 text-right" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>max 0.8x</div>
        </div>
      </div>

      <div className="p-6 rounded-xl" style={{ background: 'var(--bg-card)', border: '1px solid var(--border)' }}>
        <div className="flex items-center justify-between mb-4">
          <span className="text-xs uppercase tracking-wider" style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>Equity Curve</span>
          <span className="text-sm font-bold" style={{ fontFamily: 'var(--font-mono)', color: 'var(--green)' }}>
            ${lastNav.toLocaleString()}
          </span>
        </div>
        <ResponsiveContainer width="100%" height={280}>
          <AreaChart data={EQUITY_DATA}>
            <defs>
              <linearGradient id="navGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#00ff88" stopOpacity={0.15} />
                <stop offset="100%" stopColor="#00ff88" stopOpacity={0} />
              </linearGradient>
            </defs>
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#64748b', fontFamily: 'var(--font-mono)' }} axisLine={false} tickLine={false} interval={4} />
            <YAxis tick={{ fontSize: 10, fill: '#64748b', fontFamily: 'var(--font-mono)' }} axisLine={false} tickLine={false}
              tickFormatter={(v) => `$${(v / 1000).toFixed(0)}k`} domain={['dataMin - 5000', 'dataMax + 5000']} />
            <Tooltip
              contentStyle={{ background: '#0d0d1a', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '8px', fontFamily: 'var(--font-mono)', fontSize: 12 }}
              labelStyle={{ color: '#64748b' }}
              formatter={(v) => [`$${v.toLocaleString()}`, 'NAV']}
            />
            <Area type="monotone" dataKey="nav" stroke="#00ff88" strokeWidth={2} fill="url(#navGradient)" dot={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </section>
  )
}

// ─── News Crawl ──────────────────────────────────────────────────────────────

function NewsCrawl() {
  const doubled = [...NEWS_ITEMS, ...NEWS_ITEMS]

  return (
    <div className="fixed bottom-0 left-0 right-0 z-50 overflow-hidden py-2.5"
      style={{ background: 'rgba(7,7,15,0.92)', backdropFilter: 'blur(8px)', borderTop: '1px solid var(--border)' }}>
      <div className="flex items-center whitespace-nowrap" style={{ animation: 'crawl 60s linear infinite' }}>
        {doubled.map((item, i) => (
          <span key={i} className="inline-flex items-center gap-3 mx-6 text-xs" style={{ fontFamily: 'var(--font-mono)' }}>
            <span className="w-1 h-1 rounded-full flex-shrink-0" style={{
              background: item.startsWith('MACRO') ? 'var(--blue)' :
                item.startsWith('SEMI') || item.startsWith('ENERGY') || item.startsWith('BIOTECH') ? 'var(--amber)' :
                item.startsWith('RISK') || item.startsWith('REGIME') ? 'var(--red)' :
                item.startsWith('AUTORESEARCH') ? '#c084fc' :
                item.startsWith('SUPER') ? '#c084fc' : 'var(--green)'
            }} />
            <span style={{ color: 'var(--text-muted)' }}>{item}</span>
          </span>
        ))}
      </div>
    </div>
  )
}

// ─── App ─────────────────────────────────────────────────────────────────────

export default function App() {
  return (
    <div style={{ background: 'var(--bg-primary)', minHeight: '100vh', paddingBottom: '48px' }}>
      <Hero />
      <ArchitectureVisualizer />
      <AutoresearchLoop />
      <AgentScoreboard />
      <PerformancePanel />
      <NewsCrawl />
    </div>
  )
}
