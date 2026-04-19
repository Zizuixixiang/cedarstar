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

function fmtDiaryMeta(iso) {
  if (!iso) return '--'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '--'
  const mon = MONTHS[d.getMonth()]
  const day = d.getDate()
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${mon} ${String(day).padStart(2, '0')}  ${hh}:${mm}`
}

function reasonTag(raw) {
  const m = {
    scheduled: 'SCHEDULED',
    manual: 'MANUAL',
    sensor_change: 'SENSOR',
  }
  if (!raw) return 'N/A'
  const k = String(raw).toLowerCase()
  return m[k] || String(raw).toUpperCase()
}

function previewTitle(title, content) {
  const t = (title || '').trim()
  if (t) return t.toUpperCase()
  const c = (content || '').trim()
  if (!c) return 'UNTITLED'
  return c.slice(0, 20).toUpperCase()
}

export default function Diary() {
  const [page, setPage] = useState(1)
  const pageSize = 20
  const [total, setTotal] = useState(0)
  const [items, setItems] = useState([])
  const [openId, setOpenId] = useState(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await apiFetch(
        `/autonomous/diary?page=${page}&page_size=${pageSize}`
      )
      const data = r && r.data
      if (data && Array.isArray(data.items)) {
        setTotal(data.total || 0)
        setItems(data.items)
      } else {
        setTotal(0)
        setItems([])
      }
    } catch {
      setTotal(0)
      setItems([])
    } finally {
      setLoading(false)
    }
  }, [page])

  useEffect(() => {
    load()
  }, [load])

  const maxPage = Math.max(1, Math.ceil(total / pageSize) || 1)

  return (
    <div>
      <h1 className="page-title">DIARY</h1>
      {loading ? (
        <p className="muted">LOADING…</p>
      ) : items.length === 0 ? (
        <div className="empty">NO ENTRIES YET</div>
      ) : (
        items.map((row) => {
          const id = row.id
          const open = openId === id
          return (
            <div
              key={id}
              className="card diary-card"
              role="button"
              tabIndex={0}
              onClick={() => setOpenId(open ? null : id)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  setOpenId(open ? null : id)
                }
              }}
            >
              <div className="meta">{fmtDiaryMeta(row.created_at)}</div>
              <div className="headline">{previewTitle(row.title, row.content)}</div>
              <span className="tag">{reasonTag(row.trigger_reason)}</span>
              {open ? (
                <div className="diary-detail">{row.content || ''}</div>
              ) : null}
            </div>
          )
        })
      )}

      <div className="pager">
        <button
          type="button"
          disabled={page <= 1 || loading}
          onClick={() => setPage((p) => Math.max(1, p - 1))}
        >
          PREV
        </button>
        <span className="stat-sub">
          {page} / {maxPage}
        </span>
        <button
          type="button"
          disabled={page >= maxPage || loading}
          onClick={() => setPage((p) => p + 1)}
        >
          NEXT
        </button>
      </div>
    </div>
  )
}
