/**
 * 系统日志页面 - 完整实现
 * 查看系统运行日志
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { apiUrl } from '../apiBase';
import './../styles/logs.css';

// 平台选项
const PLATFORM_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'telegram', label: 'Telegram' },
  { value: 'discord', label: 'Discord' },
  { value: 'batch', label: '跑批任务' }
];

// 日志级别选项
const LEVEL_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'ERROR', label: 'ERROR' },
  { value: 'WARNING', label: 'WARNING' },
  { value: 'INFO', label: 'INFO' }
];

/**
 * Toast 提示组件
 */
function Toast({ message, type = 'info', onClose }) {
  useEffect(() => {
    const timer = setTimeout(() => {
      onClose();
    }, 2000);
    return () => clearTimeout(timer);
  }, [onClose]);

  return (
    <div className={`toast ${type}`}>
      {type === 'success' && '✓'}
      {type === 'error' && '✗'}
      {type === 'info' && 'ℹ️'}
      <span>{message}</span>
    </div>
  );
}

/**
 * 级别标签组件
 */
function LevelTag({ level }) {
  const levelClass = level ? level.toLowerCase() : 'info';
  return <span className={`level-tag ${levelClass}`}>{level}</span>;
}

/**
 * 平台标签组件
 */
function PlatformTag({ platform }) {
  if (platform === 'telegram') {
    return <span className="platform-tag telegram">Telegram</span>;
  } else if (platform === 'discord') {
    return <span className="platform-tag discord">Discord</span>;
  } else if (platform === 'batch') {
    return <span className="platform-tag batch">跑批任务</span>;
  } else {
    return <span className="platform-tag system">系统</span>;
  }
}

/**
 * 日志行组件
 */
function LogRow({ log }) {
  const [expanded, setExpanded] = useState(false);

  const formatTimestamp = (timestamp) => {
    try {
      const date = new Date(timestamp);
      return date.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      });
    } catch {
      return '未知时间';
    }
  };

  const hasStackTrace = log.stack_trace && log.stack_trace.trim();
  const isError = log.level === 'ERROR';

  return (
    <>
      <div className={`log-row ${isError ? 'error' : ''}`}>
        <div className="timestamp">{formatTimestamp(log.created_at)}</div>
        <div className="log-badges">
          <LevelTag level={log.level} />
          <PlatformTag platform={log.platform} />
        </div>
        <div className="log-message">{log.message}</div>
        {hasStackTrace && (
          <button
            className="expand-button"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? '收起' : '展开'}
          </button>
        )}
      </div>
      {expanded && hasStackTrace && (
        <div className="stack-trace">
          {log.stack_trace}
        </div>
      )}
    </>
  );
}

/**
 * 骨架屏组件（仅首次加载使用）
 */
function SkeletonLoader() {
  return (
    <div className="logs-container">
      <div className="skeleton-loader">
        <div className="skeleton-filter"></div>
        <div className="logs-content-scroll-area">
          <div className="logs-list-container">
            <div className="skeleton-log-row"></div>
            <div className="skeleton-log-row"></div>
            <div className="skeleton-log-row"></div>
            <div className="skeleton-log-row"></div>
            <div className="skeleton-log-row"></div>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * 空状态组件
 */
function EmptyState() {
  return (
    <div className="empty-state">
      <div className="empty-state-icon">📝</div>
      <div className="empty-state-text">暂无日志记录</div>
    </div>
  );
}

/**
 * 分段按钮组件
 */
function SegmentedButtons({ options, value, onChange }) {
  return (
    <div className="segmented-buttons">
      {options.map(option => (
        <button
          key={option.value}
          className={`segmented-button ${value === option.value ? 'active' : ''}`}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/**
 * 主 Logs 组件
 */
function Logs() {
  // 状态管理
  const [loading, setLoading] = useState(true);   // 仅首次加载
  const [fetching, setFetching] = useState(false); // 后续筛选（半透明遮罩）
  const [logs, setLogs] = useState([]);
  const [toasts, setToasts] = useState([]);

  // 筛选状态
  const [selectedPlatform, setSelectedPlatform] = useState('');
  const [selectedLevel, setSelectedLevel] = useState('');
  const [searchKeyword, setSearchKeyword] = useState('');

  // 分页状态
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize] = useState(50);
  const [totalItems, setTotalItems] = useState(0);

  // 防抖引用
  const searchTimeoutRef = useRef(null);
  // 跳过初次挂载的防抖触发
  const mountedRef = useRef(false);

  // 添加 Toast
  const addToast = useCallback((message, type = 'info') => {
    const id = Date.now();
    setToasts(prev => [...prev, { id, message, type }]);
  }, []);

  // 移除 Toast
  const removeToast = useCallback((id) => {
    setToasts(prev => prev.filter(toast => toast.id !== id));
  }, []);

  // 核心加载函数：所有参数显式传入，避免 state 异步问题
  const fetchLogs = useCallback(async ({
    platform,
    level,
    keyword,
    page,
    pageSz,
    isInit = false
  }) => {
    try {
      if (isInit) {
        setLoading(true);
      } else {
        setFetching(true);
      }

      const params = new URLSearchParams({
        page: page.toString(),
        page_size: pageSz.toString()
      });

      if (platform) params.append('platform', platform);
      if (level) params.append('level', level);
      if (keyword.trim()) params.append('keyword', keyword.trim());

      const response = await fetch(apiUrl(`/api/logs?${params}`));
      if (!response.ok) throw new Error('获取日志失败');

      const data = await response.json();

      if (data.success) {
        setLogs(data.data?.logs || []);
        setTotalItems(data.data?.total || 0);

        if (keyword.trim() && (data.data?.logs?.length || 0) === 0) {
          addToast(`未找到包含"${keyword}"的日志记录`, 'info');
        }
      } else {
        throw new Error(data.message || '获取数据失败');
      }
    } catch (error) {
      console.error('加载日志失败:', error);
      addToast('加载失败，请稍后重试', 'error');
      setLogs([]);
      setTotalItems(0);
    } finally {
      setLoading(false);
      setFetching(false);
    }
  }, [addToast]);

  // 初始化加载（仅挂载时执行一次）
  useEffect(() => {
    fetchLogs({
      platform: selectedPlatform,
      level: selectedLevel,
      keyword: searchKeyword,
      page: currentPage,
      pageSz: pageSize,
      isInit: true
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 关键词搜索防抖（1000ms，跳过初次挂载）
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true;
      return;
    }
    if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current);
    searchTimeoutRef.current = setTimeout(() => {
      setCurrentPage(1);
      fetchLogs({
        platform: selectedPlatform,
        level: selectedLevel,
        keyword: searchKeyword,
        page: 1,
        pageSz: pageSize
      });
    }, 1000);
    return () => {
      if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchKeyword]);

  // 平台切换
  const handlePlatformChange = (platform) => {
    setSelectedPlatform(platform);
    setCurrentPage(1);
    fetchLogs({
      platform,
      level: selectedLevel,
      keyword: searchKeyword,
      page: 1,
      pageSz: pageSize
    });
  };

  // 级别切换
  const handleLevelChange = (level) => {
    setSelectedLevel(level);
    setCurrentPage(1);
    fetchLogs({
      platform: selectedPlatform,
      level,
      keyword: searchKeyword,
      page: 1,
      pageSz: pageSize
    });
  };

  // 上一页
  const handlePrevPage = () => {
    if (currentPage > 1) {
      const newPage = currentPage - 1;
      setCurrentPage(newPage);
      fetchLogs({
        platform: selectedPlatform,
        level: selectedLevel,
        keyword: searchKeyword,
        page: newPage,
        pageSz: pageSize
      });
    }
  };

  // 下一页
  const handleNextPage = () => {
    const totalPages = Math.ceil(totalItems / pageSize);
    if (currentPage < totalPages) {
      const newPage = currentPage + 1;
      setCurrentPage(newPage);
      fetchLogs({
        platform: selectedPlatform,
        level: selectedLevel,
        keyword: searchKeyword,
        page: newPage,
        pageSz: pageSize
      });
    }
  };

  const totalPages = Math.ceil(totalItems / pageSize) || 1;

  if (loading) {
    return <SkeletonLoader />;
  }

  return (
    <div className="logs-container">
      {/* Toast 提示容器 */}
      <div className="toast-container">
        {toasts.map(toast => (
          <Toast
            key={toast.id}
            message={toast.message}
            type={toast.type}
            onClose={() => removeToast(toast.id)}
          />
        ))}
      </div>

      {/* 筛选栏 */}
      <div className="filter-bar">
        <div className="filter-row">
          {/* 平台切换 */}
          <div className="filter-group">
            <div className="filter-label">平台</div>
            <SegmentedButtons
              options={PLATFORM_OPTIONS}
              value={selectedPlatform}
              onChange={handlePlatformChange}
            />
          </div>

          {/* 日志级别 */}
          <div className="filter-group">
            <div className="filter-label">日志级别</div>
            <SegmentedButtons
              options={LEVEL_OPTIONS}
              value={selectedLevel}
              onChange={handleLevelChange}
            />
          </div>

          {/* 关键词搜索 */}
          <div className="filter-group">
            <div className="filter-label">关键词搜索</div>
            <input
              type="text"
              className="search-input"
              placeholder="输入关键词..."
              value={searchKeyword}
              onChange={(e) => setSearchKeyword(e.target.value)}
            />
          </div>
        </div>
      </div>

      {/* 日志列表可滚动区域 */}
      <div className="logs-content-scroll-area">
        {/* 日志列表 */}
        <div className="logs-list-container">
          <div className="section-title">
            <span>🪵 系统日志</span>
            <span style={{ color: 'var(--text-sub)', fontSize: '14px', marginLeft: 'auto' }}>
              共 {totalItems} 条记录
            </span>
          </div>

          <div
            className="logs-list"
            style={{
              opacity: fetching ? 0.5 : 1,
              transition: 'opacity 0.2s ease',
              pointerEvents: fetching ? 'none' : 'auto'
            }}
          >
            {logs.length === 0 ? (
              <EmptyState />
            ) : (
              logs.map(log => (
                <LogRow key={log.id} log={log} />
              ))
            )}
          </div>

          {/* 分页控件 */}
          {logs.length > 0 && totalPages > 1 && (
            <div className="pagination">
              <button
                className="pagination-button"
                onClick={handlePrevPage}
                disabled={currentPage <= 1}
              >
                上一页
              </button>
              <span className="pagination-info">
                第 {currentPage} 页 / 共 {totalPages} 页
              </span>
              <button
                className="pagination-button"
                onClick={handleNextPage}
                disabled={currentPage >= totalPages}
              >
                下一页
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default Logs;
