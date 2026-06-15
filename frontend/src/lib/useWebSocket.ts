/**
 * WebSocket hook — connects to backend, auto-reconnects, delivers messages.
 */
import { useEffect, useRef, useState, useCallback } from 'react'

export type WsMessage =
  | { type: 'signal'; data: Signal }
  | { type: 'feed'; feed: string; data: Record<string, unknown> }
  | { type: 'kalshi_scan'; data: Record<string, unknown> }
  | { type: 'trade_queued'; data: Record<string, unknown> }
  | { type: 'status'; message: string }
  | { type: 'pong' }

export interface Signal {
  type: string
  ticker: string
  score: number
  side: 'bullish' | 'bearish' | 'neutral'
  title: string
  description: string
  premium: number
  expiry: string | null
  strike: number | null
  option_type: string | null
  timestamp: string
  _rx?: number   // client receive time (ms) — set on arrival, used for retention/age
}

interface UseWebSocketOptions {
  onSignal?: (signal: Signal) => void
  onFeed?: (feed: string, data: Record<string, unknown>) => void
  onKalshiScan?: (data: Record<string, unknown>) => void
}

export function useWebSocket(url: string, opts: UseWebSocketOptions = {}) {
  const ws = useRef<WebSocket | null>(null)
  const [connected, setConnected] = useState(false)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>()
  const { onSignal, onFeed, onKalshiScan } = opts

  const connect = useCallback(() => {
    try {
      ws.current = new WebSocket(url)

      ws.current.onopen = () => {
        setConnected(true)
        console.log('WS connected')
        // Ping every 30s to keep alive
        const ping = setInterval(() => {
          if (ws.current?.readyState === WebSocket.OPEN) {
            ws.current.send(JSON.stringify({ action: 'ping' }))
          } else {
            clearInterval(ping)
          }
        }, 30_000)
      }

      ws.current.onmessage = (ev) => {
        try {
          const msg: WsMessage = JSON.parse(ev.data)
          if (msg.type === 'signal' && onSignal) onSignal(msg.data)
          if (msg.type === 'feed' && onFeed) onFeed(msg.feed, msg.data)
          if (msg.type === 'kalshi_scan' && onKalshiScan) onKalshiScan(msg.data)
        } catch {}
      }

      ws.current.onclose = () => {
        setConnected(false)
        reconnectTimer.current = setTimeout(connect, 3_000)
      }

      ws.current.onerror = () => {
        ws.current?.close()
      }
    } catch (e) {
      reconnectTimer.current = setTimeout(connect, 5_000)
    }
  }, [url, onSignal, onFeed, onKalshiScan])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      ws.current?.close()
    }
  }, [connect])

  return { connected }
}
