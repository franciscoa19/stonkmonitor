'use client'
import { useState, useEffect, useCallback } from 'react'

const API = process.env.NEXT_PUBLIC_API_URL

interface KalshiOpp {
  ticker: string
  title: string
  event_title: string
  category: string
  side: string
  market_price: number
  market_price_cents: number
  yes_ask: number
  yes_bid: number
  no_ask: number
  dte: number
  volume: number
  spread: number
  price_move: number
  price_move_pct: number
  opportunity_type: string
  bet_contracts: number
  bet_cost_usd: number
  rationale: string
  score: number
  annualized_yield_pct?: number
  volume_zscore?: number
  maker_price_cents?: number
}

interface ScanResult {
  balance_usd: number
  markets_scanned: number
  opportunities: KalshiOpp[]
  timestamp: string
}

const TYPE_COLORS: Record<string, string> = {
  near_certain:    'text-yellow-400 border-yellow-400/40 bg-yellow-400/10',
  high_vol_extreme:'text-purple-400 border-purple-400/40 bg-purple-400/10',
  mover:           'text-blue-400 border-blue-400/40 bg-blue-400/10',
  active:          'text-green-400 border-green-400/40 bg-green-400/10',
  yield_farm:      'text-lime-400 border-lime-400/40 bg-lime-400/10',
  smart_money:     'text-cyan-400 border-cyan-400/40 bg-cyan-400/10',
}

const TYPE_LABELS: Record<string, string> = {
  near_certain:     '🔒 NEAR CERTAIN',
  high_vol_extreme: '🔥 HIGH VOL',
  mover:            '📈 MOVER',
  active:           '⚖️ ACTIVE',
  yield_farm:       '🌾 YIELD FARM',
  smart_money:      '🐋 SMART MONEY',
}

function ScorePip({ score }: { score: number }) {
  const color = score >= 7 ? 'bg-green-500' : score >= 5 ? 'bg-yellow-500' : 'bg-blue-500'
  return (
    <div className="flex items-center gap-1">
      <div className={`w-1.5 h-1.5 rounded-full ${color}`} />
      <span className="text-xs font-bold text-text">{score.toFixed(1)}</span>
    </div>
  )
}

function OppCard({ opp, onExecute }: { opp: KalshiOpp; onExecute: (opp: KalshiOpp) => void }) {
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(false)
  const typeStyle = TYPE_COLORS[opp.opportunity_type] || 'text-muted border-border bg-card'
  const typeLabel = TYPE_LABELS[opp.opportunity_type] || opp.opportunity_type.toUpperCase()

  const priceColor = opp.market_price_cents <= 10
    ? 'text-bear'
    : opp.market_price_cents >= 90
    ? 'text-bull'
    : 'text-gold'

  const movePct = opp.price_move_pct
  const moveStr = movePct !== 0
    ? `${movePct > 0 ? '+' : ''}${movePct.toFixed(1)}¢`
    : null

  async function handleExecute() {
    setLoading(true)
    try {
      await onExecute(opp)
      setDone(true)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="bg-card border border-border rounded p-2.5 flex flex-col gap-1.5">
      {/* Header row */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <p className="text-xs text-text font-bold leading-tight truncate" title={opp.title}>
            {opp.title}
          </p>
          {opp.event_title && opp.event_title !== opp.title && (
            <p className="text-xs text-muted truncate">{opp.event_title}</p>
          )}
        </div>
        <ScorePip score={opp.score} />
      </div>

      {/* Type badge + price */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`text-xs px-1.5 py-0.5 rounded border font-bold ${typeStyle}`}>
          {typeLabel}
        </span>
        <span className={`text-sm font-bold ${priceColor}`}>
          {opp.side.toUpperCase()} @ {opp.market_price_cents.toFixed(1)}¢
        </span>
        {moveStr && (
          <span className={`text-xs ${movePct > 0 ? 'text-bull' : 'text-bear'}`}>
            {moveStr}
          </span>
        )}
      </div>

      {/* Meta row */}
      <div className="flex gap-3 text-xs text-muted flex-wrap">
        <span>{opp.dte.toFixed(1)}d</span>
        <span>vol {(opp.volume / 1000).toFixed(0)}k</span>
        <span>spread {(opp.spread * 100).toFixed(1)}¢</span>
        {!!opp.annualized_yield_pct && opp.annualized_yield_pct >= 50 && (
          <span className="text-lime-400">ann {opp.annualized_yield_pct.toFixed(0)}%</span>
        )}
        {!!opp.volume_zscore && opp.volume_zscore >= 2 && (
          <span className="text-cyan-400">Z {opp.volume_zscore.toFixed(1)}σ</span>
        )}
        {!!opp.maker_price_cents && opp.maker_price_cents !== opp.market_price_cents && (
          <span className="text-accent">maker {opp.maker_price_cents.toFixed(0)}¢</span>
        )}
        {opp.category && <span className="truncate">{opp.category}</span>}
      </div>

      {/* Rationale */}
      <p className="text-xs text-muted leading-tight line-clamp-2">{opp.rationale}</p>

      {/* Execute */}
      <div className="flex items-center gap-2 mt-0.5">
        <button
          onClick={handleExecute}
          disabled={loading || done}
          className={`flex-1 py-1 rounded text-xs font-bold transition-colors ${
            done
              ? 'bg-bull/20 text-bull border border-bull/30 cursor-default'
              : 'bg-accent/20 text-accent border border-accent/30 hover:bg-accent/30'
          }`}
        >
          {done ? '✓ Submitted' : loading ? '…' : `Buy ${opp.bet_contracts}x · $${opp.bet_cost_usd.toFixed(2)}`}
        </button>
        <span className="text-xs text-muted">{opp.ticker}</span>
      </div>
    </div>
  )
}

interface KalshiPanelProps {
  wsData?: Record<string, unknown> | null
}

export function KalshiPanel({ wsData }: KalshiPanelProps) {
  const [scan, setScan] = useState<ScanResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [lastFetch, setLastFetch] = useState<Date | null>(null)
  const [execError, setExecError] = useState<string>('')
  const [typeFilter, setTypeFilter] = useState<string>('all')

  const fetchScan = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetch(`${API}/api/kalshi/scan`)
      if (res.ok) {
        const data = await res.json()
        setScan(data)
        setLastFetch(new Date())
      }
    } catch {}
    setLoading(false)
  }, [])

  // Use live WS data if provided
  useEffect(() => {
    if (wsData) {
      setScan(wsData as unknown as ScanResult)
      setLastFetch(new Date())
    }
  }, [wsData])

  // Initial fetch on mount
  useEffect(() => {
    fetchScan()
  }, [fetchScan])

  async function executeOrder(opp: KalshiOpp) {
    setExecError('')
    try {
      // Prefer maker-side limit price to earn the spread; fall back to ask
      const priceInCents = opp.maker_price_cents
        ? Math.round(opp.maker_price_cents)
        : Math.round(opp.side === 'yes' ? opp.yes_ask * 100 : opp.no_ask * 100)
      const res = await fetch(`${API}/api/kalshi/order`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ticker:    opp.ticker,
          side:      opp.side === 'watch' ? 'yes' : opp.side,
          action:    'buy',
          count:     opp.bet_contracts,
          order_type:'limit',
          price:     priceInCents,
        }),
      })
      if (!res.ok) {
        const err = await res.json()
        setExecError(err.detail || 'Order failed')
      }
    } catch (e: unknown) {
      setExecError(e instanceof Error ? e.message : 'Order failed')
    }
  }

  const opportunities = scan?.opportunities ?? []
  const filtered = typeFilter === 'all'
    ? opportunities
    : opportunities.filter(o => o.opportunity_type === typeFilter)

  const typeFilters = [
    { id: 'all',             label: 'All' },
    { id: 'smart_money',     label: '🐋 Smart $' },
    { id: 'yield_farm',      label: '🌾 Yield' },
    { id: 'near_certain',    label: '🔒 Certain' },
    { id: 'high_vol_extreme',label: '🔥 High Vol' },
    { id: 'mover',           label: '📈 Movers' },
    { id: 'active',          label: '⚖️ Active' },
  ]

  return (
    <div className="flex flex-col gap-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-bold text-text">🎰 Kalshi Markets</h3>
          {scan && (
            <p className="text-xs text-muted">
              {scan.markets_scanned} scanned · ${scan.balance_usd.toFixed(2)} bal
            </p>
          )}
        </div>
        <button
          onClick={fetchScan}
          disabled={loading}
          className="text-xs text-accent hover:text-text transition-colors"
        >
          {loading ? '⟳' : '↻ Refresh'}
        </button>
      </div>

      {/* Last updated */}
      {lastFetch && (
        <p className="text-xs text-muted">
          Updated {lastFetch.toLocaleTimeString()}
        </p>
      )}

      {/* Type filter chips */}
      <div className="flex gap-1 flex-wrap">
        {typeFilters.map(f => (
          <button
            key={f.id}
            onClick={() => setTypeFilter(f.id)}
            className={`px-2 py-0.5 rounded text-xs font-bold transition-colors ${
              typeFilter === f.id
                ? 'bg-accent text-bg'
                : 'bg-card text-muted border border-border hover:text-text'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Error */}
      {execError && (
        <div className="bg-bear/10 border border-bear/30 text-bear text-xs rounded p-2">
          {execError}
        </div>
      )}

      {/* Opportunities */}
      {filtered.length === 0 ? (
        <div className="text-center py-8 text-muted text-xs">
          {loading ? 'Scanning markets…' : 'No opportunities found'}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <p className="text-xs text-muted">{filtered.length} opportunities</p>
          {filtered.map((opp, i) => (
            <OppCard key={`${opp.ticker}-${i}`} opp={opp} onExecute={executeOrder} />
          ))}
        </div>
      )}
    </div>
  )
}
