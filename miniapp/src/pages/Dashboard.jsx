/**
 * 控制台概览页面 - 完整实现
 * 显示系统整体状态概览
 */

import { useState, useEffect } from 'react';
import { Calendar, Database } from 'lucide-react';
import { apiFetch } from '../apiBase';
import './../styles/dashboard.css';

const SHANGHAI_TIME_ZONE = 'Asia/Shanghai';

function parseShanghaiDateTime(value) {
  if (value instanceof Date) return value;
  if (typeof value !== 'string') return new Date(value);
  const s = value.trim();
  if (!s) return new Date(NaN);
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return new Date(`${s}T00:00:00+08:00`);
  if (/(Z|[+-]\d{2}:?\d{2})$/i.test(s)) return new Date(s);
  return new Date(`${s.replace(' ', 'T')}+08:00`);
}

function getShanghaiDateParts(date) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: SHANGHAI_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(date);
  const get = (type) => parts.find((p) => p.type === type)?.value;
  return {
    year: get('year'),
    month: get('month'),
    day: get('day'),
  };
}

function formatShanghaiDateKey(date) {
  const { year, month, day } = getShanghaiDateParts(date);
  return `${year}-${month}-${day}`;
}

function formatShanghaiDateTime(value) {
  const d = parseShanghaiDateTime(value);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', { timeZone: SHANGHAI_TIME_ZONE });
}

/**
 * 系统健康档案卡片组件
 */
function HealthCard({ data, loading, batchLogs }) {
  if (loading) {
    return (
      <div className="health-card skeleton-card">
        <div className="skeleton-line" style={{ width: '100%' }}></div>
        <div className="skeleton-line" style={{ width: '80%' }}></div>
      </div>
    );
  }

  const { discord_online, telegram_online, active_api_config, model_name } = data || {};

  // 计算最近批处理状态
  const getBatchStatus = () => {
    if (!batchLogs || batchLogs.length === 0) {
      return { text: '暂无记录', color: 'var(--text-sub)' };
    }
    // 取最近一条（数组已按日期倒序，第一条即最新）
    const latest = batchLogs[0];
    const s4 = latest.step4_status ?? 0;
    const s5 = latest.step5_status ?? 0;
    const allSuccess =
      latest.step1_status === 1 &&
      latest.step2_status === 1 &&
      latest.step3_status === 1 &&
      s4 === 1 &&
      s5 === 1;
    const hasAnyRealFailure =
      latest.error_message ||
      latest.error_stack ||
      latest.retry_count > 0 ||
      [latest.step1_status, latest.step2_status, latest.step3_status, s4, s5].some((v) => Number(v) === 0);
    return allSuccess
      ? { text: '全部成功', color: 'var(--batch-success-text)' }
      : hasAnyRealFailure
        ? { text: '存在失败', color: '#C94A4A' }
        : { text: '处理中', color: 'var(--text-sub)' };
  };

  const batchStatus = getBatchStatus();
  
  return (
    <div className="health-card">
      <div className="health-row">
        <span className="health-label">ENDPOINTS</span>
        <div className="health-value-group">
          <div className="endpoint-item endpoint-item--discord" title={discord_online ? '活跃中' : '离线'}>
            <span className={`status-dot ${discord_online ? 'online' : 'offline'}`}></span>
            Discord
          </div>
          <div className="endpoint-item endpoint-item--telegram" title={telegram_online ? '活跃中' : '离线'}>
            <span className={`status-dot ${telegram_online ? 'online' : 'offline'}`}></span>
            Telegram
          </div>
        </div>
      </div>
      <div className="health-row-divider"></div>
      <div className="health-row">
        <span className="health-label">模型配置</span>
        <div className="health-value value-box value-box--neutral">
          {model_name || active_api_config || '未设置'}
        </div>
      </div>
      <div className="health-row-divider"></div>
      <div className="health-row">
        <span className="health-label">批处理</span>
        <div className={`health-value value-box value-box--${batchStatus.text === '全部成功' ? 'success' : 'error'}`}>
          {batchStatus.text === '全部成功' && <span className="check-icon">✓</span>} {batchStatus.text}
        </div>
      </div>
    </div>
  );
}

/**
 * 跑批状态日历卡片组件
 */
function BatchCalendarCard({ data, loading, selectedDay, onDayClick }) {
  if (loading) {
    return (
      <div className="dashboard-card skeleton-card">
        <div className="card-title">
          <span className="title-icon" aria-hidden>
            <Calendar size={16} strokeWidth={2} />
          </span>
          <span>记忆跑批日历</span>
        </div>
        <div className="batch-calendar-grid">
          {[...Array(7)].map((_, i) => (
            <div key={i} className="calendar-day skeleton-card">
              <div className="skeleton-line" style={{ width: '40px' }}></div>
              <div className="skeleton-line" style={{ width: '20px', height: '20px', borderRadius: '50%' }}></div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  const logs = data || [];
  
  // 生成最近7天的数据（日期键与 batch 日志一致，使用东八区日历日）
  const generateCalendarDays = () => {
    const days = [];
    const today = new Date();
    const todayKey = formatShanghaiDateKey(today);

    for (let i = 6; i >= 0; i--) {
      const date = new Date(today.getTime() - i * 24 * 60 * 60 * 1000);
      const { month, day } = getShanghaiDateParts(date);

      const dateStr = formatShanghaiDateKey(date);
      const logForDay = logs.find(log => (log.date || log.batch_date) === dateStr);
      
      let status = 'none';
      if (logForDay) {
        // 兼容 has_failure 字段；若没有该字段，则只在日志确认为已跑且存在失败/未完成时判失败。
        const s4 = logForDay.step4_status ?? 0;
        const s5 = logForDay.step5_status ?? 0;
        const completedAnyStep =
          Number(logForDay.step1_status) === 1 ||
          Number(logForDay.step2_status) === 1 ||
          Number(logForDay.step3_status) === 1 ||
          Number(s4) === 1 ||
          Number(s5) === 1;
        const hasFailureFlag = logForDay.has_failure === true || logForDay.has_failure === 1;
        const hasRealFailure =
          hasFailureFlag ||
          ((completedAnyStep || logForDay.error_message || logForDay.error_stack || Number(logForDay.retry_count) > 0) &&
            (Number(logForDay.step1_status) === 0 ||
              Number(logForDay.step2_status) === 0 ||
              Number(logForDay.step3_status) === 0 ||
              Number(s4) === 0 ||
              Number(s5) === 0));
        status = hasRealFailure ? 'failed' : 'success';
      }
      
      const isToday = dateStr === todayKey;

      days.push({
        date: dateStr,
        displayDate: `${Number(month)}/${Number(day)}`,
        status,
        log: logForDay,
        isToday
      });
    }
    
    return days;
  };

  const calendarDays = generateCalendarDays();
  const selectedLog = selectedDay ? calendarDays.find(day => day.date === selectedDay)?.log : null;

  return (
    <div className="dashboard-card">
      <div className="card-title">
        <span className="title-icon" aria-hidden>
          <Calendar size={16} strokeWidth={2} />
        </span>
        <span>记忆跑批日历</span>
      </div>
      
      <div className="batch-calendar-grid">
        {calendarDays.map(day => (
          <div 
            key={day.date}
            className={`calendar-day ${selectedDay === day.date ? 'selected' : ''} ${day.isToday ? 'calendar-day--today' : ''}`}
            onClick={() => onDayClick(day.date)}
          >
            <div className="calendar-date">{day.displayDate}</div>
            <div
              className={`calendar-status ${day.status} ${day.isToday ? 'calendar-status--today-mark' : ''}`}
            />
          </div>
        ))}
      </div>
      
      {selectedLog && (
        <div className="batch-detail-panel">
          <div className="batch-step">
            <span className="batch-step-name">Step 1: 时效状态结算</span>
            <div className="batch-step-status">
              <span className={`status-dot ${(selectedLog.step1_success ?? selectedLog.step1_status) ? 'online' : 'offline'}`}></span>
              <span>{selectedLog.step1_duration || '—'}</span>
            </div>
          </div>
          <div className="batch-step">
            <span className="batch-step-name">Step 2: 今日小传</span>
            <div className="batch-step-status">
              <span className={`status-dot ${(selectedLog.step2_success ?? selectedLog.step2_status) ? 'online' : 'offline'}`}></span>
              <span>{selectedLog.step2_duration || '—'}</span>
            </div>
          </div>
          <div className="batch-step">
            <span className="batch-step-name">Step 3: 记忆卡片与时间轴</span>
            <div className="batch-step-status">
              <span className={`status-dot ${(selectedLog.step3_success ?? selectedLog.step3_status) ? 'online' : 'offline'}`}></span>
              <span>{selectedLog.step3_duration || '—'}</span>
            </div>
          </div>
          <div className="batch-step">
            <span className="batch-step-name">Step 4: 向量归档</span>
            <div className="batch-step-status">
              <span className={`status-dot ${(selectedLog.step4_success ?? selectedLog.step4_status) ? 'online' : 'offline'}`}></span>
              <span>{selectedLog.step4_duration || '—'}</span>
            </div>
          </div>
          <div className="batch-step">
            <span className="batch-step-name">Step 5: 记忆 GC</span>
            <div className="batch-step-status">
              <span className={`status-dot ${(selectedLog.step5_success ?? selectedLog.step5_status) ? 'online' : 'offline'}`}></span>
              <span>{selectedLog.step5_duration || '—'}</span>
            </div>
          </div>
          
          {(selectedLog.error_stack || selectedLog.error_message) && (
            <div className="error-stack">
              {selectedLog.error_stack || selectedLog.error_message}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 记忆库概览卡片组件
 */
function MemoryOverviewCard({ data, loading }) {
  if (loading) {
    return (
      <div className="dashboard-card skeleton-card">
        <div className="card-title">
          <span className="title-icon" aria-hidden>
            <Database size={16} strokeWidth={2} />
          </span>
          <span>记忆库概览</span>
        </div>
        <div className="memory-overview-grid">
          <div className="memory-section">
            <div className="skeleton-line" style={{ width: '60%' }}></div>
            <div className="skeleton-line" style={{ width: '40%' }}></div>
          </div>
          <div className="memory-section">
            <div className="skeleton-line" style={{ width: '60%' }}></div>
            <div className="skeleton-line" style={{ width: '40%' }}></div>
          </div>
        </div>
      </div>
    );
  }

  const {
    chromadb_count = 0,
    short_term_limit = 40,
    dimension_status = {},
    chunk_summary_count = 0,
    latest_daily_summary_time = null,
    daily_summary_count = 0,
    active_temporal_states_count = 0
  } = data || {};
  
  const dimensions = [
    { key: 'preferences', name: '偏好习惯' },
    { key: 'interaction_patterns', name: '互动模式' },
    { key: 'current_status', name: '当前状态' },
    { key: 'goals', name: '目标愿望' },
    { key: 'relationships', name: '人际关系' },
    { key: 'key_events', name: '关键事件' },
    { key: 'rules', name: '规则底线' }
  ];

  return (
    <div className="dashboard-card">
      <div className="card-title">
        <span className="title-icon" aria-hidden>
          <Database size={16} strokeWidth={2} />
        </span>
        <span>记忆库概览</span>
      </div>
      
      <div className="memory-overview-grid">
        {/* 左列：长期记忆库 */}
        <div className="memory-section">
          <div className="section-title">
            <span>长期记忆库</span>
          </div>
          <div className="section-content">
            <div className="memory-archive-metrics">
              <div className="memory-archive-col">
                <div className="metric-hero metric-hero--stacked">
                  <div className="metric-hero__label metric-hero__label--top">已归档小传数量</div>
                  <div className="metric-hero__value">{daily_summary_count}</div>
                </div>
              </div>
              <div className="memory-archive-col">
                <div className="metric-hero metric-hero--stacked">
                  <div className="metric-hero__label metric-hero__label--top">已收录片段数量</div>
                  <div className="metric-hero__value">{chromadb_count}</div>
                </div>
              </div>
            </div>
          </div>
        </div>
        
        {/* 右列：实时感知 */}
        <div className="memory-section">
          <div className="section-title">
            <span>实时感知</span>
          </div>
          <div className="section-content">
            <div className="realtime-kpi-row">
              <div className="realtime-kpi-col">
                <div className="metric-hero metric-hero--tight metric-hero--stacked">
                  <div className="metric-hero__label metric-hero__label--top">短期携带量（条）</div>
                  <div className="metric-hero__value">{short_term_limit}</div>
                </div>
              </div>
              <div className="realtime-kpi-col">
                <div className="metric-hero metric-hero--tight metric-hero--stacked">
                  <div className="metric-hero__label metric-hero__label--top">活跃时效状态（条）</div>
                  <div className="metric-hero__value">{active_temporal_states_count}</div>
                </div>
              </div>
            </div>

            <p className="metric-secondary metric-secondary--dark metric-line--after-kpi">
              今日微批摘要条数 <span className="number-tag">{chunk_summary_count} 条</span>
            </p>

            <div className="dimension-block">
              <span className="visually-hidden">七个维度记忆卡片是否在库中激活，悬停圆点可查看维度名称</span>
              <div className="dimension-grid">
                {dimensions.map(dim => (
                  <div key={dim.key} className="dimension-dot-container">
                    <div
                      className={`dimension-dot ${dimension_status[dim.key] ? 'filled' : 'empty'}`}
                      title={dim.name}
                      aria-label={dim.name}
                    />
                    <div className="dimension-tooltip">{dim.name}</div>
                  </div>
                ))}
              </div>
            </div>

            {latest_daily_summary_time && (
              <p className="metric-secondary metric-secondary--dark" style={{ marginTop: '12px', fontSize: '0.85rem' }}>
                最近每日摘要时间: {formatShanghaiDateTime(latest_daily_summary_time)}
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * 骨架屏组件
 */
function SkeletonLoader() {
  return (
    <div className="skeleton-container">
      <div className="health-card skeleton-card">
        <div className="skeleton-line" style={{ width: '100%' }}></div>
        <div className="skeleton-line" style={{ width: '80%' }}></div>
      </div>
      
      <div className="dashboard-main-grid">
        <div className="dashboard-card skeleton-card">
          <div className="card-title">
            <span className="title-icon"><Calendar size={18} strokeWidth={2} /></span>
            <div className="skeleton-line" style={{ width: '120px' }}></div>
          </div>
          <div className="batch-calendar-grid">
            {[...Array(7)].map((_, i) => (
              <div key={i} className="calendar-day skeleton-card">
                <div className="skeleton-line" style={{ width: '40px' }}></div>
                <div className="skeleton-line" style={{ width: '20px', height: '20px', borderRadius: '50%' }}></div>
              </div>
            ))}
          </div>
        </div>
        
        <div className="dashboard-card skeleton-card">
          <div className="card-title">
            <span className="title-icon"><Database size={18} strokeWidth={2} /></span>
            <div className="skeleton-line" style={{ width: '120px' }}></div>
          </div>
          <div className="memory-overview-grid">
            <div className="memory-section">
              <div className="skeleton-line" style={{ width: '60%' }}></div>
              <div className="skeleton-line" style={{ width: '40%' }}></div>
            </div>
            <div className="memory-section">
              <div className="skeleton-line" style={{ width: '60%' }}></div>
              <div className="skeleton-line" style={{ width: '40%' }}></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * 主 Dashboard 组件
 */
function Dashboard() {
  const [loading, setLoading] = useState(true);
  const [statusData, setStatusData] = useState(null);
  const [batchLogData, setBatchLogData] = useState([]);
  const [memoryData, setMemoryData] = useState(null);
  const [selectedCalendarDay, setSelectedCalendarDay] = useState(null);
  const [error, setError] = useState(null);

  // 并发获取数据
  useEffect(() => {
    const fetchDashboardData = async () => {
      setLoading(true);
      setError(null);
      
      try {
        // 并发请求三个接口
        const [statusRes, batchLogRes, memoryRes] = await Promise.all([
          apiFetch('/api/dashboard/status'),
          apiFetch('/api/dashboard/batch-log'),
          apiFetch('/api/dashboard/memory-overview')
        ]);

        // 检查响应状态
        if (!statusRes.ok || !batchLogRes.ok || !memoryRes.ok) {
          throw new Error('部分接口请求失败');
        }

        // 解析 JSON 数据
        const [statusJson, batchLogJson, memoryJson] = await Promise.all([
          statusRes.json(),
          batchLogRes.json(),
          memoryRes.json()
        ]);

        // 检查 API 返回格式
        if (statusJson.success) setStatusData(statusJson.data);
        if (batchLogJson.success) setBatchLogData(batchLogJson.data || []);
        if (memoryJson.success) setMemoryData(memoryJson.data);
        
      } catch (err) {
        console.error('获取 Dashboard 数据失败:', err);
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    fetchDashboardData();
  }, []);

  // 处理日历日期点击
  const handleDayClick = (date) => {
    setSelectedCalendarDay(date === selectedCalendarDay ? null : date);
  };

  if (loading) {
    return (
      <div className="dashboard-container">
        <SkeletonLoader />
      </div>
    );
  }

  if (error) {
    return (
      <div className="dashboard-container">
        <div className="dashboard-card" style={{ textAlign: 'center', color: '#E07070' }}>
          <h2>⚠️ 数据加载失败</h2>
          <p>{error}</p>
          <p style={{ marginTop: '12px', fontSize: '0.9rem', color: 'var(--text-sub)' }}>
            已显示模拟数据供预览
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="dashboard-container">
      {/* 系统健康档案卡片 */}
      <HealthCard data={statusData} loading={loading} batchLogs={batchLogData} />
      
      {/* 主内容网格：跑批状态 + 记忆库概览 */}
      <div className="dashboard-main-grid">
        <BatchCalendarCard 
          data={batchLogData} 
          loading={loading}
          selectedDay={selectedCalendarDay}
          onDayClick={handleDayClick}
        />
        
        <MemoryOverviewCard data={memoryData} loading={loading} />
      </div>
    </div>
  );
}

export default Dashboard;
