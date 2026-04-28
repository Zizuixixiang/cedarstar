import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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

const SHANGHAI_TIME_ZONE = 'Asia/Shanghai'

const PERIODS = [
  { key: 'current', label: '本次' },
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

function numberOrNull(value) {
  if (value === undefined || value === null || value === '') return null
  const n = Number(value)
  return Number.isNaN(n) ? null : n
}

function providerCacheHitTokens(row) {
  const explicit = numberOrNull(row?.provider_cache_hit_tokens)
  if (explicit !== null) return explicit
  return Math.max(
    Number(row?.cache_hit_tokens || 0),
    Number(row?.cache_read_input_tokens || 0),
    Number(row?.cached_tokens || 0),
  )
}

function providerCacheHitRate(row) {
  const explicit = numberOrNull(row?.cache_hit_rate)
  if (explicit !== null) return explicit
  const prompt = Number(row?.prompt_tokens || 0)
  if (prompt <= 0) return null
  return providerCacheHitTokens(row) / prompt
}

function theoreticalCacheHitRate(row) {
  const explicit = numberOrNull(row?.theoretical_cache_hit_rate)
  return explicit === null ? providerCacheHitRate(row) : explicit
}

function theoreticalCachedTokens(row) {
  const explicit = numberOrNull(row?.theoretical_cached_tokens)
  return explicit === null ? providerCacheHitTokens(row) : explicit
}

function normalizeUsageStats(data) {
  if (!data) return data
  const recent = Array.isArray(data.recent) ? data.recent : []
  const firstRow = recent[0]
  if (!firstRow) {
    return {
      ...data,
      totals: data.totals || {
        total_tokens: 0,
        prompt_tokens: 0,
        completion_tokens: 0,
        cached_tokens: 0,
        cache_write_tokens: 0,
        cache_hit_tokens: 0,
        cache_miss_tokens: 0,
        cache_creation_input_tokens: 0,
        cache_read_input_tokens: 0,
        provider_cache_hit_tokens: 0,
        theoretical_cached_tokens: 0,
        call_count: 0,
        cache_hit_rate: 0,
        theoretical_cache_hit_rate: 0,
      },
      by_platform: Array.isArray(data.by_platform) ? data.by_platform : [],
      by_model: Array.isArray(data.by_model) ? data.by_model : [],
      by_day: Array.isArray(data.by_day) ? data.by_day : [],
      recent,
    }
  }
  const prompt = Number(firstRow.prompt_tokens || 0)
  const safeTotals = data.totals || {}
  const fallbackHitTokens = providerCacheHitTokens(firstRow)
  const hasTotalCacheFields = [
    'provider_cache_hit_tokens',
    'cache_hit_tokens',
    'cache_read_input_tokens',
    'cached_tokens',
  ].some((key) => safeTotals[key] !== undefined && safeTotals[key] !== null)
  const hitTokens = hasTotalCacheFields ? providerCacheHitTokens(safeTotals) : fallbackHitTokens
  const totals = {
    total_tokens: Number(safeTotals.total_tokens ?? firstRow.total_tokens ?? 0),
    prompt_tokens: Number(safeTotals.prompt_tokens ?? prompt),
    completion_tokens: Number(safeTotals.completion_tokens ?? firstRow.completion_tokens ?? 0),
    cached_tokens: Number(safeTotals.cached_tokens ?? firstRow.cached_tokens ?? 0),
    cache_write_tokens: Number(safeTotals.cache_write_tokens ?? firstRow.cache_write_tokens ?? 0),
    cache_hit_tokens: Number(safeTotals.cache_hit_tokens ?? firstRow.cache_hit_tokens ?? 0),
    cache_miss_tokens: Number(safeTotals.cache_miss_tokens ?? firstRow.cache_miss_tokens ?? 0),
    cache_creation_input_tokens: Number(safeTotals.cache_creation_input_tokens ?? firstRow.cache_creation_input_tokens ?? 0),
    cache_read_input_tokens: Number(safeTotals.cache_read_input_tokens ?? firstRow.cache_read_input_tokens ?? 0),
    provider_cache_hit_tokens: Number(safeTotals.provider_cache_hit_tokens ?? firstRow.provider_cache_hit_tokens ?? hitTokens),
    theoretical_cached_tokens: Number(safeTotals.theoretical_cached_tokens ?? firstRow.theoretical_cached_tokens ?? hitTokens),
    call_count: Number(safeTotals.call_count ?? recent.length ?? 0),
    cache_hit_rate: Number(safeTotals.cache_hit_rate ?? (prompt > 0 ? hitTokens / prompt : 0)),
    theoretical_cache_hit_rate: Number(safeTotals.theoretical_cache_hit_rate ?? firstRow.theoretical_cache_hit_rate ?? (prompt > 0 ? hitTokens / prompt : 0)),
  }
  const platformRows = Array.isArray(data.by_platform) && data.by_platform.length > 0 ? data.by_platform : [{ ...totals, platform: firstRow.platform || 'unknown' }]
  const modelRows = Array.isArray(data.by_model) && data.by_model.length > 0 ? data.by_model : [{ ...totals, model: firstRow.model || 'unknown' }]
  return {
    ...data,
    totals,
    by_platform: platformRows,
    by_model: modelRows,
    by_day: Array.isArray(data.by_day) ? data.by_day : [],
    recent,
  }
}

function oneCallUsageView(data) {
  const row = data?.recent?.[0]
  if (!row) return data
  const hitTokens = providerCacheHitTokens(row)
  const prompt = Number(row.prompt_tokens || 0)
  const totals = {
    total_tokens: Number(row.total_tokens || 0),
    prompt_tokens: prompt,
    completion_tokens: Number(row.completion_tokens || 0),
    cached_tokens: Number(row.cached_tokens || 0),
    cache_write_tokens: Number(row.cache_write_tokens || 0),
    cache_hit_tokens: Number(row.cache_hit_tokens || 0),
    cache_miss_tokens: Number(row.cache_miss_tokens || 0),
    cache_creation_input_tokens: Number(row.cache_creation_input_tokens || 0),
    cache_read_input_tokens: Number(row.cache_read_input_tokens || 0),
    provider_cache_hit_tokens: Number(row.provider_cache_hit_tokens ?? hitTokens),
    theoretical_cached_tokens: Number(row.theoretical_cached_tokens ?? 0),
    call_count: 1,
    cache_hit_rate: Number(row.cache_hit_rate ?? (prompt > 0 ? hitTokens / prompt : 0)),
    theoretical_cache_hit_rate: Number(row.theoretical_cache_hit_rate ?? 0),
  }
  const platformRow = { ...totals, platform: row.platform || 'unknown' }
  const modelRow = { ...totals, model: row.model || 'unknown' }
  return {
    ...data,
    totals,
    by_platform: [platformRow],
    by_model: [modelRow],
    by_day: [],
    recent: [row],
  }
}

function shortDate(value) {
  if (!value) return '-'
  const text = String(value).trim()
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : `${text.replace(' ', 'T')}+08:00`
  const d = new Date(normalized)
  if (Number.isNaN(d.getTime())) return text
  return d.toLocaleString('zh-CN', { hour12: false, timeZone: SHANGHAI_TIME_ZONE })
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
            <th>读缓存</th>
            <th>写缓存</th>
            <th>命中</th>
            <th>Total</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan="7" className="obs-empty">暂无记录</td>
            </tr>
          ) : rows.map((row) => (
            <tr key={row[labelKey]}>
              <td data-label={labelKey === 'model' ? 'Model' : 'Platform'}>
                <div className="obs-table-primary">{row[labelKey] || 'unknown'}</div>
                <TinyBar value={row.total_tokens} max={max} />
              </td>
              <td data-label="Calls">{fmt(row.call_count)}</td>
              <td data-label="Prompt">{fmt(row.prompt_tokens)}</td>
              <td data-label="读缓存">{fmt(row.cache_read_input_tokens)}</td>
              <td data-label="写缓存">{fmt(row.cache_creation_input_tokens || row.cache_write_tokens)}</td>
              <td data-label="命中">{fmt(providerCacheHitTokens(row))}</td>
              <td data-label="Total">{fmt(row.total_tokens)}</td>
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
  const limit = 160
  const summary = row.result_summary || ''
  const clipped = summary.length > limit ? `${summary.slice(0, limit)}…` : summary
  return (
    <article className="tool-row">
      <button type="button" className="tool-row-main" onClick={() => setOpen((v) => !v)}>
        <span className="tool-name">{row.tool_name}</span>
        <span className="tool-meta">{row.platform || 'unknown'} · turn {row.turn_id} · #{row.seq}</span>
        <span className="tool-time">{shortDate(row.created_at)}</span>
      </button>
      <p className="tool-summary">{clipped || '无摘要'}{summary.length > limit && !open ? ' ' : ''}</p>
      {summary.length > limit && (
        <button type="button" className="tool-more-btn" onClick={() => setOpen((v) => !v)}>
          {open ? '收起' : '显示全文'}
        </button>
      )}
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

function RecentUsageRow({ row }) {
  const cacheHit = providerCacheHitTokens(row)
  const cacheRead = Number(row.cache_read_input_tokens || 0)
  const cacheCreate = Number(row.cache_creation_input_tokens || row.cache_write_tokens || 0)
  const hitRate = providerCacheHitRate(row)
  return (
    <article className="recent-call-row">
      <div className="recent-call-main">
        <span>{shortDate(row.created_at)}</span>
        <strong>{row.model || 'unknown'}</strong>
        <span>{row.platform || 'unknown'}</span>
        <span className="recent-call-metric"><span>Total</span><strong>{fmt(row.total_tokens)}</strong></span>
        <span className="recent-call-metric"><span>读缓存</span><strong>{fmt(cacheRead)}</strong></span>
        <span className="recent-call-metric"><span>写缓存</span><strong>{fmt(cacheCreate)}</strong></span>
        <span className="recent-call-metric"><span>命中</span><strong>{fmt(cacheHit)}</strong></span>
        <span className="recent-call-metric"><span>命中率</span><strong>{hitRate == null ? '-' : pct(hitRate)}</strong></span>
        <span className="recent-call-metric"><span>理论上限</span><strong>{pct(theoreticalCacheHitRate(row))}</strong></span>
      </div>
      <div className="recent-call-mini">
        <span className="recent-call-metric"><span>Prompt</span><strong>{fmt(row.prompt_tokens)}</strong></span>
        <span className="recent-call-metric"><span>理论可命中</span><strong>{fmt(theoreticalCachedTokens(row))}</strong></span>
        <span className="recent-call-metric"><span>原始 usage</span><strong>{row.raw_usage_json ? 'yes' : 'no'}</strong></span>
      </div>
    </article>
  )
}

export default function Observability() {
  const [period, setPeriod] = useState('current')
  const [usage, setUsage] = useState(null)
  const [tools, setTools] = useState([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  const [toolPage, setToolPage] = useState(1)
  const toolPageSize = 10
  const loadSeqRef = useRef(0)

  const load = useCallback(async () => {
    const seq = ++loadSeqRef.current
    setLoading(true)
    setUsage(null)
    try {
      const [usageRes, toolsRes] = await Promise.all([
        apiFetch(`/api/observability/usage?period=${period}`),
        apiFetch('/api/observability/tool-executions?limit=60'),
      ])
      const usageData = await usageRes.json()
      const toolsData = await toolsRes.json()
      const toolPayload = toolsData.success ? toolsData.data : []
      const usagePayload = usageData.success ? usageData.data : null
      const normalizedTools = Array.isArray(toolPayload) ? toolPayload : (toolPayload?.items || [])
      const normalizedUsage = period === 'current' ? oneCallUsageView(usagePayload) : normalizeUsageStats(usagePayload)
      if (loadSeqRef.current === seq) {
        setUsage(normalizedUsage)
        setTools(normalizedTools)
        setToolPage(1)
      }
    } finally {
      if (loadSeqRef.current === seq) setLoading(false)
    }
  }, [period])

  useEffect(() => {
    load()
  }, [load])

  const totals = usage?.totals || {}
  const periodLabel = PERIODS.find((item) => item.key === period)?.label || period
  const visibleTools = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return tools
    return tools.filter((row) => {
      const hay = `${row.tool_name} ${row.platform} ${row.session_id} ${row.turn_id} ${row.result_summary}`.toLowerCase()
      return hay.includes(q)
    })
  }, [tools, filter])

  const toolTotalPages = Math.max(1, Math.ceil(visibleTools.length / toolPageSize))
  const currentToolPage = Math.min(toolPage, toolTotalPages)
  const pagedTools = visibleTools.slice((currentToolPage - 1) * toolPageSize, currentToolPage * toolPageSize)

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
        <MetricCard icon={Activity} label="调用次数" value={fmt(totals.call_count)} hint={loading ? '加载中' : periodLabel} />
        <MetricCard icon={BarChart3} label="总 tokens" value={fmt(totals.total_tokens)} hint={`Prompt ${fmt(totals.prompt_tokens)} / Completion ${fmt(totals.completion_tokens)}`} />
        <MetricCard icon={DatabaseZap} label="缓存命中 tokens" value={fmt(totals.provider_cache_hit_tokens || providerCacheHitTokens(totals))} hint={`实际 ${pct(totals.cache_hit_rate)} / 理论 ${totals.theoretical_cache_hit_rate == null ? '-' : pct(totals.theoretical_cache_hit_rate)}`} />
        <MetricCard icon={LineChart} label="缓存写入 tokens" value={fmt(totals.cache_creation_input_tokens || totals.cache_write_tokens || 0)} hint={`理论可命中 ${totals.theoretical_cached_tokens == null ? '-' : fmt(totals.theoretical_cached_tokens)} tokens`} />
      </section>

      <section className="obs-panel two-col">
        <div>
          <div className="obs-panel-title">
            <BarChart3 size={16} strokeWidth={1.8} aria-hidden />
            <h2>按平台</h2>
          </div>
          <p className="obs-field-note">使用供应商返回的 usage 字段。</p>
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
            <RecentUsageRow key={row.id} row={row} />
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
          {pagedTools.length === 0 ? (
            <div className="obs-empty">暂无工具执行记录</div>
          ) : pagedTools.map((row) => <ToolRow key={row.id} row={row} />)}
        </div>
        <div className="obs-pagination">
          <button type="button" disabled={currentToolPage <= 1} onClick={() => setToolPage((p) => Math.max(1, p - 1))}>
            上一页
          </button>
          <span>{currentToolPage} / {toolTotalPages}</span>
          <button type="button" disabled={currentToolPage >= toolTotalPages} onClick={() => setToolPage((p) => Math.min(toolTotalPages, p + 1))}>
            下一页
          </button>
        </div>
      </section>
    </div>
  )
}
