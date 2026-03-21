/**
 * 助手配置页面
 * 提供五个核心参数的滑块 + 数字输入框双向联动配置
 */

import { useState, useEffect, useCallback } from 'react';
import '../styles/config.css';

// 配置项默认值
const DEFAULT_CONFIG = {
  short_term_limit: 40,
  buffer_delay: 15,
  chunk_threshold: 50,
  longterm_score_threshold: 7,
  reranker_top_n: 2
};

// 配置项元数据
const CONFIG_METADATA = [
  {
    key: 'short_term_limit',
    name: '短期记忆携带量',
    description: '每次发给 AI 的最近原文消息条数',
    min: 10,
    max: 200
  },
  {
    key: 'buffer_delay',
    name: '消息缓冲延迟',
    description: '连发短消息的合并等待时间（秒）',
    min: 3,
    max: 100
  },
  {
    key: 'chunk_threshold',
    name: 'Chunk 触发阈值',
    description: '多少条消息触发一次日内微批总结',
    min: 20,
    max: 100
  },
  {
    key: 'longterm_score_threshold',
    name: '长期记忆价值阈值',
    description: 'Daily 摘要打分多少分以上才归档',
    min: 1,
    max: 10
  },
  {
    key: 'reranker_top_n',
    name: 'Reranker 返回数量',
    description: '重排后保留的记忆片段数量',
    min: 1,
    max: 5
  }
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
        <div className="config-card-title">助手配置</div>
        <div className="config-card-subtitle">修改后点击保存即时生效，无需重启服务</div>

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
          <div className="skeleton-line" style={{ width: '160px', height: '12px' }}></div>
          <div className="config-buttons">
            <div className="skeleton-number" style={{ width: '100px' }}></div>
            <div className="skeleton-number" style={{ width: '130px' }}></div>
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
      const response = await fetch('/api/config/config');
      const data = await response.json();
      if (response.ok && data.success && data.data) {
        setConfig({ ...DEFAULT_CONFIG, ...data.data });
        setLastSaved(new Date());
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
    const clamped = Math.max(meta.min, Math.min(meta.max, Math.round(num)));
    setConfig(prev => ({ ...prev, [key]: clamped }));
    setHasUnsavedChanges(true);
  };

  // 数字输入框 blur 时强制 clamp
  const handleNumberBlur = (key, rawValue) => {
    const meta = CONFIG_METADATA.find(item => item.key === key);
    const num = Number(rawValue);
    const clamped = isNaN(num)
      ? DEFAULT_CONFIG[key]
      : Math.max(meta.min, Math.min(meta.max, Math.round(num)));
    setConfig(prev => ({ ...prev, [key]: clamped }));
  };

  // 重置默认值（二次确认）
  const handleResetConfirm = () => {
    setConfig(DEFAULT_CONFIG);
    setHasUnsavedChanges(true);
    setShowConfirm(false);
    showToast('已恢复默认值，记得保存', 'success');
  };

  // 保存配置
  const handleSave = async () => {
    if (isSaving) return;
    setIsSaving(true);
    try {
      const response = await fetch('/api/config/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config)
      });

      const data = await response.json();

      if (response.ok && data.success) {
        setLastSaved(new Date());
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
        <div className="config-card-title">助手配置</div>
        <div className="config-card-subtitle">修改后点击保存即时生效，无需重启服务</div>

        {CONFIG_METADATA.map((item, index) => (
          <div key={item.key}>
            <div className="config-item">
              {/* 左侧：参数名 + 说明 */}
              <div className="config-info">
                <div className="config-name">{item.name}</div>
                <div className="config-desc">{item.description}</div>
              </div>

              {/* 右侧：滑块 + 数字输入框 */}
              <div className="config-controls">
                <input
                  type="range"
                  className="config-slider"
                  min={item.min}
                  max={item.max}
                  value={config[item.key]}
                  onChange={e => handleConfigChange(item.key, e.target.value)}
                />
                <input
                  type="number"
                  className="config-number-input"
                  min={item.min}
                  max={item.max}
                  value={config[item.key]}
                  onChange={e => handleConfigChange(item.key, e.target.value)}
                  onBlur={e => handleNumberBlur(item.key, e.target.value)}
                />
              </div>
            </div>

            {index < CONFIG_METADATA.length - 1 && <hr className="config-divider" />}
          </div>
        ))}

        {/* 底部操作栏 */}
        <div className="config-footer">
          <div className="config-last-saved">
            {lastSaved
              ? `上次保存时间：${lastSaved.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`
              : '尚未保存'}
          </div>

          <div className="config-buttons">
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'flex-start',
                gap: '6px'
              }}
            >
              <button
                className="config-btn-secondary"
                onClick={() => setShowConfirm(true)}
                disabled={isSaving}
              >
                重置默认值
              </button>
              <span
                style={{
                  fontSize: '0.72rem',
                  color: 'var(--text-sub)',
                  lineHeight: 1.35,
                  maxWidth: '15rem'
                }}
              >
                重置为系统默认值，可能与当前数据库配置不同
              </span>
            </div>
            <button
              className={`config-btn-primary${hasUnsavedChanges ? ' has-changes' : ''}`}
              onClick={handleSave}
              disabled={!hasUnsavedChanges || isSaving}
            >
              {isSaving ? '保存中…' : '保存并立即生效'}
            </button>
          </div>
        </div>
      </div>
      )}
    </div>
  );
}

export default Config;
