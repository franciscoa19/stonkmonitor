/**
 * WebSocket hook — connects to backend, auto-reconnects, delivers messages.
 */
import { useEffect, useRef, useState } from 'react'

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
  const callbacks = useRef(opts)
  callbacks.current = opts

  useEffect(() => {
    setConnected(false)
    let disposed = false
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined
    let pingTimer: ReturnType<typeof setInterval> | undefined
    let socket: WebSocket | null = null

    function connect() {
      if (disposed) return
      try {
        socket = new WebSocket(url)
        ws.current = socket
        const current = socket
        current.onopen = () => {
          if (disposed) return
          setConnected(true)
          pingTimer = setInterval(() => {
            if (current.readyState === WebSocket.OPEN) {
              current.send(JSON.stringify({ action: 'ping' }))
            }
          }, 30_000)
        }
        current.onmessage = ev => {
          if (disposed) return
          try {
            const msg: WsMessage = JSON.parse(ev.data)
            const handlers = callbacks.current
            if (msg.type === 'signal') handlers.onSignal?.(msg.data)
            if (msg.type === 'feed') handlers.onFeed?.(msg.feed, msg.data)
            if (msg.type === 'kalshi_scan') handlers.onKalshiScan?.(msg.data)
          } catch {}
        }
        current.onclose = () => {
          clearInterval(pingTimer)
          if (disposed) return
          setConnected(false)
          reconnectTimer = setTimeout(connect, 3_000)
        }
        current.onerror = () => current.close()
      } catch {
        if (!disposed) reconnectTimer = setTimeout(connect, 5_000)
      }
    }
    connect()
    return () => {
      disposed = true
      clearTimeout(reconnectTimer)
      clearInterval(pingTimer)
      if (socket) {
        socket.onopen = socket.onclose = socket.onerror = socket.onmessage = null
        socket.close()
      }
      ws.current = null
    }
  }, [url])

  return { connected }
}
