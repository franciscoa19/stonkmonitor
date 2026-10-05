export interface Account {
  equity: number
  cash: number
  buying_power: number
  day_trade_count: number
  status: string
}

const isNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value)

/** True only for a usable account payload. The backend answers 503 when the
 *  broker is unreachable, but anything that reaches state is checked here too:
 *  an empty object is truthy and used to crash the Trade tab on render. */
export function isAccount(value: unknown): value is Account {
  if (typeof value !== 'object' || value === null) return false
  const account = value as Record<string, unknown>
  return isNumber(account.equity) && isNumber(account.cash) && isNumber(account.buying_power)
}
