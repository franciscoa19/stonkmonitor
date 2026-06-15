'use client'
import { Signal } from '@/lib/useWebSocket'

const TYPE_ICONS: Record<string, string> = {
  golden_sweep:   '⚡',
  sweep:          '🌊',
  options_flow:   '📊',
  dark_pool:      '🌑',
  insider_buy:    '👤',
  insider_sell:   '👤',
  congress_trade: '🏛️',
  iv_high:        '🔥',
  iv_low:         '❄️',
  earnings_setup: '🎯',
}

// ── Colorblind-safe direction encoding ──────────────────────────────
// Blue (up) vs Orange (down) is distinguishable under red-green color
// blindness. Color is NEVER the only cue — every tile also shows a ▲/▼
// arrow and the CALL/PUT (or LONG/SHORT) word.
const DIR = {
  bullish: {
    arrow: '▲',
    word: (opt: string | null) => (opt ? 'CALL' : 'LONG'),
    text: 'text-accent',
    topBorder: 'border-t-accent',
    chip: 'bg-accent/15 text-accent border-accent/50',
    score: 'text-accent',
  },
  bearish: {
    arrow: '▼',
    word: (opt: string | null) => (opt ? 'PUT' : 'SHORT'),
    text: 'text-[#ff9500]',
    topBorder: 'border-t-[#ff9500]',
    chip: 'bg-[#ff9500]/15 text-[#ff9500] border-[#ff9500]/50',
    score: 'text-[#ff9500]',
  },
  neutral: {
    arrow: '■',
    word: () => 'NEUT',
    text: 'text-muted',
    topBorder: 'border-t-muted',
    chip: 'bg-muted/15 text-muted border-muted/50',
    score: 'text-text',
  },
} as const

// ── Formatting helpers ──────────────────────────────────────────────
function fmtMoney(n: number | null | undefined): string {
  if (!n || n <= 0) return '—'
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`
  if (n >= 1_000)     return `$${(n / 1_000).toFixed(0)}K`
  return `$${n.toFixed(0)}`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
function fmtExpiry(s: string | null): { label: string; dte: number | null } {
  if (!s) return { label: '—', dte: null }
  const d = new Date(s.length <= 10 ? `${s}T00:00:00` : s)
  if (isNaN(d.getTime())) return { label: s, dte: null }
  const dte = Math.round((d.getTime() - Date.now()) / 86_400_000)
  return { label: `${MONTHS[d.getMonth()]} ${d.getDate()}`, dte }
}

function fmtStrike(strike: number | null, opt: string | null): string {
  if (strike == null) return '—'
  const t = opt ? opt[0].toUpperCase() : ''
  return `$${strike % 1 === 0 ? strike.toFixed(0) : strike.toFixed(2)}${t ? t : ''}`
}

// ── Per-ticker aggregation ──────────────────────────────────────────
interface TickerCard {
  ticker: string
  score: number
  side: Signal['side']
  optionType: string | null
  contract: Signal | null
  signals: Signal[]
  premium: number
  latest: string
  rx: number
  count: number
}

function rxOf(s: Signal): number {
  if (s._rx != null) return s._rx
  const t = Date.parse(s.timestamp)
  return isNaN(t) ? Date.now() : t
}

function ageLabel(ms: number): string {
  const m = Math.floor((Date.now() - ms) / 60000)
  if (m < 1) return 'now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  return `${h}h${m % 60 ? ` ${m % 60}m` : ''} ago`
}

function aggregate(signals: Signal[]): TickerCard[] {
  const map = new Map<string, Signal[]>()
  for (const s of signals) {
    const arr = map.get(s.ticker)
    if (arr) arr.push(s)
    else map.set(s.ticker, [s])
  }

  const cards: TickerCard[] = []
  for (const [ticker, list] of Array.from(map.entries())) {
    const sorted = [...list].sort((a, b) => b.score - a.score)
    const top = sorted[0]
    const contract =
      sorted.find(s => s.strike != null && s.side === top.side) ||
      sorted.find(s => s.strike != null) ||
      null

    const seen = new Map<string, Signal>()
    for (const s of sorted) if (!seen.has(s.title)) seen.set(s.title, s)
    const contributors = Array.from(seen.values()).sort((a, b) => b.score - a.score).slice(0, 3)

    const premium = Math.max(...list.map(s => s.premium || 0))
    const latest = list.reduce((m, s) => (s.timestamp > m ? s.timestamp : m), list[0].timestamp)
    const rx = list.reduce((m, s) => Math.max(m, rxOf(s)), 0)

    cards.push({
      ticker,
      score: top.score,
      side: top.side,
      optionType: contract?.option_type ?? top.option_type ?? null,
      contract,
      signals: contributors,
      premium,
      latest,
      rx,
      count: list.length,
    })
  }
  return cards.sort((a, b) => b.score - a.score)
}

// ── Tiers (no green — uses text labels + amber/cyan/grey) ────────────
const TIERS = [
  { label: 'TOP TIER', sub: '8.5+', min: 8.5, upper: Infinity, color: 'text-gold',   bar: 'bg-gold' },
  { label: 'STRONG',   sub: '7+',   min: 7.0, upper: 8.5,      color: 'text-accent', bar: 'bg-accent' },
  { label: 'WATCH',    sub: '5+',   min: 5.0, upper: 7.0,      color: 'text-text',   bar: 'bg-muted' },
] as const

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex-1 text-center min-w-0">
      <div className="text-[8px] uppercase tracking-wider text-muted">{label}</div>
      <div className="text-xs font-bold text-text truncate">{value}</div>
    </div>
  )
}

function Tile({ card }: { card: TickerCard }) {
  const dir = DIR[card.side] || DIR.neutral
  const exp = fmtExpiry(card.contract?.expiry ?? null)
  const ageMin = (Date.now() - card.rx) / 60000
  const stale = ageMin > 20   // dim tiles older than 20 min

  return (
    <div
      className={`bg-card rounded-lg border border-border border-t-4 ${dir.topBorder} flex flex-col p-2.5 overflow-hidden transition-opacity ${stale ? 'opacity-50' : ''}`}
    >
      {/* Header: ticker + direction */}
      <div className="flex items-center justify-between gap-1">
        <span className="text-base font-bold text-text tracking-wide truncate">{card.ticker}</span>
        <span className={`shrink-0 px-1.5 py-0.5 rounded text-[10px] font-bold border ${dir.chip}`}>
          {dir.arrow} {dir.word(card.optionType)}
        </span>
      </div>

      {/* Big score, centered */}
      <div className="flex-1 flex flex-col items-center justify-center py-1.5">
        <div className={`text-4xl font-bold leading-none ${dir.score}`}>{card.score.toFixed(1)}</div>
        <div className="text-[8px] uppercase tracking-widest text-muted mt-1">score</div>
      </div>

      {/* Contract strip */}
      <div className="flex items-stretch gap-1 bg-surface rounded-md py-1.5 px-1 border border-border mb-1.5">
        <MiniStat label="Strike" value={fmtStrike(card.contract?.strike ?? null, card.optionType)} />
        <div className="w-px bg-border" />
        <MiniStat label="Expiry" value={exp.dte != null ? `${exp.label}·${exp.dte}d` : exp.label} />
        <div className="w-px bg-border" />
        <MiniStat label="Prem" value={fmtMoney(card.premium)} />
      </div>

      {/* Why — contributing signals */}
      <div className="flex flex-col gap-px">
        {card.signals.map((s, i) => (
          <div key={i} className="flex items-center justify-between text-[10px] leading-tight">
            <span className="text-text/75 truncate pr-1">
              {TYPE_ICONS[s.type] || '📡'} {s.title}
            </span>
            <span className="text-muted font-mono shrink-0">{s.score.toFixed(1)}</span>
          </div>
        ))}
      </div>

      <div className="text-[9px] text-muted font-mono text-right mt-1">
        {card.count > card.signals.length ? `+${card.count - card.signals.length} more · ` : ''}{ageLabel(card.rx)}
      </div>
    </div>
  )
}

export function SignalFeed({ signals }: { signals: Signal[] }) {
  const cards = aggregate(signals).filter(c => c.score >= 5.0)

  if (cards.length === 0) {
    return (
      <div className="h-full overflow-y-auto pr-1">
        <div className="text-muted text-sm text-center mt-8">
          {signals.length === 0 ? 'Waiting for signals…' : 'No trades ≥ 5.0 right now'}
        </div>
      </div>
    )
  }

  return (
    <div className="h-full overflow-y-auto pr-1">
      {TIERS.map(tier => {
        const group = cards.filter(c => c.score >= tier.min && c.score < tier.upper)
        if (group.length === 0) return null
        return (
          <section key={tier.label} className="mb-5">
            <div className="flex items-center gap-2 mb-2 sticky top-0 bg-bg/95 backdrop-blur py-1 z-10">
              <span className={`text-xs font-bold ${tier.color} tracking-widest`}>{tier.label}</span>
              <span className="text-[10px] text-muted">{tier.sub}</span>
              <div className={`h-px flex-1 ${tier.bar} opacity-30`} />
              <span className="text-[10px] text-muted">{group.length}</span>
            </div>
            <div className="grid grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 gap-2.5 auto-rows-fr">
              {group.map(c => (
                <Tile key={c.ticker} card={c} />
              ))}
            </div>
          </section>
        )
      })}
    </div>
  )
}
