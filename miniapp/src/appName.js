/**
 * 构建时展示名；各实例在 .env.development / .env.production 中设置 VITE_APP_NAME。
 */
const raw = import.meta.env.VITE_APP_NAME;
export const APP_DISPLAY_NAME =
  raw !== undefined && raw !== null && String(raw).trim() !== ''
    ? String(raw).trim()
    : 'CedarStar';
