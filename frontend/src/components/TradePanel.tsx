'use client'
import { useState } from 'react'
import { Account, isAccount } from '@/lib/account'

const API = process.env.NEXT_PUBLIC_API_URL

interface Position {
  symbol: string
  qty: number
  side: string
  avg_price: number
  current: number
  pnl: number
  pnl_pct: number
  market_val: number
}

export function TradePanel({
  positions,
  account,
  accountStale = false,
  onRefresh,
}: {
  positions: Position[]
  account: Account | null
  accountStale?: boolean
  onRefresh: () => void
}) {
  const [ticker, setTicker] = useState('')
  const [qty, setQty]       = useState('')
  const [side, setSide]     = useState<'buy' | 'sell'>('buy')
  const [type, setType]     = useState<'market' | 'limit'>('market')
  const [limit, setLimit]   = useState('')
  const [loading, setLoading] = useState(false)
  const [msg, setMsg]       = useState('')

  async function submitOrder() {
    if (!ticker || !qty) return
    setLoading(true)
    setMsg('')
    try {
      const res = await fetch(`${API}/api/order`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ticker: ticker.toUpperCase(),
          qty: parseFloat(qty),
          side,
          order_type: type,
          limit_price: type === 'limit' ? parseFloat(limit) : undefined,
        }),
      })
      const data = await res.json()
      if (res.ok) {
        setMsg(`✅ Order placed: ${data.id}`)
        setTicker(''); setQty(''); setLimit('')
        onRefresh()
      } else {
        setMsg(`❌ ${data.detail}`)
      }
    } catch (e) {
      setMsg('❌ Request failed')
    }
    setLoading(false)
  }

  async function closePosition(sym: string) {
    const res = await fetch(`${API}/api/positions/${sym}`, { method: 'DELETE' })
    if (res.ok) onRefresh()
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Account Summary */}
      {accountStale && (
        <div className="bg-card border border-border rounded p-3 text-xs text-gold">
          {isAccount(account)
            ? 'Broker unreachable — showing the last known account values.'
            : 'Broker account unavailable.'}
        </div>
      )}
      {isAccount(account) && (
        <div className="bg-card border border-border rounded p-3 grid grid-cols-3 gap-2 text-xs">
          <div>
            <div className="text-muted">Equity</div>
            <div className="text-text font-bold">${account.equity.toLocaleString()}</div>
          </div>
          <div>
            <div className="text-muted">Cash</div>
            <div className="text-text font-bold">${account.cash.toLocaleString()}</div>
          </div>
          <div>
            <div className="text-muted">Buying Power</div>
            <div className="text-text font-bold">${account.buying_power.toLocaleString()}</div>
          </div>
        </div>
      )}

      {/* Order Entry */}
      <div className="bg-card border border-border rounded p-3">
        <div className="text-xs text-muted mb-3 uppercase tracking-wider">New Order</div>
        <div className="flex gap-2 mb-2">
          <input
            className="flex-1 bg-surface border border-border rounded px-2 py-1.5 text-sm text-text placeholder-muted"
            placeholder="TICKER"
            value={ticker}
            onChange={e => setTicker(e.target.value.toUpperCase())}
          />
          <input
            className="w-20 bg-surface border border-border rounded px-2 py-1.5 text-sm text-text placeholder-muted"
            placeholder="Qty"
            type="number"
            value={qty}
            onChange={e => setQty(e.target.value)}
          />
        </div>
        <div className="flex gap-2 mb-2">
          <button
            onClick={() => setSide('buy')}
            className={`flex-1 py-1.5 rounded text-sm font-bold transition-colors ${
              side === 'buy' ? 'bg-bull text-bg' : 'bg-surface text-muted border border-border'
            }`}
          >
            BUY
          </button>
          <button
            onClick={() => setSide('sell')}
            className={`flex-1 py-1.5 rounded text-sm font-bold transition-colors ${
              side === 'sell' ? 'bg-bear text-white' : 'bg-surface text-muted border border-border'
            }`}
          >
            SELL
          </button>
        </div>
        <div className="flex gap-2 mb-2">
          <select
            className="flex-1 bg-surface border border-border rounded px-2 py-1.5 text-sm text-text"
            value={type}
            onChange={e => setType(e.target.value as 'market' | 'limit')}
          >
            <option value="market">Market</option>
            <option value="limit">Limit</option>
          </select>
          {type === 'limit' && (
            <input
              className="w-28 bg-surface border border-border rounded px-2 py-1.5 text-sm text-text placeholder-muted"
              placeholder="Limit $"
              type="number"
              value={limit}
              onChange={e => setLimit(e.target.value)}
            />
          )}
        </div>
        <button
          onClick={submitOrder}
          disabled={loading}
          className="w-full bg-accent text-bg py-2 rounded font-bold text-sm hover:bg-accent/90 disabled:opacity-50 transition-colors"
        >
          {loading ? 'Placing...' : '⚡ SEND ORDER'}
        </button>
        {msg && <div className="mt-2 text-xs text-center">{msg}</div>}
      </div>

      {/* Positions */}
      <div className="bg-card border border-border rounded p-3">
        <div className="text-xs text-muted mb-3 uppercase tracking-wider">Positions</div>
        {positions.length === 0 && (
          <div className="text-muted text-xs text-center py-2">No open positions</div>
        )}
        {positions.map(p => (
          <div key={p.symbol} className="flex justify-between items-center py-1.5 border-b border-border last:border-0">
            <div>
              <div className="text-sm font-bold text-text">{p.symbol}</div>
              <div className="text-xs text-muted">{p.qty} @ ${p.avg_price.toFixed(2)}</div>
            </div>
            <div className="text-right">
              <div className={`text-sm font-bold ${p.pnl >= 0 ? 'text-bull' : 'text-bear'}`}>
                {p.pnl >= 0 ? '+' : ''}{p.pnl.toFixed(2)} ({p.pnl_pct.toFixed(1)}%)
              </div>
              <button
                onClick={() => closePosition(p.symbol)}
                className="text-xs text-muted hover:text-bear transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
