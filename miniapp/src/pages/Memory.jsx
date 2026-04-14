/**
 * 记忆管理页面 - 完整实现
 * 管理 AI 助手的记忆卡片和长期记忆库
 */

import { useState, useEffect, useLayoutEffect, useRef, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { apiFetch } from '../apiBase';
import './../styles/memory.css';

// 维度映射
const DIMENSION_MAP = {
  preferences: '偏好',
  interaction_patterns: '相处模式',
  current_status: '近况',
  goals: '目标',
  relationships: '关系',
  key_events: '重要事件',
  rules: '规则'
};

const EVENT_TYPE_MAP = {
  milestone: '里程碑',
  emotional_shift: '情感转折',
  conflict: '冲突',
  daily_warmth: '日常温情'
};

/**
 * 判断记忆卡片正文是否被 -webkit-line-clamp 截断。
 * 部分环境下截断后 scrollHeight === clientHeight，仅用 scrollHeight 会漏掉「查看全文」。
 */
function isMemoryCardContentTruncated(el, text) {
  if (!el || !text || !String(text).trim()) return false;
  const w = el.clientWidth;
  const visibleH = el.clientHeight;
  if (w <= 0 || visibleH <= 0) return false;

  const cs = window.getComputedStyle(el);
  const probe = document.createElement('div');
  probe.setAttribute('aria-hidden', 'true');
  probe.style.cssText = [
    'position:fixed',
    'left:0',
    'top:0',
    'visibility:hidden',
    'pointer-events:none',
    'z-index:-1',
    'width:' + w + 'px',
    'box-sizing:border-box',
    'max-height:none',
    'overflow:visible',
    'display:block',
    'white-space:' + cs.whiteSpace,
    'word-break:' + cs.wordBreak,
    'overflow-wrap:' + cs.overflowWrap,
    'font:' + cs.font,
    'letter-spacing:' + cs.letterSpacing,
    'padding:' + cs.padding,
  ].join(';');
  probe.textContent = text;
  document.body.appendChild(probe);
  const fullH = probe.scrollHeight;
  document.body.removeChild(probe);

  return fullH > visibleH + 2;
}

const MEMORY_TABS = [
  { id: 'cards', label: '记忆卡片' },
  { id: 'longterm', label: '长期记忆' },
  { id: 'temporal', label: '时效状态' },
  { id: 'timeline', label: '关系时间线' }
];

function getTemporalDisplayStatus(row) {
  const activeFlag = Number(row.is_active) === 1;
  let beforeExpire = true;
  if (row.expire_at) {
    const t = new Date(row.expire_at).getTime();
    if (!Number.isNaN(t) && t <= Date.now()) {
      beforeExpire = false;
    }
  }
  if (activeFlag && beforeExpire) {
    return { label: '生效中', className: 'temporal-status-active' };
  }
  return { label: '已过期', className: 'temporal-status-expired' };
}

function formatLastAccessTs(ts) {
  if (ts == null || ts === '') {
    return '—';
  }
  const n = Number(ts);
  if (Number.isNaN(n)) {
    return '—';
  }
  return new Date(n * 1000).toLocaleString('zh-CN');
}

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
 * 只读查看全文弹窗（无需进入编辑）
 */
function ViewMemoryCardModal({ dimension, content, onClose }) {
  useEffect(() => {
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKey = (e) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      document.body.style.overflow = prevOverflow;
      window.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  if (typeof document === 'undefined') {
    return null;
  }

  const title = DIMENSION_MAP[dimension];

  return createPortal(
    <div className="memory-view-overlay" onClick={onClose} role="presentation">
      <div
        className="memory-view-sheet"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="memory-view-title"
      >
        <header className="memory-view-sheet-header">
          <div className="memory-view-sheet-headings">
            <h2 id="memory-view-title" className="memory-view-sheet-title">
              {title}
            </h2>
            <p className="memory-view-sheet-sub">只读预览</p>
          </div>
          <button type="button" className="memory-view-close" onClick={onClose} aria-label="关闭">
            完成
          </button>
        </header>
        <div className="memory-view-sheet-body">{content}</div>
      </div>
    </div>,
    document.body
  );
}

/**
 * 编辑弹窗组件
 */
function EditModal({ dimension, content, onClose, onSave }) {
  const [editContent, setEditContent] = useState(content || '');
  const [showConfirm, setShowConfirm] = useState(false);
  
  const handleSave = () => {
    setShowConfirm(true);
  };
  
  const confirmSave = () => {
    onSave(dimension, editContent);
    setShowConfirm(false);
  };
  
  if (showConfirm) {
    return (
      <div className="modal-overlay">
        <div className="modal-container confirm-modal">
          <div className="modal-title">确认更新</div>
          <div className="confirm-message">
            确认更新<span style={{ color: 'var(--accent)', fontWeight: '500' }}> {DIMENSION_MAP[dimension]} </span>
            的记忆卡片吗？
          </div>
          <div className="confirm-warning">此操作将覆盖原有的内容。</div>
          <div className="modal-actions">
            <button className="modal-button cancel" onClick={() => setShowConfirm(false)}>
              取消
            </button>
            <button className="modal-button confirm" onClick={confirmSave}>
              确认更新
            </button>
          </div>
        </div>
      </div>
    );
  }
  
  return (
    <div className="modal-overlay">
      <div className="modal-container">
        <div className="modal-title">编辑记忆卡片</div>
        <div className="modal-section">
          <div className="modal-label">维度：{DIMENSION_MAP[dimension]}</div>
          {content && (
            <>
              <div className="modal-label">当前内容：</div>
              <div className="current-content">
                {content}
              </div>
            </>
          )}
        </div>
        <div className="modal-section">
          <div className="modal-label">编辑内容：</div>
          <textarea
            className="edit-textarea"
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            placeholder="在此编辑记忆内容..."
            autoFocus
          />
        </div>
        <div className="modal-actions">
          <button className="modal-button cancel" onClick={onClose}>
            取消
          </button>
          <button className="modal-button confirm" onClick={handleSave}>
            保存
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 删除确认弹窗组件
 */
function DeleteConfirmModal({ dimension, onClose, onConfirm }) {
  return (
    <div className="modal-overlay">
      <div className="modal-container confirm-modal">
        <div className="modal-title">确认删除</div>
        <div className="confirm-message">
          此操作将清空<span style={{ color: '#E07070', fontWeight: '500' }}> {DIMENSION_MAP[dimension]} </span>
          维度的记忆内容
        </div>
        <div className="confirm-warning">删除后不可恢复，确认删除吗？</div>
        <div className="modal-actions">
          <button className="modal-button cancel" onClick={onClose}>
            取消
          </button>
          <button className="modal-button delete" onClick={onConfirm}>
            确认删除
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 新增记忆弹窗组件
 */
function AddMemoryModal({ onClose, onSubmit }) {
  const [content, setContent] = useState('');
  const [score, setScore] = useState(5);
  const [halflifeDays, setHalflifeDays] = useState(30);
  const [submitting, setSubmitting] = useState(false);
  
  const handleSubmit = async () => {
    if (!content.trim() || submitting) {
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit({
        content: content.trim(),
        score: parseInt(score, 10) || 5,
        halflife_days: parseInt(halflifeDays, 10) || 30
      });
    } finally {
      setSubmitting(false);
    }
  };
  
  return (
    <div className="modal-overlay">
      <div className="modal-container">
        <div className="modal-title">新增长期记忆</div>
        <div className="modal-section">
          <div className="modal-label">记忆内容：</div>
          <textarea
            className="edit-textarea"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="输入要记录的长期记忆内容..."
            autoFocus
            disabled={submitting}
          />
        </div>
        <div className="modal-section" style={{ display: 'flex', gap: '16px' }}>
          <div style={{ flex: 1 }}>
            <div className="modal-label">分数 (1-10)：</div>
            <input
              type="number"
              className="search-input"
              value={score}
              onChange={e => setScore(e.target.value)}
              min="1" max="10"
              disabled={submitting}
            />
          </div>
          <div style={{ flex: 1 }}>
            <div className="modal-label">半衰期 (天)：</div>
            <input
              type="number"
              className="search-input"
              value={halflifeDays}
              onChange={e => setHalflifeDays(e.target.value)}
              min="1"
              disabled={submitting}
            />
          </div>
        </div>
        <div className="modal-actions">
          <button className="modal-button cancel" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button className="modal-button confirm" onClick={handleSubmit} disabled={!content.trim() || submitting}>
            {submitting ? '提交中...' : '提交'}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 新增时效状态弹窗
 */
function AddTemporalStateModal({ onClose, onSubmit }) {
  const [stateContent, setStateContent] = useState('');
  const [actionRule, setActionRule] = useState('');
  const [expireAt, setExpireAt] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async () => {
    if (!stateContent.trim() || submitting) {
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit({
        state_content: stateContent.trim(),
        action_rule: actionRule.trim() || null,
        expire_at: expireAt.trim() || null
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay">
      <div className="modal-container">
        <div className="modal-title">新增时效状态</div>
        <div className="modal-section">
          <div className="modal-label">状态内容（state_content）</div>
          <textarea
            className="edit-textarea"
            style={{ minHeight: '100px' }}
            value={stateContent}
            onChange={(e) => setStateContent(e.target.value)}
            placeholder="描述当前时效状态…"
            autoFocus
            disabled={submitting}
          />
        </div>
        <div className="modal-section">
          <div className="modal-label">行为规则（action_rule，可选）</div>
          <textarea
            className="edit-textarea"
            style={{ minHeight: '80px' }}
            value={actionRule}
            onChange={(e) => setActionRule(e.target.value)}
            placeholder="可选：相关行为或动作规则"
            disabled={submitting}
          />
        </div>
        <div className="modal-section">
          <div className="modal-label">到期时间（expire_at，可选）</div>
          <input
            type="datetime-local"
            className="search-input"
            value={expireAt}
            onChange={(e) => setExpireAt(e.target.value)}
            disabled={submitting}
          />
        </div>
        <div className="modal-actions">
          <button className="modal-button cancel" type="button" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button
            className="modal-button confirm"
            type="button"
            onClick={handleSubmit}
            disabled={!stateContent.trim() || submitting}
          >
            {submitting ? '提交中…' : '提交'}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 时效状态列表项
 */
function TemporalStateItem({ row, addToast, onRefresh }) {
  const [showConfirm, setShowConfirm] = useState(false);
  const status = getTemporalDisplayStatus(row);
  const expireLabel = row.expire_at
    ? new Date(row.expire_at).toLocaleString('zh-CN')
    : '未设置';

  const runDelete = async () => {
    try {
      const response = await apiFetch(`/api/memory/temporal-states/${encodeURIComponent(row.id)}`, {
        method: 'DELETE'
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) {
        addToast(data.message || '停用失败', 'error');
        return;
      }
      addToast('已停用该时效状态', 'success');
      setShowConfirm(false);
      onRefresh();
    } catch (e) {
      console.error(e);
      addToast(e.message || '操作失败', 'error');
    }
  };

  if (showConfirm) {
    return (
      <div className="modal-overlay">
        <div className="modal-container confirm-modal">
          <div className="modal-title">确认停用</div>
          <div className="confirm-message">将该时效状态设为停用（is_active=0）？</div>
          <div className="modal-actions">
            <button className="modal-button cancel" type="button" onClick={() => setShowConfirm(false)}>
              取消
            </button>
            <button className="modal-button delete" type="button" onClick={runDelete}>
              确认停用
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="temporal-item">
      <div className="temporal-item-head">
        <span className={`temporal-status-pill ${status.className}`}>{status.label}</span>
        {/* 仅「生效中」可手动停用；已到期仍 is_active=1 时显示「已过期」，由日终 Step1 结算，不再提供软删除 */}
        {status.label === '生效中' && (
          <button className="delete-button" type="button" onClick={() => setShowConfirm(true)}>
            软删除
          </button>
        )}
      </div>
      <div className="temporal-content">{row.state_content || '（无内容）'}</div>
      {row.action_rule ? <div className="temporal-action-rule">规则：{row.action_rule}</div> : null}
      <div className="temporal-meta">
        <span>到期：{expireLabel}</span>
      </div>
    </div>
  );
}

/**
 * 记忆卡片组件
 */
function MemoryCard({ dimension, content, updatedAt, onEdit, onDelete }) {
  const [viewOpen, setViewOpen] = useState(false);
  const [showViewFull, setShowViewFull] = useState(false);
  const cardContentRef = useRef(null);
  const isEmpty = !content || content.trim() === '';
  const displayContent = isEmpty ? '暂无内容，点击编辑添加' : content;
  const displayTime = updatedAt ? new Date(updatedAt).toLocaleDateString('zh-CN') : '未记录';

  useLayoutEffect(() => {
    if (isEmpty) {
      setShowViewFull(false);
      return;
    }
    const el = cardContentRef.current;
    if (!el) {
      setShowViewFull(false);
      return;
    }
    const checkOverflow = () => {
      const node = cardContentRef.current;
      if (!node) return;
      setShowViewFull(isMemoryCardContentTruncated(node, displayContent));
    };
    checkOverflow();
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(checkOverflow) : null;
    if (ro) ro.observe(el);
    window.addEventListener('resize', checkOverflow);
    return () => {
      if (ro) ro.disconnect();
      window.removeEventListener('resize', checkOverflow);
    };
  }, [content, isEmpty]);

  return (
    <div
      className={`memory-card ${isEmpty ? 'empty' : ''} ${showViewFull ? 'memory-card--has-view-link' : ''}`}
    >
      {viewOpen && (
        <ViewMemoryCardModal
          dimension={dimension}
          content={content}
          onClose={() => setViewOpen(false)}
        />
      )}
      <div className="memory-card-header">
        <div className="card-title">{DIMENSION_MAP[dimension]}</div>
        <div className="card-actions">
          <button className="action-button edit-button" onClick={() => onEdit(dimension)}>
            编辑
          </button>
          {!isEmpty && (
            <button className="action-button delete-card-button" onClick={() => onDelete(dimension)}>
              删除
            </button>
          )}
        </div>
      </div>
      <div
        ref={cardContentRef}
        className={`card-content ${isEmpty ? 'empty' : ''}`}
      >
        {displayContent}
      </div>
      {showViewFull && (
        <button
          type="button"
          className="card-view-full-link"
          onClick={() => setViewOpen(true)}
        >
          查看全文
        </button>
      )}
      <div className="card-footer">
        <div className="card-timestamp">更新: {displayTime}</div>
      </div>
    </div>
  );
}

/**
 * 长期记忆项组件
 */
function LongTermMemoryItem({ memory, onDelete, gcExemptHitsThreshold }) {
  const [showConfirm, setShowConfirm] = useState(false);
  
  const handleDelete = () => {
    setShowConfirm(true);
  };
  
  const confirmDelete = () => {
    onDelete(memory.id);
    setShowConfirm(false);
  };

  const hitsNum = memory.hits != null ? Number(memory.hits) : null;
  const isGcExempt = hitsNum != null && gcExemptHitsThreshold != null && hitsNum >= gcExemptHitsThreshold;
  const arousalDisplay = memory.arousal != null ? Number(memory.arousal).toFixed(2) : null;
  
  if (showConfirm) {
    return (
      <div className="modal-overlay">
        <div className="modal-container confirm-modal">
          <div className="modal-title">确认删除</div>
          <div className="confirm-message">确认删除这条长期记忆吗？</div>
          <div className="confirm-warning">删除后不可恢复。</div>
          <div className="modal-actions">
            <button className="modal-button cancel" onClick={() => setShowConfirm(false)}>
              取消
            </button>
            <button className="modal-button delete" onClick={confirmDelete}>
              确认删除
            </button>
          </div>
        </div>
      </div>
    );
  }
  
  return (
    <div className="memory-item">
      <div className="memory-summary">
        {memory.content}
        {isGcExempt && (
          <span className="gc-exempt-badge" title={`引用次数 ${hitsNum} ≥ 阈值 ${gcExemptHitsThreshold}，已豁免自动删除`}>
            🔒 免删
          </span>
        )}
      </div>
      {memory.is_orphan ? (
        <div className="memory-orphan-hint">未同步到向量库（is_orphan）</div>
      ) : null}
      <div className="memory-detail-row memory-detail-row--chroma-stats">
        <span className="memory-meta-chip">
          引用次数 hits：{hitsNum != null ? hitsNum : '—'}
        </span>
        <span className="memory-meta-chip">
          半衰期 halflife_days：{memory.halflife_days != null ? memory.halflife_days : '—'}
        </span>
        {arousalDisplay != null && (
          <span className="memory-meta-chip">
            情绪强度 arousal：{arousalDisplay}
          </span>
        )}
      </div>
      <div className="memory-detail-row memory-detail-row-single">
        最近访问 last_access_ts：
        {formatLastAccessTs(memory.last_access_ts)}
      </div>
      <div className="memory-meta">
        <span>归档: {new Date(memory.created_at).toLocaleDateString('zh-CN')}</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <span className="score-badge">★ {memory.score || '0'}分</span>
          <button className="delete-button" onClick={handleDelete}>
            删除
          </button>
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
    <div className="memory-container">
      <div className="memory-tabs-scroll" aria-label="记忆页签切换">
        <div className="memory-tabs skeleton-tabs">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="skeleton-line short" style={{ width: '88px', height: '36px' }} />
          ))}
        </div>
      </div>
      <div className="memory-content-scroll-area">
        {/* 记忆卡片骨架屏 */}
        <>
          <div className="memory-tab-header">
            <h2 className="memory-tab-header__title">
              <span className="memory-tab-header__emoji" aria-hidden="true">{'\u00a0'}</span>
              <span className="memory-tab-header__title-text">
                <span className="skeleton-line" style={{ width: '140px', height: '20px', display: 'inline-block', verticalAlign: 'middle' }} />
              </span>
            </h2>
          </div>
          <div className="skeleton-card-grid">
            {[...Array(7)].map((_, i) => (
              <div key={i} className="skeleton-card">
                <div className="skeleton-line short"></div>
                <div className="skeleton-line medium"></div>
                <div className="skeleton-line" style={{ width: '40%' }}></div>
              </div>
            ))}
          </div>
        </>

        {/* 长期记忆骨架屏 */}
        <>
          <div className="memory-tab-header">
            <h2 className="memory-tab-header__title">
              <span className="memory-tab-header__emoji" aria-hidden="true">{'\u00a0'}</span>
              <span className="memory-tab-header__title-text">
                <span className="skeleton-line" style={{ width: '140px', height: '20px', display: 'inline-block', verticalAlign: 'middle' }} />
              </span>
            </h2>
            <div className="memory-tab-header__actions">
              <div className="skeleton-line" style={{ width: '100px', height: '36px', borderRadius: 10 }} />
            </div>
          </div>
          <div className="longterm-header">
            <div className="skeleton-line" style={{ width: '100%' }}></div>
            <div className="skeleton-line" style={{ width: '80px' }}></div>
          </div>
          <div className="memory-list">
            {[...Array(3)].map((_, i) => (
              <div key={i} className="skeleton-card">
                <div className="skeleton-line medium"></div>
                <div className="skeleton-line short"></div>
              </div>
            ))}
          </div>
        </>
      </div>
    </div>
  );
}

/**
 * 主 Memory 组件
 */
function Memory() {
  // 状态管理
  const [loading, setLoading] = useState(true);
  const [memoryCards, setMemoryCards] = useState({});
  const [longTermMemories, setLongTermMemories] = useState([]);
  const [toasts, setToasts] = useState([]);
  const [gcExemptHitsThreshold, setGcExemptHitsThreshold] = useState(null);
  
  // 弹窗状态
  const [showEditModal, setShowEditModal] = useState(false);
  const [editingDimension, setEditingDimension] = useState(null);
  const [editingContent, setEditingContent] = useState('');
  
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [deletingDimension, setDeletingDimension] = useState(null);
  
  const [showAddModal, setShowAddModal] = useState(false);
  const [showTemporalAddModal, setShowTemporalAddModal] = useState(false);
  
  const [activeTab, setActiveTab] = useState('cards');
  const [temporalStates, setTemporalStates] = useState([]);
  const [temporalLoading, setTemporalLoading] = useState(false);
  const [timelineEvents, setTimelineEvents] = useState([]);
  const [timelineLoading, setTimelineLoading] = useState(false);
  
  // 搜索和分页
  const [searchKeyword, setSearchKeyword] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const searchTimeoutRef = useRef(null);
  
  // 添加 Toast
  const addToast = useCallback((message, type = 'info') => {
    const id = Date.now();
    setToasts(prev => [...prev, { id, message, type }]);
  }, []);
  
  // 移除 Toast
  const removeToast = useCallback((id) => {
    setToasts(prev => prev.filter(toast => toast.id !== id));
  }, []);
  
  // 加载记忆卡片数据
  const loadMemoryCards = useCallback(async () => {
    try {
      const response = await apiFetch('/api/memory/cards');
      if (!response.ok) {
        throw new Error('获取记忆卡片失败');
      }
      const data = await response.json();
      
      if (data.success) {
        // 将卡片按维度分组
        const cardsByDimension = {};
        const dimensions = Object.keys(DIMENSION_MAP);
        
        // 初始化所有维度
        dimensions.forEach(dim => {
          cardsByDimension[dim] = {
            id: null,
            content: '',
            updated_at: null
          };
        });
        
        // 填充已有卡片（同一 dimension 可能有多条：不同 user；保留 updated_at 最新的一条，避免后遍历覆盖成旧数据）
        if (data.data && Array.isArray(data.data)) {
          const cardTime = (c) => {
            const raw = c.updated_at || c.created_at;
            if (!raw) return 0;
            const n = new Date(raw).getTime();
            return Number.isNaN(n) ? 0 : n;
          };
          data.data.forEach(card => {
            if (!card.dimension || !DIMENSION_MAP[card.dimension]) return;
            const next = {
              id: card.id || null,
              content: card.content || '',
              updated_at: card.updated_at || card.created_at
            };
            const slot = cardsByDimension[card.dimension];
            const slotT = cardTime(slot);
            if (cardTime(card) >= slotT) {
              cardsByDimension[card.dimension] = next;
            }
          });
        }
        
        setMemoryCards(cardsByDimension);
      }
    } catch (error) {
      console.error('加载记忆卡片失败:', error);
      // 初始化空卡片
      const emptyCards = {};
      Object.keys(DIMENSION_MAP).forEach(dim => {
        emptyCards[dim] = { id: null, content: '', updated_at: null };
      });
      setMemoryCards(emptyCards);
      addToast('加载记忆卡片失败：' + error.message, 'error');
    }
  }, [addToast]);
  
  // 加载长期记忆数据
  const loadLongTermMemories = useCallback(async (keyword = '', page = 1) => {
    try {
      const params = new URLSearchParams({
        keyword,
        page: page.toString(),
        page_size: '20'
      });
      
      const response = await apiFetch(`/api/memory/longterm?${params}`);
      if (!response.ok) {
        throw new Error('获取长期记忆失败');
      }
      const data = await response.json();
      
      if (data.success) {
        setLongTermMemories(data.data?.items || []);
        setTotalPages(data.data?.total_pages || 1);
        setCurrentPage(data.data?.current_page || 1);
      }
    } catch (error) {
      console.error('加载长期记忆失败:', error);
      setLongTermMemories([]);
      setTotalPages(1);
      setCurrentPage(1);
      addToast('加载长期记忆失败：' + error.message, 'error');
    }
  }, [addToast]);
  
  const loadTemporalStates = useCallback(async () => {
    setTemporalLoading(true);
    try {
      const response = await apiFetch('/api/memory/temporal-states');
      if (!response.ok) {
        throw new Error('获取时效状态失败');
      }
      const data = await response.json();
      if (data.success) {
        setTemporalStates(Array.isArray(data.data) ? data.data : []);
      } else {
        throw new Error(data.message || '获取失败');
      }
    } catch (error) {
      console.error('加载时效状态失败:', error);
      setTemporalStates([]);
      addToast(error.message || '加载时效状态失败', 'error');
    } finally {
      setTemporalLoading(false);
    }
  }, [addToast]);
  
  const loadRelationshipTimeline = useCallback(async () => {
    setTimelineLoading(true);
    try {
      const response = await apiFetch('/api/memory/relationship-timeline');
      if (!response.ok) {
        throw new Error('获取关系时间线失败');
      }
      const data = await response.json();
      if (data.success) {
        setTimelineEvents(Array.isArray(data.data) ? data.data : []);
      } else {
        throw new Error(data.message || '获取失败');
      }
    } catch (error) {
      console.error('加载关系时间线失败:', error);
      setTimelineEvents([]);
      addToast(error.message || '加载关系时间线失败', 'error');
    } finally {
      setTimelineLoading(false);
    }
  }, [addToast]);
  
  useEffect(() => {
    if (activeTab === 'temporal') {
      loadTemporalStates();
    } else if (activeTab === 'timeline') {
      loadRelationshipTimeline();
    }
  }, [activeTab, loadTemporalStates, loadRelationshipTimeline]);
  
  // 初始化加载数据
  useEffect(() => {
    const loadAllData = async () => {
      setLoading(true);
      await Promise.all([
        loadMemoryCards(),
        loadLongTermMemories(),
        apiFetch('/api/config/config')
          .then(r => r.json())
          .then(d => {
            if (d.success && d.data) {
              const val = Number(d.data.gc_exempt_hits_threshold);
              if (!Number.isNaN(val)) setGcExemptHitsThreshold(val);
            }
          })
          .catch(() => {}),
      ]);
      setLoading(false);
    };
    
    loadAllData();
  }, [loadMemoryCards, loadLongTermMemories]);
  
  // 搜索防抖（仅在长期记忆 Tab 时触发请求）
  useEffect(() => {
    if (activeTab !== 'longterm') {
      return undefined;
    }
    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current);
    }
    
    searchTimeoutRef.current = setTimeout(() => {
      loadLongTermMemories(searchKeyword, 1);
    }, 500);
    
    return () => {
      if (searchTimeoutRef.current) {
        clearTimeout(searchTimeoutRef.current);
      }
    };
  }, [searchKeyword, loadLongTermMemories, activeTab]);
  
  // 处理编辑记忆卡片
  const handleEditCard = (dimension) => {
    setEditingDimension(dimension);
    setEditingContent(memoryCards[dimension]?.content || '');
    setShowEditModal(true);
  };
  
  const handleSaveCard = async (dimension, content) => {
    try {
      const card = memoryCards[dimension];
      const cardId = card?.id;
      
      if (cardId) {
        // 更新现有卡片
        const response = await apiFetch(`/api/memory/cards/${cardId}`, {
          method: 'PUT',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            content,
            dimension
          })
        });
        
        if (!response.ok) {
          throw new Error('更新卡片失败');
        }
        
        addToast('记忆卡片更新成功', 'success');
      } else {
        // 创建新卡片 - 使用POST /api/memory/cards
        const response = await apiFetch('/api/memory/cards', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            user_id: 'default_user',
            character_id: 'default_character',
            dimension: dimension,
            content: content
          })
        });
        
        if (!response.ok) {
          throw new Error('创建卡片失败');
        }
        
        const data = await response.json();
        if (data.success) {
          addToast('记忆卡片创建成功', 'success');
          // 回写服务器分配的 card_id，避免下次编辑时重复创建
          const newCardId = data.data?.card_id || null;
          setMemoryCards(prev => ({
            ...prev,
            [dimension]: {
              id: newCardId,
              content,
              updated_at: new Date().toISOString()
            }
          }));
          setShowEditModal(false);
          return;
        } else {
          throw new Error(data.message || '创建卡片失败');
        }
      }
      
      // 更新现有卡片的本地状态
      setMemoryCards(prev => ({
        ...prev,
        [dimension]: {
          ...prev[dimension],
          content,
          updated_at: new Date().toISOString()
        }
      }));
      
      setShowEditModal(false);
    } catch (error) {
      console.error('保存记忆卡片失败:', error);
      addToast('操作失败，请重试', 'error');
    }
  };
  
  // 处理删除记忆卡片
  const handleDeleteCard = (dimension) => {
    setDeletingDimension(dimension);
    setShowDeleteModal(true);
  };
  
  const confirmDeleteCard = async () => {
    try {
      const card = memoryCards[deletingDimension];
      const cardId = card?.id;
      
      if (cardId) {
        // 调用删除API
        const response = await apiFetch(`/api/memory/cards/${cardId}`, {
          method: 'DELETE'
        });
        
        if (!response.ok) {
          const errorData = await response.json();
          throw new Error(errorData.message || '删除卡片失败');
        }
        
        const data = await response.json();
        if (data.success) {
          addToast('记忆卡片已清空', 'success');
        } else {
          throw new Error(data.message || '删除卡片失败');
        }
      } else {
        // 没有cardId，说明卡片不存在或为空，直接更新本地状态
        addToast('记忆卡片已清空', 'success');
      }
      
      // 更新本地状态
      setMemoryCards(prev => ({
        ...prev,
        [deletingDimension]: {
          ...prev[deletingDimension],
          id: null,
          content: '',
          updated_at: null
        }
      }));
      
      setShowDeleteModal(false);
    } catch (error) {
      console.error('删除记忆卡片失败:', error);
      addToast(`操作失败：${error.message}`, 'error');
    }
  };
  
  // 处理新增长期记忆
  const handleAddMemory = async (payload) => {
    try {
      // 调用新增API
      const response = await apiFetch('/api/memory/longterm', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload)
      });
      
      if (!response.ok) {
        throw new Error('新增记忆失败');
      }
      
      addToast('长期记忆添加成功', 'success');
      setShowAddModal(false);
      
      // 重新加载数据
      loadLongTermMemories(searchKeyword, currentPage);
    } catch (error) {
      console.error('新增长期记忆失败:', error);
      addToast('操作失败，请重试', 'error');
    }
  };
  
  // 处理删除长期记忆
  const handleDeleteMemory = async (memoryId) => {
    try {
      // 调用删除API
      const response = await apiFetch(`/api/memory/longterm/${memoryId}`, {
        method: 'DELETE'
      });
      
      if (!response.ok) {
        throw new Error('删除记忆失败');
      }
      
      addToast('长期记忆删除成功', 'success');
      
      // 重新加载数据
      loadLongTermMemories(searchKeyword, currentPage);
    } catch (error) {
      console.error('删除长期记忆失败:', error);
      addToast('操作失败，请重试', 'error');
    }
  };
  
  // 处理分页
  const handlePrevPage = () => {
    if (currentPage > 1) {
      const newPage = currentPage - 1;
      setCurrentPage(newPage);
      loadLongTermMemories(searchKeyword, newPage);
    }
  };
  
  const handleNextPage = () => {
    if (currentPage < totalPages) {
      const newPage = currentPage + 1;
      setCurrentPage(newPage);
      loadLongTermMemories(searchKeyword, newPage);
    }
  };

  const handleFirstPage = () => {
    if (currentPage <= 1) {
      return;
    }
    setCurrentPage(1);
    loadLongTermMemories(searchKeyword, 1);
  };

  const handleLastPage = () => {
    if (currentPage >= totalPages) {
      return;
    }
    setCurrentPage(totalPages);
    loadLongTermMemories(searchKeyword, totalPages);
  };
  
  const handleAddTemporalState = async (payload) => {
    try {
      const response = await apiFetch('/api/memory/temporal-states', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) {
        throw new Error(data.message || '创建时效状态失败');
      }
      addToast('时效状态已创建', 'success');
      setShowTemporalAddModal(false);
      loadTemporalStates();
    } catch (error) {
      console.error('新增时效状态失败:', error);
      addToast(error.message || '操作失败', 'error');
    }
  };
  
  if (loading) {
    return <SkeletonLoader />;
  }
  
  return (
    <div className="memory-container">
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
      
      {/* 编辑弹窗 */}
      {showEditModal && (
        <EditModal
          dimension={editingDimension}
          content={editingContent}
          onClose={() => setShowEditModal(false)}
          onSave={handleSaveCard}
        />
      )}
      
      {/* 删除确认弹窗 */}
      {showDeleteModal && (
        <DeleteConfirmModal
          dimension={deletingDimension}
          onClose={() => setShowDeleteModal(false)}
          onConfirm={confirmDeleteCard}
        />
      )}
      
      {/* 新增记忆弹窗 */}
      {showAddModal && (
        <AddMemoryModal
          onClose={() => setShowAddModal(false)}
          onSubmit={handleAddMemory}
        />
      )}
      
      {showTemporalAddModal && (
        <AddTemporalStateModal
          onClose={() => setShowTemporalAddModal(false)}
          onSubmit={handleAddTemporalState}
        />
      )}
      
      <div className="memory-tabs-scroll" aria-label="记忆页签切换">
        <div className="memory-tabs" role="tablist">
          {MEMORY_TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={activeTab === tab.id}
              className={`memory-tab ${activeTab === tab.id ? 'active' : ''}`}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      <div className="memory-content-scroll-area">
      {activeTab === 'cards' && (
        <>
          <div className="memory-tab-header">
            <h2 className="memory-tab-header__title">
              <span className="memory-tab-header__emoji" aria-hidden="true">📓</span>
              <span className="memory-tab-header__title-text">记忆卡片</span>
            </h2>
          </div>
          <div className="memory-cards-grid">
            {Object.keys(DIMENSION_MAP).map((dimension) => (
              <MemoryCard
                key={dimension}
                dimension={dimension}
                content={memoryCards[dimension]?.content}
                updatedAt={memoryCards[dimension]?.updated_at}
                onEdit={handleEditCard}
                onDelete={handleDeleteCard}
              />
            ))}
          </div>
        </>
      )}
      
      {activeTab === 'longterm' && (
        <>
          <div className="memory-tab-header">
            <h2 className="memory-tab-header__title">
              <span className="memory-tab-header__emoji" aria-hidden="true">📚</span>
              <span className="memory-tab-header__title-text">长期记忆库</span>
            </h2>
            <div className="memory-tab-header__actions">
              <button className="add-button" type="button" onClick={() => setShowAddModal(true)}>
                + 手动新增
              </button>
            </div>
          </div>

          <div className="longterm-header">
            <input
              type="text"
              className="search-input"
              placeholder="搜索长期记忆..."
              value={searchKeyword}
              onChange={(e) => setSearchKeyword(e.target.value)}
            />
          </div>
          
          <div className="memory-list">
            {longTermMemories.length === 0 ? (
              <div className="empty-state">
                <div className="empty-state-icon">📝</div>
                <div className="empty-state-text">暂无长期记忆记录</div>
              </div>
            ) : (
              longTermMemories.map((memory) => (
                <LongTermMemoryItem
                  key={memory.id}
                  memory={memory}
                  onDelete={handleDeleteMemory}
                  gcExemptHitsThreshold={gcExemptHitsThreshold}
                />
              ))
            )}
          </div>
        </>
      )}

      {activeTab === 'temporal' && (
        <>
          <div className="memory-tab-header">
            <h2 className="memory-tab-header__title">
              <span className="memory-tab-header__emoji" aria-hidden="true">⏱</span>
              <span className="memory-tab-header__title-text">时效状态</span>
            </h2>
            <div className="memory-tab-header__actions">
              <button className="add-button" type="button" onClick={() => setShowTemporalAddModal(true)}>
                + 新增
              </button>
            </div>
          </div>
          {temporalLoading ? (
            <div className="tab-loading">加载中…</div>
          ) : temporalStates.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-icon">⏱</div>
              <div className="empty-state-text">暂无时效状态</div>
            </div>
          ) : (
            <div className="memory-list">
              {temporalStates.map((row) => (
                <TemporalStateItem
                  key={row.id}
                  row={row}
                  addToast={addToast}
                  onRefresh={loadTemporalStates}
                />
              ))}
            </div>
          )}
        </>
      )}

      {activeTab === 'timeline' && (
        <>
          <div className="memory-tab-header">
            <h2 className="memory-tab-header__title">
              <span className="memory-tab-header__emoji" aria-hidden="true">💞</span>
              <span className="memory-tab-header__title-text">关系时间线</span>
            </h2>
          </div>
          {timelineLoading ? (
            <div className="tab-loading">加载中…</div>
          ) : timelineEvents.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-icon">📅</div>
              <div className="empty-state-text">暂无关系时间线记录</div>
            </div>
          ) : (
            <div className="timeline-list">
              {timelineEvents.map((ev) => (
                <div key={ev.id} className="timeline-item">
                  <div className="timeline-item-head">
                    <span className="timeline-time">
                      {ev.created_at ? new Date(ev.created_at).toLocaleString('zh-CN') : '—'}
                    </span>
                    <span className="timeline-type-badge">
                      {EVENT_TYPE_MAP[ev.event_type] || ev.event_type}
                    </span>
                  </div>
                  <div className="timeline-content-text">{ev.content || '—'}</div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
      </div>

      {activeTab === 'longterm' && longTermMemories.length > 0 && (
        <div className="pagination pagination--outside">
          <button
            className="pagination-button"
            type="button"
            onClick={handleFirstPage}
            disabled={currentPage <= 1}
          >
            首页
          </button>
          <button
            className="pagination-button"
            type="button"
            onClick={handlePrevPage}
            disabled={currentPage <= 1}
          >
            上页
          </button>
          <div
            className="pagination-info pagination-info--stacked"
            role="status"
            aria-live="polite"
          >
            <span className="pagination-info-line">第 {currentPage} 页</span>
            <span className="pagination-info-line">共 {totalPages} 页</span>
          </div>
          <button
            className="pagination-button"
            type="button"
            onClick={handleNextPage}
            disabled={currentPage >= totalPages}
          >
            下页
          </button>
          <button
            className="pagination-button"
            type="button"
            onClick={handleLastPage}
            disabled={currentPage >= totalPages}
          >
            尾页
          </button>
        </div>
      )}
    </div>
  );
}

export default Memory;