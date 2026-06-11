import { useEffect, useRef, useState, useCallback } from 'react'
import { api } from './api'
import {
  Activity, TrendingUp, TrendingDown, Shield, Zap, StopCircle,
  RefreshCw, Play, Square, ChevronRight, BarChart2, Cpu, Wallet,
  AlertTriangle, CheckCircle, Clock, Eye
} from 'lucide-react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, BarChart, Bar, Cell } from 'recharts'

// ── Helpers ──────────────────────────────────────────────────────────────────

const fmt = (ts: string) => {
  if (!ts) return '—'
  const d = new Date(ts)
  return d.toLocaleString('en-US', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}
const usd = (n?: number | null) => n != null ? `$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—'
const pct = (n?: number | null) => n != null ? `${(n * 100).toFixed(1)}%` : '—'
const sign = (n?: number | null) => n != null ? (n >= 0 ? `+${(n*100).toFixed(1)}%` : `${(n*100).toFixed(1)}%`) : '—'
const clx = (...c: (string | false | undefined)[]) => c.filter(Boolean).join(' ')

// ── Stat Card ────────────────────────────────────────────────────────────────

function StatCard({ label, value, sub, color, icon: Icon }: {
  label: string; value: string; sub?: string; color?: string; icon?: any
}) {
  return (
    <div className="card flex flex-col gap-1 min-w-0">
      <div className="flex items-center gap-1.5">
        {Icon && <Icon size={11} className="text-terminal-muted shrink-0" />}
        <span className="label mb-0">{label}</span>
      </div>
      <span className={clx('text-2xl font-bold tracking-tight truncate', color || 'text-white')}>{value}</span>
      {sub && <span className="text-xs text-terminal-muted truncate">{sub}</span>}
    </div>
  )
}

// ── Position Row ─────────────────────────────────────────────────────────────

function PositionRow({ p }: { p: any }) {
  const pnl = p.pnl_pct
  const locked = p.stop_loss && p.entry_price ? ((p.stop_loss - p.entry_price) / p.entry_price) : null
  return (
    <tr className="table-row">
      <td className="td font-bold text-white">{p.ticker}</td>
      <td className="td"><span className="text-terminal-muted text-xs">{p.asset_type}</span></td>
      <td className="td">{usd(p.entry_price)}</td>
      <td className="td">{p.current_price ? <span className={pnl >= 0 ? 'text-terminal-green' : 'text-terminal-red'}>{usd(p.current_price)}</span> : '—'}</td>
      <td className="td text-yellow-400 font-medium">{usd(p.high_water)}</td>
      <td className="td">
        <span className="text-terminal-red">{usd(p.stop_loss)}</span>
        {locked != null && <span className="text-xs text-terminal-muted ml-1">({locked >= 0 ? '+' : ''}{(locked*100).toFixed(1)}%)</span>}
      </td>
      <td className="td text-terminal-cyan">{usd(p.take_profit)}</td>
      <td className="td">
        {pnl != null
          ? <span className={clx('font-semibold', pnl >= 0 ? 'text-terminal-green' : 'text-terminal-red')}>{sign(pnl)}</span>
          : '—'}
      </td>
      <td className="td text-terminal-muted text-xs">{fmt(p.opened_at)}</td>
    </tr>
  )
}

// ── Decision Row ─────────────────────────────────────────────────────────────

function DecisionRow({ d, onExpand }: { d: any; onExpand: (d: any) => void }) {
  const actionClass: Record<string, string> = { BUY: 'badge-buy', SELL: 'badge-sell', HOLD: 'badge-hold' }
  return (
    <tr className="table-row cursor-pointer" onClick={() => onExpand(d)}>
      <td className="td text-terminal-muted text-xs">{fmt(d.created_at)}</td>
      <td className="td font-bold text-white">{d.ticker}</td>
      <td className="td"><span className={actionClass[d.action] || 'badge'}>{d.action}</span></td>
      <td className="td text-terminal-muted">{d.rating}</td>
      <td className="td">
        <div className="flex items-center gap-1.5">
          <div className="h-1.5 w-16 bg-terminal-border rounded-full overflow-hidden">
            <div className="h-full bg-terminal-green rounded-full" style={{ width: `${(d.confidence || 0) * 100}%` }} />
          </div>
          <span className="text-xs">{d.confidence != null ? `${(d.confidence*100).toFixed(0)}%` : '—'}</span>
        </div>
      </td>
      <td className="td text-terminal-cyan">{d.price_target ? usd(d.price_target) : '—'}</td>
      <td className="td max-w-xs">
        <span className="text-terminal-muted text-xs line-clamp-1">{(d.investment_thesis || '').slice(0, 90)}{d.investment_thesis?.length > 90 ? '…' : ''}</span>
      </td>
      <td className="td"><ChevronRight size={12} className="text-terminal-muted" /></td>
    </tr>
  )
}

// ── Exec Row ─────────────────────────────────────────────────────────────────

function ExecRow({ e }: { e: any }) {
  const statusClass: Record<string, string> = { submitted: 'badge-submitted', blocked: 'badge-blocked', dry_run: 'badge-dry_run', error: 'badge-error' }
  const sideClass: Record<string, string> = { buy: 'badge-buy', sell: 'badge-sell' }
  return (
    <tr className="table-row">
      <td className="td text-terminal-muted text-xs">{fmt(e.created_at)}</td>
      <td className="td font-bold text-white">{e.ticker}</td>
      <td className="td"><span className={sideClass[e.side] || 'badge'}>{e.side?.toUpperCase()}</span></td>
      <td className="td">{usd(e.notional_usd)}</td>
      <td className="td"><span className={statusClass[e.status] || 'badge'}>{e.status}</span></td>
      <td className="td text-terminal-muted text-xs truncate max-w-[120px]">{e.order_id || '—'}</td>
      <td className="td text-terminal-muted text-xs">{e.block_reason || ''}</td>
    </tr>
  )
}

// ── Detail Modal ──────────────────────────────────────────────────────────────

function DetailModal({ d, onClose }: { d: any; onClose: () => void }) {
  if (!d) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" />
      <div className="relative bg-terminal-card border border-terminal-border rounded-xl w-full max-w-2xl max-h-[80vh] overflow-y-auto p-6 shadow-2xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-start justify-between mb-4">
          <div>
            <span className="text-2xl font-bold text-white">{d.ticker}</span>
            <span className={clx('ml-3', d.action === 'BUY' ? 'badge-buy' : d.action === 'SELL' ? 'badge-sell' : 'badge-hold')}>{d.action}</span>
          </div>
          <button onClick={onClose} className="text-terminal-muted hover:text-white text-xl leading-none">×</button>
        </div>
        <div className="grid grid-cols-2 gap-3 mb-4 text-sm">
          <div><span className="text-terminal-muted">Rating: </span><span className="text-white">{d.rating}</span></div>
          <div><span className="text-terminal-muted">Confidence: </span><span className="text-terminal-green">{d.confidence != null ? `${(d.confidence*100).toFixed(0)}%` : '—'}</span></div>
          <div><span className="text-terminal-muted">Price Target: </span><span className="text-terminal-cyan">{d.price_target ? usd(d.price_target) : '—'}</span></div>
          <div><span className="text-terminal-muted">Date: </span><span className="text-white">{fmt(d.created_at)}</span></div>
        </div>
        <div className="mb-4">
          <div className="label">Investment Thesis</div>
          <p className="text-sm text-terminal-text leading-relaxed">{d.investment_thesis}</p>
        </div>
        {d.raw_state && (() => {
          try {
            const s = typeof d.raw_state === 'string' ? JSON.parse(d.raw_state) : d.raw_state
            const reports = [
              { k: 'Market', v: s.market_report }, { k: 'Sentiment', v: s.sentiment_report },
              { k: 'News', v: s.news_report }, { k: 'Fundamentals', v: s.fundamentals_report },
              { k: 'Options Flow', v: s.options_report },
            ].filter(r => r.v)
            if (!reports.length) return null
            return (
              <div>
                <div className="label">Analyst Reports</div>
                <div className="space-y-2">
                  {reports.map(r => (
                    <div key={r.k} className="bg-terminal-surface rounded p-3">
                      <div className="text-terminal-cyan text-xs font-semibold mb-1">{r.k}</div>
                      <p className="text-xs text-terminal-muted leading-relaxed">{(r.v || '').slice(0, 300)}{(r.v?.length || 0) > 300 ? '…' : ''}</p>
                    </div>
                  ))}
                </div>
              </div>
            )
          } catch { return null }
        })()}
      </div>
    </div>
  )
}

// ── Backtest Panel ────────────────────────────────────────────────────────────

function BacktestPanel() {
  const [ticker, setTicker] = useState('AAPL')
  const [period, setPeriod] = useState('1y')
  const [result, setResult] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const run = async () => {
    setLoading(true); setErr(''); setResult(null)
    try { setResult(await api.backtest(ticker.toUpperCase(), period)) }
    catch (e: any) { setErr(e.message) }
    finally { setLoading(false) }
  }

  const metrics = result ? [
    { label: 'Total Return', value: result.total_return != null ? sign(result.total_return) : '—', color: result.total_return >= 0 ? 'text-terminal-green' : 'text-terminal-red' },
    { label: 'Sharpe Ratio', value: result.sharpe_ratio?.toFixed(2) || '—', color: 'text-terminal-cyan' },
    { label: 'Max Drawdown', value: result.max_drawdown != null ? `${(result.max_drawdown*100).toFixed(1)}%` : '—', color: 'text-terminal-red' },
    { label: 'Win Rate', value: pct(result.win_rate), color: 'text-yellow-400' },
    { label: 'Total Trades', value: result.total_trades?.toString() || '—', color: 'text-white' },
    { label: 'Avg Trade', value: result.avg_trade_return != null ? sign(result.avg_trade_return) : '—', color: 'text-terminal-muted' },
  ] : []

  return (
    <div className="card">
      <div className="label flex items-center gap-2"><BarChart2 size={11} />Backtester</div>
      <div className="flex items-center gap-2 mt-3 mb-4">
        <input value={ticker} onChange={e => setTicker(e.target.value.toUpperCase())}
          className="bg-terminal-surface border border-terminal-border rounded px-3 py-1.5 text-sm text-white w-24 uppercase focus:outline-none focus:border-terminal-cyan"
          placeholder="AAPL" />
        <select value={period} onChange={e => setPeriod(e.target.value)}
          className="bg-terminal-surface border border-terminal-border rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-terminal-cyan">
          {['3mo','6mo','1y','2y','3y'].map(p => <option key={p} value={p}>{p}</option>)}
        </select>
        <button onClick={run} disabled={loading} className="btn-muted flex items-center gap-1.5">
          {loading ? <RefreshCw size={11} className="animate-spin" /> : <Play size={11} />}
          {loading ? 'Running…' : 'Run Backtest'}
        </button>
      </div>
      {err && <div className="text-terminal-red text-xs mb-3">{err}</div>}
      {result && (
        <>
          <div className="grid grid-cols-3 gap-3 mb-3">
            {metrics.map(m => (
              <div key={m.label} className="bg-terminal-surface rounded p-3">
                <div className="label text-[10px] mb-1">{m.label}</div>
                <div className={clx('text-lg font-bold', m.color)}>{m.value}</div>
              </div>
            ))}
          </div>
          <div className="text-xs text-terminal-muted bg-terminal-surface rounded p-2">{result.summary}</div>
        </>
      )}
    </div>
  )
}

// ── Regime Badge ──────────────────────────────────────────────────────────────

function RegimeBadge({ regime }: { regime: any }) {
  if (!regime) return null
  const colors: Record<string, string> = { bull: 'text-terminal-green border-green-800 bg-green-950', neutral: 'text-yellow-400 border-yellow-800 bg-yellow-950', bear: 'text-terminal-red border-red-900 bg-red-950', crash: 'text-red-300 border-red-900 bg-red-950' }
  const icons: Record<string, string> = { bull: '▲', neutral: '◆', bear: '▼', crash: '☠' }
  const c = colors[regime.regime] || 'text-terminal-muted border-terminal-border bg-terminal-surface'
  return (
    <span className={clx('inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-semibold', c)}>
      <span>{icons[regime.regime] || '◆'}</span>
      <span>{(regime.regime || '').toUpperCase()}</span>
      <span className="opacity-60">VIX {regime.vix?.toFixed(0)}</span>
      <span className="opacity-60">{regime.kelly_multiplier != null ? `${regime.kelly_multiplier}x Kelly` : ''}</span>
    </span>
  )
}

// ── P&L Sparkline ─────────────────────────────────────────────────────────────

function PnlChart({ executions }: { executions: any[] }) {
  const data = executions
    .filter(e => e.status === 'submitted' && e.notional_usd)
    .slice(0, 20)
    .reverse()
    .map((e, i) => ({ i, v: e.side === 'sell' ? e.notional_usd : -e.notional_usd }))
  if (!data.length) return <div className="text-terminal-muted text-xs text-center py-4">No execution data yet</div>
  return (
    <ResponsiveContainer width="100%" height={80}>
      <BarChart data={data} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
        <Bar dataKey="v" radius={[2, 2, 0, 0]}>
          {data.map((d, i) => <Cell key={i} fill={d.v >= 0 ? '#00ff88' : '#ff4757'} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

// ── Main App ──────────────────────────────────────────────────────────────────

type Tab = 'overview' | 'positions' | 'decisions' | 'executions' | 'backtest'

export default function App() {
  const [tab, setTab] = useState<Tab>('overview')
  const [status, setStatus] = useState<any>(null)
  const [positions, setPositions] = useState<any[]>([])
  const [decisions, setDecisions] = useState<any[]>([])
  const [executions, setExecutions] = useState<any[]>([])
  const [metrics, setMetrics] = useState<any>(null)
  const [portfolio, setPortfolio] = useState<any>(null)
  const [regime, setRegime] = useState<any>(null)
  const [selected, setSelected] = useState<any>(null)
  const [lastUpdate, setLastUpdate] = useState('')
  const [loading, setLoading] = useState(false)
  const [toast, setToast] = useState('')

  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(''), 3000)
  }

  const load = useCallback(async () => {
    try {
      const [s, pos, dec, exc, met, reg, port] = await Promise.allSettled([
        api.status(), api.positions(), api.decisions(), api.executions(), api.metrics(), api.regime(), api.portfolio()
      ])
      if (s.status === 'fulfilled') setStatus(s.value)
      if (pos.status === 'fulfilled') setPositions(pos.value)
      if (dec.status === 'fulfilled') setDecisions(dec.value)
      if (exc.status === 'fulfilled') setExecutions(exc.value)
      if (met.status === 'fulfilled') setMetrics(met.value)
      if (reg.status === 'fulfilled') setRegime(reg.value)
      if (port.status === 'fulfilled') setPortfolio(port.value)
      setLastUpdate(new Date().toLocaleTimeString('en-US', { hour12: false }))
    } catch {}
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 10_000)
    return () => clearInterval(t)
  }, [load])

  const handleTrigger = async () => {
    setLoading(true)
    try { await api.trigger(); showToast('✅ Scan triggered') }
    catch (e: any) { showToast(`❌ ${e.message}`) }
    finally { setLoading(false) }
  }

  const handleHalt = async () => {
    try { await api.halt(); showToast('🛑 System halted'); load() }
    catch {}
  }

  const handleResume = async () => {
    try { await api.resume(); showToast('▶ System resumed'); load() }
    catch {}
  }

  const halted = status?.halted
  const dryRun = status?.dry_run
  const broker = status?.broker || 'robinhood'

  const tabs: { id: Tab; label: string; icon: any; count?: number }[] = [
    { id: 'overview',   label: 'Overview',   icon: Activity },
    { id: 'positions',  label: 'Positions',  icon: Wallet,   count: positions.length },
    { id: 'decisions',  label: 'Decisions',  icon: Cpu,      count: decisions.length },
    { id: 'executions', label: 'Executions', icon: Zap,      count: executions.length },
    { id: 'backtest',   label: 'Backtest',   icon: BarChart2 },
  ]

  return (
    <div className="min-h-screen bg-terminal-bg text-terminal-text font-mono flex flex-col">

      {/* ── Header ── */}
      <header className="bg-terminal-surface border-b border-terminal-border px-4 py-2.5 flex items-center justify-between gap-4 sticky top-0 z-40">
        <div className="flex items-center gap-3 shrink-0">
          <div className="flex items-center gap-1.5">
            <span className={clx('w-2 h-2 rounded-full', halted ? 'bg-terminal-red animate-pulse-slow' : 'bg-terminal-green animate-pulse-slow')} />
            <span className="text-white font-bold text-sm tracking-widest">TRADING TERMINAL</span>
          </div>
          {dryRun !== undefined && (
            <span className={clx('text-xs px-2 py-0.5 rounded border font-semibold', dryRun ? 'border-yellow-700 text-yellow-400 bg-yellow-950' : 'border-green-800 text-terminal-green bg-green-950')}>
              {dryRun ? 'PAPER' : 'LIVE'}
            </span>
          )}
          <span className="text-xs text-terminal-muted uppercase border border-terminal-border px-2 py-0.5 rounded">{broker}</span>
          {regime && <RegimeBadge regime={regime} />}
        </div>

        <nav className="hidden md:flex items-center gap-0.5">
          {tabs.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={clx('flex items-center gap-1.5 px-3 py-1.5 rounded text-xs transition-colors',
                tab === t.id ? 'bg-terminal-card text-white' : 'text-terminal-muted hover:text-white')}>
              <t.icon size={11} />
              {t.label}
              {t.count ? <span className="bg-terminal-border text-terminal-muted rounded-full px-1.5 text-[10px]">{t.count}</span> : null}
            </button>
          ))}
        </nav>

        <div className="flex items-center gap-2 shrink-0">
          <button onClick={load} className="btn-muted flex items-center gap-1"><RefreshCw size={11} /><span className="hidden sm:inline">Refresh</span></button>
          <button onClick={handleTrigger} disabled={loading || halted} className="btn-green flex items-center gap-1">
            {loading ? <RefreshCw size={11} className="animate-spin" /> : <Play size={11} />}
            <span className="hidden sm:inline">Scan</span>
          </button>
          {halted
            ? <button onClick={handleResume} className="btn-green flex items-center gap-1"><Play size={11} />Resume</button>
            : <button onClick={handleHalt} className="btn-red flex items-center gap-1"><Square size={11} />Halt</button>
          }
          <span className="text-terminal-muted text-xs hidden lg:block">{lastUpdate && `↻ ${lastUpdate}`}</span>
        </div>
      </header>

      {/* ── Mobile tabs ── */}
      <div className="md:hidden flex items-center gap-0.5 px-3 py-2 bg-terminal-surface border-b border-terminal-border overflow-x-auto">
        {tabs.map(t => (
          <button key={t.id} onClick={() => setTab(t.id)}
            className={clx('flex items-center gap-1 px-3 py-1.5 rounded text-xs whitespace-nowrap',
              tab === t.id ? 'bg-terminal-card text-white' : 'text-terminal-muted')}>
            <t.icon size={11} />{t.label}
          </button>
        ))}
      </div>

      {/* ── Content ── */}
      <main className="flex-1 p-4 space-y-4 overflow-auto">

        {/* OVERVIEW */}
        {tab === 'overview' && (
          <>
            {/* Stats grid */}
            <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 gap-3">
              <StatCard label="Equity" value={usd(portfolio?.equity)} sub={portfolio?.is_paper ? 'Paper account' : 'Live account'} icon={Wallet} color="text-white" />
              <StatCard label="Buying Power" value={usd(portfolio?.buying_power || status?.broker_status?.buying_power)} icon={TrendingUp} color="text-terminal-cyan" />
              <StatCard label="Day P&L" value={portfolio?.day_pnl != null ? `${portfolio.day_pnl >= 0 ? '+' : ''}${usd(portfolio.day_pnl)}` : '—'}
                sub={portfolio?.day_pnl_pct != null ? sign(portfolio.day_pnl_pct) : undefined}
                color={portfolio?.day_pnl >= 0 ? 'text-terminal-green' : 'text-terminal-red'} icon={TrendingUp} />
              <StatCard label="Open Stops" value={String(positions.length)} sub={`${executions.filter(e=>e.status==='submitted').length} submitted today`} icon={Shield} color="text-yellow-400" />
              <StatCard label="Win Rate 30d" value={metrics?.win_rate_30d != null ? `${(metrics.win_rate_30d*100).toFixed(0)}%` : '—'}
                color={metrics?.win_rate_30d >= 0.55 ? 'text-terminal-green' : metrics?.win_rate_30d <= 0.4 ? 'text-terminal-red' : 'text-yellow-400'} icon={CheckCircle} />
              <StatCard label="Fear & Greed" value={metrics?.fear_greed_score != null ? String(Math.round(metrics.fear_greed_score)) : '—'}
                sub={metrics?.fear_greed_label || undefined}
                color={metrics?.fear_greed_score >= 70 ? 'text-terminal-red' : metrics?.fear_greed_score <= 30 ? 'text-terminal-green' : 'text-yellow-400'} icon={AlertTriangle} />
            </div>

            {/* P&L chart + recent executions */}
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
              <div className="card lg:col-span-1">
                <div className="label flex items-center gap-2 mb-3"><BarChart2 size={11} />Recent Executions Flow</div>
                <PnlChart executions={executions} />
              </div>
              <div className="card lg:col-span-2">
                <div className="label flex items-center gap-2 mb-3"><Clock size={11} />Latest Decisions</div>
                <div className="overflow-x-auto">
                  <table className="w-full">
                    <tbody>
                      {decisions.slice(0, 5).map(d => (
                        <tr key={d.id} className="table-row cursor-pointer" onClick={() => setSelected(d)}>
                          <td className="td text-terminal-muted text-xs w-24">{fmt(d.created_at)}</td>
                          <td className="td font-bold text-white w-16">{d.ticker}</td>
                          <td className="td w-16"><span className={d.action === 'BUY' ? 'badge-buy' : d.action === 'SELL' ? 'badge-sell' : 'badge-hold'}>{d.action}</span></td>
                          <td className="td">
                            <div className="flex items-center gap-1">
                              <div className="h-1 w-12 bg-terminal-border rounded-full overflow-hidden">
                                <div className="h-full bg-terminal-green rounded-full" style={{ width: `${(d.confidence||0)*100}%` }} />
                              </div>
                              <span className="text-xs text-terminal-muted">{d.confidence != null ? `${(d.confidence*100).toFixed(0)}%` : ''}</span>
                            </div>
                          </td>
                          <td className="td max-w-xs hidden md:table-cell">
                            <span className="text-xs text-terminal-muted line-clamp-1">{(d.investment_thesis||'').slice(0,70)}…</span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!decisions.length && <p className="text-terminal-muted text-xs text-center py-6">No decisions yet — trigger a scan</p>}
                </div>
              </div>
            </div>

            {/* Open positions summary */}
            {positions.length > 0 && (
              <div className="card">
                <div className="label flex items-center gap-2 mb-3"><Eye size={11} />Open Positions with Trailing Stops</div>
                <div className="overflow-x-auto">
                  <table className="w-full">
                    <thead><tr>
                      {['Ticker','Type','Entry','Current','High Water','Stop','Target','P&L','Opened'].map(h => <th key={h} className="th">{h}</th>)}
                    </tr></thead>
                    <tbody>{positions.map((p, i) => <PositionRow key={i} p={p} />)}</tbody>
                  </table>
                </div>
              </div>
            )}
          </>
        )}

        {/* POSITIONS */}
        {tab === 'positions' && (
          <div className="card">
            <div className="label flex items-center gap-2 mb-3"><Shield size={11} />Open Positions — Trailing Stops Active</div>
            {!positions.length
              ? <p className="text-terminal-muted text-sm text-center py-12">No open positions. Stops activate after a BUY executes.</p>
              : <div className="overflow-x-auto">
                  <table className="w-full">
                    <thead><tr>{['Ticker','Type','Entry','Current','High Water','Trailing Stop','Take Profit','P&L','Opened'].map(h => <th key={h} className="th">{h}</th>)}</tr></thead>
                    <tbody>{positions.map((p, i) => <PositionRow key={i} p={p} />)}</tbody>
                  </table>
                </div>
            }
          </div>
        )}

        {/* DECISIONS */}
        {tab === 'decisions' && (
          <div className="card">
            <div className="label flex items-center gap-2 mb-3"><Cpu size={11} />Agent Decisions — click a row to see full thesis + analyst reports</div>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead><tr>{['Time','Ticker','Action','Rating','Confidence','Target','Thesis',''].map(h => <th key={h} className="th">{h}</th>)}</tr></thead>
                <tbody>{decisions.map(d => <DecisionRow key={d.id} d={d} onExpand={setSelected} />)}</tbody>
              </table>
              {!decisions.length && <p className="text-terminal-muted text-sm text-center py-12">No decisions yet</p>}
            </div>
          </div>
        )}

        {/* EXECUTIONS */}
        {tab === 'executions' && (
          <div className="card">
            <div className="label flex items-center gap-2 mb-3"><Zap size={11} />Trade Executions</div>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead><tr>{['Time','Ticker','Side','Notional','Status','Order ID','Reason'].map(h => <th key={h} className="th">{h}</th>)}</tr></thead>
                <tbody>{executions.map(e => <ExecRow key={e.id} e={e} />)}</tbody>
              </table>
              {!executions.length && <p className="text-terminal-muted text-sm text-center py-12">No executions yet</p>}
            </div>
          </div>
        )}

        {/* BACKTEST */}
        {tab === 'backtest' && <BacktestPanel />}
      </main>

      {/* ── Footer ── */}
      <footer className="border-t border-terminal-border px-4 py-2 flex items-center justify-between text-xs text-terminal-muted">
        <span>Multi-Agent Trading System</span>
        <span>{lastUpdate && `Last updated ${lastUpdate}`}</span>
      </footer>

      {/* ── Modal ── */}
      {selected && <DetailModal d={selected} onClose={() => setSelected(null)} />}

      {/* ── Toast ── */}
      {toast && (
        <div className="fixed bottom-6 right-6 z-50 bg-terminal-card border border-terminal-border rounded-lg px-4 py-2.5 text-sm shadow-xl animate-pulse-slow">
          {toast}
        </div>
      )}
    </div>
  )
}
