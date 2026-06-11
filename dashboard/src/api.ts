const BASE = import.meta.env.VITE_API_URL || ''

async function req<T = any>(path: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(BASE + path, { ...opts, headers: { 'Content-Type': 'application/json', ...opts?.headers } })
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
  return r.json()
}

export const api = {
  status:     () => req('/status'),
  positions:  () => req('/positions'),
  decisions:  (limit = 30) => req(`/decisions?limit=${limit}`),
  executions: (limit = 30) => req(`/executions?limit=${limit}`),
  metrics:    () => req('/metrics'),
  portfolio:  () => req('/portfolio'),
  regime:     () => req('/regime'),
  backtest:   (ticker: string, period = '1y') => req(`/backtest/${ticker}?period=${period}`),
  trigger:    () => req('/trigger', { method: 'POST' }),
  halt:       () => req('/halt', { method: 'POST', body: JSON.stringify({ reason: 'dashboard halt' }) }),
  resume:     () => req('/resume', { method: 'POST' }),
  predictions:      () => req('/predictions'),
  addPrediction:    (p: { symbol: string; name?: string; category?: string; entry_price?: number; quantity?: number }) =>
    req('/predictions', { method: 'POST', body: JSON.stringify(p) }),
  removePrediction: (symbol: string) => req(`/predictions/${symbol}`, { method: 'DELETE' }),
  scanStatus:       () => req('/scan-status'),
  brain:            () => req('/brain'),
}
