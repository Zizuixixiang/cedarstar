const rawBase = import.meta.env.VITE_API_BASE_URL
const API_BASE_URL =
  rawBase === undefined || rawBase === null
    ? ''
    : String(rawBase).trim().replace(/\/$/, '')
const PORTAL_TOKEN = String(import.meta.env.VITE_PORTAL_TOKEN || '').trim()

export function apiUrl(path) {
  return `${API_BASE_URL}/api${path}`
}

export async function apiFetch(path, options = {}) {
  const method = String(options.method || 'GET').toUpperCase()
  /** 与 miniapp 一致：GET 且无 body 时不带 Content-Type，减少代理/预检问题 */
  const headers = {
    ...(method === 'GET' && options.body == null
      ? {}
      : { 'Content-Type': 'application/json' }),
    'X-Cedarstar-Token': PORTAL_TOKEN,
    ...(options.headers || {}),
  }
  const res = await fetch(apiUrl(path), {
    ...options,
    headers,
  })
  return res.json()
}
