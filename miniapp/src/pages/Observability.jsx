import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  BarChart3,
  Braces,
  Clock3,
  DatabaseZap,
  Hammer,
  LineChart,
  RefreshCw,
  Search,
} from 'lucide-react'
import { apiFetch } from '../apiBase'
import './../styles/observability.css'

const PERIODS = [
  { key: 'today', label: '今日' },
  { key: 'week', label: '本周' },
  { key: 'month', label: '本月' },
]

function fmt(n) {
  if (n == null || Number.isNaN(Number(n))) return '0'
  return Number(n).toLocaleString()
}

function pct(n) {
  if (n == null || Number.isNaN(Number(n))) return '0.0%'
  return `${(Number(n) * 100).toFixed(1)}%`
}

function shortDate(value) {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  return d.toLocaleString('zh-CN', { hour12: false })
}

function MetricCard({ icon: Icon, label, value, hint }) {
  return (
    <section className="obs-metric">
      <div className="obs-metric-icon">
        <Icon size={18} strokeWidth={1.8} aria-hidden />
      </div>
      <div className="obs-metric-copy">
        <span className="obs-metric-label">{label}</span>
        <strong className="obs-metric-value">{value}</strong>
        {hint && <span className="obs-metric-hint">{hint}</span>}
      </div>
    </section>
  )
}

function TinyBar({ value, max }) {
  const width = max > 0 ? Math.max(3, Math.round((Number(value || 0) / max) * 100)) : 0
  return (
    <span className="obs-tinybar" aria-hidden>
      <span style={{ width: `${width}%` }} />
    </span>
  )
}

function UsageTable({ rows, labelKey }) {
  const max = Math.max(...rows.map((r) => Number(r.total_tokens || 0)), 0)
  return (
    <div className="obs-table-wrap">
      <table className="obs-table">
        <thead>
          <tr>
            <th>{labelKey === 'model' ? '模型' : '平台'}</th>
            <th>调用</th>
            <th>Prompt</th>
            <th>缓存读</th>
            <th>缓存写</th>
            <th>Total</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan="6" className="obs-empty">暂无记录</td>
            </tr>
          ) : rows.map((row) => (
            <tr key={row[labelKey]}>
              <td>
                <div className="obs-table-primary">{row[labelKey] || 'unknown'}</div>
                <TinyBar value={row.total_tokens} max={max} />
              </td>
              <td>{fmt(row.call_count)}</td>
              <td>{fmt(row.prompt_tokens)}</td>
              <td>{fmt((row.cached_tokens || 0) + (row.cache_hit_tokens || 0))}</td>
              <td>{fmt(row.cache_write_tokens)}</td>
              <td>{fmt(row.total_tokens)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ToolRow({ row }) {
  const [open, setOpen] = useState(false)
  const raw = row.result_raw_preview || ''
  const args = row.arguments_json ? JSON.stringify(row.arguments_json, null, 2) : ''
  return (
    <article className="tool-row">
      <button type="button" className="tool-row-main" onClick={() => setOpen((v) => !v)}>
        <span className="tool-name">{row.tool_name}</span>
        <span className="tool-meta">{row.platform || 'unknown'} · turn {row.turn_id} · #{row.seq}</span>
        <span className="tool-time">{shortDate(row.created_at)}</span>
      </button>
      <p className="tool-summary">{row.result_summary || '无摘要'}</p>
      {open && (
        <div className="tool-detail">
          {args && (
            <pre>
              <code>{args}</code>
            </pre>
          )}
          {raw ? (
            <pre>
              <code>{raw}{row.result_raw_length > raw.length ? '\n...(raw 已截断展示)' : ''}</code>
            </pre>
          ) : (
            <div className="obs-empty">没有 raw 预览</div>
          )}
        </div>
      )}
    </article>
  )
}

export default function Observability() {
  const [period, setPeriod] = useState('today')
  const [usage, setUsage] = useState(null)
  const [tools, setTools] = useState([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [usageRes, toolsRes] = await Promise.all([
        apiFetch(`/api/observability/usage?period=${period}`),
        apiFetch('/api/observability/tool-executions?limit=60'),
      ])
      const usageData = await usageRes.json()
      const toolsData = await toolsRes.json()
      setUsage(usageData.success ? usageData.data : null)
      setTools(toolsData.success ? (toolsData.data || []) : [])
    } finally {
      setLoading(false)
    }
  }, [period])

  useEffect(() => {
    load()
  }, [load])

  const totals = usage?.totals || {}
  const visibleTools = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return tools
    return tools.filter((row) => {
      const hay = `${row.tool_name} ${row.platform} ${row.session_id} ${row.turn_id} ${row.result_summary}`.toLowerCase()
      return hay.includes(q)
    })
  }, [tools, filter])

  return (
    <div className="observability-page">
      <header className="obs-header">
        <div>
          <p className="obs-kicker">LLM OBSERVABILITY</p>
          <h1>调用观测</h1>
        </div>
        <div className="obs-actions">
          <div className="obs-periods" role="tablist" aria-label="统计周期">
            {PERIODS.map((item) => (
              <button
                type="button"
                key={item.key}
                className={`obs-period ${period === item.key ? 'active' : ''}`}
                onClick={() => setPeriod(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <button type="button" className="obs-refresh" onClick={load} title="刷新">
            <RefreshCw size={16} strokeWidth={1.8} aria-hidden />
          </button>
        </div>
      </header>

      <section className="obs-grid">
        <MetricCard icon={Activity} label="调用次数" value={fmt(totals.call_count)} hint={loading ? '加载中' : period} />
        <MetricCard icon={BarChart3} label="总 tokens" value={fmt(totals.total_tokens)} hint={`Prompt ${fmt(totals.prompt_tokens)} / Completion ${fmt(totals.completion_tokens)}`} />
        <MetricCard icon={DatabaseZap} label="缓存读取 tokens" value={fmt((totals.cached_tokens || 0) + (totals.cache_hit_tokens || 0) + (totals.cache_read_input_tokens || 0))} hint={`估算命中率 ${pct(totals.cache_hit_rate)}`} />
        <MetricCard icon={LineChart} label="缓存写入 tokens" value={fmt((totals.cache_write_tokens || 0) + (totals.cache_creation_input_tokens || 0))} hint="不内置价格表" />
      </section>

      <section className="obs-panel two-col">
        <div>
          <div className="obs-panel-title">
            <BarChart3 size={16} strokeWidth={1.8} aria-hidden />
            <h2>按平台</h2>
          </div>
          <UsageTable rows={usage?.by_platform || []} labelKey="platform" />
        </div>
        <div>
          <div className="obs-panel-title">
            <Braces size={16} strokeWidth={1.8} aria-hidden />
            <h2>按模型</h2>
          </div>
          <UsageTable rows={usage?.by_model || []} labelKey="model" />
        </div>
      </section>

      <section className="obs-panel">
        <div className="obs-panel-title">
          <Clock3 size={16} strokeWidth={1.8} aria-hidden />
          <h2>最近调用</h2>
        </div>
        <div className="recent-calls">
          {(usage?.recent || []).length === 0 ? (
            <div className="obs-empty">暂无 token usage 记录</div>
          ) : (usage?.recent || []).slice(0, 12).map((row) => (
            <div className="recent-call" key={row.id}>
              <span>{shortDate(row.created_at)}</span>
              <strong>{row.model || 'unknown'}</strong>
              <span>{row.platform || 'unknown'}</span>
              <span>total {fmt(row.total_tokens)}</span>
              <span>cache {fmt((row.cached_tokens || 0) + (row.cache_hit_tokens || 0) + (row.cache_read_input_tokens || 0))}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="obs-panel">
        <div className="obs-panel-toolbar">
          <div className="obs-panel-title">
            <Hammer size={16} strokeWidth={1.8} aria-hidden />
            <h2>工具执行</h2>
          </div>
          <label className="obs-search">
            <Search size={15} strokeWidth={1.7} aria-hidden />
            <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="过滤工具、平台、turn、摘要" />
          </label>
        </div>
        <div className="tool-list">
          {visibleTools.length === 0 ? (
            <div className="obs-empty">暂无工具执行记录</div>
          ) : visibleTools.map((row) => <ToolRow key={row.id} row={row} />)}
        </div>
      </section>
    </div>
  )
}
