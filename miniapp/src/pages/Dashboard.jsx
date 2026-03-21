/**
 * 控制台概览页面 - 完整实现
 * 显示系统整体状态概览
 */

import { useState, useEffect } from 'react';
import './../styles/dashboard.css';

// API 基础 URL
const API_BASE_URL = 'http://localhost:8000';

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
    return allSuccess
      ? { text: '全部成功', color: 'var(--status-green)' }
      : { text: '存在失败', color: '#E07070' };
  };

  const batchStatus = getBatchStatus();
  
  return (
    <div className="health-card">
      <div className="health-item">
        <span className="health-label">Discord 状态</span>
        <div className="health-value">
          <span className={`status-dot ${discord_online ? 'online' : 'offline'}`}></span>
          {discord_online ? '活跃中' : '离线'}
        </div>
      </div>
      
      <div className="health-item">
        <span className="health-label">Telegram 状态</span>
        <div className="health-value">
          <span className={`status-dot ${telegram_online ? 'online' : 'offline'}`}></span>
          {telegram_online ? '活跃中' : '离线'}
        </div>
      </div>
      
      <div className="health-item">
        <span className="health-label">激活配置</span>
        <div className="health-value">
          {active_api_config || '未设置'} · {model_name || '未设置'}
        </div>
      </div>
      
      <div className="health-item">
        <span className="health-label">最近批处理</span>
        <div className="health-value" style={{ color: batchStatus.color }}>
          {batchStatus.text}
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
          <span>📅 记忆跑批日历</span>
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
  
  // 生成最近7天的数据
  const generateCalendarDays = () => {
    const days = [];
    const today = new Date();
    
    for (let i = 6; i >= 0; i--) {
      const date = new Date(today);
      date.setDate(today.getDate() - i);
      
      const dateStr = date.toISOString().split('T')[0];
      const logForDay = logs.find(log => (log.date || log.batch_date) === dateStr);
      
      let status = 'none';
      if (logForDay) {
        // 兼容 has_failure 字段或通过 step_status 判断
        const s4 = logForDay.step4_status ?? 0;
        const s5 = logForDay.step5_status ?? 0;
        const hasFail = logForDay.has_failure ??
          (logForDay.step1_status === 0 || logForDay.step2_status === 0 || logForDay.step3_status === 0 ||
            s4 === 0 || s5 === 0);
        status = hasFail ? 'failed' : 'success';
      }
      
      days.push({
        date: dateStr,
        displayDate: `${date.getMonth() + 1}/${date.getDate()}`,
        status,
        log: logForDay
      });
    }
    
    return days;
  };

  const calendarDays = generateCalendarDays();
  const selectedLog = selectedDay ? calendarDays.find(day => day.date === selectedDay)?.log : null;

  return (
    <div className="dashboard-card">
      <div className="card-title">
        <span>📅 记忆跑批日历</span>
      </div>
      
      <div className="batch-calendar-grid">
        {calendarDays.map(day => (
          <div 
            key={day.date}
            className={`calendar-day ${selectedDay === day.date ? 'selected' : ''}`}
            onClick={() => onDayClick(day.date)}
          >
            <div className="calendar-date">{day.displayDate}</div>
            <div className={`calendar-status ${day.status}`}></div>
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
          <span>📚 记忆库概览</span>
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
    longterm_score_threshold = 7,
    short_term_limit = 40,
    dimension_status = {},
    chunk_summary_count = 0,
    latest_daily_summary_time = null
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
        <span>📚 记忆库概览</span>
      </div>
      
      <div className="memory-overview-grid">
        {/* 左列：长期记忆库 */}
        <div className="memory-section">
          <div className="section-title">
            <span>长期记忆库</span>
          </div>
          <div className="section-content">
            <p>已收录片段数量 <span className="number-tag">{chromadb_count} 条</span></p>
            <p style={{ marginTop: '8px' }}>记忆打分阈值 <span className="number-tag">≥ {longterm_score_threshold} 分</span></p>
          </div>
        </div>
        
        {/* 右列：实时感知 */}
        <div className="memory-section">
          <div className="section-title">
            <span>实时感知</span>
            <span className="number-tag">{short_term_limit} 条</span>
          </div>
          <div className="section-content">
            <p style={{ marginBottom: '12px' }}>短期携带量 <span className="number-tag">{short_term_limit} 条</span></p>
            
            <div style={{ marginTop: '12px' }}>
              <p style={{ fontSize: '0.85rem', color: 'var(--text-sub)', marginBottom: '8px' }}>
                7个维度记忆卡片状态：
              </p>
              <div className="dimension-grid">
                {dimensions.map(dim => (
                  <div key={dim.key} className="dimension-dot-container">
                    <div 
                      className={`dimension-dot ${dimension_status[dim.key] ? 'filled' : 'empty'}`}
                    ></div>
                    <div className="dimension-tooltip">{dim.name}</div>
                  </div>
                ))}
              </div>
            </div>
            
            {latest_daily_summary_time && (
              <p style={{ marginTop: '12px', fontSize: '0.85rem' }}>
                最近摘要: {new Date(latest_daily_summary_time).toLocaleDateString('zh-CN')}
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
          fetch(`${API_BASE_URL}/api/dashboard/status`),
          fetch(`${API_BASE_URL}/api/dashboard/batch-log`),
          fetch(`${API_BASE_URL}/api/dashboard/memory-overview`)
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