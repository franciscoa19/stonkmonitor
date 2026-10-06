export interface ManualOrderPayload {
  ticker: string
  qty: number
  side: 'buy' | 'sell'
  order_type: 'market' | 'limit'
  limit_price?: number
  tif?: string
}

export interface ManualOrderResult {
  status: 'confirmed' | 'rejected' | 'pending'
  id?: string
  error?: string
}

const KEY = 'stonkmonitor.manualOrder'
const PREFIX = `${KEY}.`
interface SavedRequest { request_id: string; payload: ManualOrderPayload }

/** Persist before POST; an uncertain order keeps its identity across reloads. */
export class ManualOrderClient {
  private saved: SavedRequest | null
  private busy = false

  constructor(
    private api: string,
    private storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem' | 'key' | 'length'>,
    private transport: typeof fetch = fetch,
    private newId: () => string = () => crypto.randomUUID(),
  ) {
    const legacy = storage.getItem(KEY)
    if (legacy) {
      const saved = this.parse(legacy)
      storage.setItem(PREFIX + saved.request_id, legacy)
      storage.removeItem(KEY)
    }
    this.saved = this.readSaved()
  }

  private parse(raw: string): SavedRequest {
    const saved = JSON.parse(raw)
    if (!saved || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(saved.request_id)
        || !saved.payload) {
      throw new Error('Saved order needs manual reconciliation before placing another order.')
    }
    return saved
  }

  private readSaved(): SavedRequest | null {
    // One key per identity prevents another tab from overwriting an uncertain
    // request. Re-read shared storage before starting any deliberate new order.
    const keys: string[] = []
    for (let i = 0; i < this.storage.length; i++) {
      const key = this.storage.key(i)
      if (key?.startsWith(PREFIX)) keys.push(key)
    }
    for (const key of keys.sort()) {
      const raw = this.storage.getItem(key)
      if (raw) {
        const saved = this.parse(raw)
        if (key !== PREFIX + saved.request_id) throw new Error('Saved order identity mismatch; reconcile before trading')
        return saved
      }
    }
    return null
  }

  get pending() { return this.saved !== null || this.readSaved() !== null }

  async submit(payload: ManualOrderPayload): Promise<ManualOrderResult> {
    this.saved ??= this.readSaved()
    if (this.saved) return this.reconcile()
    const saved = { request_id: this.newId(), payload }
    // Storage failure stops submission, preserving recovery after a reload.
    this.storage.setItem(PREFIX + saved.request_id, JSON.stringify(saved))
    this.saved = saved
    return this.request(true)
  }

  async reconcile(): Promise<ManualOrderResult> {
    this.saved ??= this.readSaved()
    if (!this.saved) return { status: 'rejected', error: 'No pending request' }
    return this.request(false)
  }

  private async request(post: boolean): Promise<ManualOrderResult> {
    if (this.busy) return { status: 'pending', error: 'Checking the current order' }
    this.busy = true
    try {
      const saved = this.saved!
      const send = () => this.transport(`${this.api}/api/order`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...saved.payload, request_id: saved.request_id }),
      })
      let res = post ? await send() : await this.transport(
        `${this.api}/api/order-requests/${saved.request_id}`)
      let data = res.status === 404 ? null : await res.json()
      // A request that never reached the server can reuse the same identity.
      // The server persists and atomically claims it before any broker POST.
      if (!post && (res.status === 404 || data?.status === 'ready')) {
        res = await send()
        data = await res.json()
      }
      if (data?.status === 'confirmed' && data.id) {
        this.storage.removeItem(PREFIX + saved.request_id)
        this.saved = this.readSaved()
        return { status: 'confirmed', id: data.id }
      }
      if (data?.status === 'rejected' || res.status === 422) {
        this.storage.removeItem(PREFIX + saved.request_id)
        this.saved = this.readSaved()
        return { status: 'rejected', error: data?.error || 'Invalid order request' }
      }
      return { status: 'pending', error: data?.error || data?.detail || 'Order outcome unknown; checking broker' }
    } catch {
      return { status: 'pending', error: 'Order outcome unknown; checking broker before another order' }
    } finally {
      this.busy = false
    }
  }
}
