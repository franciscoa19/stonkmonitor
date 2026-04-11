'use client'
import { Signal } from '@/lib/useWebSocket'

const SIDE_COLORS = {
  bullish: 'border-bull text-bull',
  bearish: 'border-bear text-bear',
  neutral: 'border-muted text-muted',
}

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

function ScoreBar({ score }: { score: number }) {
  const filled = Math.round(score)
  return (
    <div className="flex gap-0.5 items-center">
      {Array.from({ length: 10 }, (_, i) => (
        <div
          key={i}
          className={`h-1.5 w-2.5 rounded-sm ${
            i < filled
              ? score >= 8 ? 'bg-gold' : score >= 6 ? 'bg-bull' : 'bg-accent'
              : 'bg-border'
          }`}
        />
      ))}
      <span className="ml-1 text-xs text-muted">{score.toFixed(1)}</span>
    </div>
  )
}

function SignalCard({ signal }: { signal: Signal }) {
  const colorClass = SIDE_COLORS[signal.side] || SIDE_COLORS.neutral
  const icon = TYPE_ICONS[signal.type] || '📡'
  const isGolden = signal.type === 'golden_sweep'
  const time = new Date(signal.timestamp).toLocaleTimeString()

  return (
    <div
      className={`signal-new border-l-2 ${colorClass} bg-card rounded-r p-3 mb-2 hover:bg-surface transition-colors ${
        isGolden ? 'ring-1 ring-gold/40' : ''
      }`}
    >
      <div className="flex justify-between items-start mb-1">
        <span className="font-bold text-sm text-text">
          {icon} {signal.title}
        </span>
        <span className="text-xs text-muted font-mono">{time}</span>
      </div>
      <div className="text-xs text-muted mb-2">{signal.description}</div>
      <ScoreBar score={signal.score} />
    </div>
  )
}

export function SignalFeed({ signals }: { signals: Signal[] }) {
  return (
    <div className="h-full overflow-y-auto pr-1">
      {signals.length === 0 && (
        <div className="text-muted text-sm text-center mt-8">
          Waiting for signals...
        </div>
      )}
      {signals.map((s, i) => (
        <SignalCard key={`${s.ticker}-${s.timestamp}-${i}`} signal={s} />
      ))}
    </div>
  )
}
