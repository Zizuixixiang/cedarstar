/**
 * 助手配置页面
 * 运行参数滑块 + 数字输入框双向联动，与 /api/config/config 对齐
 */

import { useState, useEffect, useCallback } from 'react';
import { apiFetch } from '../apiBase';
import '../styles/config.css';

// 配置项默认值（与 api/config.py DEFAULT_CONFIG 一致，供合并与重置）
const DEFAULT_CONFIG = {
  short_term_limit: 40,
  buffer_delay: 5,
  chunk_threshold: 50,
  context_max_daily_summaries: 5,
  context_max_longterm: 3,
  event_split_max: 8,
  mmr_lambda: 0.75,
  daily_batch_hour: 23,
  relationship_timeline_limit: 3,
  gc_stale_days: 180,
  retrieval_top_k: 5,
  telegram_max_chars: 50,
  telegram_max_msg: 8,
  send_cot_to_telegram: 1,
  offline_mode_active: 0,
  group_chat_silent_mode: 0,
  group_chat_max_rounds: 3,
  group_chat_interject_enabled: 0,
  group_chat_interject_probability: 0.2,
};

/** Telegram 分段参数：单独 PUT 保存，与 api/config.py 一致 */
const TELEGRAM_CONFIG_ROWS = [
  {
    key: 'telegram_max_chars',
    name: '每段上限字数',
    description: '对应提示词中的 MAX_CHARS',
    min: 10,
    max: 1000,
    step: 10,
  },
  {
    key: 'telegram_max_msg',
    name: '正文最多几条',
    description: '对应提示词中的 MAX_MSG',
    min: 1,
    max: 20,
    step: 1,
  },
];

function clampTelegramChars(raw) {
  let v = Math.round(Number(raw));
  if (Number.isNaN(v)) return DEFAULT_CONFIG.telegram_max_chars;
  v = Math.max(10, Math.min(1000, v));
  v = Math.round(v / 10) * 10;
  return Math.max(10, Math.min(1000, v));
}

function clampTelegramMsg(raw) {
  const v = Math.round(Number(raw));
  if (Number.isNaN(v)) return DEFAULT_CONFIG.telegram_max_msg;
  return Math.max(1, Math.min(20, v));
}

// 配置项元数据
/** 解析后端 SQLite 风格或 ISO 时间字符串为本地 Date */
function parseConfigUpdatedAt(ts) {
  if (ts == null || typeof ts !== 'string' || !ts.trim()) return null;
  const s = ts.trim();
  // 数据库存储的是东八区本地时间（无时区信息），直接作为本地时间解析，不加 Z（加了会被当 UTC 再偏移 +8）
  const normalized = s.includes('T') ? s : s.replace(' ', 'T');
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 剥离 data._meta，合并参数并解析上次保存时间（来自库内 MAX(updated_at)，非「当前请求时刻」） */
function mergeConfigApiPayload(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const { _meta, ...rest } = payload;
  const params = { ...DEFAULT_CONFIG, ...rest };
  const lastSaved = _meta?.updated_at ? parseConfigUpdatedAt(_meta.updated_at) : null;
  return { params, lastSaved };
}

const CONFIG_METADATA = [
  {
    key: 'buffer_delay',
    name: '消息缓冲延迟',
    description: '连发短消息的合并等待时间（秒）',
    hint: '用户发消息后等待合并的秒数，调大可减少碎片消息',
    min: 3,
    max: 100,
  },
  {
    key: 'short_term_limit',
    name: '短期记忆携带量',
    description: '每次发给 AI 的最近原文消息条数',
    hint: '每次对话注入多少条近期原始消息',
    min: 10,
    max: 200,
  },
  {
    key: 'chunk_threshold',
    name: 'Chunk 触发阈值',
    description: '多少条消息触发一次日内微批总结',
    hint: '累积多少条消息触发一次微批摘要压缩',
    min: 20,
    max: 100,
  },
  {
    key: 'context_max_daily_summaries',
    name: '每日小传注入条数',
    description: 'Context 中纳入的 summary_type=daily 条数',
    hint: '注入最近几天的今日小传作为背景',
    min: 1,
    max: 30,
  },
  {
    key: 'context_max_longterm',
    name: '长期记忆注入条数',
    description: '向量召回并经精排后最终注入的条数',
    hint: '从向量库召回后最终注入几条长期记忆',
    min: 1,
    max: 10,
  },
  {
    key: 'event_split_max',
    name: '事件拆分软上限',
    description: 'Step 4 单日最多拆出的事件片段数',
    hint: '平淡日常通常 1-2 条，复杂日期最多不超过该上限',
    min: 1,
    max: 15,
  },
  {
    key: 'mmr_lambda',
    name: 'MMR 相关性权重',
    description: '长期记忆召回中相关性与多样性的平衡',
    hint: '越接近 1 越偏相关性，越接近 0.5 越偏多样性',
    min: 0.5,
    max: 1,
    step: 0.05,
  },
  {
    key: 'daily_batch_hour',
    name: '日终跑批时刻',
    description: '东八区每天触发的时刻（支持半小时）',
    hint: '每天几点触发日终跑批（24小时制，东八区）',
    min: 0,
    max: 23.5,
    step: 0.5,
  },
  {
    key: 'group_chat_max_rounds',
    name: '群聊互聊上限',
    description: '两个 Bot 连续互相回应的最大轮数',
    hint: '超过后自动静默，用户发言会清零',
    min: 1,
    max: 12,
  },
  {
    key: 'group_chat_interject_probability',
    name: '群聊插话概率',
    description: '另一 Bot 发言后主动插话概率',
    hint: '0 表示不随机插话，1 表示总是插话',
    min: 0,
    max: 1,
    step: 0.05,
  },
  {
    key: 'relationship_timeline_limit',
    name: '关系时间线条数',
    description: '注入 Context 的关系时间线事件条数',
    hint: '注入最近几条关系时间线事件',
    min: 1,
    max: 20,
  },
  {
    key: 'gc_stale_days',
    name: 'GC 闲置天数',
    description: '向量记忆多久未引用可参与 GC',
    hint: '向量库中超过多少天未被引用的记忆才会被清理',
    min: 30,
    max: 730,
  },
  {
    key: 'retrieval_top_k',
    name: '双路召回每路条数',
    description: '向量检索与 BM25 各自取的候选数',
    hint: '向量检索和关键词检索各自捞多少条候选',
    min: 1,
    max: 20,
  },
];

/**
 * Toast 提示组件
 */
function Toast({ message, type, onClose }) {
  useEffect(() => {
    const timer = setTimeout(onClose, 2000);
    return () => clearTimeout(timer);
  }, [onClose]);

  return (
    <div className={`config-toast${type === 'error' ? ' error' : ''}`}>
      {message}
    </div>
  );
}

/**
 * 确认对话框组件（替代 window.confirm）
 */
function ConfirmDialog({ title, desc, onConfirm, onCancel }) {
  return (
    <div className="config-confirm-overlay" onClick={onCancel}>
      <div className="config-confirm-box" onClick={e => e.stopPropagation()}>
        <div className="config-confirm-title">{title}</div>
        <div className="config-confirm-desc">{desc}</div>
        <div className="config-confirm-actions">
          <button className="config-btn-secondary" style={{ flex: 1 }} onClick={onCancel}>
            取消
          </button>
          <button className="config-btn-primary" style={{ flex: 1 }} onClick={onConfirm}>
            确认重置
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 骨架屏
 */
function ConfigSkeleton() {
  return (
    <div className="config-container">
      <div className="config-card">
        <header className="config-card-header">
          <h1 className="config-card-title">
            <span className="config-card-title__prefix" aria-hidden="true">
              ■
            </span>
            <span className="config-card-title__text">助手配置</span>
          </h1>
          <p className="config-card-subtitle">
            <span className="config-card-subtitle__prompt">[INFO]</span>
            修改后点击保存即时生效，无需重启服务
          </p>
        </header>

        {CONFIG_METADATA.map((item, index) => (
          <div key={item.key}>
            <div className="config-skeleton-item">
              <div className="config-skeleton-left">
                <div className="skeleton-line" style={{ width: '45%' }}></div>
                <div className="skeleton-line" style={{ width: '70%' }}></div>
              </div>
              <div className="config-skeleton-right">
                <div className="skeleton-slider"></div>
                <div className="skeleton-number"></div>
              </div>
            </div>
            {index < CONFIG_METADATA.length - 1 && <hr className="config-divider" />}
          </div>
        ))}

        <div className="config-footer">
          <div className="config-footer-bar config-footer-bar--skeleton">
            <div className="config-footer-left">
              <div className="skeleton-line" style={{ width: '88px', height: '10px' }} />
              <div className="skeleton-line" style={{ width: '56px', height: '18px', marginTop: '8px' }} />
            </div>
            <div className="config-footer-actions">
              <div className="skeleton-number" style={{ width: '100px', height: '38px' }} />
              <div className="skeleton-number" style={{ width: '132px', height: '38px' }} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * 主 Config 组件
 */
function Config() {
  /** 加载成功后才为对象；失败时为 null，不套用 DEFAULT_CONFIG 冒充服务端数据 */
  const [config, setConfig] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [lastSaved, setLastSaved] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [toast, setToast] = useState(null); // { message, type }
  const [showConfirm, setShowConfirm] = useState(false);
  const [savingTelegramKey, setSavingTelegramKey] = useState(null);

  // 显示 toast
  const showToast = useCallback((message, type = 'success') => {
    setToast({ message, type });
  }, []);

  const hideToast = useCallback(() => {
    setToast(null);
  }, []);

  const fetchConfig = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const response = await apiFetch('/api/config/config');
      const data = await response.json();
      if (response.ok && data.success && data.data) {
        const merged = mergeConfigApiPayload(data.data);
        if (merged) {
          setConfig(merged.params);
          setLastSaved(merged.lastSaved);
        }
      } else {
        setConfig(null);
        setLoadError(
          data.message ||
            (!response.ok
              ? `请求失败（HTTP ${response.status}）`
              : '获取配置失败，请稍后重试')
        );
      }
    } catch (error) {
      console.error('获取配置失败:', error);
      setConfig(null);
      setLoadError(
        error instanceof Error ? error.message : '网络错误，无法加载配置'
      );
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchConfig();
  }, [fetchConfig]);

  // 处理配置变更（自动 clamp 到范围）
  const handleConfigChange = (key, rawValue) => {
    const meta = CONFIG_METADATA.find(item => item.key === key);
    const num = Number(rawValue);
    if (isNaN(num)) return;
    const step = meta.step || 1;
    const rounded = Math.round(num / step) * step;
    const clamped = Math.max(meta.min, Math.min(meta.max, rounded));
    setConfig(prev => ({ ...prev, [key]: clamped }));
    setHasUnsavedChanges(true);
  };

  // 数字输入框 blur 时强制 clamp
  const handleNumberBlur = (key, rawValue) => {
    const meta = CONFIG_METADATA.find(item => item.key === key);
    const num = Number(rawValue);
    const step = meta.step || 1;
    const clamped = isNaN(num)
      ? DEFAULT_CONFIG[key]
      : Math.max(meta.min, Math.min(meta.max, Math.round(num / step) * step));
    setConfig(prev => ({ ...prev, [key]: clamped }));
  };

  // 重置默认值（二次确认）
  const handleResetConfirm = () => {
    setConfig(DEFAULT_CONFIG);
    setHasUnsavedChanges(true);
    setShowConfirm(false);
    showToast('已恢复默认值，记得保存', 'success');
  };

  const handleTelegramFieldChange = (key, rawValue) => {
    const row = TELEGRAM_CONFIG_ROWS.find((r) => r.key === key);
    if (!row) return;
    const num = Number(rawValue);
    if (Number.isNaN(num)) return;
    const v = Math.max(row.min, Math.min(row.max, Math.round(num)));
    setConfig((prev) => ({ ...prev, [key]: v }));
    setHasUnsavedChanges(true);
  };

  const handleTelegramBlur = (key, rawValue) => {
    const v =
      key === 'telegram_max_chars'
        ? clampTelegramChars(rawValue)
        : clampTelegramMsg(rawValue);
    setConfig((prev) => ({ ...prev, [key]: v }));
  };

  const adjustTelegram = (key, delta) => {
    const row = TELEGRAM_CONFIG_ROWS.find((r) => r.key === key);
    if (!row) return;
    const cur = config[key];
    const next =
      key === 'telegram_max_chars'
        ? clampTelegramChars(cur + delta)
        : clampTelegramMsg(cur + delta);
    setConfig((prev) => ({ ...prev, [key]: next }));
    setHasUnsavedChanges(true);
  };

  /** 单行 PUT /api/config/config，仅提交一个 key */
  const saveTelegramRow = async (key) => {
    if (!config) return;
    const v =
      key === 'telegram_max_chars'
        ? clampTelegramChars(config[key])
        : clampTelegramMsg(config[key]);
    setConfig((prev) => ({ ...prev, [key]: v }));
    setSavingTelegramKey(key);
    try {
      const response = await apiFetch('/api/config/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: v }),
      });
      const data = await response.json();
      if (response.ok && data.success) {
        showToast('✓ 已保存', 'success');
        await fetchConfig();
      } else {
        showToast(data.message || '保存失败', 'error');
      }
    } catch (error) {
      console.error('Telegram 配置保存失败:', error);
      showToast('网络错误', 'error');
    } finally {
      setSavingTelegramKey(null);
    }
  };

  // 切换线下模式
  const handleToggleOfflineMode = async () => {
    if (isSaving || !config) return;
    const isCurrentlyActive = parseInt(config.offline_mode_active || '0', 10) === 1;
    const enable = !isCurrentlyActive;
    
    setIsSaving(true);
    try {
      const response = await apiFetch('/api/config/offline-mode/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enable })
      });
      const data = await response.json();
      if (response.ok && data.success) {
        showToast(data.message || (enable ? '线下模式已开启' : '线下模式已关闭'), 'success');
        await fetchConfig();
        setHasUnsavedChanges(false);
      } else {
        throw new Error(data.message || '切换失败');
      }
    } catch (error) {
      console.error('切换线下模式失败:', error);
      showToast(`切换失败：${error.message}`, 'error');
    } finally {
      setIsSaving(false);
    }
  };

  // 保存配置
  const handleSave = async () => {
    if (isSaving) return;
    setIsSaving(true);
    try {
      const response = await apiFetch('/api/config/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config)
      });

      const data = await response.json();

      if (response.ok && data.success) {
        const merged = data.data ? mergeConfigApiPayload(data.data) : null;
        if (merged) {
          setConfig(merged.params);
          // 保存成功后直接使用前端当前的本地时间，这样最准确且不会有时区问题
          setLastSaved(new Date());
        } else {
          setLastSaved(new Date());
        }
        setHasUnsavedChanges(false);
        showToast('✓ 配置已生效', 'success');
      } else {
        throw new Error(data.message || '保存失败');
      }
    } catch (error) {
      console.error('保存配置失败:', error);
      showToast(`保存失败：${error.message}`, 'error');
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return <ConfigSkeleton />;
  }

  const loadErrorBannerStyle = {
    width: '100%',
    padding: '12px 16px',
    marginBottom: '8px',
    borderRadius: '8px',
    border: '1px solid #fecaca',
    background: '#fef2f2',
    color: '#b91c1c',
    fontSize: '0.9rem',
    lineHeight: 1.5,
    display: 'flex',
    flexWrap: 'wrap',
    alignItems: 'center',
    gap: '12px',
    boxSizing: 'border-box'
  };

  const retryBtnStyle = {
    padding: '6px 14px',
    fontSize: '0.85rem',
    cursor: 'pointer',
    borderRadius: '6px',
    border: '1px solid #b91c1c',
    background: '#fff',
    color: '#b91c1c',
    fontWeight: 600
  };

  return (
    <div className="config-container">
      {/* 加载失败：页面上方红色提示，不使用本地默认值冒充数据库配置 */}
      {loadError && (
        <div role="alert" style={loadErrorBannerStyle}>
          <span style={{ flex: '1 1 200px' }}>{loadError}</span>
          <button type="button" style={retryBtnStyle} onClick={() => fetchConfig()}>
            重新加载
          </button>
        </div>
      )}

      {/* Toast 提示 */}
      {toast && (
        <Toast
          message={toast.message}
          type={toast.type}
          onClose={hideToast}
        />
      )}

      {/* 重置确认弹窗 */}
      {showConfirm && config && (
        <ConfirmDialog
          title="重置默认值"
          desc="确定要把所有参数恢复为默认值吗？此操作不会自动保存，需手动点击保存。"
          onConfirm={handleResetConfirm}
          onCancel={() => setShowConfirm(false)}
        />
      )}

      {/* 仅成功拉取配置后展示表单 */}
      {config && (
      <div className="config-card">
        <header className="config-card-header">
          <h1 className="config-card-title">
            <span className="config-card-title__prefix" aria-hidden="true">
              ■
            </span>
            <span className="config-card-title__text">助手配置</span>
          </h1>
          <p className="config-card-subtitle">
            <span className="config-card-subtitle__prompt">[INFO]</span>
            修改后点击保存即时生效，无需重启服务
          </p>
        </header>

        {/* 线下模式开关 */}
        <div className="config-item">
          <div className="config-info">
            <div className="config-name">线下极速模式</div>
            <div className="config-desc">一键进入极速响应状态（延迟 1s，独立消息不分段）。</div>
          </div>
          <div className="config-controls config-controls--telegram-row" style={{ flexWrap: 'wrap', gap: '16px', justifyContent: 'flex-end', flex: 1 }}>
            <button
              type="button"
              className={`config-offline-toggle ${
                parseInt(config.offline_mode_active || '0', 10) === 1 ? 'is-on' : ''
              }`}
              onClick={handleToggleOfflineMode}
              disabled={isSaving}
            >
              {parseInt(config.offline_mode_active || '0', 10) === 1 ? '[ ACTIVE ]' : '点击开启'}
            </button>
          </div>
        </div>
        <hr className="config-divider" />

        <div className="config-item">
          <div className="config-info">
            <div className="config-name">群聊静默模式</div>
            <div className="config-desc">开启后群聊消息一律不回复，/wake 指令除外。</div>
          </div>
          <div className="config-controls config-controls--telegram-row" style={{ flexWrap: 'wrap', gap: '16px' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', flex: 1 }}>
              <input
                type="checkbox"
                style={{ width: '18px', height: '18px', cursor: 'pointer' }}
                checked={config.group_chat_silent_mode === 1}
                onChange={(e) => {
                  setConfig((prev) => ({ ...prev, group_chat_silent_mode: e.target.checked ? 1 : 0 }));
                  setHasUnsavedChanges(true);
                }}
              />
              <span style={{ fontSize: '0.95rem', color: '#374151', fontWeight: 500 }}>静默</span>
            </label>
          </div>
        </div>
        <hr className="config-divider" />

        <div className="config-item">
          <div className="config-info">
            <div className="config-name">群聊随机插话</div>
            <div className="config-desc">另一 Bot 发言后按概率插话；每次插话消耗 2 轮额度。</div>
          </div>
          <div className="config-controls config-controls--telegram-row" style={{ flexWrap: 'wrap', gap: '16px' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', flex: 1 }}>
              <input
                type="checkbox"
                style={{ width: '18px', height: '18px', cursor: 'pointer' }}
                checked={config.group_chat_interject_enabled === 1}
                onChange={(e) => {
                  setConfig((prev) => ({ ...prev, group_chat_interject_enabled: e.target.checked ? 1 : 0 }));
                  setHasUnsavedChanges(true);
                }}
              />
              <span style={{ fontSize: '0.95rem', color: '#374151', fontWeight: 500 }}>允许插话</span>
            </label>
          </div>
        </div>
        <hr className="config-divider" />

        {CONFIG_METADATA.map((item, index) => (
          <div key={item.key}>
            <div className="config-item">
              {/* 左侧：参数名 + 说明 */}
              <div className="config-info">
                <div className="config-name">{item.name}</div>
                <div className="config-desc">{item.description}</div>
                {item.hint ? (
                  <div className="config-hint">{item.hint}</div>
                ) : null}
              </div>

              {/* 右侧：滑块 + 数字输入框 */}
              <div className="config-controls">
                <input
                  type="range"
                  className="config-slider"
                  min={item.min}
                  max={item.max}
                  step={item.step || 1}
                  value={config[item.key]}
                  onChange={e => handleConfigChange(item.key, e.target.value)}
                />
                <div className="config-number-wrapper">
                  <button 
                    className="config-stepper-btn" 
                    onClick={() => handleConfigChange(item.key, Number(config[item.key]) - (item.step || 1))}
                    disabled={config[item.key] <= item.min}
                    aria-label="减少"
                  >
                    -
                  </button>
                  <input
                    type="number"
                    inputMode="numeric"
                    className="config-number-input"
                    min={item.min}
                    max={item.max}
                    step={item.step || 1}
                    value={config[item.key]}
                    onChange={e => handleConfigChange(item.key, e.target.value)}
                    onBlur={e => handleNumberBlur(item.key, e.target.value)}
                  />
                  <button 
                    className="config-stepper-btn" 
                    onClick={() => handleConfigChange(item.key, Number(config[item.key]) + (item.step || 1))}
                    disabled={config[item.key] >= item.max}
                    aria-label="增加"
                  >
                    +
                  </button>
                </div>
              </div>
            </div>

            {index < CONFIG_METADATA.length - 1 && <hr className="config-divider" />}
          </div>
        ))}

        <hr className="config-divider" />
        <div className="config-telegram-section">
          <div className="config-telegram-section-header">
            <div className="config-name">Telegram 参数</div>
            <div className="config-desc">
              流式与分段设置；每项修改后可点「保存此项」单独提交
            </div>
          </div>

          <div className="config-item">
            <div className="config-info">
              <div className="config-name">发送思维链</div>
              <div className="config-desc">是否将长考模型的思维链内容发送至 Telegram 对话。</div>
            </div>
            <div className="config-controls config-controls--telegram-row" style={{ flexWrap: 'wrap', gap: '16px' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', flex: 1 }}>
                <input
                  type="checkbox"
                  style={{ width: '18px', height: '18px', cursor: 'pointer' }}
                  checked={config.send_cot_to_telegram === 1}
                  onChange={(e) => {
                    const val = e.target.checked ? 1 : 0;
                    setConfig((prev) => ({ ...prev, send_cot_to_telegram: val }));
                    setHasUnsavedChanges(true);
                  }}
                />
                <span style={{ fontSize: '0.95rem', color: '#374151', fontWeight: 500 }}>启用思维链显示</span>
              </label>
              <button
                type="button"
                className="config-btn-secondary config-btn-telegram-inline-save"
                onClick={async () => {
                  const val = config.send_cot_to_telegram;
                  setSavingTelegramKey('send_cot_to_telegram');
                  try {
                    const response = await apiFetch('/api/config/config', {
                      method: 'PUT',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ send_cot_to_telegram: val }),
                    });
                    const data = await response.json();
                    if (data.success) {
                      showToast('✓ 已保存', 'success');
                      setHasUnsavedChanges(false);
                    } else showToast('保存失败', 'error');
                  } catch(e) {
                    showToast('网络错误', 'error');
                  } finally {
                    setSavingTelegramKey(null);
                  }
                }}
                disabled={savingTelegramKey === 'send_cot_to_telegram'}
              >
                {savingTelegramKey === 'send_cot_to_telegram' ? '保存中…' : '保存此项'}
              </button>
            </div>
          </div>
          <hr className="config-divider" />

          {TELEGRAM_CONFIG_ROWS.map((row, idx) => (
            <div key={row.key}>
              <div className="config-item">
                <div className="config-info">
                  <div className="config-name">{row.name}</div>
                  <div className="config-desc">{row.description}</div>
                </div>
                <div className="config-controls config-controls--telegram-row">
                  <div className="config-slider-spacer" aria-hidden="true" />
                  <div className="config-number-wrapper">
                    <button
                      type="button"
                      className="config-stepper-btn"
                      onClick={() => adjustTelegram(row.key, -row.step)}
                      disabled={config[row.key] <= row.min}
                      aria-label="减少"
                    >
                      -
                    </button>
                    <input
                      type="number"
                      inputMode="numeric"
                      className="config-number-input config-number-input--telegram"
                      min={row.min}
                      max={row.max}
                      step={row.step}
                      value={config[row.key]}
                      onChange={(e) => handleTelegramFieldChange(row.key, e.target.value)}
                      onBlur={(e) => handleTelegramBlur(row.key, e.target.value)}
                    />
                    <button
                      type="button"
                      className="config-stepper-btn"
                      onClick={() => adjustTelegram(row.key, row.step)}
                      disabled={config[row.key] >= row.max}
                      aria-label="增加"
                    >
                      +
                    </button>
                  </div>
                  <button
                    type="button"
                    className="config-btn-secondary config-btn-telegram-inline-save"
                    onClick={() => saveTelegramRow(row.key)}
                    disabled={savingTelegramKey === row.key}
                  >
                    {savingTelegramKey === row.key ? '保存中…' : '保存此项'}
                  </button>
                </div>
              </div>
              {idx < TELEGRAM_CONFIG_ROWS.length - 1 && <hr className="config-divider" />}
            </div>
          ))}
        </div>

        {/* 底部操作栏：左信息 / 右按钮组 */}
        <div className="config-footer">
          <div className="config-footer-bar">
            <div className="config-footer-left">
              <span className="config-footer-saved-label">上次保存时间</span>
              <span className="config-footer-saved-time">
                {lastSaved
                  ? lastSaved.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
                  : '尚未保存'}
              </span>
            </div>

            <div className="config-footer-right">
              <div className="config-footer-actions">
                <span className="config-reset-wrap">
                  <button
                    type="button"
                    className="config-btn-secondary config-btn-footer-secondary"
                    onClick={() => setShowConfirm(true)}
                    disabled={isSaving}
                    title="重置为系统默认值，可能与当前数据库配置不同"
                  >
                    重置默认值
                  </button>
                  <span className="config-reset-tooltip" aria-hidden="true">
                    重置为系统默认值，可能与当前数据库配置不同
                  </span>
                </span>
                <button
                  type="button"
                  className={`config-btn-primary config-btn-footer-primary${hasUnsavedChanges ? ' has-changes' : ''}`}
                  onClick={handleSave}
                  disabled={!hasUnsavedChanges || isSaving}
                >
                  {isSaving ? '保存中…' : '保存并立即生效'}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
      )}
    </div>
  );
}

export default Config;
