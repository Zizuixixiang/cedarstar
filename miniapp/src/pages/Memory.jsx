/**
 * 记忆管理页面 - 完整实现
 * 管理 AI 助手的记忆卡片和长期记忆库
 */

import { useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { apiFetch } from '../apiBase';
import {
  Calendar,
  Database,
  BookOpen,
  Timer,
  HeartHandshake,
  FileText,
  Lock,
  Star,
  ScrollText,
} from 'lucide-react';
import './../styles/memory.css';
import { useHorizontalDragScroll } from '../useHorizontalDragScroll';

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

const SUMMARY_TYPE_LABELS = {
  daily: '日总 daily',
  daily_event: '日终片段 daily_event',
  manual: '手动 manual',
  app_event: 'APP端 app_event',
  state_archive: '状态归档 state_archive',
};

const LONGTERM_PAGE_SIZE = 20;
const SUMMARIES_PAGE_SIZE = 20;

const SUMMARY_KIND_LABELS = {
  chunk: 'chunk',
  daily: 'daily',
};

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

function formatShanghaiDateTime(value) {
  if (value == null || value === '') return '—';
  const d = parseShanghaiDateTime(value);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', { timeZone: SHANGHAI_TIME_ZONE });
}

function formatShanghaiDate(value) {
  if (value == null || value === '') return '—';
  const d = parseShanghaiDateTime(value);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString('zh-CN', { timeZone: SHANGHAI_TIME_ZONE });
}

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
  { id: 'summaries', label: '摘要' },
  { id: 'temporal', label: '时效状态' },
  { id: 'timeline', label: '关系时间线' },
  { id: 'cards', label: '记忆卡片' },
  { id: 'longterm', label: '长期记忆' },
];

function getTemporalDisplayStatus(row) {
  const activeFlag = Number(row.is_active) === 1;
  let beforeExpire = true;
  if (row.expire_at) {
    const t = parseShanghaiDateTime(row.expire_at).getTime();
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
  return formatShanghaiDateTime(n * 1000);
}

function formatLongtermTitleLine(memory) {
  if (memory.date) {
    return memory.date;
  }
  return formatLastAccessTs(memory.last_access_ts);
}

/** 将 date input 规范为 YYYY-MM-DD（兼容部分环境带 / ） */
function normalizeSummaryDateInput(value) {
  const t = String(value || '').trim();
  if (!t) return '';
  const iso = t.replace(/\//g, '-');
  return /^\d{4}-\d{2}-\d{2}$/.test(iso) ? iso : t;
}

function formatSummaryRecordTitle(row) {
  if (row.source_date) {
    try {
      const d = parseShanghaiDateTime(row.source_date);
      if (!Number.isNaN(d.getTime())) {
        return formatShanghaiDate(d);
      }
    } catch {
      /* fallthrough */
    }
    return String(row.source_date).slice(0, 10);
  }
  if (row.created_at) {
    try {
      return formatShanghaiDateTime(row.created_at);
    } catch {
      return '—';
    }
  }
  return '—';
}

function ContextTraceNote({ trace }) {
  if (!trace?.built_at) {
    return (
      <div className="context-trace-note">
        暂无本轮 context 记录
      </div>
    );
  }
  const summaryCount =
    (trace.daily_summary_ids?.length || 0) +
    (trace.chunk_summary_ids?.length || 0) +
    (trace.archived_daily_summary_ids?.length || 0);
  return (
    <div
      className="context-trace-note"
      title={trace.user_message_preview ? `用户消息：${trace.user_message_preview}` : undefined}
    >
      最近构建：{formatShanghaiDateTime(trace.built_at)}
      {trace.session_id ? ` · session: ${trace.session_id}` : ''}
      {' · '}
      摘要 {summaryCount} 条 · 长期记忆 {trace.longterm_doc_ids?.length || 0} 条
    </div>
  );
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
 * 长期记忆：只读全文弹窗
 */
function ViewLongtermMemoryModal({ title, content, onClose }) {
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

  const heading = title || '长期记忆';

  return createPortal(
    <div className="memory-view-overlay" onClick={onClose} role="presentation">
      <div
        className="memory-view-sheet"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="longterm-view-title"
      >
        <header className="memory-view-sheet-header">
          <div className="memory-view-sheet-headings">
            <h2 id="longterm-view-title" className="memory-view-sheet-title">
              {heading}
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
 * 摘要表：编辑正文（与 History 单条编辑一致：textarea + 保存）
 */
function SummaryEditModal({ row, onClose, onSaved, addToast }) {
  const [text, setText] = useState(row?.summary_text || '');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setText(row?.summary_text || '');
  }, [row]);

  const handleSave = async () => {
    if (!row?.id || busy) return;
    setBusy(true);
    try {
      const res = await apiFetch(`/api/memory/summaries/${row.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ summary_text: text }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.success) {
        throw new Error(data.message || '保存失败');
      }
      onSaved?.();
      onClose();
    } catch (e) {
      console.error(e);
      addToast?.(e.message || '保存失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={() => !busy && onClose()}>
      <div
        className="modal-container summary-edit-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="summary-edit-title"
      >
        <div className="modal-title" id="summary-edit-title">
          编辑摘要
        </div>
        <div className="modal-section">
          <div className="modal-label">正文</div>
          <textarea
            className="edit-textarea summary-edit-textarea"
            rows={12}
            value={text}
            onChange={(e) => setText(e.target.value)}
            disabled={busy}
            autoFocus
          />
        </div>
        <div className="modal-actions">
          <button type="button" className="modal-button cancel" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="button" className="modal-button confirm" onClick={handleSave} disabled={busy}>
            {busy ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 摘要表：单条卡片（长期记忆列表风格 + Settings 行内删除确认）
 */
function SummaryRecordItem({
  row,
  isInCurrentContext,
  contextTraceLabel,
  confirmDeleteId,
  onBeginDelete,
  onCancelDelete,
  onDeleteConfirm,
  onEdit,
  onToggleStar,
}) {
  const [viewOpen, setViewOpen] = useState(false);
  const [showViewFull, setShowViewFull] = useState(false);
  const bodyRef = useRef(null);
  const bodyText = row.summary_text || '';
  const titleLine = formatSummaryRecordTitle(row);
  const typeLabel = SUMMARY_KIND_LABELS[row.summary_type] || row.summary_type || '—';
  const isChunk = row.summary_type === 'chunk';
  const hasDailySummary = Boolean(row.has_daily_summary);
  const isStarred = Boolean(row.is_starred);

  useLayoutEffect(() => {
    if (!bodyText.trim()) {
      setShowViewFull(false);
      return;
    }
    const el = bodyRef.current;
    if (!el) {
      setShowViewFull(false);
      return;
    }
    const check = () => setShowViewFull(isMemoryCardContentTruncated(el, bodyText));
    check();
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(check) : null;
    if (ro) ro.observe(el);
    window.addEventListener('resize', check);
    return () => {
      if (ro) ro.disconnect();
      window.removeEventListener('resize', check);
    };
  }, [bodyText]);

  return (
    <div
      className={`memory-item summary-record-item ${showViewFull ? 'memory-item--has-view-link' : ''} ${
        isInCurrentContext ? 'memory-item--context' : ''
      }`}
    >
      {viewOpen && (
        <ViewLongtermMemoryModal title={titleLine} content={bodyText} onClose={() => setViewOpen(false)} />
      )}
      <div className="memory-item-head memory-item-head--longterm">
        <div className="memory-item-head__meta">
          <span className="memory-longterm-title">{titleLine}</span>
          <span className="memory-longterm-type-badge timeline-type-badge" title="summary_type">
            {typeLabel}
          </span>
          {isChunk ? (
            <span
              className={`memory-longterm-type-badge timeline-type-badge summary-daily-status-badge ${
                hasDailySummary ? 'summary-daily-status-badge--done' : 'summary-daily-status-badge--pending'
              }`}
              title="该 chunk 所属日期是否已经生成 daily 日摘要"
            >
              {hasDailySummary ? '已日摘要' : '未日摘要'}
            </span>
          ) : null}
          {isInCurrentContext ? (
            <span
              className="memory-context-badge timeline-type-badge"
              title={contextTraceLabel || '最近一轮 context 实际注入了这条摘要'}
            >
              本轮
            </span>
          ) : null}
        </div>
      </div>
      <div className="memory-longterm-body-wrap">
        <div ref={bodyRef} className="memory-summary memory-summary--longterm-only">
          {bodyText}
        </div>
      </div>
      <div className="summary-record-actions">
        {confirmDeleteId === row.id ? (
          <span className="summary-delete-confirm-wrap">
            <span className="summary-delete-confirm-text">确认删除？</span>
            <button type="button" className="summary-delete-confirm-btn" onClick={() => onDeleteConfirm(row.id)}>
              确认
            </button>
            <button type="button" className="summary-delete-cancel-btn" onClick={onCancelDelete}>
              取消
            </button>
          </span>
        ) : (
          <>
            {showViewFull ? (
              <button type="button" className="card-view-full-link" onClick={() => setViewOpen(true)}>
                查看全文
              </button>
            ) : null}
            {isChunk ? (
              <button
                type="button"
                className={`summary-star-button ${isStarred ? 'summary-star-button--active' : ''}`}
                onClick={() => onToggleStar(row)}
                title={isStarred ? '取消收藏' : '收藏 chunk'}
                aria-label={isStarred ? '取消收藏' : '收藏 chunk'}
              >
                <Star size={13} strokeWidth={2} fill={isStarred ? 'currentColor' : 'none'} aria-hidden />
              </button>
            ) : null}
            <button type="button" className="action-button edit-button" onClick={() => onEdit(row)}>
              编辑
            </button>
            <button type="button" className="delete-button" onClick={() => onBeginDelete(row.id)}>
              删除
            </button>
          </>
        )}
      </div>
      <div className="summary-record-meta-footer">
        <div
          className="summary-record-meta-line"
          title={
            row.session_id
              ? `id: ${row.id} · session: ${row.session_id}`
              : `id: ${row.id}`
          }
        >
          <span className="summary-record-meta-id">id: {row.id}</span>
          {row.session_id ? (
            <>
              <span className="summary-record-meta-dot"> · </span>
              <span className="summary-record-meta-session">session: {row.session_id}</span>
            </>
          ) : null}
        </div>
      </div>
    </div>
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
 * 编辑时效状态弹窗
 */
function EditTemporalStateModal({ row, onClose, onSaved, addToast }) {
  const [stateContent, setStateContent] = useState(row?.state_content || '');
  const [actionRule, setActionRule] = useState(row?.action_rule || '');
  const [expireAt, setExpireAt] = useState(() => {
    if (!row?.expire_at) return '';
    try {
      const d = parseShanghaiDateTime(row.expire_at);
      if (Number.isNaN(d.getTime())) return '';
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    } catch { return ''; }
  });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setStateContent(row?.state_content || '');
    setActionRule(row?.action_rule || '');
    if (row?.expire_at) {
      try {
        const d = parseShanghaiDateTime(row.expire_at);
        if (!Number.isNaN(d.getTime())) {
          const pad = (n) => String(n).padStart(2, '0');
          setExpireAt(`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`);
          return;
        }
      } catch { /* fallthrough */ }
    }
    setExpireAt('');
  }, [row]);

  const handleSave = async () => {
    if (!row?.id || busy) return;
    setBusy(true);
    try {
      const payload = {};
      if (stateContent !== (row.state_content || '')) {
        payload.state_content = stateContent;
      }
      if (actionRule !== (row.action_rule || '')) {
        payload.action_rule = actionRule || null;
      }
      const origExpire = (() => {
        if (!row.expire_at) return '';
        try {
          const d = parseShanghaiDateTime(row.expire_at);
          if (Number.isNaN(d.getTime())) return '';
          const pad = (n) => String(n).padStart(2, '0');
          return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
        } catch { return ''; }
      })();
      if (expireAt !== origExpire) {
        payload.expire_at = expireAt || '';
      }
      if (Object.keys(payload).length === 0) {
        onClose();
        return;
      }
      const res = await apiFetch(`/api/memory/temporal-states/${encodeURIComponent(row.id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.success) {
        throw new Error(data.message || '保存失败');
      }
      onSaved?.();
      onClose();
    } catch (e) {
      console.error(e);
      addToast?.(e.message || '保存失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={() => !busy && onClose()}>
      <div
        className="modal-container"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="edit-temporal-title"
      >
        <div className="modal-title" id="edit-temporal-title">编辑时效状态</div>
        <div className="modal-section">
          <div className="modal-label">状态内容（state_content）</div>
          <textarea
            className="edit-textarea"
            style={{ minHeight: '100px' }}
            value={stateContent}
            onChange={(e) => setStateContent(e.target.value)}
            disabled={busy}
            autoFocus
          />
        </div>
        <div className="modal-section">
          <div className="modal-label">行为规则（action_rule，可选）</div>
          <textarea
            className="edit-textarea"
            style={{ minHeight: '80px' }}
            value={actionRule}
            onChange={(e) => setActionRule(e.target.value)}
            disabled={busy}
          />
        </div>
        <div className="modal-section">
          <div className="modal-label">到期时间（expire_at，可选）</div>
          <input
            type="datetime-local"
            className="search-input"
            value={expireAt}
            onChange={(e) => setExpireAt(e.target.value)}
            disabled={busy}
          />
        </div>
        <div className="modal-actions">
          <button type="button" className="modal-button cancel" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="button" className="modal-button confirm" onClick={handleSave} disabled={busy}>
            {busy ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 时效状态列表项
 */
function TemporalStateItem({ row, addToast, onRefresh, onEdit }) {
  const [showConfirm, setShowConfirm] = useState(false);
  const status = getTemporalDisplayStatus(row);
  const expireLabel = row.expire_at
    ? formatShanghaiDateTime(row.expire_at)
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
        <span style={{ display: 'flex', gap: '6px' }}>
          <button className="action-button edit-button" type="button" onClick={() => onEdit(row)}>
            编辑
          </button>
          {/* 仅「生效中」可手动停用；已到期仍 is_active=1 时显示「已过期」，由日终 Step1 结算，不再提供软删除 */}
          {status.label === '生效中' && (
            <button className="delete-button" type="button" onClick={() => setShowConfirm(true)}>
              软删除
            </button>
          )}
        </span>
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
  const displayTime = updatedAt ? formatShanghaiDate(updatedAt) : '未记录';

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
 * 长期记忆：编辑 Chroma 元数据（halflife_days、arousal）
 */
function LongTermMetadataModal({ memory, onClose, onSave }) {
  const [halflifeDays, setHalflifeDays] = useState('30');
  const [arousal, setArousal] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!memory) {
      return;
    }
    setHalflifeDays(String(memory.halflife_days ?? 30));
    setArousal(memory.arousal != null && memory.arousal !== '' ? String(memory.arousal) : '');
  }, [memory]);

  if (!memory) {
    return null;
  }

  const handleSubmit = async () => {
    const payload = {};
    const hl = parseInt(halflifeDays, 10);
    if (!Number.isNaN(hl)) {
      payload.halflife_days = hl;
    }
    const ar = arousal.trim();
    if (ar !== '') {
      const a = parseFloat(ar);
      if (!Number.isNaN(a)) {
        payload.arousal = a;
      }
    }
    if (!Object.keys(payload).length) {
      onClose();
      return;
    }
    setSubmitting(true);
    try {
      const ok = await onSave(memory.chroma_doc_id, payload);
      if (ok) {
        onClose();
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <div
        className="modal-container"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="longterm-meta-title"
      >
        <div className="modal-title" id="longterm-meta-title">
          编辑元数据
        </div>
        <div className="modal-section">
          <div className="modal-label">半衰期 halflife_days（天）</div>
          <input
            type="number"
            className="search-input"
            value={halflifeDays}
            onChange={(e) => setHalflifeDays(e.target.value)}
            min={1}
            disabled={submitting}
          />
        </div>
        <div className="modal-section">
          <div className="modal-label">情绪强度 arousal（可选）</div>
          <input
            type="number"
            step="any"
            className="search-input"
            value={arousal}
            onChange={(e) => setArousal(e.target.value)}
            placeholder="留空表示不在此修改"
            disabled={submitting}
          />
        </div>
        <div className="modal-actions">
          <button type="button" className="modal-button cancel" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button type="button" className="modal-button confirm" onClick={handleSubmit} disabled={submitting}>
            {submitting ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 长期记忆项组件（Chroma 全量列表）
 */
function LongTermMemoryItem({ memory, onDelete, onEdit, gcExemptHitsThreshold, isInCurrentContext, contextTraceLabel }) {
  const [showConfirm, setShowConfirm] = useState(false);
  const [viewOpen, setViewOpen] = useState(false);
  const [showViewFull, setShowViewFull] = useState(false);
  const longtermSummaryRef = useRef(null);
  const bodyText = memory.content || '';

  const handleDelete = () => {
    setShowConfirm(true);
  };

  const confirmDelete = () => {
    onDelete(memory.chroma_doc_id);
    setShowConfirm(false);
  };

  useLayoutEffect(() => {
    if (!bodyText.trim()) {
      setShowViewFull(false);
      return;
    }
    const el = longtermSummaryRef.current;
    if (!el) {
      setShowViewFull(false);
      return;
    }
    const checkOverflow = () => {
      setShowViewFull(isMemoryCardContentTruncated(el, bodyText));
    };
    checkOverflow();
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(checkOverflow) : null;
    if (ro) {
      ro.observe(el);
    }
    window.addEventListener('resize', checkOverflow);
    return () => {
      if (ro) {
        ro.disconnect();
      }
      window.removeEventListener('resize', checkOverflow);
    };
  }, [bodyText]);

  const hitsNum = memory.hits != null ? Number(memory.hits) : null;
  const isGcExempt = hitsNum != null && gcExemptHitsThreshold != null && hitsNum >= gcExemptHitsThreshold;
  const arousalDisplay = memory.arousal != null ? Number(memory.arousal).toFixed(2) : null;
  const baseDisplay =
    memory.base_score != null && memory.base_score !== ''
      ? Number(memory.base_score).toFixed(1)
      : '—';
  const summaryType = memory.summary_type || '';
  const typeLabel = SUMMARY_TYPE_LABELS[summaryType] || summaryType || '—';

  if (showConfirm) {
    return (
      <div className="modal-overlay">
        <div className="modal-container confirm-modal">
          <div className="modal-title">确认删除</div>
          <div className="confirm-message">确认删除这条长期记忆吗？</div>
          <div className="confirm-warning">删除后不可恢复。</div>
          <div className="modal-actions">
            <button type="button" className="modal-button cancel" onClick={() => setShowConfirm(false)}>
              取消
            </button>
            <button type="button" className="modal-button delete" onClick={confirmDelete}>
              确认删除
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={`memory-item ${showViewFull ? 'memory-item--has-view-link' : ''} ${isInCurrentContext ? 'memory-item--context' : ''}`}>
      {viewOpen && (
        <ViewLongtermMemoryModal
          title={formatLongtermTitleLine(memory)}
          content={bodyText}
          onClose={() => setViewOpen(false)}
        />
      )}
      <div className="memory-item-head memory-item-head--longterm">
        <div className="memory-item-head__meta">
          <span className="memory-longterm-title">{formatLongtermTitleLine(memory)}</span>
          {summaryType ? (
            <span className="memory-longterm-type-badge timeline-type-badge" title="summary_type">
              {typeLabel}
            </span>
          ) : null}
          {isInCurrentContext ? (
            <span
              className="memory-context-badge timeline-type-badge"
              title={contextTraceLabel || '最近一轮 context 实际注入了这条长期记忆'}
            >
              本轮
            </span>
          ) : null}
        </div>
      </div>
      {isGcExempt ? (
        <div className="memory-longterm-badge-row">
          <span className="gc-exempt-badge" title={`引用次数 ${hitsNum} ≥ 阈值 ${gcExemptHitsThreshold}，已豁免自动删除`}>
            <Lock className="gc-exempt-badge__icon" size={12} strokeWidth={2} aria-hidden />
            免删
          </span>
        </div>
      ) : null}
      <div className="memory-longterm-body-wrap">
        <div ref={longtermSummaryRef} className="memory-summary memory-summary--longterm-only">
          {memory.content}
        </div>
      </div>
      <div className="memory-detail-row memory-detail-row--chroma-stats">
        <span className="memory-meta-chip">hits：{hitsNum != null ? hitsNum : '—'}</span>
        <span className="memory-meta-chip">halflife_days：{memory.halflife_days != null ? memory.halflife_days : '—'}</span>
        <span className="memory-meta-chip">arousal：{arousalDisplay != null ? arousalDisplay : '—'}</span>
        <span className="memory-meta-chip">base_score：{baseDisplay}</span>
      </div>
      <div className="memory-longterm-meta-footer">
        <div className="memory-longterm-footer-actions">
          {showViewFull ? (
            <button type="button" className="card-view-full-link" onClick={() => setViewOpen(true)}>
              查看全文
            </button>
          ) : null}
          <button type="button" className="action-button edit-button" onClick={() => onEdit(memory)}>
            编辑
          </button>
          {memory.is_manual ? (
            <button type="button" className="delete-button" onClick={handleDelete}>
              删除
            </button>
          ) : null}
        </div>
        <div className="memory-longterm-meta-footer__line">
          最近访问 last_access_ts：{formatLastAccessTs(memory.last_access_ts)}
        </div>
        <div className="memory-longterm-meta-footer__doc">
          <span className="memory-longterm-meta-footer__doc-label">doc_id: </span>
          <span className="memory-longterm-doc-id">{memory.chroma_doc_id}</span>
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
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="skeleton-line short" style={{ width: '88px', height: '36px' }} />
          ))}
        </div>
      </div>
      <div className="memory-content-scroll-area">
        {/* 记忆卡片骨架屏 */}
        <>
          <div className="memory-tab-header">
            <h2 className="memory-tab-header__title">
              <span className="memory-tab-header__icon memory-tab-header__icon--skeleton" aria-hidden="true" />
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
              <span className="memory-tab-header__icon memory-tab-header__icon--skeleton" aria-hidden="true" />
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
  const memoryTabsRef = useHorizontalDragScroll();

  // 状态管理
  const [loading, setLoading] = useState(true);
  const [memoryCards, setMemoryCards] = useState({});
  const [longTermMemories, setLongTermMemories] = useState([]);
  const [toasts, setToasts] = useState([]);
  const [gcExemptHitsThreshold, setGcExemptHitsThreshold] = useState(null);
  const [contextTrace, setContextTrace] = useState(null);
  
  // 弹窗状态
  const [showEditModal, setShowEditModal] = useState(false);
  const [editingDimension, setEditingDimension] = useState(null);
  const [editingContent, setEditingContent] = useState('');
  
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [deletingDimension, setDeletingDimension] = useState(null);
  
  const [showAddModal, setShowAddModal] = useState(false);
  const [showTemporalAddModal, setShowTemporalAddModal] = useState(false);
  const [temporalEditingRow, setTemporalEditingRow] = useState(null);
  const [longtermEditMemory, setLongtermEditMemory] = useState(null);
  
  const [activeTab, setActiveTab] = useState('summaries');
  const [temporalStates, setTemporalStates] = useState([]);
  const [temporalLoading, setTemporalLoading] = useState(false);
  const [temporalActiveOnly, setTemporalActiveOnly] = useState(false);
  const [timelineEvents, setTimelineEvents] = useState([]);
  const [timelineLoading, setTimelineLoading] = useState(false);

  // 摘要表（summaries）：chunk/daily + 可选 source_date 区间 + 分页
  const [summariesItems, setSummariesItems] = useState([]);
  const [summariesTotal, setSummariesTotal] = useState(0);
  const [summariesPage, setSummariesPage] = useState(1);
  const [summariesLoading, setSummariesLoading] = useState(false);
  const [summaryKindFilter, setSummaryKindFilter] = useState('chunk');
  const [summariesContextOnly, setSummariesContextOnly] = useState(false);
  const [summariesDateFrom, setSummariesDateFrom] = useState('');
  const [summariesDateTo, setSummariesDateTo] = useState('');
  const [confirmDeleteSummaryId, setConfirmDeleteSummaryId] = useState(null);
  const [summaryEditingRow, setSummaryEditingRow] = useState(null);
  const summariesFetchSeqRef = useRef(0);
  
  // 长期记忆：summary_type 筛选与分页
  const [longtermTypeFilter, setLongtermTypeFilter] = useState('');
  const [longtermContextOnly, setLongtermContextOnly] = useState(false);
  const [currentPage, setCurrentPage] = useState(1);
  const [longtermTotal, setLongtermTotal] = useState(0);
  const totalPages = Math.max(1, Math.ceil(longtermTotal / LONGTERM_PAGE_SIZE));
  const summariesTotalPages = Math.max(1, Math.ceil(summariesTotal / SUMMARIES_PAGE_SIZE));
  const contextSummaryIdSet = useMemo(() => {
    const ids = [
      ...(contextTrace?.daily_summary_ids || []),
      ...(contextTrace?.chunk_summary_ids || []),
      ...(contextTrace?.archived_daily_summary_ids || []),
    ];
    return new Set(ids.map((id) => Number(id)).filter((id) => !Number.isNaN(id)));
  }, [contextTrace]);
  const contextLongtermDocIdSet = useMemo(
    () => new Set((contextTrace?.longterm_doc_ids || []).map((id) => String(id))),
    [contextTrace]
  );
  const contextTraceLabel = contextTrace?.built_at
    ? `最近构建：${formatShanghaiDateTime(contextTrace.built_at)}${
        contextTrace.session_id ? ` · session: ${contextTrace.session_id}` : ''
      }`
    : '';
  
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
            const n = parseShanghaiDateTime(raw).getTime();
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

  const loadContextTrace = useCallback(async () => {
    try {
      const response = await apiFetch('/api/memory/context-trace');
      const data = await response.json().catch(() => ({}));
      if (response.ok && data.success) {
        setContextTrace(data.data || null);
      }
    } catch (error) {
      console.error('加载本轮 context 标记失败:', error);
    }
  }, []);
  
  // 加载长期记忆数据（ChromaDB 全量分页）
  const loadLongTermMemories = useCallback(
    async (page = 1, summaryTypeFilter = longtermTypeFilter, contextOnlyFilter = longtermContextOnly) => {
      try {
        const params = new URLSearchParams({
          page: page.toString(),
          page_size: String(LONGTERM_PAGE_SIZE),
        });
        if (summaryTypeFilter) {
          params.set('summary_type', summaryTypeFilter);
        }
        if (contextOnlyFilter) {
          params.set('context_only', 'true');
        }

        const response = await apiFetch(`/api/memory/longterm?${params}`);
        if (!response.ok) {
          throw new Error('获取长期记忆失败');
        }
        const data = await response.json();

        if (data.success) {
          setLongTermMemories(data.data?.items || []);
          setLongtermTotal(Number(data.data?.total) || 0);
          setCurrentPage(data.data?.page ?? page);
        }
      } catch (error) {
        console.error('加载长期记忆失败:', error);
        setLongTermMemories([]);
        setLongtermTotal(0);
        setCurrentPage(1);
        addToast('加载长期记忆失败：' + error.message, 'error');
      }
    },
    [addToast, longtermTypeFilter, longtermContextOnly]
  );
  
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

  const loadSummaries = useCallback(async () => {
    const seq = ++summariesFetchSeqRef.current;
    setSummariesLoading(true);
    try {
      const params = new URLSearchParams({
        page: String(summariesPage),
        page_size: String(SUMMARIES_PAGE_SIZE),
      });
      if (!summariesContextOnly) {
        params.set('summary_type', summaryKindFilter);
      } else {
        params.set('context_only', 'true');
        params.set('summary_type', summaryKindFilter);
      }
      const df = normalizeSummaryDateInput(summariesDateFrom);
      const dt = normalizeSummaryDateInput(summariesDateTo);
      if (!summariesContextOnly && df) {
        params.set('source_date_from', df);
      }
      if (!summariesContextOnly && dt) {
        params.set('source_date_to', dt);
      }
      const response = await apiFetch(`/api/memory/summaries?${params.toString()}`);
      const data = await response.json();
      if (seq !== summariesFetchSeqRef.current) {
        return;
      }
      if (data.success) {
        const items = data.data?.items || [];
        const total = Number(data.data?.total) || 0;
        setSummariesItems(items);
        setSummariesTotal(total);
        const p = Number(data.data?.page);
        if (!Number.isNaN(p) && p >= 1) {
          setSummariesPage(p);
        }
        if (items.length === 0 && total > 0) {
          setSummariesPage((prev) => Math.max(1, prev - 1));
        }
        if (total === 0) {
          setSummariesPage(1);
        }
      } else {
        throw new Error(data.message || '加载失败');
      }
    } catch (error) {
      if (seq === summariesFetchSeqRef.current) {
        console.error('加载摘要列表失败:', error);
        setSummariesItems([]);
        setSummariesTotal(0);
        addToast(error.message || '加载摘要失败', 'error');
      }
    } finally {
      if (seq === summariesFetchSeqRef.current) {
        setSummariesLoading(false);
      }
    }
  }, [summariesPage, summaryKindFilter, summariesContextOnly, summariesDateFrom, summariesDateTo, addToast]);
  
  useEffect(() => {
    if (activeTab === 'summaries' || activeTab === 'longterm') {
      loadContextTrace();
    }
    if (activeTab === 'temporal') {
      loadTemporalStates();
    } else if (activeTab === 'timeline') {
      loadRelationshipTimeline();
    } else if (activeTab === 'summaries') {
      loadSummaries();
    }
  }, [activeTab, loadTemporalStates, loadRelationshipTimeline, loadSummaries, loadContextTrace]);
  
  // 初始化加载数据
  useEffect(() => {
    const loadAllData = async () => {
      setLoading(true);
      await Promise.all([
        loadMemoryCards(),
        loadLongTermMemories(),
        loadContextTrace(),
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
  }, [loadMemoryCards, loadLongTermMemories, loadContextTrace]);
  
  
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

      await loadLongTermMemories(currentPage);
    } catch (error) {
      console.error('新增长期记忆失败:', error);
      addToast('操作失败，请重试', 'error');
    }
  };
  
  // 删除长期记忆（仅 manual_ 文档）
  const handleDeleteMemory = async (chromaDocId) => {
    try {
      const response = await apiFetch(`/api/memory/longterm/${encodeURIComponent(chromaDocId)}`, {
        method: 'DELETE',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) {
        throw new Error(data.message || '删除记忆失败');
      }

      addToast('长期记忆删除成功', 'success');
      await loadLongTermMemories(currentPage);
    } catch (error) {
      console.error('删除长期记忆失败:', error);
      addToast(error.message || '操作失败，请重试', 'error');
    }
  };

  const handleSaveLongtermMetadata = async (chromaDocId, payload) => {
    try {
      const response = await apiFetch(
        `/api/memory/longterm/${encodeURIComponent(chromaDocId)}/metadata`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }
      );
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) {
        addToast(data.message || '更新失败', 'error');
        return false;
      }
      addToast('元数据已更新', 'success');
      await loadLongTermMemories(currentPage);
      return true;
    } catch (error) {
      addToast(error.message || '更新失败', 'error');
      return false;
    }
  };
  
  // 处理分页
  const handlePrevPage = () => {
    if (currentPage > 1) {
      const newPage = currentPage - 1;
      setCurrentPage(newPage);
      loadLongTermMemories(newPage);
    }
  };

  const handleNextPage = () => {
    if (currentPage < totalPages) {
      const newPage = currentPage + 1;
      setCurrentPage(newPage);
      loadLongTermMemories(newPage);
    }
  };

  const handleFirstPage = () => {
    if (currentPage <= 1) {
      return;
    }
    setCurrentPage(1);
    loadLongTermMemories(1);
  };

  const handleLastPage = () => {
    if (currentPage >= totalPages) {
      return;
    }
    setCurrentPage(totalPages);
    loadLongTermMemories(totalPages);
  };

  const handleSummariesPrevPage = () => {
    if (summariesPage > 1) {
      setSummariesPage(summariesPage - 1);
    }
  };

  const handleSummariesNextPage = () => {
    if (summariesPage < summariesTotalPages) {
      setSummariesPage(summariesPage + 1);
    }
  };

  const handleSummariesFirstPage = () => {
    if (summariesPage <= 1) {
      return;
    }
    setSummariesPage(1);
  };

  const handleSummariesLastPage = () => {
    if (summariesPage >= summariesTotalPages) {
      return;
    }
    setSummariesPage(summariesTotalPages);
  };

  const handleSummaryDeleteConfirm = async (id) => {
    setConfirmDeleteSummaryId(null);
    try {
      const response = await apiFetch(`/api/memory/summaries/${id}`, { method: 'DELETE' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) {
        throw new Error(data.message || '删除失败');
      }
      addToast('已删除', 'success');
      await loadSummaries();
    } catch (error) {
      console.error('删除摘要失败:', error);
      addToast(error.message || '删除失败', 'error');
    }
  };

  const handleSummaryToggleStar = async (row) => {
    const nextStarred = !Boolean(row.is_starred);
    setSummariesItems((items) =>
      items.map((item) =>
        item.id === row.id ? { ...item, is_starred: nextStarred } : item
      )
    );
    try {
      const response = await apiFetch(`/api/memory/summaries/${row.id}/star`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ is_starred: nextStarred }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) {
        throw new Error(data.message || '更新收藏失败');
      }
      addToast(nextStarred ? '已收藏' : '已取消收藏', 'success');
    } catch (error) {
      console.error('更新摘要收藏失败:', error);
      setSummariesItems((items) =>
        items.map((item) =>
          item.id === row.id ? { ...item, is_starred: Boolean(row.is_starred) } : item
        )
      );
      addToast(error.message || '更新收藏失败', 'error');
    }
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

      {longtermEditMemory && (
        <LongTermMetadataModal
          memory={longtermEditMemory}
          onClose={() => setLongtermEditMemory(null)}
          onSave={handleSaveLongtermMetadata}
        />
      )}
      
      {showTemporalAddModal && (
        <AddTemporalStateModal
          onClose={() => setShowTemporalAddModal(false)}
          onSubmit={handleAddTemporalState}
        />
      )}

      {temporalEditingRow && (
        <EditTemporalStateModal
          row={temporalEditingRow}
          addToast={addToast}
          onClose={() => setTemporalEditingRow(null)}
          onSaved={() => {
            addToast('已保存', 'success');
            setTemporalEditingRow(null);
            loadTemporalStates();
          }}
        />
      )}

      {summaryEditingRow && (
        <SummaryEditModal
          row={summaryEditingRow}
          addToast={addToast}
          onClose={() => setSummaryEditingRow(null)}
          onSaved={() => {
            addToast('已保存', 'success');
            setSummaryEditingRow(null);
            loadSummaries();
          }}
        />
      )}
      
      <div className="memory-tabs-scroll" ref={memoryTabsRef} aria-label="记忆页签切换">
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
              <span className="memory-tab-header__icon" aria-hidden="true">
                <BookOpen size={20} strokeWidth={2} />
              </span>
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
            <span className="memory-tab-header__icon" aria-hidden="true">
              <Database size={20} strokeWidth={2} />
            </span>
            <span className="memory-tab-header__title-text">长期记忆库</span>
          </h2>
            <div className="memory-tab-header__actions">
              <button className="add-button" type="button" onClick={() => setShowAddModal(true)}>
                + 手动新增
              </button>
            </div>
          </div>

          <div className="longterm-header">
            <label className="longterm-filter-label" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span style={{ whiteSpace: 'nowrap' }}>类型筛选</span>
              <select
                className="search-input longterm-type-select"
                value={longtermTypeFilter}
                onChange={(e) => {
                  const v = e.target.value;
                  setLongtermTypeFilter(v);
                  setCurrentPage(1);
                  loadLongTermMemories(1, v);
                }}
              >
                <option value="">全部</option>
                <option value="daily">日总</option>
                <option value="daily_event">日终片段</option>
                <option value="manual">手动</option>
                <option value="app_event">APP端</option>
                <option value="state_archive">状态归档</option>
              </select>
            </label>
            <button
              type="button"
              className={`memory-context-filter-btn ${longtermContextOnly ? 'active' : ''}`}
              onClick={() => {
                const next = !longtermContextOnly;
                setLongtermContextOnly(next);
                setCurrentPage(1);
                loadLongTermMemories(1, longtermTypeFilter, next);
              }}
            >
              只看本轮
            </button>
          </div>

          <div className="memory-list">
            {longTermMemories.length === 0 ? (
              <div className="empty-state">
                <div className="empty-state-icon" aria-hidden>
                  <FileText size={48} strokeWidth={1.25} />
                </div>
                <div className="empty-state-text">暂无长期记忆记录</div>
              </div>
            ) : (
              longTermMemories.map((memory) => (
                <LongTermMemoryItem
                  key={memory.chroma_doc_id}
                  memory={memory}
                  onDelete={handleDeleteMemory}
                  onEdit={(m) => setLongtermEditMemory(m)}
                  gcExemptHitsThreshold={gcExemptHitsThreshold}
                  isInCurrentContext={contextLongtermDocIdSet.has(String(memory.chroma_doc_id))}
                  contextTraceLabel={contextTraceLabel}
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
              <span className="memory-tab-header__icon" aria-hidden="true">
                <Timer size={20} strokeWidth={2} />
              </span>
              <span className="memory-tab-header__title-text">时效状态</span>
            </h2>
            <div className="memory-tab-header__actions" style={{ display: 'flex', alignItems: 'center', gap: '8px', width: '100%' }}>
              <button
                type="button"
                className={`memory-context-filter-btn ${temporalActiveOnly ? 'active' : ''}`}
                onClick={() => setTemporalActiveOnly((prev) => !prev)}
              >
                只看生效中
              </button>
              <button className="add-button" type="button" style={{ marginLeft: 'auto' }} onClick={() => setShowTemporalAddModal(true)}>
                + 新增
              </button>
            </div>
          </div>
          {temporalLoading ? (
            <div className="tab-loading">加载中…</div>
          ) : temporalStates.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-icon" aria-hidden>
                <Timer size={48} strokeWidth={1.25} />
              </div>
              <div className="empty-state-text">暂无时效状态</div>
            </div>
          ) : (() => {
            const filtered = temporalActiveOnly
              ? temporalStates.filter((row) => getTemporalDisplayStatus(row).label === '生效中')
              : temporalStates;
            return filtered.length === 0 ? (
              <div className="empty-state">
                <div className="empty-state-text">暂无生效中的时效状态</div>
              </div>
            ) : (
              <div className="memory-list">
                {filtered.map((row) => (
                  <TemporalStateItem
                    key={row.id}
                    row={row}
                    addToast={addToast}
                    onRefresh={loadTemporalStates}
                    onEdit={setTemporalEditingRow}
                  />
                ))}
              </div>
            );
          })()}
        </>
      )}

      {activeTab === 'timeline' && (
        <>
          <div className="memory-tab-header">
            <h2 className="memory-tab-header__title">
              <span className="memory-tab-header__icon" aria-hidden="true">
                <HeartHandshake size={20} strokeWidth={2} />
              </span>
              <span className="memory-tab-header__title-text">关系时间线</span>
            </h2>
          </div>
          {timelineLoading ? (
            <div className="tab-loading">加载中…</div>
          ) : timelineEvents.length === 0 ? (
            <div className="empty-state">
            <div className="empty-state-icon"><Calendar size={48} strokeWidth={1} /></div>
            <div className="empty-state-text">暂无关系时间线记录</div>
            </div>
          ) : (
            <div className="timeline-list">
              {timelineEvents.map((ev) => (
                <div key={ev.id} className="timeline-item">
                  <div className="timeline-item-head">
                    <span className="timeline-time">
                      {ev.created_at ? formatShanghaiDateTime(ev.created_at) : '—'}
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

      {activeTab === 'summaries' && (
        <>
          <div className="memory-tab-header">
            <h2 className="memory-tab-header__title">
              <span className="memory-tab-header__icon" aria-hidden="true">
                <ScrollText size={20} strokeWidth={2} />
              </span>
              <span className="memory-tab-header__title-text">摘要</span>
            </h2>
          </div>

          <div className="summaries-toolbar">
            <div className="summaries-type-toggle" role="group" aria-label="摘要类型">
              <button
                type="button"
                className={`summaries-type-btn ${summaryKindFilter === 'chunk' ? 'active' : ''}`}
                onClick={() => {
                  setSummaryKindFilter('chunk');
                  setSummariesPage(1);
                }}
              >
                chunk
              </button>
              <button
                type="button"
                className={`summaries-type-btn ${summaryKindFilter === 'daily' ? 'active' : ''}`}
                onClick={() => {
                  setSummaryKindFilter('daily');
                  setSummariesPage(1);
                }}
              >
                daily
              </button>
            </div>
            <button
              type="button"
              className={`memory-context-filter-btn ${summariesContextOnly ? 'active' : ''}`}
              onClick={() => {
                setSummariesContextOnly((prev) => !prev);
                setSummariesPage(1);
              }}
            >
              只看本轮
            </button>
            <div className="summaries-date-range" role="group" aria-label="source_date 范围">
              <span className="summaries-filter-label-text">source_date</span>
              <label className="summaries-date-field">
                <span className="summaries-date-field-label">起</span>
                <input
                  type="date"
                  className="search-input summaries-date-input"
                  value={summariesDateFrom}
                  onChange={(e) => {
                    setSummariesDateFrom(e.target.value);
                    setSummariesPage(1);
                  }}
                />
              </label>
              <span className="summaries-date-sep" aria-hidden>
                —
              </span>
              <label className="summaries-date-field">
                <span className="summaries-date-field-label">止</span>
                <input
                  type="date"
                  className="search-input summaries-date-input"
                  value={summariesDateTo}
                  onChange={(e) => {
                    setSummariesDateTo(e.target.value);
                    setSummariesPage(1);
                  }}
                />
              </label>
            </div>
          </div>

          {summariesLoading ? (
            <div className="tab-loading">加载中…</div>
          ) : summariesItems.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-icon" aria-hidden>
                <ScrollText size={48} strokeWidth={1.25} />
              </div>
              <div className="empty-state-text">暂无摘要记录</div>
            </div>
          ) : (
            <div className="memory-list">
              {summariesItems.map((row) => (
                <SummaryRecordItem
                  key={row.id}
                  row={row}
                  confirmDeleteId={confirmDeleteSummaryId}
                  onBeginDelete={(id) => setConfirmDeleteSummaryId(id)}
                  onCancelDelete={() => setConfirmDeleteSummaryId(null)}
                  onDeleteConfirm={handleSummaryDeleteConfirm}
                  onEdit={(r) => setSummaryEditingRow(r)}
                  onToggleStar={handleSummaryToggleStar}
                  isInCurrentContext={contextSummaryIdSet.has(Number(row.id))}
                  contextTraceLabel={contextTraceLabel}
                />
              ))}
            </div>
          )}
        </>
      )}
      </div>

      {activeTab === 'longterm' && longtermTotal > 0 && totalPages > 1 && (
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

      {activeTab === 'summaries' && summariesTotal > 0 && summariesTotalPages > 1 && (
        <div className="pagination pagination--outside">
          <button
            className="pagination-button"
            type="button"
            onClick={handleSummariesFirstPage}
            disabled={summariesPage <= 1}
          >
            首页
          </button>
          <button
            className="pagination-button"
            type="button"
            onClick={handleSummariesPrevPage}
            disabled={summariesPage <= 1}
          >
            上页
          </button>
          <div
            className="pagination-info pagination-info--stacked"
            role="status"
            aria-live="polite"
          >
            <span className="pagination-info-line">第 {summariesPage} 页</span>
            <span className="pagination-info-line">共 {summariesTotalPages} 页</span>
          </div>
          <button
            className="pagination-button"
            type="button"
            onClick={handleSummariesNextPage}
            disabled={summariesPage >= summariesTotalPages}
          >
            下页
          </button>
          <button
            className="pagination-button"
            type="button"
            onClick={handleSummariesLastPage}
            disabled={summariesPage >= summariesTotalPages}
          >
            尾页
          </button>
        </div>
      )}
    </div>
  );
}

export default Memory;
