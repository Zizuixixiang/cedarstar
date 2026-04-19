import { useCallback, useEffect, useState } from 'react'
import { apiFetch } from '../apiBase'

const MONTHS = [
  'JAN',
  'FEB',
  'MAR',
  'APR',
  'MAY',
  'JUN',
  'JUL',
  'AUG',
  'SEP',
  'OCT',
  'NOV',
  'DEC',
]

function fmtHeaderDate(d) {
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`
}

function fmtTimeOnly(iso) {
  if (!iso) return '--'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '--'
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

function fmtSteps(n) {
  if (n == null || Number.isNaN(Number(n))) return '--'
  return Number(n).toLocaleString('en-US')
}

function weatherGlyph(condition, icon) {
  const t = (condition || '').toLowerCase()
  if (t.includes('晴') || t.includes('sun')) return '☀'
  if (t.includes('雨') || t.includes('rain')) return '☂'
  if (t.includes('雪') || t.includes('snow')) return '❄'
  if (t.includes('云') || t.includes('阴')) return '☁'
  return icon ? '◌' : '☁'
}

function WaveDivider() {
  return (
    <div className="wave-wrap" aria-hidden>
      <svg viewBox="0 0 400 12" preserveAspectRatio="none">
        <path
          d="M0,6 Q25,0 50,6 T100,6 T150,6 T200,6 T250,6 T300,6 T350,6 T400,6 L400,12 L0,12 Z"
          fill="var(--ink)"
        />
      </svg>
    </div>
  )
}

export default function Home() {
  const [weather, setWeather] = useState(null)
  const [sensor, setSensor] = useState(null)
  const [loading, setLoading] = useState(true)
  const [wakeLoading, setWakeLoading] = useState(false)
  const [toast, setToast] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [w, s] = await Promise.all([
        apiFetch('/weather/current'),
        apiFetch('/sensor/summary'),
      ])
      setWeather(w && typeof w === 'object' ? w : null)
      setSensor(s && typeof s === 'object' ? s : null)
    } catch {
      setWeather(null)
      setSensor(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const now = new Date()
  const city = (weather && weather.city) || 'NANJING'
  const health = sensor && sensor.health
  const battery = sensor && sensor.battery

  const steps = health && health.steps != null ? health.steps : null
  const heart = health && health.heart_rate != null ? health.heart_rate : null
  const hasBattery = battery != null && typeof battery === 'object'
  const battLevel = hasBattery && battery.level != null ? battery.level : null
  const charging = hasBattery && battery.charging === true

  const temp = weather && weather.temp != null ? String(weather.temp) : '--'
  const cond = weather && weather.condition ? String(weather.condition) : '--'
  const hi = weather && weather.high != null ? weather.high : '--'
  const lo = weather && weather.low != null ? weather.low : '--'

  async function onWake() {
    setWakeLoading(true)
    setToast('')
    try {
      const r = await apiFetch('/autonomous/trigger', {
        method: 'POST',
        body: '{}',
      })
      if (r && r.success) {
        setToast(r.message || 'OK')
        window.setTimeout(() => setToast(''), 3500)
      } else {
        setToast('N/A')
        window.setTimeout(() => setToast(''), 3500)
      }
    } catch {
      setToast('N/A')
      window.setTimeout(() => setToast(''), 3500)
    } finally {
      setWakeLoading(false)
    }
  }

  return (
    <div>
      <header className="header-row">
        <div>
          <div className="header-city">{city}</div>
          <div className="header-sub">JIANGSU / CN</div>
        </div>
        <div className="header-date">
          {fmtHeaderDate(now)}
          <br />
          {now.getFullYear()}
        </div>
      </header>

      <section className="weather-block">
        <div className="weather-temp">
          <span>
            {loading ? '--' : temp}
            {temp !== '--' ? '°' : ''}
          </span>
          <span className="weather-icon">
            {weatherGlyph(weather && weather.condition, weather && weather.icon)}
          </span>
        </div>
        <div className="weather-desc">{loading ? '--' : cond.toUpperCase()}</div>
        <div className="weather-hilo">
          H: {hi}
          {hi !== '--' ? '°' : ''} &nbsp; L: {lo}
          {lo !== '--' ? '°' : ''}
        </div>
      </section>

      <WaveDivider />

      <div className="grid-2">
        <div className="card">
          <div className="card-label">STEPS</div>
          <div className="stat-value">{fmtSteps(steps)}</div>
          <div className="stat-sub">OF 10K</div>
        </div>
        <div className="card">
          <div className="card-label">HEART</div>
          <div className="stat-value">{heart != null ? heart : '--'}</div>
          <div className="stat-sub">BPM AVG</div>
        </div>
        <div className="card">
          <div className="card-label">BATTERY</div>
          <div className="stat-value">
            {battLevel != null ? `${battLevel}%` : '--'}
          </div>
          <div className="stat-sub">
            {!hasBattery ? '--' : charging ? 'CHARGING' : 'NOT CHARGING'}
          </div>
        </div>
        <div className="card">
          <div className="card-label">LAST SEEN</div>
          <div className="stat-value">
            {sensor && sensor.last_seen ? fmtTimeOnly(sensor.last_seen) : '--'}
          </div>
          <div className="stat-sub">
            {!sensor
              ? '--'
              : sensor.is_active
                ? 'ACTIVE'
                : 'INACTIVE'}
          </div>
        </div>
      </div>

      <button
        type="button"
        className="btn-wake"
        disabled={wakeLoading}
        onClick={onWake}
      >
        {wakeLoading ? '…' : '[ WAKE UP / 唤醒 ]'}
      </button>
      {toast ? <div className="toast">{toast}</div> : null}
    </div>
  )
}
