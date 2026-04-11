'use client'
import { useState, useCallback, useEffect } from 'react'
import { useWebSocket, Signal } from '@/lib/useWebSocket'
import { SignalFeed } from '@/components/SignalFeed'
import { TradePanel } from '@/components/TradePanel'
import { Watchlist } from '@/components/Watchlist'
import { History } from '@/components/History'
import { Analytics } from '@/components/Analytics'
import { TradeQueue } from '@/components/TradeQueue'
import { KalshiPanel } from '@/components/KalshiPanel'

const API = process.env.NEXT_PUBLIC_API_URL
const WS  = process.env.NEXT_PUBLIC_WS_URL

const FILTER_TYPES = ['All', 'Sweeps', 'Dark Pool', 'Insider', 'Congress', 'IV', 'Earnings']

type FilterType = typeof FILTER_TYPES[number]

function filterSignals(signals: Signal[], filter: FilterType): Signal[] {
  if (filter === 'All') return signals
  const map: Record<string, string[]> = {
    'Sweeps':    ['golden_sweep', 'sweep', 'options_flow'],
    'Dark Pool': ['dark_pool'],
    'Insider':   ['insider_buy', 'insider_sell'],
    'Congress':  ['congress_trade'],
    'IV':        ['iv_high', 'iv_low'],
    'Earnings':  ['earnings_setup'],
  }
  const types = map[filter] || []
  return signals.filter(s => types.includes(s.type))
}

function StatusDot({ connected }: { connected: boolean }) {
  return (
    <div className="flex items-center gap-1.5">
      <div className={`w-2 h-2 rounded-full ${connected ? 'bg-bull animate-pulse' : 'bg-bear'}`} />
      <span className={`text-xs ${connected ? 'text-bull' : 'text-bear'}`}>
        {connected ? 'LIVE' : 'DISCONNECTED'}
      </span>
    </div>
  )
}

export default function Dashboard() {
  const [signals, setSignals]     = useState<Signal[]>([])
  const [filter, setFilter]       = useState<FilterType>('All')
  const [minScore, setMinScore]   = useState(0)
  const [watchlist, setWatchlist] = useState<string[]>([])
  const [positions, setPositions] = useState([])
  const [account, setAccount]     = useState(null)
  const [activeTab, setActiveTab] = useState<'feed' | 'history' | 'trade' | 'analytics' | 'kalshi'>('feed')
  const [kalshiScan, setKalshiScan] = useState<Record<string, unknown> | null>(null)

  const onSignal = useCallback((signal: Signal) => {
    setSignals(prev => [signal, ...prev].slice(0, 500))
  }, [])

  const onKalshiScan = useCallback((data: Record<string, unknown>) => {
    setKalshiScan(data)
  }, [])

  const { connected } = useWebSocket(WS!, { onSignal, onKalshiScan })

  async function refreshAccount() {
    try {
      const [accRes, posRes] = await Promise.all([
        fetch(`${API}/api/account`),
        fetch(`${API}/api/positions`),
      ])
      if (accRes.ok) setAccount(await accRes.json())
      if (posRes.ok) setPositions(await posRes.json())
    } catch {}
  }

  async function addToWatchlist(ticker: string) {
    try {
      await fetch(`${API}/api/watchlist`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker }),
      })
      setWatchlist(prev => [...prev, ticker])
    } catch {}
  }

  async function removeFromWatchlist(ticker: string) {
    try {
      await fetch(`${API}/api/watchlist/${ticker}`, { method: 'DELETE' })
      setWatchlist(prev => prev.filter(t => t !== ticker))
    } catch {}
  }

  useEffect(() => {
    refreshAccount()
    const interval = setInterval(refreshAccount, 30_000)
    return () => clearInterval(interval)
  }, [])

  const filtered = filterSignals(signals, filter).filter(s => s.score >= minScore)

  const counts: Record<string, number> = {
    bull: signals.filter(s => s.side === 'bullish').length,
    bear: signals.filter(s => s.side === 'bearish').length,
    golden: signals.filter(s => s.type === 'golden_sweep').length,
  }

  return (
    <div className="min-h-screen bg-bg font-mono flex flex-col">
      {/* Header */}
      <header className="border-b border-border bg-surface px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <h1 className="text-lg font-bold text-accent glow-accent">⚡ STONKMONITOR</h1>
          <StatusDot connected={connected} />
        </div>
        <div className="flex items-center gap-6 text-xs">
          <span className="text-bull">↑ {counts.bull} bull</span>
          <span className="text-bear">↓ {counts.bear} bear</span>
          <span className="text-gold">⚡ {counts.golden} golden</span>
          <span className="text-muted">{signals.length} total signals</span>
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden">
        {/* Left: Filters + Feed */}
        <main className="flex-1 flex flex-col overflow-hidden p-4">
          {/* Filter Bar */}
          <div className="flex items-center gap-3 mb-4">
            <div className="flex gap-1">
              {FILTER_TYPES.map(f => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={`px-3 py-1 rounded text-xs font-bold transition-colors ${
                    filter === f
                      ? 'bg-accent text-bg'
                      : 'bg-card text-muted border border-border hover:text-text'
                  }`}
                >
                  {f}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2 ml-auto">
              <span className="text-xs text-muted">Min Score:</span>
              <input
                type="range"
                min="0" max="10" step="0.5"
                value={minScore}
                onChange={e => setMinScore(parseFloat(e.target.value))}
                className="w-24 accent-accent"
              />
              <span className="text-xs text-accent w-6">{minScore}</span>
            </div>
          </div>

          {/* Signal Feed */}
          <div className="flex-1 overflow-hidden">
            <SignalFeed signals={filtered} />
          </div>
        </main>

        {/* Right Sidebar */}
        <aside className="w-80 border-l border-border bg-surface flex flex-col overflow-y-auto p-4 gap-4">
          {/* Tab Bar */}
          <div className="flex gap-1">
            {([
              { id: 'feed',      label: '📡 Watch' },
              { id: 'history',   label: '🗄️ History' },
              { id: 'analytics', label: '🎯 Patterns' },
              { id: 'trade',     label: '💹 Trade' },
              { id: 'kalshi',    label: '🎰 Kalshi' },
            ] as const).map(tab => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex-1 py-1.5 rounded text-xs font-bold transition-colors ${
                  activeTab === tab.id
                    ? 'bg-accent text-bg'
                    : 'bg-card text-muted border border-border hover:text-text'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {activeTab === 'feed' && (
            <Watchlist
              tickers={watchlist}
              onAdd={addToWatchlist}
              onRemove={removeFromWatchlist}
            />
          )}
          {activeTab === 'history' && <History />}
          {activeTab === 'analytics' && <Analytics />}
          {activeTab === 'trade' && (
            <div className="flex flex-col gap-4">
              <TradeQueue />
              <TradePanel
                positions={positions}
                account={account}
                onRefresh={refreshAccount}
              />
            </div>
          )}
          {activeTab === 'kalshi' && (
            <KalshiPanel wsData={kalshiScan} />
          )}
        </aside>
      </div>
    </div>
  )
}
