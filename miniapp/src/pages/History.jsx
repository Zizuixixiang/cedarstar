/**
 * 对话历史页面 - 完整实现
 * 查看和管理历史对话记录
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { apiFetch } from '../apiBase';
import './../styles/history.css';

// 平台选项
const PLATFORM_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'telegram', label: 'Telegram' },
  { value: 'discord', label: 'Discord' },
  { value: 'rikkahub', label: 'RikkaHub', disabled: true }
];

// 日期快捷选项
const DATE_RANGE_OPTIONS = [
  { value: '7', label: '近 7 天' },
  { value: '30', label: '近 30 天' },
  { value: '90', label: '近 3 个月' },
  { value: '180', label: '近半年' },
  { value: '365', label: '近一年' },
  { value: 'custom', label: '自定义范围' }
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
 * 平台标签组件
 */
function PlatformTag({ platform }) {
  if (platform === 'telegram') {
    return <span className="platform-tag telegram">Telegram</span>;
  } else if (platform === 'discord') {
    return <span className="platform-tag discord">Discord</span>;
  } else {
    return <span className="platform-tag">未知平台</span>;
  }
}

/**
 * 高亮关键词函数：将文本中匹配关键词的部分包裹为 <mark> 元素
 */
function highlightText(text, keyword) {
  if (!keyword || !keyword.trim() || !text) {
    return text;
  }
  // 转义正则特殊字符
  const escaped = keyword.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const regex = new RegExp(`(${escaped})`, 'gi');
  const parts = text.split(regex);
  return parts.map((part, i) =>
    regex.test(part)
      ? <mark key={i} className="highlight-keyword">{part}</mark>
      : part
  );
}

/**
 * 消息气泡组件
 */
function MessageBubble({ message, keyword }) {
  const [expanded, setExpanded] = useState(false);
  const [thinkingExpanded, setThinkingExpanded] = useState(false);

  const isUser = message.role === 'user';
  const isAssistant = message.role === 'assistant';

  // 判断内容是否需要折叠（超过5行）
  const contentLines = message.content.split('\n').filter(line => line.trim());
  const shouldCollapse = contentLines.length > 5;
  const displayContent = shouldCollapse && !expanded
    ? contentLines.slice(0, 5).join('\n') + '...'
    : message.content;

  // 格式化时间
  const formatTime = (timestamp) => {
    try {
      const date = new Date(timestamp);
      return date.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
      });
    } catch (error) {
      return '未知时间';
    }
  };

  return (
    <div className={`message-bubble ${isUser ? 'user' : 'assistant'}`}>
      <div className="message-header">
        <PlatformTag platform={message.platform} />
        <span className="time-text">{formatTime(message.created_at)}</span>
      </div>

      <div className="message-content">
        {highlightText(displayContent, keyword)}
      </div>

      {shouldCollapse && (
        <button
          className="toggle-content-btn"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? '收起' : '展开全文'}
        </button>
      )}

      {isAssistant && message.thinking && message.thinking.trim() && (
        <>
          <button
            className="toggle-thinking-btn"
            onClick={() => setThinkingExpanded(!thinkingExpanded)}
          >
            {thinkingExpanded ? '收起思维链' : '🧠 展开思维链'}
          </button>

          {thinkingExpanded && (
            <div className="thinking-container">
              {highlightText(message.thinking, keyword)}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/**
 * 骨架屏组件
 */
function SkeletonLoader() {
  return (
    <div className="history-container">
      <div className="skeleton-loader">
        {/* 筛选栏骨架屏 */}
        <div className="skeleton-filter"></div>

        {/* 消息列表骨架屏 */}
        <div className="message-list-container">
          <div className="history-chat-column">
            <div className="skeleton-message"></div>
            <div className="skeleton-message"></div>
            <div className="skeleton-message"></div>
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
      <div className="empty-state-icon">🗨️</div>
      <div className="empty-state-text">暂无对话记录</div>
    </div>
  );
}

/**
 * 计算日期范围（纯函数，不依赖组件状态）
 */
function calculateDateRange(option, customDateFrom, customDateTo) {
  if (option === 'custom') {
    return { from: customDateFrom, to: customDateTo };
  }

  const today = new Date();
  const fromDate = new Date();
  const days = parseInt(option, 10);
  if (!isNaN(days)) {
    fromDate.setDate(today.getDate() - days);
  }

  const formatDate = (date) => {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  };

  return {
    from: formatDate(fromDate),
    to: formatDate(today)
  };
}

/**
 * 主 History 组件
 */
function History() {
  // 状态管理
  const [loading, setLoading] = useState(true);   // 初次加载
  const [fetching, setFetching] = useState(false); // 后续筛选加载（不替换页面）
  const [messages, setMessages] = useState([]);
  const [toasts, setToasts] = useState([]);

  // 筛选状态 - 用 ref 保存最新值供 loadHistory 使用
  const [selectedPlatform, setSelectedPlatform] = useState('');
  const [searchKeyword, setSearchKeyword] = useState('');
  const [dateRangeOption, setDateRangeOption] = useState('7');
  const [customDateFrom, setCustomDateFrom] = useState('');
  const [customDateTo, setCustomDateTo] = useState('');

  // 分页状态
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize] = useState(30);
  const [totalItems, setTotalItems] = useState(0);

  // 防抖引用
  const searchTimeoutRef = useRef(null);
  // 标记是否已完成初始化（防止搜索防抖 effect 在挂载时多触发一次请求）
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

  // 核心加载函数 - 接收所有参数，避免依赖 React state 的异步问题
  // isInit: 是否为初次加载（决定用骨架屏还是局部遮罩）
  const fetchHistory = useCallback(async ({
    platform,
    keyword,
    dateOption,
    dateFrom,
    dateTo,
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
      const dateRange = calculateDateRange(dateOption, dateFrom, dateTo);

      const params = new URLSearchParams({
        page: page.toString(),
        page_size: pageSz.toString()
      });

      if (platform) {
        params.append('platform', platform);
      }

      if (keyword.trim()) {
        params.append('keyword', keyword.trim());
      }

      if (dateRange.from) {
        params.append('date_from', dateRange.from);
      }

      if (dateRange.to) {
        params.append('date_to', dateRange.to);
      }

      console.log('API请求参数:', params.toString());

      const response = await apiFetch(`/api/history?${params}`);
      if (!response.ok) {
        throw new Error('获取对话历史失败');
      }

      const data = await response.json();

      if (data.success) {
        console.log('API响应数据:', data.data?.messages?.length || 0, '条消息');
        setMessages(data.data?.messages || []);
        setTotalItems(data.data?.total || 0);

        if (keyword.trim() && data.data?.messages?.length === 0) {
          addToast(`未找到包含"${keyword}"的对话记录`, 'info');
        }
      } else {
        throw new Error(data.message || '获取数据失败');
      }
    } catch (error) {
      console.error('加载对话历史失败:', error);
      addToast('加载失败，请稍后重试', 'error');
      setMessages([]);
      setTotalItems(0);
    } finally {
      setLoading(false);
      setFetching(false);
    }
  }, [addToast]);

  // 初始化加载
  useEffect(() => {
    fetchHistory({
      platform: selectedPlatform,
      keyword: searchKeyword,
      dateOption: dateRangeOption,
      dateFrom: customDateFrom,
      dateTo: customDateTo,
      page: currentPage,
      pageSz: pageSize,
      isInit: true
    });
    // 仅在挂载时执行一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 搜索防抖 - 仅监听 searchKeyword，跳过初次挂载
  useEffect(() => {
    // 初次挂载时跳过，避免与初始化加载重复请求
    if (!mountedRef.current) {
      mountedRef.current = true;
      return;
    }

    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current);
    }

    searchTimeoutRef.current = setTimeout(() => {
      console.log('触发搜索，关键词:', searchKeyword);
      setCurrentPage(1);
      fetchHistory({
        platform: selectedPlatform,
        keyword: searchKeyword,
        dateOption: dateRangeOption,
        dateFrom: customDateFrom,
        dateTo: customDateTo,
        page: 1,
        pageSz: pageSize
      });
    }, 1000);

    return () => {
      if (searchTimeoutRef.current) {
        clearTimeout(searchTimeoutRef.current);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchKeyword]);

  // 平台切换
  const handlePlatformChange = (platform) => {
    setSelectedPlatform(platform);
    setCurrentPage(1);
    fetchHistory({
      platform,
      keyword: searchKeyword,
      dateOption: dateRangeOption,
      dateFrom: customDateFrom,
      dateTo: customDateTo,
      page: 1,
      pageSz: pageSize
    });
  };

  // 日期快捷选项切换
  const handleDateRangeChange = (option) => {
    setDateRangeOption(option);
    setCurrentPage(1);
    // custom 模式等用户填写日期后再触发，非 custom 立即加载
    if (option !== 'custom') {
      fetchHistory({
        platform: selectedPlatform,
        keyword: searchKeyword,
        dateOption: option,
        dateFrom: customDateFrom,
        dateTo: customDateTo,
        page: 1,
        pageSz: pageSize
      });
    }
  };

  // 自定义开始日期
  const handleCustomDateFromChange = (date) => {
    setCustomDateFrom(date);
    if (dateRangeOption === 'custom') {
      setCurrentPage(1);
      fetchHistory({
        platform: selectedPlatform,
        keyword: searchKeyword,
        dateOption: 'custom',
        dateFrom: date,
        dateTo: customDateTo,
        page: 1,
        pageSz: pageSize
      });
    }
  };

  // 自定义结束日期
  const handleCustomDateToChange = (date) => {
    setCustomDateTo(date);
    if (dateRangeOption === 'custom') {
      setCurrentPage(1);
      fetchHistory({
        platform: selectedPlatform,
        keyword: searchKeyword,
        dateOption: 'custom',
        dateFrom: customDateFrom,
        dateTo: date,
        page: 1,
        pageSz: pageSize
      });
    }
  };

  // 上一页
  const handlePrevPage = () => {
    if (currentPage > 1) {
      const newPage = currentPage - 1;
      setCurrentPage(newPage);
      fetchHistory({
        platform: selectedPlatform,
        keyword: searchKeyword,
        dateOption: dateRangeOption,
        dateFrom: customDateFrom,
        dateTo: customDateTo,
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
      fetchHistory({
        platform: selectedPlatform,
        keyword: searchKeyword,
        dateOption: dateRangeOption,
        dateFrom: customDateFrom,
        dateTo: customDateTo,
        page: newPage,
        pageSz: pageSize
      });
    }
  };

  // 计算总页数
  const totalPages = Math.ceil(totalItems / pageSize) || 1;

  if (loading) {
    return <SkeletonLoader />;
  }

  return (
    <div className="history-container">
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
        <div className="filter-controls-row">
          {/* 平台切换 */}
          <div className="filter-group">
            <div className="filter-label">平台</div>
            <div className="platform-tabs">
              {PLATFORM_OPTIONS.map(option => (
                <button
                  key={option.value}
                  className={`tab-button ${
                    selectedPlatform === option.value ? 'active' : ''
                  } ${option.disabled ? 'disabled' : ''}`}
                  onClick={() => !option.disabled && handlePlatformChange(option.value)}
                  disabled={option.disabled}
                >
                  {option.label}
                  {option.disabled && ' (即将支持)'}
                </button>
              ))}
            </div>
          </div>

          {/* 关键词搜索 */}
          <div className="filter-group">
            <div className="filter-label">关键词搜索</div>
            <input
              type="text"
              className="history-search-input"
              placeholder="输入关键词..."
              value={searchKeyword}
              onChange={(e) => setSearchKeyword(e.target.value)}
            />
          </div>

          {/* 日期快捷选项 */}
          <div className="filter-group">
            <div className="filter-label">时间范围</div>
            <select
              className="date-select"
              value={dateRangeOption}
              onChange={(e) => handleDateRangeChange(e.target.value)}
            >
              {DATE_RANGE_OPTIONS.map(option => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* 自定义日期范围（独立一行，避免拉高主筛选行） */}
        <div className={`date-range-container ${dateRangeOption === 'custom' ? '' : 'hidden'}`}>
          <input
            type="date"
            className="date-select"
            value={customDateFrom}
            onChange={(e) => handleCustomDateFromChange(e.target.value)}
            placeholder="开始日期"
          />
          <span className="date-range-sep">至</span>
          <input
            type="date"
            className="date-select"
            value={customDateTo}
            onChange={(e) => handleCustomDateToChange(e.target.value)}
            placeholder="结束日期"
          />
        </div>
      </div>

      {/* 消息列表（外层全宽卡片；内层窄栏模拟移动端对话宽度） */}
      <div className="message-list-container">
        <div className="history-chat-column">
          <div className="section-title">
            <span>🕰️ 对话历史</span>
            <span style={{ color: 'var(--text-sub)', fontSize: '14px', marginLeft: 'auto' }}>
              共 {totalItems} 条记录
            </span>
          </div>

          <div className="message-list" style={{ opacity: fetching ? 0.5 : 1, transition: 'opacity 0.2s ease', pointerEvents: fetching ? 'none' : 'auto' }}>
            {messages.length === 0 ? (
              <EmptyState />
            ) : (
              messages.map(message => (
                <div key={message.id} className={`message-row ${message.role === 'user' ? 'user-row' : 'assistant-row'}`}>
                  <MessageBubble message={message} keyword={searchKeyword} />
                </div>
              ))
            )}
          </div>

          {messages.length > 0 && totalPages > 1 && (
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

export default History;
