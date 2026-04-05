/**
 * Backend origin for REST calls. Empty string = same origin; dev uses Vite `/api` proxy.
 * Production: set `VITE_API_BASE_URL` (e.g. in `.env.production`).
 */
const raw = import.meta.env.VITE_API_BASE_URL;
export const API_BASE_URL =
  raw === undefined || raw === null
    ? ''
    : String(raw).trim().replace(/\/$/, '');

export function apiUrl(path) {
  const p = path.startsWith('/') ? path : `/${path}`;
  return API_BASE_URL ? `${API_BASE_URL}${p}` : p;
}

export const MINIAPP_TOKEN = import.meta.env.VITE_MINIAPP_TOKEN || '';

export async function apiFetch(path, options = {}) {
  const url = apiUrl(path);
  const headers = {
    'Content-Type': 'application/json',
    'X-Cedarstar-Token': MINIAPP_TOKEN,
    ...(options.headers || {}),
  };
  return fetch(url, { ...options, headers });
}
